"""The indexing pipeline: meta, thumbnails, embeddings, storyboards, NSFW,
previews.

Every loop turn runs one small batch of each stage, in that order, newest
first. Small is the point: a first index of 25,000 files takes an hour, and
a photo that arrives in the middle of it should be in the grid, with a
thumbnail and searchable, within a few turns rather than after the other
24,999. Batches are sized so a turn is a few seconds; previews do not block
a turn at all (see agent/previews.py).

    meta        exiftool -> dates, size, place, camera, Live pairing, edits
    thumbs      Pillow / one ffmpeg frame, in a process pool
    embeddings  the search model over the thumbnails
    story       a strip of video frames for hovering, in the process pool
    nsfw        the NSFW classifier over thumbnails and storyboards
    faces       faces found in the originals, one vector each, decoded in the pool
    ocr         the text in photos and in video frames, read in the pool
    previews    browser-playable copies of videos, in the background

A stage only takes files the previous one has finished with (thumbnails
need the metadata's duration and codec, embeddings and storyboards need the
thumbnail, a video's NSFW score its storyboard), and never stops for one
file: a failure is recorded on the row, see agent/stages.py.
"""

from __future__ import annotations

import multiprocessing
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from concurrent.futures.process import BrokenProcessPool

from sqlalchemy import and_, exists, func, select, update
from sqlalchemy.orm import Session

from core import embed
from core.config import Settings
from core.media import original_path, story_path, thumb_path
from core.models import Photo, PhotoEmbedding
from core.timeutil import home_zone, utcnow

from . import (
    edits,
    embedder,
    faces,
    geo,
    live,
    meta,
    nsfw,
    ocr,
    stages,
    story,
    thumbs,
    video,
)
from .log import log
from .previews import Previews

META_BATCH = 200
# Per-batch wall clock for the thumbnail pool. A decode that takes longer
# than this is hung, not slow.
THUMB_BATCH_TIMEOUT = 300.0
# A suspect file retried alone gets the same allowance a whole batch did.
SINGLE_TIMEOUT = THUMB_BATCH_TIMEOUT
# After the search model fails as a whole (a download that did not
# complete), wait this long before trying again. The NSFW model likewise.
EMBED_BACKOFF = 120.0


class Pipeline:
    def __init__(self, settings: Settings, *, only_ids: frozenset[int] | None = None,
                 once: bool = False):
        self.settings = settings
        self.zone = home_zone(settings.timezone)
        self.only_ids = only_ids
        self.once = once
        self.caps = video.capabilities(settings.ffmpeg)
        self.cooldown = stages.Cooldown(None if once else 300.0)
        self.previews = Previews(settings, only_ids, self.cooldown)
        self.counts: Counter = Counter()
        # stage -> why it stopped for this run (``--once``) or is backing off.
        self.broken: dict[str, str] = {}
        self._embed_retry_at = 0.0
        self._nsfw_retry_at = 0.0
        self._faces_retry_at = 0.0
        self._ocr_retry_at = 0.0
        self._pool: ProcessPoolExecutor | None = None
        self.phase = "idle"

    # --- the process pool ------------------------------------------------------

    def _get_pool(self) -> ProcessPoolExecutor:
        if self._pool is None:
            # forkserver, never fork: this process has threads (heartbeat,
            # sync), and forking a threaded process can copy a held lock
            # into the child. Workers are recycled now and then because
            # decoding thousands of HEICs fragments a heap for good.
            self._pool = ProcessPoolExecutor(
                max_workers=self.settings.workers,
                mp_context=multiprocessing.get_context("forkserver"),
                max_tasks_per_child=200,
            )
        return self._pool

    def _reset_pool(self) -> None:
        pool, self._pool = self._pool, None
        if pool is None:
            return
        kill = getattr(pool, "kill_workers", None)      # Python 3.14+
        if kill is not None:
            try:
                kill()
            except OSError:     # already gone
                pass
        pool.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        self.previews.shutdown()
        self._reset_pool()

    # --- meta --------------------------------------------------------------------

    def meta_batch(self, db: Session) -> int:
        todo = db.execute(stages.newest(
            stages.base_columns().where(Photo.meta_sig != Photo.sig),
            self.only_ids, META_BATCH, self.cooldown.ids(),
        )).all()
        if not todo:
            return 0
        self.phase = "meta"
        by_path: dict[str, object] = {}
        for row in todo:
            src = original_path(row.root, row.rel_path)
            if src is None:
                self._fail(db, row, "meta", f"library root {row.root!r} is not configured")
            else:
                by_path[str(src)] = row
        processes = max(1, min(self.settings.workers, 6))
        records = meta.read_many(list(by_path), self.settings.exiftool, processes)

        done: list[tuple[object, dict]] = []
        for path, row in by_path.items():
            rec = records.get(path)
            if rec is None:
                self._fail(db, row, "meta", "exiftool found no such file (removed since the scan?)")
                continue
            if isinstance(rec, Exception):
                self._fail(db, row, "meta", rec)
                continue
            if rec.get("Error"):
                self._fail(db, row, "meta", f"exiftool: {rec['Error']}")
                continue
            try:
                cols = meta.extract(rec, kind=row.kind, name=row.name, mtime_ns=row.mtime_ns,
                                    zone=self.zone)
            except Exception as exc:  # noqa: BLE001 - one odd record must not stop the batch
                self._fail(db, row, "meta", exc)
                continue
            done.append((row, cols))

        located = [(row, cols) for row, cols in done if cols["lat"] is not None]
        try:
            places = geo.lookup([(cols["lat"], cols["lon"]) for _, cols in located])
        except Exception as exc:  # noqa: BLE001 - the geocoder's data file is part of the install
            log(f"geo: lookup failed: {type(exc).__name__}: {exc}", error=True)
            places = [geo.EMPTY] * len(located)
        for (_, cols), place in zip(located, places):
            cols.update(place.columns())
        for _, cols in done:
            if cols["lat"] is None:
                cols.update(geo.EMPTY.columns())

        if done:
            now = utcnow()
            db.execute(update(Photo), [
                {"id": row.id, **cols, "meta_sig": row.sig, "updated_at": now}
                for row, cols in done
            ])
            # Both the old and the new identifier: a file that changed its
            # identifier leaves a group behind that may now be unpaired.
            cids = {row.content_id for row, _ in done} | {cols["content_id"] for _, cols in done}
            live.relink(db, cids, [row.id for row, _ in done])
            # An edit and its original: the capture time and the identifier
            # that decide which original are only known from here on.
            edits.relink(db, [row for row, _ in done])
            stages.cleared(db, [row.id for row, _ in done], "meta")
        db.commit()
        self.counts["meta"] += len(done)
        failed = len(todo) - len(done)
        log(f"meta: {len(done)} file(s)" + (f", {failed} failed" if failed else ""))
        return len(todo)

    # --- thumbnails --------------------------------------------------------------

    def thumbs_batch(self, db: Session) -> int:
        limit = max(8, self.settings.workers * 4)
        todo = db.execute(stages.newest(
            stages.base_columns().where(Photo.meta_sig == Photo.sig, Photo.thumb_sig != Photo.sig),
            self.only_ids, limit, self.cooldown.ids(),
        )).all()
        if not todo:
            return 0
        self.phase = "thumbnails"
        tasks, rows = [], {}
        errors: Counter = Counter()
        for row in todo:
            src = original_path(row.root, row.rel_path)
            if src is None:
                self._fail(db, row, "thumbs", f"library root {row.root!r} is not configured")
                continue
            if row.kind == "video" and row.video_codec == "hevc" and not self.caps.hevc:
                # On such a host this is every iPhone video: one summary line
                # per batch, not a line per file.
                self._fail(db, row, "thumbs", video.HEVC_MISSING, quiet=True)
                errors[video.HEVC_MISSING] += 1
                continue
            rows[row.id] = row
            tasks.append(thumbs.ThumbTask(
                photo_id=row.id, src=str(src), dest=str(thumb_path(row.sig)), kind=row.kind,
                duration=row.duration, ffmpeg=self.settings.ffmpeg, ffprobe=self.settings.ffprobe,
                hevc=self.caps.hevc, tonemap=self.caps.tonemap,
            ))

        results = self._in_pool(thumbs.render, tasks, "thumbnail")
        ok = []
        for photo_id, result in results.items():
            row = rows[photo_id]
            if isinstance(result, BaseException):
                self._fail(db, row, "thumbs", result, quiet=True)
                errors[stages.short(result)] += 1
                continue
            w, h, duration = result
            values = {"width": w, "height": h, "thumb_sig": row.sig, "updated_at": utcnow()}
            # exiftool has no duration for some containers (MPEG-PS); the
            # frame grab had to ask ffprobe anyway.
            if row.kind == "video" and row.duration is None and duration:
                values["duration"] = duration
            db.execute(update(Photo).where(Photo.id == photo_id, Photo.sig == row.sig).values(values))
            ok.append(photo_id)
        stages.cleared(db, ok, "thumbs")
        db.commit()
        self.counts["thumbs"] += len(ok)
        failed = len(todo) - len(ok)
        line = f"thumbs: {len(ok)} file(s)"
        if failed:
            common = "; ".join(f"{n}x {msg}" for msg, n in errors.most_common(2))
            line += f", {failed} failed" + (f" ({common})" if common else "")
        log(line)
        return len(todo)

    def _in_pool(self, fn: Callable, tasks: list, what: str) -> dict[int, object]:
        """photo id -> what ``fn(task)`` returned, or the exception. Survives
        a worker that crashes (a decoder segfault) or hangs, by retrying the
        files it took down with it one at a time in a fresh pool."""
        out: dict[int, object] = {}
        if not tasks:
            return out
        suspects: list = []
        pool = self._get_pool()
        futures = {pool.submit(fn, task): task for task in tasks}
        try:
            for future in as_completed(futures, timeout=THUMB_BATCH_TIMEOUT):
                task = futures[future]
                try:
                    out[task.photo_id] = future.result()
                except BrokenProcessPool:
                    suspects.append(task)
                except Exception as exc:  # noqa: BLE001 - recorded on the row
                    out[task.photo_id] = exc
        except FuturesTimeout:
            suspects += [t for f, t in futures.items() if t.photo_id not in out and t not in suspects]
        if not suspects:
            return out
        self._reset_pool()
        for task in suspects:
            pool = self._get_pool()
            try:
                out[task.photo_id] = pool.submit(fn, task).result(timeout=SINGLE_TIMEOUT)
            except BrokenProcessPool:
                out[task.photo_id] = thumbs.ThumbError(f"the {what} worker crashed on this file")
                self._reset_pool()
            except FuturesTimeout:
                out[task.photo_id] = thumbs.ThumbError(f"drawing the {what} timed out")
                self._reset_pool()
            except Exception as exc:  # noqa: BLE001 - recorded on the row
                out[task.photo_id] = exc
        return out

    # --- embeddings --------------------------------------------------------------

    def embed_batch(self, db: Session) -> int:
        if "embeddings" in self.broken and (self.once or time.monotonic() < self._embed_retry_at):
            return 0
        self.phase = "embeddings"
        try:
            n = embedder.run_batch(db, self.only_ids, cooldown=self.cooldown)
        except Exception as exc:  # noqa: BLE001 - the model, not a photo: back off
            db.rollback()
            reason = f"{type(exc).__name__}: {exc}"
            if self.broken.get("embeddings") != reason:
                log(f"embeddings: the search model failed: {reason}", error=True)
            self.broken["embeddings"] = reason
            self._embed_retry_at = time.monotonic() + EMBED_BACKOFF
            return 0
        self.broken.pop("embeddings", None)
        if n:
            self.counts["embeddings"] += n
            log(f"embeddings: {n} photo(s)")
        return n

    # --- storyboards -------------------------------------------------------------

    def story_batch(self, db: Session) -> int:
        limit = max(4, self.settings.workers * 2)
        todo = db.execute(stages.newest(
            stages.base_columns().where(
                Photo.kind == "video", Photo.is_companion.is_(False),
                Photo.thumb_sig == Photo.sig, Photo.story_sig != Photo.sig,
            ),
            self.only_ids, limit, self.cooldown.ids(),
        )).all()
        if not todo:
            return 0
        self.phase = "storyboards"
        tasks, rows = [], {}
        errors: Counter = Counter()
        for row in todo:
            src = original_path(row.root, row.rel_path)
            if src is None:
                self._fail(db, row, "story", f"library root {row.root!r} is not configured")
                continue
            if row.video_codec == "hevc" and not self.caps.hevc:
                self._fail(db, row, "story", video.HEVC_MISSING, quiet=True)
                errors[video.HEVC_MISSING] += 1
                continue
            rows[row.id] = row
            tasks.append(story.StoryTask(
                photo_id=row.id, src=str(src), dest=str(story_path(row.sig)), duration=row.duration,
                ffmpeg=self.settings.ffmpeg, ffprobe=self.settings.ffprobe,
                hevc=self.caps.hevc, tonemap=self.caps.tonemap,
            ))

        ok = []
        for photo_id, result in self._in_pool(story.render, tasks, "storyboard").items():
            row = rows[photo_id]
            if isinstance(result, BaseException):
                self._fail(db, row, "story", result, quiet=True)
                errors[stages.short(result)] += 1
                continue
            db.execute(update(Photo).where(Photo.id == photo_id, Photo.sig == row.sig)
                       .values(story_sig=row.sig, story_frames=int(result), updated_at=utcnow()))
            ok.append(photo_id)
        stages.cleared(db, ok, "story")
        db.commit()
        self.counts["story"] += len(ok)
        failed = len(todo) - len(ok)
        line = f"story: {len(ok)} video(s)"
        if failed:
            common = "; ".join(f"{n}x {msg}" for msg, n in errors.most_common(2))
            line += f", {failed} failed" + (f" ({common})" if common else "")
        log(line)
        return len(todo)

    # --- nsfw --------------------------------------------------------------------

    def nsfw_batch(self, db: Session) -> int:
        if not self.settings.nsfw_enabled:
            return 0
        if "nsfw" in self.broken and (self.once or time.monotonic() < self._nsfw_retry_at):
            return 0
        self.phase = "nsfw"
        try:
            n = nsfw.run_batch(db, self.settings, self.only_ids, cooldown=self.cooldown)
        except Exception as exc:  # noqa: BLE001 - the model, not a photo: back off
            db.rollback()
            reason = f"{type(exc).__name__}: {exc}"
            if self.broken.get("nsfw") != reason:
                log(f"nsfw: the classifier failed: {reason}", error=True)
            self.broken["nsfw"] = reason
            self._nsfw_retry_at = time.monotonic() + EMBED_BACKOFF
            return 0
        self.broken.pop("nsfw", None)
        if n:
            self.counts["nsfw"] += n
            log(f"nsfw: {n} file(s)")
        return n

    # --- faces -------------------------------------------------------------------

    def faces_batch(self, db: Session) -> int:
        if not self.settings.faces_enabled:
            return 0
        if "faces" in self.broken and (self.once or time.monotonic() < self._faces_retry_at):
            return 0
        self.phase = "faces"
        try:
            n = faces.run_batch(db, self.settings, self._in_pool, self.only_ids, cooldown=self.cooldown)
        except Exception as exc:  # noqa: BLE001 - the models, not a photo: back off
            db.rollback()
            reason = f"{type(exc).__name__}: {exc}"
            if self.broken.get("faces") != reason:
                log(f"faces: the face models failed: {reason}", error=True)
            self.broken["faces"] = reason
            self._faces_retry_at = time.monotonic() + EMBED_BACKOFF
            return 0
        self.broken.pop("faces", None)
        if n:
            self.counts["faces"] += n
            log(f"faces: {n} photo(s)")
        return n

    # --- text --------------------------------------------------------------------

    def ocr_batch(self, db: Session) -> int:
        if not self.settings.ocr_enabled:
            return 0
        if "ocr" in self.broken and (self.once or time.monotonic() < self._ocr_retry_at):
            return 0
        self.phase = "text"
        try:
            n = ocr.run_batch(db, self.settings, self._in_pool, self.caps, self.only_ids,
                              limit=max(8, self.settings.workers * 2), cooldown=self.cooldown)
        except Exception as exc:  # noqa: BLE001 - the models, not a photo: back off
            db.rollback()
            reason = f"{type(exc).__name__}: {exc}"
            if self.broken.get("ocr") != reason:
                log(f"ocr: the text models failed: {reason}", error=True)
            self.broken["ocr"] = reason
            self._ocr_retry_at = time.monotonic() + EMBED_BACKOFF
            return 0
        self.broken.pop("ocr", None)
        if n:
            self.counts["ocr"] += n
            log(f"ocr: {n} file(s)")
        return n

    # --- the whole turn ----------------------------------------------------------

    def turn(self, db: Session, between: Callable[[], object] | None = None) -> int:
        """One batch of each stage. Returns how much work was done, 0 = idle.

        ``between`` runs after each stage: the loop claims jobs there, so a
        video somebody just opened waits for one stage, not a whole turn.
        """
        work = 0
        for stage in (self.meta_batch, self.thumbs_batch, self.embed_batch, self.story_batch,
                      self.nsfw_batch, self.faces_batch, self.ocr_batch):
            work += stage(db)
            if between is not None:
                between()
        self.phase = "previews" if self.previews.running else "idle"
        # A background conversion of a 4K HEVC clip is one ffmpeg on eight
        # cores for minutes, and on the first index of a real library it
        # halved the thumbnail rate. The grid and search are what make the
        # library usable, so new background conversions wait for a turn in
        # which the other stages found nothing to do. One already running
        # finishes, and a video somebody opened is never made to wait.
        work += self.previews.tick(db, background=not work)
        if not work and not self.previews.running:
            self.phase = "idle"
        return work

    def drain(self, db: Session) -> None:
        """Every stage until nothing is left (``--once``)."""
        while True:
            work = self.turn(db)
            if work:
                continue
            if self.previews.busy or self.previews.has_background_work(db):
                time.sleep(0.2)
                continue
            return

    def _fail(self, db: Session, row, stage: str, error, *, quiet: bool = False) -> None:
        stages.failed(db, row.id, row.sig, stage, error)
        self.cooldown.add(row.id)
        self.counts[f"{stage}_failed"] += 1
        if not quiet:
            log(f"{stage}: {row.name}: {stages.short(error)}", error=True)


def pending(db: Session, settings: Settings) -> dict:
    """How much is left per stage, for the heartbeat and the UI."""
    live_rows = Photo.fail_count < stages.MAX_FAILS
    has_vec = exists().where(and_(
        PhotoEmbedding.photo_id == Photo.id,
        PhotoEmbedding.model == embed.model_name(),
        PhotoEmbedding.sig == Photo.sig,
    ))
    visible = Photo.is_companion.is_(False)
    row = db.execute(select(
        func.count().filter(live_rows, Photo.meta_sig != Photo.sig),
        func.count().filter(live_rows, Photo.thumb_sig != Photo.sig),
        func.count().filter(live_rows, visible, ~has_vec),
        func.count().filter(live_rows, Photo.kind == "video", Photo.preview_sig != Photo.sig),
        func.count().filter(Photo.fail_count >= stages.MAX_FAILS),
        func.count().filter(live_rows, visible, Photo.kind == "video", Photo.story_sig != Photo.sig),
        func.count().filter(live_rows, visible, Photo.nsfw_sig != Photo.sig),
        func.count().filter(live_rows, visible, Photo.kind == "photo", Photo.superseded_by.is_(None),
                            Photo.faces_sig != Photo.sig),
        func.count().filter(live_rows, visible, Photo.superseded_by.is_(None), Photo.ocr_sig != Photo.sig,
                            *(() if settings.ocr_videos else (Photo.kind == "photo",))),
    )).one()
    return {
        "meta": row[0],
        "thumbs": row[1],
        "embeddings": row[2],
        "previews": row[3] if settings.video_previews == "all" else 0,
        "story": row[5],
        "nsfw": row[6] if settings.nsfw_enabled else 0,
        "faces": row[7] if settings.faces_enabled else 0,
        "ocr": row[8] if settings.ocr_enabled else 0,
        "failed": row[4],
    }
