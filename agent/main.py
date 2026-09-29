"""meerpic-agent: walks the library, indexes it, and runs the Sync button.

    python -m agent.main                    # the service: loop forever
    python -m agent.main --once             # scan + every stage until done, exit
    python -m agent.main --once --limit 30  # ... for the newest 30 files only
    python -m agent.main --test             # check every dependency, change nothing
    python -m agent.main --sync             # one rclone sync in the foreground
    python -m agent.main --albums           # read favourites and albums from iCloud
    python -m agent.main --reindex thumbs   # make a stage pending again, exit
                                            # (with --once: and run it now)

The loop, roughly every second: claim queued jobs; scan when it is time (every
``agent.scan_interval`` seconds, every 30 while a sync is downloading, and
right after one finishes); run one small batch of each pipeline stage. A heartbeat
thread tells the UI the agent is alive whatever the loop is busy with.

Everything that talks to iCloud (a sync, the album refresh that follows it,
a delete) runs on one thread of its own, one job at a time: a first download
of 100 GB does not hold up the thumbnails of what has already arrived, and a
delete never runs while a sync or a listing is looking at the same files. A
job of those kinds that arrives meanwhile waits in the queue.

Nothing that goes wrong with one file, one job or one turn ends the loop: it
is logged, recorded where the UI can show it, and the loop goes on. The only
ways out are a signal and a broken configuration.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
import traceback

from sqlalchemy import select

from core.config import get_settings
from core.database import SessionLocal, init_db
from core.models import Photo
from core.version import VERSION

from . import albums, checks, delete, jobs, scan, sync
from .log import log, setup
from .pipeline import Pipeline, pending
from .status import Heartbeat

# While rclone is downloading, scan this often so photos appear as they land.
SCAN_WHILE_SYNCING = 30.0
# How often the sync timer (``sync.interval``) is looked at.
SYNC_TIMER_EVERY = 60.0
# Jobs claimed per turn at most; the rest wait a turn.
JOBS_PER_TURN = 10
# The jobs that talk to iCloud, run one at a time on the remote thread.
REMOTE_KINDS = ("sync", "albums", "delete")
# How often the trash is looked at for day folders past their thirty days.
PURGE_EVERY = 24 * 3600.0
_PHASES = {"sync": "syncing", "albums": "albums", "delete": "deleting"}


class Agent:
    def __init__(self, settings):
        self.settings = settings
        self.pipeline = Pipeline(settings)
        self.heartbeat = Heartbeat(settings, self.phase, self.job_ids)
        # The one thread that talks to iCloud, what it is running and for
        # which job (SyncRun, AlbumsRun or DeleteRun: each has job_id, stop()).
        self.remote_thread: threading.Thread | None = None
        self.remote_kind = ""
        self.remote_run = None
        self._sync_ended = False
        self._last_scan = float("-inf")
        self._last_timer = float("-inf")
        self._last_purge = float("-inf")
        self._scan_requested = True     # the first turn scans

    # --- what the heartbeat reports -------------------------------------------

    @property
    def remote_busy(self) -> bool:
        return self.remote_thread is not None and self.remote_thread.is_alive()

    @property
    def syncing(self) -> bool:
        return self.remote_busy and self.remote_kind == "sync"

    def phase(self) -> str:
        phase = self.pipeline.phase
        if phase == "idle" and self.remote_busy:
            return _PHASES.get(self.remote_kind, phase)
        return phase

    def job_ids(self) -> list[int]:
        ids = self.pipeline.previews.job_ids()
        if self.remote_busy and self.remote_run is not None:
            ids.append(self.remote_run.job_id)
        return ids

    # --- jobs -------------------------------------------------------------------

    def remote_free(self) -> bool:
        """Whether a sync, a refresh or a delete may start now: none running
        here, and none in another process (a ``--sync`` in a terminal)."""
        if self.remote_busy:
            return False
        return not jobs.running_elsewhere(REMOTE_KINDS)

    def handle_jobs(self, db) -> int:
        kinds = ["scan", "preview", "reindex"]
        if self.remote_free():
            kinds += REMOTE_KINDS
        handled = 0
        for _ in range(JOBS_PER_TURN):
            job = jobs.claim(kinds)
            if job is None:
                break
            handled += 1
            if job.kind in REMOTE_KINDS:
                kinds = [k for k in kinds if k not in REMOTE_KINDS]
            if job.status != "running":        # cancelled while queued
                continue
            try:
                self.dispatch(db, job)
            except Exception as exc:  # noqa: BLE001 - one bad job must not stop the loop
                db.rollback()
                jobs.finish(job.id, "failed", message="failed", error=f"{type(exc).__name__}: {exc}")
                log(f"job {job.id} ({job.kind}): {type(exc).__name__}: {exc}", error=True)
        return handled

    def dispatch(self, db, job) -> None:
        if job.kind in REMOTE_KINDS:
            self.start_remote(job)
        elif job.kind == "scan":
            result = self.do_scan(db)
            jobs.finish(job.id, "done", message=result.summary)
        elif job.kind == "preview":
            self.pipeline.previews.request(db, job.id, job.params)
        elif job.kind == "reindex":
            stage = str((job.params or {}).get("stage") or "all")
            message = jobs.reindex(stage)
            log(message)
            jobs.finish(job.id, "done", message=message)
        else:
            jobs.finish(job.id, "failed", message="unknown job", error=f"unknown job kind {job.kind!r}")

    def start_remote(self, job) -> None:
        """Start a sync, a refresh or a delete on the remote thread."""
        kind = job.kind
        if kind in ("sync", "albums") and not self.settings.sync_enabled:
            jobs.finish(job.id, "failed", message="sync is disabled",
                        error="Sync is disabled (sync.enabled = false in meerpic.toml).")
            return
        if kind == "sync":
            run = sync.SyncRun(job.id, self.settings)
        elif kind == "albums":
            run = albums.AlbumsRun(job.id, self.settings)
        else:
            run = delete.DeleteRun(job.id, self.settings, job.params)

        def main() -> None:
            try:
                status = run.run()
                if kind == "sync":
                    # Photos that arrived are worth a scan now, not after
                    # the album listing that follows.
                    self._sync_ended = True
                    if status == "done" and self.settings.sync_albums:
                        self.refresh_after_sync()
            except Exception as exc:  # noqa: BLE001 - the loop must hear about it, not die of it
                jobs.finish(job.id, "failed", message=f"{kind} failed", error=f"{type(exc).__name__}: {exc}")
                log(f"{kind}: {type(exc).__name__}: {exc}", error=True)
            finally:
                if kind == "sync":
                    self._sync_ended = True

        self.remote_run = run
        self.remote_kind = kind
        self.remote_thread = threading.Thread(target=main, name=kind, daemon=True)
        self.remote_thread.start()

    def refresh_after_sync(self) -> None:
        """The album refresh a sync ends with, on the sync's thread. A refresh
        somebody queued meanwhile is the one that runs, not a second one."""
        job = jobs.claim(["albums"])
        if job is None or job.status != "running":
            job = jobs.create("albums", {"reason": "sync"}, running=True)
        run = albums.AlbumsRun(job.id, self.settings)
        self.remote_run = run
        self.remote_kind = "albums"
        run.run()

    # --- scanning ---------------------------------------------------------------

    def do_scan(self, db) -> scan.ScanResult:
        before = self.pipeline.phase
        self.pipeline.phase = "scanning"
        started = time.monotonic()
        try:
            result = scan.scan(db, self.settings)
        finally:
            self.pipeline.phase = before
        self._last_scan = time.monotonic()
        self._scan_requested = False
        if result.new or result.changed or result.removed:
            log(f"scan: {result.summary} ({time.monotonic() - started:.1f} s)")
        return result

    def scan_due(self) -> bool:
        if self._sync_ended:
            self._sync_ended = False
            return True
        interval = SCAN_WHILE_SYNCING if self.syncing else float(self.settings.agent_scan_interval)
        return self._scan_requested or time.monotonic() - self._last_scan >= interval

    # --- the loop ---------------------------------------------------------------

    def turn(self) -> int:
        with SessionLocal() as db:
            work = self.handle_jobs(db)
            now = time.monotonic()
            if now - self._last_timer >= SYNC_TIMER_EVERY:
                self._last_timer = now
                if sync.due(self.settings):
                    job = jobs.create("sync", {"reason": "timer"})
                    log(f"sync: timer, queued job {job.id}")
            if self.scan_due():
                self.do_scan(db)
            if now - self._last_purge >= PURGE_EVERY:
                self._last_purge = now
                removed = delete.purge_trash(self.settings)
                if removed:
                    log(f"trash: emptied {removed} day folder(s) older than {delete.KEEP_DAYS} days")

            def between() -> None:
                # A preview job claimed mid-turn starts now, not at the end.
                if self.handle_jobs(db):
                    self.pipeline.previews.tick(db)

            work += self.pipeline.turn(db, between)
        return work

    def run_forever(self) -> int:
        roots = ", ".join(f"{name}={path}" for name, path in self.settings.roots.items())
        log(f"meerpic-agent {VERSION}: {roots}; {self.settings.workers} worker(s), "
            f"previews {self.settings.video_previews}")
        orphans = jobs.fail_orphans()
        if orphans:
            log(f"{orphans} job(s) left running by a previous agent marked failed")
        with SessionLocal() as db:
            if albums.startup_due(db, self.settings) and jobs.active("albums") is None:
                job = jobs.create("albums", {"reason": "startup"})
                log(f"albums: last refresh over a day ago, queued job {job.id}")
        self.heartbeat.start()
        failing = ""
        try:
            while True:
                try:
                    work = self.turn()
                    if failing:
                        log("loop: recovered")
                    failing = ""
                except Exception as exc:  # noqa: BLE001 - never crash the loop
                    # A database restart, a full disk: wait it out. Log a
                    # repeat of the same failure once, not every two seconds.
                    reason = f"{type(exc).__name__}: {exc}".splitlines()[0]
                    if reason != failing:
                        log(f"loop: {reason}", error=True)
                        traceback.print_exc(file=sys.stderr)
                    failing = reason
                    time.sleep(5.0)
                    continue
                if not work:
                    time.sleep(1.0)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        log("stopping")
        if self.remote_run is not None and self.remote_busy:
            self.remote_run.stop()
            if self.remote_thread is not None:
                self.remote_thread.join(timeout=15)
        self.pipeline.close()
        self.pipeline.phase = "stopped"
        self.heartbeat.stop()


def run_once(settings, limit: int | None) -> int:
    """``--once``: scan, then every stage until nothing is left."""
    with SessionLocal() as db:
        result = scan.scan(db, settings)
        log(f"scan: {result.summary}")
        only = None
        if limit:
            only = frozenset(db.scalars(
                select(Photo.id).order_by(Photo.sort_at.desc(), Photo.id.desc()).limit(limit)
            ))
            log(f"limited to the newest {len(only)} file(s)")
        pipe = Pipeline(settings, only_ids=only, once=True)
        heartbeat = Heartbeat(settings, lambda: pipe.phase, pipe.previews.job_ids)
        heartbeat.start()
        started = time.monotonic()
        try:
            pipe.drain(db)
        finally:
            pipe.close()
            heartbeat.stop()
        counts = pipe.counts
        log(
            f"done in {time.monotonic() - started:.1f} s: {counts['meta']} meta, "
            f"{counts['thumbs']} thumbnail(s), {counts['embeddings']} embedding(s), "
            f"{counts['story']} storyboard(s), {counts['nsfw']} nsfw score(s), "
            f"{counts['faces']} photo(s) searched for faces, {counts['ocr']} file(s) read for text, "
            f"{pipe.previews.done} preview(s); failed: {counts['meta_failed']} meta, "
            f"{counts['thumbs_failed']} thumbnail(s), {counts['story_failed']} storyboard(s), "
            f"{pipe.previews.failed_count} preview(s)"
        )
        left = pending(db, settings)
        log("still pending: " + ", ".join(f"{k} {v}" for k, v in left.items()))
        for stage, reason in pipe.broken.items():
            log(f"{stage} did not run: {reason}", error=True)
    return 1 if pipe.broken else 0


def run_sync(settings) -> int:
    """``--sync``: one sync in the foreground, then a scan."""
    if not settings.sync_enabled:
        log("sync is disabled (sync.enabled = false)", error=True)
        return 2
    current = jobs.active("sync")
    if current is not None and current.status == "running":
        log(f"a sync is already running (job {current.id}); cancel it in the UI first", error=True)
        return 1
    if jobs.running_elsewhere(("albums", "delete")):
        log("an album refresh or a delete is running; try again when it is done", error=True)
        return 1
    job = jobs.claim(["sync"]) if current is not None else None
    if job is None or job.status != "running":
        job = jobs.create("sync", {"reason": "cli"}, running=True)
    run = sync.SyncRun(job.id, settings, echo=lambda line: log(f"sync: {line}"))
    _foreground(run, "rclone")
    with SessionLocal() as db:
        result = scan.scan(db, settings)
        log(f"scan: {result.summary}")
    if run.status == "done" and settings.sync_albums:
        run_albums(settings)
    return 0 if run.status == "done" else 1


def run_albums(settings) -> int:
    """``--albums``: one refresh of favourites, hidden photos and albums."""
    if not settings.sync_enabled:
        log("sync is disabled (sync.enabled = false)", error=True)
        return 2
    if jobs.running_elsewhere(("sync", "albums", "delete")):
        log("a sync, refresh or delete is running; try again when it is done", error=True)
        return 1
    job = jobs.claim(["albums"])
    if job is None or job.status != "running":
        job = jobs.create("albums", {"reason": "cli"}, running=True)
    run = albums.AlbumsRun(job.id, settings, echo=lambda line: log(f"albums: {line}"))
    _foreground(run, "the listing")
    return 0 if run.status == "done" else 1


def _foreground(run, what: str) -> None:
    thread = threading.Thread(target=run.run, name="foreground", daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            thread.join(timeout=0.5)
    except KeyboardInterrupt:
        log(f"stopping {what}")
        run.stop()
        thread.join(timeout=15)


def run_relink() -> int:
    """``--relink``: the Live and edit pairing over the whole library.

    Both normally run for the files the meta stage just read, so a library
    indexed before a change to the pairing rules keeps the old pairs until
    each file happens to be read again. This applies the current rules to
    everything at once, without re-reading a single file. Only rows that
    define a group are needed: every content identifier, and every edit.
    """
    from . import edits, live

    with SessionLocal() as db:
        cids = set(db.scalars(select(Photo.content_id).where(Photo.content_id != "")))
        live_changed = live.relink(db, cids)
        edited = db.scalars(select(Photo).where(Photo.name.ilike("%-edited%"))).all()
        edit_changed = edits.relink(db, edited)
        db.commit()
    log(f"relink: {len(cids)} Live group(s), {live_changed} row(s) changed; "
        f"{len(edited)} edit(s), {edit_changed} row(s) changed")
    return 0


def _terminate(signum, frame):
    # docker stop sends SIGTERM; unwind like Ctrl-C so the finally blocks
    # stop ffmpeg and rclone instead of leaving them orphaned.
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meerpic-agent")
    parser.add_argument("--once", action="store_true", help="scan and index everything, then exit")
    parser.add_argument("--test", action="store_true", help="check every dependency and exit")
    parser.add_argument("--sync", action="store_true", help="run one sync in the foreground")
    parser.add_argument("--albums", action="store_true",
                        help="read favourites, hidden photos and albums from iCloud")
    parser.add_argument("--relink", action="store_true",
                        help="re-pair Live Photos and edits across the whole library, then exit")
    parser.add_argument("--reindex", metavar="STAGE", choices=(*jobs.STAGES, "all"),
                        help="make a stage pending again: " + ", ".join((*jobs.STAGES, "all")))
    parser.add_argument("--limit", type=int, metavar="N", help="with --once: only the newest N files")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)

    if args.version:
        print(VERSION)
        return 0
    if args.limit is not None and not args.once:
        parser.error("--limit only makes sense with --once")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    setup()
    settings = get_settings()
    signal.signal(signal.SIGTERM, _terminate)

    if args.test:
        return 1 if checks.run(settings) else 0

    init_db()
    if args.reindex:
        log(jobs.reindex(args.reindex))
        if not args.once:
            return 0
    if args.relink:
        return run_relink()
    if args.sync:
        return run_sync(settings)
    if args.albums:
        return run_albums(settings)
    if args.once:
        return run_once(settings, args.limit)
    return Agent(settings).run_forever()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
