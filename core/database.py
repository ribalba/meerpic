"""Engine, session factory and the schema bootstrap.

The agent and the web app both import this: Postgres is the only channel
between them, so it is also the only thing they share besides `core`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

log = logging.getLogger("meerpic.db")

settings = get_settings()

engine = create_engine(
    settings.database_url,
    future=True,
    # The agent holds a connection across long indexing passes; both outlive
    # whatever a firewall considers idle.
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def wait_for_db(timeout: float = 60.0) -> None:
    """Block until Postgres answers, or give up loudly.

    Compose starts the database and the app together and `depends_on` only
    waits for the container, not for the server inside it.
    """
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except OperationalError as exc:  # not up yet
            last = exc
            time.sleep(1.0)
    raise SystemExit(f"meerpic: database not reachable at {settings.database_url!r}: {last}")


def _embedding_dim(conn) -> int | None:
    """The dimension the embeddings column was created with, if it exists."""
    row = conn.execute(text(
        "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "WHERE c.relname = 'photo_embeddings' AND a.attname = 'embedding' AND NOT a.attisdropped"
    )).scalar()
    if not row or "(" not in row:
        return None
    return int(row.split("(", 1)[1].rstrip(")"))


# Everything SQLAlchemy's create_all cannot express: indexes it has no words
# for, and columns that arrived after the first libraries were indexed
# (create_all only ever adds whole tables). This is a schema bootstrap, not a
# migration framework, and the project is young enough not to need one yet.
#
# Each item is created only when the catalog says it is missing. `ALTER TABLE
# ... ADD COLUMN IF NOT EXISTS` is not a no-op when the column exists: it
# takes an ACCESS EXCLUSIVE lock on the table first and checks after, so on
# every start it queued behind whatever the agent had open and every query on
# `photos` queued behind it. A catalog lookup takes no lock at all.
_INDEXES = (
    # Nearest-neighbour search over the photo vectors. HNSW rather than
    # IVFFlat because it needs no training pass over existing rows and stays
    # good as the library grows, which a library does one photo at a time.
    ("ix_emb_hnsw",
     "CREATE INDEX ix_emb_hnsw ON photo_embeddings USING hnsw (embedding vector_cosine_ops)"),
    # `file:` and `in:` search with ILIKE '%...%', which a btree cannot help.
    ("ix_photos_name_trgm", "CREATE INDEX ix_photos_name_trgm ON photos USING gin (name gin_trgm_ops)"),
    ("ix_photos_place_trgm", "CREATE INDEX ix_photos_place_trgm ON photos USING gin (place gin_trgm_ops)"),
    # The grid's one query: newest first, companions (the video half of a
    # Live Photo) left out. Keyset pagination walks this index and nothing else.
    ("ix_photos_timeline",
     "CREATE INDEX ix_photos_timeline ON photos (sort_at DESC, id DESC) WHERE NOT is_companion"),
    # The map asks one thing: what is inside this box.
    ("ix_photos_geo", "CREATE INDEX ix_photos_geo ON photos (lat, lon) WHERE lat IS NOT NULL"),
    ("ix_photos_superseded_by", "CREATE INDEX ix_photos_superseded_by ON photos (superseded_by)"),
    # `is:nsfw` is a range over this, and the answer is a few dozen rows.
    ("ix_photos_nsfw", "CREATE INDEX ix_photos_nsfw ON photos (nsfw DESC) WHERE nsfw IS NOT NULL"),
    # The photos marked safe, which every flagged photo is compared with: a
    # handful out of the whole library.
    ("ix_photos_nsfw_safe", "CREATE INDEX ix_photos_nsfw_safe ON photos (id) WHERE nsfw_safe"),
    # `text:` is ILIKE '%...%' over what the photos say.
    ("ix_photo_texts_trgm",
     "CREATE INDEX ix_photo_texts_trgm ON photo_texts USING gin (text gin_trgm_ops)"),
)

_COLUMNS = (
    ("superseded_by", "INTEGER REFERENCES photos(id) ON DELETE SET NULL"),
    ("favorite", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("hidden", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("icloud_deleted", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("added_at", "TIMESTAMP"),
    ("trashed_at", "TIMESTAMP"),
    ("nsfw", "DOUBLE PRECISION"),
    ("nsfw_safe", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("story_sig", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ("story_frames", "INTEGER NOT NULL DEFAULT 0"),
    ("nsfw_sig", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ("faces_sig", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ("ocr_sig", "VARCHAR(16) NOT NULL DEFAULT ''"),
)

# When a migration does have to run, it waits this long for the table and
# then says so, rather than hanging a startup behind a long transaction.
_LOCK_TIMEOUT = "15s"


def _missing_ddl(conn) -> list[str]:
    have_cols = set(conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'photos'"
    )).scalars())
    have_idx = set(conn.execute(text(
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
    )).scalars())
    todo = [f"ALTER TABLE photos ADD COLUMN {name} {ddl}" for name, ddl in _COLUMNS if name not in have_cols]
    todo += [ddl for name, ddl in _INDEXES if name not in have_idx]
    return todo


# One schema bootstrap at a time. The server and the agent start together and
# both run init_db; two create_all calls racing to add the same new table end
# in a duplicate-key error on pg_type for the loser, which then crash-loops
# until its restart happens to come second. A session-level advisory lock on
# a connection of its own makes the second one wait for the first and then
# find nothing left to do.
_INIT_LOCK = 0x6D72_7030     # "mrp0"


def init_db() -> None:
    wait_for_db()
    with engine.connect() as guard:
        guard.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _INIT_LOCK})
        try:
            _init_db()
        finally:
            guard.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _INIT_LOCK})
            guard.commit()


def _init_db() -> None:
    # `models` is imported for its side effect: it registers the mappers
    # that create_all creates.
    from . import embed, models  # noqa: F401

    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        # A different model with a different dimension cannot share the
        # column: every stored vector is meaningless to it. Dropping the table
        # is the whole migration, and the agent re-embeds from the thumbnails,
        # which is minutes, not the hours the first index took.
        have = _embedding_dim(conn)
        if have is not None and have != embed.dim():
            log.warning("embedding dimension %s -> %s: dropping stored vectors", have, embed.dim())
            conn.execute(text("DROP TABLE photo_embeddings"))
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        todo = _missing_ddl(conn)
    if not todo:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
            for stmt in todo:
                log.info("schema: %s", stmt)
                conn.execute(text(stmt))
    except OperationalError as exc:
        raise SystemExit(
            f"meerpic: could not update the database schema within {_LOCK_TIMEOUT}: "
            f"another process holds the photos table ({exc.orig}). Stop the agent and "
            "the server, then start again."
        ) from exc
