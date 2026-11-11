"""Sync: ``rclone copy`` from iCloud into the library, as a job with progress.

The command is the one the user already runs by hand,

    rclone copy 'iclouddrive:PrimarySync/All Photos' ~/Pictures/iCloud --progress

except for ``--progress``, which draws a terminal UI with cursor movements
that nothing can parse. ``--use-json-log --stats 2s`` makes rclone print the
same numbers as one JSON object every two seconds, plus a JSON line per file
copied and per error, and that is what the UI's progress bar is made of.

``copy``, never ``sync``: a photo deleted in iCloud stays on disk. The
library folder is the user's backup as much as it is this app's input, and
a mirror would let one mistaken deletion on the phone reach it.

The other way round, a photo deleted here stays in iCloud (rclone cannot
delete there; see agent/delete.py), and every copy excludes it by name
(``--exclude-from``, a file of rules written for this one run from the
``deleted_files`` table). So does an offloaded video, whose original is
meant to be in iCloud only (agent/offload.py).

The parser (``Tracker``) is separate from the process handling (``SyncRun``)
so it can be tested line by line on recorded output.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select

from core.config import Settings
from core.database import SessionLocal
from core.media import classify
from core.models import DeletedFile, Photo
from core.timeutil import utcnow

from . import jobs
from .log import log

# What an expired iCloud session looks like in rclone's output: Apple wants
# the trust token renewed every few weeks, and the only fix is interactive.
_AUTH = re.compile(
    r"authenticat|\b2fa\b|two[- ]factor|trust[ _-]?token|\b401\b|\b403\b|reconnect", re.IGNORECASE
)
_ERRORISH = re.compile(r"\b(error|failed|critical|fatal)\b", re.IGNORECASE)
_COPIED = ("Copied (new)", "Copied (replaced existing)")
_ERROR_LEVELS = {"error", "critical", "fatal", "emergency", "alert"}

PROGRESS_EVERY = 1.0      # seconds between progress writes
CANCEL_EVERY = 1.0
TERM_GRACE = 10.0
# rclone's exit status for "stopped at --max-transfer".
MAX_TRANSFER_EXIT = 8


# What an rclone glob gives a meaning to; a backslash takes it away.
_GLOB = re.compile(r"([\\*?\[\]{}])")


def build_argv(settings: Settings, exclude_from: str | None = None, *,
               root: str | None = None, max_transfer: int | None = None) -> list[str]:
    """The ``rclone copy`` of one remote into its root (``sync.root`` unless
    another of ``settings.sync_sources`` is named)."""
    root = root or settings.sync_root
    return [
        settings.rclone, "copy", settings.sync_sources[root], str(settings.root_path(root)),
        "--use-json-log", "--stats", "2s", "--stats-log-level", "NOTICE", "-v",
        *(["--exclude-from", exclude_from] if exclude_from else []),
        # In bytes: a bare number is KiB to rclone. Cautious: no file starts
        # that would go past the limit (soft lets four 4K videos overshoot it).
        *(["--max-transfer", f"{max_transfer}B", "--cutoff-mode", "cautious"] if max_transfer else []),
        *settings.sync_args,
    ]


def room(settings: Settings, root: str) -> int | None:
    """Bytes a copy into this root may bring before the disk is down to
    ``sync.keep_free``: None for no limit, 0 or less for none at all."""
    if settings.sync_keep_free <= 0 or any(a.startswith("--max-transfer") for a in settings.sync_args):
        return None
    path = settings.root_path(root)
    # The root is made by its first copy; until then its disk is a parent's.
    while path is not None and not path.exists() and path.parent != path:
        path = path.parent
    try:
        free = shutil.disk_usage(path).free
    except (OSError, TypeError):
        return None
    return free - int(settings.sync_keep_free * 1e9)


def exclude_rule(name: str) -> str | None:
    """A filter rule that matches this one name at the top of the remote and
    nothing else. None for a name no rule can hold: rclone reads one rule per
    line and trims each (the scan still knows such a file by its content)."""
    if not name or name != name.strip() or "\n" in name or "\r" in name:
        return None
    return "/" + _GLOB.sub(r"\\\1", name)


def excluded_names(settings: Settings, root: str | None = None) -> list[str]:
    """The names of the photos deleted here, which every sync leaves in
    iCloud. One root's (``sync.root``'s unless named): names repeat across
    remotes."""
    with SessionLocal() as db:
        return list(db.scalars(
            select(DeletedFile.name).distinct()
            .where(DeletedFile.root == (root or settings.sync_root), DeletedFile.name.is_not(None))
            .order_by(DeletedFile.name)
        ))


def offloaded_names(settings: Settings, root: str | None = None) -> list[str]:
    """The names of the videos offloaded here, whose originals every sync
    leaves in iCloud until somebody asks for one. One root's, as above."""
    with SessionLocal() as db:
        return list(db.scalars(
            select(Photo.name)
            .where(Photo.root == (root or settings.sync_root), Photo.offloaded_at.is_not(None),
                   ~Photo.rel_path.contains("/"))
            .order_by(Photo.name)
        ))


def write_excludes(names: list[str]) -> str | None:
    """The rules for ``--exclude-from`` in a file of their own; its path, or
    None when there is nothing to exclude."""
    rules = [rule for rule in map(exclude_rule, names) if rule]
    if not rules:
        return None
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="meerpic-excluded-",
                                     suffix=".txt", delete=False) as fh:
        fh.write("".join(rule + "\n" for rule in rules))
    return fh.name


def looks_like_auth(text: str) -> bool:
    """Whether rclone's output says the iCloud session needs renewing."""
    return bool(_AUTH.search(text or ""))


def auth_hint(settings: Settings, remote: str | None = None) -> str:
    name = (remote or settings.sync_remote).split(":", 1)[0] or "iclouddrive"
    return f"The iCloud session has expired. In a terminal run: rclone config reconnect {name}:"


def empty_progress() -> dict:
    return {
        "bytes": 0, "total_bytes": 0, "files": 0, "total_files": 0,
        "checks": 0, "total_checks": 0, "speed": 0.0, "eta": None,
        "errors": 0, "current": [], "percent": None, "copied": 0,
    }


def _number(value, default=0):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


class Tracker:
    """rclone's output, one line at a time, as the job's progress."""

    def __init__(self):
        self.progress = empty_progress()
        self.copied = 0
        # Of those, the ones the library will show: "12 new photos" should
        # not count a sidecar or a stray file rclone brought along.
        self.copied_media = 0
        self.error_lines = 0
        self.last_error = ""
        self.auth = False
        self.lines: list[str] = []      # for job.log, drained by the runner

    def feed(self, line: str) -> bool:
        """Take one line. True when ``progress`` changed."""
        text = line.strip()
        if not text:
            return False
        obj = None
        if text.startswith("{"):
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                obj = None
        if not isinstance(obj, dict):
            # Before logging is set up (a bad flag, a missing config) rclone
            # prints plain text, and that is usually the line that explains.
            self.lines.append(text)
            if _AUTH.search(text):
                self.auth = True
            if _ERRORISH.search(text):
                self.last_error = text
            return False

        msg = str(obj.get("msg") or "").strip()
        level = str(obj.get("level") or "").lower()
        name = str(obj.get("object") or "")

        stats = obj.get("stats")
        if isinstance(stats, dict):
            self._stats(stats)
            return True

        if any(marker in msg for marker in _COPIED):
            # Older rclone put the name in the message ("x.HEIC: Copied (new)").
            if not name and ": " in msg:
                name = msg.split(": ", 1)[0]
            self.copied += 1
            if classify(name.rsplit("/", 1)[-1]) is not None:
                self.copied_media += 1
            self.progress["copied"] = self.copied
            self.lines.append(f"copied {name}")
            return True

        if level in _ERROR_LEVELS or msg.startswith("Failed to"):
            text = f"{name}: {msg}" if name else msg
            if level in _ERROR_LEVELS:
                self.error_lines += 1
                self.progress["errors"] = max(self.progress["errors"], self.error_lines)
            self.last_error = text
            self.lines.append(f"{level.upper() or 'ERROR'} {text}")
            if _AUTH.search(text):
                self.auth = True
            return True
        return False

    def _stats(self, s: dict) -> None:
        p = self.progress
        p["bytes"] = int(_number(s.get("bytes")))
        p["total_bytes"] = int(_number(s.get("totalBytes")))
        p["files"] = int(_number(s.get("transfers")))
        p["total_files"] = int(_number(s.get("totalTransfers")))
        p["checks"] = int(_number(s.get("checks")))
        p["total_checks"] = int(_number(s.get("totalChecks")))
        p["speed"] = float(_number(s.get("speed"), 0.0))
        eta = s.get("eta")
        p["eta"] = eta if isinstance(eta, (int, float)) and not isinstance(eta, bool) else None
        p["errors"] = max(int(_number(s.get("errors"))), self.error_lines)
        p["current"] = [
            str(t.get("name") or "") for t in (s.get("transferring") or []) if isinstance(t, dict)
        ][:10]
        if p["total_bytes"] > 0:
            p["percent"] = round(min(100.0, 100.0 * p["bytes"] / p["total_bytes"]), 1)
        elif p["total_files"] > 0:
            p["percent"] = round(min(100.0, 100.0 * p["files"] / p["total_files"]), 1)
        else:
            p["percent"] = None
        if s.get("lastError"):
            self.last_error = str(s["lastError"])

    def take_lines(self) -> list[str]:
        out, self.lines = self.lines, []
        return out


def done_message(copied: int) -> str:
    if copied == 0:
        return "No new photos"
    return f"{copied} new photo" + ("" if copied == 1 else "s")


def paused_message(copied: int, keep_free: float) -> str:
    """A sync that stopped to leave ``sync.keep_free`` on the disk."""
    why = f"the disk is down to {keep_free:g} GB free (sync.keep_free)"
    if copied == 0:
        return f"Paused before copying anything: {why}"
    return f"{done_message(copied)}, then paused: {why}"


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def describe(p: dict) -> str:
    """One line of progress, for ``--sync`` in a terminal."""
    parts = [f"{p['files']}/{p['total_files']} files",
             f"{_human_bytes(p['bytes'])} / {_human_bytes(p['total_bytes'])}"]
    if p.get("speed"):
        parts.append(f"{_human_bytes(p['speed'])}/s")
    if p.get("eta") is not None:
        parts.append(f"ETA {timedelta(seconds=int(p['eta']))}")
    if p.get("errors"):
        parts.append(f"{p['errors']} error(s)")
    return ", ".join(parts)


class SyncRun:
    """One sync job: an rclone process per remote (``sync.remote``, then each
    of ``sync.remotes``), one after the other, reported into the job's row."""

    def __init__(self, job_id: int, settings: Settings, *,
                 echo: Callable[[str], None] | None = None):
        self.job_id = job_id
        self.settings = settings
        self.echo = echo
        self.tracker = Tracker()
        self._stop = threading.Event()
        self._db_failing = False
        self.status = "running"
        self.error = ""
        # The remotes copied before this one: their files count in "N new".
        self._copied_before = 0
        # Which remote of how many, when there is more than one.
        self._step: dict = {}

    def stop(self) -> None:
        """Ask a running sync to end (the agent is shutting down)."""
        self._stop.set()

    def _terminate(self, proc: subprocess.Popen) -> None:
        # rclone runs in a session of its own so the signal reaches it and
        # anything it started, and not this process.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            return
        try:
            proc.wait(timeout=TERM_GRACE)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass

    def _progress(self) -> dict:
        p = dict(self.tracker.progress)
        p["copied"] = self._copied_before + self.tracker.copied
        return p | self._step

    def _flush(self) -> None:
        p = self._progress()
        if self.echo:
            self.echo(describe(p))
        lines = self.tracker.take_lines()
        try:
            if lines:
                jobs.append_log(self.job_id, lines)
            jobs.update_job(self.job_id, progress=p, message=describe(p))
        except Exception as exc:  # noqa: BLE001 - a database blip must not end a 100 GB download
            self.tracker.lines[:0] = lines          # keep them for the next write
            if not self._db_failing:
                log(f"sync: cannot write progress: {type(exc).__name__}: {exc}", error=True)
            self._db_failing = True
            return
        self._db_failing = False

    def _cancel_requested(self) -> bool:
        try:
            return jobs.cancel_requested(self.job_id)
        except Exception:  # noqa: BLE001 - unknown means "not cancelled"; retried in a second
            return False

    def run(self) -> str:
        """Run to the end; returns the job's final status."""
        roots = list(self.settings.sync_sources)
        media = 0
        full = False
        for step, root in enumerate(roots, 1):
            self._copied_before += self.tracker.copied
            self.tracker = Tracker()
            if len(roots) > 1:
                self._step = {"source": root, "step": step, "steps": len(roots)}
                line = f"copying {self.settings.sync_sources[root]} into {root} ({step}/{len(roots)})"
                jobs.append_log(self.job_id, [line])
            # A root whose disk is full waits for the next sync. The next root
            # may be on another disk; on this one it finds no room either.
            budget = room(self.settings, root)
            if budget is not None and budget <= 0:
                full = True
                jobs.append_log(self.job_id, [f"no room left for {root} on its disk"])
                continue
            result = self._copy(root, budget)
            media += self.tracker.copied_media
            if result == "full":
                full = True
                continue
            if result != "done":
                return self._failed(result, root, len(roots))
        progress = self._progress()
        self.status = "done"
        if full:
            # rclone counts stopping at --max-transfer as errors; it is not one.
            progress["errors"] = 0
            message = paused_message(media, self.settings.sync_keep_free)
            jobs.append_log(self.job_id, [message])
        else:
            message = done_message(media)
        jobs.finish(self.job_id, "done", message=message, progress=progress)
        log(f"sync: {message}")
        return self.status

    def _failed(self, result: str, root: str, remotes: int) -> str:
        progress = self._progress()
        if result == "cancelled":
            self.status = "cancelled"
            jobs.finish(self.job_id, "cancelled", message="cancelled", progress=progress)
            return self.status
        self.status = "failed"
        if result == "stopped":
            jobs.finish(self.job_id, "failed", message="agent stopped", error="agent stopped",
                        progress=progress)
            return self.status
        error = self.error
        if remotes > 1 and result == "failed" and not self.tracker.auth:
            error = f"{self.settings.sync_sources[root]}: {error}"
        jobs.finish(self.job_id, "failed", message="sync failed", error=error, progress=progress)
        log(f"sync: failed: {error}", error=True)
        return self.status

    def _copy(self, root: str, budget: int | None) -> str:
        """One ``rclone copy`` into ``root``, leaving out what was deleted or
        offloaded there: "done", "full" (stopped at ``sync.keep_free``),
        "cancelled", "stopped", "unstarted" or "failed" (see ``error``)."""
        deleted = excluded_names(self.settings, root)
        offloaded = offloaded_names(self.settings, root)
        exclude_from = write_excludes(sorted(set(deleted) | set(offloaded)))
        try:
            argv = build_argv(self.settings, exclude_from, root=root, max_transfer=budget)
            return self._run(argv, len(deleted), len(offloaded), limited=budget is not None)
        finally:
            if exclude_from:
                try:
                    os.unlink(exclude_from)
                except OSError:
                    pass

    def _run(self, argv: list[str], excluded: int, offloaded: int = 0, *, limited: bool = False) -> str:
        left_out = []
        if excluded:
            left_out.append(f"{excluded} file(s) deleted here")
        if offloaded:
            left_out.append(f"{offloaded} video(s) offloaded")
        what = " and ".join(left_out)
        log(f"sync: {' '.join(argv[:4])}" + (f", leaving out {what}" if what else ""))
        if what:
            jobs.append_log(self.job_id, [f"leaving out {what}"])
        jobs.update_job(self.job_id, message="starting rclone", progress=self._progress())
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
        except OSError as exc:
            self.error = f"rclone could not be started ({self.settings.rclone}): {exc.strerror or exc}"
            return "unstarted"

        lines: queue.Queue = queue.Queue()

        def reader() -> None:
            assert proc.stdout is not None
            for raw in proc.stdout:
                lines.put(raw.decode("utf-8", "replace"))
            lines.put(None)

        threading.Thread(target=reader, name="rclone-reader", daemon=True).start()

        cancelled = stopped = False
        last_write = last_cancel = 0.0
        try:
            while True:
                try:
                    line = lines.get(timeout=0.5)
                except queue.Empty:
                    line = ""
                if line is None:
                    break
                if line:
                    self.tracker.feed(line)
                now = time.monotonic()
                if now - last_write >= PROGRESS_EVERY:
                    self._flush()
                    last_write = now
                if not (cancelled or stopped) and now - last_cancel >= CANCEL_EVERY:
                    last_cancel = now
                    if self._stop.is_set():
                        stopped = True
                        self._terminate(proc)
                    elif self._cancel_requested():
                        cancelled = True
                        log("sync: cancelled")
                        self._terminate(proc)
        except BaseException:
            # Whatever ended the loop, rclone must not outlive it.
            self._terminate(proc)
            raise
        code = proc.wait()
        self._flush()

        if cancelled:
            return "cancelled"
        if stopped:
            return "stopped"
        if code == 0:
            return "done"
        if code == MAX_TRANSFER_EXIT and limited:
            return "full"
        if self.tracker.auth:
            self.error = auth_hint(self.settings, argv[2])
        else:
            self.error = self.tracker.last_error or f"rclone exited with status {code}"
        return "failed"


def due(settings: Settings) -> bool:
    """Whether the timer wants a sync now: ``sync.interval`` minutes after
    the last one was asked for, and none queued or running already."""
    if not settings.sync_enabled or settings.sync_interval <= 0:
        return False
    if jobs.active("sync") is not None:
        return False
    last = jobs.latest("sync")
    if last is None:
        return True
    return utcnow() - last.created_at >= timedelta(minutes=settings.sync_interval)
