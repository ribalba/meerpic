"""The few images the server draws itself.

The agent makes every thumbnail ahead of time; the server only fills the gaps
that the browser cannot wait for or cannot draw:

* a **thumbnail on the fly**, for a photo the agent has not reached yet, so a
  photo that has just arrived shows in the grid at once and not a minute later;
* a **display JPEG**, for the formats no browser draws (HEIC above all: every
  iPhone photo since 2017) and for JPEGs too big to hand a browser whole;
* the **export**: what a drag into a mail, "Download" and "Copy" hand over.

One rule for all three: orientation is applied to the pixels, because the
thing on the other end (a browser's canvas, a mail client, the clipboard) may
or may not read the tag, and a portrait photo that arrives on its side is the
bug everyone has seen. pillow-heif already applies a HEIF's own rotation and
resets its EXIF tag to 1, so ``exif_transpose`` is a no-op there and the same
code serves both.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps

from core.media import DISPLAY_EDGE, THUMB_EDGE, THUMB_MAX_LONG

pillow_heif.register_heif_opener()

ORIENTATION = 0x0112
GPS_IFD = 0x8825

THUMB_QUALITY = 80
DISPLAY_QUALITY = 88
WHITE = (255, 255, 255)


class ImageError(Exception):
    """A file that is in the library but cannot be decoded as an image."""


def atomic_write(dest: Path, data: bytes) -> None:
    """Write ``dest`` so that a reader sees the old file or the new one, never
    half of one. Two requests racing to fill the same cache entry both win."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# What Pillow and pillow-heif raise for a file they cannot read: a truncated
# download, a format they do not know, a bomb.
_DECODE_ERRORS = (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError)


@contextmanager
def _opened(path: Path) -> Iterator[Image.Image]:
    """The image at ``path``, with every decoding failure as ImageError.

    Opening reads only the header; the pixels are decoded later, inside the
    block, which is where a truncated file actually fails.
    """
    try:
        with Image.open(path) as img:
            yield img
    except _DECODE_ERRORS as exc:
        raise ImageError(f"{path.name}: {exc}") from exc


def flatten(img: Image.Image) -> Image.Image:
    """RGB, with anything transparent composited onto white.

    White rather than black because the common transparent image in a photo
    library is a screenshot or a sticker, and JPEG's default of black behind a
    black-text screenshot is an all-black rectangle.
    """
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    if has_alpha:
        rgba = img.convert("RGBA")
        out = Image.new("RGB", rgba.size, WHITE)
        out.paste(rgba, mask=rgba.getchannel("A"))
        return out
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _icc(img: Image.Image) -> bytes | None:
    # iPhones shoot in Display P3; without its profile the colours of every
    # converted photo come out flat. A CMYK profile on an image converted to
    # RGB would be worse than none.
    if img.mode == "CMYK":
        return None
    return img.info.get("icc_profile") or None


def thumb_size(w: int, h: int) -> tuple[int, int]:
    """The agent's rule: short edge THUMB_EDGE, long edge at most
    THUMB_MAX_LONG, never larger than the source."""
    short, long = min(w, h), max(w, h)
    if short <= 0:
        return max(1, w), max(1, h)
    scale = min(1.0, THUMB_EDGE / short, THUMB_MAX_LONG / long)
    return max(1, round(w * scale)), max(1, round(h * scale))


def fit_long_edge(w: int, h: int, edge: int) -> tuple[int, int]:
    """(w, h) scaled so the long edge is at most ``edge``, never up."""
    long = max(w, h)
    if edge <= 0 or long <= edge:
        return w, h
    scale = edge / long
    return max(1, round(w * scale)), max(1, round(h * scale))


def _draft(img: Image.Image, target: tuple[int, int]) -> None:
    # A JPEG can be decoded at 1/2, 1/4 or 1/8 of its size for nearly free,
    # which turns a 48-megapixel decode for a 400-pixel thumbnail into a
    # 3-megapixel one. The final resize below still does the real work.
    if img.format == "JPEG":
        img.draft("RGB", target)


def _oriented(img: Image.Image) -> Image.Image:
    return ImageOps.exif_transpose(img)


def render_thumb(path: Path) -> bytes:
    """A WEBP thumbnail by the agent's rules, made in memory and not kept."""
    with _opened(path) as img:
        icc = _icc(img)
        # Ask for the drafted size in stored orientation; any rotation swaps
        # both axes and the target is only a lower bound anyway.
        w, h = img.size
        _draft(img, thumb_size(w, h))
        out = flatten(_oriented(img))
        out = out.resize(thumb_size(*out.size), Image.Resampling.LANCZOS)
        buf = BytesIO()
        out.save(buf, "WEBP", quality=THUMB_QUALITY, icc_profile=icc)
        return buf.getvalue()


def render_display(path: Path, dest: Path) -> None:
    """A full-screen JPEG of ``path`` at ``dest`` (long edge DISPLAY_EDGE)."""
    with _opened(path) as img:
        icc = _icc(img)
        _draft(img, fit_long_edge(*img.size, DISPLAY_EDGE))
        out = flatten(_oriented(img))
        size = fit_long_edge(*out.size, DISPLAY_EDGE)
        if size != out.size:
            out = out.resize(size, Image.Resampling.LANCZOS)
        buf = BytesIO()
        out.save(buf, "JPEG", quality=DISPLAY_QUALITY, icc_profile=icc, progressive=True)
    atomic_write(dest, buf.getvalue())


def export_photo(path: Path, ext: str, *, max_edge: int, quality: int, strip_gps: bool) -> bytes | None:
    """The mail-friendly JPEG of a photo, or None for "send the original".

    None exactly when the original is a JPEG or PNG and there is nothing to
    change: no size limit it exceeds and no location to remove. A JPEG whose
    EXIF says "rotate me" is sent as it is too: every mail client, browser and
    phone honours the tag, and rotating the pixels would mean a lossy
    re-encode of a photo the user asked to send, not to alter.

    Otherwise a JPEG at ``quality``: orientation applied and its tag reset to
    1, EXIF otherwise kept (the date and the camera are part of a photo),
    except the GPS block when ``strip_gps``. XMP is not carried over; Pillow
    drops it, and Apple's copy of the location lives in the EXIF anyway.
    """
    with _opened(path) as img:
        too_big = max_edge > 0 and max(img.size) > max_edge
        if ext in ("jpg", "jpeg", "png") and not too_big and not strip_gps:
            return None
        try:
            exif = img.getexif()
        except _DECODE_ERRORS:
            exif = Image.Exif()
        icc = _icc(img)
        if too_big:
            _draft(img, fit_long_edge(*img.size, max_edge))
        out = flatten(_oriented(img))
        if too_big:
            out = out.resize(fit_long_edge(*out.size, max_edge), Image.Resampling.LANCZOS)
        if ORIENTATION in exif:
            exif[ORIENTATION] = 1
        if strip_gps and GPS_IFD in exif:
            del exif[GPS_IFD]
        buf = BytesIO()
        try:
            exif_bytes = exif.tobytes()
        except (OSError, ValueError, TypeError, KeyError):
            # A maker's EXIF that Pillow can read but not write back. The
            # pixels matter more than the tags; send them without.
            exif_bytes = b""
        out.save(buf, "JPEG", quality=quality, exif=exif_bytes, icc_profile=icc, optimize=True)
        return buf.getvalue()
