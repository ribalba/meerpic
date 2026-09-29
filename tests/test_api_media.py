"""The bytes: thumbnails, display images, originals, exports and previews.

Real files, drawn with Pillow (and pillow-heif for the HEIC) into the
temporary library, because what is under test is mostly what happens to them:
which bytes go out unchanged, which are converted, which way up a converted
photo ends up, what is left of its EXIF, and what the browser is told it may
keep.
"""

from __future__ import annotations

from io import BytesIO
from urllib.parse import quote

import pytest
from PIL import Image
from server_helpers import (
    BLUE,
    RED,
    add_file,
    image_bytes,
    library,
    make_photo,
    needs_db,
    running_app,
    session,
    write_preview,
    write_story,
    write_thumb,
)

pytestmark = needs_db


@pytest.fixture(scope="module")
def client():
    with running_app() as c:
        yield c


@pytest.fixture
def db():
    with session() as s:
        yield s


@pytest.fixture
def settings():
    """The live settings object, for a test to change with monkeypatch."""
    from core.config import get_settings

    return get_settings()

IMMUTABLE = "private, max-age=31536000, immutable"
ORIENTATION, MAKE, GPS = 0x0112, 0x010F, 0x8825
NON_ASCII = "Café／Ürlaub 1.HEIC"       # an e-acute, iCloud's fullwidth solidus, a U-umlaut
PREVIEW_BYTES = bytes(range(256)) * 16


def exif(orientation: int = 1, make: str = "TestMake", gps: bool = True) -> Image.Exif:
    e = Image.Exif()
    e[ORIENTATION] = orientation
    e[MAKE] = make
    if gps:
        g = e.get_ifd(GPS)
        g[1], g[2], g[3], g[4] = "N", (52.0, 23.0, 30.0), "E", (13.0, 3.0, 0.0)
    return e


@pytest.fixture(scope="module")
def lib(client):
    from core.database import SessionLocal

    with SessionLocal() as s:
        p = {}
        # 64x48 stored, "rotate 90" in its EXIF: 48x64 as displayed.
        p["jpeg"] = add_file(s, "IMG_1000.JPG", image_bytes(RED, (64, 48), exif=exif(6)), width=48, height=64)
        write_thumb(p["jpeg"])
        # pillow-heif writes the orientation as the HEIF's own transform, and
        # applies it (and resets the tag) when it reads the file back.
        p["heic"] = add_file(s, "IMG_1001.HEIC", image_bytes(BLUE, (40, 20), "HEIF", exif=exif(6, "HeicMake")),
                             width=20, height=40)
        p["png"] = add_file(s, "Screenshot 1.png",
                            image_bytes((255, 0, 0, 0), (64, 32), "PNG", mode="RGBA"), is_screenshot=True)
        p["big"] = add_file(s, "IMG_1002.JPG", image_bytes(RED, (1200, 800), quality=70))
        # The size column says 30 MB; only the column is asked.
        p["huge"] = add_file(s, "IMG_1007.JPG", image_bytes(BLUE, (300, 200)), size=30 * 1024 * 1024)
        p["accents"] = add_file(s, NON_ASCII, image_bytes(RED, (16, 16), "HEIF"))
        p["mov"] = add_file(s, "IMG_1003.MOV", b"\x00\x00\x00\x14ftypqt  " + bytes(500), video_codec="hevc")
        write_preview(p["mov"], PREVIEW_BYTES)
        p["mp4"] = add_file(s, "VID_1004.mp4", b"\x00\x00\x00\x18ftypmp42" + bytes(300), video_codec="h264")
        p["mov_raw"] = add_file(s, "IMG_1005.MOV", b"\x00\x00\x00\x14ftypqt  " + bytes(400), video_codec="hevc")
        p["gone"] = make_photo(s, "IMG_1006.JPG")        # a row whose file is not there
        s.commit()
        return {k: (v.id, v.sig) for k, v in p.items()}


def get(client, path, **kw):
    return client.get(path, **kw)


def original_bytes(name: str) -> bytes:
    return (library() / name).read_bytes()


def jpeg(data: bytes) -> Image.Image:
    img = Image.open(BytesIO(data))
    assert img.format == "JPEG"
    return img


# --- thumbnails ---------------------------------------------------------------


def test_a_thumbnail_is_immutable_under_its_sig(client, lib):
    pid, sig = lib["jpeg"]
    r = get(client, f"/media/thumb/{pid}?v={sig}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert r.headers["cache-control"] == IMMUTABLE


def test_a_stale_or_missing_sig_is_not_pinned(client, lib):
    pid, _ = lib["jpeg"]
    assert get(client, f"/media/thumb/{pid}?v=0000000000000000").headers["cache-control"] == "private, no-cache"
    assert get(client, f"/media/thumb/{pid}").headers["cache-control"] == "private, no-cache"


def test_a_missing_thumbnail_is_drawn_on_the_fly_and_not_kept(client, lib):
    from core.media import thumb_path

    pid, sig = lib["big"]
    r = get(client, f"/media/thumb/{pid}?v={sig}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    img = Image.open(BytesIO(r.content))
    assert img.format == "WEBP"
    assert img.size == (600, 400)                 # short edge 400, as the agent makes them
    assert not thumb_path(sig).exists()           # the agent's to write, not ours


def test_an_on_the_fly_thumbnail_is_the_right_way_up_and_never_enlarged(client, db):
    photo = add_file(db, "IMG_1010.JPG", image_bytes(RED, (64, 48), exif=exif(6)))
    db.commit()
    r = get(client, f"/media/thumb/{photo.id}")
    assert Image.open(BytesIO(r.content)).size == (48, 64)


def test_a_video_without_a_thumbnail_is_a_404(client, lib):
    r = get(client, f"/media/thumb/{lib['mov_raw'][0]}")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("path", ["/media/thumb/999999", "/media/display/999999", "/media/original/999999",
                                  "/media/export/999999", "/media/preview/999999", "/media/story/999999"])
def test_a_missing_row_is_a_json_404(client, lib, path):
    r = get(client, path)
    assert r.status_code == 404
    assert r.json()["detail"] == "No such photo"


@pytest.mark.parametrize("kind", ["thumb", "display", "original", "export"])
def test_a_missing_file_is_a_404(client, lib, kind):
    r = get(client, f"/media/{kind}/{lib['gone'][0]}")
    assert r.status_code == 404


# --- display ------------------------------------------------------------------


def test_a_browser_image_is_displayed_as_it_is(client, lib):
    pid, sig = lib["jpeg"]
    r = get(client, f"/media/display/{pid}?v={sig}")
    assert r.status_code == 200
    assert r.content == original_bytes("IMG_1000.JPG")
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == IMMUTABLE

    png = get(client, f"/media/display/{lib['png'][0]}")
    assert png.headers["content-type"] == "image/png"
    assert png.content == original_bytes("Screenshot 1.png")


def test_a_heic_is_converted_once_and_kept(client, lib):
    from core.media import display_path

    pid, sig = lib["heic"]
    r = get(client, f"/media/display/{pid}?v={sig}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == IMMUTABLE
    assert jpeg(r.content).size == (20, 40)      # the HEIF's rotation applied
    cached = display_path(sig)
    assert cached.is_file()
    stamp = cached.stat().st_mtime_ns
    again = get(client, f"/media/display/{pid}?v={sig}")
    assert again.content == r.content
    assert cached.stat().st_mtime_ns == stamp    # served from the cache, not redrawn


def test_a_huge_jpeg_is_converted_too(client, lib):
    pid, _ = lib["huge"]
    r = get(client, f"/media/display/{pid}")
    assert r.content != original_bytes("IMG_1007.JPG")
    assert jpeg(r.content).size == (300, 200)


def test_a_video_has_no_display_image(client, lib):
    assert get(client, f"/media/display/{lib['mov'][0]}").status_code == 404


# --- originals ----------------------------------------------------------------


def test_the_original_is_the_file_with_its_own_type(client, lib):
    r = get(client, f"/media/original/{lib['heic'][0]}")
    assert r.status_code == 200
    assert r.content == original_bytes("IMG_1001.HEIC")
    assert r.headers["content-type"] == "image/heic"
    assert r.headers["cache-control"] == "private, max-age=3600"
    assert "content-disposition" not in r.headers


def test_download_names_the_file(client, lib):
    r = get(client, f"/media/original/{lib['jpeg'][0]}?download=1")
    assert r.headers["content-disposition"] == 'attachment; filename="IMG_1000.JPG"'


def test_a_name_that_is_not_ascii_is_sent_both_ways(client, lib):
    r = get(client, f"/media/original/{lib['accents'][0]}?download=1")
    cd = r.headers["content-disposition"]
    assert cd.startswith('attachment; filename="Cafe_Urlaub 1.HEIC"; ')
    assert cd.endswith("filename*=UTF-8''" + quote(NON_ASCII, safe=""))


def test_an_original_answers_range_requests(client, lib):
    r = get(client, f"/media/original/{lib['mov_raw'][0]}", headers={"range": "bytes=0-9"})
    assert r.status_code == 206
    assert r.content == original_bytes("IMG_1005.MOV")[:10]
    assert r.headers["content-range"].startswith("bytes 0-9/")


# --- export -------------------------------------------------------------------


def export(client, pid, **params):
    r = get(client, f"/media/export/{pid}", params=params)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "private, no-cache"
    return r


def test_a_jpeg_with_nothing_to_change_is_sent_byte_for_byte(client, lib):
    r = export(client, lib["jpeg"][0])
    assert r.content == original_bytes("IMG_1000.JPG")
    assert r.headers["content-disposition"] == 'attachment; filename="IMG_1000.JPG"'


def test_a_smaller_export_is_turned_upright_and_keeps_its_exif(client, lib):
    r = export(client, lib["jpeg"][0], max=16)
    img = jpeg(r.content)
    assert img.size == (12, 16)                    # 64x48 rotated to 48x64, then long edge 16
    e = img.getexif()
    assert e[ORIENTATION] == 1
    assert e[MAKE] == "TestMake"
    assert e.get_ifd(GPS)                          # the location stays unless told otherwise
    assert r.headers["content-disposition"] == 'attachment; filename="IMG_1000.jpg"'


def test_a_heic_is_exported_as_an_upright_jpeg(client, lib):
    r = export(client, lib["heic"][0])
    img = jpeg(r.content)
    assert img.size == (20, 40)
    e = img.getexif()
    assert e[ORIENTATION] == 1
    assert e[MAKE] == "HeicMake"
    assert e.get_ifd(GPS)
    assert r.headers["content-disposition"] == 'attachment; filename="IMG_1001.jpg"'


def test_strip_location_removes_the_gps_and_nothing_else(client, lib, settings, monkeypatch):
    monkeypatch.setattr(settings, "share_strip_location", True)
    for key in ("jpeg", "heic"):
        img = jpeg(export(client, lib[key][0]).content)
        e = img.getexif()
        assert GPS not in e and not e.get_ifd(GPS), key
        assert e[MAKE] in ("TestMake", "HeicMake")
    # And the card no longer offers the file on disk as the thing to drag.
    card = client.get(f"/api/photos/{lib['jpeg'][0]}").json()
    assert (card["export_name"], card["export_direct"]) == ("IMG_1000.jpg", False)


def test_the_share_size_limit_applies(client, lib, settings, monkeypatch):
    monkeypatch.setattr(settings, "share_max_edge", 32)
    assert max(jpeg(export(client, lib["big"][0]).content).size) == 32
    # The smaller of the two limits wins.
    assert max(jpeg(export(client, lib["big"][0], max=20).content).size) == 20


def test_a_png_goes_as_it_is_until_it_has_to_change(client, lib):
    pid, _ = lib["png"]
    assert export(client, pid).content == original_bytes("Screenshot 1.png")
    small = jpeg(export(client, pid, max=16).content)
    # Transparent became white, not JPEG's black.
    assert small.convert("RGB").getpixel((0, 0)) == pytest.approx((255, 255, 255), abs=3)


def test_a_video_exports_its_preview_when_the_original_will_not_play(client, lib):
    r = export(client, lib["mov"][0])
    assert r.content == PREVIEW_BYTES
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["content-disposition"] == 'attachment; filename="IMG_1003.mp4"'


def test_an_h264_mp4_is_exported_as_it_is(client, lib):
    r = export(client, lib["mp4"][0])
    assert r.content == original_bytes("VID_1004.mp4")
    assert r.headers["content-disposition"] == 'attachment; filename="VID_1004.mp4"'


def test_a_video_without_a_preview_exports_the_original(client, lib):
    r = export(client, lib["mov_raw"][0])
    assert r.content == original_bytes("IMG_1005.MOV")
    assert r.headers["content-disposition"] == 'attachment; filename="IMG_1005.MOV"'


@pytest.mark.parametrize("key,name,direct", [
    ("jpeg", "IMG_1000.JPG", True),
    ("png", "Screenshot 1.png", True),
    ("heic", "IMG_1001.jpg", False),
    ("mov", "IMG_1003.mp4", False),
    ("mp4", "VID_1004.mp4", True),
    ("mov_raw", "IMG_1005.MOV", False),
])
def test_the_card_names_what_the_export_sends(client, lib, key, name, direct):
    card = client.get(f"/api/photos/{lib[key][0]}").json()
    assert (card["export_name"], card["export_direct"]) == (name, direct)


# --- previews -----------------------------------------------------------------


def test_a_preview_is_served_with_ranges(client, lib):
    pid, sig = lib["mov"]
    r = get(client, f"/media/preview/{pid}?v={sig}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["cache-control"] == IMMUTABLE
    assert r.headers["accept-ranges"] == "bytes"
    part = get(client, f"/media/preview/{pid}?v={sig}", headers={"range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.content == PREVIEW_BYTES[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(PREVIEW_BYTES)}"


def test_no_preview_is_a_404(client, lib):
    assert get(client, f"/media/preview/{lib['mov_raw'][0]}").status_code == 404
    assert get(client, f"/media/preview/{lib['jpeg'][0]}").status_code == 404


# --- hovering a video ---------------------------------------------------------


def test_a_storyboard_is_immutable_under_its_sig(client, db):
    from core.models import Photo

    video = add_file(db, "IMG_1020.MOV", b"\x00\x00\x00\x14ftypqt  " + bytes(100), video_codec="hevc")
    db.commit()
    assert get(client, f"/media/story/{video.id}?v={video.sig}").status_code == 404   # not made yet
    write_story(db.get(Photo, video.id))
    db.commit()
    r = get(client, f"/media/story/{video.id}?v={video.sig}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert r.headers["cache-control"] == IMMUTABLE
    assert Image.open(BytesIO(r.content)).size == (320, 180)
    assert get(client, f"/media/story/{video.id}").headers["cache-control"] == "private, no-cache"


def test_a_photo_has_no_storyboard(client, lib):
    assert get(client, f"/media/story/{lib['jpeg'][0]}").status_code == 404


def test_a_video_card_offers_what_hovering_it_can_show(client, db):
    from core.models import Photo

    video = add_file(db, "IMG_1021.MOV", b"\x00\x00\x00\x14ftypqt  " + bytes(100), video_codec="hevc")
    db.commit()
    card = client.get(f"/api/photos/{video.id}").json()
    assert (card["preview"], card["story"], card["story_frames"]) == (None, None, 0)

    row = db.get(Photo, video.id)
    write_preview(row)
    write_story(row, frames=7)
    db.commit()
    card = client.get(f"/api/photos/{video.id}").json()
    assert card["preview"] == f"/media/preview/{video.id}?v={video.sig}"
    assert card["story"] == f"/media/story/{video.id}?v={video.sig}"
    assert card["story_frames"] == 7

    # Made for an older version of the file: neither is offered.
    row.story_sig = row.preview_sig = "0000000000000000"
    db.commit()
    card = client.get(f"/api/photos/{video.id}").json()
    assert (card["preview"], card["story"]) == (None, None)

    # A strip with no frames in it is no strip.
    row.story_sig, row.story_frames = row.sig, 0
    db.commit()
    assert client.get(f"/api/photos/{video.id}").json()["story"] is None


def test_a_live_photo_s_motion_is_not_a_hover_preview(client, db):
    # Its motion plays from the viewer's LIVE button; on the grid it is a photo.
    from core.models import Photo

    motion = make_photo(db, "IMG_1022.MOV", is_companion=True)
    still = make_photo(db, "IMG_1022.HEIC", live_video_id=motion.id)
    write_preview(db.get(Photo, motion.id))
    db.commit()
    card = client.get(f"/api/photos/{still.id}").json()
    assert (card["preview"], card["story"]) == (None, None)


# --- the gate -----------------------------------------------------------------


def test_media_needs_the_session_cookie_an_img_tag_sends(client, lib, monkeypatch):
    from fastapi.testclient import TestClient

    from app import security
    from app.main import app

    monkeypatch.setattr(security.settings, "server_password", "hunter2")
    monkeypatch.setattr(security.settings, "trusted_proxies", [])
    pid, sig = lib["jpeg"]
    browser = TestClient(app, base_url="https://photos.example.com")   # no lifespan: already up
    assert browser.get(f"/media/thumb/{pid}?v={sig}").status_code == 401
    assert browser.post("/api/auth/login", json={"password": "hunter2"}).status_code == 200
    r = browser.get(f"/media/thumb/{pid}?v={sig}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
