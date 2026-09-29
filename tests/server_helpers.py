"""What the server's tests share: a database to seed, and photos to seed it with.

The database tests run against ``MEERPIC_TEST_DB`` and skip without it (see
conftest.py), because what they test is Postgres' part of the work: vector
distance, keyset comparison of row values, JSONB, trigram ILIKE. Rows are
written straight through the ORM, the way the agent writes them, so these
tests do not depend on the agent at all; files are drawn with Pillow into the
temporary library and cache that conftest.py points the settings at.

The helpers are context managers rather than fixtures, and each test module
declares the two or three fixtures it wants in terms of them. Importing a
fixture by name and then naming it as a parameter is a redefinition as far as
any linter can tell, and ``pytest_plugins`` would make ``db`` and ``client``
global, where they could collide with another suite's fixtures of the same
name.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

needs_db = pytest.mark.skipif(not os.environ.get("MEERPIC_TEST_DB"), reason="MEERPIC_TEST_DB is not set")

ROOT = "icloud"
_serial = itertools.count(1)

RED, GREEN, BLUE, YELLOW, WHITE, BLACK = (
    (255, 0, 0), (0, 160, 0), (0, 0, 255), (255, 230, 0), (255, 255, 255), (0, 0, 0),
)


def wall(y: int, mo: int, d: int, h: int = 12, mi: int = 0) -> datetime:
    """A naive wall clock, the shape of ``taken_local``."""
    return datetime.combine(date(y, mo, d), time(h, mi))


def library() -> Path:
    from core.config import get_settings

    path = get_settings().root_path(ROOT)
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- the database -------------------------------------------------------------


def truncate() -> None:
    """Empty every table; ids start at 1 again."""
    from sqlalchemy import text

    from core.database import engine

    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE photo_albums, albums, photo_embeddings, jobs, settings, deleted_files, photos"
            " RESTART IDENTITY CASCADE"
        ))


@contextmanager
def running_app() -> Iterator:
    """The app, its lifespan run (init_db), and an empty database."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        truncate()
        yield c


@contextmanager
def session() -> Iterator:
    from core.database import SessionLocal

    with SessionLocal() as s:
        yield s


# --- photos -------------------------------------------------------------------


def make_photo(db, name: str | None = None, *, taken: datetime | None | bool = True, **columns):
    """A photo row as the agent would leave it once every stage has run.

    ``taken`` is the wall clock (``taken_local``); the instant is derived in
    the test zone (Europe/Berlin, see conftest.py) unless ``tz_offset`` is
    given. ``taken=None`` makes an undated photo. Any column can be
    overridden by keyword.
    """
    from core.config import get_settings
    from core.media import classify, make_sig
    from core.models import UNDATED, Photo
    from core.timeutil import home_zone, wall_to_utc

    n = next(_serial)
    name = name or f"IMG_{n:05d}.JPG"
    ext, kind, mime = classify(name)
    rel_path = columns.pop("rel_path", name)
    size = columns.pop("size", 1000 + n)
    mtime_ns = columns.pop("mtime_ns", 1_700_000_000_000_000_000 + n)
    sig = make_sig(ROOT, rel_path, size, mtime_ns)
    if taken is True:
        taken = wall(2024, 1, 1) + timedelta(hours=n)
    tz_offset = columns.get("tz_offset")
    if taken is None:
        taken_at = None
    elif tz_offset is not None:
        taken_at = taken - timedelta(minutes=tz_offset)
    else:
        taken_at = wall_to_utc(taken, home_zone(get_settings().timezone))
    values = {
        "root": ROOT, "rel_path": rel_path, "name": name, "ext": ext, "kind": kind,
        "mime": mime, "size": size, "mtime_ns": mtime_ns, "sig": sig,
        "taken_at": taken_at, "taken_local": taken,
        "date_source": "exif" if taken else "none",
        "sort_at": taken_at or UNDATED,
        "width": 4032, "height": 3024,
        "meta_sig": sig, "thumb_sig": sig, "preview_sig": "",
    }
    values.update(columns)
    photo = Photo(**values)
    db.add(photo)
    db.flush()
    return photo


def image_bytes(color=RED, size=(64, 48), fmt="JPEG", exif=None, mode="RGB", **save) -> bytes:
    """An image file's bytes: one colour, any format Pillow (and pillow-heif,
    for "HEIF") can write, with ``exif`` (a PIL.Image.Exif) when given."""
    import pillow_heif

    pillow_heif.register_heif_opener()
    img = Image.new(mode, size, color)
    if exif is not None:
        save["exif"] = exif.tobytes()
    buf = BytesIO()
    img.save(buf, fmt, **save)
    return buf.getvalue()


def add_file(db, name: str, data: bytes, **columns):
    """Write ``data`` into the library as ``name`` and add its row."""
    path = library() / columns.get("rel_path", name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    columns.setdefault("size", len(data))
    return make_photo(db, name, **columns)


def write_thumb(photo, color=RED) -> Path:
    """The agent's thumbnail for ``photo``, where the agent would put it."""
    from core.media import thumb_path

    path = thumb_path(photo.sig)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), color).save(path, "WEBP")
    return path


def write_preview(photo, data: bytes = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 40) -> Path:
    """A stand-in preview mp4 (bytes only: nothing here decodes it)."""
    from core.media import preview_path

    path = preview_path(photo.sig)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    photo.preview_sig = photo.sig
    return path


def write_story(photo, frames: int = 10) -> Path:
    """The agent's storyboard for a video: ``frames`` frames side by side."""
    from core.media import STORY_HEIGHT, story_path

    path = story_path(photo.sig)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32 * frames, STORY_HEIGHT), (40, 40, 40)).save(path, "WEBP")
    photo.story_sig = photo.sig
    photo.story_frames = frames
    return path


def color_vector(*colors):
    """The fake model's vector for an image of horizontal colour bands.

    One colour is a solid image; two are a top and a bottom half, which sits
    between the two solids in similarity, so rankings have an order to check.
    """
    from core import embed

    img = Image.new("RGB", (4, 4 * len(colors)))
    for i, color in enumerate(colors):
        img.paste(color, (0, 4 * i, 4, 4 * (i + 1)))
    return embed.embed_images([img])[0]


def add_album(db, name: str, members=(), *, remote_count: int | None = None, kind: str | None = None):
    """An album as the agent's albums job leaves it: a row, and its members."""
    from core.models import SMART_ALBUMS, Album, PhotoAlbum

    album = Album(
        name=name,
        kind=kind or ("smart" if name in SMART_ALBUMS else "user"),
        count=len(members),
        remote_count=len(members) if remote_count is None else remote_count,
    )
    db.add(album)
    db.flush()
    for photo in members:
        db.add(PhotoAlbum(album_id=album.id, photo_id=photo.id))
    db.flush()
    return album


def add_embedding(db, photo, vector) -> None:
    from core import embed
    from core.models import PhotoEmbedding

    db.add(PhotoEmbedding(photo_id=photo.id, model=embed.model_name(), sig=photo.sig, embedding=vector))
    db.flush()
