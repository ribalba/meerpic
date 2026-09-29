"""What iCloud knows and the files do not: favourites, the Hidden album,
Recently Deleted, edits, albums, and the pictures WhatsApp saved.

The agent reads all of it from ``rclone lsjson --metadata`` (agent/albums.py)
into columns and two tables; here those are written straight, the way the
agent leaves them, and what is checked is what the grid, the counts, the map
and the album list make of them. A small library with every case in it: a
photo in the Hidden album, one in Recently Deleted, one in both, one a Delete
here is still busy with, an edit with its original behind it, three WhatsApp
saves (one of them hidden), and a Live Photo whose still and motion are both
in the WhatsApp album, the way iCloud lists them.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from server_helpers import (
    RED,
    add_album,
    add_embedding,
    color_vector,
    make_photo,
    needs_db,
    running_app,
    session,
    wall,
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
    from core.config import get_settings

    return get_settings()


APPLE = {"make": "Apple", "model": "iPhone 15 Pro"}
POTSDAM = {"place": "Potsdam, Brandenburg, Germany", "city": "Potsdam", "lat": 52.39, "lon": 13.06}
SECRET = {"place": "Secretville, Nowhere, Germany", "city": "Secretville", "lat": 48.1, "lon": 11.5}
POLAND = {"place": "Sopot, Pomerania, Poland", "city": "Sopot", "lat": 54.44, "lon": 18.56}
ADDED = datetime(2024, 5, 1, 10, 0)  # noqa: DTZ001 - naive UTC, like every stored instant


@pytest.fixture(scope="module")
def lib(client):
    from core.database import SessionLocal

    with SessionLocal() as s:
        p = {}

        def cam(key, name, taken, **kw):
            p[key] = make_photo(s, name, taken=taken, **{**APPLE, **kw})

        cam("plain", "IMG_0100.HEIC", wall(2024, 6, 1), **POTSDAM)
        cam("fav", "IMG_0101.HEIC", wall(2024, 6, 2), favorite=True)
        cam("hidden", "IMG_0102.HEIC", wall(2024, 6, 3), hidden=True, **SECRET)
        cam("deleted", "IMG_0103.HEIC", wall(2024, 6, 4), icloud_deleted=True)
        cam("both", "IMG_0104.HEIC", wall(2024, 6, 5), hidden=True, icloud_deleted=True)
        cam("trashed", "IMG_0105.HEIC", wall(2024, 6, 6), trashed_at=wall(2026, 9, 1))
        cam("edit", "IMG_0106-edited.HEIC", wall(2024, 6, 7), content_id="c-106")
        cam("original", "IMG_0106.HEIC", wall(2024, 6, 7), content_id="c-106", superseded_by=p["edit"].id)
        # What WhatsApp saves: a JPEG with a UUID for a name and no tags at all.
        p["wa1"] = make_photo(s, "0B6F2C1E-7A51-4F0E-9C3B-1D2E3F405161.jpg", taken=wall(2024, 5, 1),
                              date_source="mtime", added_at=ADDED)
        p["wa2"] = make_photo(s, "5C1A9E0B-2D4F-4B6A-8E7C-9F0A1B2C3D4E.jpg", taken=wall(2024, 5, 2))
        p["wa_hidden"] = make_photo(s, "9E8D7C6B-5A4F-4E3D-2C1B-0A9F8E7D6C5B.jpg", taken=wall(2024, 5, 3),
                                    hidden=True)
        cam("companion", "IMG_0107.MOV", wall(2024, 4, 1), is_companion=True, content_id="c-107")
        cam("live", "IMG_0107.HEIC", wall(2024, 4, 1), content_id="c-107", live_video_id=p["companion"].id)
        # No camera either, but a screenshot and a video are not "saved".
        p["shot"] = make_photo(s, "Screenshot 1.png", taken=wall(2024, 3, 1), is_screenshot=True)
        p["clip"] = make_photo(s, "IMG_0108.MOV", taken=wall(2024, 3, 2), duration=4.0)
        cam("urlaub", "IMG_0109.HEIC", wall(2023, 8, 1), **POLAND)

        add_embedding(s, p["plain"], color_vector(RED))
        add_embedding(s, p["hidden"], color_vector(RED))

        # Both halves of the Live Photo are album members, as iCloud lists them.
        add_album(s, "WhatsApp", [p["wa1"], p["wa2"], p["wa_hidden"], p["live"], p["companion"]],
                  remote_count=339)
        add_album(s, "Urlaub Polen mit Kindern", [p["urlaub"], p["fav"]])
        add_album(s, "Hühner", [p["plain"], p["fav"]])
        add_album(s, "Instagram", [])
        add_album(s, "Favorites", [p["fav"]])
        add_album(s, "Hidden", [p["hidden"], p["both"], p["wa_hidden"]])
        add_album(s, "Recently Deleted", [p["deleted"], p["both"]])
        s.commit()
        return {k: v.id for k, v in p.items()}


# The grid, newest first: no hidden, no deleted, nothing being deleted, no
# original behind an edit, no Live Photo motion.
LISTED = ["edit", "fav", "plain", "wa2", "wa1", "live", "clip", "shot", "urlaub"]


def photos(client, **params) -> dict:
    r = client.get("/api/photos", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def names(lib, page) -> list[str]:
    by_id = {v: k for k, v in lib.items()}
    return [by_id.get(c["id"], c["id"]) for c in page["items"]]


def detail(client, photo_id) -> dict:
    r = client.get(f"/api/photos/{photo_id}")
    assert r.status_code == 200, r.text
    return r.json()


# --- what is listed -------------------------------------------------------------


def test_the_grid_leaves_out_what_icloud_hides(client, lib):
    page = photos(client)
    assert names(lib, page) == LISTED
    assert page["total"] == len(LISTED)


@pytest.mark.parametrize("q,expected", [
    # Asked for by name, and then only those.
    ("is:hidden", ["hidden", "wa_hidden"]),
    ("is:deleted", ["deleted"]),
    ("is:hidden is:deleted", ["both"]),
    ("is:whatsapp is:hidden", ["wa_hidden"]),
    ("album:Hidden is:hidden", ["hidden", "wa_hidden"]),
    ("is:favorite", ["fav"]),
    ("is:favourite", ["fav"]),
    ("is:whatsapp", ["wa2", "wa1", "live"]),
    ("album:whatsapp", ["wa2", "wa1", "live"]),
    ("album:WHATSAPP", ["wa2", "wa1", "live"]),
    ('album:"urlaub polen mit kindern"', ["fav", "urlaub"]),
    ("album:HÜHNER", ["fav", "plain"]),
    ("album:Instagram", []),
    ("album:Nowhere", []),
    ("is:saved", ["wa2", "wa1"]),
    ("is:edited", ["edit"]),
    ("is:whatsapp is:saved", ["wa2", "wa1"]),
])
def test_filters(client, lib, q, expected):
    page = photos(client, q=q)
    assert names(lib, page) == expected, q
    assert page["total"] == len(expected)
    assert page["mode"] == "date"
    assert not page["query"]["errors"]


def test_filters_are_described_back_canonically(client, lib):
    page = photos(client, q='is:favourite album:"Urlaub Polen mit Kindern"')
    assert page["query"]["filters"] == ["is:favorite", 'album:"Urlaub Polen mit Kindern"']


def test_a_search_leaves_out_the_hidden_too(client, lib):
    assert names(lib, photos(client, q="red")) == ["plain"]
    assert names(lib, photos(client, q="red is:hidden")) == ["hidden"]


def test_the_timeline_counts_what_the_grid_lists(client, lib):
    t = client.get("/api/timeline").json()
    assert sum(m["count"] for m in t["months"]) + t["undated"] == len(LISTED)
    months = {m["month"]: m["count"] for m in t["months"]}
    assert months["2024-06"] == 3                   # edit, fav, plain; not the five left out
    hidden = client.get("/api/timeline", params={"q": "is:hidden"}).json()
    assert hidden == {"months": [{"month": "2024-06", "count": 1}, {"month": "2024-05", "count": 1}],
                      "undated": 0}


def test_places_and_the_map_leave_out_the_hidden(client, lib):
    places = [p["city"] for p in client.get("/api/places").json()["places"]]
    assert sorted(places) == ["Potsdam", "Sopot"]
    world = client.get("/api/map/clusters", params={"zoom": 3}).json()
    assert world["total"] == 2
    assert client.get("/api/map/clusters", params={"zoom": 3, "q": "is:hidden"}).json()["total"] == 1


def test_the_counts_are_what_each_filter_lists(client, lib):
    counts = client.get("/api/state").json()["counts"]
    assert counts == {
        "all": 9, "photos": 8, "videos": 1, "live": 1, "screenshots": 1, "located": 2,
        "undated": 0, "favorites": 1, "whatsapp": 3, "saved": 2, "nsfw": 0, "hidden": 2,
        "deleted": 1,
    }
    for key, q in {
        "all": "", "photos": "is:photo", "videos": "is:video", "live": "is:live",
        "screenshots": "is:screenshot", "located": "is:located", "undated": "is:undated",
        "favorites": "is:favorite", "whatsapp": "is:whatsapp", "saved": "is:saved",
        "hidden": "is:hidden", "deleted": "is:deleted",
    }.items():
        assert photos(client, q=q)["total"] == counts[key], key


# --- cards and detail ---------------------------------------------------------


def test_cards_say_favourite_and_edited(client, lib):
    cards = {c["id"]: c for c in photos(client)["items"]}
    assert cards[lib["fav"]]["favorite"] is True
    assert cards[lib["plain"]]["favorite"] is False
    assert cards[lib["edit"]]["edited"] is True
    assert [c["edited"] for i, c in cards.items() if i != lib["edit"]] == [False] * (len(LISTED) - 1)


def test_an_edit_names_its_original_and_the_original_is_still_there(client, lib):
    edit = detail(client, lib["edit"])
    assert edit["edited"] is True
    assert edit["original_id"] == lib["original"]
    # Not on the grid, but "Show original" opens it.
    original = detail(client, lib["original"])
    assert original["name"] == "IMG_0106.HEIC"
    assert (original["edited"], original["original_id"]) == (False, None)


def test_detail_lists_the_albums_the_user_s_first(client, lib):
    assert detail(client, lib["fav"])["albums"] == ["Hühner", "Urlaub Polen mit Kindern", "Favorites"]
    assert detail(client, lib["live"])["albums"] == ["WhatsApp"]
    assert detail(client, lib["clip"])["albums"] == []


def test_detail_says_what_icloud_says(client, lib):
    wa1 = detail(client, lib["wa1"])
    assert wa1["added_at"] == "2024-05-01T10:00:00Z"
    both = detail(client, lib["both"])
    assert (both["hidden"], both["icloud_deleted"]) == (True, True)
    assert both["albums"] == ["Hidden", "Recently Deleted"]
    assert detail(client, lib["plain"])["added_at"] is None


def test_detail_says_whether_it_can_be_deleted(client, lib, settings, monkeypatch):
    assert detail(client, lib["plain"])["deletable"] is True
    monkeypatch.setattr(settings, "delete_enabled", False)
    assert detail(client, lib["plain"])["deletable"] is False


# --- the album list -----------------------------------------------------------


def test_albums_the_user_s_by_name_then_icloud_s(client, lib):
    albums = client.get("/api/albums").json()["albums"]
    assert all(set(a) == {"id", "name", "kind", "count", "remote_count"} for a in albums)
    assert [(a["name"], a["kind"], a["count"]) for a in albums] == [
        ("Hühner", "user", 2),
        ("Instagram", "user", 0),
        ("Urlaub Polen mit Kindern", "user", 2),
        # Five members in iCloud's list: the Live Photo once (not its motion
        # too), and not the hidden one. What album:WhatsApp shows.
        ("WhatsApp", "user", 3),
        ("Favorites", "smart", 1),
        ("Hidden", "smart", 0),
        ("Recently Deleted", "smart", 0),
    ]
    whatsapp = next(a for a in albums if a["name"] == "WhatsApp")
    assert whatsapp["remote_count"] == 339


def test_albums_refresh_is_queued_once(client, lib, db):
    from core.models import Job

    first = client.post("/api/albums/refresh").json()["job"]
    try:
        assert (first["kind"], first["status"], first["params"]) == ("albums", "queued", {})
        assert client.post("/api/albums/refresh").json()["job"]["id"] == first["id"]
        job = db.get(Job, first["id"])
        job.status = "running"
        db.commit()
        assert client.post("/api/albums/refresh").json()["job"]["id"] == first["id"]
        # A sync queued meanwhile is a job of its own, not this one.
        assert client.post("/api/sync").json()["job"]["id"] != first["id"]
        job.status = "done"
        db.commit()
        again = client.post("/api/albums/refresh").json()["job"]
        assert again["id"] != first["id"]
    finally:
        db.query(Job).delete()
        db.commit()


def test_albums_refresh_needs_sync(client, lib, settings, monkeypatch):
    monkeypatch.setattr(settings, "sync_enabled", False)
    r = client.post("/api/albums/refresh")
    assert r.status_code == 400
    assert "enabled" in r.json()["detail"]
