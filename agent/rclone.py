"""Short rclone commands: an album listing, one file's deletion.

The sync streams rclone's JSON log for minutes (agent/sync.py); these are
different: a command that answers within seconds or a minute, whose output is
read as a whole. They still run in a session of their own and are polled, so
Cancel in the UI and a stopping agent end them instead of waiting out a
listing of 25,000 photos.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from .sync import looks_like_auth

TERM_GRACE = 10.0
POLL = 1.0

# rclone's plain-text log prefix: "2026/09/29 12:08:59 ERROR : ".
_PREFIX = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} (?:[A-Z]+\s*:\s*)?")


class RcloneError(RuntimeError):
    def __init__(self, message: str, *, auth: bool = False):
        super().__init__(message)
        self.auth = auth


class Stopped(Exception):
    """The command was ended on purpose: a cancel or a stopping agent."""


@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def auth(self) -> bool:
        return looks_like_auth(self.err)

    def reason(self) -> str:
        """The line that explains a failure, without rclone's timestamp."""
        lines = [_PREFIX.sub("", line).strip() for line in self.err.splitlines() if line.strip()]
        # rclone retries and then sums up ("Failed to deletefile: ..."); the
        # summary is the line to show, an ERROR line the next best.
        for marker in ("Failed to", "CRITICAL", "ERROR"):
            hits = [line for raw, line in zip(self.err.splitlines(), lines) if marker in raw]
            if hits:
                return hits[-1]
        return lines[-1] if lines else f"rclone exited with status {self.code}"


def terminate(proc: subprocess.Popen) -> None:
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


def run(argv: list[str], *, timeout: float, should_stop: Callable[[], bool] | None = None) -> Result:
    """Run to the end and return what it said. Raises RcloneError when it
    cannot start or runs past ``timeout``, Stopped when ``should_stop``
    turned true meanwhile (checked about once a second)."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        raise RcloneError(f"rclone could not be started ({argv[0]}): {exc.strerror or exc}") from exc
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=POLL)
                break
            except subprocess.TimeoutExpired:
                if should_stop is not None and should_stop():
                    raise Stopped() from None
                if time.monotonic() > deadline:
                    raise RcloneError(f"rclone {argv[1] if len(argv) > 1 else ''} timed out".strip()) from None
    except BaseException:
        terminate(proc)
        proc.communicate()
        raise
    return Result(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
