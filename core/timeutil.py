"""Time, in the two shapes a photo has.

* **The instant** it was taken: naive UTC, stored as ``photos.taken_at`` and
  what everything is sorted by. "The latest photo" is a question about
  instants, whatever zone each one was taken in.
* **The wall clock** where it was taken: ``photos.taken_local``. What the day
  headings group by and what the UI prints, because a photo taken at 23:30 in
  New York was taken on that evening, not on the next morning in Berlin.

A photo that carries an offset has both. One that carries only a wall clock
(most EXIF written before 2016, most cameras still) is read in
``server.timezone``, which is the honest guess: it is where you usually are.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc

# The two files a machine writes its zone down in. Named here rather than
# inline so that a test can point them somewhere it controls.
TZ_NAME_FILE = "/etc/timezone"     # one line, the IANA name
TZ_LINK_FILE = "/etc/localtime"    # a symlink into the zoneinfo database


@lru_cache(maxsize=64)
def zone(tz_id: str | None) -> ZoneInfo:
    """A zone by name, falling back to UTC rather than raising."""
    tz_id = (tz_id or "").strip()
    if not tz_id or tz_id.upper() == "UTC":
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(tz_id)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def system_zone_name() -> str:
    """This host's IANA zone name, by the three ways it is ever written down.

    The TZ environment variable first, because it is the only one that
    survives being put in a container (docker-compose.yml passes the host's
    in); then /etc/timezone; then the symlink /etc/localtime points at. The
    same order and the same reasons as meercal's.
    """
    env = os.environ.get("TZ", "").strip()
    if env:
        return env
    try:
        with open(TZ_NAME_FILE, encoding="utf-8") as handle:
            name = handle.read().strip()
        if name:
            return name
    except OSError:
        pass
    try:
        link = os.path.realpath(TZ_LINK_FILE)
        marker = "/zoneinfo/"
        if marker in link:
            return link.split(marker, 1)[1]
    except OSError:
        pass
    return "UTC"


def home_zone(name: str) -> ZoneInfo:
    """The zone a wall clock with no offset is read in. "system" = this host's."""
    if not name or name == "system":
        return zone(system_zone_name())
    return zone(name)


def utcnow() -> datetime:
    """Now, as the naive UTC everything is stored in."""
    return datetime.now(UTC).replace(tzinfo=None)


def wall_to_utc(wall: datetime, tz: ZoneInfo) -> datetime:
    """A naive wall clock read in ``tz``, as naive UTC."""
    return wall.replace(tzinfo=tz).astimezone(UTC).replace(tzinfo=None)


def utc_to_wall(instant: datetime, tz: ZoneInfo) -> datetime:
    """A naive UTC instant as the naive wall clock it was in ``tz``."""
    return instant.replace(tzinfo=UTC).astimezone(tz).replace(tzinfo=None)


def offset_minutes(dt: datetime) -> int | None:
    """Minutes east of UTC for an aware datetime, None for a naive one."""
    off = dt.utcoffset() if dt.tzinfo else None
    if off is None:
        return None
    return int(off / timedelta(minutes=1))
