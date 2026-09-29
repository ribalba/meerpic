"""The agent's heartbeat: ``Setting('agent.status')``, every two seconds.

It is how the UI knows the agent is alive (seen within 30 s), what it is
doing, and how much is left. It runs on a thread of its own rather than in
the loop, because one loop turn can be several seconds of thumbnails and
vectors, and a heartbeat that stops while the agent is at its busiest would
say "not running" exactly when it is running hardest.

The pending counts are one pass of filtered COUNTs over the photos table,
cheap at 25,000 rows but not free, so they are refreshed every ten seconds,
not every beat.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable

from sqlalchemy.dialects.postgresql import insert as pg_insert

from core import embed
from core.config import Settings
from core.database import SessionLocal
from core.models import Setting
from core.timeutil import utcnow
from core.version import VERSION

from . import jobs
from .log import log

BEAT = 2.0
COUNT_EVERY = 10.0
KEY = "agent.status"


def iso_z(dt) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"


def write(value: dict) -> None:
    with SessionLocal() as db:
        stmt = pg_insert(Setting).values(key=KEY, value=value, updated_at=utcnow())
        db.execute(stmt.on_conflict_do_update(
            index_elements=[Setting.key],
            set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
        ))
        db.commit()


class Heartbeat:
    def __init__(self, settings: Settings, phase: Callable[[], str],
                 job_ids: Callable[[], list[int]]):
        self.settings = settings
        self.phase = phase
        self.job_ids = job_ids
        self.pending: dict = {}
        self._counted = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failing = False

    def value(self) -> dict:
        return {
            "version": VERSION,
            "host": socket.gethostname(),
            "seen_at": iso_z(utcnow()),
            "phase": self.phase(),
            "workers": self.settings.workers,
            "model": embed.model_name(),
            "pending": self.pending,
        }

    def beat(self) -> None:
        from .pipeline import pending

        now = time.monotonic()
        if now - self._counted >= COUNT_EVERY or not self.pending:
            with SessionLocal() as db:
                self.pending = pending(db, self.settings)
            self._counted = now
        write(self.value())
        jobs.heartbeat(self.job_ids())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.beat()
                if self._failing:
                    log("heartbeat: database reachable again")
                self._failing = False
            except Exception as exc:  # noqa: BLE001 - a database restart must not kill the thread
                if not self._failing:
                    log(f"heartbeat: {type(exc).__name__}: {exc}", error=True)
                self._failing = True
            self._stop.wait(BEAT)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop beating, after one last beat with fresh counts, so what the
        UI reads afterwards is how things were left, not a minute before."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._counted = float("-inf")
        try:
            self.beat()
        except Exception as exc:  # noqa: BLE001 - shutting down either way
            log(f"heartbeat: {type(exc).__name__}: {exc}", error=True)
