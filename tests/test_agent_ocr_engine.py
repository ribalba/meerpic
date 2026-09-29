"""The reader's own arithmetic: the detector's input size, the probability
map to boxes, reading order, the crop, the recogniser's input and CTC, and
a download that is not what was pinned leaving nothing behind.

No model is loaded here. The whole reader was checked against RapidOCR's
output on real photos (the same lines, character for character); these
keep the pieces it is made of from drifting."""

from __future__ import annotations

import hashlib
import io

import numpy as np
import pytest

from agent import ocr_engine as oe

# The engine imports these when it runs, not when it is imported.
pytest.importorskip("cv2")
pytest.importorskip("pyclipper")


# --- the detector's input ----------------------------------------------------------


@pytest.mark.parametrize(("size", "want"), [
    ((4032, 3024), (1152, 864)),        # a 12 MP photo: a million pixels, 4:3 kept
    ((1170, 2532), (672, 1472)),        # a screenshot: tall, the same budget
    ((640, 480), (640, 480)),           # small enough already: not scaled up
    ((20, 10), (32, 32)),               # tiny: the network's smallest input
])
def test_the_detector_sees_about_a_million_pixels_in_steps_of_32(size, want):
    w, h = oe.det_size(*size)
    assert (w, h) == want
    assert w % 32 == 0 and h % 32 == 0
    assert w * h <= max(oe.DET_PIXELS, 32 * 32)


def test_the_budget_is_never_overshot_by_rounding():
    for width in range(900, 1300, 7):
        for height in range(700, 1100, 11):
            w, h = oe.det_size(width, height, budget=500_000)
            assert w * h <= 500_000 and w % 32 == 0 and h % 32 == 0


def test_the_detector_input_is_scaled_to_minus_one_to_one():
    x = oe.det_tensor(np.array([[[0, 255, 51]]], dtype=np.uint8))
    assert x.shape == (1, 3, 1, 1) and x.dtype == np.float32
    assert x.ravel().tolist() == pytest.approx([-1.0, 1.0, -0.6])


# --- the probability map to boxes --------------------------------------------------


def blocks(*rects, shape=(96, 160)):
    prob = np.zeros(shape, dtype=np.float32)
    for x0, y0, x1, y1, value in rects:
        prob[y0:y1, x0:x1] = value
    return prob


def test_an_empty_map_has_no_boxes():
    boxes, scores = oe.db_boxes(np.zeros((64, 64), dtype=np.float32))
    assert boxes.shape == (0, 4, 2) and scores == []


def test_a_line_is_found_where_it_is_and_grown_back_out():
    boxes, scores = oe.db_boxes(blocks((20, 30, 120, 42, 0.9)))
    # The mean inside the box, which the dilation made a pixel larger.
    assert len(boxes) == 1 and 0.75 < scores[0] < 0.9
    (x0, y0), (x1, y1) = boxes[0].min(axis=0), boxes[0].max(axis=0)
    # The model marks text shrunk; the box covers it and a margin around it.
    assert x0 < 20 and y0 < 30 and x1 > 120 and y1 > 42
    assert x0 > 10 and y0 > 20 and x1 < 130 and y1 < 52
    # Corners clockwise from the top left.
    tl, tr, br, bl = boxes[0]
    assert tl[0] < tr[0] and bl[0] < br[0] and tl[1] < bl[1] and tr[1] < br[1]


def test_weak_regions_and_specks_are_dropped():
    boxes, _ = oe.db_boxes(blocks((20, 10, 120, 22, 0.9), (20, 50, 120, 62, 0.4), (140, 80, 142, 82, 0.9)))
    assert len(boxes) == 1 and boxes[0][:, 1].max() < 40


def test_boxes_stay_inside_the_map():
    boxes, _ = oe.db_boxes(blocks((0, 0, 160, 10, 0.9)))
    assert len(boxes) == 1
    assert boxes[..., 0].min() >= 0 and boxes[..., 0].max() <= 159
    assert boxes[..., 1].min() >= 0 and boxes[..., 1].max() <= 95


def test_lines_are_read_top_to_bottom_and_left_to_right():
    def quad(x, y):
        return [[x, y], [x + 20, y], [x + 20, y + 8], [x, y + 8]]

    # Two words on one row (their tops 4 px apart), and a line below.
    boxes = np.array([quad(60, 104), quad(10, 50), quad(70, 52), quad(40, 100)], dtype=np.float32)
    assert oe.reading_order(boxes).tolist() == [1, 2, 3, 0]
    assert oe.reading_order(np.zeros((0, 4, 2), dtype=np.float32)).tolist() == []


# --- the crop and the recogniser's input -------------------------------------------


def test_the_crop_is_the_quad_straightened():
    img = np.zeros((50, 100, 3), dtype=np.uint8)
    img[10:20, 30:70] = (255, 0, 0)
    out = oe.crop(img, np.array([[30, 10], [70, 10], [70, 20], [30, 20]], dtype=np.float32))
    assert out.shape == (10, 40, 3)
    assert (out[2:-2, 2:-2] == (255, 0, 0)).all()


def test_a_tall_crop_is_turned_to_be_read_across():
    img = np.zeros((100, 50, 3), dtype=np.uint8)
    out = oe.crop(img, np.array([[10, 10], [20, 10], [20, 60], [10, 60]], dtype=np.float32))
    assert out.shape[:2] == (10, 50)
    assert oe.rec_input(out, 320).shape == (3, 48, 320)      # and it can be read


def test_the_recogniser_input_keeps_the_aspect_and_pads_with_zero():
    crop = np.full((24, 48, 3), 255, dtype=np.uint8)          # 2:1, so 96 wide at 48 high
    x = oe.rec_input(crop, 320)
    assert x.shape == (3, 48, 320) and x.dtype == np.float32
    assert (x[:, :, :96] == 1.0).all() and (x[:, :, 96:] == 0.0).all()
    # Wider than the batch: squeezed into it.
    assert oe.rec_input(np.zeros((10, 1000, 3), dtype=np.uint8), 320)[:, :, -1].min() == -1.0


# --- CTC ---------------------------------------------------------------------------


def steps(*classes, n=6, p=0.9):
    """One line's probabilities: ``classes`` one per step, each at ``p``."""
    out = np.full((len(classes), n), (1 - p) / (n - 1), dtype=np.float32)
    for t, c in enumerate(classes):
        out[t, c] = p
    return out


def test_the_alphabet_has_the_blank_first_and_the_space_last():
    chars = oe.characters("a\nb\nü")
    assert chars == ["blank", "a", "b", "ü", " "]


def test_ctc_merges_repeats_and_drops_blanks():
    chars = oe.characters("a\nb\nü\nß")                        # 0 blank, 5 the space
    probs = np.stack([
        steps(1, 1, 0, 1, 5, 3, 3),                            # a a _ a ' ' ü ü
        steps(0, 4, 4, 4, 0, 0, 0),
        steps(0, 0, 0, 0, 0, 0, 0),
    ])
    assert oe.ctc_decode(probs, chars) == [
        ("aa ü", pytest.approx(0.9)),                          # a blank keeps a double letter double
        ("ß", pytest.approx(0.9)),
        ("", 0.0),
    ]


def test_the_score_is_the_mean_of_the_kept_characters():
    chars = oe.characters("a\nb")
    probs = np.stack([np.concatenate([steps(1, n=4, p=0.9), steps(0, n=4, p=0.2), steps(2, n=4, p=0.5)])])
    [(text, score)] = oe.ctc_decode(probs, chars)
    assert text == "ab" and score == pytest.approx(0.7)


# --- the download ------------------------------------------------------------------


def test_a_download_that_is_not_what_was_pinned_leaves_nothing(tmp_path, monkeypatch):
    payload = b"not the model"
    monkeypatch.setattr(oe.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(payload))
    monkeypatch.setattr(oe, "MODELS", {"m.onnx": ("https://example.invalid/m.onnx", "0" * 64)})
    with pytest.raises(RuntimeError, match="sha256"):
        oe.download(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_file_is_fetched_once_and_again_when_it_changed(tmp_path, monkeypatch):
    payload = b"the model"
    calls = []

    def urlopen(url, timeout):
        calls.append(url)
        return io.BytesIO(payload)

    monkeypatch.setattr(oe.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(oe, "MODELS", {"m.onnx": ("https://example.invalid/m.onnx",
                                                  hashlib.sha256(payload).hexdigest())})
    assert oe.download(tmp_path) == {"m.onnx": tmp_path / "m.onnx"}
    assert oe.download(tmp_path) and len(calls) == 1
    (tmp_path / "m.onnx").write_bytes(b"half a mod")
    oe.download(tmp_path)
    assert len(calls) == 2 and (tmp_path / "m.onnx").read_bytes() == payload
    assert [p.name for p in tmp_path.iterdir()] == ["m.onnx"]
