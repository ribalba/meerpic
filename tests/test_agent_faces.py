"""The faces stage: the arithmetic around the two models (SCRFD's anchors and
outputs, non-maximum suppression, the similarity transform and the warp that
lines a face up for ArcFace), decoding upright at the right size, the
stand-in the tests run on (``MEERPIC_FACES_FAKE=1``: a coloured patch is a
face, one colour one person), and the stage writing rows, crops and sigs.

The real models are exercised only when their files are already on this
machine (read, never downloaded or written by a test)."""

from __future__ import annotations

# Naive datetimes on purpose: meerpic stores naive UTC and naive wall clocks.
# ruff: noqa: DTZ001
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from agent_helpers import add_photo, jpeg, needs_db, use_db, use_library
from PIL import Image
from sqlalchemy import select

from agent import faces
from agent.pipeline import Pipeline, pending
from core.config import get_settings
from core.media import FACE_EDGE, face_path
from core.models import Face, Photo

RED, GREEN, BLUE, WHITE = (230, 20, 20), (20, 200, 40), (20, 40, 220), (250, 250, 250)


# --- the arithmetic ----------------------------------------------------------------


def test_anchors_are_every_point_of_the_map_twice_in_row_order():
    centers = faces.anchor_centers(32, edge=64)
    assert centers.tolist() == [[0, 0], [0, 0], [32, 0], [32, 0], [0, 32], [0, 32], [32, 32], [32, 32]]
    assert len(faces.anchor_centers(8)) == 80 * 80 * 2


def test_nms_keeps_the_best_of_overlapping_boxes():
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60], [0, 0, 10, 10.5]], dtype=np.float32)
    scores = np.array([0.8, 0.9, 0.7, 0.6], dtype=np.float32)
    assert faces.nms(boxes, scores).tolist() == [1, 2]
    assert faces.nms(np.zeros((0, 4)), np.zeros(0)).tolist() == []


def outputs(edge: int, hits: dict):
    """SCRFD's nine outputs for a ``edge`` square with nothing in it but
    ``hits``: {(stride, anchor index): (score, distances, landmark offsets)},
    all in stride units, as the model gives them."""
    outs = []
    for stride in faces.STRIDES:
        n = (edge // stride) ** 2 * faces.ANCHORS
        outs.append(np.zeros((n, 1), np.float32))
    for stride in faces.STRIDES:
        outs.append(np.zeros(((edge // stride) ** 2 * faces.ANCHORS, 4), np.float32))
    for stride in faces.STRIDES:
        outs.append(np.zeros(((edge // stride) ** 2 * faces.ANCHORS, 10), np.float32))
    for (stride, index), (score, dist, kps) in hits.items():
        i = faces.STRIDES.index(stride)
        outs[i][index, 0] = score
        outs[i + 3][index] = dist
        outs[i + 6][index] = kps
    return outs


def test_detections_are_read_in_canvas_pixels():
    # Anchor 2 * 5 + 1 of the stride-16 map of a 64 square: point (1, 1), at
    # (16, 16). Distances of 1 stride each way make a 32-pixel box around it.
    kps = [0, 0, 1, 0, 0.5, 0.5, 0, 1, 1, 1]
    outs = outputs(64, {
        (16, 11): (0.95, [1, 1, 1, 1], kps),
        (16, 10): (0.90, [1, 1, 1.1, 1.1], kps),     # the same face again, a little bigger
        (32, 0): (0.30, [1, 1, 1, 1], kps),          # not sure enough
        (8, 0): (0.80, [0.5, 0.5, 1, 1], kps),       # a small one in the corner
    })
    boxes, points, scores = faces.read_detections(outs, 0.5, edge=64)
    assert scores.tolist() == pytest.approx([0.95, 0.80])
    assert boxes[0].tolist() == [0, 0, 32, 32]
    assert points[0].tolist() == [[16, 16], [32, 16], [24, 24], [16, 32], [32, 32]]
    assert boxes[1].tolist() == [-4, -4, 8, 8]
    empty = faces.read_detections(outputs(64, {}), 0.5, edge=64)
    assert [a.shape[0] for a in empty] == [0, 0, 0]


def test_the_similarity_transform_undoes_a_turn_a_zoom_and_a_shift():
    angle = math.radians(30)
    turn = np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
    src = faces.ARCFACE_POINTS @ (2.5 * turn).T + np.array([300.0, 120.0])
    m = faces.similarity(src, faces.ARCFACE_POINTS)
    back = np.c_[src, np.ones(5)] @ m.T
    assert back[:, :2] == pytest.approx(faces.ARCFACE_POINTS, abs=1e-4)
    assert math.sqrt(np.linalg.det(m[:2, :2])) == pytest.approx(1 / 2.5)


@pytest.mark.parametrize("zoom", [1.0, 4.0])
def test_align_puts_the_eyes_where_arcface_wants_them(zoom):
    # A "face" ``zoom`` times ArcFace's size, shifted, with a red left eye.
    points = faces.ARCFACE_POINTS * zoom + np.array([37.0, 21.0])
    img = Image.new("RGB", (int(112 * zoom) + 80, int(112 * zoom) + 60), WHITE)
    x, y = points[0]
    r = 3 * zoom
    img.paste(RED, (round(x - r), round(y - r), round(x + r), round(y + r)))
    out = faces.align(img, points)
    assert out.size == (faces.ALIGN_EDGE, faces.ALIGN_EDGE)
    a = np.asarray(out, dtype=np.int32)
    red = (a[..., 0] > 150) & (a[..., 1] < 100)
    ys, xs = np.nonzero(red)
    assert xs.mean() == pytest.approx(faces.ARCFACE_POINTS[0][0], abs=1.5)
    assert ys.mean() == pytest.approx(faces.ARCFACE_POINTS[0][1], abs=1.5)


def test_align_outside_the_photo_is_black():
    points = faces.ARCFACE_POINTS - np.array([60.0, 0.0])       # half off the left edge
    out = np.asarray(faces.align(Image.new("RGB", (200, 200), WHITE), points))
    assert out[56, 2].tolist() == [0, 0, 0] and out[56, 110].tolist() == [250, 250, 250]


def test_the_crop_is_square_and_stays_inside_the_photo():
    img = Image.new("RGB", (400, 300), BLUE)
    img.paste(RED, (0, 0, 40, 40))
    c = faces.crop(img, np.array([0, 0, 40, 40]))
    assert c.size == (FACE_EDGE, FACE_EDGE)
    a = np.asarray(c)
    assert a[5, 5].tolist() == list(RED)                     # pushed in, not padded with black
    small = faces.crop(Image.new("RGB", (30, 20), BLUE), np.array([0, 0, 30, 20]))
    assert small.size == (FACE_EDGE, FACE_EDGE)


def test_the_detector_input_is_letterboxed():
    src = faces.source_of(Image.new("RGB", (320, 160), RED))
    assert src.pixels.shape == (160, 320, 3) and src.canvas.shape == (640, 640, 3)
    assert src.scale == 2.0
    assert src.canvas[319, 639].tolist() == list(RED) and src.canvas[320, 0].tolist() == [0, 0, 0]


def test_decode_is_upright_and_at_most_the_source_edge(tmp_path):
    turned = tmp_path / "turned.jpg"
    jpeg(turned, size=(80, 40), orientation=6)               # stored landscape, shown portrait
    assert faces.decode(faces.DecodeTask(1, str(turned))).pixels.shape[:2] == (80, 40)
    big = tmp_path / "big.png"
    Image.new("RGB", (4000, 1000), BLUE).save(big)
    src = faces.decode(faces.DecodeTask(2, str(big)))
    assert src.pixels.shape[:2] == (480, faces.SOURCE_EDGE)


# --- the stand-in ------------------------------------------------------------------


def picture(*patches, size=(400, 300)):
    """A white picture with coloured squares: (colour, x, y, side)."""
    img = Image.new("RGB", size, WHITE)
    for color, x, y, side in patches:
        img.paste(color, (x, y, x + side, y + side))
    return img


def test_the_stand_in_sees_one_face_per_colour_and_knows_them_again():
    assert faces.FAKE and faces.model_name(get_settings()) == "fake"
    m = faces.model(get_settings())
    found = faces.find(m, faces.source_of(picture((RED, 20, 20, 60), (BLUE, 200, 100, 120))), 0.5)
    assert len(found) == 2
    assert found[0].box.tolist() == [200, 100, 320, 220]     # the largest first
    assert found[0].crop.size == (FACE_EDGE, FACE_EDGE)
    again = faces.find(m, faces.source_of(picture((RED, 300, 200, 50))), 0.5)
    v = m.embed([f.aligned for f in found + again])
    assert v[1] @ v[2] == pytest.approx(1.0) and v[0] @ v[1] == pytest.approx(0.0)


def test_small_faces_are_left_out_and_many_are_capped(monkeypatch):
    m = faces.model(get_settings())
    tiny = faces.find(m, faces.source_of(picture((RED, 20, 20, faces.MIN_FACE - 4))), 0.5)
    assert tiny == []
    monkeypatch.setattr(faces, "MAX_FACES", 1)
    found = faces.find(m, faces.source_of(picture((RED, 20, 20, 60), (BLUE, 200, 100, 80))), 0.5)
    assert len(found) == 1 and found[0].box.tolist() == [200, 100, 280, 180]


_REAL = sorted(Path("~/.cache/meerpic/models").expanduser().glob(
    "models--immich-app--buffalo_l/snapshots/*/recognition/model.onnx"))


@pytest.mark.skipif(not _REAL, reason="the real face models are not on this machine")
def test_the_real_models_load_and_answer(monkeypatch):
    base = _REAL[-1].parent.parent
    files = {faces.DETECTION_FILE: base / faces.DETECTION_FILE, faces.RECOGNITION_FILE: base / faces.RECOGNITION_FILE}
    monkeypatch.setattr(faces, "download", lambda settings: files)
    m = faces.build(get_settings(), threads=2)
    assert faces.find(m, faces.source_of(Image.new("RGB", (300, 200), (128, 128, 128))), 0.7) == []
    rng = np.random.default_rng(1)
    noise = Image.fromarray(rng.integers(0, 255, (112, 112, 3), dtype=np.uint8))
    v = m.embed([noise, noise.transpose(Image.Transpose.FLIP_LEFT_RIGHT)] * 20)
    assert v.shape == (40, 512)
    assert np.linalg.norm(v, axis=1) == pytest.approx(1.0, abs=1e-5)
    # Seen and mirrored, summed: a face and its mirror image are one vector.
    assert v[0] @ v[1] == pytest.approx(1.0, abs=1e-4)


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


def ready(db, lib, name, img, **cols):
    """A photo on disk whose thumbnail is drawn, as the thumbs stage leaves it."""
    img.save(lib / name)
    row = add_photo(db, name, **cols)
    row.meta_sig = row.thumb_sig = row.sig
    db.commit()
    return row


def faces_of(db, photo_id):
    db.expire_all()
    return list(db.scalars(select(Face).where(Face.photo_id == photo_id).order_by(Face.pos)))


@needs_db
def test_the_stage_stores_faces_crops_and_its_sig(db, lib):
    two = ready(db, lib, "two.png", picture((RED, 20, 20, 60), (BLUE, 200, 100, 120)),
                sort_at=datetime(2025, 1, 2))
    none = ready(db, lib, "none.png", picture(), sort_at=datetime(2025, 1, 1))
    assert faces.run_batch(db, get_settings(), inline) == 2
    rows = faces_of(db, two.id)
    assert [(f.pos, f.model, f.sig) for f in rows] == [(0, "fake", two.sig), (1, "fake", two.sig)]
    big = rows[0]
    assert (big.x, big.y, big.w, big.h) == pytest.approx((0.5, 1 / 3, 0.3, 0.4), abs=1e-4)
    assert len(big.embedding) == 512 and big.score == pytest.approx(0.99)
    assert Image.open(face_path(two.sig, 0)).size == (FACE_EDGE, FACE_EDGE)
    assert face_path(two.sig, 1).is_file()
    assert faces_of(db, none.id) == []
    assert {p.faces_sig == p.sig for p in db.scalars(select(Photo))} == {True}
    assert faces.run_batch(db, get_settings(), inline) == 0
    assert pending(db, get_settings())["faces"] == 0


@needs_db
def test_a_changed_file_has_its_faces_replaced(db, lib):
    row = ready(db, lib, "a.png", picture((RED, 20, 20, 60), (BLUE, 200, 100, 120)))
    faces.run_batch(db, get_settings(), inline)
    picture((GREEN, 50, 50, 80)).save(lib / "a.png")
    photo = db.get(Photo, row.id)
    photo.sig = photo.thumb_sig = photo.meta_sig = "0123456789abcdef"
    db.commit()
    assert pending(db, get_settings())["faces"] == 1
    assert faces.run_batch(db, get_settings(), inline) == 1
    rows = faces_of(db, row.id)
    assert [(f.pos, f.sig) for f in rows] == [(0, "0123456789abcdef")]


@needs_db
def test_videos_companions_and_originals_behind_edits_are_skipped(db, lib):
    edit = ready(db, lib, "IMG_1-edited.png", picture((RED, 20, 20, 60)))
    ready(db, lib, "IMG_1.png", picture((RED, 20, 20, 60)), superseded_by=edit.id)
    clip = add_photo(db, "clip.mov")
    clip.meta_sig = clip.thumb_sig = clip.sig
    db.commit()
    assert faces.run_batch(db, get_settings(), inline) == 1
    assert pending(db, get_settings())["faces"] == 0
    assert {p.name for p in db.scalars(select(Photo)) if p.faces_sig} == {"IMG_1-edited.png"}


@needs_db
def test_a_photo_waits_for_its_thumbnail(db, lib):
    row = ready(db, lib, "a.png", picture((RED, 20, 20, 60)))
    row.thumb_sig = ""
    db.commit()
    assert faces.run_batch(db, get_settings(), inline) == 0


@needs_db
def test_a_file_that_will_not_decode_counts_against_the_photo(db, lib):
    row = ready(db, lib, "a.png", picture((RED, 20, 20, 60)))
    (lib / "a.png").write_bytes(b"not a png")
    assert faces.run_batch(db, get_settings(), inline) == 0
    db.expire_all()
    row = db.get(Photo, row.id)
    assert (row.error_stage, row.fail_count, row.faces_sig) == ("faces", 1, "")


@needs_db
def test_the_pipeline_runs_it_in_its_process_pool(db, lib):
    row = ready(db, lib, "a.png", picture((RED, 20, 20, 60)))
    pipe = Pipeline(get_settings(), once=True)
    try:
        assert pipe.faces_batch(db) == 1
    finally:
        pipe.close()
    assert len(faces_of(db, row.id)) == 1 and pipe.counts["faces"] == 1


@needs_db
def test_broken_models_blame_no_photo_and_back_off(db, lib, monkeypatch):
    row = ready(db, lib, "a.png", picture((RED, 20, 20, 60)))
    calls = []

    def broken(settings):
        calls.append(1)
        raise RuntimeError("model download failed")

    monkeypatch.setattr(faces, "model", broken)
    pipe = Pipeline(get_settings())                          # the service, not --once
    try:
        assert pipe.faces_batch(db) == 0
        assert pipe.faces_batch(db) == 0                     # backing off: not asked again
    finally:
        pipe.close()
    assert len(calls) == 1 and "model download failed" in pipe.broken["faces"]
    db.expire_all()
    row = db.get(Photo, row.id)
    assert row.fail_count == 0 and row.faces_sig == ""


@needs_db
def test_disabled_does_nothing(db, lib, monkeypatch):
    ready(db, lib, "a.png", picture((RED, 20, 20, 60)))
    monkeypatch.setattr(get_settings(), "faces_enabled", False)
    pipe = Pipeline(get_settings(), once=True)
    try:
        assert pipe.faces_batch(db) == 0
    finally:
        pipe.close()
    assert pending(db, get_settings())["faces"] == 0
