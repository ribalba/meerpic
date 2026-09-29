"""``is:nsfw``: a filter everywhere, and on its own a ranking by the classifier.

The agent stores each photo's probability in ``photos.nsfw`` (agent/nsfw.py);
here it is written straight. The threshold is 0.7 unless a test changes it,
and a photo exactly at it counts.
"""

from __future__ import annotations

import pytest
from server_helpers import (
    RED,
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


@pytest.fixture(scope="module")
def lib(client):
    from core.database import SessionLocal

    with SessionLocal() as s:
        p = {}
        p["sure"] = make_photo(s, "a.jpg", taken=wall(2024, 1, 1), nsfw=0.98765)
        p["likely"] = make_photo(s, "b.jpg", taken=wall(2024, 3, 1), nsfw=0.8)
        p["tie_a"] = make_photo(s, "c.jpg", taken=wall(2024, 2, 1), nsfw=0.75)
        p["tie_b"] = make_photo(s, "d.jpg", taken=wall(2024, 5, 1), nsfw=0.75)
        p["edge"] = make_photo(s, "e.jpg", taken=wall(2024, 4, 1), nsfw=0.7)
        p["clip"] = make_photo(s, "f.MOV", taken=wall(2024, 6, 1), nsfw=0.9, duration=3.0)
        p["low"] = make_photo(s, "g.jpg", taken=wall(2024, 7, 1), nsfw=0.3)
        p["unscored"] = make_photo(s, "h.jpg", taken=wall(2024, 8, 1))
        # Hidden in iCloud, and so here, however sure the classifier is.
        p["hidden"] = make_photo(s, "i.jpg", taken=wall(2024, 9, 1), nsfw=0.99, hidden=True)
        add_embedding(s, p["likely"], color_vector(RED))
        add_embedding(s, p["low"], color_vector(RED))
        s.commit()
        return {k: v.id for k, v in p.items()}


RANKED = ["sure", "clip", "likely", "tie_a", "tie_b", "edge"]


def photos(client, **params) -> dict:
    r = client.get("/api/photos", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def names(lib, page) -> list[str]:
    by_id = {v: k for k, v in lib.items()}
    return [by_id[c["id"]] for c in page["items"]]


def test_nsfw_is_a_ranking_most_certain_first(client, lib):
    page = photos(client, q="is:nsfw")
    assert page["mode"] == "score"
    assert names(lib, page) == RANKED            # a tie in id order
    assert page["total"] == len(RANKED)
    assert [c["score"] for c in page["items"]] == [0.9877, 0.9, 0.8, 0.75, 0.75, 0.7]
    assert page["query"]["filters"] == ["is:nsfw"]
    assert page["similar_to"] is None
    assert all(c["nsfw_flag"] for c in page["items"])


def test_sort_date_shows_the_same_ones_by_date(client, lib):
    page = photos(client, q="is:nsfw sort:date")
    assert page["mode"] == "score"
    assert names(lib, page) == ["clip", "tie_b", "edge", "likely", "tie_a", "sure"]
    assert page["total"] == len(RANKED)


def test_nsfw_pages_by_offset(client, lib):
    first = photos(client, q="is:nsfw", limit=4)
    second = photos(client, q="is:nsfw", limit=4, cursor=first["next"])
    assert names(lib, first) + names(lib, second) == RANKED
    assert second["next"] is None


def test_nsfw_takes_other_filters(client, lib):
    assert names(lib, photos(client, q="is:nsfw is:video")) == ["clip"]
    assert names(lib, photos(client, q="is:nsfw year:2023")) == []
    assert names(lib, photos(client, q="is:nsfw is:hidden")) == ["hidden"]


def test_with_words_the_words_rank_and_nsfw_filters(client, lib):
    page = photos(client, q="red is:nsfw")
    assert names(lib, page) == ["likely"]
    assert page["items"][0]["score"] == pytest.approx(1.0, abs=1e-3)   # the words' score


def test_the_timeline_and_the_map_filter_by_it(client, lib):
    t = client.get("/api/timeline", params={"q": "is:nsfw"}).json()
    assert sum(m["count"] for m in t["months"]) == len(RANKED)
    assert client.get("/api/map/clusters", params={"q": "is:nsfw"}).json() == {"clusters": [], "total": 0}


def test_cards_carry_the_score_and_the_flag(client, lib):
    cards = {c["id"]: c for c in photos(client)["items"]}
    assert (cards[lib["sure"]]["nsfw"], cards[lib["sure"]]["nsfw_flag"]) == (0.9877, True)
    assert (cards[lib["edge"]]["nsfw"], cards[lib["edge"]]["nsfw_flag"]) == (0.7, True)
    assert (cards[lib["low"]]["nsfw"], cards[lib["low"]]["nsfw_flag"]) == (0.3, False)
    assert (cards[lib["unscored"]]["nsfw"], cards[lib["unscored"]]["nsfw_flag"]) == (None, False)
    d = client.get(f"/api/photos/{lib['likely']}").json()
    assert (d["nsfw"], d["nsfw_flag"]) == (0.8, True)


def test_the_threshold_is_the_setting(client, lib, settings, monkeypatch):
    monkeypatch.setattr(settings, "nsfw_threshold", 0.85)
    assert names(lib, photos(client, q="is:nsfw")) == ["sure", "clip"]
    cards = {c["id"]: c for c in photos(client)["items"]}
    assert cards[lib["likely"]]["nsfw_flag"] is False
    state = client.get("/api/state").json()
    assert state["nsfw"]["threshold"] == 0.85
    assert state["counts"]["nsfw"] == 2


def test_state_says_how_nsfw_is_set_up(client, lib, settings, monkeypatch):
    state = client.get("/api/state").json()
    assert state["nsfw"] == {"enabled": True, "threshold": 0.7, "blur": True}
    assert state["counts"]["nsfw"] == len(RANKED)
    monkeypatch.setattr(settings, "nsfw_blur", False)
    monkeypatch.setattr(settings, "nsfw_enabled", False)
    assert client.get("/api/state").json()["nsfw"] == {"enabled": False, "threshold": 0.7, "blur": False}
