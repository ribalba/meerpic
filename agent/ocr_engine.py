"""Reading the text in a photo: PaddleOCR's PP-OCRv6 on ONNX Runtime.

Two models. The detector (DB, "differentiable binarisation") paints a map of
how likely every pixel is to be text; the regions above a threshold are grown
back out (the model was trained on shrunk text) and become one quad per line,
turned with the line when it is written at an angle. The recogniser is shown
each quad straightened out, 48 pixels high, and answers with a probability
for every character at every step along the line; CTC reads that off: the
likeliest character per step, repeats merged, the "blank" in between dropped.
Its alphabet (18,708 characters: Latin with umlauts and ß, Greek, Cyrillic,
CJK, symbols, emoji) is stored in the ONNX file itself.

Det tiny with rec small, measured on this library: the tiny detector is about
7x faster than the small one and finds nearly the same boxes, and detection
runs on every photo, while recognition runs only where text was found. The
tiny recogniser made visible mistakes ("Aach wenn" for "Auch wenn") and read
CJK glyphs into UI icons, so there the small one is worth its 21 MB.

The detector sees the photo scaled to at most ``DET_PIXELS``: text a reader
can make out at 1 MP is found, and the time grows with the pixels. The crops
the recogniser reads are cut from the full image the caller passed, where
small print still has its strokes.

This is our own glue rather than the ``rapidocr`` package, whose code it
follows step for step (preprocessing, DBPostProcess with its default
thresholds, the crop, the batching, CTC) and whose output it was checked
against on sample photos: the same lines, the same text. That package pulls
the full opencv-python (which wants libGL), omegaconf and colorlog into the
agent for what is a couple of hundred lines. Here it is opencv-python-headless
and pyclipper. The models are Apache-2.0, trained by PaddleOCR and converted
to ONNX by RapidOCR, fetched from its ModelScope release with pinned hashes.
"""

from __future__ import annotations

import hashlib
import math
import os
import secrets
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

RELEASE = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv6"
DET_FILE = "PP-OCRv6_det_tiny.onnx"
REC_FILE = "PP-OCRv6_rec_small.onnx"
MODELS = {
    DET_FILE: (f"{RELEASE}/det/{DET_FILE}", "f42c0fbd294d95eac1a550e131b277dac97462c8025fa4b6c3cec1b7894bd3d5"),
    REC_FILE: (f"{RELEASE}/rec/{REC_FILE}", "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884"),
}
TIMEOUT = 60            # s, for each read of a download

DET_PIXELS = 1_000_000  # the detector sees the photo scaled to at most this many pixels
MIN_SCORE = 0.5         # recognised lines below this confidence are dropped

# DBPostProcess, with RapidOCR's defaults from its config.yaml.
DB_THRESH = 0.3         # a pixel of the map above this is text
BOX_THRESH = 0.5        # a region whose mean probability is below this is not
UNCLIP_RATIO = 1.6      # how far a region is grown back out
MAX_CANDIDATES = 1000   # regions looked at per photo
MIN_SIDE = 3            # px on the map: a thinner region is a speck
ROW_GAP = 10            # px on the map: boxes whose tops are closer are one row

REC_HEIGHT = 48         # px, the recogniser's input height
REC_WIDTH = 320         # px, its narrowest input: a wider line widens its whole batch
REC_BATCH = 6           # crops per inference call, of similar width


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch(url: str, sha256: str, path: Path) -> None:
    """``url`` to ``path`` if it hashes to ``sha256``; a reader never sees
    half a file, and a bad or broken download leaves none."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp, tmp.open("wb") as fh:
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                digest.update(chunk)
                fh.write(chunk)
        if digest.hexdigest() != sha256:
            raise RuntimeError(f"{url}: sha256 is {digest.hexdigest()}, expected {sha256}")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def download(dest: Path) -> dict[str, Path]:
    """Both files in ``dest``, fetched when missing or not what was pinned."""
    dest.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, (url, sha256) in MODELS.items():
        path = dest / name
        if not (path.is_file() and sha256_of(path) == sha256):
            _fetch(url, sha256, path)
        files[name] = path
    return files


@dataclass(frozen=True)
class Line:
    text: str
    score: float
    box: tuple[float, float, float, float]  # x, y, w, h of the text's bounds, as fractions of the image


# --- detection -------------------------------------------------------------------


def det_size(width: int, height: int, budget: int = DET_PIXELS) -> tuple[int, int]:
    """The detector's input size: the aspect kept, at most ``budget`` pixels,
    both sides multiples of 32 (the network halves its maps five times) and
    at least 32. Never larger than the image but for that rounding."""
    scale = min(1.0, math.sqrt(budget / max(1, width * height)))
    w, h = width * scale, height * scale
    dw, dh = max(32, round(w / 32) * 32), max(32, round(h / 32) * 32)
    # Rounding both sides to the nearest step can overshoot the budget by a
    # row or column: take it back from the side that was rounded up most.
    while dw * dh > budget and max(dw, dh) > 32:
        if dh == 32 or (dw > 32 and dw / w >= dh / h):
            dw -= 32
        else:
            dh -= 32
    return dw, dh


def det_tensor(bgr: np.ndarray) -> np.ndarray:
    """H x W x 3 uint8 to the detector's 1 x 3 x H x W, scaled to -1..1."""
    x = (bgr.astype(np.float32) * (1 / 255.0) - 0.5) / 0.5
    return np.ascontiguousarray(x.transpose(2, 0, 1)[np.newaxis])


def mini_box(points: np.ndarray) -> tuple[np.ndarray, float]:
    """The smallest rotated rectangle around ``points``: its corners as
    top-left, top-right, bottom-right, bottom-left, and its short side."""
    import cv2

    rect = cv2.minAreaRect(points)
    p = sorted(cv2.boxPoints(rect), key=lambda q: q[0])
    tl, bl = (0, 1) if p[1][1] > p[0][1] else (1, 0)
    tr, br = (2, 3) if p[3][1] > p[2][1] else (3, 2)
    return np.array([p[tl], p[tr], p[br], p[bl]]), min(rect[1])


def box_score(prob: np.ndarray, box: np.ndarray) -> float:
    """The mean probability inside ``box`` (DB's "fast" score)."""
    import cv2

    h, w = prob.shape
    xmin = np.clip(np.floor(box[:, 0].min()).astype(np.int32), 0, w - 1)
    xmax = np.clip(np.ceil(box[:, 0].max()).astype(np.int32), 0, w - 1)
    ymin = np.clip(np.floor(box[:, 1].min()).astype(np.int32), 0, h - 1)
    ymax = np.clip(np.ceil(box[:, 1].max()).astype(np.int32), 0, h - 1)
    mask = np.zeros((ymax - ymin + 1, xmax - xmin + 1), dtype=np.uint8)
    local = box.copy()
    local[:, 0] -= xmin
    local[:, 1] -= ymin
    cv2.fillPoly(mask, local.reshape(1, -1, 2).astype(np.int32), 1)
    return cv2.mean(prob[ymin:ymax + 1, xmin:xmax + 1], mask)[0]


def unclip(box: np.ndarray, ratio: float = UNCLIP_RATIO) -> np.ndarray:
    """``box`` grown by area * ratio / perimeter on every side: the model
    marks text shrunk by that much."""
    import pyclipper

    x, y = box[:, 0].astype(np.float64), box[:, 1].astype(np.float64)
    area = abs(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y)) / 2
    length = np.hypot(x - np.roll(x, -1), y - np.roll(y, -1)).sum()
    offset = pyclipper.PyclipperOffset()
    offset.AddPath(box, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
    return np.array([p for path in offset.Execute(area * ratio / length) for p in path]).reshape(-1, 1, 2)


def order_corners(box: np.ndarray) -> np.ndarray:
    """Top-left, top-right, bottom-right, bottom-left, by x then y."""
    by_x = box[np.argsort(box[:, 0]), :]
    left, right = by_x[:2], by_x[2:]
    tl, bl = left[np.argsort(left[:, 1]), :]
    tr, br = right[np.argsort(right[:, 1]), :]
    return np.array([tl, tr, br, bl], dtype=np.float32)


def db_boxes(
    prob: np.ndarray,
    thresh: float = DB_THRESH,
    box_thresh: float = BOX_THRESH,
    unclip_ratio: float = UNCLIP_RATIO,
    max_candidates: int = MAX_CANDIDATES,
) -> tuple[np.ndarray, list[float]]:
    """The detector's H x W probability map to text quads (n x 4 x 2, whole
    pixels of the map, clipped to it) and their scores, as RapidOCR's
    DBPostProcess and filter_det_res make them."""
    import cv2

    h, w = prob.shape
    # A 2 x 2 dilation joins the letters of a word the threshold left apart.
    mask = cv2.dilate((prob > thresh).astype(np.uint8), np.ones((2, 2), np.uint8))
    contours, _ = cv2.findContours(mask * 255, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes, scores = [], []
    for contour in contours[:max_candidates]:
        points, side = mini_box(contour)
        if side < MIN_SIDE:
            continue
        score = box_score(prob, points.reshape(-1, 2))
        if score < box_thresh:
            continue
        grown = unclip(points, unclip_ratio)
        if len(grown) < 3:
            continue
        box, side = mini_box(grown)
        if side < MIN_SIDE + 2:
            continue
        # RapidOCR scales to the size it was given, which is the map's own;
        # x / w * w is not always x in float32, and that decides some roundings.
        box[:, 0] = np.clip(np.round(box[:, 0] / w * w), 0, w)
        box[:, 1] = np.clip(np.round(box[:, 1] / h * h), 0, h)
        box = order_corners(box.astype(np.int32))
        box[:, 0] = np.clip(box[:, 0], 0, w - 1)
        box[:, 1] = np.clip(box[:, 1], 0, h - 1)
        if int(np.linalg.norm(box[0] - box[1])) <= 3 or int(np.linalg.norm(box[0] - box[3])) <= 3:
            continue
        boxes.append(box)
        scores.append(score)
    return np.array(boxes, dtype=np.float32).reshape(-1, 4, 2), scores


def reading_order(boxes: np.ndarray, gap: float = ROW_GAP) -> np.ndarray:
    """Indices of ``boxes`` top to bottom, then left to right within a row:
    tops less than ``gap`` apart are one row (RapidOCR's sorted_boxes)."""
    if len(boxes) == 0:
        return np.zeros(0, dtype=np.intp)
    by_y = np.argsort(boxes[:, 0, 1], kind="stable")
    rows = np.concatenate([[0], np.cumsum(np.diff(boxes[by_y, 0, 1]) >= gap)])
    return by_y[np.lexsort((boxes[by_y, 0, 0], rows))]


# --- recognition -----------------------------------------------------------------


def crop(bgr: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """The quad cut out of the image and straightened; turned a quarter when
    it is much taller than wide, as vertical text is read across."""
    import cv2

    quad = np.asarray(quad, dtype=np.float32)
    width = max(1, int(max(np.linalg.norm(quad[0] - quad[1]), np.linalg.norm(quad[2] - quad[3]))))
    height = max(1, int(max(np.linalg.norm(quad[0] - quad[3]), np.linalg.norm(quad[1] - quad[2]))))
    square = np.array([[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float32)
    out = cv2.warpPerspective(bgr, cv2.getPerspectiveTransform(quad, square), (width, height),
                              borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC)
    if out.shape[0] * 1.0 / out.shape[1] >= 1.5:
        out = np.rot90(out)
    return out


def rec_input(crop: np.ndarray, width: int) -> np.ndarray:
    """A crop as the recogniser wants it: 3 x 48 x ``width``, scaled to -1..1,
    its aspect kept and the rest of the width left 0."""
    import cv2

    h, w = crop.shape[:2]
    # 48 * (w / h), not 48 * w / h: the same float as RapidOCR, the same ceil.
    resized_w = min(width, math.ceil(REC_HEIGHT * (w / float(h))))
    x = cv2.resize(crop, (resized_w, REC_HEIGHT)).astype(np.float32).transpose(2, 0, 1) / 255
    x -= 0.5
    x /= 0.5
    out = np.zeros((3, REC_HEIGHT, width), dtype=np.float32)
    out[:, :, :resized_w] = x
    return out


def characters(meta: str) -> list[str]:
    """The recogniser's classes from its ONNX metadata: CTC's blank first,
    then the alphabet, then the space the list leaves out."""
    return ["blank", *meta.splitlines(), " "]


def ctc_decode(probs: np.ndarray, chars: list[str]) -> list[tuple[str, float]]:
    """N x T x C probabilities to (text, score) per line: the likeliest class
    per step, a class repeated in adjacent steps once, blanks dropped. The
    score is the mean probability of the characters kept, 0 for none."""
    out = []
    for idx, prob in zip(probs.argmax(axis=2), probs.max(axis=2), strict=True):
        keep = np.ones(len(idx), dtype=bool)
        keep[1:] = idx[1:] != idx[:-1]
        keep &= idx != 0
        text = "".join(chars[i] for i in idx[keep])
        out.append((text, float(prob[keep].mean(dtype=np.float64)) if keep.any() else 0.0))
    return out


# --- the engine ------------------------------------------------------------------


class Engine:
    """The two ONNX Runtime sessions and the recogniser's alphabet."""

    def __init__(self, det_path: Path, rec_path: Path, threads: int = 1):
        import onnxruntime as ort

        def session(path: Path):
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = max(1, threads)
            opts.inter_op_num_threads = 1
            # Every photo is another input shape: the arena would hold on to
            # the largest one's buffers for good.
            opts.enable_cpu_mem_arena = False
            return ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])

        self.det = session(det_path)
        self.rec = session(rec_path)
        self.det_input = self.det.get_inputs()[0].name
        self.rec_input = self.rec.get_inputs()[0].name
        self.chars = characters(self.rec.get_modelmeta().custom_metadata_map["character"])

    def detect(self, img) -> np.ndarray:
        """The text quads of an RGB image (n x 4 x 2, its pixels), in reading order."""
        from PIL import Image

        width, height = img.size
        dw, dh = det_size(width, height)
        small = np.asarray(img.resize((dw, dh), Image.Resampling.BILINEAR))[:, :, ::-1]
        prob = self.det.run(None, {self.det_input: det_tensor(small)})[0][0, 0]
        boxes, _ = db_boxes(prob)
        # Sorted where RapidOCR sorts them, on the detector's input, so that
        # ROW_GAP means what it does there; then scaled to the whole image.
        quads = boxes[reading_order(boxes)] * np.array([width / dw, height / dh], dtype=np.float32)
        quads[..., 0] = np.clip(quads[..., 0], 0, width - 1)
        quads[..., 1] = np.clip(quads[..., 1], 0, height - 1)
        return quads

    def recognize(self, crops: list[np.ndarray]) -> list[tuple[str, float]]:
        """(text, score) for each crop, in their order. They are read in
        batches of similar width, so that few are padded much."""
        ratios = [c.shape[1] / float(c.shape[0]) for c in crops]
        order = np.argsort(np.array(ratios))
        out: list[tuple[str, float]] = [("", 0.0)] * len(crops)
        for start in range(0, len(crops), REC_BATCH):
            batch = order[start:start + REC_BATCH]
            widest = max(REC_WIDTH / REC_HEIGHT, *(ratios[i] for i in batch))
            width = int(REC_HEIGHT * widest)
            x = np.stack([rec_input(crops[i], width) for i in batch])
            probs = self.rec.run(None, {self.rec_input: x})[0]
            for i, res in zip(batch, ctc_decode(probs, self.chars), strict=True):
                out[i] = res
        return out

    def read(self, img) -> list[Line]:
        """Every line of text in an RGB image, in reading order; [] for none."""
        if img.mode != "RGB":
            img = img.convert("RGB")
        quads = self.detect(img)
        if not len(quads):
            return []
        # Like the model's training data, and the detector's input: BGR.
        bgr = np.ascontiguousarray(np.asarray(img)[:, :, ::-1])
        read = self.recognize([crop(bgr, q) for q in quads])
        width, height = img.size
        lines = []
        for quad, (text, score) in zip(quads, read, strict=True):
            if score < MIN_SCORE or not text.strip():
                continue
            (x0, y0), (x1, y1) = quad.min(axis=0), quad.max(axis=0)
            box = (float(x0) / width, float(y0) / height, float(x1 - x0) / width, float(y1 - y0) / height)
            lines.append(Line(text.strip(), score, box))
        return lines
