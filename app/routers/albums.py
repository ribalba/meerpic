"""iCloud's albums, as the agent last read them, and asking it to read again.

The server never lists the remote itself: it holds no iCloud session (see
app/main.py). The agent reads the albums after every sync (agent/albums.py)
and keeps them in ``albums`` and ``photo_albums``; this reads those tables,
and the refresh link leaves a job for the agent, the way the Sync button does.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from core.config import get_settings
from core.database import get_db
from core.models import Album, Photo, PhotoAlbum

from ..query import QuerySpec, listed
from ..security import require_auth
from ..serialize import job_json
from .jobs import SYNC_OFF, enqueue_once

router = APIRouter(prefix="/api", tags=["albums"], dependencies=[Depends(require_auth)])
DB = Annotated[Session, Depends(get_db)]
settings = get_settings()

# See photos.LOCK_PREVIEW and jobs.LOCK_SYNC.
LOCK_ALBUMS = 0x6D72_6130     # "mra0"


@router.get("/albums")
def albums(db: DB) -> dict:
    """Every album, the user's (and the apps') by name, then iCloud's own.

    ``count`` is what ``album:"Name"`` lists, counted now: a Live Photo is in
    an album twice as far as iCloud is concerned (the still and its video),
    and a hidden or deleted member is not shown, so the number the agent
    stored (``Album.count``, members matched to a file) would promise tiles
    the grid does not have. ``remote_count`` is iCloud's own number.
    """
    members = (
        select(PhotoAlbum.album_id, func.count().label("n"))
        .join(Photo, Photo.id == PhotoAlbum.photo_id)
        .where(*listed(QuerySpec()))
        .group_by(PhotoAlbum.album_id)
        .subquery()
    )
    rows = db.execute(
        select(Album, func.coalesce(members.c.n, 0))
        .outerjoin(members, members.c.album_id == Album.id)
        .order_by(case((Album.kind == "user", 0), else_=1), func.lower(Album.name), Album.id)
    ).all()
    return {
        "albums": [
            {
                "id": album.id,
                "name": album.name,
                "kind": album.kind,
                "count": int(n),
                "remote_count": album.remote_count,
            }
            for album, n in rows
        ]
    }


@router.post("/albums/refresh")
def refresh(db: DB) -> dict:
    # The albums are read from the remote the sync copies from; without a
    # sync there is no remote to read.
    if not settings.sync_enabled:
        raise HTTPException(400, SYNC_OFF)
    return {"job": job_json(enqueue_once(db, "albums", LOCK_ALBUMS))}
