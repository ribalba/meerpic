"""What a file says about itself: exiftool, in batches, turned into columns.

exiftool reads every format in the library (HEIC, JPEG, PNG, MOV, MP4, the odd
3GP) with one set of tag names, which is worth its Perl start-up time many
times over. It runs at roughly 100 files a second per process, so a batch is
split into chunks run by several processes at once, and the file list goes in
through ``-@ argfile`` because iCloud names contain spaces, commas, ``+`` and
U+FF0F, and a command line is the wrong place for any of them.

``extract`` is pure: exiftool's record in, the ``photos`` columns out. The
runner and the stage that writes the rows are elsewhere (``run_exiftool``
below, ``agent/pipeline.py``), so the rules can be tested on recorded records.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from . import dates

# Asked for by name, so exiftool skips everything else (MakerNotes are most of
# a HEIC's metadata and none of them matter here). Group-qualified where the
# unqualified name would pick the wrong group: IPTC DateCreated is a bare date,
# the XMP one is what apps write.
TAGS = (
    "CreationDate", "DateTimeOriginal", "SubSecTimeOriginal", "OffsetTimeOriginal",
    "OffsetTime", "XMP:DateCreated", "DateTimeCreated", "CreateDate",
    "GPSLatitude", "GPSLongitude", "GPSAltitude",
    "Make", "Model", "LensModel", "LensMake", "ISO", "FNumber", "ExposureTime",
    "FocalLength", "FocalLengthIn35mmFormat",
    "ImageWidth", "ImageHeight", "ExifImageWidth", "ExifImageHeight",
    "Orientation", "Rotation", "Duration", "CompressorID", "ContentIdentifier",
    "UserComment", "Software", "Flash", "VideoFrameRate", "AvgBitrate", "HDRHeadroom",
    # Not a tag in the file: exiftool's own verdict ("File format error",
    # "File is empty"), reported only when asked for by name like the rest.
    "Error",
)

# The part of the record worth showing and not worth a column. Only keys the
# file actually has end up in ``photos.exif``.
EXIF_KEEP = (
    "Software", "LensMake", "FocalLengthIn35mmFormat", "Flash", "VideoFrameRate",
    "AvgBitrate", "HDRHeadroom", "OffsetTimeOriginal", "SubSecTimeOriginal",
)

_SCREENSHOT_NAME = re.compile(r"screenshot|bildschirmfoto|screen shot", re.IGNORECASE)

# CompressorID is a QuickTime four-character code; the UI and the preview
# stage want the codec's name.
_CODECS = {"hvc1": "hevc", "hev1": "hevc", "avc1": "h264", "avc3": "h264"}

# Per exiftool process. A 4 GB video is read in well under this; a hung
# network mount is not, and must not hang the agent with it.
_TIMEOUT_BASE = 60.0
_TIMEOUT_PER_FILE = 2.0


class ExiftoolError(RuntimeError):
    pass


# --- running exiftool -----------------------------------------------------------


def _argv(exiftool: str, argfile: str) -> list[str]:
    return [
        exiftool, "-j", "-n", "-charset", "filename=utf8",
        "-api", "LargeFileSupport=1",
        *(f"-{tag}" for tag in TAGS),
        "-@", argfile,
    ]


def run_exiftool(paths: Sequence[str], exiftool: str = "exiftool") -> dict[str, dict]:
    """exiftool's record for each path it could read, keyed by that path.

    A file exiftool cannot parse comes back as a record with an ``Error``
    key; one that vanished comes back not at all. Raises ExiftoolError when
    the process itself fails or says nothing parseable.
    """
    if not paths:
        return {}
    fd, argfile = tempfile.mkstemp(prefix="meerpic-exif-", suffix=".args")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as fh:
            for path in paths:
                fh.write(path + "\n")
        try:
            proc = subprocess.run(
                _argv(exiftool, argfile),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_TIMEOUT_BASE + _TIMEOUT_PER_FILE * len(paths),
                check=False,
            )
        except FileNotFoundError as exc:
            raise ExiftoolError(f"exiftool not found: {exiftool}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ExiftoolError(f"exiftool timed out on {len(paths)} file(s)") from exc
    finally:
        try:
            os.unlink(argfile)
        except OSError:
            pass
    out = proc.stdout.decode("utf-8", "replace").strip()
    # exit status 1 only means "some file had an error", which the records
    # say for themselves; no output at all is the real failure.
    if not out:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        # Every file missing is not a failure of exiftool's: nothing to say.
        if err and all("File not found" in line for line in err):
            return {}
        raise ExiftoolError(err[-1] if err else f"exiftool exited {proc.returncode} with no output")
    try:
        records = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ExiftoolError(f"exiftool output is not JSON: {exc}") from exc
    return {str(r.get("SourceFile", "")): r for r in records if isinstance(r, dict)}


def read_many(
    paths: Sequence[str], exiftool: str = "exiftool", processes: int = 4
) -> dict[str, dict | Exception]:
    """``run_exiftool`` over chunks in parallel, isolating failures.

    A chunk whose process fails is retried one file at a time, so one file
    that crashes exiftool costs its own record, not the forty beside it.
    The value is the record, or the exception for a file that could not be
    read at all; a path missing from the result vanished before exiftool
    got to it.
    """
    paths = list(paths)
    if not paths:
        return {}
    processes = max(1, min(processes, len(paths)))
    size = math.ceil(len(paths) / processes)
    chunks = [paths[i:i + size] for i in range(0, len(paths), size)]

    def one_chunk(chunk: list[str]) -> dict[str, dict | Exception]:
        try:
            return dict(run_exiftool(chunk, exiftool))
        except ExiftoolError:
            if len(chunk) == 1:
                raise
        out: dict[str, dict | Exception] = {}
        for path in chunk:
            try:
                out.update(run_exiftool([path], exiftool))
            except ExiftoolError as exc:
                out[path] = exc
        return out

    result: dict[str, dict | Exception] = {}
    with ThreadPoolExecutor(max_workers=processes, thread_name_prefix="exiftool") as pool:
        futures = [(chunk, pool.submit(one_chunk, chunk)) for chunk in chunks]
        for chunk, future in futures:
            try:
                result.update(future.result())
            except ExiftoolError as exc:
                result[chunk[0]] = exc
    return result


# --- record -> columns ----------------------------------------------------------


def _clean_str(value: object, limit: int) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).replace("\x00", "").strip()[:limit]


def _num(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(str(value).split()[0]) if isinstance(value, str) else float(value)
    except (ValueError, IndexError, TypeError):
        return None
    return out if math.isfinite(out) else None


def _int(value: object) -> int | None:
    out = _num(value)
    if out is None or abs(out) > 2**31 - 1:
        return None
    return round(out)


def _json_safe(value: object) -> object:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value.replace("\x00", "")[:500]
    if isinstance(value, (int, bool)) or value is None:
        return value
    return str(value)[:500]


def rotated(kind: str, orientation: int | None, rotation: float | None) -> bool:
    """Whether the stored pixels are turned a quarter from how they show.

    Three places say so: EXIF Orientation 5-8; a HEIF's ``irot`` (exiftool
    calls it Rotation too, counted in quarter turns: 1 and 3); a video's
    Rotation in degrees. A HEIC carries both of the first two for the same
    turn, which is why this is an "or" and not a sum.
    """
    if orientation is not None and 5 <= orientation <= 8:
        return True
    if rotation is None:
        return False
    rot = round(rotation)
    if kind == "photo" and rot in (1, 3):
        return True
    return rot % 360 in (90, 270)


def dimensions(rec: Mapping[str, object], kind: str) -> tuple[int | None, int | None]:
    w = _int(rec.get("ImageWidth")) or _int(rec.get("ExifImageWidth"))
    h = _int(rec.get("ImageHeight")) or _int(rec.get("ExifImageHeight"))
    if not w or not h or w <= 0 or h <= 0:
        return None, None
    if rotated(kind, _int(rec.get("Orientation")), _num(rec.get("Rotation"))):
        w, h = h, w
    return w, h


def gps(rec: Mapping[str, object]) -> tuple[float | None, float | None, float | None]:
    """(lat, lon, altitude). (0, 0) is a phone that had no fix, not a photo
    taken in the Gulf of Guinea."""
    lat, lon = _num(rec.get("GPSLatitude")), _num(rec.get("GPSLongitude"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None, None
    if abs(lat) < 1e-9 and abs(lon) < 1e-9:
        return None, None, None
    return lat, lon, _num(rec.get("GPSAltitude"))


def is_screenshot(rec: Mapping[str, object], name: str) -> bool:
    if _clean_str(rec.get("UserComment"), 100).lower() == "screenshot":
        return True
    return bool(_SCREENSHOT_NAME.search(name))


def video_codec(rec: Mapping[str, object]) -> str:
    raw = _clean_str(rec.get("CompressorID"), 32).lower()
    return _CODECS.get(raw, raw)


def extract(
    rec: Mapping[str, object],
    *,
    kind: str,
    name: str,
    mtime_ns: int | None,
    zone: ZoneInfo,
    now: datetime | None = None,
) -> dict:
    """Every column the meta stage owns, from one exiftool record.

    The place columns are left to geo (they need a batch lookup); this
    returns lat/lon and leaves city/region/country to the caller.
    """
    width, height = dimensions(rec, kind)
    lat, lon, alt = gps(rec)
    duration = _num(rec.get("Duration")) if kind == "video" else None
    exif = {
        key: _json_safe(rec[key])
        for key in EXIF_KEEP
        if key in rec and rec[key] not in (None, "")
    }
    cols = {
        **dates.resolve(rec, kind=kind, name=name, mtime_ns=mtime_ns, zone=zone, now=now).columns(),
        "width": width,
        "height": height,
        "duration": duration if duration is None or duration >= 0 else None,
        "video_codec": video_codec(rec) if kind == "video" else "",
        "make": _clean_str(rec.get("Make"), 128),
        "model": _clean_str(rec.get("Model"), 128),
        "lens": _clean_str(rec.get("LensModel"), 256),
        "iso": _int(rec.get("ISO")),
        "f_number": _num(rec.get("FNumber")),
        "exposure_time": _num(rec.get("ExposureTime")),
        "focal_length": _num(rec.get("FocalLength")),
        "is_screenshot": is_screenshot(rec, name),
        "exif": exif,
        "lat": lat,
        "lon": lon,
        "altitude": alt,
        "content_id": _clean_str(rec.get("ContentIdentifier"), 64),
    }
    return cols
