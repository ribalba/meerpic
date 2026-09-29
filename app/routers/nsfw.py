"""Marking pictures safe: the classifier's flag, overruled by a person.

``POST /api/photos/safe`` with ``{"ids": [...], "safe": true}`` marks photos
safe (``false`` takes the mark back). A marked photo is never flagged, and a
flagged photo that looks just like a marked one is not flagged either, so one
mark clears a series of near-identical shots (app/query.py, ``safe_near``).
Nothing is retrained and nothing is stored but the mark: whether a photo is
cleared is worked out whenever it is asked, so taking a mark back flags its
lookalikes again at once.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from core.config import get_settings
from core.database import get_db
from core.models import Photo

from ..query import safe_ids, safe_near
from ..security import require_auth

router = APIRouter(prefix="/api", tags=["nsfw"], dependencies=[Depends(require_auth)])
DB = Annotated[Session, Depends(get_db)]

MAX_PHOTOS = 5000


class SafeBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=MAX_PHOTOS)
    safe: bool = True


def _cleared(db: Session) -> set[int]:
    """The photos the classifier flags that look just like a marked one. A
    few hundred flagged photos at most, compared with the marked handful."""
    s = get_settings()
    marked = safe_ids(db) if s.nsfw_clear_above > 0 else []
    if not marked:
        return set()
    return set(db.scalars(select(Photo.id).where(
        Photo.nsfw >= s.nsfw_threshold, Photo.nsfw_safe.is_(False), safe_near(s.nsfw_clear_above, marked),
    )))


@router.post("/photos/safe")
def mark_safe(body: SafeBody, db: DB) -> dict:
    """Mark (or unmark) photos safe. ``changed`` is how many marks changed;
    ``cleared`` the other photos that stopped being flagged with it,
    ``flagged`` the ones flagged again (a mark taken back)."""
    before = _cleared(db)
    result = db.execute(
        update(Photo)
        .where(Photo.id.in_(body.ids), Photo.nsfw_safe.is_not(body.safe))
        .values(nsfw_safe=body.safe)
    )
    db.flush()
    after = _cleared(db)
    db.commit()
    marked = set(body.ids)
    return {
        "safe": body.safe,
        "changed": result.rowcount,
        "cleared": sorted(after - before - marked),
        "flagged": sorted(before - after - marked),
    }
