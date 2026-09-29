"""Metadata: exiftool's records to columns, and exiftool itself on real files.

The recorded records are what ``exiftool -j -n`` said about files in the real
library (a Live Photo's HEIC and MOV, an iPhone screenshot, an app's JPEG
with nothing in it), trimmed to the tags the agent asks for.
"""

from __future__ import annotations

# Naive datetimes on purpose: meerpic stores naive UTC and naive wall clocks.
# ruff: noqa: DTZ001
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from agent_helpers import jpeg, needs_exiftool

from agent import meta

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 29, 12, 0)

HEIC = {
    "SourceFile": "IMG_0001_AdAIUla9dmMVmwPwG／1／ZiISxnx8.HEIC",
    "DateTimeOriginal": "2026:09:22 10:52:17", "OffsetTimeOriginal": "+02:00",
    "OffsetTime": "+02:00", "SubSecTimeOriginal": 673, "DateCreated": "2026:09:22 10:52:17",
    "CreateDate": "2026:09:22 10:52:17",
    "GPSLatitude": 52.4318083333333, "GPSLongitude": 12.9640166666667, "GPSAltitude": 33.05382111,
    "Make": "Apple", "Model": "iPhone 13", "LensModel": "iPhone 13 back dual wide camera 5.1mm f/1.6",
    "ISO": 640, "FNumber": 1.6, "ExposureTime": 0.02702702703, "FocalLength": 5.1,
    "FocalLengthIn35mmFormat": 26, "ImageWidth": 4032, "ImageHeight": 3024,
    "ExifImageWidth": 4032, "ExifImageHeight": 3024, "Orientation": 6, "Rotation": 3,
    "Software": "26.6.2", "Flash": 16, "HDRHeadroom": 0.5, "LensMake": "Apple",
    "ContentIdentifier": "E7CC542A-580B-4D65-A406-ABE1F2241954",
}
MOV = {
    "SourceFile": "IMG_0001_AdAIUla9dmMVmwPwG／1／ZiISxnx8.MOV",
    "ContentIdentifier": "E7CC542A-580B-4D65-A406-ABE1F2241954",
    "CreationDate": "2024:11:10 11:28:16+01:00", "CreateDate": "2024:11:10 10:28:17",
    "GPSLatitude": 52.4319, "GPSLongitude": 12.9641, "GPSAltitude": 33.061,
    "Make": "Apple", "Model": "iPhone 13", "ImageWidth": 1920, "ImageHeight": 1440,
    "Rotation": 90, "Duration": 2.91166666666667, "CompressorID": "hvc1",
    "Software": "18.1", "VideoFrameRate": 30, "AvgBitrate": 13853603,
}
SCREENSHOT = {
    "SourceFile": "IMG_5700.PNG", "DateTimeOriginal": "2026:09:22 11:12:27",
    "DateCreated": "2026:09:22 11:12:27", "ImageWidth": 1170, "ImageHeight": 2532,
    "ExifImageWidth": 1170, "ExifImageHeight": 2532, "Orientation": 1, "UserComment": "Screenshot",
}
BARE = {"SourceFile": "c3e65ce8-457b-4a37-934d-162bc851fece.jpg", "ImageWidth": 946, "ImageHeight": 2048}


def extract(rec, kind="photo", name=None, mtime_ns=None):
    return meta.extract(rec, kind=kind, name=name or rec["SourceFile"], mtime_ns=mtime_ns,
                        zone=BERLIN, now=NOW)


def test_heic_record():
    c = extract(HEIC)
    assert c["date_source"] == "exif"
    assert c["taken_at"] == datetime(2026, 9, 22, 8, 52, 17)
    assert c["tz_offset"] == 120
    # Orientation 6 and irot 3 are the same quarter turn: swapped once.
    assert (c["width"], c["height"]) == (3024, 4032)
    assert (c["lat"], c["lon"]) == (pytest.approx(52.4318, abs=1e-4), pytest.approx(12.9640, abs=1e-4))
    assert c["altitude"] == pytest.approx(33.05, abs=0.01)
    assert (c["make"], c["model"]) == ("Apple", "iPhone 13")
    assert c["lens"].startswith("iPhone 13 back")
    assert c["iso"] == 640 and c["f_number"] == 1.6 and c["focal_length"] == 5.1
    assert c["exposure_time"] == pytest.approx(1 / 37, rel=1e-3)
    assert c["content_id"] == "E7CC542A-580B-4D65-A406-ABE1F2241954"
    assert c["video_codec"] == "" and c["duration"] is None
    assert c["is_screenshot"] is False
    assert c["exif"] == {
        "Software": "26.6.2", "LensMake": "Apple", "FocalLengthIn35mmFormat": 26, "Flash": 16,
        "HDRHeadroom": 0.5, "OffsetTimeOriginal": "+02:00", "SubSecTimeOriginal": 673,
    }


def test_mov_record():
    c = extract(MOV, kind="video")
    assert c["date_source"] == "quicktime"
    assert c["taken_local"] == datetime(2024, 11, 10, 11, 28, 16)
    assert c["taken_at"] == datetime(2024, 11, 10, 10, 28, 16)
    assert (c["width"], c["height"]) == (1440, 1920)
    assert c["duration"] == pytest.approx(2.9117, abs=1e-3)
    assert c["video_codec"] == "hevc"
    assert c["content_id"] == HEIC["ContentIdentifier"]
    assert c["exif"] == {"Software": "18.1", "VideoFrameRate": 30, "AvgBitrate": 13853603}


def test_screenshot_record():
    c = extract(SCREENSHOT)
    assert c["is_screenshot"] is True
    assert (c["width"], c["height"]) == (1170, 2532)
    assert c["date_source"] == "exif"
    assert c["lat"] is None


def test_bare_record_falls_back_to_mtime():
    ns = int((datetime(2025, 3, 3, 3, 3, 3) - datetime(1970, 1, 1)).total_seconds()) * 10**9
    c = extract(BARE, mtime_ns=ns)
    assert c["date_source"] == "mtime"
    assert c["taken_at"] == datetime(2025, 3, 3, 3, 3, 3)
    assert (c["width"], c["height"]) == (946, 2048)
    assert c["exif"] == {} and c["make"] == "" and c["iso"] is None


@pytest.mark.parametrize("kind, orientation, rotation, expected", [
    ("photo", None, None, False),
    ("photo", 1, None, False),
    ("photo", 3, None, False),       # upside down, same shape
    ("photo", 5, None, True), ("photo", 6, None, True), ("photo", 7, None, True), ("photo", 8, None, True),
    ("photo", None, 1, True), ("photo", None, 3, True),  # HEIF irot quarter turns
    ("photo", None, 2, False),
    ("photo", 6, 3, True),           # both, one turn
    ("video", None, 90, True), ("video", None, 270, True), ("video", None, -90, True),
    ("video", None, 180, False), ("video", None, 0, False),
    ("video", None, 1, False),       # a video's rotation is degrees, not quarters
])
def test_rotated(kind, orientation, rotation, expected):
    assert meta.rotated(kind, orientation, rotation) is expected


def test_dimensions_prefers_image_width_and_falls_back_to_exif():
    assert meta.dimensions({"ExifImageWidth": 100, "ExifImageHeight": 50}, "photo") == (100, 50)
    assert meta.dimensions({"ImageWidth": 0, "ImageHeight": 50}, "photo") == (None, None)
    assert meta.dimensions({}, "photo") == (None, None)


@pytest.mark.parametrize("rec, expected", [
    ({"GPSLatitude": 0.0, "GPSLongitude": 0.0}, (None, None, None)),
    ({"GPSLatitude": 91.0, "GPSLongitude": 10.0}, (None, None, None)),
    ({"GPSLatitude": 10.0}, (None, None, None)),
    ({"GPSLatitude": "52.5", "GPSLongitude": "13.4", "GPSAltitude": -3.5}, (52.5, 13.4, -3.5)),
    ({"GPSLatitude": -33.86, "GPSLongitude": -70.65}, (-33.86, -70.65, None)),
])
def test_gps(rec, expected):
    assert meta.gps(rec) == expected


@pytest.mark.parametrize("raw, codec", [
    ("hvc1", "hevc"), ("hev1", "hevc"), ("avc1", "h264"), ("AVC1", "h264"), ("mp4v", "mp4v"), ("", ""),
])
def test_video_codec(raw, codec):
    assert meta.video_codec({"CompressorID": raw}) == codec


@pytest.mark.parametrize("name, comment, expected", [
    ("IMG_1.PNG", "Screenshot", True),
    ("IMG_1.PNG", "screenshot ", True),
    ("Screenshot 2024-01-01 at 12.34.56.png", None, True),
    ("Bildschirmfoto 2024-01-01 um 12.34.56.png", None, True),
    ("Screen Shot 2019-01-01 at 10.00.00.png", None, True),
    ("Screenshot_20240101-123456.png", None, True),
    ("IMG_1.PNG", "Nice day", False),
    ("IMG_1.JPG", None, False),
])
def test_is_screenshot(name, comment, expected):
    rec = {"UserComment": comment} if comment is not None else {}
    assert meta.is_screenshot(rec, name) is expected


def test_odd_values_are_cleaned():
    c = extract({"SourceFile": "x.jpg", "Make": "Can\x00on ", "Model": ["a"], "ISO": "100 200",
                 "FNumber": "nan", "Software": "x" * 900})
    assert c["make"] == "Canon"
    assert c["model"] == ""
    assert c["iso"] == 100
    assert c["f_number"] is None
    assert len(c["exif"]["Software"]) == 500


# --- exiftool itself --------------------------------------------------------------------


@needs_exiftool
def test_exiftool_on_generated_files_with_odd_names(tmp_path):
    odd = tmp_path / "IMG_5695_AWs／2Cj, x+y.jpg"
    jpeg(odd, size=(64, 48), date="2023:07:14 18:30:05", offset="-04:00",
         gps=(-33.86, -70.65), orientation=6, make="TestMake", model="TestModel")
    plain = jpeg(tmp_path / "plain.jpg")
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(bytes(range(256)) * 20)
    missing = str(tmp_path / "missing.jpg")

    out = meta.read_many([str(odd), str(plain), str(broken), missing], "exiftool", processes=2)
    assert missing not in out
    assert "Error" in out[str(broken)]
    rec = out[str(odd)]
    c = meta.extract(rec, kind="photo", name=odd.name, mtime_ns=None, zone=BERLIN, now=NOW)
    assert c["date_source"] == "exif"
    assert c["tz_offset"] == -240
    assert c["taken_at"] == datetime(2023, 7, 14, 22, 30, 5)
    assert (c["width"], c["height"]) == (48, 64)
    assert c["lat"] == pytest.approx(-33.86, abs=1e-3)
    assert c["lon"] == pytest.approx(-70.65, abs=1e-3)
    assert c["make"] == "TestMake"
    assert "Error" not in out[str(plain)]


@needs_exiftool
def test_exiftool_reads_user_comment_and_create_date(tmp_path):
    path = jpeg(tmp_path / "shot.jpg", user_comment="Screenshot", create_date="2020:02:02 10:00:00")
    rec = meta.run_exiftool([str(path)])[str(path)]
    c = meta.extract(rec, kind="photo", name=path.name, mtime_ns=None, zone=BERLIN, now=NOW)
    assert c["is_screenshot"] is True
    assert c["date_source"] == "createdate"
    assert c["taken_local"] == datetime(2020, 2, 2, 10, 0)


def test_run_exiftool_missing_binary():
    with pytest.raises(meta.ExiftoolError, match="not found"):
        meta.run_exiftool(["/nonexistent.jpg"], exiftool="/nonexistent/exiftool")


def test_read_many_isolates_a_failing_chunk(monkeypatch):
    calls = []

    def fake(paths, exiftool="exiftool"):
        calls.append(list(paths))
        if "bad" in paths:
            raise meta.ExiftoolError("exiftool died")
        return {p: {"SourceFile": p} for p in paths}

    monkeypatch.setattr(meta, "run_exiftool", fake)
    out = meta.read_many(["a", "b", "bad", "c"], processes=1)
    assert isinstance(out["bad"], meta.ExiftoolError)
    assert out["a"] == {"SourceFile": "a"} and out["c"] == {"SourceFile": "c"}
    assert ["a"] in calls and ["bad"] in calls       # retried one at a time


def test_read_many_empty():
    assert meta.read_many([]) == {}
