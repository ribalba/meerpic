"""Photos and words as vectors in one space: the whole of "find similar" and
"search for cows".

A multimodal model (SigLIP 2 by default, CLIP as the light alternative) maps an
image and a sentence to vectors that point the same way when the sentence
describes the image. Two queries fall out of that and nothing else:

* **similar**: the photos whose vectors are nearest this photo's;
* **search**: the photos whose vectors are nearest the vector of the words.

Both are one ``ORDER BY embedding <=> :v`` against a pgvector index, which is
why the vectors live in Postgres beside everything else instead of in a second
store that would have to be kept in step with it.

The agent embeds images; the server embeds the words of a query. Both go
through this module so that the two halves cannot disagree about the model,
its dimension, or whether the vectors are normalised. They always are: stored
vectors have unit length, so cosine distance is ``1 - dot`` and a score is
comparable across queries.

Why these models and not a bigger one: measured on a real 25,000-photo
library on a 20-core laptop without a GPU, SigLIP 2 base costs ~110 ms per
photo (45 minutes for the first index, then only what is new) and ~60 ms per
query, and it put actual sheep ahead of fields for "sheep" where CLIP B/32 did
not. It also reads German, French and the rest, where CLIP reads English only.
CLIP B/32 is six times faster and a quarter of the download, for a machine
where that matters more.

``MEERPIC_EMBED_FAKE=1`` swaps the model for a deterministic stand-in that
needs no download (colour layout for images, colour words for text), so the
test suite can exercise search and similarity end to end on any machine.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import get_settings


@dataclass(frozen=True)
class ModelInfo:
    image: str          # fastembed ImageEmbedding name
    text: str           # fastembed TextEmbedding name
    dim: int
    # Below this cosine similarity a text match is noise, and below the second
    # an image is not "similar" in any sense a person would agree with. Measured
    # per model: the scales differ by a factor of three between SigLIP and CLIP.
    min_score: float
    similar_min_score: float
    note: str


MODELS: dict[str, ModelInfo] = {
    "google/siglip2-base-patch16-224": ModelInfo(
        image="google/siglip2-base-patch16-224",
        text="google/siglip2-base-patch16-224",
        dim=768,
        min_score=0.06,
        similar_min_score=0.55,
        note="multilingual, best results; ~110 ms per photo, 1.5 GB download",
    ),
    "Qdrant/clip-ViT-B-32": ModelInfo(
        image="Qdrant/clip-ViT-B-32-vision",
        text="Qdrant/clip-ViT-B-32-text",
        dim=512,
        min_score=0.23,
        similar_min_score=0.75,
        note="English only, fast; ~20 ms per photo, 0.6 GB download",
    ),
    "jinaai/jina-clip-v1": ModelInfo(
        image="jinaai/jina-clip-v1",
        text="jinaai/jina-clip-v1",
        dim=768,
        min_score=0.18,
        similar_min_score=0.70,
        note="English only; between the two above",
    ),
}

FAKE = os.environ.get("MEERPIC_EMBED_FAKE", "").strip() not in ("", "0", "false")


def model_info(name: str | None = None) -> ModelInfo:
    name = name or get_settings().search_model
    info = MODELS.get(name)
    if info is None:
        known = ", ".join(sorted(MODELS))
        raise SystemExit(f"meerpic: unknown search.model {name!r}; known models: {known}")
    return info


def model_name() -> str:
    """What is written into ``photo_embeddings.model``: the configured name,
    or "fake" under the stand-in, so a test database can never pass for a
    real index."""
    return "fake" if FAKE else get_settings().search_model


def dim() -> int:
    return model_info().dim


def min_score() -> float:
    s = get_settings().search_min_score
    return s if s > 0 else (0.2 if FAKE else model_info().min_score)


def similar_min_score() -> float:
    s = get_settings().similar_min_score
    return s if s > 0 else (0.5 if FAKE else model_info().similar_min_score)


def _normalise(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


# --- the stand-in ------------------------------------------------------------

_COLOURS = {
    "red": (255, 0, 0), "rot": (255, 0, 0),
    "green": (0, 160, 0), "grün": (0, 160, 0),
    "blue": (0, 0, 255), "blau": (0, 0, 255),
    "yellow": (255, 230, 0), "gelb": (255, 230, 0),
    "white": (255, 255, 255), "weiß": (255, 255, 255),
    "black": (0, 0, 0), "schwarz": (0, 0, 0),
}


def _fake_image(img) -> np.ndarray:
    """A 4x4 colour layout, centred, padded out to the model's dimension."""
    from PIL import Image

    small = img.convert("RGB").resize((4, 4), Image.Resampling.BOX)
    v = np.asarray(small, dtype=np.float32).reshape(-1) / 255.0
    v = v - v.mean()
    out = np.zeros(dim(), dtype=np.float32)
    out[: v.size] = v
    if not out.any():            # a perfectly flat grey: still a direction
        out[0] = 1.0
    return _normalise(out)


def _fake_text(text: str) -> np.ndarray:
    from PIL import Image

    words = text.lower().split()
    for word in words:
        if word in _COLOURS:
            return _fake_image(Image.new("RGB", (4, 4), _COLOURS[word]))
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
    return _normalise(np.random.default_rng(seed).standard_normal(dim()))


# --- the real thing ----------------------------------------------------------


class _Lazy:
    """One model per process, loaded on first use, shared across threads.

    Loading SigLIP's text half takes half a minute and 1.5 GB of memory the
    first time (the download) and several seconds every time after, so it is
    done once and kept. ONNX Runtime sessions are safe to call from several
    threads at once; only the construction needs the lock.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._image = None
        self._text = None

    def _kwargs(self, threads: int | None) -> dict:
        cache = get_settings().models_path
        cache.mkdir(parents=True, exist_ok=True)
        kw: dict = {"cache_dir": str(cache)}
        if threads:
            kw["threads"] = threads
        return kw

    def image(self, threads: int | None = None):
        with self._lock:
            if self._image is None:
                from fastembed import ImageEmbedding

                self._image = ImageEmbedding(model_info().image, **self._kwargs(threads))
            return self._image

    def text(self, threads: int | None = None):
        with self._lock:
            if self._text is None:
                from fastembed import TextEmbedding

                self._text = TextEmbedding(model_info().text, **self._kwargs(threads))
            return self._text

    @property
    def text_loaded(self) -> bool:
        return FAKE or self._text is not None


_models = _Lazy()


def embed_images(images: Iterable, batch_size: int = 16, threads: int | None = None) -> list[np.ndarray]:
    """Unit vectors for PIL images or image paths, in the order given."""
    from PIL import Image

    items = list(images)
    if not items:
        return []
    if FAKE:
        out = []
        for item in items:
            img = item if isinstance(item, Image.Image) else Image.open(item)
            out.append(_fake_image(img))
        return out
    model = _models.image(threads)
    return [_normalise(v) for v in model.embed(items, batch_size=batch_size)]


def embed_text(text: str) -> np.ndarray:
    """The unit vector for a query."""
    text = (text or "").strip()
    if FAKE:
        return _fake_text(text)
    return _normalise(next(iter(_models.text().embed([text]))))


def warm_text(threads: int | None = None) -> None:
    """Load the text model now, so the first search does not pay for it."""
    if not FAKE:
        _models.text(threads)


def text_ready() -> bool:
    return _models.text_loaded


def to_sql(v: np.ndarray) -> str:
    """A vector as the pgvector literal, for a bound parameter cast ::vector."""
    return "[" + ",".join(f"{x:.6f}" for x in np.asarray(v, dtype=np.float32)) + "]"


def cache_ready() -> bool:
    """Whether the model files are already on disk (so nothing will download)."""
    if FAKE:
        return True
    path: Path = get_settings().models_path
    return path.is_dir() and any(path.iterdir())
