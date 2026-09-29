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
``deleted_files`` table).

The parser (``Tracker``) is separate from the process handling (``SyncRun``)
so it can be tested line by line on recorded output.
"""

from __future__ import annotations

import json
import os
import queue
import re
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
from core.models import DeletedFile
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


# What an rclone glob gives a meaning to; a backslash takes it away.
_GLOB = re.compile(r"([\\*?\[\]{}])")


def build_argv(settings: Settings, exclude_from: str | None = None) -> list[str]:
    return [
        settings.rclone, "copy", settings.sync_remote,
        str(settings.root_path(settings.sync_root)),
        "--use-json-log", "--stats", "2s", "--stats-log-level", "NOTICE", "-v",
        *(["--exclude-from", exclude_from] if exclude_from else []),
        *settings.sync_args,
    ]


def exclude_rule(name: str) -> str | None:
    """A filter rule that matches this one name at the top of the remote and
    nothing else. None for a name no rule can hold: rclone reads one rule per
    line and trims each (the scan still knows such a file by its content)."""
    if not name or name != name.strip() or "\n" in name or "\r" in name:
        return None
    return "/" + _GLOB.sub(r"\\\1", name)


def excluded_names(settings: Settings) -> list[str]:
    """The names of the photos deleted here, which every sync leaves in iCloud."""
    with SessionLocal() as db:
        return list(db.scalars(
            select(DeletedFile.name).distinct()
            .where(DeletedFile.root == settings.sync_root, DeletedFile.name.is_not(None))
            .order_by(DeletedFile.name)
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


def auth_hint(settings: Settings) -> str:
    remote = settings.sync_remote.split(":", 1)[0] or "iclouddrive"
    return f"The iCloud session has expired. In a terminal run: rclone config reconnect {remote}:"


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
    """One rclone process for one job, reported into the job's row."""

    def __init__(self, job_id: int, settings: Settings, *,
                 echo: Callable[[str], None] | None = None):
        self.job_id = job_id
        self.settings = settings
        self.echo = echo
        self.tracker = Tracker()
        self._stop = threading.Event()
        self._db_failing = False
        self.status = "running"

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

    def _flush(self) -> None:
        p = self.tracker.progress
        if self.echo:
            self.echo(describe(p))
        lines = self.tracker.take_lines()
        try:
            if lines:
                jobs.append_log(self.job_id, lines)
            jobs.update_job(self.job_id, progress=dict(p), message=describe(p))
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
        names = excluded_names(self.settings)
        exclude_from = write_excludes(names)
        try:
            return self._run(build_argv(self.settings, exclude_from), len(names))
        finally:
            if exclude_from:
                try:
                    os.unlink(exclude_from)
                except OSError:
                    pass

    def _run(self, argv: list[str], excluded: int) -> str:
        log(f"sync: {' '.join(argv[:4])}" + (f", leaving out {excluded} deleted here" if excluded else ""))
        if excluded:
            jobs.append_log(self.job_id, [f"leaving out {excluded} file(s) deleted here"])
        jobs.update_job(self.job_id, message="starting rclone", progress=empty_progress())
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
        except OSError as exc:
            error = f"rclone could not be started ({self.settings.rclone}): {exc.strerror or exc}"
            jobs.finish(self.job_id, "failed", message="sync failed", error=error)
            log(f"sync: {error}", error=True)
            self.status = "failed"
            return self.status

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

        progress = dict(self.tracker.progress)
        if cancelled:
            self.status = "cancelled"
            jobs.finish(self.job_id, "cancelled", message="cancelled", progress=progress)
        elif stopped:
            self.status = "failed"
            jobs.finish(self.job_id, "failed", message="agent stopped", error="agent stopped",
                        progress=progress)
        elif code == 0:
            self.status = "done"
            message = done_message(self.tracker.copied_media)
            jobs.finish(self.job_id, "done", message=message, progress=progress)
            log(f"sync: {message}")
        else:
            self.status = "failed"
            if self.tracker.auth:
                error = auth_hint(self.settings)
            else:
                error = self.tracker.last_error or f"rclone exited with status {code}"
            jobs.finish(self.job_id, "failed", message="sync failed", error=error, progress=progress)
            log(f"sync: failed: {error}", error=True)
        return self.status


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
