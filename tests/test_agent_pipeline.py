"""The pipeline end to end: files in a temporary library through meta,
thumbnails, embeddings and previews, into rows and cache files.

Real exiftool, real Pillow in the real process pool, real ffmpeg where this
machine has what a test needs, and the stand-in search model.
"""

from __future__ import annotations

# Naive datetimes on purpose: meerpic stores naive UTC and naive wall clocks.
# ruff: noqa: DTZ001
import time
from datetime import datetime

import pytest
from agent_helpers import (
    add_photo,
    jpeg,
    make_video,
    needs_db,
    needs_encoder,
    needs_exiftool,
    needs_h264_encoder,
    png,
    set_mtime,
    use_db,
    use_library,
)
from sqlalchemy import select

from agent import jobs, meta, pipeline, scan, video
from agent.main import run_once
from agent.pipeline import Pipeline, pending
from core import embed
from core.config import get_settings
from core.media import preview_path, thumb_path
from core.models import Job, Photo, PhotoEmbedding

pytestmark = [needs_db, needs_exiftool]


@pytest.fixture
def lib(tmp_path, monkeypatch):
    yield from use_library(tmp_path, monkeypatch)


@pytest.fixture
def db():
    yield from use_db()


@pytest.fixture
def pipe(lib):
    p = Pipeline(get_settings(), once=True)
    yield p
    p.close()


def by_name(db) -> dict[str, Photo]:
    db.expire_all()
    return {p.name: p for p in db.scalars(select(Photo))}


def embedding(db, photo_id):
    return db.scalar(select(PhotoEmbedding).where(PhotoEmbedding.photo_id == photo_id))


def test_photos_end_to_end(db, lib, pipe):
    jpeg(lib / "IMG_1.jpg", size=(800, 600), date="2024:07:01 12:00:00", offset="+02:00",
         gps=(52.39, 13.06), orientation=6, make="Apple", model="iPhone 13")
    png(lib / "Screenshot 2024-01-01 at 12.34.56.png", size=(300, 600))
    bare = jpeg(lib / "c3e65ce8-457b-4a37-934d-162bc851fece.jpg", size=(200, 100))
    set_mtime(bare, datetime(2025, 5, 5, 5, 5, 5))
    scan.scan(db, get_settings())
    pipe.drain(db)

    rows = by_name(db)
    a = rows["IMG_1.jpg"]
    assert (a.date_source, a.taken_at, a.tz_offset) == ("exif", datetime(2024, 7, 1, 10, 0), 120)
    assert (a.width, a.height) == (600, 800)
    assert (a.city, a.region, a.country, a.country_code) == ("Potsdam", "Brandenburg", "Germany", "DE")
    assert a.place == "Potsdam, Brandenburg, Germany"
    assert (a.make, a.model) == ("Apple", "iPhone 13")
    assert a.meta_sig == a.thumb_sig == a.sig
    assert (a.error, a.fail_count) == ("", 0)
    assert thumb_path(a.sig).is_file()
    e = embedding(db, a.id)
    assert (e.model, e.sig) == (embed.model_name(), a.sig)

    s = rows["Screenshot 2024-01-01 at 12.34.56.png"]
    assert s.is_screenshot and s.date_source == "filename"
    assert s.taken_local == datetime(2024, 1, 1, 12, 34, 56)
    assert (s.width, s.height) == (300, 600)

    b = rows["c3e65ce8-457b-4a37-934d-162bc851fece.jpg"]
    assert b.date_source == "mtime" and b.taken_at == datetime(2025, 5, 5, 5, 5, 5)
    assert b.lat is None and b.place == ""

    assert pending(db, get_settings()) == {"meta": 0, "thumbs": 0, "embeddings": 0, "previews": 0,
                                           "story": 0, "nsfw": 0, "faces": 0, "ocr": 0, "failed": 0}
    # Newest first in the grid's order: sort_at follows the real dates now.
    order = [p.name for p in db.scalars(select(Photo).order_by(Photo.sort_at.desc(), Photo.id.desc()))]
    assert order == ["c3e65ce8-457b-4a37-934d-162bc851fece.jpg", "IMG_1.jpg",
                     "Screenshot 2024-01-01 at 12.34.56.png"]


def test_a_broken_file_is_recorded_and_the_rest_go_on(db, lib, pipe):
    jpeg(lib / "good.jpg")
    (lib / "broken.jpg").write_bytes(bytes(range(256)) * 20)          # exiftool: format error
    (lib / "notes.jpg").write_bytes(b"just some text, not a picture")  # exiftool: TXT; Pillow: no
    scan.scan(db, get_settings())
    pipe.drain(db)
    rows = by_name(db)
    assert rows["good.jpg"].thumb_sig == rows["good.jpg"].sig
    broken = rows["broken.jpg"]
    assert (broken.error_stage, broken.fail_count) == ("meta", 1)
    assert broken.error == "exiftool: File format error"
    notes = rows["notes.jpg"]
    assert notes.meta_sig == notes.sig
    assert (notes.error_stage, notes.fail_count) == ("thumbs", 1)
    assert "UnidentifiedImageError" in notes.error


def test_three_failures_park_a_file_until_it_changes(db, lib):
    bad = lib / "broken.jpg"
    bad.write_bytes(bytes(range(256)) * 20)
    scan.scan(db, get_settings())
    for _ in range(4):
        p = Pipeline(get_settings(), once=True)
        p.drain(db)
        p.close()
    row = by_name(db)["broken.jpg"]
    assert row.fail_count == 3
    assert pending(db, get_settings())["failed"] == 1
    assert pending(db, get_settings())["meta"] == 0
    jpeg(bad)                                   # replaced by a real photo
    set_mtime(bad, datetime(2025, 1, 1))
    scan.scan(db, get_settings())
    p = Pipeline(get_settings(), once=True)
    p.drain(db)
    p.close()
    row = by_name(db)["broken.jpg"]
    assert (row.fail_count, row.error) == (0, "")
    assert row.thumb_sig == row.sig


def test_a_success_clears_only_its_own_stage(db, lib, pipe):
    path = jpeg(lib / "a.jpg")
    scan.scan(db, get_settings())
    row = by_name(db)["a.jpg"]
    row.error_stage, row.error, row.fail_count = "previews", "old", 1
    db.commit()
    pipe.drain(db)
    row = by_name(db)["a.jpg"]
    assert (row.error_stage, row.fail_count) == ("previews", 1)
    assert path.exists()


def test_newest_first(db, lib, monkeypatch):
    for i, day in enumerate((3, 1, 5, 2, 4)):
        set_mtime(jpeg(lib / f"f{i}.jpg"), datetime(2025, 1, day))
    scan.scan(db, get_settings())
    monkeypatch.setattr(pipeline, "META_BATCH", 2)
    p = Pipeline(get_settings(), once=True)
    try:
        p.meta_batch(db)
    finally:
        p.close()
    done = {name for name, row in by_name(db).items() if row.meta_sig == row.sig}
    assert done == {"f2.jpg", "f4.jpg"}                  # the 5th and the 4th


def test_limit_only_touches_the_newest(db, lib):
    for i, day in enumerate((3, 1, 5, 2, 4)):
        set_mtime(jpeg(lib / f"f{i}.jpg"), datetime(2025, 1, day))
    assert run_once(get_settings(), limit=2) == 0
    rows = by_name(db)
    assert {n for n, r in rows.items() if r.thumb_sig == r.sig} == {"f2.jpg", "f4.jpg"}
    assert len(rows) == 5


def test_live_pairing_through_meta(db, lib, pipe, monkeypatch):
    jpeg(lib / "IMG_1.JPG")
    jpeg(lib / "IMG_2.JPG")
    (lib / "IMG_1.MOV").write_bytes(b"not really a video")
    real = meta.read_many

    def with_ids(paths, exiftool="exiftool", processes=4):
        out = real(paths, exiftool, processes)
        for path, rec in out.items():
            if "IMG_1." in path:
                rec["ContentIdentifier"] = "CID-1"
        return out

    monkeypatch.setattr(meta, "read_many", with_ids)
    scan.scan(db, get_settings())
    pipe.meta_batch(db)
    rows = by_name(db)
    still, motion = rows["IMG_1.JPG"], rows["IMG_1.MOV"]
    assert still.live_video_id == motion.id and motion.is_companion
    assert rows["IMG_2.JPG"].live_video_id is None
    pipe.drain(db)
    # A companion gets no vector: it is never listed.
    assert embedding(db, motion.id) is None
    assert embedding(db, still.id) is not None


def test_hevc_on_a_host_that_cannot_decode_it(db, lib, pipe):
    pipe.caps = video.Caps(found=True, decoders=frozenset({"h264"}), encoders=frozenset({"libx264"}))
    pipe.previews.caps = pipe.caps
    row = add_photo(db, "IMG_9.MOV", video_codec="hevc", duration=2.0)
    row.meta_sig = row.sig
    db.commit()
    (lib / "IMG_9.MOV").write_bytes(b"x")
    pipe.thumbs_batch(db)
    row = by_name(db)["IMG_9.MOV"]
    assert row.error == "this ffmpeg cannot decode HEVC; run the agent in its container"
    assert (row.error_stage, row.fail_count) == ("thumbs", 1)
    pipe.cooldown = type(pipe.cooldown)(None)
    pipe.previews.cooldown = pipe.cooldown
    pipe.previews.tick(db)
    row = by_name(db)["IMG_9.MOV"]
    assert (row.error_stage, row.fail_count) == ("previews", 2)
    assert pipe.previews.hevc_failures == 1


def test_a_lost_thumbnail_is_drawn_again_before_embedding(db, lib, pipe):
    jpeg(lib / "a.jpg")
    scan.scan(db, get_settings())
    pipe.drain(db)
    row = by_name(db)["a.jpg"]
    thumb_path(row.sig).unlink()
    db.delete(embedding(db, row.id))
    db.commit()
    p = Pipeline(get_settings(), once=True)
    try:
        p.embed_batch(db)
        assert by_name(db)["a.jpg"].thumb_sig == ""
        p.drain(db)
    finally:
        p.close()
    row = by_name(db)["a.jpg"]
    assert thumb_path(row.sig).is_file() and embedding(db, row.id) is not None
    assert row.fail_count == 0


def test_a_broken_model_blames_no_photo(db, lib, pipe, monkeypatch):
    jpeg(lib / "a.jpg")
    scan.scan(db, get_settings())

    def broken(*a, **kw):
        raise RuntimeError("model download failed")

    monkeypatch.setattr(embed, "embed_images", broken)
    pipe.drain(db)
    row = by_name(db)["a.jpg"]
    assert row.thumb_sig == row.sig and row.fail_count == 0
    assert embedding(db, row.id) is None
    assert "model download failed" in pipe.broken["embeddings"]


def test_reindex_thumbs_redraws(db, lib, pipe):
    jpeg(lib / "a.jpg")
    scan.scan(db, get_settings())
    pipe.drain(db)
    row = by_name(db)["a.jpg"]
    thumb_path(row.sig).unlink()
    jobs.reindex("thumbs")
    assert pending(db, get_settings())["thumbs"] == 1
    p = Pipeline(get_settings(), once=True)
    try:
        p.drain(db)
    finally:
        p.close()
    assert thumb_path(row.sig).is_file()


# --- videos ------------------------------------------------------------------------------


@needs_encoder("mpeg4")
@needs_h264_encoder()
def test_video_end_to_end(db, lib, pipe):
    make_video(lib / "clip.mov", duration=2, size=(640, 480), audio="pcm_s16le")
    scan.scan(db, get_settings())
    pipe.drain(db)
    row = by_name(db)["clip.mov"]
    assert row.kind == "video"
    assert row.duration == pytest.approx(2.0, abs=0.1)
    assert (row.width, row.height) == (640, 480)
    assert row.thumb_sig == row.sig and thumb_path(row.sig).is_file()
    assert embedding(db, row.id) is not None
    assert row.preview_sig == row.sig and preview_path(row.sig).is_file()
    assert not list(preview_path(row.sig).parent.glob(".*"))      # no temp or log files left
    assert pipe.previews.done == 1


@needs_encoder("mpeg4")
@needs_h264_encoder()
def test_on_demand_preview_for_a_live_still(db, lib, monkeypatch):
    monkeypatch.setattr(get_settings(), "video_previews", "on-demand")
    jpeg(lib / "IMG_1.JPG")
    make_video(lib / "IMG_1.MOV", duration=1)
    scan.scan(db, get_settings())
    p = Pipeline(get_settings(), once=True)
    try:
        p.drain(db)
        rows = by_name(db)
        still, motion = rows["IMG_1.JPG"], rows["IMG_1.MOV"]
        assert motion.preview_sig == ""                   # on-demand: nothing ahead of time
        still.live_video_id = motion.id
        motion.is_companion = True
        db.commit()
        job = jobs.create("preview", {"photo_id": still.id}, running=True)
        p.previews.request(db, job.id, job.params)
        deadline = time.monotonic() + 60
        while p.previews.busy and time.monotonic() < deadline:
            p.previews.tick(db)
            time.sleep(0.1)
    finally:
        p.close()
    db.expire_all()
    assert db.get(Job, job.id).status == "done"
    motion = by_name(db)["IMG_1.MOV"]
    assert motion.preview_sig == motion.sig and preview_path(motion.sig).is_file()


def test_preview_job_for_a_photo_without_motion(db, lib, pipe):
    row = add_photo(db, "a.jpg")
    job = jobs.create("preview", {"photo_id": row.id}, running=True)
    pipe.previews.request(db, job.id, job.params)
    db.expire_all()
    assert db.get(Job, job.id).status == "failed"
    job = jobs.create("preview", {}, running=True)
    pipe.previews.request(db, job.id, job.params)
    db.expire_all()
    assert "photo_id" in db.get(Job, job.id).error


def _slow_preview(monkeypatch, pipe, lib, db):
    """A 'video' whose ffmpeg is ``sleep``: to test what happens around it."""
    info = video.StreamInfo("h264", 1280, 720, "yuv420p", 8, "bt709", 0, 10.0, "aac")
    monkeypatch.setattr(video, "probe", lambda path, ffprobe="ffprobe": {})
    monkeypatch.setattr(video, "stream_info", lambda data: info)
    monkeypatch.setattr(video, "argv", lambda *a, **kw: ["sleep", "60"])
    (lib / "slow.mp4").write_bytes(b"x")
    row = add_photo(db, "slow.mp4")
    row.meta_sig = row.sig
    db.commit()
    return row


def test_a_cancelled_preview_job_stops_ffmpeg(db, lib, pipe, monkeypatch):
    monkeypatch.setattr(pipe.previews.settings, "video_previews", "on-demand")
    row = _slow_preview(monkeypatch, pipe, lib, db)
    job = jobs.create("preview", {"photo_id": row.id}, running=True)
    pipe.previews.request(db, job.id, job.params)
    pipe.previews.tick(db)
    (running,) = pipe.previews.running
    db.get(Job, job.id).cancel_requested = True
    db.commit()
    time.sleep(1.1)
    pipe.previews.tick(db)
    assert not pipe.previews.running
    assert running.proc.poll() is not None
    db.expire_all()
    assert db.get(Job, job.id).status == "cancelled"
    assert by_name(db)["slow.mp4"].fail_count == 0      # not the file's fault


def test_a_hung_preview_times_out(db, lib, pipe, monkeypatch):
    row = _slow_preview(monkeypatch, pipe, lib, db)
    monkeypatch.setattr(video, "timeout_for", lambda info, mode: 0.3)
    pipe.previews.tick(db)
    assert len(pipe.previews.running) == 1
    time.sleep(0.5)
    pipe.previews.tick(db)
    assert not pipe.previews.running
    row = by_name(db)["slow.mp4"]
    assert (row.error_stage, row.error, row.fail_count) == ("previews", "ffmpeg timed out (remux)", 1)


def test_background_previews_leave_a_slot_for_the_browser(db, lib, pipe, monkeypatch):
    slow = _slow_preview(monkeypatch, pipe, lib, db)
    (lib / "other.mp4").write_bytes(b"x")
    other = add_photo(db, "other.mp4")
    other.meta_sig = other.sig
    db.commit()
    pipe.previews.tick(db)
    (background,) = pipe.previews.running                # one background slot only
    waiting = slow if background.photo_id == other.id else other
    job = jobs.create("preview", {"photo_id": waiting.id}, running=True)
    pipe.previews.request(db, job.id, job.params)
    pipe.previews.tick(db)
    assert len(pipe.previews.running) == 2
    assert {r.background for r in pipe.previews.running} == {True, False}
    # Asking for the one already running joins it rather than starting another.
    again = jobs.create("preview", {"photo_id": background.photo_id}, running=True)
    pipe.previews.request(db, again.id, again.params)
    assert len(pipe.previews.running) == 2 and again.id in background.job_ids


@needs_encoder("mpeg4")
def test_a_missing_duration_is_filled_from_the_frame_grab(db, lib, pipe):
    make_video(lib / "old.mpg.mov", duration=2)
    scan.scan(db, get_settings())
    pipe.meta_batch(db)
    row = by_name(db)["old.mpg.mov"]
    row.duration = None                         # what exiftool says about MPEG-PS
    db.commit()
    pipe.thumbs_batch(db)
    assert by_name(db)["old.mpg.mov"].duration == pytest.approx(2.0, abs=0.1)


def test_the_loop_gets_a_word_in_between_stages(db, lib, pipe):
    jpeg(lib / "a.jpg")
    scan.scan(db, get_settings())
    seen = []
    pipe.turn(db, lambda: seen.append(pipe.phase))
    # No video, so no storyboard: the phase stays where the last stage left it.
    assert seen == ["meta", "thumbnails", "embeddings", "embeddings", "nsfw", "faces", "text"]


def test_background_previews_take_videos_before_live_photo_motion(db, lib, pipe):
    motion = add_photo(db, "IMG_1.MOV", is_companion=True, sort_at=datetime(2025, 1, 2))
    clip = add_photo(db, "clip.mov", sort_at=datetime(2020, 1, 1))
    newer = add_photo(db, "clip2.mov", sort_at=datetime(2021, 1, 1))
    for row in (motion, clip, newer):
        row.meta_sig = row.sig
    db.commit()
    assert pipe.previews._next_background(db).id == newer.id
    newer.preview_sig = newer.sig
    db.commit()
    assert pipe.previews._next_background(db).id == clip.id
    clip.preview_sig = clip.sig
    db.commit()
    assert pipe.previews._next_background(db).id == motion.id


def test_background_previews_wait_while_the_index_is_busy(db, lib, pipe, monkeypatch):
    """A 4K conversion must not take the CPU the thumbnails of a first index
    need; a requested one still starts."""
    slow = _slow_preview(monkeypatch, pipe, lib, db)
    pipe.previews.tick(db, background=False)
    assert not pipe.previews.running
    job = jobs.create("preview", {"photo_id": slow.id}, running=True)
    pipe.previews.request(db, job.id, job.params)
    pipe.previews.tick(db, background=False)
    (running,) = pipe.previews.running
    assert not running.background


def test_a_turn_with_indexing_work_starts_no_background_preview(db, lib, pipe, monkeypatch):
    _slow_preview(monkeypatch, pipe, lib, db)
    calls = []
    real = pipe.previews.tick
    monkeypatch.setattr(pipe, "meta_batch", lambda db: 5)
    monkeypatch.setattr(pipe, "thumbs_batch", lambda db: 0)
    monkeypatch.setattr(pipe, "embed_batch", lambda db: 0)
    monkeypatch.setattr(pipe, "story_batch", lambda db: 0)
    monkeypatch.setattr(pipe, "nsfw_batch", lambda db: 0)
    monkeypatch.setattr(pipe.previews, "tick", lambda db, background=True: calls.append(background) or real(db, background))
    pipe.turn(db)
    monkeypatch.setattr(pipe, "meta_batch", lambda db: 0)
    pipe.turn(db)
    assert calls == [False, True]
    assert len(pipe.previews.running) == 1
