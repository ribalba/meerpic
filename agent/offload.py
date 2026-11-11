"""Offloading old videos, and fetching one back: the two jobs.

Which videos are meant (``offload.after`` days and older, from iCloud, no
Live Photo motions) is core/offloading.py's to say. Their small copies are
made beside the other conversions (agent/previews.py, ``lowres``). This is
the part that deletes, and the part that downloads.

**Offload** (``offload`` job, every hour while there is something to do, or
``--offload`` in a terminal) lists iCloud's "All Photos" once and deletes an
original only when all of this holds:

* the file here is the one that was indexed (size and time as in its row);
* iCloud lists a file of that name with the same size and the same time to
  the second, which is what ``rclone copy`` gave the local one;
* its small copy is there, plays as long as the original does, and is less
  than half its size (a 300 KB clip from 2004 is not worth the round trip);
* every stage that reads the original has read this version of it.

The row is marked before the file goes, never the other way round: a row
whose file is missing and that is not marked is a deleted photo to the scan
(agent/scan.py), and an agent stopped in between would lose it. The 720p
preview goes with the original; the small copy is what plays now. A dry run
(``params.dry_run``, ``--offload --dry-run``) checks everything, including
videos whose small copy is not made yet, and deletes nothing.

**Fetch** (``fetch`` job, the viewer's "Get the original") downloads one
original with ``rclone copyto`` into ``.meerpic-offloaded/.fetching/``, checks
that it is the file the row describes, and moves it to where it was. rclone
gives it iCloud's time, so its sig is the old one and every thumbnail, face
and line of text found in it is still its own. Then the 720p preview is
queued, and the original stays ``offload.keep_fetched`` days.

Both run on the agent's remote thread (agent/main.py): one rclone at a time
against the iCloud session, never beside a sync.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, update

from core.config import Settings
from core.database import SessionLocal
from core.media import offload_path, original_path, preview_path
from core.models import Photo
from core.offloading import due, from_remote, indexed, lowres_file, remote_of
from core.timeutil import utcnow

from . import jobs, rclone, video
from .albums import LIST_TIMEOUT, AlbumsError, Entry, parse_entries, remote_path
from .log import log
from .sync import _human_bytes, auth_hint

# The small copy must play this close to the original's length.
DURATION_SLACK = 1.0            # seconds, or
DURATION_SHARE = 0.02           # of the length, whichever is more
# A copy at least this share of the original saves too little to bother.
WORTH_IT = 0.5
FETCHING = ".fetching"
# The slowest download worth waiting for, to set rclone's timeout.
SLOWEST = 256 * 1024            # bytes a second


class OffloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    id: int
    root: str
    rel_path: str
    name: str
    kind: str
    size: int
    mtime_ns: int
    sig: str
    duration: float | None
    lowres_sig: str
    offloaded_at: object


def _columns():
    return select(Photo.id, Photo.root, Photo.rel_path, Photo.name, Photo.kind, Photo.size,
                  Photo.mtime_ns, Photo.sig, Photo.duration, Photo.lowres_sig, Photo.offloaded_at)


def candidates(db, settings: Settings, *, every: bool = False) -> list[Candidate]:
    """The videos an offload would delete the original of, oldest first:
    still here, with a small copy worth keeping instead. ``every`` (a dry
    run) takes all whose original is here, small copy or not.

    Whatever is left out here costs no listing of iCloud, which is why a
    copy that saves too little is: it would be "kept" again every time."""
    query = _columns().where(*due(settings), *indexed(settings))
    if not every:
        query = query.where(Photo.lowres_sig == Photo.sig)
    rows = [Candidate(*row) for row in db.execute(query.order_by(Photo.sort_at, Photo.id))]
    out = []
    for row in rows:
        path = original_path(row.root, row.rel_path)
        if path is None or not path.is_file():
            continue
        if not every:
            small = lowres_file(row)
            try:
                if small is not None and small.stat().st_size >= row.size * WORTH_IT:
                    continue
            except OSError:
                pass
        out.append(row)
    return out


def has_work(db, settings: Settings) -> bool:
    """Whether an offload now would delete anything (the agent's timer)."""
    return settings.offload_enabled and bool(candidates(db, settings))


def same_time(mtime_ns: int, seconds: int | None) -> bool:
    """rclone sets a file's time to iCloud's and lists it to the second."""
    return seconds is not None and abs(mtime_ns // 1_000_000_000 - seconds) <= 1


def lowres_problem(path: Path, row: Candidate, ffprobe: str) -> str | None:
    """Why this small copy cannot stand in for its original, or None."""
    size = path.stat().st_size
    if size >= row.size * WORTH_IT:
        return f"the small copy ({_human_bytes(size)}) would save too little"
    try:
        info = video.stream_info(video.probe(str(path), ffprobe))
    except video.VideoError as exc:
        return f"the small copy does not play: {exc}"
    if not info.duration:
        return "the small copy has no length"
    if row.duration:
        slack = max(DURATION_SLACK, row.duration * DURATION_SHARE)
        if abs(info.duration - row.duration) > slack:
            return f"the small copy is {info.duration:.1f} s long, the original {row.duration:.1f} s"
    return None


def _unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


class _Run:
    """What both jobs share: reporting into the row, being told to stop."""

    kind = ""

    def __init__(self, job_id: int, settings: Settings, params: dict | None = None, *,
                 echo: Callable[[str], None] | None = None):
        self.job_id = job_id
        self.settings = settings
        self.params = params or {}
        self.echo = echo
        self.status = "running"
        self._stopped = False
        self._cancelled = False

    def stop(self) -> None:
        self._stopped = True

    def _should_stop(self) -> bool:
        if self._stopped:
            return True
        try:
            self._cancelled = jobs.cancel_requested(self.job_id)
        except Exception:  # noqa: BLE001 - unknown means "not cancelled"; asked again in a second
            return False
        return self._cancelled

    def _say(self, line: str) -> None:
        if self.echo:
            self.echo(line)
        jobs.append_log(self.job_id, [line])

    def _stopped_end(self, progress: dict) -> str:
        if self._cancelled:
            return self._end("cancelled", "cancelled", None, progress)
        return self._end("failed", "agent stopped", "agent stopped", progress)

    def _end(self, status: str, message: str, error: str | None, progress: dict) -> str:
        self.status = status
        jobs.finish(self.job_id, status, message=message, error=error, progress=progress)
        log(f"{self.kind}: {message}" + (f": {error.splitlines()[0]}" if error else ""),
            error=status == "failed")
        return status


class OffloadRun(_Run):
    """One offload job: list iCloud once per remote, then each video that
    is ready.

    ``listed`` is the "All Photos" of each library root made moments ago
    (the album refresh after a sync), which saves making them again."""

    kind = "offload"

    def __init__(self, job_id: int, settings: Settings, params: dict | None = None, *,
                 echo: Callable[[str], None] | None = None,
                 listed: dict[str, list[Entry]] | None = None):
        super().__init__(job_id, settings, params, echo=echo)
        self.listed = listed or {}

    @property
    def dry_run(self) -> bool:
        return bool(self.params.get("dry_run"))

    def _list(self, roots: list[str]) -> dict[str, dict[str, Entry]]:
        """By root, the files its remote lists, by name."""
        out = {}
        for root in roots:
            entries = self.listed.get(root)
            if entries is None:
                argv = [self.settings.rclone, "lsjson", "--files-only", remote_of(self.settings, root)]
                result = rclone.run(argv, timeout=LIST_TIMEOUT, should_stop=self._should_stop)
                if not result.ok:
                    raise AlbumsError(result.reason(), auth=result.auth)
                entries = parse_entries(result.out)
            out[root] = {entry.name: entry for entry in entries}
        return out

    def check(self, row: Candidate, entry: Entry | None) -> str | None:
        """Why this original must stay, or None to let it go."""
        path = original_path(row.root, row.rel_path)
        try:
            st = path.stat()
        except (OSError, AttributeError):
            return "the original is not here"
        if st.st_size != row.size or st.st_mtime_ns != row.mtime_ns:
            return "the file changed since it was indexed"
        if entry is None:
            return "iCloud lists no file of this name"
        if entry.size != row.size or not same_time(row.mtime_ns, entry.mtime):
            return "iCloud's file of this name is not this one"
        small = lowres_file(row)
        if small is None:
            if row.lowres_sig == row.sig and not self.dry_run:
                # Made once and gone since: make it again.
                with SessionLocal() as db:
                    db.execute(update(Photo).where(Photo.id == row.id, Photo.sig == row.sig)
                               .values(lowres_sig=""))
                    db.commit()
            return "its small copy is not made yet"
        return lowres_problem(small, row, self.settings.ffprobe)

    def offload(self, row: Candidate) -> None:
        """Mark, delete, then drop the 720p preview. Raises OffloadError or
        OSError with the row unmarked and the file where it was."""
        path = original_path(row.root, row.rel_path)
        with SessionLocal() as db:
            marked = db.execute(
                update(Photo).where(Photo.id == row.id, Photo.sig == row.sig)
                .values(offloaded_at=utcnow())
            ).rowcount
            db.commit()
        if not marked:
            raise OffloadError("it changed while it was being checked")
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            with SessionLocal() as db:
                db.execute(update(Photo).where(Photo.id == row.id).values(offloaded_at=None))
                db.commit()
            raise
        with SessionLocal() as db:
            db.execute(update(Photo).where(Photo.id == row.id).values(preview_sig=""))
            db.commit()
        _unlink(preview_path(row.sig))

    def run(self) -> str:
        """Run to the end; returns the job's final status."""
        progress = {"done": 0, "total": 0, "offloaded": 0, "freed": 0}
        if not self.settings.offload_enabled:
            return self._end("failed", "offload is off",
                             "Offloading is off ([offload] after = 0 in meerpic.toml, or sync is off).",
                             progress)
        with SessionLocal() as db:
            rows = candidates(db, self.settings, every=self.dry_run)
        total = len(rows)
        progress["total"] = total
        if not rows:
            return self._end("done", "Nothing to offload", None, progress)

        try:
            jobs.update_job(self.job_id, message=f"checking {total} video(s) against iCloud",
                            progress=progress)
            listed = self._list(sorted({row.root for row in rows}))
        except rclone.Stopped:
            return self._stopped_end(progress)
        except (AlbumsError, rclone.RcloneError) as exc:
            error = auth_hint(self.settings) if getattr(exc, "auth", False) else str(exc)
            return self._end("failed", "offload failed", error, progress)
        offloaded = freed = kept = 0
        # An empty listing is a broken session as likely as an emptied
        # library: nothing is checked against it. The other roots go on.
        empty = sorted(root for root, names in listed.items() if not names)
        if len(empty) == len(listed):
            return self._end("failed", "offload failed",
                             "iCloud listed no photos at all; nothing was deleted", progress)
        if empty:
            held = [row for row in rows if row.root in empty]
            rows = [row for row in rows if row.root not in empty]
            kept, total = len(held), len(rows)
            self._say(f"kept {kept} video(s) in {', '.join(empty)}: iCloud listed no photos there")
        for i, row in enumerate(rows):
            progress = {"done": i, "total": total, "offloaded": offloaded, "freed": freed}
            if self._should_stop():
                return self._stopped_end(progress)
            jobs.update_job(self.job_id, progress=progress,
                            message=f"checking {row.name} ({i + 1}/{total})")
            reason = self.check(row, listed[row.root].get(row.name))
            if reason:
                kept += 1
                self._say(f"kept {row.name}: {reason}")
                continue
            if self.dry_run:
                offloaded += 1
                freed += row.size
                self._say(f"would offload {row.name} ({_human_bytes(row.size)})")
                continue
            try:
                self.offload(row)
            except (OffloadError, OSError) as exc:
                kept += 1
                self._say(f"kept {row.name}: {exc}")
                log(f"offload: {row.name}: {exc}", error=True)
                continue
            offloaded += 1
            freed += row.size
            self._say(f"offloaded {row.name} ({_human_bytes(row.size)})")
        progress = {"done": total, "total": total, "offloaded": offloaded, "freed": freed}
        verb = "Would offload" if self.dry_run else "Offloaded"
        message = f"{verb} {offloaded} video(s), {_human_bytes(freed)}"
        if kept:
            message += f"; {kept} kept"
        return self._end("done", message, None, progress)


class FetchRun(_Run):
    """One fetch job: the original of ``params.photo_id``, back from iCloud."""

    kind = "fetch"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._folder: Path | None = None
        self._row: Candidate | None = None

    def _tick(self) -> bool:
        """Polled about once a second while rclone runs: the download's
        progress (what has arrived in its folder so far), and whether to stop."""
        row, folder = self._row, self._folder
        if row is not None and folder is not None:
            try:
                got = sum(entry.stat().st_size for entry in os.scandir(folder) if entry.is_file())
            except OSError:
                got = 0
            percent = round(min(100.0, 100.0 * got / row.size), 1) if row.size else None
            try:
                jobs.update_job(self.job_id, progress={"bytes": got, "total_bytes": row.size,
                                                       "percent": percent},
                                message=f"downloading {row.name} ({_human_bytes(got)} of "
                                        f"{_human_bytes(row.size)})")
            except Exception:  # noqa: BLE001 - a missed progress line must not end a download
                pass
        return self._should_stop()

    def _cleanup(self) -> None:
        if self._folder is not None:
            shutil.rmtree(self._folder, ignore_errors=True)

    def run(self) -> str:
        progress: dict = {}
        try:
            photo_id = int(self.params.get("photo_id"))
        except (TypeError, ValueError):
            return self._end("failed", "bad request", "params.photo_id is missing", progress)
        with SessionLocal() as db:
            found = db.execute(_columns().where(Photo.id == photo_id)).first()
        if found is None:
            return self._end("failed", "not found", "photo not found", progress)
        row = self._row = Candidate(*found)
        dest = original_path(row.root, row.rel_path)
        if dest is None:
            return self._end("failed", "not here", f"library root {row.root!r} is not configured",
                             progress)
        # First: a row that stepped aside (agent/albums.py) has its small
        # copy for a path, which is a file, and no original anywhere.
        remote = remote_of(self.settings, row.root)
        if remote is None or not from_remote(row, self.settings):
            return self._end("failed", "not in iCloud",
                             "This file did not come from iCloud, so there is nowhere to fetch it from.",
                             progress)
        if dest.is_file():
            self._back(row)
            return self._end("done", "already here", None, progress)

        self._folder = offload_path(row.root) / FETCHING / str(self.job_id)
        try:
            self._folder.mkdir(parents=True, exist_ok=True)
            tmp = self._folder / row.name
            argv = [self.settings.rclone, "copyto", remote_path(remote, row.name), str(tmp)]
            jobs.update_job(self.job_id, message=f"downloading {row.name}",
                            progress={"bytes": 0, "total_bytes": row.size, "percent": 0.0})
            result = rclone.run(argv, timeout=300.0 + row.size / SLOWEST, should_stop=self._tick)
            if not result.ok:
                error = auth_hint(self.settings) if result.auth else result.reason()
                return self._end("failed", "could not download the original", error, progress)
            try:
                st = tmp.stat()
            except OSError:
                return self._end("failed", "could not download the original",
                                 "rclone finished without a file", progress)
            if st.st_size != row.size or not same_time(row.mtime_ns, st.st_mtime_ns // 1_000_000_000):
                return self._end("failed", "iCloud has another file now",
                                 f"iCloud's {row.name} is not the video kept here "
                                 f"({_human_bytes(st.st_size)}, not {_human_bytes(row.size)}); "
                                 "nothing was changed", progress)
            # Within the second rclone keeps; to the nanosecond, so the sig is the same.
            if st.st_mtime_ns != row.mtime_ns:
                os.utime(tmp, ns=(st.st_atime_ns, row.mtime_ns))
            # Moved before the row is unmarked: an unmarked row without its
            # file is a deleted photo to the scan.
            if not os.path.lexists(dest):
                os.rename(tmp, dest)
        except rclone.Stopped:
            return self._stopped_end(progress)
        except (rclone.RcloneError, OSError) as exc:
            return self._end("failed", "could not download the original", str(exc), progress)
        finally:
            self._cleanup()
        self._back(row)
        return self._end("done", "downloaded", None,
                         {"bytes": row.size, "total_bytes": row.size, "percent": 100.0})

    def _back(self, row: Candidate) -> None:
        """The original is here again: say so, and have it made playable."""
        with SessionLocal() as db:
            db.execute(update(Photo).where(Photo.id == row.id)
                       .values(offloaded_at=None, fetched_at=utcnow()))
            db.commit()
            photo = db.get(Photo, row.id)
            ready = photo is not None and photo.preview_sig == photo.sig and preview_path(photo.sig).is_file()
        if not ready:
            job = jobs.create("preview", {"photo_id": row.id, "reason": "fetch"})
            self._say(f"queued its playable copy (job {job.id})")
