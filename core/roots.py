"""Which library folders are there right now.

A library root on a network share (or a USB disk) is not always there: the
share answers only at home, the disk is in a drawer. While a root is away
its photos are in no listing (app/query.py, ``never_listed``), the agent
neither scans nor indexes it (agent/scan.py, agent/stages.py), and its rows
stay exactly as they are, so everything is back the moment the folder is.

A root counts as there when its folder can be listed. Existing is not
enough: an automount point exists while its share is away, and listing it
is what asks the share. Empty counts as there. A new folder holds nothing
yet, and the scanner already refuses to take an empty root for a library
whose every photo was deleted.

Each check runs in a thread of its own, with a deadline. A share that went
away while mounted does not fail, it hangs (a CIFS mount until its timeout,
an NFS hard mount for ever), and a web request must not hang with it. A
check still running past the deadline is the answer "not there", and the
next one starts only after it has returned. Answers are kept for
``CHECK_EVERY`` seconds, and an older answer is handed out at once while a
new check runs behind it, so a request waits only on the very first check.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings, get_settings
from .models import Photo

# Seconds an answer is good for.
CHECK_EVERY = 15.0
# Seconds a check may take before the folder counts as not there. Listing a
# local folder or a share that answers is milliseconds.
DEADLINE = 3.0


@dataclass
class _Check:
    started: float
    done: threading.Event = field(default_factory=threading.Event)
    there: bool = False


_lock = threading.Lock()
_answers: dict[Path, tuple[bool, float]] = {}     # folder -> (there, when)
_checks: dict[Path, _Check] = {}                  # folder -> the check running on it


def listable(path: Path) -> bool:
    try:
        with os.scandir(path) as entries:
            next(entries, None)
    except OSError:
        return False
    return True


def _run(path: Path, check: _Check) -> None:
    check.there = listable(path)
    with _lock:
        _answers[path] = (check.there, time.monotonic())
        if _checks.get(path) is check:
            del _checks[path]
    check.done.set()


def offline(settings: Settings | None = None, *, fresh: bool = False) -> frozenset[str]:
    """The names of the roots whose folder is not there right now.

    ``fresh`` checks again now instead of believing an answer up to
    ``CHECK_EVERY`` old, for the agent, which is about to walk the folder or
    to blame a file for a failure. Either way this waits ``DEADLINE`` at most.
    """
    settings = settings or get_settings()
    now = time.monotonic()
    known: dict[str, bool] = {}
    waiting: dict[str, _Check] = {}
    with _lock:
        for name, path in settings.roots.items():
            answer = _answers.get(path)
            if answer is not None and not fresh and now - answer[1] < CHECK_EVERY:
                known[name] = answer[0]
                continue
            check = _checks.get(path)
            if check is None:
                check = _checks[path] = _Check(started=now)
                threading.Thread(target=_run, args=(path, check), daemon=True,
                                 name=f"meerpic-roots:{name}").start()
            elif now - check.started >= DEADLINE:
                # Still hanging from before: that is the answer.
                _answers[path] = (False, now)
                known[name] = False
                continue
            if answer is not None and not fresh:
                known[name] = answer[0]
            else:
                waiting[name] = check
    for name, check in waiting.items():
        left = DEADLINE - (time.monotonic() - check.started)
        known[name] = check.done.wait(max(0.0, left)) and check.there
    return frozenset(name for name, there in known.items() if not there)


def reachable(settings: Settings | None = None) -> list:
    """WHERE clauses for a select on photos: "in a root that is there". No
    clause at all while every root is, which is the usual case."""
    away = offline(settings)
    return [Photo.root.not_in(sorted(away))] if away else []


def forget() -> None:
    """Drop every answer, for tests that move folders around."""
    with _lock:
        _answers.clear()
