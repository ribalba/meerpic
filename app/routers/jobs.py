"""What the agent is doing, and asking it to do something now.

The web app runs no rclone and no ffmpeg and holds no iCloud session. The Sync
button therefore leaves a row in ``jobs`` that the agent, polling every
couple of seconds, picks up and writes its progress back into; the sidebar
polls ``/api/status`` and draws that row. The same shape as meercal's pending
actions, for the same reason: the process with the credentials is the one on
your machine, not the one behind a web server.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Integer, and_, cast, func, select
from sqlalchemy.orm import Session

from core import embed
from core.config import get_settings
from core.database import get_db
from core.models import Job, Photo, PhotoEmbedding, Setting
from core.timeutil import utcnow

from ..query import QuerySpec, listed
from ..security import require_auth
from ..serialize import iso_z, job_json

router = APIRouter(prefix="/api", tags=["jobs"], dependencies=[Depends(require_auth)])
DB = Annotated[Session, Depends(get_db)]
settings = get_settings()

# The agent writes its heartbeat every ~2 s; three missed beats in a row is a
# hiccup, fifteen is an agent that is not running.
ALIVE_WITHIN_S = 30
FINISHED_SHOWN = 5
ACTIVE = ("queued", "running")
FINISHED = ("done", "failed", "cancelled")
# See photos.LOCK_PREVIEW: one class of "enqueue unless one is queued".
LOCK_SYNC = 0x6D72_7330       # "mrs0"
# The agent parks a file after this many failures on one version of it; a
# parked file is not pending, or the sidebar would say "indexing" forever.
MAX_FAILS = 3


def _parse_instant(text: object) -> datetime | None:
    """The agent's ISO ``...Z`` heartbeat as naive UTC, or None if it is not one."""
    if not isinstance(text, str) or not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value


def agent_json(db: Session) -> dict | None:
    row = db.get(Setting, "agent.status")
    if row is None or not isinstance(row.value, dict):
        return None
    v = row.value
    seen = _parse_instant(v.get("seen_at"))
    return {
        "alive": seen is not None and (utcnow() - seen).total_seconds() <= ALIVE_WITHIN_S,
        "seen_at": iso_z(seen),
        "version": v.get("version"),
        "host": v.get("host"),
        "phase": v.get("phase"),
        "workers": v.get("workers"),
        "model": v.get("model"),
    }


def index_json(db: Session) -> dict:
    """What is still to do, counted in SQL in one pass.

    Companions are counted where the agent does work on them (metadata,
    thumbnails, previews) and not where it does not (they are never embedded,
    storyboarded or classified, and never listed, so they are not part of
    "total"). Like embeddings, a storyboard or a score still waiting for its
    thumbnail is pending: it is work left, whichever stage it waits in.
    """
    fresh = Photo.fail_count < MAX_FAILS
    own = ~Photo.is_companion
    emb = (
        select(PhotoEmbedding.photo_id)
        .where(
            PhotoEmbedding.photo_id == Photo.id,
            PhotoEmbedding.model == embed.model_name(),
            PhotoEmbedding.sig == Photo.sig,
        )
        .exists()
    )
    row = db.execute(
        select(
            func.count().filter(own),
            func.count().filter(and_(fresh, Photo.meta_sig != Photo.sig)),
            func.count().filter(and_(fresh, Photo.thumb_sig != Photo.sig)),
            func.count().filter(and_(fresh, own, ~emb)),
            func.count().filter(and_(fresh, Photo.kind == "video", Photo.preview_sig != Photo.sig)),
            func.count().filter(and_(fresh, own, Photo.kind == "video", Photo.story_sig != Photo.sig)),
            func.count().filter(and_(fresh, own, Photo.nsfw_sig != Photo.sig)),
            func.count().filter(and_(fresh, own, Photo.kind == "photo", Photo.superseded_by.is_(None),
                                     Photo.faces_sig != Photo.sig)),
            func.count().filter(~fresh),
            func.count().filter(and_(fresh, own, Photo.superseded_by.is_(None), Photo.ocr_sig != Photo.sig,
                                     *(() if settings.ocr_videos else (Photo.kind == "photo",)))),
        )
    ).one()
    total, meta, thumbs, embeddings, previews, story, nsfw, faces, failed, ocr = (int(v) for v in row)
    if not settings.nsfw_enabled:
        # Turned off, nothing will ever score them.
        nsfw = 0
    if not settings.faces_enabled:
        faces = 0
    if not settings.ocr_enabled:
        ocr = 0
    if settings.video_previews != "all":
        # On demand, a video without a preview is not pending: nothing will
        # make one until it is opened. What is pending is what was asked for.
        previews = int(db.scalar(
            select(func.count()).select_from(Job)
            .where(Job.kind == "preview", Job.status.in_(ACTIVE))
        ) or 0)
    return {
        "total": total,
        "meta": meta,
        "thumbs": thumbs,
        "embeddings": embeddings,
        "previews": previews,
        "story": story,
        "nsfw": nsfw,
        "faces": faces,
        "ocr": ocr,
        "failed": failed,
    }


@router.get("/status")
def status(db: DB) -> dict:
    sync = db.execute(
        select(Job).where(Job.kind == "sync").order_by(Job.id.desc()).limit(1)
    ).scalar()
    last_sync = db.scalar(
        select(Job.finished_at)
        .where(Job.kind == "sync", Job.status == "done")
        .order_by(Job.finished_at.desc().nulls_last(), Job.id.desc())
        .limit(1)
    )
    active = db.execute(
        select(Job).where(Job.status.in_(ACTIVE)).order_by(Job.id)
    ).scalars().all()
    finished = db.execute(
        select(Job).where(Job.status.in_(FINISHED)).order_by(Job.id.desc()).limit(FINISHED_SHOWN)
    ).scalars().all()
    # The grid's first tile: what "New photos" compares with.
    latest = db.execute(
        select(Photo.id, Photo.sort_at)
        .where(*listed(QuerySpec()))
        .order_by(Photo.sort_at.desc(), Photo.id.desc())
        .limit(1)
    ).first()
    return {
        "agent": agent_json(db),
        "sync": job_json(sync) if sync else None,
        "last_sync": iso_z(last_sync),
        "jobs": [job_json(j) for j in (*active, *finished)],
        "index": index_json(db),
        "latest": {"id": latest.id, "sort_at": iso_z(latest.sort_at)} if latest else None,
    }


SYNC_OFF = "Sync is turned off in meerpic.toml ([sync] enabled = false)"


def enqueue_once(db: Session, kind: str, lock: int) -> Job:
    """The queued or running job of this kind, or a new one; committed.

    One at a time, and a second click is an answer, not an error: it returns
    the job already queued or running. The advisory lock (one class per
    kind) makes "is there one, else add one" a single step for two windows
    clicking at once.
    """
    db.execute(select(func.pg_advisory_xact_lock(cast(lock, Integer), cast(0, Integer))))
    job = db.execute(
        select(Job).where(Job.kind == kind, Job.status.in_(ACTIVE)).order_by(Job.id).limit(1)
    ).scalar()
    if job is None:
        job = Job(kind=kind, params={})
        db.add(job)
    db.commit()
    db.refresh(job)
    return job


@router.post("/sync")
def start_sync(db: DB) -> dict:
    if not settings.sync_enabled:
        raise HTTPException(400, SYNC_OFF)
    return {"job": job_json(enqueue_once(db, "sync", LOCK_SYNC))}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(db: DB, job_id: int) -> dict:
    # FOR UPDATE: the agent claims with FOR UPDATE SKIP LOCKED, so while this
    # holds the row the agent cannot start the job it is being told to drop.
    job = db.get(Job, job_id, with_for_update=True)
    if job is None:
        raise HTTPException(404, "No such job")
    if job.status == "queued":
        # Nobody has it yet, so nobody else has to agree.
        job.status = "cancelled"
        job.cancel_requested = True
        job.finished_at = utcnow()
        job.message = "Cancelled before it started"
    elif job.status == "running":
        # The agent polls this flag and stops (see agent/jobs.py).
        job.cancel_requested = True
    db.commit()
    db.refresh(job)
    return {"job": job_json(job)}
