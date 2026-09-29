"""What the agent's tests share: a library and cache of their own, a clean
database, and media files made on the spot.

Every file a test needs is generated: JPEGs with whatever EXIF the test is
about (Pillow writes dates, offsets, orientation and GPS), PNGs, HEICs
(pillow-heif ships an encoder), and short videos from ffmpeg's test source.
Nothing reads the real library. Tests that need a tool this machine lacks
(exiftool, an H.264 encoder, zscale) skip and say which.

The database tests run against ``MEERPIC_TEST_DB`` and skip without it (see
conftest.py). Each test module wraps ``use_library`` and ``use_db`` in
fixtures of its own (two lines each), as the server's tests do.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image
from PIL.ExifTags import GPS, IFD

needs_db = pytest.mark.skipif(not os.environ.get("MEERPIC_TEST_DB"), reason="MEERPIC_TEST_DB is not set")
needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool is not installed")
needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg/ffprobe not installed"
)

ROOT = "icloud"


def ffmpeg_caps():
    from agent import video

    return video.capabilities("ffmpeg")


def needs_encoder(name: str):
    have = shutil.which("ffmpeg") is not None and name in ffmpeg_caps().encoders
    return pytest.mark.skipif(not have, reason=f"this ffmpeg has no {name} encoder")


def needs_h264_encoder():
    have = shutil.which("ffmpeg") is not None and ffmpeg_caps().h264_encoder is not None
    return pytest.mark.skipif(not have, reason="this ffmpeg has no H.264 encoder")


def needs_tonemap():
    have = shutil.which("ffmpeg") is not None and ffmpeg_caps().tonemap
    return pytest.mark.skipif(not have, reason="this ffmpeg has no zscale/tonemap")


# --- a library and a cache of the test's own --------------------------------------


def use_library(tmp_path, monkeypatch):
    """A fresh library root and cache for one test; yields the root path.

    Patched onto the one cached Settings object, never by clearing the cache:
    modules imported at collection time (the server's among them) hold that
    object, and a second one would leave them, and every later test in the
    run, looking at a different configuration from the one being patched.
    """
    s = settings()
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setattr(s, "library_roots", {ROOT: str(root)})
    monkeypatch.setattr(s, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(s, "video_previews", "all")
    monkeypatch.setattr(s, "agent_workers", 2)
    # Whatever a test forgets to fake, rclone never reaches a real remote.
    monkeypatch.setattr(s, "rclone", str(rclone_guard(tmp_path)))
    yield root


def rclone_guard(tmp_path: Path) -> Path:
    """An rclone that runs the real one on local paths only.

    The default remote is the user's iCloud, and a delete test that forgot
    to point ``sync.remote`` somewhere else would delete real photos. Any
    argument that looks like ``remote:path`` makes this exit 97 instead.
    """
    real = shutil.which("rclone") or "false"
    script = tmp_path / "rclone-guard"
    script.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        "    /*|-*) ;;\n"
        '    *:*) echo "rclone-guard: refusing a remote in a test: $*" >&2; exit 97 ;;\n'
        "  esac\n"
        "done\n"
        f'exec {real} "$@"\n'
    )
    script.chmod(0o755)
    return script


def local_remote(tmp_path: Path, monkeypatch, album: str = "All Photos") -> Path:
    """Point ``sync.remote`` at a local folder ``<tmp>/remote/<album>``, the
    shape of iCloud's (albums are its siblings). Returns that folder."""
    folder = tmp_path / "remote" / album
    folder.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings(), "sync_remote", str(folder))
    return folder


def settings():
    """The process's Settings object, the one every module shares."""
    from core.config import get_settings

    return get_settings()


# --- the database ------------------------------------------------------------------

_ready = False


def truncate() -> None:
    from sqlalchemy import text

    from core.database import engine

    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE photo_embeddings, photo_albums, albums, jobs, settings, deleted_files, photos"
            " RESTART IDENTITY CASCADE"
        ))


def use_db():
    """A session on an empty test database (tables created once per run)."""
    global _ready
    if not os.environ.get("MEERPIC_TEST_DB"):
        pytest.skip("MEERPIC_TEST_DB is not set")
    from core.database import SessionLocal, init_db

    if not _ready:
        init_db()
        _ready = True
    truncate()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def add_photo(db, name: str, **cols):
    """A row as the scan would write it, plus whatever columns the test sets."""
    from core.media import classify, make_sig
    from core.models import UNDATED, Photo

    ext, kind, mime = classify(name)
    values = {
        "root": ROOT, "rel_path": name, "name": name, "ext": ext, "kind": kind, "mime": mime,
        "size": 100, "mtime_ns": 1_700_000_000 * 10**9, "sig": make_sig(ROOT, name, 100, 1),
        "sort_at": UNDATED,
    }
    values.update(cols)
    row = Photo(**values)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --- files ---------------------------------------------------------------------------


def set_mtime(path: Path, when: datetime) -> None:
    """Set a file's mtime from a naive UTC datetime."""
    ts = when.replace(tzinfo=timezone.utc).timestamp()
    os.utime(path, (ts, ts))


def jpeg(
    path: Path,
    *,
    size: tuple[int, int] = (64, 48),
    color: tuple[int, int, int] = (200, 30, 30),
    date: str | None = None,
    offset: str | None = None,
    create_date: str | None = None,
    gps: tuple[float, float] | None = None,
    orientation: int | None = None,
    make: str | None = None,
    model: str | None = None,
    user_comment: str | None = None,
    image: Image.Image | None = None,
) -> Path:
    """A JPEG with exactly the EXIF asked for."""
    img = image or Image.new("RGB", size, color)
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    if make:
        exif[0x010F] = make
    if model:
        exif[0x0110] = model
    sub = exif.get_ifd(IFD.Exif)
    if date:
        sub[0x9003] = date                  # DateTimeOriginal
    if offset:
        sub[0x9011] = offset                # OffsetTimeOriginal
    if create_date:
        sub[0x9004] = create_date           # CreateDate (DateTimeDigitized)
    if user_comment:
        sub[0x9286] = b"ASCII\x00\x00\x00" + user_comment.encode()
    if gps:
        lat, lon = gps
        g = exif.get_ifd(IFD.GPSInfo)
        g[GPS.GPSLatitudeRef] = "N" if lat >= 0 else "S"
        g[GPS.GPSLatitude] = _dms(abs(lat))
        g[GPS.GPSLongitudeRef] = "E" if lon >= 0 else "W"
        g[GPS.GPSLongitude] = _dms(abs(lon))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", exif=exif, quality=90)
    return path


def _dms(value: float) -> tuple[float, float, float]:
    d = int(value)
    m = int((value - d) * 60)
    s = round((value - d - m / 60) * 3600, 4)
    return (float(d), float(m), s)


def png(path: Path, *, size=(80, 60), color=(0, 0, 255, 255), mode: str = "RGBA") -> Path:
    img = Image.new(mode, size, color if mode != "RGB" else color[:3])
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")
    return path


def heic(path: Path, *, size=(96, 64), color=(0, 160, 0)) -> Path:
    import pillow_heif

    pillow_heif.register_heif_opener()
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "HEIF", quality=80)
    return path


def make_video(
    path: Path,
    *,
    duration: float = 1.0,
    size: tuple[int, int] = (320, 240),
    rate: int = 10,
    codec: str = "mpeg4",
    audio: str | None = None,
    extra: list[str] | None = None,
) -> Path:
    """A short test-pattern video. ``codec`` is an ffmpeg encoder name;
    ``audio`` an audio encoder ("aac", "pcm_s16le") or None for silence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=duration={duration}:size={size[0]}x{size[1]}:rate={rate}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    cmd += ["-c:v", codec, "-pix_fmt", "yuv420p"]
    if codec == "mpeg4":
        cmd += ["-q:v", "5"]
    if audio:
        cmd += ["-c:a", audio]
    cmd += [*(extra or []), str(path)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    return path
