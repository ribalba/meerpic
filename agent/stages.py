"""What every stage shares: picking work, and keeping score of failures.

Each stage is "the rows whose sig for this stage is not the file's sig",
newest first, so the photo taken a minute ago is thumbnailed before the one
from 2014 even in the middle of a first index. A stage that throws on a file
records it on that row (``error_stage``, ``error``, ``fail_count``) and moves
on; three failures park the row until the file changes (the scan resets the
count with the sig) or a reindex clears it. A stage that succeeds clears
the failure only if it was its own: a video whose preview failed keeps
saying so after its thumbnail is redrawn.
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from sqlalchemy import ColumnElement, Select, select, update
from sqlalchemy.orm import Session

from core.models import Photo
from core.timeutil import utcnow

MAX_FAILS = 3


def newest(
    query: Select,
    only_ids: frozenset[int] | None,
    limit: int,
    exclude: Iterable[int] = (),
    first: ColumnElement[bool] | None = None,
) -> Select:
    """Newest first, parked rows left out, optionally within ``only_ids`` and
    without ``exclude`` (rows cooling down after a failure). ``first`` puts
    the rows it is true for ahead of the rest, each part newest first."""
    query = query.where(Photo.fail_count < MAX_FAILS)
    if only_ids is not None:
        query = query.where(Photo.id.in_(only_ids) if only_ids else Photo.id < 0)
    skip = list(exclude)
    if skip:
        query = query.where(Photo.id.notin_(skip))
    order = [Photo.sort_at.desc(), Photo.id.desc()]
    if first is not None:
        # true sorts after false in Postgres, so descending puts it first.
        order.insert(0, first.desc())
    return query.order_by(*order).limit(limit)


class Cooldown:
    """Rows that just failed, kept out of the next picks for a while.

    Without it a file that fails is the newest pending one again on the very
    next turn, and burns its three attempts in three seconds: no time for a
    file still being written to settle, and a turn spent on it each time.
    ``--once`` never retries within the run (``seconds=None``); the next run
    does.
    """

    def __init__(self, seconds: float | None = 300.0):
        self.seconds = seconds
        self._until: dict[int, float] = {}

    def add(self, photo_id: int) -> None:
        self._until[photo_id] = (
            float("inf") if self.seconds is None else time.monotonic() + self.seconds
        )

    def ids(self) -> list[int]:
        now = time.monotonic()
        self._until = {k: v for k, v in self._until.items() if v > now}
        return list(self._until)


def short(exc: BaseException | str, limit: int = 500) -> str:
    """One line for ``photos.error``: the UI shows it in a tooltip."""
    if isinstance(exc, str):
        text = exc
    elif isinstance(exc, RuntimeError):
        # The agent's own errors (VideoError, ThumbError, ...) are sentences
        # already; a library's OSError needs its class to mean anything.
        text = str(exc) or type(exc).__name__
    else:
        text = f"{type(exc).__name__}: {exc}"
    lines = text.strip().splitlines()
    return (lines[0] if lines else type(exc).__name__).replace("\x00", "")[:limit]


def failed(db: Session, photo_id: int, sig: str, stage: str, error: BaseException | str) -> None:
    """Record one failure. Only for the version of the file that failed: a
    file replaced meanwhile starts with a clean slate."""
    db.execute(
        update(Photo)
        .where(Photo.id == photo_id, Photo.sig == sig)
        .values(error_stage=stage, error=short(error), fail_count=Photo.fail_count + 1,
                updated_at=utcnow())
    )


def cleared(db: Session, photo_ids: Iterable[int], stage: str) -> None:
    """Forget the failures of ``stage`` on rows it just succeeded on."""
    ids = list(photo_ids)
    if ids:
        db.execute(
            update(Photo)
            .where(Photo.id.in_(ids), Photo.error_stage == stage)
            .values(error_stage="", error="", fail_count=0)
        )


def base_columns() -> Select:
    """The columns every stage needs to find a file and decide about it."""
    return select(
        Photo.id, Photo.root, Photo.rel_path, Photo.name, Photo.ext, Photo.kind,
        Photo.sig, Photo.mtime_ns, Photo.content_id, Photo.duration, Photo.video_codec,
        Photo.is_companion, Photo.live_video_id,
    )
