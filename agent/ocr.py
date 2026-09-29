"""The text stage: the words in each photo and video, for ``text:``.

A receipt, a letter from the council, a sign on a door, the screenshot of a
booking: what makes these findable is what they say, and the search model
only knows what they show. PP-OCRv6 reads them (agent/ocr_engine.py): a
detector finds the lines of text, a recogniser reads each one. The result
is stored as lines (``photo_texts``), and ``text:`` is a case-insensitive
substring search over them (app/query.py).

Everything runs in the process pool, decoding and both models: the models
are 23 MB, small enough for every worker to hold its own, so what crosses
back to the agent is a few lines of text rather than a 12 MP image. A photo
is read at up to ``SOURCE_EDGE`` pixels (the detector itself sees a million
of them; the recogniser gets its crops from the full decode, where small
print still has pixels). Measured on a real library with the machine busy,
a photo without text costs about 0.2 s of detection on top of its decoding,
and one with text about 30 ms per line more.

A video is read on ``VIDEO_FRAMES`` frames spread over the clip, the ones a
storyboard is made of, at ``VIDEO_EDGE`` pixels (agent/story.py grabs them).
The same sign in eight frames is one line: lines are kept once each, the
most confident reading winning. Text that is on screen for less than a
tenth of the clip may be missed; a caption, a sign, a slide is not.

Screenshots and pictures apps saved (``make = ''``: no camera wrote them)
are read first: that is where text is.

``MEERPIC_OCR_FAKE=1`` swaps the models for a stand-in with no download: a
picture mostly of one strong colour "says" that colour's name (``RED``).
Decoding and frame grabbing run for real either way.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from sqlalchemy import and_, delete, insert, or_, select, update
from sqlalchemy.orm import Session

from core.config import Settings
from core.media import original_path
from core.models import Photo, PhotoText
from core.timeutil import utcnow

from . import stages, video

FAKE = os.environ.get("MEERPIC_OCR_FAKE", "").strip() not in ("", "0", "false")

MODEL = "pp-ocrv6-det-tiny+rec-small"
SOURCE_EDGE = 2048      # px, the long edge a photo is decoded at
VIDEO_EDGE = 1280       # px, the long edge of a video frame
VIDEO_FRAMES = 10
MAX_LINES = 2000        # per file: a scanned phone book stops somewhere


def model_name() -> str:
    return "fake" if FAKE else MODEL


# --- in the process pool -----------------------------------------------------------


@dataclass(frozen=True)
class ReadTask:
    photo_id: int
    src: str
    kind: str                           # photo | video
    models: tuple[str, str] = ("", "")  # detector, recogniser; empty when FAKE
    duration: float | None = None
    width: int | None = None            # as displayed, videos
    height: int | None = None
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    hevc: bool = True
    tonemap: bool = True


@dataclass
class Found:
    """What a file says: its lines, in reading order, as stored."""

    lines: list[dict] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(line["t"] for line in self.lines)


FAKE_COLOURS = {"RED": 0, "GREEN": 1, "BLUE": 2}


@dataclass(frozen=True)
class _FakeLine:
    text: str
    score: float
    box: tuple[float, float, float, float]


class _Fake:
    def read(self, img) -> list[_FakeLine]:
        a = np.asarray(img.convert("RGB"), dtype=np.int16)
        if not a.size:
            return []
        for name, channel in FAKE_COLOURS.items():
            others = [c for c in range(3) if c != channel]
            strong = (a[..., channel] > 180) & (a[..., others[0]] < 90) & (a[..., others[1]] < 90)
            if strong.mean() > 0.5:
                return [_FakeLine(name, 0.99, (0.0, 0.0, 1.0, 1.0))]
        return []


_engine = None
_engine_key: tuple[str, str] | None = None


def engine(models: tuple[str, str]):
    """This worker's reader, built on first use. One thread: the pool runs
    one file per core already."""
    global _engine, _engine_key
    if FAKE:
        return _Fake()
    if _engine is None or _engine_key != models:
        import cv2

        from .ocr_engine import Engine

        cv2.setNumThreads(1)
        _engine = Engine(Path(models[0]), Path(models[1]), threads=1)
        _engine_key = models
    return _engine


def load(src: str, edge: int = SOURCE_EDGE):
    """The photo upright and in RGB, the long edge at most ``edge``."""
    from PIL import Image, ImageOps

    # thumbs registers the HEIF opener, and flattens a transparent PNG onto
    # white, which is also what a screenshot's text is written on.
    from .thumbs import _flatten

    with Image.open(src) as img:
        img.seek(0)             # the first frame of an animated GIF/WEBP
        orientation = img.getexif().get(0x0112, 1)
        turned = orientation in (5, 6, 7, 8)
        shown = (img.height, img.width) if turned else img.size
        scale = min(1.0, edge / max(shown))
        size = (max(1, round(shown[0] * scale)), max(1, round(shown[1] * scale)))
        if img.format == "JPEG":
            # libjpeg decodes at 1/2, 1/4 or 1/8 for less than the whole cost.
            img.draft("RGB", (size[1], size[0]) if turned else size)
        img = _flatten(ImageOps.exif_transpose(img))
        if img.size != size:
            img = img.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
        img.load()
        return img


def frame_height(width: int | None, height: int | None, edge: int = VIDEO_EDGE) -> int:
    """The frame height that makes a frame's long edge ``edge``, even
    (zscale refuses odd sides). 720 when the size is not known yet."""
    if not width or not height:
        return 720
    h = edge if height >= width else edge * height / width
    return max(2, round(h / 2) * 2)


def frames(task: ReadTask) -> list:
    from .story import StoryTask, grab

    return grab(StoryTask(
        photo_id=task.photo_id, src=task.src, dest="", duration=task.duration,
        ffmpeg=task.ffmpeg, ffprobe=task.ffprobe, hevc=task.hevc, tonemap=task.tonemap,
        frames=VIDEO_FRAMES, height=frame_height(task.width, task.height),
    ))


def _key(text: str) -> str:
    return " ".join(text.split()).casefold()


def read(task: ReadTask) -> Found:
    """The process pool's entry point: top-level, one picklable argument."""
    reader = engine(task.models)
    if task.kind != "video":
        lines = [
            {"t": line.text, "s": round(float(line.score), 3),
             "b": [round(float(v), 4) for v in line.box]}
            for line in reader.read(load(task.src))
        ]
        return Found(lines[:MAX_LINES])
    # Once each, in the order they first appear, at their most confident.
    best: dict[str, dict] = {}
    for frame in frames(task):
        for line in reader.read(frame):
            key = _key(line.text)
            score = round(float(line.score), 3)
            if key not in best:
                best[key] = {"t": line.text, "s": score}
            elif score > best[key]["s"]:
                best[key].update(t=line.text, s=score)
    return Found(list(best.values())[:MAX_LINES])


# --- in the agent ------------------------------------------------------------------


_lock = threading.Lock()
_paths: dict[Path, tuple[str, str]] = {}


def models(settings: Settings) -> tuple[str, str]:
    """The two model files, fetched into the models cache on first use."""
    if FAKE:
        return ("", "")
    from . import ocr_engine

    dest = settings.models_path / "ocr"
    with _lock:
        if dest not in _paths:
            files = ocr_engine.download(dest)
            # Built once here and dropped: the workers build their own, and a
            # broken install (a wheel that will not import, a model ONNX
            # Runtime cannot load) should stop the stage, not fail every
            # photo in turn until each is parked.
            ocr_engine.Engine(files[ocr_engine.DET_FILE], files[ocr_engine.REC_FILE])
            _paths[dest] = (str(files[ocr_engine.DET_FILE]), str(files[ocr_engine.REC_FILE]))
        return _paths[dest]


def pending_query(settings: Settings):
    # Not the original behind an edit (never listed, and the edit says the
    # same), nor a Live Photo's motion (its still is read instead). The
    # thumbnail first: a file that cannot be drawn cannot be read either,
    # and a video's size and codec are known from then on.
    query = select(
        Photo.id, Photo.sig, Photo.root, Photo.rel_path, Photo.name, Photo.kind,
        Photo.duration, Photo.width, Photo.height, Photo.video_codec,
    ).where(
        Photo.is_companion.is_(False),
        Photo.superseded_by.is_(None),
        Photo.thumb_sig == Photo.sig,
        Photo.ocr_sig != Photo.sig,
    )
    if not settings.ocr_videos:
        query = query.where(Photo.kind == "photo")
    return query


def text_first():
    """Screenshots and what apps saved: no camera wrote them."""
    return and_(Photo.kind == "photo", or_(Photo.make == "", Photo.is_screenshot.is_(True)))


def run_batch(
    db: Session,
    settings: Settings,
    pool,
    caps: video.Caps,
    only_ids: frozenset[int] | None = None,
    limit: int = 16,
    cooldown: stages.Cooldown | None = None,
) -> int:
    """Read up to ``limit`` files, screenshots and saved pictures first,
    newest first within each. Returns how many were done. ``pool(fn,
    tasks, what)`` runs the reading: the pipeline's process pool, which
    survives a decoder that crashes.

    Raises only when the models cannot be had (a download): that is not any
    one photo's fault and must not count against them.
    """
    exclude = cooldown.ids() if cooldown else ()
    todo = db.execute(stages.newest(pending_query(settings), only_ids, limit, exclude,
                                    first=text_first())).all()
    if not todo:
        return 0
    files = models(settings)      # before any decoding: a missing model wastes none

    def failed(row, error) -> None:
        stages.failed(db, row.id, row.sig, "ocr", error)
        if cooldown:
            cooldown.add(row.id)

    rows, tasks = {}, []
    for row in todo:
        src = original_path(row.root, row.rel_path)
        if src is None:
            failed(row, f"library root {row.root!r} is not configured")
            continue
        if row.kind == "video" and row.video_codec == "hevc" and not caps.hevc:
            failed(row, video.HEVC_MISSING)
            continue
        rows[row.id] = row
        tasks.append(ReadTask(
            photo_id=row.id, src=str(src), kind=row.kind, models=files,
            duration=row.duration, width=row.width, height=row.height,
            ffmpeg=settings.ffmpeg, ffprobe=settings.ffprobe, hevc=caps.hevc, tonemap=caps.tonemap,
        ))

    done: dict[int, Found] = {}
    for photo_id, result in pool(read, tasks, "text").items():
        if isinstance(result, BaseException):
            failed(rows[photo_id], result)
        else:
            done[photo_id] = result
    if not done:
        db.commit()
        return 0

    name = model_name()
    now = utcnow()
    db.execute(delete(PhotoText).where(PhotoText.photo_id.in_(list(done))))
    values = [
        {"photo_id": photo_id, "sig": rows[photo_id].sig, "model": name,
         "text": found.text, "lines": found.lines, "created_at": now}
        for photo_id, found in done.items() if found.lines
    ]
    if values:
        db.execute(insert(PhotoText), values)
    for photo_id in done:
        row = rows[photo_id]
        db.execute(update(Photo).where(Photo.id == photo_id, Photo.sig == row.sig)
                   .values(ocr_sig=row.sig, updated_at=now))
    stages.cleared(db, list(done), "ocr")
    db.commit()
    return len(done)
