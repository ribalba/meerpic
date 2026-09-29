"""When a photo was taken, from whatever the file offers.

Pure functions, on purpose: this is the part of the agent most likely to be
subtly wrong (time zones, cameras that write nonsense, files with no date at
all), and the part where being wrong is most visible, because the grid is
sorted by it. Everything here takes plain values and returns plain values, so
every rule has a test of its own.

The sources, best first (see SPEC and core/models.DATE_SOURCES):

1. Apple's QuickTime ``CreationDate``: the only video date with an offset.
2. EXIF ``DateTimeOriginal``, with ``OffsetTimeOriginal`` (or ``OffsetTime``)
   when the camera wrote one.
3. XMP ``DateCreated`` / IPTC ``DateTimeCreated``: what editors and apps write.
4. ``CreateDate``: a wall clock in an image, but UTC in a QuickTime file,
   which is the QuickTime specification's rule even where cameras ignore it.
5. A date in the file name: phones, messengers and screenshot tools all put
   one there, and for an app's save it is often the only one.
6. The file's mtime, which rclone sets to iCloud's own date for the asset.
7. Nothing: the photo sorts after everything dated.

A value counts only when it parses *and* is plausible: 1900 or later and not
in the future (with a day of slack for clocks and zones). A camera whose clock
was never set writes ``0000:00:00 00:00:00`` or 1970, and a wrong date that
sorts a photo to 1970 is worse than falling through to the next source.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.models import UNDATED
from core.timeutil import utc_to_wall, utcnow, wall_to_utc

# Naive throughout, like every datetime in meerpic (see core/timeutil.py).
_EPOCH = datetime(1970, 1, 1)  # noqa: DTZ001

# An offset beyond this is not an offset: the extremes in use are -12:00 and
# +14:00, and ISO 8601 allows up to 18 hours either way.
_MAX_OFFSET = 18 * 60


@dataclass(frozen=True)
class Dated:
    """The answer, in the shape the ``photos`` columns want."""

    taken_at: datetime | None       # naive UTC
    taken_local: datetime | None    # naive wall clock
    tz_offset: int | None           # minutes east of UTC, when the file said
    source: str                     # one of core.models.DATE_SOURCES

    @property
    def sort_at(self) -> datetime:
        return self.taken_at or UNDATED

    def columns(self) -> dict:
        return {
            "taken_at": self.taken_at,
            "taken_local": self.taken_local,
            "tz_offset": self.tz_offset,
            "date_source": self.source,
            "sort_at": self.sort_at,
        }


NONE = Dated(None, None, None, "none")


# --- parsing ------------------------------------------------------------------

# exiftool's "YYYY:MM:DD HH:MM:SS", and the variants other writers produce:
# dashes, a T, fractional seconds, a trailing offset or Z, no seconds, or no
# time at all (XMP's DateCreated may be a bare date).
_STAMP = re.compile(
    r"^\s*(\d{4})[:\-/.](\d{1,2})[:\-/.](\d{1,2})"
    r"(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:[.,]\d+)?)?"
    r"\s*(Z|[+-]\d{2}(?::?\d{2})?)?\s*$"
)
_OFFSET = re.compile(r"^\s*(Z|([+-])(\d{2})(?::?(\d{2}))?)\s*$")

# Z means the writer converted to UTC: an instant, not a wall clock with a
# zero offset. A distinct marker keeps the two apart.
UTC_MARK = "Z"


def parse_offset(value: object) -> int | str | None:
    """Minutes east of UTC from "+02:00" / "+0200" / "+02", ``UTC_MARK``
    for "Z", None for anything else."""
    if value is None:
        return None
    m = _OFFSET.match(str(value))
    if not m:
        return None
    if m.group(1) == "Z":
        return UTC_MARK
    minutes = int(m.group(3)) * 60 + int(m.group(4) or 0)
    if minutes > _MAX_OFFSET:
        return None
    return -minutes if m.group(2) == "-" else minutes


def parse_stamp(value: object) -> tuple[datetime, int | str | None] | None:
    """(wall clock, offset) from one date tag's value, or None.

    The offset is minutes, ``UTC_MARK``, or None when the value carries none.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).replace("\x00", "").strip()
    m = _STAMP.match(text)
    if not m:
        return None
    y, mo, d, hh, mm, ss, off = m.groups()
    try:
        wall = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))  # noqa: DTZ001
    except ValueError:          # 0000:00:00, month 13, 25 o'clock
        return None
    offset = parse_offset(off) if off else None
    if off and offset is None:  # "+99:00": the whole value is suspect
        return None
    return wall, offset


def plausible(instant: datetime, now: datetime) -> bool:
    return instant.year >= 1900 and instant > UNDATED and instant <= now + timedelta(days=1)


# --- the three shapes a date comes in ----------------------------------------


def from_offset(wall: datetime, offset: int, source: str) -> Dated:
    """A wall clock that says where it was: both halves are exact."""
    return Dated(wall - timedelta(minutes=offset), wall, offset, source)


def from_wall(wall: datetime, zone: ZoneInfo, source: str) -> Dated:
    """A wall clock that does not say where: read it in the home zone."""
    return Dated(wall_to_utc(wall, zone), wall, None, source)


def from_instant(instant: datetime, zone: ZoneInfo, source: str) -> Dated:
    """An instant with no wall clock of its own: show it as home time."""
    return Dated(instant, utc_to_wall(instant, zone), None, source)


def _stamped(
    value: object,
    zone: ZoneInfo,
    source: str,
    now: datetime,
    *,
    offset: int | str | None = None,
    utc: bool = False,
) -> Dated | None:
    """One tag's value as a Dated, or None if it does not parse or is not
    plausible. An offset inside the value beats one from a separate tag;
    ``utc`` says a bare value is an instant (QuickTime CreateDate)."""
    parsed = parse_stamp(value)
    if parsed is None:
        return None
    wall, own = parsed
    off = own if own is not None else offset
    if off == UTC_MARK or (off is None and utc):
        dated = from_instant(wall, zone, source)
    elif isinstance(off, int):
        dated = from_offset(wall, off, source)
    else:
        dated = from_wall(wall, zone, source)
    if dated.taken_at is None or not plausible(dated.taken_at, now):
        return None
    return dated


# --- file names ---------------------------------------------------------------

# macOS writes "at 1.34.56 PM" with a narrow no-break space before the PM.
_NNBSP = chr(0x202F)

# Every pattern names year, month, day, hour, minute and second, and
# optionally an AM/PM marker. Tried in order, every match of each, first
# valid one wins.
_NAME_PATTERNS = (
    # IMG_20240101_123456, VID_..., PXL_20240101_123456789, MVIMG_...,
    # 20240101_123456, Screenshot_20240101-123456 (Android)
    re.compile(r"(?<!\d)(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})[_-](?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})"),
    # signal-2024-01-01-123456 (newer Signal versions dash the time too)
    re.compile(r"(?<!\d)(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})-(?P<h>\d{2})-?(?P<mi>\d{2})-?(?P<s>\d{2})"),
    # 2012-05-03 12.08.08 (Dropbox camera uploads),
    # Screenshot 2024-01-01 at 12.34.56 (macOS, 12-hour clocks add PM),
    # WhatsApp Image 2024-01-01 at 12.34.56, Bildschirmfoto 2024-01-01 um
    # 12.34.56, and 2022-03-07_19-03-19_000 (an app's export, seen in the
    # real library)
    re.compile(
        r"(?<!\d)(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})[ _]+(?:at |um )?"
        r"(?P<h>\d{1,2})(?P<sep>[.:-])(?P<mi>\d{2})(?P=sep)(?P<s>\d{2})"
        r"(?:[\s" + _NNBSP + r"]*(?P<ampm>[AaPp][Mm])(?![A-Za-z]))?"
    ),
)


def from_filename(name: str) -> datetime | None:
    """The wall clock a file name spells out, or None."""
    for pattern in _NAME_PATTERNS:
        for m in pattern.finditer(name):
            y, mo, d, hh, mm, ss = (int(m.group(k)) for k in ("y", "mo", "d", "h", "mi", "s"))
            ampm = (m.groupdict().get("ampm") or "").lower()
            if ampm:
                if not 1 <= hh <= 12:
                    continue
                hh = hh % 12 + (12 if ampm == "pm" else 0)
            try:
                return datetime(y, mo, d, hh, mm, ss)  # noqa: DTZ001 - a wall clock
            except ValueError:
                continue
    return None


def from_mtime(mtime_ns: int | None, zone: ZoneInfo, now: datetime | None = None) -> Dated | None:
    """The file's modification time as the photo's date, or None.

    Before 1971 means "never set" (a handful of files in the real library
    say 1970-01-01), and a date from the future means a clock was wrong.
    """
    if mtime_ns is None:
        return None
    now = now or utcnow()
    try:
        instant = _EPOCH + timedelta(microseconds=mtime_ns // 1000)
    except OverflowError:
        return None
    if instant.year < 1971 or not plausible(instant, now):
        return None
    return from_instant(instant, zone, "mtime")


# --- the whole decision -------------------------------------------------------


def resolve(
    tags: Mapping[str, object],
    *,
    kind: str,
    name: str,
    mtime_ns: int | None,
    zone: ZoneInfo,
    now: datetime | None = None,
) -> Dated:
    """The best date for one file, by the priority in the module docstring.

    ``tags`` is exiftool's record (``-j -n``) for the file, keyed by tag name
    without group; ``kind`` is "photo" or "video".
    """
    now = now or utcnow()

    dated = _stamped(tags.get("CreationDate"), zone, "quicktime", now)
    if dated:
        return dated

    offset = parse_offset(tags.get("OffsetTimeOriginal"))
    if offset is None:
        offset = parse_offset(tags.get("OffsetTime"))
    dated = _stamped(tags.get("DateTimeOriginal"), zone, "exif", now, offset=offset)
    if dated:
        return dated

    for key in ("DateCreated", "DateTimeCreated"):
        dated = _stamped(tags.get(key), zone, "xmp", now)
        if dated:
            return dated

    dated = _stamped(tags.get("CreateDate"), zone, "createdate", now, utc=kind == "video")
    if dated:
        return dated

    wall = from_filename(name)
    if wall is not None:
        dated = from_wall(wall, zone, "filename")
        if dated.taken_at is not None and plausible(dated.taken_at, now):
            return dated

    return from_mtime(mtime_ns, zone, now) or NONE
