"""The grid, one photo, and the timeline beside the grid.

Two ways to list, chosen by the query (see app/query.py):

* **date mode**, the default: newest first, paged by keyset on
  ``(sort_at, id)``. A keyset rather than an offset because the library grows
  at the top while someone scrolls: an offset would shift by every photo that
  arrived since the first page and show some twice, a key does not. It also
  walks ``ix_photos_timeline`` and nothing else, so page 100 costs what page 1
  does.
* **score mode**, when there are words, ``similar:`` or ``face:``: a ranking, best first,
  above the model's floor and capped (``query.SCORE_CAP``). Paged by offset,
  because a ranking is computed whole anyway and does not grow while you read
  it. ``is:nsfw`` on its own is a ranking of the same shape, by the
  classifier's probability instead of a vector's.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Integer, case, cast, func, select, tuple_
from sqlalchemy.orm import Session

from core.database import get_db
from core.models import UNDATED, Album, Face, Job, Photo, PhotoAlbum, PhotoText

from ..query import (
    QuerySpec,
    listed,
    matches,
    nsfw_matches,
    parse_period,
    parse_query,
    resolve_scoring,
    safe_cleared,
    safe_lookalike,
)
from ..security import require_auth
from ..serialize import card_json, detail_json, face_url, preview_ready, preview_url

router = APIRouter(prefix="/api", tags=["photos"], dependencies=[Depends(require_auth)])
DB = Annotated[Session, Depends(get_db)]

DEFAULT_LIMIT = 150
MAX_LIMIT = 500

# Two-key advisory locks (see /api/sync for the other): one class per kind of
# "enqueue unless one is already queued", keyed by the photo within it.
LOCK_PREVIEW = 0x6D72_7031   # "mrp1"


# --- cursors ------------------------------------------------------------------


def encode_cursor(p: Photo) -> str:
    raw = f"{p.sort_at.isoformat()}|{p.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        stamp, _, ident = raw.partition("|")
        return datetime.fromisoformat(stamp), int(ident)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(400, "Bad cursor") from exc


def decode_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        offset = int(cursor)
    except ValueError as exc:
        raise HTTPException(400, "Bad cursor") from exc
    if offset < 0:
        raise HTTPException(400, "Bad cursor")
    return offset


def period_end(text: str) -> datetime:
    """``2024-07`` or ``2024-07-22`` (or ``2024``) as the first wall-clock
    moment after it."""
    period = parse_period(text)
    if period is None:
        raise HTTPException(400, "from= takes YYYY-MM, YYYY-MM-DD or undated")
    return period[1]


# --- cards --------------------------------------------------------------------


def edited_among(db: Session, ids: Iterable[int]) -> set[int]:
    """Which of these photos are edits: some other photo names them in
    ``superseded_by``. One indexed query per page, not one per tile."""
    ids = list(ids)
    if not ids:
        return set()
    return set(db.scalars(
        select(Photo.superseded_by).where(Photo.superseded_by.in_(ids)).distinct()
    ))


def cards(db: Session, photos: list[Photo], scores: list[float] | None = None) -> list[dict]:
    edited = edited_among(db, (p.id for p in photos))
    cleared = safe_cleared(db, photos)
    return [
        card_json(p, scores[i] if scores is not None else None, p.id in edited, p.id in cleared)
        for i, p in enumerate(photos)
    ]


def card(db: Session, photo: Photo) -> dict:
    return cards(db, [photo])[0]


# --- the listing --------------------------------------------------------------


def _date_page(db: Session, spec: QuerySpec, clauses: list, cursor: str | None,
               start: str | None, limit: int) -> dict:
    key = tuple_(Photo.sort_at, Photo.id)
    stmt = select(Photo).where(*clauses)
    if cursor:
        at, ident = decode_cursor(cursor)
        stmt = stmt.where(key < tuple_(at, ident))
    elif start == "undated":
        # The viewer's "Show in all photos" on a photo with no date: they
        # sort after every dated one, and no month is late enough to reach them.
        stmt = stmt.where(Photo.sort_at <= UNDATED)
    elif start:
        # The scrubber's jump. The timeline groups on the wall clock and the
        # grid sorts on the instant, so "July 2024" starts at the newest photo
        # whose *wall clock* is in or before July, found once, and the
        # listing carries on from there in the usual order.
        end = period_end(start)
        anchor = db.execute(
            select(Photo.sort_at, Photo.id)
            .where(*clauses, Photo.taken_local < end)
            .order_by(Photo.sort_at.desc(), Photo.id.desc())
            .limit(1)
        ).first()
        if anchor is not None:
            stmt = stmt.where(key <= tuple_(anchor.sort_at, anchor.id))
        else:
            # Nothing dated that early: what is left is the undated.
            stmt = stmt.where(Photo.sort_at <= UNDATED)
    rows = db.execute(
        stmt.order_by(Photo.sort_at.desc(), Photo.id.desc()).limit(limit + 1)
    ).scalars().all()
    more = len(rows) > limit
    rows = rows[:limit]
    total = None
    if not cursor:
        total = db.scalar(select(func.count()).select_from(Photo).where(*clauses))
    return {
        "items": cards(db, rows),
        "next": encode_cursor(rows[-1]) if more and rows else None,
        "mode": "date",
        "total": total,
        "query": spec.describe(),
        "similar_to": None,
        "face": None,
    }


def _score_page(db: Session, spec: QuerySpec, clauses: list, cursor: str | None, limit: int) -> dict:
    offset = decode_offset(cursor)
    scoring = None
    if spec.scored:
        scoring, source = resolve_scoring(db, spec)
        ranked = matches(scoring, clauses) if scoring is not None else None
    else:
        source, ranked = None, nsfw_matches(clauses)
    face = scoring.face if scoring is not None else None
    page = {
        "items": [],
        "next": None,
        "mode": "score",
        "total": 0 if offset == 0 else None,
        "query": spec.describe(),
        "similar_to": card(db, source) if source is not None else None,
        # The face a face: search looks for, for the header above it.
        "face": {"id": face.id, "url": face_url(face), "photo_id": face.photo_id} if face else None,
    }
    if ranked is None:
        return page
    order = (
        (Photo.sort_at.desc(), Photo.id.desc()) if spec.by_date else (ranked.c.score.desc(), Photo.id)
    )
    # The window count is the whole ranking's size, computed in the same pass
    # as the page: the expensive part of this query is the distance to every
    # vector, and a separate COUNT would do all of it again.
    rows = db.execute(
        select(Photo, ranked.c.score, func.count().over().label("total"))
        .join(ranked, ranked.c.id == Photo.id)
        .order_by(*order)
        .offset(offset)
        .limit(limit + 1)
    ).all()
    more = len(rows) > limit
    rows = rows[:limit]
    page["items"] = cards(db, [r[0] for r in rows], [r[1] for r in rows])
    page["next"] = str(offset + limit) if more else None
    if rows:
        page["total"] = int(rows[0].total)
    return page


@router.get("/photos")
def list_photos(
    db: DB,
    q: str = "",
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    start: Annotated[str | None, Query(alias="from")] = None,
) -> dict:
    spec = parse_query(q)
    limit = max(1, min(limit, MAX_LIMIT))
    clauses = listed(spec)
    if spec.mode == "score":
        return _score_page(db, spec, clauses, cursor, limit)
    return _date_page(db, spec, clauses, cursor, start, limit)


# --- one photo ----------------------------------------------------------------


def get_photo(db: Session, photo_id: int) -> Photo:
    photo = db.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(404, "No such photo")
    return photo


def album_names(db: Session, photo_id: int) -> list[str]:
    """The albums a photo is in: the user's own first (they are what the
    person made), then iCloud's smart ones, each by name."""
    return list(db.scalars(
        select(Album.name)
        .join(PhotoAlbum, PhotoAlbum.album_id == Album.id)
        .where(PhotoAlbum.photo_id == photo_id)
        .order_by(case((Album.kind == "user", 0), else_=1), func.lower(Album.name), Album.id)
    ))


def original_of(db: Session, photo_id: int) -> int | None:
    """The original this photo is an edit of, if it is one. Lowest id if the
    agent ever lets two originals name the same edit, so the answer is stable."""
    return db.scalar(
        select(Photo.id).where(Photo.superseded_by == photo_id).order_by(Photo.id).limit(1)
    )


def faces_in(db: Session, photo: Photo) -> list[Face]:
    """The faces found in this version of the photo, largest first."""
    return list(db.scalars(
        select(Face).where(Face.photo_id == photo.id, Face.sig == photo.sig).order_by(Face.pos)
    ))


def text_of(db: Session, photo: Photo) -> PhotoText | None:
    """What this version of the file says, if the text stage found anything."""
    return db.scalar(
        select(PhotoText).where(PhotoText.photo_id == photo.id, PhotoText.sig == photo.sig)
    )


@router.get("/photos/{photo_id}")
def photo_detail(db: DB, photo_id: int) -> dict:
    photo = get_photo(db, photo_id)
    live = db.get(Photo, photo.live_video_id) if photo.live_video_id else None
    return detail_json(photo, live, album_names(db, photo.id), original_of(db, photo.id),
                       faces=faces_in(db, photo), text=text_of(db, photo),
                       safe_like=safe_lookalike(db, photo))


@router.post("/photos/{photo_id}/preview")
def request_preview(db: DB, photo_id: int) -> dict:
    """Ask for a playable copy of a video, or of a Live Photo's motion.

    With ``video.previews = "on-demand"`` this is how every video gets one;
    with "all" it moves this one to the front of the queue. Asking twice while
    the first is still queued or running returns the first.
    """
    photo = get_photo(db, photo_id)
    if photo.kind == "video":
        target = photo
    elif photo.live_video_id:
        target = db.get(Photo, photo.live_video_id)
    else:
        target = None
    if target is None:
        raise HTTPException(400, "This is a still photo: there is no motion to prepare")
    if preview_ready(target):
        return {"ready": True, "url": preview_url(target)}

    # Serialise "is there one already, else make one" per video, so that a
    # double click or two open windows cannot queue the same ffmpeg twice.
    db.execute(select(func.pg_advisory_xact_lock(cast(LOCK_PREVIEW, Integer), cast(target.id, Integer))))
    existing = db.execute(
        select(Job)
        .where(
            Job.kind == "preview",
            Job.status.in_(("queued", "running")),
            Job.params["photo_id"].astext == str(target.id),
        )
        .order_by(Job.id)
        .limit(1)
    ).scalar()
    if existing is None:
        existing = Job(kind="preview", params={"photo_id": target.id})
        db.add(existing)
    db.commit()
    return {"ready": False, "job_id": existing.id}


# --- the timeline -------------------------------------------------------------


@router.get("/timeline")
def timeline(db: DB, q: str = "") -> dict:
    """Months with photos in them, newest first, for the scrubber and the
    sidebar's years. Grouped on the wall clock, like the day headings, so a
    New Year's Eve photo from New York is in December. Filters apply; words
    and similar: do not, because a ranking has no timeline."""
    spec = parse_query(q)
    inner = select(
        func.to_char(Photo.taken_local, "YYYY-MM").label("month")
    ).where(*listed(spec)).subquery()
    rows = db.execute(
        select(inner.c.month, func.count())
        .group_by(inner.c.month)
        .order_by(inner.c.month.desc().nulls_last())
    ).all()
    months = [{"month": m, "count": int(n)} for m, n in rows if m is not None]
    undated = next((int(n) for m, n in rows if m is None), 0)
    return {"months": months, "undated": undated}

