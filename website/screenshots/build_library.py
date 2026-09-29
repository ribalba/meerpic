"""Build the demo photo library (see demo_photos.py) under $DEMO_DIR.

Runs in the meerpic agent image, which has everything this needs and a host
may not: pillow-heif with an HEVC encoder for the HEIC stills, an ffmpeg with
libx265 for the iPhone-style videos, and exiftool for the metadata.

    make -C website demo-library       # what normally runs it
    docker compose -f website/screenshots/docker-compose.yml run --rm --no-deps \
        agent python /website/screenshots/build_library.py

What it writes, and nothing else:

    $DEMO_DIR/sources/<id>.jpg          the CC0 originals, downloaded once
    $DEMO_DIR/library/iCloud/...        what `rclone copy` of iCloud Photos would leave
    $DEMO_DIR/library/Camera/...        dated folders of camera imports
    $DEMO_DIR/library.json              what was built for which day
    website/public/img/screenshots/credits.txt

Files are rebuilt in place when the day they were built for is not today
(their dates are relative to it), and left alone otherwise; --force rebuilds
all of them, which is what a change to this script needs. It never deletes:
a file in the library that the manifest no longer names is reported, with
the command that would remove it, for you to run or not.

The stills are StockSnap's 960 px renditions, cropped to the camera's aspect
and scaled up to its size, so the grid and the info panel look like a phone's
library. Nobody should mistake them for 12 megapixels of detail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo_photos as demo

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
WEBSITE = Path(__file__).resolve().parent.parent
CREDITS = WEBSITE / "public" / "img" / "screenshots" / "credits.txt"

IPHONE_SIZE = (4032, 3024)
CAMERA_SIZE = (3000, 2000)
WHATSAPP_EDGE = 1600
VIDEO_SIZE = (1920, 1080)
LIVE_SIZE = (1440, 1080)
LIVE_SECONDS = 2.8


def demo_dir() -> Path:
    raw = os.environ.get("DEMO_DIR", "").strip()
    if not raw:
        sys.exit("DEMO_DIR is not set: the folder the demo library is built in")
    return Path(raw)


# --- sources ----------------------------------------------------------------------


def fetch(code: str, sources: Path) -> Path:
    path = sources / f"{code}.jpg"
    if path.is_file() and path.stat().st_size > 1000:
        return path
    req = urllib.request.Request(demo.source_url(code), headers={"User-Agent": UA, "Referer": "https://stocksnap.io/"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            if not data.startswith(b"\xff\xd8"):
                raise RuntimeError("not a JPEG")
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(path)
            return path
        except Exception as exc:
            if attempt == 3:
                raise RuntimeError(f"could not download {demo.source_url(code)}: {exc}") from exc
            time.sleep(2 + attempt * 3)
    return path


# --- pictures ---------------------------------------------------------------------


def crop_to(img, aspect: float):
    """The largest centred crop of ``img`` with width/height == aspect."""
    w, h = img.size
    if w / h > aspect:
        nw = round(h * aspect)
        left = (w - nw) // 2
        return img.crop((left, 0, left + nw, h))
    nh = round(w / aspect)
    top = (h - nh) // 2
    return img.crop((0, top, w, top + nh))


def still(src: Path, size: tuple[int, int], portrait: bool):
    from PIL import Image

    w, h = size if not portrait else (size[1], size[0])
    img = Image.open(src).convert("RGB")
    return crop_to(img, w / h).resize((w, h), Image.LANCZOS)


def warmer(img):
    """What "edited" looks like: a touch warmer and punchier, slightly cropped."""
    from PIL import Image, ImageEnhance

    w, h = img.size
    img = img.crop((w * 0.04, h * 0.04, w * 0.96, h * 0.96)).resize((w, h), Image.LANCZOS)
    img = ImageEnhance.Color(img).enhance(1.3)
    img = ImageEnhance.Contrast(img).enhance(1.08)
    r, g, b = img.split()
    r = r.point(lambda v: min(255, int(v * 1.06)))
    b = b.point(lambda v: int(v * 0.94))
    return Image.merge("RGB", (r, g, b))


def save_heic(img, path: Path) -> None:
    import pillow_heif

    pillow_heif.register_heif_opener()
    img.save(path, format="HEIF", quality=80)


def ken_burns(src: Path, out: Path, size: tuple[int, int], seconds: float, zoom_to: float) -> None:
    """A slow push-in on a still: a video that is plainly a video in a grid."""
    from PIL import Image

    w, h = size
    frames = round(seconds * 30)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "base.png"
        img = Image.open(src).convert("RGB")
        crop_to(img, w / h).resize((w * 5 // 4, h * 5 // 4), Image.LANCZOS).save(base)
        step = (zoom_to - 1.0) / frames
        vf = (f"zoompan=z='min(zoom+{step:.6f},{zoom_to})':d={frames}"
              f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps=30")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-i", str(base),
             "-vf", vf, "-frames:v", str(frames),
             "-c:v", "libx265", "-tag:v", "hvc1", "-pix_fmt", "yuv420p", "-crf", "26",
             # No thread pool: several encodes run side by side, and x265
             # left to itself takes every core for each of them.
             "-preset", "fast", "-x265-params", "log-level=error:pools=none",
             "-movflags", "+faststart", str(out)],
            check=True,
        )


# --- metadata -----------------------------------------------------------------------


def _offset(td: timedelta) -> str:
    minutes = int(td.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def apple_makernote(content_id: str) -> bytes:
    """The smallest Apple maker note that carries a ContentIdentifier.

    exiftool can only write Apple's maker-note tags into a maker note that
    already exists, and a phone's HEIC is the only thing that has one. This
    is that block with one entry: the tag a Live Photo's still shares with
    its video, which is the only thing that pairs them.
    """
    value = content_id.encode("ascii") + b"\0"
    header = b"Apple iOS\0" + b"\x00\x01" + b"MM"
    data_offset = len(header) + 2 + 12 + 4
    entry = struct.pack(">HHII", 0x0011, 2, len(value), data_offset)
    return header + struct.pack(">H", 1) + entry + struct.pack(">I", 0) + value


def exif_args(r: demo.Resolved) -> list[str]:
    item = r.item
    cam = item.camera
    iso, exposure = demo.LIGHT[item.light]
    stamp = f"{r.local:%Y:%m:%d %H:%M:%S}"
    off = _offset(r.offset)
    args = [
        f"-Make={cam.make}", f"-Model={cam.model}", f"-LensModel={cam.lens}",
        f"-Software={cam.software}",
        f"-DateTimeOriginal={stamp}", f"-CreateDate={stamp}", f"-ModifyDate={stamp}",
        f"-OffsetTimeOriginal={off}", f"-OffsetTimeDigitized={off}", f"-OffsetTime={off}",
        "-SubSecTimeOriginal=412",
        f"-ISO={iso}", f"-FNumber={cam.f_number}", f"-ExposureTime={exposure}",
        f"-FocalLength={cam.focal}", f"-FocalLengthIn35mmFormat={cam.focal35}",
        # The pixels are stored upright. `#`: the number itself; without it
        # exiftool reads "1" against its descriptions and writes 3, Rotate 180.
        "-Orientation#=1",
    ]
    if cam.lens_make:
        args.append(f"-LensMake={cam.lens_make}")
    if cam.make == "Apple":
        args.append(f"-HostComputer={cam.model}")
    if r.lat is not None:
        args += [
            f"-GPSLatitude={abs(r.lat)}", f"-GPSLatitudeRef={'N' if r.lat >= 0 else 'S'}",
            f"-GPSLongitude={abs(r.lon)}", f"-GPSLongitudeRef={'E' if r.lon >= 0 else 'W'}",
            f"-GPSAltitude={r.alt}", "-GPSAltitudeRef=0",
        ]
    return args


def video_args(r: demo.Resolved, content_id: str = "") -> list[str]:
    cam = r.item.camera
    args = [
        f"-Keys:CreationDate={r.local:%Y:%m:%d %H:%M:%S}{_offset(r.offset)}",
        f"-QuickTime:CreateDate={r.utc:%Y:%m:%d %H:%M:%S}",
        f"-QuickTime:ModifyDate={r.utc:%Y:%m:%d %H:%M:%S}",
        f"-Keys:Make={cam.make}", f"-Keys:Model={cam.model}", f"-Keys:Software={cam.software}",
    ]
    if r.lat is not None:
        args.append(f"-Keys:GPSCoordinates={r.lat}, {r.lon}, {r.alt}")
    if content_id:
        args.append(f"-Keys:ContentIdentifier={content_id}")
    return args


def exiftool(path: Path, args: list[str]) -> None:
    subprocess.run(["exiftool", "-q", "-q", "-overwrite_original", *args, str(path)], check=True)


def building(path: Path) -> Path:
    return path.with_name(f".{path.stem}.building{path.suffix}")


def touch(path: Path, when: datetime) -> None:
    """rclone sets a copied file's time to iCloud's date for it.

    To the second; the fraction is a hash of the content. meerpic knows a
    file by its path, size and time (core.media.make_sig), so a file rebuilt
    with other bytes but the same size would otherwise keep its old
    thumbnail, metadata and vector.
    """
    digest = hashlib.blake2b(path.read_bytes(), digest_size=8).digest()
    ns = int(when.timestamp()) * 1_000_000_000 + int.from_bytes(digest, "big") % 1_000_000_000
    os.utime(path, ns=(ns, ns))


# --- one item ---------------------------------------------------------------------


def build(r: demo.Resolved, library: Path, sources: Path) -> str:
    item = r.item
    base = library / demo.ROOT_DIRS[item.root]
    target = base / r.rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    # A dot name, which the agent's scanner never looks at, keeping the real
    # extension, which ffmpeg picks its container by.
    tmp = building(target)

    if item.kind == "screenshot":
        src = sources / f"{item.key}.png"
        if not src.is_file():
            return f"skipped {r.rel_path}: no {src.name} (run `make -C website demo-phone` first)"
        shutil.copyfile(src, tmp)
        stamp = f"{r.local:%Y:%m:%d %H:%M:%S}"
        exiftool(tmp, ["-UserComment=Screenshot", f"-DateTimeOriginal={stamp}",
                       f"-OffsetTimeOriginal={_offset(r.offset)}"])
    elif item.kind == "whatsapp":
        from PIL import Image

        img = Image.open(fetch(item.src, sources)).convert("RGB")
        img.thumbnail((WHATSAPP_EDGE, WHATSAPP_EDGE), Image.LANCZOS)
        # No EXIF at all: that is what WhatsApp saves.
        img.save(tmp, format="JPEG", quality=82)
    elif item.kind == "video":
        src = fetch(item.src, sources)
        ken_burns(src, tmp, VIDEO_SIZE, item.seconds, 1.18)
        exiftool(tmp, video_args(r))
    else:
        src = fetch(item.src, sources)
        if item.root == demo.CAMERA:
            img = still(src, CAMERA_SIZE, item.portrait)
            img.save(tmp, format="JPEG", quality=88)
        else:
            img = still(src, IPHONE_SIZE, item.portrait)
            save_heic(img, tmp)
        args = exif_args(r)
        if r.content_id:
            with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
                fh.write(apple_makernote(r.content_id))
                note = fh.name
            try:
                exiftool(tmp, [*args, f"-MakerNotes<={note}"])
            finally:
                os.unlink(note)
        else:
            exiftool(tmp, args)
        if r.edit:
            edit = base / r.edit
            etmp = building(edit)
            save_heic(warmer(img), etmp)
            exiftool(etmp, [*args, "-Software=Photos 10.0"])
            etmp.replace(edit)
            touch(edit, r.utc + timedelta(minutes=3))
        if r.companion:
            motion = base / r.companion
            mtmp = building(motion)
            ken_burns(src, mtmp, LIVE_SIZE, LIVE_SECONDS, 1.05)
            exiftool(mtmp, video_args(r, r.content_id))
            mtmp.replace(motion)
            touch(motion, r.utc)
    tmp.replace(target)
    touch(target, r.utc)
    return f"built {demo.ROOT_DIRS[item.root]}/{r.rel_path}" + (f" + {r.companion}" if r.companion else "") + (
        f" + {r.edit}" if r.edit else "")


def stamp(r: demo.Resolved, sources: Path) -> str:
    """What a built file was built from: its day, and for a screenshot the
    rendering it was copied from, which changes whenever the site does."""
    out = f"{r.local:%Y-%m-%d %H:%M:%S}"
    if r.item.kind == "screenshot":
        src = sources / f"{r.item.key}.png"
        if src.is_file():
            out += f" {src.stat().st_size}:{src.stat().st_mtime_ns}"
    return out


def _build(args):
    r, library, sources = args
    try:
        return build(r, library, sources)
    except Exception as exc:  # noqa: BLE001 - reported per file, the rest still build
        return f"FAILED {r.rel_path}: {type(exc).__name__}: {exc}"


# --- credits ------------------------------------------------------------------------


def write_credits(rows: list[demo.Resolved]) -> None:
    used = sorted({r.item.src for r in rows if r.item.src}, key=lambda c: demo.SOURCES[c][1].lower())
    lines = [
        "The photos in the meerpic screenshots",
        "",
        "Every picture in the screenshots on this site is a demo photo, not anybody's own",
        "library. All of them are from StockSnap and dedicated to the public domain under",
        "CC0 1.0 (https://creativecommons.org/publicdomain/zero/1.0/): no attribution is",
        "required, and it is given here anyway. Dates, places and cameras shown with them",
        "are made up for the demo.",
        "",
    ]
    for code in used:
        by, title = demo.SOURCES[code]
        lines.append(f"{title}, by {by}: {demo.source_page(code)}")
    lines.append("")
    CREDITS.parent.mkdir(parents=True, exist_ok=True)
    CREDITS.write_text("\n".join(lines), encoding="utf-8")


# --- main -----------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--force", action="store_true",
                    help="rebuild every file, also those already built for today (after changing this script)")
    args = ap.parse_args()
    root = demo_dir()
    sources = root / "sources"
    library = root / "library"
    stamp_file = root / "library.json"
    for d in (sources, *(library / sub for sub in demo.ROOT_DIRS.values())):
        d.mkdir(parents=True, exist_ok=True)

    on = demo.today()
    rows = demo.resolve(on)
    try:
        built = json.loads(stamp_file.read_text())
    except (OSError, ValueError):
        built = {}

    # Downloads first and one at a time: StockSnap's CDN is polite to people
    # who are polite to it, and the workers below then only read local files.
    for code in sorted({r.item.src for r in rows if r.item.src}):
        fetch(code, sources)

    todo = []
    for r in rows:
        key = f"{demo.ROOT_DIRS[r.item.root]}/{r.rel_path}"
        target = library / key
        want = stamp(r, sources)
        extra_ok = all((target.parent / n).is_file() for n in r.names[1:])
        if not args.force and built.get(key) == want and target.is_file() and extra_ok:
            continue
        todo.append(r)

    # A background job, not the machine's main business: a quarter of the
    # cores at most, at low priority (the workers inherit it).
    workers = max(1, min(4, (os.cpu_count() or 4) // 4))
    os.nice(10)
    print(f"demo library for {on}: {len(rows)} items, {len(todo)} to build, {workers} workers", flush=True)
    failed = 0
    with ProcessPoolExecutor(workers) as pool:
        for r, line in zip(todo, pool.map(_build, [(r, library, sources) for r in todo]), strict=True):
            print(f"  {line}", flush=True)
            if line.startswith("FAILED"):
                failed += 1
            elif line.startswith("built"):
                built[f"{demo.ROOT_DIRS[r.item.root]}/{r.rel_path}"] = stamp(r, sources)
    stamp_file.write_text(json.dumps(built, indent=1, sort_keys=True))
    write_credits(rows)

    # Anything in the library the manifest does not name: say so, delete nothing.
    wanted = {f"{demo.ROOT_DIRS[r.item.root]}/{n}" for r in rows for n in r.names}
    stray = sorted(
        str(p.relative_to(library)) for p in library.rglob("*")
        if p.is_file() and not p.name.startswith(".") and str(p.relative_to(library)) not in wanted
    )
    if stray:
        print(f"\n{len(stray)} file(s) in {library} that the manifest does not name; they will show up")
        print("in the screenshots. To remove them:")
        for s in stray:
            print(f"  rm {library / s!s}")
    print(f"\ncredits: {CREDITS.relative_to(WEBSITE.parent)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
