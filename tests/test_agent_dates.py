"""Dates: every source in its priority, every file-name pattern, and the
values cameras write that must not be believed.

All pure; the home zone is Europe/Berlin (+01:00 in winter, +02:00 in
summer) and "now" is fixed, so nothing here depends on the machine.
"""

from __future__ import annotations

# Naive datetimes on purpose: meerpic stores naive UTC and naive wall clocks.
# ruff: noqa: DTZ001
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent import dates
from core.models import UNDATED

BERLIN = ZoneInfo("Europe/Berlin")
NNBSP = chr(0x202F)           # macOS: "at 1.34.56 PM"
NOW = datetime(2026, 9, 29, 12, 0, 0)


def resolve(tags=None, *, kind="photo", name="IMG_0001.HEIC", mtime=None):
    mtime_ns = None
    if mtime is not None:
        mtime_ns = int((mtime - datetime(1970, 1, 1)).total_seconds() * 1e9)
    return dates.resolve(tags or {}, kind=kind, name=name, mtime_ns=mtime_ns, zone=BERLIN, now=NOW)


# --- parsing ------------------------------------------------------------------------


@pytest.mark.parametrize("value, wall, offset", [
    ("2026:09:22 10:52:17", datetime(2026, 9, 22, 10, 52, 17), None),
    ("2026:09:22 10:52:21+02:00", datetime(2026, 9, 22, 10, 52, 21), 120),
    ("2021:12:04 13:33:41+01:00", datetime(2021, 12, 4, 13, 33, 41), 60),
    ("2026-09-22T10:52:17", datetime(2026, 9, 22, 10, 52, 17), None),
    ("2026-09-22T10:52:17.123-04:00", datetime(2026, 9, 22, 10, 52, 17), -240),
    ("2026:09:22 10:52:17.5+0530", datetime(2026, 9, 22, 10, 52, 17), 330),
    ("2026:09:22 10:52:17Z", datetime(2026, 9, 22, 10, 52, 17), dates.UTC_MARK),
    ("2012:05:03", datetime(2012, 5, 3), None),
    ("2012:05:03 12:08", datetime(2012, 5, 3, 12, 8), None),
    ("  2012:05:03 12:08:08\x00", datetime(2012, 5, 3, 12, 8, 8), None),
])
def test_parse_stamp_accepts_what_writers_write(value, wall, offset):
    assert dates.parse_stamp(value) == (wall, offset)


@pytest.mark.parametrize("value", [
    None, "", "0000:00:00 00:00:00", "    :  :     :  :  ", "2026:13:01 10:00:00",
    "2026:02:30 10:00:00", "2026:01:01 25:00:00", "garbage", "2026:01:01 10:00:00+99:00", True,
])
def test_parse_stamp_rejects_nonsense(value):
    assert dates.parse_stamp(value) is None


@pytest.mark.parametrize("value, expected", [
    ("+02:00", 120), ("-04:00", -240), ("+0530", 330), ("+05", 300), ("-00:30", -30),
    ("Z", dates.UTC_MARK), (None, None), ("", None), ("+25:00", None), ("2", None),
])
def test_parse_offset(value, expected):
    assert dates.parse_offset(value) == expected


# --- 1. QuickTime CreationDate ------------------------------------------------------


def test_quicktime_creation_date_with_offset_wins():
    d = resolve({
        "CreationDate": "2026:09:22 10:52:21+02:00",
        "DateTimeOriginal": "2020:01:01 00:00:00",
        "CreateDate": "2026:09:22 08:52:22",
    }, kind="video")
    assert d.source == "quicktime"
    assert d.taken_local == datetime(2026, 9, 22, 10, 52, 21)
    assert d.tz_offset == 120
    assert d.taken_at == datetime(2026, 9, 22, 8, 52, 21)
    assert d.sort_at == d.taken_at


def test_quicktime_creation_date_in_another_zone_keeps_its_wall_clock():
    d = resolve({"CreationDate": "2024:03:10 09:15:00-05:00"}, kind="video")
    assert d.taken_local == datetime(2024, 3, 10, 9, 15)
    assert d.taken_at == datetime(2024, 3, 10, 14, 15)
    assert d.tz_offset == -300


def test_quicktime_creation_date_without_offset_is_a_home_wall_clock():
    d = resolve({"CreationDate": "2024:01:15 10:00:00"}, kind="video")
    assert d.source == "quicktime"
    assert d.tz_offset is None
    assert d.taken_at == datetime(2024, 1, 15, 9, 0)       # Berlin winter, +1


def test_creation_date_beats_a_different_createdate_after_an_edit():
    # CreateDate is when the edited file was written; CreationDate is the moment.
    d = resolve({"CreationDate": "2023:06:01 18:00:00+02:00", "CreateDate": "2023:06:05 10:00:00"},
                kind="video")
    assert d.taken_at == datetime(2023, 6, 1, 16, 0)


# --- 2. EXIF DateTimeOriginal ---------------------------------------------------------


def test_exif_with_offset_time_original():
    d = resolve({"DateTimeOriginal": "2026:09:22 10:52:17", "OffsetTimeOriginal": "+02:00",
                 "SubSecTimeOriginal": 673})
    assert d.source == "exif"
    assert d.taken_local == datetime(2026, 9, 22, 10, 52, 17)
    assert d.taken_at == datetime(2026, 9, 22, 8, 52, 17)
    assert d.tz_offset == 120


def test_exif_falls_back_to_offset_time():
    d = resolve({"DateTimeOriginal": "2023:07:14 18:30:05", "OffsetTime": "-04:00"})
    assert d.tz_offset == -240
    assert d.taken_at == datetime(2023, 7, 14, 22, 30, 5)


def test_exif_offset_time_original_beats_offset_time():
    d = resolve({"DateTimeOriginal": "2023:07:14 18:30:05", "OffsetTimeOriginal": "+09:00",
                 "OffsetTime": "-04:00"})
    assert d.tz_offset == 540


def test_exif_without_offset_is_read_in_the_home_zone_summer_and_winter():
    summer = resolve({"DateTimeOriginal": "2024:07:01 12:00:00"})
    assert summer.tz_offset is None
    assert summer.taken_local == datetime(2024, 7, 1, 12, 0)
    assert summer.taken_at == datetime(2024, 7, 1, 10, 0)
    winter = resolve({"DateTimeOriginal": "2024:01:01 12:00:00"})
    assert winter.taken_at == datetime(2024, 1, 1, 11, 0)


def test_exif_with_its_own_offset_in_the_value():
    d = resolve({"DateTimeOriginal": "2021:12:04 13:33:41+01:00", "OffsetTimeOriginal": "+05:00"})
    assert d.tz_offset == 60


def test_exif_z_suffix_is_an_instant():
    d = resolve({"DateTimeOriginal": "2024:07:01 10:00:00Z"})
    assert d.taken_at == datetime(2024, 7, 1, 10, 0)
    assert d.taken_local == datetime(2024, 7, 1, 12, 0)     # shown as Berlin time
    assert d.tz_offset is None


def test_a_bad_offset_tag_is_ignored_not_fatal():
    d = resolve({"DateTimeOriginal": "2024:07:01 12:00:00", "OffsetTimeOriginal": "garbage"})
    assert d.source == "exif"
    assert d.tz_offset is None


# --- 3. XMP ----------------------------------------------------------------------------


def test_xmp_date_created():
    d = resolve({"DateCreated": "2012:05:03 12:08:08"})
    assert d.source == "xmp"
    assert d.taken_local == datetime(2012, 5, 3, 12, 8, 8)


def test_xmp_date_time_created_when_date_created_is_missing():
    d = resolve({"DateTimeCreated": "2012:05:03 12:08:08+02:00"})
    assert d.source == "xmp"
    assert d.tz_offset == 120


def test_xmp_bare_date_is_midnight_wall_clock():
    d = resolve({"DateCreated": "2012:05:03"})
    assert d.taken_local == datetime(2012, 5, 3, 0, 0)


# --- 4. CreateDate ------------------------------------------------------------------------


def test_createdate_in_an_image_is_a_wall_clock():
    d = resolve({"CreateDate": "2024:07:01 12:00:00"}, kind="photo")
    assert d.source == "createdate"
    assert d.taken_local == datetime(2024, 7, 1, 12, 0)
    assert d.taken_at == datetime(2024, 7, 1, 10, 0)


def test_createdate_in_a_video_is_utc():
    d = resolve({"CreateDate": "2026:09:22 08:52:22"}, kind="video")
    assert d.source == "createdate"
    assert d.taken_at == datetime(2026, 9, 22, 8, 52, 22)
    assert d.taken_local == datetime(2026, 9, 22, 10, 52, 22)
    assert d.tz_offset is None


def test_createdate_of_zeroes_falls_through():
    d = resolve({"CreateDate": "0000:00:00 00:00:00"}, kind="video", name="clip.mov")
    assert d.source == "none"


# --- 5. file names --------------------------------------------------------------------------


@pytest.mark.parametrize("name, wall", [
    ("IMG_20240101_123456.jpg", datetime(2024, 1, 1, 12, 34, 56)),
    ("IMG_20240101_123456_1.jpg", datetime(2024, 1, 1, 12, 34, 56)),
    ("VID_20240101_123456.mp4", datetime(2024, 1, 1, 12, 34, 56)),
    ("PXL_20240101_123456789.jpg", datetime(2024, 1, 1, 12, 34, 56)),
    ("PXL_20240101_123456789.MP.jpg", datetime(2024, 1, 1, 12, 34, 56)),
    ("MVIMG_20240101_123456.jpg", datetime(2024, 1, 1, 12, 34, 56)),
    ("20240101_123456.jpg", datetime(2024, 1, 1, 12, 34, 56)),
    ("Screenshot_20240101-123456.png", datetime(2024, 1, 1, 12, 34, 56)),
    ("Screenshot_20240101-123456_Chrome.jpg", datetime(2024, 1, 1, 12, 34, 56)),
    ("2012-05-03 12.08.08.jpg", datetime(2012, 5, 3, 12, 8, 8)),
    ("Screenshot 2024-01-01 at 12.34.56.png", datetime(2024, 1, 1, 12, 34, 56)),
    ("Screenshot 2024-01-01 at 1.34.56" + NNBSP + "PM.png", datetime(2024, 1, 1, 13, 34, 56)),
    ("Screenshot 2024-01-01 at 12.05.00 AM.png", datetime(2024, 1, 1, 0, 5, 0)),
    ("WhatsApp Image 2024-01-01 at 12.34.56.jpeg", datetime(2024, 1, 1, 12, 34, 56)),
    ("WhatsApp Image 2024-01-01 at 12.34.56 (1).jpeg", datetime(2024, 1, 1, 12, 34, 56)),
    ("WhatsApp Video 2024-01-01 at 12.34.56.mp4", datetime(2024, 1, 1, 12, 34, 56)),
    ("signal-2024-01-01-123456.jpg", datetime(2024, 1, 1, 12, 34, 56)),
    ("signal-2024-01-01-12-34-56-789.jpg", datetime(2024, 1, 1, 12, 34, 56)),
    ("Bildschirmfoto 2024-01-01 um 12.34.56.png", datetime(2024, 1, 1, 12, 34, 56)),
    ("sub/dir/IMG_20231231_235959.jpg", datetime(2023, 12, 31, 23, 59, 59)),
    ("2022-03-07_19-03-19_000.jpeg", datetime(2022, 3, 7, 19, 3, 19)),
    ("2022-03-07_19-03-34_000 (2022-03-07T18_28_58.108).jpeg", datetime(2022, 3, 7, 19, 3, 34)),
])
def test_filename_patterns(name, wall):
    assert dates.from_filename(name) == wall


@pytest.mark.parametrize("name", [
    "IMG_5700.PNG",
    "c3e65ce8-457b-4a37-934d-162bc851fece.jpg",
    "IMG_5695_AWs／2CjGbWObttBlw5UGhaBBu7Dh.HEIC",
    "IMG_20241399_123456.jpg",               # month 13
    "IMG_20240101_256060.jpg",               # 25:60:60
    "photo.jpg",
    "2022-03-07_19.03-19.jpg",               # mixed separators
])
def test_filename_without_a_date(name):
    assert dates.from_filename(name) is None


def test_filename_date_is_a_home_wall_clock():
    d = resolve({}, name="IMG_20240701_120000.jpg", mtime=datetime(2025, 1, 1))
    assert d.source == "filename"
    assert d.taken_local == datetime(2024, 7, 1, 12, 0)
    assert d.taken_at == datetime(2024, 7, 1, 10, 0)
    assert d.tz_offset is None


def test_filename_date_in_the_future_falls_through_to_mtime():
    d = resolve({}, name="IMG_20990101_120000.jpg", mtime=datetime(2025, 1, 1))
    assert d.source == "mtime"


def test_filename_year_before_1900_is_not_a_date():
    d = resolve({}, name="12340101_120000.jpg", mtime=datetime(2025, 1, 1))
    assert d.source == "mtime"


# --- 6. mtime, 7. nothing -----------------------------------------------------------------


def test_mtime_is_an_instant_shown_as_home_time():
    d = resolve({}, name="c3e65ce8-457b-4a37-934d-162bc851fece.jpg", mtime=datetime(2025, 6, 1, 10, 0))
    assert d.source == "mtime"
    assert d.taken_at == datetime(2025, 6, 1, 10, 0)
    assert d.taken_local == datetime(2025, 6, 1, 12, 0)
    assert d.tz_offset is None


def test_mtime_before_1971_is_absent():
    d = resolve({}, name="x.jpg", mtime=datetime(1970, 1, 1, 0, 0, 5))
    assert d == dates.NONE
    assert d.taken_at is None and d.taken_local is None
    assert d.sort_at == UNDATED
    assert d.columns()["date_source"] == "none"


def test_mtime_in_the_future_is_absent():
    assert resolve({}, name="x.jpg", mtime=NOW + timedelta(days=3)).source == "none"


def test_nothing_at_all_is_undated():
    d = resolve({}, name="x.jpg")
    assert d.source == "none"
    assert d.sort_at == UNDATED


def test_from_mtime_directly():
    ns = int((datetime(2020, 2, 2, 2, 2, 2) - datetime(1970, 1, 1)).total_seconds()) * 10**9
    d = dates.from_mtime(ns, BERLIN, NOW)
    assert d.taken_at == datetime(2020, 2, 2, 2, 2, 2)
    assert dates.from_mtime(None, BERLIN, NOW) is None
    assert dates.from_mtime(0, BERLIN, NOW) is None


# --- plausibility ---------------------------------------------------------------------------


def test_implausible_values_fall_through_to_the_next_source():
    d = resolve({
        "CreationDate": "0000:00:00 00:00:00",
        "DateTimeOriginal": "1899:12:31 23:59:59",
        "DateCreated": "2099:01:01 00:00:00",
        "CreateDate": "2019:05:05 05:05:05",
    })
    assert d.source == "createdate"
    assert d.taken_local == datetime(2019, 5, 5, 5, 5, 5)


def test_a_day_of_slack_for_clocks_ahead():
    tomorrow = (NOW + timedelta(hours=20)).strftime("%Y:%m:%d %H:%M:%S")
    assert resolve({"DateTimeOriginal": tomorrow + "+00:00"}).source == "exif"
    later = (NOW + timedelta(days=2)).strftime("%Y:%m:%d %H:%M:%S")
    assert resolve({"DateTimeOriginal": later + "+00:00"}).source == "none"


def test_columns_shape():
    cols = resolve({"DateTimeOriginal": "2026:09:22 10:52:17", "OffsetTimeOriginal": "+02:00"}).columns()
    assert set(cols) == {"taken_at", "taken_local", "tz_offset", "date_source", "sort_at"}
    assert cols["sort_at"] == cols["taken_at"]


def test_non_string_values_do_not_crash():
    d = resolve({"DateTimeOriginal": 12345, "CreateDate": ["x"], "OffsetTime": 2})
    assert d.source == "none"
