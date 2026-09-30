"""The grid's listing in date mode, its filters, one photo, and the timeline.

A small library with the awkward cases in it: a Live Photo and its hidden
companion, two photos taken in the same second, undated files, a New Year's
Eve photo from New York that is already next year in Berlin, and places on
both sides of the antimeridian.
"""

from __future__ import annotations

import pytest
from server_helpers import (
    make_photo,
    needs_db,
    running_app,
    session,
    wall,
    write_preview,
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

POTSDAM = {
    "place": "Potsdam, Brandenburg, Germany", "city": "Potsdam", "region": "Brandenburg",
    "country": "Germany", "country_code": "DE", "lat": 52.3906, "lon": 13.0645,
}

CARD_KEYS = {
    "id", "kind", "name", "taken", "day", "date_source", "w", "h", "duration", "live",
    "screenshot", "place", "city", "lat", "lon", "thumb", "path", "export", "export_name",
    "export_direct", "favorite", "nsfw", "nsfw_flag", "nsfw_safe", "edited", "preview", "story",
    "story_frames",
}
DETAIL_KEYS = CARD_KEYS | {
    "root", "rel_path", "size", "mime", "ext", "taken_utc", "tz_offset", "camera", "exposure",
    "exif", "location", "video_codec", "live_video_id", "urls", "preview_ready", "error",
    "albums", "added_at", "original_id", "hidden", "icloud_deleted", "deletable",
    "faces", "faces_state", "nsfw_like", "text_lines", "text_state",
}


@pytest.fixture(scope="module")
def lib(client):
    from core.database import SessionLocal

    with SessionLocal() as s:
        p = {}
        p["jul22"] = make_photo(
            s, "IMG_0001.HEIC", taken=wall(2024, 7, 22, 10), make="Apple", model="iPhone 15 Pro",
            lens="iPhone 15 Pro back camera 6.765mm f/1.78", iso=50, f_number=1.78,
            exposure_time=1 / 120, focal_length=6.765, altitude=35.0, exif={"Software": "17.5"},
            **POTSDAM,
        )
        p["jul01"] = make_photo(s, "IMG_0002.JPG", taken=wall(2024, 7, 1, 9), make="Canon", model="EOS R5")
        p["shot"] = make_photo(s, "Screenshot 2024-06-15 at 12.00.00.png", taken=wall(2024, 6, 15, 12),
                               is_screenshot=True, date_source="filename")
        p["near"] = make_photo(s, "IMG_0010.JPG", taken=wall(2024, 6, 1, 12), lat=52.3951, lon=13.0645)
        p["far"] = make_photo(s, "IMG_0011.JPG", taken=wall(2024, 5, 30, 12), lat=52.4176, lon=13.0645)
        p["video"] = make_photo(s, "IMG_0003.MOV", taken=wall(2024, 5, 5, 18), duration=12.5,
                                video_codec="hevc", width=1920, height=1080)
        p["companion"] = make_photo(s, "IMG_0004.MOV", taken=wall(2024, 5, 1, 8), is_companion=True,
                                    duration=2.5, video_codec="hevc", content_id="live-1")
        p["live"] = make_photo(s, "IMG_0004.HEIC", taken=wall(2024, 5, 1, 8), content_id="live-1",
                               live_video_id=p["companion"].id)
        p["fiji_e"] = make_photo(s, "IMG_0020.JPG", taken=wall(2024, 4, 2, 12), lat=-17.8, lon=179.5)
        p["fiji_w"] = make_photo(s, "IMG_0021.JPG", taken=wall(2024, 4, 1, 12), lat=-16.5, lon=-179.8)
        p["tie_a"] = make_photo(s, "IMG_0030.JPG", taken=wall(2024, 3, 10, 12))
        p["tie_b"] = make_photo(s, "IMG_0031.JPG", taken=wall(2024, 3, 10, 12))
        p["nothumb"] = make_photo(s, "IMGA0001.JPG", taken=wall(2024, 2, 2, 12), thumb_sig="")
        # 23:30 on New Year's Eve in New York is 04:30 UTC on the 1st, which
        # is later than 01:00 on the 1st in Berlin (00:00 UTC).
        p["nye_ny"] = make_photo(s, "IMG_0040.JPG", taken=wall(2023, 12, 31, 23, 30), tz_offset=-300,
                                 place="New York, New York, United States", city="New York",
                                 lat=40.7128, lon=-74.006)
        p["jan1"] = make_photo(s, "IMG_0041.JPG", taken=wall(2024, 1, 1, 1, 0))
        p["old"] = make_photo(s, "DSC_0001.JPG", taken=wall(2015, 3, 3, 15), make="NIKON CORPORATION",
                              model="NIKON D90")
        p["undated_a"] = make_photo(s, "a1b2c3d4.jpg", taken=None)
        p["undated_b"] = make_photo(s, "e5f6a7b8.png", taken=None)
        s.commit()
        return {k: v.id for k, v in p.items()}


# The listing's order, newest instant first, as the fixture defines it.
ORDER = [
    "jul22", "jul01", "shot", "near", "far", "video", "live", "fiji_e", "fiji_w",
    "tie_b", "tie_a", "nothumb", "nye_ny", "jan1", "old", "undated_b", "undated_a",
]


def photos(client, **params) -> dict:
    r = client.get("/api/photos", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def ids(page) -> list[int]:
    return [c["id"] for c in page["items"]]


def names(lib, page) -> list[str]:
    by_id = {v: k for k, v in lib.items()}
    return [by_id[i] for i in ids(page)]


# --- date mode ----------------------------------------------------------------


def test_the_first_page_is_newest_first_with_the_exact_total(client, lib):
    page = photos(client)
    assert page["mode"] == "date"
    assert names(lib, page) == ORDER
    assert page["total"] == len(ORDER)
    assert page["next"] is None
    assert page["similar_to"] is None
    assert page["query"] == {"text": "", "similar": None, "face": None, "filters": [], "errors": []}


def test_a_card_has_exactly_the_agreed_shape(client, lib):
    card = photos(client)["items"][0]
    assert set(card) == CARD_KEYS                     # no "score" in date mode
    assert card["taken"] == "2024-07-22T10:00:00"     # a wall clock: no Z, no offset
    assert card["day"] == "2024-07-22"
    assert card["kind"] == "photo"
    assert card["place"] == POTSDAM["place"]
    assert card["city"] == "Potsdam"
    assert (card["lat"], card["lon"]) == (52.3906, 13.0645)
    assert card["thumb"].startswith(f"/media/thumb/{card['id']}?v=")
    assert card["path"].endswith("/IMG_0001.HEIC") and card["path"].startswith("/")
    assert card["export"] == f"/media/export/{card['id']}"
    assert card["export_name"] == "IMG_0001.jpg"
    assert card["export_direct"] is False
    assert (card["w"], card["h"]) == (4032, 3024)
    assert card["favorite"] is False
    assert (card["nsfw"], card["nsfw_flag"]) == (None, False)
    assert card["edited"] is False
    # A still has nothing to play on hover.
    assert (card["preview"], card["story"], card["story_frames"]) == (None, None, 0)


def test_companions_are_never_listed_and_their_still_says_live(client, lib):
    page = photos(client)
    assert lib["companion"] not in ids(page)
    live = next(c for c in page["items"] if c["id"] == lib["live"])
    assert live["live"] is True


def test_undated_photos_come_last_with_no_date(client, lib):
    items = photos(client)["items"]
    assert [c["taken"] for c in items[-2:]] == [None, None]
    assert [c["day"] for c in items[-2:]] == [None, None]


def test_a_photo_without_its_thumbnail_says_so(client, lib):
    card = next(c for c in photos(client)["items"] if c["id"] == lib["nothumb"])
    assert card["thumb"] is None


def test_the_wall_clock_is_the_photo_s_own(client, lib):
    card = next(c for c in photos(client)["items"] if c["id"] == lib["nye_ny"])
    assert card["taken"] == "2023-12-31T23:30:00"
    assert card["day"] == "2023-12-31"


def test_paging_by_cursor_visits_everything_once(client, lib):
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = photos(client, **params)
        pages += 1
        assert len(page["items"]) <= 3
        # The count is for the first page; later pages do not pay for it.
        assert (page["total"] is not None) == (pages == 1)
        seen += ids(page)
        cursor = page["next"]
        if not cursor:
            break
    assert seen == [lib[k] for k in ORDER]
    assert pages == 6


def test_paging_through_a_tie_keeps_both(client, lib):
    # Two photos in the same second: the id breaks the tie, and the cursor
    # carries both halves of the key so neither is skipped or repeated.
    first = photos(client, q="on:2024-03-10", limit=1)
    second = photos(client, q="on:2024-03-10", limit=1, cursor=first["next"])
    assert ids(first) + ids(second) == [lib["tie_b"], lib["tie_a"]]
    assert second["next"] is None


def test_a_photo_arriving_while_scrolling_does_not_shift_the_pages(client, lib, db):
    from core.models import Photo

    first = photos(client, limit=4)
    newest = make_photo(db, "IMG_9999.JPG", taken=wall(2026, 9, 1, 12))
    db.commit()
    try:
        second = photos(client, limit=4, cursor=first["next"])
        assert ids(first) + ids(second) == [lib[k] for k in ORDER[:8]]
    finally:
        db.delete(db.get(Photo, newest.id))
        db.commit()


def test_limit_is_clamped_not_refused(client, lib):
    assert len(photos(client, limit=0)["items"]) == 1
    assert len(photos(client, limit=100000)["items"]) == len(ORDER)


def test_a_bad_cursor_is_a_bad_request(client, lib):
    assert client.get("/api/photos", params={"cursor": "!!!"}).status_code == 400
    assert client.get("/api/photos", params={"cursor": "bm90IGEga2V5"}).status_code == 400


# --- from= (the scrubber's jump) -------------------------------------------------


def test_from_a_month_starts_at_its_newest_photo(client, lib):
    page = photos(client, **{"from": "2024-05"})
    assert names(lib, page)[:3] == ["far", "video", "live"]
    # The total is still the whole listing's: the header says how many there
    # are, not how many are below the jump.
    assert page["total"] == len(ORDER)
    rest = photos(client, cursor=page["next"]) if page["next"] else {"items": []}
    assert lib["jul22"] not in ids(rest)


def test_from_a_day(client, lib):
    assert names(lib, photos(client, **{"from": "2024-06-01"}))[0] == "near"


def test_from_goes_by_the_wall_clock(client, lib):
    # December 2023 starts with the New York photo, whose instant is already
    # January in Berlin: the scrubber groups by wall clock, and so does the jump.
    assert names(lib, photos(client, **{"from": "2023-12"}))[0] == "nye_ny"


def test_from_before_everything_leaves_the_undated(client, lib):
    assert names(lib, photos(client, **{"from": "1990-01"})) == ["undated_b", "undated_a"]


def test_from_undated_is_the_undated(client, lib):
    assert names(lib, photos(client, **{"from": "undated"})) == ["undated_b", "undated_a"]


def test_from_in_the_future_is_the_top(client, lib):
    assert names(lib, photos(client, **{"from": "2030-01"}))[0] == "jul22"


def test_from_a_year_starts_at_its_end(client, lib):
    assert names(lib, photos(client, **{"from": "2023"}))[0] == "nye_ny"


@pytest.mark.parametrize("value", ["24", "2024-13", "yesterday", "2024-02-30"])
def test_a_bad_from_is_a_bad_request(client, lib, value):
    assert client.get("/api/photos", params={"from": value}).status_code == 400


# --- before= (scrolling up from a jump) -------------------------------------------


def test_a_jump_says_where_the_walk_up_starts(client, lib):
    assert photos(client, **{"from": "2024-05"})["prev"]
    # At the top there is nothing above, however the top was reached.
    assert photos(client)["prev"] is None
    assert photos(client, **{"from": "2030-01"})["prev"] is None
    assert photos(client, limit=3, cursor=photos(client, limit=3)["next"])["prev"] is None


def test_paging_up_from_a_jump_visits_everything_above_once(client, lib):
    page = photos(client, limit=3, **{"from": "2024-05"})
    above, cursor, pages = [], page["prev"], 0
    while cursor:
        up = photos(client, limit=2, before=cursor)
        pages += 1
        assert len(up["items"]) <= 2
        assert up["total"] is None and up["next"] is None
        above = ids(up) + above
        cursor = up["prev"]
    # Newest first, as the listing is, and joined to the jump with no gap.
    assert above == [lib[k] for k in ORDER[:ORDER.index("far")]]
    assert above + ids(page) == [lib[k] for k in ORDER[:len(above) + 3]]
    assert pages == 2


def test_paging_up_through_a_tie_keeps_both(client, lib, db):
    from app.routers.photos import encode_cursor
    from core.models import Photo

    up = photos(client, q="on:2024-03-10", limit=1, before=encode_cursor(db.get(Photo, lib["tie_a"])))
    assert ids(up) == [lib["tie_b"]]
    assert up["prev"] is None


def test_paging_up_keeps_to_the_filters(client, lib):
    # The only video: there is nothing of its kind above it to walk up to.
    assert photos(client, q="is:video", **{"from": "2024-05"})["prev"] is None
    page = photos(client, q="is:located", **{"from": "2024-05"})
    assert names(lib, page)[0] == "far"
    assert names(lib, photos(client, q="is:located", before=page["prev"])) == ["jul22", "near"]


def test_a_bad_before_is_a_bad_request(client, lib):
    assert client.get("/api/photos", params={"before": "!!!"}).status_code == 400


# --- filters ------------------------------------------------------------------


@pytest.mark.parametrize("q,expected", [
    ("is:video", ["video"]),
    ("is:live", ["live"]),
    ("is:screenshot", ["shot"]),
    ("is:photo is:screenshot", ["shot"]),
    ("is:undated", ["undated_b", "undated_a"]),
    ("in:potsdam", ["jul22"]),
    ('in:"new york"', ["nye_ny"]),
    ("camera:iphone", ["jul22"]),
    ("camera:nikon", ["old"]),
    ("file:IMGA", ["nothumb"]),
    ("file:DSC_", ["old"]),
    ("year:2015", ["old"]),
    ("2015", ["old"]),
    ("year:2023", ["nye_ny"]),
    ("month:2024-07", ["jul22", "jul01"]),
    ("on:2024-07-22", ["jul22"]),
    ("after:2024-07", ["jul22", "jul01"]),
    ("before:2016", ["old"]),
    ("after:2024-06 before:2024-06", ["shot", "near"]),
    ("near:52.3906,13.0645", ["jul22", "near"]),          # 0 m and 500 m
    ("near:52.3906,13.0645,5", ["jul22", "near", "far"]),  # and 3 km
    ("bbox:13,52,14,53", ["jul22", "near", "far"]),
    ("bbox:179,-20,-179,-15", ["fiji_e", "fiji_w"]),        # across the antimeridian
    ("bbox:179,-20,181,-15", ["fiji_e", "fiji_w"]),         # the same, from a map scrolled east
    ("is:located in:potsdam year:2024", ["jul22"]),
    ("in:nowhere", []),
])
def test_filters(client, lib, q, expected):
    page = photos(client, q=q)
    assert names(lib, page) == expected, q
    assert page["total"] == len(expected)
    assert page["mode"] == "date"
    assert not page["query"]["errors"]


def test_located_and_unlocated_split_the_library(client, lib):
    located = set(ids(photos(client, q="is:located")))
    unlocated = set(ids(photos(client, q="is:unlocated")))
    assert located.isdisjoint(unlocated)
    assert located | unlocated == {lib[k] for k in ORDER}


def test_the_query_is_described_back(client, lib):
    page = photos(client, q="IN:Potsdam 2024 is:unicorn foo:bar")
    assert page["query"]["filters"] == ["in:Potsdam", "year:2024"]
    assert page["query"]["errors"] == [
        (
            '"is:unicorn": is: takes photo, video, live, screenshot, located, unlocated, dated, undated, '
            "favorite, hidden, deleted, whatsapp, saved, nsfw, safe, edited"
        ),
        'unknown filter "foo:bar"',
    ]
    assert names(lib, page) == ["jul22"]


# --- one photo ----------------------------------------------------------------


def test_detail_of_a_photo(client, lib):
    r = client.get(f"/api/photos/{lib['jul22']}")
    assert r.status_code == 200
    d = r.json()
    assert set(d) == DETAIL_KEYS
    sig = d["thumb"].split("v=")[1]
    assert d["root"] == "icloud"
    assert d["rel_path"] == "IMG_0001.HEIC"
    assert d["mime"] == "image/heic"
    assert d["ext"] == "heic"
    assert d["taken_utc"] == "2024-07-22T08:00:00Z"
    assert d["tz_offset"] is None
    assert d["camera"] == {"make": "Apple", "model": "iPhone 15 Pro",
                           "lens": "iPhone 15 Pro back camera 6.765mm f/1.78"}
    assert d["exposure"]["iso"] == 50
    assert d["exposure"]["exposure_time"] == pytest.approx(1 / 120)
    assert d["exif"] == {"Software": "17.5"}
    assert d["location"] == {"lat": 52.3906, "lon": 13.0645, "altitude": 35.0, "city": "Potsdam",
                             "region": "Brandenburg", "country": "Germany", "country_code": "DE"}
    assert d["urls"] == {
        "display": f"/media/display/{lib['jul22']}?v={sig}",
        "original": f"/media/original/{lib['jul22']}",
        "download": f"/media/original/{lib['jul22']}?download=1",
        "export": f"/media/export/{lib['jul22']}",
        "video": None,
        "live": None,
    }
    assert d["preview_ready"] is False
    assert d["live_video_id"] is None
    assert d["error"] == ""
    # What iCloud knows, before the agent has read any of it.
    assert d["albums"] == []
    assert d["added_at"] is None
    assert d["original_id"] is None
    assert (d["hidden"], d["icloud_deleted"]) == (False, False)
    assert d["deletable"] is True


def test_detail_of_a_photo_with_an_offset(client, lib):
    d = client.get(f"/api/photos/{lib['nye_ny']}").json()
    assert d["taken"] == "2023-12-31T23:30:00"
    assert d["taken_utc"] == "2024-01-01T04:30:00Z"
    assert d["tz_offset"] == -300


def test_detail_of_an_unlocated_photo_has_no_location(client, lib):
    assert client.get(f"/api/photos/{lib['jul01']}").json()["location"] is None


def test_a_video_s_urls_follow_its_preview(client, lib, db):
    from core.models import Photo

    d = client.get(f"/api/photos/{lib['video']}").json()
    assert d["urls"]["display"] == d["thumb"]      # a video's still is its poster frame
    assert d["urls"]["video"] is None
    assert d["preview_ready"] is False
    assert d["export_name"] == "IMG_0003.MOV"

    video = db.get(Photo, lib["video"])
    write_preview(video)
    db.commit()
    d = client.get(f"/api/photos/{lib['video']}").json()
    assert d["urls"]["video"] == f"/media/preview/{video.id}?v={video.sig}"
    assert d["preview_ready"] is True
    assert d["export_name"] == "IMG_0003.mp4"
    assert d["video_codec"] == "hevc"


def test_a_live_photo_s_motion_is_its_companion_s_preview(client, lib, db):
    from core.models import Photo

    d = client.get(f"/api/photos/{lib['live']}").json()
    assert d["live_video_id"] == lib["companion"]
    assert d["urls"]["live"] is None
    assert d["preview_ready"] is False

    companion = db.get(Photo, lib["companion"])
    write_preview(companion)
    db.commit()
    d = client.get(f"/api/photos/{lib['live']}").json()
    assert d["urls"]["live"] == f"/media/preview/{companion.id}?v={companion.sig}"
    assert d["urls"]["video"] is None
    assert d["preview_ready"] is True


def test_a_missing_photo_is_a_404(client, lib):
    r = client.get("/api/photos/999999")
    assert r.status_code == 404
    assert r.json()["detail"] == "No such photo"


# --- asking for a preview -----------------------------------------------------


def test_a_still_has_no_motion_to_prepare(client, lib):
    assert client.post(f"/api/photos/{lib['jul01']}/preview").status_code == 400


def test_a_preview_is_queued_once(client, lib, db):
    from core.models import Job, Photo

    video = make_photo(db, "IMG_0050.MOV", taken=wall(2020, 1, 1), video_codec="hevc")
    db.commit()
    try:
        first = client.post(f"/api/photos/{video.id}/preview").json()
        again = client.post(f"/api/photos/{video.id}/preview").json()
        assert first["ready"] is False
        assert again == first
        job = db.get(Job, first["job_id"])
        assert (job.kind, job.status, job.params) == ("preview", "queued", {"photo_id": video.id})

        # Once that one is finished, asking again is a new request.
        job.status = "failed"
        db.commit()
        third = client.post(f"/api/photos/{video.id}/preview").json()
        assert third["job_id"] != first["job_id"]

        # And once the preview is there, the answer is where it is.
        write_preview(db.get(Photo, video.id))
        db.commit()
        ready = client.post(f"/api/photos/{video.id}/preview").json()
        assert ready == {"ready": True, "url": f"/media/preview/{video.id}?v={video.sig}"}
    finally:
        db.rollback()
        db.delete(db.get(Photo, video.id))
        db.commit()


def test_a_live_still_asks_for_its_companion(client, lib, db):
    from core.models import Job, Photo

    companion = make_photo(db, "IMG_0060.MOV", taken=wall(2020, 1, 2), is_companion=True)
    still = make_photo(db, "IMG_0060.HEIC", taken=wall(2020, 1, 2), live_video_id=companion.id)
    db.commit()
    try:
        answer = client.post(f"/api/photos/{still.id}/preview").json()
        assert answer["ready"] is False
        assert db.get(Job, answer["job_id"]).params == {"photo_id": companion.id}
    finally:
        db.rollback()
        for p in (still, companion):
            db.delete(db.get(Photo, p.id))
        db.commit()


# --- the timeline -------------------------------------------------------------


def test_the_timeline_groups_on_the_wall_clock(client, lib):
    t = client.get("/api/timeline").json()
    months = {m["month"]: m["count"] for m in t["months"]}
    assert [m["month"] for m in t["months"]] == sorted(months, reverse=True)
    # The New York photo is December's, although its instant is January's.
    assert months["2023-12"] == 1
    assert months["2024-01"] == 1
    assert months["2024-07"] == 2
    assert months["2015-03"] == 1
    assert t["undated"] == 2
    assert sum(months.values()) + t["undated"] == len(ORDER)


def test_the_timeline_applies_filters_and_ignores_words(client, lib):
    t = client.get("/api/timeline", params={"q": "is:video"}).json()
    assert t == {"months": [{"month": "2024-05", "count": 1}], "undated": 0}
    words = client.get("/api/timeline", params={"q": "red car is:video"}).json()
    assert words == t
