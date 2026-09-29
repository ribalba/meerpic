"""The NSFW stage: preprocessing as the model's config asks, the stand-in the
tests run on (``MEERPIC_NSFW_FAKE=1``, red pixels), the order rows are
scored in, videos scored on their storyboards, and a model that will not
load backing off without blaming any photo.

The real classifier is exercised only when its files are already on this
machine (read, never downloaded or written by a test)."""

from __future__ import annotations

# Naive datetimes on purpose: meerpic stores naive UTC and naive wall clocks.
# ruff: noqa: DTZ001
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from agent_helpers import add_photo, needs_db, use_db, use_library
from PIL import Image
from sqlalchemy import select

from agent import nsfw
from agent.pipeline import Pipeline, pending
from core.config import get_settings
from core.media import story_path, thumb_path
from core.models import Photo

RED, BLUE = (230, 20, 20), (20, 40, 220)

PREPROCESSOR = {
    "do_normalize": True, "do_rescale": True, "do_resize": True,
    "image_mean": [0.5, 0.5, 0.5], "image_std": [0.5, 0.5, 0.5],
    "resample": 2, "rescale_factor": 0.00392156862745098, "size": {"height": 384, "width": 384},
}


def test_the_stand_in_counts_red_pixels():
    assert nsfw.FAKE
    assert nsfw.fake_score(Image.new("RGB", (40, 30), RED)) == 1.0
    assert nsfw.fake_score(Image.new("RGB", (40, 30), BLUE)) == 0.0
    half = Image.new("RGB", (40, 30), BLUE)
    half.paste(RED, (0, 0, 20, 30))
    assert nsfw.fake_score(half) == pytest.approx(0.5)
    assert nsfw.model_name(get_settings()) == "fake"


def test_preprocessing_follows_the_config():
    prep = nsfw.Prep.from_config(PREPROCESSOR)
    assert (prep.width, prep.height, prep.resample) == (384, 384, 2)
    t = prep.tensor([Image.new("RGB", (100, 50), (255, 255, 255)), Image.new("L", (10, 10), 0)])
    assert t.shape == (2, 3, 384, 384) and t.dtype == np.float32
    assert t[0].min() == pytest.approx(1.0) and t[1].max() == pytest.approx(-1.0)
    plain = nsfw.Prep.from_config({"do_normalize": False, "size": {"shortest_edge": 224}})
    assert (plain.width, plain.mean, plain.std) == (224, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0))


def test_the_label_comes_from_the_config():
    assert nsfw.nsfw_index({"id2label": {"0": "sfw", "1": "nsfw"}}) == 1
    assert nsfw.nsfw_index({"id2label": {"0": "NSFW", "1": "normal"}}) == 0
    with pytest.raises(ValueError):
        nsfw.nsfw_index({"id2label": {"0": "cat", "1": "dog"}})


def test_softmax():
    p = nsfw.softmax(np.array([[0.0, 0.0], [10.0, -10.0]], dtype=np.float32))
    assert p[0] == pytest.approx([0.5, 0.5]) and p[1, 0] == pytest.approx(1.0)


class FakeSession:
    """ONNX Runtime's interface, as far as the model uses it."""

    def __init__(self):
        self.batches = []

    def get_inputs(self):
        return [type("I", (), {"name": "pixel_values"})()]

    def run(self, outputs, feed):
        x = feed["pixel_values"]
        self.batches.append(x.shape[0])
        # Logit for "nsfw" = mean of the red channel minus the blue one.
        red = x[:, 0].mean(axis=(1, 2)) - x[:, 2].mean(axis=(1, 2))
        return [np.stack([np.zeros_like(red), red * 10], axis=1)]


def test_the_model_scores_in_batches_of_sixteen():
    session = FakeSession()
    m = nsfw.Model(session, nsfw.Prep(width=32, height=32), index=1)
    images = [Image.new("RGB", (8, 8), RED)] * 20 + [Image.new("RGB", (8, 8), BLUE)]
    scores = m.score(images)
    assert session.batches == [16, 5]
    assert scores[0] > 0.99 and scores[-1] < 0.01


_REAL = sorted(Path("~/.cache/meerpic/models").expanduser().glob(
    "models--AdamCodd--vit-base-nsfw-detector/snapshots/*/onnx/model_int8.onnx"))


@pytest.mark.skipif(not _REAL, reason="the real NSFW model is not on this machine")
def test_the_real_model_loads_and_answers(monkeypatch):
    onnx = _REAL[-1].parent
    files = {nsfw.MODEL_FILE: onnx / "model_int8.onnx", nsfw.CONFIG_FILE: onnx / "config.json",
             nsfw.PREPROCESSOR_FILE: onnx / "preprocessor_config.json"}
    monkeypatch.setattr(nsfw, "download", lambda settings: files)
    m = nsfw.build(get_settings(), threads=2)
    assert m.index == 1 and (m.prep.width, m.prep.height) == (384, 384)
    scores = m.score([Image.new("RGB", (64, 64), (120, 160, 200)), Image.new("RGB", (400, 300), BLUE)])
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert json.loads(files[nsfw.CONFIG_FILE].read_text())["id2label"]["1"] == "nsfw"


def test_frames_of_a_strip():
    strip = Image.new("RGB", (30, 10), BLUE)
    strip.paste(RED, (20, 0, 30, 10))
    frames = nsfw.frames_of(strip, 3)
    assert [f.size for f in frames] == [(10, 10)] * 3
    assert [nsfw.fake_score(f) for f in frames] == [0.0, 0.0, 1.0]
    assert nsfw.frames_of(strip, 0) == [strip]


# --- the stage -----------------------------------------------------------------------------


@pytest.fixture
def lib(tmp_path, monkeypatch):
    yield from use_library(tmp_path, monkeypatch)


@pytest.fixture
def db():
    yield from use_db()


def ready(db, name, color, **cols):
    """A row whose thumbnail is drawn, as the thumbs stage leaves it."""
    row = add_photo(db, name, **cols)
    row.meta_sig = row.thumb_sig = row.sig
    db.commit()
    path = thumb_path(row.sig)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), color).save(path, "WEBP")
    return row


def scored(db):
    db.expire_all()
    return {p.name: p for p in db.scalars(select(Photo)) if p.nsfw_sig == p.sig}



@needs_db
def test_photos_are_scored_apps_saves_first(db, lib):
    ready(db, "IMG_1.JPG", RED, make="Apple", sort_at=datetime(2025, 1, 2))
    ready(db, "saved.jpg", RED, make="", sort_at=datetime(2020, 1, 1))
    ready(db, "IMG_2.JPG", BLUE, make="Apple", sort_at=datetime(2025, 1, 1))
    assert nsfw.run_batch(db, get_settings(), limit=1) == 1
    assert set(scored(db)) == {"saved.jpg"}                  # older, but no camera wrote it
    assert nsfw.run_batch(db, get_settings(), limit=1) == 1
    assert set(scored(db)) == {"saved.jpg", "IMG_1.JPG"}     # then newest first
    nsfw.run_batch(db, get_settings())
    rows = scored(db)
    assert rows["IMG_1.JPG"].nsfw == 1.0 and rows["IMG_2.JPG"].nsfw == 0.0
    assert nsfw.run_batch(db, get_settings()) == 0
    assert pending(db, get_settings())["nsfw"] == 0


@needs_db
def test_a_video_waits_for_its_storyboard_and_takes_its_worst_frame(db, lib):
    clip = ready(db, "clip.mov", BLUE, duration=30.0)
    companion = ready(db, "IMG_3.MOV", RED, is_companion=True)
    assert nsfw.run_batch(db, get_settings()) == 0           # no storyboard yet
    strip = Image.new("RGB", (30, 10), BLUE)
    strip.paste(RED, (20, 0, 30, 10))                        # the last of three frames
    path = story_path(clip.sig)
    path.parent.mkdir(parents=True, exist_ok=True)
    strip.save(path, "WEBP", lossless=True)
    row = db.get(Photo, clip.id)
    row.story_sig, row.story_frames = row.sig, 3
    db.commit()
    assert nsfw.run_batch(db, get_settings()) == 1
    rows = scored(db)
    assert rows["clip.mov"].nsfw == 1.0                      # its thumbnail is blue
    assert "IMG_3.MOV" not in rows and companion.id


@needs_db
def test_a_video_whose_storyboard_file_is_gone_waits_for_a_new_one(db, lib):
    clip = ready(db, "clip.mov", RED, duration=3.0)
    row = db.get(Photo, clip.id)
    row.story_sig, row.story_frames = row.sig, 10
    db.commit()
    assert nsfw.run_batch(db, get_settings()) == 0
    db.expire_all()
    row = db.get(Photo, clip.id)
    assert (row.story_sig, row.nsfw_sig, row.fail_count) == ("", "", 0)


@needs_db
def test_a_lost_thumbnail_is_drawn_again(db, lib):
    row = ready(db, "a.jpg", RED)
    thumb_path(row.sig).unlink()
    assert nsfw.run_batch(db, get_settings()) == 0
    db.expire_all()
    row = db.get(Photo, row.id)
    assert row.thumb_sig == "" and row.fail_count == 0


@needs_db
def test_a_damaged_thumbnail_counts_against_the_photo(db, lib):
    row = ready(db, "a.jpg", RED)
    thumb_path(row.sig).write_bytes(b"not a webp")
    nsfw.run_batch(db, get_settings())
    db.expire_all()
    row = db.get(Photo, row.id)
    assert (row.error_stage, row.fail_count) == ("nsfw", 1)


@needs_db
def test_a_broken_model_blames_no_photo_and_backs_off(db, lib, monkeypatch):
    row = ready(db, "a.jpg", RED)
    calls = []

    def broken(settings):
        calls.append(1)
        raise RuntimeError("model download failed")

    monkeypatch.setattr(nsfw, "model", broken)
    pipe = Pipeline(get_settings())                          # the service, not --once
    try:
        assert pipe.nsfw_batch(db) == 0
        assert pipe.nsfw_batch(db) == 0                      # backing off: not asked again
    finally:
        pipe.close()
    assert len(calls) == 1 and "model download failed" in pipe.broken["nsfw"]
    db.expire_all()
    row = db.get(Photo, row.id)
    assert row.fail_count == 0 and row.nsfw_sig == ""


@needs_db
def test_disabled_does_nothing(db, lib, monkeypatch):
    ready(db, "a.jpg", RED)
    monkeypatch.setattr(get_settings(), "nsfw_enabled", False)
    pipe = Pipeline(get_settings(), once=True)
    try:
        assert pipe.nsfw_batch(db) == 0
    finally:
        pipe.close()
    assert pending(db, get_settings())["nsfw"] == 0
    assert scored(db) == {}


@needs_db
def test_threads_are_half_the_workers(lib, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "agent_workers", 8)
    assert nsfw.threads_for(s) == 4
    monkeypatch.setattr(s, "agent_workers", 1)
    assert nsfw.threads_for(s) == 1
