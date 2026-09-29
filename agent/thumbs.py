"""Thumbnails: one small WEBP per photo and per video, for the grid.

Runs in a process pool (decoding a 12 MP HEIC is ~0.2 s of pure CPU, and a
library is tens of thousands of them), so the entry point is a top-level
function taking one picklable task, and nothing here reads the settings or
the database: the parent works out every path and flag and hands them over.
That also keeps a worker's imports to Pillow and the standard library.

Size rules (core.media): the short edge becomes ``THUMB_EDGE``, never
upscaled, and the long edge stops at ``THUMB_MAX_LONG`` so a panorama does not
become a 400 x 6000 strip. The grid lays tiles out from the size the photo
*displays* at, so every render also reports that size, measured from the
decoded image after rotation rather than trusted from the metadata.
"""

from __future__ import annotations

import io
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps

from core.media import THUMB_EDGE, THUMB_MAX_LONG

from . import video

pillow_heif.register_heif_opener()

# Pillow refuses images over ~179 MP as a decompression-bomb guard. These are
# the user's own files, and a stitched panorama can be bigger than that; the
# limit here is about memory, not trust.
Image.MAX_IMAGE_PIXELS = 400_000_000

WEBP_QUALITY = 80
FRAME_TIMEOUT = 120.0


class ThumbError(RuntimeError):
    pass


@dataclass(frozen=True)
class ThumbTask:
    photo_id: int
    src: str
    dest: str
    kind: str                   # photo | video
    duration: float | None = None
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    hevc: bool = True           # this ffmpeg decodes HEVC
    tonemap: bool = True        # this ffmpeg has zscale + tonemap


def target_size(width: int, height: int) -> tuple[int, int]:
    """The thumbnail's size for a displayed width x height."""
    short, long = min(width, height), max(width, height)
    scale = min(1.0, THUMB_EDGE / short, THUMB_MAX_LONG / long)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _flatten(img: Image.Image) -> Image.Image:
    """Any mode to RGB, transparent areas onto white (a screenshot's
    rounded corners, a PNG logo): black corners look like damage."""
    if img.mode in ("I;16", "I;16L", "I;16B", "I;16N", "I"):
        img = img.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    if img.mode == "P" and "transparency" in img.info:
        img = img.convert("RGBA")
    if img.mode in ("RGBA", "LA", "PA", "RGBa", "La"):
        rgba = img.convert("RGBA")
        base = Image.new("RGB", rgba.size, (255, 255, 255))
        base.paste(rgba, mask=rgba.getchannel("A"))
        return base
    return img if img.mode == "RGB" else img.convert("RGB")


def _write_webp(img: Image.Image, dest: str, quality: int = WEBP_QUALITY) -> None:
    """Atomically: a reader never sees half a file, and a crash leaves none."""
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        img.save(tmp, "WEBP", quality=quality)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def render_image(src: str, dest: str) -> tuple[int, int]:
    with Image.open(src) as img:
        img.seek(0)             # the first frame of an animated GIF/WEBP
        stored = img.size
        # pillow-heif has already applied the HEIF transforms and reset the
        # EXIF orientation to 1, so for every format EXIF says what is left.
        orientation = img.getexif().get(0x0112, 1)
        turned = orientation in (5, 6, 7, 8)
        shown = (stored[1], stored[0]) if turned else stored
        tw, th = target_size(*shown)
        if img.format == "JPEG":
            # Let libjpeg decode at 1/2, 1/4 or 1/8 scale: 5x faster on a
            # camera JPEG, and still at least the size asked for.
            img.draft("RGB", (th, tw) if turned else (tw, th))
        img = ImageOps.exif_transpose(img)
        img = _flatten(img)
        if img.size != (tw, th):
            img = img.resize((tw, th), Image.Resampling.LANCZOS, reducing_gap=3.0)
        _write_webp(img, dest)
    return shown


def _frame(task: ThumbTask, at: float, size: tuple[int, int], hdr: bool) -> bytes:
    width, height = size
    if hdr and task.tonemap:
        # zscale refuses a 4:2:0 frame with an odd side ("image dimensions
        # must be divisible by subsampling factor"), and target_size gives
        # odd sides freely: a portrait 4K iPhone video is 400x711. Every HDR
        # video of that shape failed its thumbnail. The frame is one pixel
        # larger here and resized to the exact size by the caller.
        width, height = width + width % 2, height + height % 2
    chain = [f"scale={width}:{height}"]
    if hdr and task.tonemap:
        chain.append(video.TONEMAP)
    chain.append("format=rgb24")
    cmd = [task.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if at > 0:
        cmd += ["-ss", f"{at:.3f}"]
    cmd += ["-i", task.src, "-frames:v", "1", "-an", "-sn", "-dn",
            "-vf", ",".join(chain), "-f", "image2pipe", "-c:v", "png", "-"]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=FRAME_TIMEOUT, check=False)
    except FileNotFoundError as exc:
        raise ThumbError(f"ffmpeg not found: {task.ffmpeg}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ThumbError("ffmpeg timed out drawing a frame") from exc
    if proc.returncode != 0 and not proc.stdout:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ThumbError(f"ffmpeg: {err[-1] if err else 'exit ' + str(proc.returncode)}")
    return proc.stdout


def render_video(task: ThumbTask) -> tuple[int, int, float | None]:
    info = video.stream_info(video.probe(task.src, task.ffprobe))
    if info.codec == "hevc" and not task.hevc:
        raise ThumbError(video.HEVC_MISSING)
    shown = (info.width, info.height)
    size = target_size(*shown)
    duration = task.duration or info.duration
    at = min(1.0, duration / 3) if duration else 0.0
    data = _frame(task, at, size, info.hdr)
    if not data and at > 0:
        # Seeking past a very short clip's only keyframe yields nothing.
        data = _frame(task, 0.0, size, info.hdr)
    if not data:
        raise ThumbError("ffmpeg produced no frame")
    with Image.open(io.BytesIO(data)) as img:
        frame = _flatten(img)
        if frame.size != size:
            frame = frame.resize(size, Image.Resampling.LANCZOS)
        _write_webp(frame, task.dest)
    return shown[0], shown[1], info.duration


def render(task: ThumbTask) -> tuple[int, int, float | None]:
    """Draw ``task.dest``; return the source's displayed width and height,
    and for a video the duration ffprobe found (None for a photo).

    The process pool's entry point: top-level, one picklable argument.
    """
    if task.kind == "video":
        return render_video(task)
    w, h = render_image(task.src, task.dest)
    return w, h, None
