"""One request that tells the client everything it needs before it draws.

Deliberately a single call, as in meercal: the zone, the map's tiles, whether
sync is on, the UI's own preferences and the numbers for the sidebar all
arrive together, and nothing useful can be drawn until all of them have.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from core import embed
from core.config import get_settings
from core.database import get_db
from core.models import Photo, Setting
from core.timeutil import utcnow
from core.version import VERSION

from ..query import (
    WHATSAPP_ALBUM,
    album_clause,
    never_listed,
    nsfw_clause,
    saved_clause,
)
from ..security import require_auth
from ..serialize import TZ, deletable

router = APIRouter(prefix="/api", tags=["state"], dependencies=[Depends(require_auth)])
DB = Annotated[Session, Depends(get_db)]
settings = get_settings()

PREFS_KEY = "prefs"


def counts(db: Session) -> dict:
    """The sidebar's numbers, in one pass.

    Each is the ``total`` the grid would show for the filter the sidebar row
    sets (see query.listed): companions are not photos you took twice, and an
    edit's original is not a second photo, so neither is counted; Hidden and
    Recently Deleted are counted only as themselves, the way ``is:hidden``
    and ``is:deleted`` list them.
    """
    shown = and_(~Photo.hidden, ~Photo.icloud_deleted)

    def n(*where):
        return func.count().filter(and_(shown, *where))

    row = db.execute(
        select(
            n(),
            n(Photo.kind == "photo"),
            n(Photo.kind == "video"),
            n(Photo.live_video_id.is_not(None)),
            n(Photo.is_screenshot.is_(True)),
            n(Photo.lat.is_not(None)),
            n(Photo.taken_local.is_(None)),
            n(Photo.favorite.is_(True)),
            n(album_clause(WHATSAPP_ALBUM)),
            n(saved_clause()),
            n(nsfw_clause()),
            func.count().filter(and_(Photo.hidden.is_(True), ~Photo.icloud_deleted)),
            func.count().filter(and_(Photo.icloud_deleted.is_(True), ~Photo.hidden)),
        ).where(*never_listed())
    ).one()
    keys = (
        "all", "photos", "videos", "live", "screenshots", "located", "undated",
        "favorites", "whatsapp", "saved", "nsfw", "hidden", "deleted",
    )
    return dict(zip(keys, (int(v) for v in row), strict=True))


@router.get("/state")
def state(db: DB) -> dict:
    prefs = db.get(Setting, PREFS_KEY)
    return {
        "version": VERSION,
        # The zone a photo without one of its own is read in, by its IANA
        # name. The browser prints wall clocks as they are and never converts
        # them; this is for saying which zone "15:30" is in, not for maths.
        "timezone": TZ.key,
        "roots": list(settings.library_roots),
        "map": {
            "tile_url": settings.map_tile_url,
            "attribution": settings.map_attribution,
            "max_zoom": settings.map_max_zoom,
        },
        "sync": {"enabled": settings.sync_enabled, "remote": settings.sync_remote},
        "search": {"model": embed.model_name(), "ready": embed.text_ready()},
        "faces": {"enabled": settings.faces_enabled},
        "ocr": {"enabled": settings.ocr_enabled, "videos": settings.ocr_videos},
        "nsfw": {
            "enabled": settings.nsfw_enabled,
            "threshold": settings.nsfw_threshold,
            "blur": settings.nsfw_blur,
        },
        # Whether there is a Delete button at all (see serialize.deletable).
        "delete": deletable(),
        "prefs": prefs.value if prefs else {},
        "counts": counts(db),
    }


class PrefsBody(BaseModel):
    value: dict[str, Any]


@router.put("/prefs")
def put_prefs(body: PrefsBody, db: DB) -> dict:
    # An upsert rather than get-then-add: two windows saving at once must not
    # turn into a unique violation on the first save.
    now = utcnow()
    stmt = insert(Setting).values(key=PREFS_KEY, value=body.value, updated_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Setting.key], set_={"value": body.value, "updated_at": now}
    )
    db.execute(stmt)
    db.commit()
    return {"ok": True}
