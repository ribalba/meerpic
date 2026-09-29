"""The text stage: decoding upright at the right size, the frame size a
video is read at, the stand-in the tests run on (``MEERPIC_OCR_FAKE=1``: a
red picture says RED), and the stage writing text, sigs and failures: the
order it reads in, what it skips, a video read on its frames with each line
kept once, and a model download that fails blaming no photo.

The reader itself (agent/ocr_engine.py) has tests of its own."""

from __future__ import annotations

# Naive datetimes on purpose: meerpic stores naive UTC and naive wall clocks.
# ruff: noqa: DTZ001
import subprocess
from datetime import datetime

import pytest
from agent_helpers import add_photo, jpeg, needs_db, needs_ffmpeg, use_db, use_library
from PIL import Image
from sqlalchemy import select

from agent import jobs, ocr, video
from agent.pipeline import Pipeline, pending
from core.config import get_settings
from core.models import Photo, PhotoText

RED, GREEN, WHITE = (230, 20, 20), (20, 200, 40), (250, 250, 250)


def test_the_stand_in_reads_a_colour_name():
    assert ocr.FAKE and ocr.model_name() == "fake"
    fake = ocr.engine(("", ""))
    [line] = fake.read(Image.new("RGB", (40, 30), RED))
    assert (line.text, line.box) == ("RED", (0.0, 0.0, 1.0, 1.0))
    assert [x.text for x in fake.read(Image.new("RGB", (40, 30), GREEN))] == ["GREEN"]
    assert fake.read(Image.new("RGB", (40, 30), WHITE)) == []


def test_load_is_upright_and_at_most_the_source_edge(tmp_path):
    # Stored 300 x 100 and turned a quarter by its EXIF: shown 100 x 300.
    turned = jpeg(tmp_path / "turned.jpg", size=(300, 100), orientation=6)
    assert ocr.load(str(turned)).size == (100, 300)
    assert ocr.load(str(turned), edge=150).size == (50, 150)
    Image.new("RGBA", (3000, 1000), (0, 0, 0, 0)).save(tmp_path / "wide.png")
    wide = ocr.load(str(tmp_path / "wide.png"))
    # Transparency is flattened onto white, where a screenshot's text is.
    assert wide.size == (2048, 683) and wide.mode == "RGB" and wide.getpixel((5, 5)) == (255, 255, 255)


def test_a_video_frame_is_read_at_the_video_edge():
    assert ocr.frame_height(1920, 1080) == 720             # landscape: 1280 x 720
    assert ocr.frame_height(1080, 1920) == 1280            # portrait: 720 x 1280
    assert ocr.frame_height(1000, 1000) == 1280
    assert ocr.frame_height(333, 100) % 2 == 0             # zscale wants even sides
    assert ocr.frame_height(None, None) == 720


# --- the stage -----------------------------------------------------------------------


@pytest.fixture
def lib(tmp_path, monkeypatch):
    yield from use_library(tmp_path, monkeypatch)


@pytest.fixture
def db():
    yield from use_db()


def inline(fn, tasks, what):
    """The pipeline's pool, without the processes."""
    out = {}
    for task in tasks:
        try:
            out[task.photo_id] = fn(task)
        except Exception as exc:  # noqa: BLE001 - as the pool reports it
            out[task.photo_id] = exc
    return out


def caps():
    return video.capabilities(get_settings().ffmpeg)


def run(db, **kw):
    return ocr.run_batch(db, get_settings(), inline, caps(), **kw)


def ready(db, lib, name, color=RED, **cols):
    """A picture on disk whose thumbnail is drawn, as the thumbs stage leaves it."""
    Image.new("RGB", (60, 40), color).save(lib / name)
    row = add_photo(db, name, **cols)
    row.meta_sig = row.thumb_sig = row.sig
    db.commit()
    return row


def text_of(db, photo_id):
    db.expire_all()
    return db.get(PhotoText, photo_id)


@needs_db
def test_the_stage_stores_what_a_photo_says_and_its_sig(db, lib):
    red = ready(db, lib, "red.png", sort_at=datetime(2025, 1, 2))
    blank = ready(db, lib, "blank.png", WHITE, sort_at=datetime(2025, 1, 1))
    assert run(db) == 2
    said = text_of(db, red.id)
    assert (said.text, said.sig, said.model) == ("RED", red.sig, "fake")
    assert said.lines == [{"t": "RED", "s": 0.99, "b": [0.0, 0.0, 1.0, 1.0]}]
    # Read, and nothing found: no row, but done all the same.
    assert text_of(db, blank.id) is None
    assert {p.ocr_sig == p.sig for p in db.scalars(select(Photo))} == {True}
    assert run(db) == 0
    assert pending(db, get_settings())["ocr"] == 0


@needs_db
def test_a_changed_file_is_read_again_and_its_text_replaced(db, lib):
    row = ready(db, lib, "a.png")
    run(db)
    Image.new("RGB", (60, 40), GREEN).save(lib / "a.png")
    photo = db.get(Photo, row.id)
    photo.sig = photo.thumb_sig = photo.meta_sig = "0123456789abcdef"
    db.commit()
    assert pending(db, get_settings())["ocr"] == 1
    assert run(db) == 1
    said = text_of(db, row.id)
    assert (said.text, said.sig) == ("GREEN", "0123456789abcdef")
    # And a file that no longer says anything loses its row.
    Image.new("RGB", (60, 40), WHITE).save(lib / "a.png")
    photo = db.get(Photo, row.id)
    photo.sig = photo.thumb_sig = photo.meta_sig = "fedcba9876543210"
    db.commit()
    assert run(db) == 1 and text_of(db, row.id) is None


@needs_db
def test_screenshots_and_saved_pictures_are_read_first(db, lib):
    camera = ready(db, lib, "IMG_1.png", make="Apple", sort_at=datetime(2025, 1, 3))
    saved = ready(db, lib, "saved.png", sort_at=datetime(2025, 1, 1))
    shot = ready(db, lib, "shot.png", make="Apple", is_screenshot=True, sort_at=datetime(2025, 1, 2))
    order = []
    for _ in range(3):
        run(db, limit=1)
        db.expire_all()
        order += [p.name for p in db.scalars(select(Photo).where(Photo.ocr_sig != "")) if p.name not in order]
    assert order == [shot.name, saved.name, camera.name]


@needs_db
def test_companions_and_originals_behind_edits_are_skipped(db, lib, monkeypatch):
    edit = ready(db, lib, "IMG_1-edited.png")
    ready(db, lib, "IMG_1.png", superseded_by=edit.id)
    motion = add_photo(db, "IMG_1.mov", is_companion=True)
    motion.meta_sig = motion.thumb_sig = motion.sig
    db.commit()
    assert run(db) == 1
    assert pending(db, get_settings())["ocr"] == 0
    assert {p.name for p in db.scalars(select(Photo)) if p.ocr_sig} == {"IMG_1-edited.png"}


@needs_db
def test_videos_can_be_left_out(db, lib, monkeypatch):
    clip = add_photo(db, "clip.mov")
    clip.meta_sig = clip.thumb_sig = clip.sig
    db.commit()
    monkeypatch.setattr(get_settings(), "ocr_videos", False)
    assert pending(db, get_settings())["ocr"] == 0
    assert run(db) == 0


@needs_db
def test_a_photo_waits_for_its_thumbnail(db, lib):
    row = ready(db, lib, "a.png")
    row.thumb_sig = ""
    db.commit()
    assert run(db) == 0


@needs_db
def test_a_file_that_will_not_decode_counts_against_the_photo(db, lib):
    row = ready(db, lib, "a.png")
    (lib / "a.png").write_bytes(b"not a png")
    assert run(db) == 0
    db.expire_all()
    row = db.get(Photo, row.id)
    assert (row.error_stage, row.fail_count, row.ocr_sig) == ("ocr", 1, "")


@needs_db
@needs_ffmpeg
def test_a_video_is_read_on_its_frames_and_each_line_kept_once(db, lib):
    path = lib / "red.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "color=c=0xE61414:size=160x120:rate=10:duration=2",
         "-c:v", "mpeg4", "-q:v", "2", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True, timeout=60,
    )
    row = add_photo(db, "red.mp4", duration=2.0, width=160, height=120, video_codec="mpeg4")
    row.meta_sig = row.thumb_sig = row.sig
    db.commit()
    assert run(db) == 1
    said = text_of(db, row.id)
    # Ten frames, all red: one line, and no box, since it is on every frame.
    assert said.text == "RED" and said.lines == [{"t": "RED", "s": 0.99}]


@needs_db
def test_the_pipeline_runs_it_in_its_process_pool(db, lib):
    row = ready(db, lib, "a.png")
    pipe = Pipeline(get_settings(), once=True)
    try:
        assert pipe.ocr_batch(db) == 1
    finally:
        pipe.close()
    assert text_of(db, row.id).text == "RED" and pipe.counts["ocr"] == 1


@needs_db
def test_models_that_cannot_be_had_blame_no_photo_and_back_off(db, lib, monkeypatch):
    row = ready(db, lib, "a.png")
    calls = []

    def broken(settings):
        calls.append(1)
        raise RuntimeError("model download failed")

    monkeypatch.setattr(ocr, "models", broken)
    pipe = Pipeline(get_settings())                          # the service, not --once
    try:
        assert pipe.ocr_batch(db) == 0
        assert pipe.ocr_batch(db) == 0                       # backing off: not asked again
    finally:
        pipe.close()
    assert len(calls) == 1 and "model download failed" in pipe.broken["ocr"]
    db.expire_all()
    row = db.get(Photo, row.id)
    assert row.fail_count == 0 and row.ocr_sig == ""


@needs_db
def test_disabled_does_nothing(db, lib, monkeypatch):
    ready(db, lib, "a.png")
    monkeypatch.setattr(get_settings(), "ocr_enabled", False)
    pipe = Pipeline(get_settings(), once=True)
    try:
        assert pipe.ocr_batch(db) == 0
    finally:
        pipe.close()
    assert pending(db, get_settings())["ocr"] == 0


@needs_db
def test_reindex_reads_everything_again(db, lib):
    ready(db, lib, "a.png")
    run(db)
    assert pending(db, get_settings())["ocr"] == 0
    assert "ocr: 1 file(s)" in jobs.reindex("ocr")
    db.expire_all()
    assert pending(db, get_settings())["ocr"] == 1
