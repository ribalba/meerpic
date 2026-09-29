"""The faces stage: where the faces are in each photo, and whose they are.

Two models, InsightFace's ``buffalo_l`` as Immich exports it to ONNX.
SCRFD-10GF finds the faces and five landmarks on each (the eyes, the nose,
the corners of the mouth). ArcFace (a ResNet-50 trained on WebFace600K) is
then shown each face turned upright and scaled so that those landmarks sit
where it expects them, and answers with 512 numbers: two faces of one
person are close in that space, across years, glasses and light, and two
people are not. It is the most accurate recognition model InsightFace
publishes (MR-ALL 91.25, against 86.79 for antelopev2's ResNet-100). Its
weights are licensed for non-commercial use only.

Nothing is grouped here. A face is found and measured, and "who else is
this" is a nearest-neighbour question the server asks when somebody clicks
a face (app/query.py, ``face:``): no clusters to go stale, to chain two
cousins into one person, or to redo when a photo arrives.

The input is the original, not the thumbnail: 400 pixels across a group
photo leave each face a smudge. The process pool decodes it at up to
``SOURCE_EDGE`` pixels, which is the slow half (a HEIC is 0.2 s), and
letterboxes a copy for the detector. The models run here, in the agent's own
process like the other two, so one copy of their 190 MB serves every worker.
Only stills: a Live Photo's still stands for its motion, and a storyboard's
frames are too small to recognise anybody in.

``MEERPIC_FACES_FAKE=1`` swaps both models for a stand-in with no download:
every strongly coloured patch is a "face", and patches of one colour are one
"person". The alignment and the crops run for real either way.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from core.config import Settings
from core.media import FACE_EDGE, face_path, original_path
from core.models import FACE_DIM, Face, Photo
from core.timeutil import utcnow

from . import stages

FAKE = os.environ.get("MEERPIC_FACES_FAKE", "").strip() not in ("", "0", "false")

DETECTION_FILE = "detection/model.onnx"
RECOGNITION_FILE = "recognition/model.onnx"

SOURCE_EDGE = 1920      # px, the long edge a photo is decoded at
DETECT_EDGE = 640       # px, the detector's square input: the size SCRFD was trained at
ALIGN_EDGE = 112        # px, ArcFace's square input
NMS_IOU = 0.4           # boxes overlapping more than this are one face
# Smaller than this at SOURCE_EDGE is a face in the crowd: too few pixels to
# say who it is, and a vector made from them lands near everybody.
MIN_FACE = 32
MAX_FACES = 24          # per photo, the largest
BATCH = 16              # photos per turn
EMBED_BATCH = 32        # faces per inference call

# Where ArcFace wants the five landmarks in its 112 x 112 input: left eye,
# right eye, nose tip, left and right corner of the mouth.
ARCFACE_POINTS = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]],
    dtype=np.float32,
)
# SCRFD's three feature maps, and the two anchors at every point of each.
STRIDES = (8, 16, 32)
ANCHORS = 2


def model_name(settings: Settings) -> str:
    return "fake" if FAKE else settings.faces_model


# --- decoding, in the process pool ------------------------------------------------


@dataclass(frozen=True)
class DecodeTask:
    photo_id: int
    src: str


@dataclass(frozen=True)
class Source:
    """One photo, ready for the detector."""

    pixels: np.ndarray      # H x W x 3, upright, the long edge at most SOURCE_EDGE
    canvas: np.ndarray      # DETECT_EDGE square, ``pixels`` scaled into its top-left corner
    scale: float            # canvas pixels per ``pixels`` pixel


def source_of(img) -> Source:
    """An upright RGB image as the detector wants it: the long edge scaled to
    DETECT_EDGE (up, too: faces in a small picture are small), the rest of
    the square left black."""
    from PIL import Image

    scale = DETECT_EDGE / max(img.size)
    w, h = min(DETECT_EDGE, max(1, round(img.width * scale))), min(DETECT_EDGE, max(1, round(img.height * scale)))
    canvas = np.zeros((DETECT_EDGE, DETECT_EDGE, 3), dtype=np.uint8)
    canvas[:h, :w] = np.asarray(img.resize((w, h), Image.Resampling.BILINEAR))
    return Source(np.asarray(img), canvas, scale)


def decode(task: DecodeTask) -> Source:
    """The process pool's entry point: top-level, one picklable argument."""
    from PIL import Image, ImageOps

    # thumbs registers the HEIF opener, and knows what to do with a
    # transparent PNG or a 16-bit TIFF.
    from .thumbs import _flatten

    with Image.open(task.src) as img:
        img.seek(0)             # the first frame of an animated GIF/WEBP
        orientation = img.getexif().get(0x0112, 1)
        turned = orientation in (5, 6, 7, 8)
        shown = (img.height, img.width) if turned else img.size
        scale = min(1.0, SOURCE_EDGE / max(shown))
        size = (max(1, round(shown[0] * scale)), max(1, round(shown[1] * scale)))
        if img.format == "JPEG":
            # libjpeg decodes at 1/2, 1/4 or 1/8 for less than the whole cost.
            img.draft("RGB", (size[1], size[0]) if turned else size)
        img = _flatten(ImageOps.exif_transpose(img))
        if img.size != size:
            img = img.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
        return source_of(img)


# --- the arithmetic around the models --------------------------------------------


def anchor_centers(stride: int, edge: int = DETECT_EDGE) -> np.ndarray:
    """(x, y) of every anchor of one feature map, each point twice, in the
    order SCRFD's outputs list them."""
    n = edge // stride
    ys, xs = np.mgrid[:n, :n]
    centers = np.stack([xs, ys], axis=-1).reshape(-1, 2).astype(np.float32) * stride
    return np.repeat(centers, ANCHORS, axis=0)


def nms(boxes: np.ndarray, scores: np.ndarray, iou: float = NMS_IOU) -> np.ndarray:
    """Indices of the boxes to keep, best first: each box that overlaps a
    better one by more than ``iou`` is the same face found twice."""
    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i, rest = order[0], order[1:]
        keep.append(i)
        w = np.maximum(0.0, np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]))
        h = np.maximum(0.0, np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]))
        inter = w * h
        overlap = inter / np.maximum(areas[i] + areas[rest] - inter, 1e-9)
        order = rest[overlap <= iou]
    return np.asarray(keep, dtype=np.int64)


def read_detections(outs: list, threshold: float, edge: int = DETECT_EDGE):
    """SCRFD's nine outputs (scores, box distances and landmark offsets for
    each stride) as (boxes N x 4, landmarks N x 5 x 2, scores N) in canvas
    pixels, one row per face, best first."""
    boxes, points, scores = [], [], []
    for i, stride in enumerate(STRIDES):
        score = np.asarray(outs[i], dtype=np.float32).reshape(-1)
        hit = np.nonzero(score >= threshold)[0]
        if not hit.size:
            continue
        centers = anchor_centers(stride, edge)[hit]
        dist = np.asarray(outs[i + 3], dtype=np.float32).reshape(-1, 4)[hit] * stride
        kps = np.asarray(outs[i + 6], dtype=np.float32).reshape(-1, 5, 2)[hit] * stride
        boxes.append(np.concatenate([centers - dist[:, :2], centers + dist[:, 2:]], axis=1))
        points.append(kps + centers[:, None, :])
        scores.append(score[hit])
    if not boxes:
        return np.zeros((0, 4), np.float32), np.zeros((0, 5, 2), np.float32), np.zeros(0, np.float32)
    boxes, points, scores = np.concatenate(boxes), np.concatenate(points), np.concatenate(scores)
    keep = nms(boxes, scores)
    return boxes[keep], points[keep], scores[keep]


def similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """The rotation, scale and shift (3 x 3) that best carries the points
    ``src`` onto ``dst``, by least squares (Umeyama 1991): what skimage's
    SimilarityTransform does for InsightFace, without skimage."""
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    n, dim = src.shape
    src_mean, dst_mean = src.mean(axis=0), dst.mean(axis=0)
    src_c, dst_c = src - src_mean, dst - dst_mean
    cov = dst_c.T @ src_c / n
    d = np.ones(dim)
    if np.linalg.det(cov) < 0:
        d[-1] = -1
    u, s, vt = np.linalg.svd(cov)
    rotation = u @ np.diag(d) @ vt
    variance = src_c.var(axis=0).sum()
    scale = (s @ d) / variance if variance > 0 else 1.0
    out = np.eye(dim + 1)
    out[:dim, :dim] = rotation * scale
    out[:dim, dim] = dst_mean - scale * rotation @ src_mean
    return out


def align(img, points: np.ndarray):
    """The face whose landmarks are ``points`` as ArcFace wants to see it:
    112 pixels square, upright, eyes and mouth where its training put them.

    A face much larger than that is shrunk with a proper filter first. The
    affine warp samples four pixels per output pixel whatever the scale, and
    a 400-pixel face sampled that way is a face with its detail aliased
    into noise.
    """
    from PIL import Image

    carry = similarity(points, ARCFACE_POINTS)
    back = np.linalg.inv(carry)
    # Where the output square comes from in the photo.
    corners = back @ np.array([[0, ALIGN_EDGE, 0, ALIGN_EDGE], [0, 0, ALIGN_EDGE, ALIGN_EDGE], [1, 1, 1, 1]])
    x0, y0 = np.floor(corners[:2].min(axis=1)).astype(int)
    x1, y1 = np.ceil(corners[:2].max(axis=1)).astype(int) + 1
    # Outside the photo is black, as in InsightFace's own warp.
    region = img.crop((int(x0), int(y0), int(x1), int(y1)))
    zoom = np.sqrt(abs(np.linalg.det(carry[:2, :2])))     # output pixels per photo pixel
    sx = sy = 1.0
    if zoom < 0.5:
        size = (max(1, round(region.width * zoom * 2)), max(1, round(region.height * zoom * 2)))
        sx, sy = size[0] / region.width, size[1] / region.height
        region = region.resize(size, Image.Resampling.LANCZOS)
    to_region = np.array([[sx, 0, -x0 * sx], [0, sy, -y0 * sy], [0, 0, 1]]) @ back
    return region.transform(
        (ALIGN_EDGE, ALIGN_EDGE), Image.Transform.AFFINE,
        data=tuple(to_region[:2].reshape(-1)), resample=Image.Resampling.BILINEAR,
    )


def crop(img, box: np.ndarray):
    """The face as the info panel shows it: a FACE_EDGE square around it,
    with room for the hair and the chin, kept inside the photo where the
    photo is big enough."""
    from PIL import Image

    x1, y1, x2, y2 = (float(v) for v in box)
    side = min(max(x2 - x1, y2 - y1) * 1.6, float(max(img.size)))
    left = min(max((x1 + x2 - side) / 2, 0.0), max(0.0, img.width - side))
    top = min(max((y1 + y2 - side) / 2, 0.0), max(0.0, img.height - side))
    region = img.crop((round(left), round(top), round(left + side), round(top + side)))
    return region.resize((FACE_EDGE, FACE_EDGE), Image.Resampling.LANCZOS)


# --- the models ---------------------------------------------------------------------


def _unit(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)


class Models:
    """The two ONNX Runtime sessions."""

    def __init__(self, detector, recognizer):
        self.detector = detector
        self.recognizer = recognizer
        self.detector_input = detector.get_inputs()[0].name
        self.recognizer_input = recognizer.get_inputs()[0].name

    def detect(self, source: Source, threshold: float):
        """(boxes, landmarks, scores) in ``source.pixels`` coordinates."""
        blob = ((source.canvas.astype(np.float32) - 127.5) / 128.0).transpose(2, 0, 1)[None]
        outs = self.detector.run(None, {self.detector_input: blob})
        boxes, points, scores = read_detections(outs, threshold)
        return boxes / source.scale, points / source.scale, scores

    def embed(self, faces: list) -> np.ndarray:
        """One unit vector per aligned face. Each face is seen twice, once
        mirrored, and the two vectors summed: the flip test InsightFace's
        own benchmarks are measured with, for half a point of accuracy."""
        batch = np.stack([np.asarray(f, dtype=np.float32) for f in faces])
        both = np.concatenate([batch, batch[:, :, ::-1]])
        blob = ((both - 127.5) / 127.5).transpose(0, 3, 1, 2)
        outs = [
            self.recognizer.run(None, {self.recognizer_input: np.ascontiguousarray(blob[i:i + EMBED_BATCH])})[0]
            for i in range(0, len(blob), EMBED_BATCH)
        ]
        out = np.concatenate(outs).astype(np.float32)
        return _unit(out[:len(faces)] + out[len(faces):])


# The fake's six "people", by hue: red, yellow, green, cyan, blue, magenta.
FAKE_HUES = 6


def _fake_hues(img):
    """Per pixel: which of FAKE_HUES a strongly coloured pixel is, else -1."""
    hsv = np.asarray(img.convert("HSV"), dtype=np.int32)
    strong = (hsv[..., 1] > 150) & (hsv[..., 2] > 100)
    hue = ((hsv[..., 0] * FAKE_HUES + 128) // 256) % FAKE_HUES
    return np.where(strong, hue, -1)


class _Fake:
    def detect(self, source: Source, threshold: float):
        from PIL import Image

        hues = _fake_hues(Image.fromarray(source.pixels))
        boxes, points = [], []
        for hue in range(FAKE_HUES):
            ys, xs = np.nonzero(hues == hue)
            if xs.size < 64:
                continue
            box = np.array([xs.min(), ys.min(), xs.max() + 1, ys.max() + 1], dtype=np.float32)
            boxes.append(box)
            # Landmarks where a face that fills the box would have them.
            points.append(box[:2] + ARCFACE_POINTS / ALIGN_EDGE * (box[2:] - box[:2]))
        if not boxes:
            return np.zeros((0, 4), np.float32), np.zeros((0, 5, 2), np.float32), np.zeros(0, np.float32)
        return np.stack(boxes), np.stack(points), np.full(len(boxes), 0.99, dtype=np.float32)

    def embed(self, faces: list) -> np.ndarray:
        out = np.zeros((len(faces), FACE_DIM), dtype=np.float32)
        for i, face in enumerate(faces):
            counts = np.bincount(_fake_hues(face).reshape(-1) + 1, minlength=FAKE_HUES + 1)[1:]
            out[i, int(counts.argmax()) if counts.any() else FACE_DIM - 1] = 1.0
        return out


def download(settings: Settings) -> dict[str, Path]:
    """The two models, from the models cache (fetched on first use)."""
    from huggingface_hub import hf_hub_download

    cache = settings.models_path
    cache.mkdir(parents=True, exist_ok=True)
    return {
        name: Path(hf_hub_download(settings.faces_model, name, cache_dir=str(cache)))
        for name in (DETECTION_FILE, RECOGNITION_FILE)
    }


def build(settings: Settings, threads: int) -> Models:
    import onnxruntime as ort

    files = download(settings)

    def session(path: Path):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, threads)
        opts.inter_op_num_threads = 1
        return ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])

    return Models(session(files[DETECTION_FILE]), session(files[RECOGNITION_FILE]))


_lock = threading.Lock()
_models: dict[tuple[str, int], object] = {}


def threads_for(settings: Settings) -> int:
    """About half the worker count, like the NSFW classifier: a turn runs
    this between the process pool's batches, not instead of them."""
    return max(1, settings.workers // 2)


def model(settings: Settings):
    """The sessions, built once per process and model."""
    if FAKE:
        return _Fake()
    key = (settings.faces_model, threads_for(settings))
    with _lock:
        if key not in _models:
            _models[key] = build(settings, key[1])
        return _models[key]


# --- finding the faces in one photo ------------------------------------------------


@dataclass
class Found:
    box: np.ndarray         # x1, y1, x2, y2 in Source.pixels
    score: float
    aligned: object         # the ALIGN_EDGE square ArcFace is shown
    crop: object            # the FACE_EDGE square the info panel shows


def find(models, source: Source, threshold: float) -> list[Found]:
    """Every face worth keeping in one photo, largest first."""
    from PIL import Image

    boxes, points, scores = models.detect(source, threshold)
    height, width = source.pixels.shape[:2]
    keep = []
    for box, pts, score in zip(boxes, points, scores):
        box = np.array([np.clip(box[0], 0, width), np.clip(box[1], 0, height),
                        np.clip(box[2], 0, width), np.clip(box[3], 0, height)], dtype=np.float32)
        if min(box[2] - box[0], box[3] - box[1]) < MIN_FACE:
            continue
        keep.append((box, pts, float(score)))
    keep.sort(key=lambda k: (k[0][2] - k[0][0]) * (k[0][3] - k[0][1]), reverse=True)
    img = Image.fromarray(source.pixels)
    out = []
    for box, pts, score in keep[:MAX_FACES]:
        try:
            aligned = align(img, pts)
        except np.linalg.LinAlgError:
            continue            # landmarks on top of each other: not a face to measure
        out.append(Found(box, score, aligned, crop(img, box)))
    return out


# --- the stage ---------------------------------------------------------------------


def pending_query():
    # Not the original behind an edit: it is never listed, and the edit has
    # the same people in it. Should the edit go, the original comes back
    # here by itself (its faces_sig was never set).
    return select(Photo.id, Photo.sig, Photo.root, Photo.rel_path, Photo.name).where(
        Photo.kind == "photo",
        Photo.is_companion.is_(False),
        Photo.superseded_by.is_(None),
        Photo.thumb_sig == Photo.sig,
        Photo.faces_sig != Photo.sig,
    )


def run_batch(
    db: Session,
    settings: Settings,
    pool,
    only_ids: frozenset[int] | None = None,
    limit: int = BATCH,
    cooldown: stages.Cooldown | None = None,
) -> int:
    """Find the faces in up to ``limit`` photos, newest first. Returns how
    many were done. ``pool(fn, tasks, what)`` runs the decoding: the
    pipeline's process pool, which survives a decoder that crashes.

    Raises only when the models themselves fail (a download, a broken
    install): that is not any one photo's fault and must not count against
    them.
    """
    from .thumbs import _write_webp

    exclude = cooldown.ids() if cooldown else ()
    todo = db.execute(stages.newest(pending_query(), only_ids, limit, exclude)).all()
    if not todo:
        return 0
    models = model(settings)      # before any decoding: a broken model wastes none

    def failed(row, error) -> None:
        stages.failed(db, row.id, row.sig, "faces", error)
        if cooldown:
            cooldown.add(row.id)

    rows, tasks = {}, []
    for row in todo:
        src = original_path(row.root, row.rel_path)
        if src is None:
            failed(row, f"library root {row.root!r} is not configured")
            continue
        rows[row.id] = row
        tasks.append(DecodeTask(photo_id=row.id, src=str(src)))

    found: dict[int, tuple[Source, list[Found]]] = {}
    for photo_id, result in pool(decode, tasks, "face").items():
        if isinstance(result, BaseException):
            failed(rows[photo_id], result)
            continue
        try:
            found[photo_id] = (result, find(models, result, settings.faces_detect_score))
        except Exception as exc:  # noqa: BLE001 - one odd picture must not stop the stage
            failed(rows[photo_id], exc)
    if not found:
        db.commit()
        return 0

    everyone = [f for _, faces in found.values() for f in faces]
    vectors = models.embed([f.aligned for f in everyone]) if everyone else np.zeros((0, FACE_DIM))
    by_face = {id(f): v for f, v in zip(everyone, vectors)}

    name = model_name(settings)
    now = utcnow()
    values = []
    for photo_id, (source, faces) in found.items():
        row = rows[photo_id]
        height, width = source.pixels.shape[:2]
        for pos, face in enumerate(faces):
            # The crop first: once the row is there, the info panel asks for it.
            _write_webp(face.crop, str(face_path(row.sig, pos)))
            x1, y1, x2, y2 = (float(v) for v in face.box)
            values.append({
                "photo_id": photo_id, "sig": row.sig, "pos": pos, "model": name,
                "x": round(x1 / width, 5), "y": round(y1 / height, 5),
                "w": round((x2 - x1) / width, 5), "h": round((y2 - y1) / height, 5),
                "score": round(face.score, 4), "embedding": by_face[id(face)], "created_at": now,
            })
    db.execute(delete(Face).where(Face.photo_id.in_(list(found))))
    if values:
        db.execute(insert(Face), values)
    for photo_id in found:
        row = rows[photo_id]
        db.execute(update(Photo).where(Photo.id == photo_id, Photo.sig == row.sig)
                   .values(faces_sig=row.sig, updated_at=now))
    stages.cleared(db, list(found), "faces")
    db.commit()
    return len(found)
