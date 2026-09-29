"""Running video previews without holding up everything else.

A preview of a long 4K clip is minutes of ffmpeg. Run inline, one of those
would stall the loop, and the photo you took a minute ago would wait for a
video from last summer before it got a thumbnail. So ffmpeg is started with
``Popen`` and the loop polls it once a turn, finishing what finished and
starting what is next, while meta, thumbnails and embeddings carry on.

Background work goes through videos newest first, and through every
standalone video before the first Live Photo motion.

At most two run at once, and background work (``video.previews = "all"``)
only ever takes one of them: the other is kept for somebody who just opened
a video in the browser and is looking at "Preparing a playable copy...". A
``preview`` job always runs, whatever the setting, and goes first.

ffmpeg's stderr goes to a file beside the output rather than a pipe: a
broken file can make ffmpeg print for minutes, and a pipe nobody reads
while polling fills up and stops it dead.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from core.config import Settings
from core.media import original_path, preview_path
from core.models import Photo
from core.timeutil import utcnow

from . import jobs, stages, video
from .log import log

CAPACITY = 2
BACKGROUND = 1
CANCEL_POLL = 1.0
# Background starts tried per turn. A start that fails at once (an HEVC file
# on a host that cannot decode it) must not turn one turn into thousands.
STARTS_PER_TICK = 4


@dataclass
class Running:
    photo_id: int
    sig: str
    name: str
    proc: subprocess.Popen
    tmp: Path
    errlog: Path
    dest: Path
    deadline: float
    mode: str
    background: bool
    job_ids: list[int] = field(default_factory=list)


def _last_line(path: Path) -> str:
    try:
        data = path.read_bytes()[-4000:].decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [line.strip() for line in data.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _unlink(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass


class Previews:
    def __init__(
        self,
        settings: Settings,
        only_ids: frozenset[int] | None = None,
        cooldown: stages.Cooldown | None = None,
    ):
        self.settings = settings
        self.only_ids = only_ids
        self.cooldown = cooldown or stages.Cooldown()
        self.caps = video.capabilities(settings.ffmpeg)
        self.nice = shutil.which("nice")
        self.running: list[Running] = []
        self.queue: list[tuple[int, int]] = []      # (video id, job id), in order
        self.done = 0
        self.failed_count = 0
        self.hevc_failures = 0
        self._notes: set[str] = set()
        self._cancel_checked = 0.0

    # --- state -----------------------------------------------------------------

    @property
    def background_enabled(self) -> bool:
        return self.settings.video_previews == "all"

    @property
    def busy(self) -> bool:
        return bool(self.running or self.queue)

    def job_ids(self) -> list[int]:
        return [j for r in self.running for j in r.job_ids] + [j for _, j in self.queue]

    def pending_query(self):
        return select(Photo.id).where(
            Photo.kind == "video", Photo.meta_sig == Photo.sig, Photo.preview_sig != Photo.sig
        )

    # --- on-demand -------------------------------------------------------------

    def request(self, db: Session, job_id: int, params: dict) -> None:
        """A ``preview`` job: queue its video ahead of everything else."""
        try:
            photo_id = int((params or {}).get("photo_id"))
        except (TypeError, ValueError):
            jobs.finish(job_id, "failed", error="params.photo_id is missing", message="bad request")
            return
        row = db.get(Photo, photo_id)
        if row is not None and row.kind == "photo":
            row = db.get(Photo, row.live_video_id) if row.live_video_id else None
            if row is None:
                jobs.finish(job_id, "failed", error="this photo has no motion to play", message="not a video")
                return
        if row is None:
            jobs.finish(job_id, "failed", error="photo not found", message="not found")
            return
        if row.preview_sig == row.sig and preview_path(row.sig).is_file():
            jobs.finish(job_id, "done", message="ready")
            return
        for r in self.running:
            if r.photo_id == row.id:
                r.job_ids.append(job_id)
                jobs.update_job(job_id, message=f"preparing {row.name}")
                return
        self.queue.append((row.id, job_id))
        jobs.update_job(job_id, message=f"waiting to prepare {row.name}")

    # --- the turn --------------------------------------------------------------

    def tick(self, db: Session, background: bool = True) -> int:
        """Finish what finished, start what fits. Returns how much changed.

        ``background=False`` starts no new background conversion this turn
        (the rest of the pipeline is busy); requested ones still start.
        """
        changed = self._poll(db)
        changed += self._check_cancels(db)
        while self.queue and len(self.running) < CAPACITY:
            video_id, job_id = self.queue.pop(0)
            if jobs.cancel_requested(job_id):
                jobs.finish(job_id, "cancelled", message="cancelled")
                continue
            attached = next((r for r in self.running if r.photo_id == video_id), None)
            if attached:
                attached.job_ids.append(job_id)
                continue
            row = db.get(Photo, video_id)
            if row is None:
                jobs.finish(job_id, "failed", error="photo not found", message="not found")
                continue
            self._start(db, row, [job_id], background=False)
            changed += 1
        if self.background_enabled and background:
            for _ in range(STARTS_PER_TICK):
                if (
                    sum(r.background for r in self.running) >= BACKGROUND
                    or len(self.running) >= CAPACITY
                ):
                    break
                row = self._next_background(db)
                if row is None:
                    break
                self._start(db, row, [], background=True)
                changed += 1
        return changed

    def has_background_work(self, db: Session) -> bool:
        return self.background_enabled and self._next_background(db) is not None

    def _next_background(self, db: Session) -> Photo | None:
        query = self.pending_query()
        skip = [r.photo_id for r in self.running] + self.cooldown.ids()
        # Videos somebody will open first, newest first; the motion of Live
        # Photos (thousands of two-second clips, played on hover) after.
        found = db.execute(stages.newest(query, self.only_ids, 1, skip,
                                         first=Photo.is_companion.is_(False))).scalar()
        return db.get(Photo, found) if found is not None else None

    def _note(self, text: str) -> None:
        if text and text not in self._notes:
            self._notes.add(text)
            log(f"previews: {text}")

    def _start(self, db: Session, row: Photo, job_ids: list[int], *, background: bool) -> None:
        src = original_path(row.root, row.rel_path)
        dest = preview_path(row.sig)
        try:
            if src is None:
                raise video.VideoError(f"library root {row.root!r} is not configured")
            # Known from the metadata already: no need to ask ffprobe to
            # learn that this ffmpeg cannot do anything with it.
            if row.video_codec == "hevc" and not self.caps.hevc:
                raise video.VideoError(video.HEVC_MISSING)
            info = video.stream_info(video.probe(str(src), self.settings.ffprobe))
            p = video.plan(info, self.caps, height=self.settings.video_height,
                           crf=self.settings.video_crf, preset=self.settings.video_preset)
            self._note(p.note)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
            errlog = dest.with_name(f".{dest.name}.{os.getpid()}.log")
            cmd = video.argv(self.settings.ffmpeg, str(src), str(tmp), p, nice=self.nice)
            with open(errlog, "wb") as err:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=err, start_new_session=True)
        except (video.VideoError, OSError) as exc:
            self._failed(db, row.id, row.sig, row.name, job_ids, exc)
            return
        self.running.append(Running(
            photo_id=row.id, sig=row.sig, name=row.name, proc=proc, tmp=tmp, errlog=errlog,
            dest=dest, deadline=time.monotonic() + video.timeout_for(info, p.mode),
            mode=p.mode, background=background, job_ids=list(job_ids),
        ))
        for job_id in job_ids:
            jobs.update_job(job_id, message=f"preparing {row.name}", progress={"mode": p.mode})

    def _poll(self, db: Session) -> int:
        changed = 0
        for r in list(self.running):
            code = r.proc.poll()
            if code is None and time.monotonic() < r.deadline:
                continue
            self.running.remove(r)
            changed += 1
            if code is None:
                self._kill(r)
                self._failed(db, r.photo_id, r.sig, r.name, r.job_ids,
                             f"ffmpeg timed out ({r.mode})")
                continue
            if code == 0 and r.tmp.is_file() and r.tmp.stat().st_size > 0:
                os.replace(r.tmp, r.dest)
                _unlink(r.errlog)
                db.execute(update(Photo).where(Photo.id == r.photo_id, Photo.sig == r.sig)
                           .values(preview_sig=r.sig, updated_at=utcnow()))
                stages.cleared(db, [r.photo_id], "previews")
                db.commit()
                self.done += 1
                log(f"preview: {r.name} ({r.mode})")
                for job_id in r.job_ids:
                    jobs.finish(job_id, "done", message="ready")
                continue
            reason = _last_line(r.errlog) or f"ffmpeg exited {code}"
            _unlink(r.tmp, r.errlog)
            self._failed(db, r.photo_id, r.sig, r.name, r.job_ids, f"ffmpeg: {reason}")
        return changed

    def _check_cancels(self, db: Session) -> int:
        now = time.monotonic()
        if now - self._cancel_checked < CANCEL_POLL:
            return 0
        self._cancel_checked = now
        changed = 0
        for r in list(self.running):
            for job_id in list(r.job_ids):
                if jobs.cancel_requested(job_id):
                    r.job_ids.remove(job_id)
                    jobs.finish(job_id, "cancelled", message="cancelled")
                    changed += 1
            # Nobody wants it any more and it was not background work anyway.
            if not r.background and not r.job_ids:
                self.running.remove(r)
                self._kill(r)
                _unlink(r.tmp, r.errlog)
        jobs.heartbeat(self.job_ids())
        return changed

    def _failed(self, db: Session, photo_id: int, sig: str, name: str, job_ids: list[int],
                error: BaseException | str) -> None:
        message = stages.short(error)
        stages.failed(db, photo_id, sig, "previews", message)
        db.commit()
        self.cooldown.add(photo_id)
        self.failed_count += 1
        # The same host problem for every HEVC video would be thousands of
        # identical lines; say it once, and count the rest.
        if message == video.HEVC_MISSING:
            self.hevc_failures += 1
            if self.hevc_failures == 1:
                log(f"preview: {name}: {message} (further HEVC failures are counted, not logged)",
                    error=True)
        else:
            log(f"preview: {name}: {message}", error=True)
        for job_id in job_ids:
            jobs.finish(job_id, "failed", error=message, message="could not prepare a preview")

    @staticmethod
    def _kill(r: Running) -> None:
        # ffmpeg (nice execs it in place) runs in a session of its own, so
        # signalling the group reaches it and nothing of the agent's.
        try:
            os.killpg(r.proc.pid, signal.SIGTERM)
            r.proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(r.proc.pid, signal.SIGKILL)
                r.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        _unlink(r.tmp)

    def shutdown(self) -> None:
        """Stop everything in flight; the next start picks it up again."""
        for r in self.running:
            self._kill(r)
            _unlink(r.errlog)
            for job_id in r.job_ids:
                jobs.finish(job_id, "failed", error="agent stopped", message="agent stopped")
        for _, job_id in self.queue:
            jobs.finish(job_id, "failed", error="agent stopped", message="agent stopped")
        self.running.clear()
        self.queue.clear()
