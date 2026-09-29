"""The ``jobs`` table from the agent's side: claim, report, finish.

The server never runs anything itself. The Sync button, "prepare this video
now" and "reindex" each leave a row here; the agent claims it (``FOR UPDATE
SKIP LOCKED``, so two agents pointed at one database never both take it),
writes its progress back into the same row, and the UI polls that row.

Every write goes through its own short session and commits at once. A job's
row is read by the server while the agent works, and a progress update that
sat in an open transaction would be one nobody sees; it also lets the sync
thread report without sharing a session with the indexing loop.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import delete, select, update

from core.database import SessionLocal
from core.models import Job, Photo, PhotoEmbedding
from core.timeutil import utcnow

# job.log keeps the tail of a job's output, not all of it: a first sync of a
# large library is 25,000 "Copied (new)" lines, and the UI shows forty.
LOG_CAP = 20_000

STAGES = ("meta", "thumbs", "embeddings", "previews", "story", "nsfw", "faces", "ocr")

# A running job whose row has not been touched for this long belongs to a
# process that died without saying so (a ``--sync`` in a terminal, killed).
STALE = timedelta(minutes=5)


def tail(text: str, cap: int = LOG_CAP) -> str:
    """The last ``cap`` characters, cut at a line boundary where possible."""
    if len(text) <= cap:
        return text
    cut = text[-cap:]
    nl = cut.find("\n")
    return cut[nl + 1:] if 0 <= nl < len(cut) - 1 else cut


def claim(kinds: Iterable[str]) -> Job | None:
    """Take the oldest queued job of one of ``kinds`` and mark it running.

    Returns a detached copy; its row is updated through the functions below.
    A job cancelled before it started is closed here rather than run.
    """
    kinds = list(kinds)
    if not kinds:
        return None
    with SessionLocal() as db:
        job = db.execute(
            select(Job)
            .where(Job.status == "queued", Job.kind.in_(kinds))
            .order_by(Job.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if job is None:
            return None
        now = utcnow()
        if job.cancel_requested:
            job.status = "cancelled"
            job.finished_at = now
            job.message = "cancelled before it started"
        else:
            job.status = "running"
            job.started_at = now
            job.heartbeat_at = now
        db.commit()
        db.refresh(job)
        db.expunge(job)
        return job


def create(kind: str, params: dict | None = None, *, running: bool = False) -> Job:
    """A new job row, for the agent's own work (``--sync``, the timer)."""
    with SessionLocal() as db:
        now = utcnow()
        job = Job(kind=kind, params=params or {}, status="running" if running else "queued")
        if running:
            job.started_at = now
            job.heartbeat_at = now
        db.add(job)
        db.commit()
        db.refresh(job)
        db.expunge(job)
        return job


def update_job(job_id: int, **values) -> None:
    """Set columns on one job. ``heartbeat_at`` is bumped with every write."""
    values.setdefault("heartbeat_at", utcnow())
    with SessionLocal() as db:
        db.execute(update(Job).where(Job.id == job_id).values(**values))
        db.commit()


def append_log(job_id: int, lines: Iterable[str]) -> None:
    text = "".join(line.rstrip("\n") + "\n" for line in lines)
    if not text:
        return
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.log = tail((job.log or "") + text)
        job.heartbeat_at = utcnow()
        db.commit()


def finish(job_id: int, status: str, *, message: str | None = None, error: str | None = None,
           progress: dict | None = None) -> None:
    values: dict = {"status": status, "finished_at": utcnow()}
    if message is not None:
        values["message"] = message
    if error is not None:
        values["error"] = error[:4000]
    if progress is not None:
        values["progress"] = progress
    update_job(job_id, **values)


def cancel_requested(job_id: int) -> bool:
    with SessionLocal() as db:
        return bool(db.execute(select(Job.cancel_requested).where(Job.id == job_id)).scalar())


def active(kind: str) -> Job | None:
    """The queued or running job of this kind, if there is one."""
    with SessionLocal() as db:
        job = db.execute(
            select(Job).where(Job.kind == kind, Job.status.in_(("queued", "running")))
            .order_by(Job.id).limit(1)
        ).scalar_one_or_none()
        if job is not None:
            db.expunge(job)
        return job


def running_elsewhere(kinds: Iterable[str], exclude: Iterable[int] = ()) -> bool:
    """Whether a job of one of ``kinds`` is running and alive (its row
    touched within ``STALE``), other than ``exclude``: how the agent knows
    that a ``--sync`` in a terminal holds the remote before it starts a
    delete or an album refresh beside it."""
    skip = list(exclude)
    with SessionLocal() as db:
        query = select(Job.id).where(
            Job.kind.in_(list(kinds)), Job.status == "running",
            Job.heartbeat_at >= utcnow() - STALE,
        )
        if skip:
            query = query.where(Job.id.notin_(skip))
        return db.execute(query.limit(1)).first() is not None


def latest(kind: str) -> Job | None:
    with SessionLocal() as db:
        job = db.execute(
            select(Job).where(Job.kind == kind).order_by(Job.id.desc()).limit(1)
        ).scalar_one_or_none()
        if job is not None:
            db.expunge(job)
        return job


def fail_orphans() -> int:
    """Jobs a previous agent left ``running`` never finish on their own:
    nothing is running them. Called once at startup."""
    with SessionLocal() as db:
        result = db.execute(
            update(Job).where(Job.status == "running").values(
                status="failed", error="agent restarted", message="agent restarted",
                finished_at=utcnow(),
            )
        )
        db.commit()
        return result.rowcount or 0


def heartbeat(job_ids: Iterable[int]) -> None:
    ids = list(job_ids)
    if not ids:
        return
    with SessionLocal() as db:
        db.execute(update(Job).where(Job.id.in_(ids), Job.status == "running")
                   .values(heartbeat_at=utcnow()))
        db.commit()


def reindex(stage: str) -> str:
    """Make a stage pending again for every file. Returns a summary line.

    Clearing a stage's sig is the whole of it (see core/models: a stage is
    pending exactly when its sig differs), plus the failures that belong to
    that stage, so files parked after three attempts get another chance:
    reindexing is what somebody does after fixing whatever made them fail.
    """
    if stage not in (*STAGES, "all"):
        raise ValueError(f"unknown stage {stage!r}; one of {', '.join(STAGES)}, all")
    stages = STAGES if stage == "all" else (stage,)
    column = {"meta": "meta_sig", "thumbs": "thumb_sig", "previews": "preview_sig",
              "story": "story_sig", "nsfw": "nsfw_sig", "faces": "faces_sig", "ocr": "ocr_sig"}
    done = []
    with SessionLocal() as db:
        for name in stages:
            if name == "embeddings":
                n = db.execute(delete(PhotoEmbedding)).rowcount or 0
                done.append(f"{n} embedding(s) dropped")
                continue
            n = db.execute(
                update(Photo).where(getattr(Photo, column[name]) != "").values({column[name]: ""})
            ).rowcount or 0
            done.append(f"{name}: {n} file(s)")
        failed = Photo.error_stage.in_(stages) if stage != "all" else Photo.fail_count > 0
        db.execute(update(Photo).where(failed).values(error_stage="", error="", fail_count=0))
        db.commit()
    return "reindex " + stage + ": " + ", ".join(done)
