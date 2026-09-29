"""The NSFW stage: how likely each picture is to be sexually explicit.

A dedicated classifier, not the search model: SigLIP can be asked about
nudity, and its answer is a similarity score that means something different
on every photo. ``AdamCodd/vit-base-nsfw-detector`` is a ViT fine-tuned for
exactly this question and answers with a probability. Its int8 ONNX export
(88 MB) runs on ONNX Runtime, which the search model already brings along,
and was faster than the fp32 one here while agreeing on every hit.

The input is the thumbnail, like the search stage's, and for a video every
frame of its storyboard, the highest score winning: a clip is as explicit as
its most explicit moment, and a frame from the first second says little
about the rest. Pictures that apps saved (``make = ''``: no camera wrote
them) are scored first: that is where such pictures arrive from, and the
blur in the grid should reach them before it reaches holiday photos.

``MEERPIC_NSFW_FAKE=1`` swaps the model for a deterministic stand-in with no
download: the share of strongly red pixels. A solid red image scores 1.0,
anything else about 0, which is all a test needs to tell the two apart.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from core.config import Settings
from core.media import story_path, thumb_path
from core.models import Photo
from core.timeutil import utcnow

from . import stages

FAKE = os.environ.get("MEERPIC_NSFW_FAKE", "").strip() not in ("", "0", "false")

MODEL_FILE = "onnx/model_int8.onnx"
CONFIG_FILE = "onnx/config.json"
PREPROCESSOR_FILE = "onnx/preprocessor_config.json"

BATCH = 16          # rows per turn, and images per inference call


def model_name(settings: Settings) -> str:
    return "fake" if FAKE else settings.nsfw_model


@dataclass(frozen=True)
class Prep:
    """What ``preprocessor_config.json`` asks for, as numbers."""

    width: int = 384
    height: int = 384
    resample: int = 2               # bilinear
    rescale: float = 1 / 255
    mean: tuple[float, float, float] = (0.5, 0.5, 0.5)
    std: tuple[float, float, float] = (0.5, 0.5, 0.5)

    @classmethod
    def from_config(cls, cfg: dict) -> Prep:
        size = cfg.get("size") or {}
        if isinstance(size, int):
            width = height = size
        else:
            width = int(size.get("width") or size.get("shortest_edge") or 384)
            height = int(size.get("height") or size.get("shortest_edge") or 384)
        mean, std = (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)
        if cfg.get("do_normalize", True):
            mean = tuple(float(x) for x in cfg.get("image_mean") or (0.5, 0.5, 0.5))
            std = tuple(float(x) for x in cfg.get("image_std") or (0.5, 0.5, 0.5))
        rescale = float(cfg.get("rescale_factor") or 1 / 255) if cfg.get("do_rescale", True) else 1.0
        # transformers stores the filter as PIL's own number (2 = bilinear).
        resample = int(cfg.get("resample", 2))
        return cls(width=width, height=height, resample=resample if 0 <= resample <= 5 else 2,
                   rescale=rescale, mean=mean, std=std)

    def tensor(self, images: list) -> np.ndarray:
        """NCHW float32, as the ViT's ``pixel_values`` wants it."""
        mean = np.asarray(self.mean, dtype=np.float32)
        std = np.asarray(self.std, dtype=np.float32)
        out = []
        for img in images:
            small = img.convert("RGB").resize((self.width, self.height), self.resample)
            a = np.asarray(small, dtype=np.float32) * self.rescale
            out.append(((a - mean) / std).transpose(2, 0, 1))
        return np.stack(out).astype(np.float32, copy=False)


def nsfw_index(config: dict) -> int:
    """Which output is "nsfw", by the model's own labels."""
    for key, label in (config.get("id2label") or {}).items():
        if str(label).strip().lower() == "nsfw":
            return int(key)
    raise ValueError("the model's config.json has no 'nsfw' label")


def softmax(logits: np.ndarray) -> np.ndarray:
    z = logits - logits.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def fake_score(img) -> float:
    a = np.asarray(img.convert("RGB"), dtype=np.int16)
    red = (a[..., 0] > 180) & (a[..., 1] < 90) & (a[..., 2] < 90)
    return float(red.mean()) if red.size else 0.0


class Model:
    """One ONNX Runtime session and the numbers around it."""

    def __init__(self, session, prep: Prep, index: int):
        self.session = session
        self.prep = prep
        self.index = index
        self.input = session.get_inputs()[0].name

    def score(self, images: list) -> list[float]:
        out: list[float] = []
        for i in range(0, len(images), BATCH):
            chunk = images[i:i + BATCH]
            logits = self.session.run(None, {self.input: self.prep.tensor(chunk)})[0]
            out += [float(p) for p in softmax(np.asarray(logits, dtype=np.float32))[:, self.index]]
        return out


class _Fake:
    def score(self, images: list) -> list[float]:
        return [fake_score(img) for img in images]


def download(settings: Settings) -> dict[str, Path]:
    """The three files, from the models cache (fetched on first use)."""
    from huggingface_hub import hf_hub_download

    cache = settings.models_path
    cache.mkdir(parents=True, exist_ok=True)
    return {
        name: Path(hf_hub_download(settings.nsfw_model, name, cache_dir=str(cache)))
        for name in (MODEL_FILE, CONFIG_FILE, PREPROCESSOR_FILE)
    }


def build(settings: Settings, threads: int) -> Model:
    import onnxruntime as ort

    files = download(settings)
    config = json.loads(files[CONFIG_FILE].read_text(encoding="utf-8"))
    prep = Prep.from_config(json.loads(files[PREPROCESSOR_FILE].read_text(encoding="utf-8")))
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = max(1, threads)
    opts.inter_op_num_threads = 1
    session = ort.InferenceSession(str(files[MODEL_FILE]), sess_options=opts,
                                   providers=["CPUExecutionProvider"])
    return Model(session, prep, nsfw_index(config))


_lock = threading.Lock()
_models: dict[tuple[str, int], object] = {}


def threads_for(settings: Settings) -> int:
    """About half the worker count: the thumbnail pool and ffmpeg want the
    rest, and a turn runs this between them, not instead of them."""
    return max(1, settings.workers // 2)


def model(settings: Settings):
    """The session, built once per process and model."""
    if FAKE:
        return _Fake()
    key = (settings.nsfw_model, threads_for(settings))
    with _lock:
        if key not in _models:
            _models[key] = build(settings, key[1])
        return _models[key]


# --- the stage -------------------------------------------------------------------


def pending_query():
    return select(Photo.id, Photo.sig, Photo.kind, Photo.story_sig, Photo.story_frames).where(
        Photo.thumb_sig == Photo.sig,
        Photo.is_companion.is_(False),
        Photo.nsfw_sig != Photo.sig,
        # A video waits for its storyboard, which is what it is scored on;
        # scored from its thumbnail first, it would never be looked at again.
        or_(Photo.kind != "video", Photo.story_sig == Photo.sig),
    )


def saved_first():
    """Pictures no camera wrote: what apps and browsers saved."""
    return and_(Photo.kind == "photo", Photo.make == "")


def frames_of(strip, count: int) -> list:
    width = strip.width // count if count > 0 else 0
    if width <= 0:
        return [strip]
    return [strip.crop((i * width, 0, (i + 1) * width, strip.height)) for i in range(count)]


def run_batch(
    db: Session,
    settings: Settings,
    only_ids: frozenset[int] | None = None,
    limit: int = BATCH,
    cooldown: stages.Cooldown | None = None,
) -> int:
    """Score up to ``limit`` rows, apps' pictures first, newest first within
    each. Returns how many were done.

    Raises only when the model itself fails (a download, a broken install):
    that is not any one photo's fault and must not count against them.
    """
    from PIL import Image

    exclude = cooldown.ids() if cooldown else ()
    todo = db.execute(stages.newest(pending_query(), only_ids, limit, exclude, first=saved_first())).all()
    if not todo:
        return 0

    images: list = []
    spans: list[tuple[object, int, int]] = []      # row, first image, how many
    lost, lost_story = [], []
    for row in todo:
        try:
            pictures = []
            if row.kind == "video" and row.story_frames > 0:
                strip = story_path(row.sig)
                if not strip.is_file():
                    # The cache was cleared: the storyboard is drawn again,
                    # and the video scored on it then.
                    lost_story.append(row.id)
                    continue
                with Image.open(strip) as img:
                    pictures = frames_of(img.convert("RGB"), row.story_frames)
            if not pictures:
                with Image.open(thumb_path(row.sig)) as img:
                    pictures = [img.convert("RGB")]
        except FileNotFoundError:
            lost.append(row.id)
            continue
        except Exception as exc:  # noqa: BLE001 - a truncated webp: recorded, and drawn again
            stages.failed(db, row.id, row.sig, "nsfw", exc)
            if cooldown:
                cooldown.add(row.id)
            continue
        spans.append((row, len(images), len(pictures)))
        images += pictures
    if lost:
        # The cache was cleared under us: the thumbnail stage draws it again
        # and this one comes back to it afterwards.
        db.execute(update(Photo).where(Photo.id.in_(lost)).values(thumb_sig=""))
    if lost_story:
        db.execute(update(Photo).where(Photo.id.in_(lost_story)).values(story_sig=""))
    if not spans:
        db.commit()
        return 0

    scores = model(settings).score(images)
    now = utcnow()
    for row, start, count in spans:
        value = round(max(scores[start:start + count]), 4)
        db.execute(update(Photo).where(Photo.id == row.id, Photo.sig == row.sig)
                   .values(nsfw=value, nsfw_sig=row.sig, updated_at=now))
    stages.cleared(db, [row.id for row, _, _ in spans], "nsfw")
    db.commit()
    return len(spans)
