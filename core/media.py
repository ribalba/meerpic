"""What counts as a photo, and where everything derived from one is kept.

Shared by the agent, which writes the cache, and the server, which reads it,
so that the two agree on a thumbnail's path by construction rather than by
convention.

The cache key is ``photos.sig``: a short hash of where a file is, how big it is
and when it last changed. A file that is replaced in place gets a new sig, so
its derived files get new names and its URLs change with them, which is what
lets the server hand every derived file out as immutable and a browser keep it
forever without ever showing a stale one.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import get_settings

# Extension (lower case, no dot) -> (kind, MIME type). Anything else in the
# library is ignored: sidecars (.aae, .xmp), rclone's partial files, a stray
# .DS_Store.
EXTENSIONS: dict[str, tuple[str, str]] = {
    "jpg": ("photo", "image/jpeg"),
    "jpeg": ("photo", "image/jpeg"),
    "heic": ("photo", "image/heic"),
    "heif": ("photo", "image/heif"),
    "png": ("photo", "image/png"),
    "webp": ("photo", "image/webp"),
    "gif": ("photo", "image/gif"),
    "avif": ("photo", "image/avif"),
    "tif": ("photo", "image/tiff"),
    "tiff": ("photo", "image/tiff"),
    "dng": ("photo", "image/x-adobe-dng"),
    "mov": ("video", "video/quicktime"),
    "mp4": ("video", "video/mp4"),
    "m4v": ("video", "video/x-m4v"),
    "3gp": ("video", "video/3gpp"),
    "mpg": ("video", "video/mpeg"),
    "mpeg": ("video", "video/mpeg"),
    "avi": ("video", "video/x-msvideo"),
    "mkv": ("video", "video/x-matroska"),
    "webm": ("video", "video/webm"),
}

# Formats every current browser draws in an <img> as they are. Anything else
# (HEIC above all) is converted by the server before it is shown.
BROWSER_IMAGES = {"jpg", "jpeg", "png", "webp", "gif", "avif"}

# Names the scanner never looks at: rclone writes a download to NAME.partial
# and renames it when it is complete, and a half-written HEIC is not a photo.
IGNORED_SUFFIXES = (".partial", ".tmp", ".part")


def classify(name: str) -> tuple[str, str, str] | None:
    """(ext, kind, mime) for a file name, or None if it is not media."""
    if name.startswith(".") or name.endswith(IGNORED_SUFFIXES):
        return None
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    hit = EXTENSIONS.get(ext)
    if not hit:
        return None
    return ext, hit[0], hit[1]


def make_sig(root: str, rel_path: str, size: int, mtime_ns: int) -> str:
    """The cache key for one version of one file. 16 hex characters."""
    raw = f"{root}\0{rel_path}\0{size}\0{mtime_ns}".encode("utf-8", "surrogateescape")
    return hashlib.blake2b(raw, digest_size=8).hexdigest()


# --- derived files -----------------------------------------------------------
#
# <cache>/thumb/ab/abcdef0123456789.webp     every photo and video (a frame)
# <cache>/preview/ab/abcdef0123456789.mp4    videos, H.264 + AAC, faststart
# <cache>/display/ab/abcdef0123456789.jpg    HEIC and friends, made on demand
# <cache>/story/ab/abcdef0123456789.webp     videos: a strip of frames, for hovering
# <cache>/face/ab/abcdef0123456789-0.webp    photos: one square per face found in it
#
# Two hex characters of fan-out keep any one directory to a few hundred
# entries for a library of 100,000.

THUMB_EDGE = 400            # short edge, px; the grid draws rows ~200 CSS px tall
THUMB_MAX_LONG = 1600       # a panorama's long edge stops somewhere
DISPLAY_EDGE = 2560         # long edge of a converted full-screen image
STORY_FRAMES = 10           # frames in a video's hover strip
STORY_HEIGHT = 180          # px, each frame; the width follows the video
FACE_EDGE = 160             # px, a face's square crop, for the info panel


def _derived(kind: str, sig: str, suffix: str) -> Path:
    return get_settings().cache_path / kind / sig[:2] / f"{sig}.{suffix}"


def thumb_path(sig: str) -> Path:
    return _derived("thumb", sig, "webp")


def preview_path(sig: str) -> Path:
    return _derived("preview", sig, "mp4")


def display_path(sig: str) -> Path:
    return _derived("display", sig, "jpg")


def story_path(sig: str) -> Path:
    return _derived("story", sig, "webp")


def face_path(sig: str, pos: int) -> Path:
    """The crop of the ``pos``-th face in one version of a photo."""
    return get_settings().cache_path / "face" / sig[:2] / f"{sig}-{pos}.webp"


# Where Delete puts the local copy once iCloud has let go of it: a hidden
# folder inside the library root, which the scanner skips (dot-name) and
# `rclone copy` leaves alone (it never touches what is only on this side).
# Not the cache: nothing in the cache may be something that cannot be rebuilt.
TRASH_DIR = ".meerpic-trash"


def trash_path(root: str) -> Path | None:
    base = get_settings().root_path(root)
    return base / TRASH_DIR if base is not None else None


def original_path(root: str, rel_path: str) -> Path | None:
    """Where a photo's file is, on this machine, or None for an unknown root."""
    base = get_settings().root_path(root)
    if base is None:
        return None
    return base / rel_path
