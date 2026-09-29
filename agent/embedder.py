"""The search stage: one vector per photo, from its thumbnail.

The thumbnail rather than the original, on purpose: the model looks at 224
pixels, the thumbnail has 400 on its short edge, and it is already decoded,
rotated and flattened onto white, so this stage never opens a HEIC and never
disagrees with the grid about which way up a photo is. It also means a
video is searchable by the frame the grid shows for it.

Companions (the video half of a Live Photo) get no vector: they are never
listed, and the still beside them is the same moment.
"""

from __future__ import annotations

from sqlalchemy import and_, exists, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from core import embed
from core.media import thumb_path
from core.models import Photo, PhotoEmbedding
from core.timeutil import utcnow

from . import stages

BATCH = 32


def pending_query(model: str):
    have = exists().where(
        and_(PhotoEmbedding.photo_id == Photo.id, PhotoEmbedding.model == model,
             PhotoEmbedding.sig == Photo.sig)
    )
    return select(Photo.id, Photo.sig).where(
        Photo.thumb_sig == Photo.sig, Photo.is_companion.is_(False), ~have
    )


def run_batch(
    db: Session,
    only_ids: frozenset[int] | None = None,
    limit: int = BATCH,
    cooldown: stages.Cooldown | None = None,
) -> int:
    """Embed up to ``limit`` photos, newest first. Returns how many were done.

    Raises only when the model itself fails (a download, a broken install):
    that is not any one photo's fault and must not count against them.
    """
    from PIL import Image

    model = embed.model_name()
    exclude = cooldown.ids() if cooldown else ()
    todo = db.execute(stages.newest(pending_query(model), only_ids, limit, exclude)).all()
    if not todo:
        return 0

    images, ready = [], []
    lost = []
    for row in todo:
        path = thumb_path(row.sig)
        try:
            with Image.open(path) as img:
                images.append(img.convert("RGB"))
            ready.append(row)
        except FileNotFoundError:
            lost.append(row.id)
        except Exception as exc:  # noqa: BLE001 - a truncated webp: draw it again
            stages.failed(db, row.id, row.sig, "embeddings", exc)
            if cooldown:
                cooldown.add(row.id)
            lost.append(row.id)
    if lost:
        # The cache was cleared under us, or the file is damaged: the thumb
        # stage redraws it and this one comes back to it afterwards.
        db.execute(update(Photo).where(Photo.id.in_(lost)).values(thumb_sig=""))
    if not ready:
        db.commit()
        return 0

    vectors = embed.embed_images(images, batch_size=BATCH)
    now = utcnow()
    values = [
        {"photo_id": row.id, "model": model, "sig": row.sig, "embedding": vec, "created_at": now}
        for row, vec in zip(ready, vectors)
    ]
    stmt = pg_insert(PhotoEmbedding).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[PhotoEmbedding.photo_id],
        set_={"model": stmt.excluded.model, "sig": stmt.excluded.sig,
              "embedding": stmt.excluded.embedding, "created_at": stmt.excluded.created_at},
    )
    db.execute(stmt)
    stages.cleared(db, [row.id for row in ready], "embeddings")
    db.commit()
    return len(ready)
