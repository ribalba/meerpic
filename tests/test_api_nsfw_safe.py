"""Marking pictures safe: a person overrules the NSFW classifier.

A marked photo is never flagged. A flagged photo whose search vector is at
least ``nsfw.clear_above`` close to a marked one is not flagged either; one at
least ``nsfw.demote_above`` close stays flagged but is listed last in
``is:nsfw``. The vectors here are made by hand so each photo's similarity to
the marked one is exactly what the test says (cosine c: c*e1 + sqrt(1-c^2)*e2).
"""

from __future__ import annotations

import math

import pytest
from server_helpers import (
    add_embedding,
    make_photo,
    needs_db,
    running_app,
    session,
    truncate,
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


def vector(cos: float, axis: int = 1) -> list[float]:
    """Unit vector at cosine ``cos`` to e0, leaning towards e``axis``."""
    from core import embed

    v = [0.0] * embed.dim()
    v[0] = cos
    v[axis] = math.sqrt(max(0.0, 1 - cos * cos))
    return v


@pytest.fixture
def lib(client):
    from core.database import SessionLocal

    truncate()
    with SessionLocal() as s:
        p = {}
        p["marked"] = make_photo(s, "baby.jpg", taken=wall(2024, 1, 1), nsfw=0.95)
        p["twin"] = make_photo(s, "baby2.jpg", taken=wall(2024, 1, 2), nsfw=0.97)       # 0.96 alike
        p["cousin"] = make_photo(s, "baby3.jpg", taken=wall(2024, 1, 3), nsfw=0.99)     # 0.88 alike
        p["other"] = make_photo(s, "beach.jpg", taken=wall(2024, 1, 4), nsfw=0.8)       # 0.50 alike
        p["clean"] = make_photo(s, "tree.jpg", taken=wall(2024, 1, 5), nsfw=0.1)         # 0.99 alike, not flagged
        for key, cos, axis in (("marked", 1.0, 1), ("twin", 0.96, 2), ("cousin", 0.88, 3),
                               ("other", 0.5, 4), ("clean", 0.99, 5)):
            add_embedding(s, p[key], vector(cos, axis))
        s.commit()
        ids = {k: v.id for k, v in p.items()}
    yield ids
    truncate()


def listing(client, q: str) -> list[int]:
    r = client.get("/api/photos", params={"q": q})
    assert r.status_code == 200, r.text
    return [c["id"] for c in r.json()["items"]]


def cards(client) -> dict[int, dict]:
    return {c["id"]: c for c in client.get("/api/photos").json()["items"]}


def mark(client, ids, safe=True) -> dict:
    r = client.post("/api/photos/safe", json={"ids": ids, "safe": safe})
    assert r.status_code == 200, r.text
    return r.json()


def detail(client, photo_id) -> dict:
    return client.get(f"/api/photos/{photo_id}").json()


def nsfw_count(client) -> int:
    return client.get("/api/state").json()["counts"]["nsfw"]


def test_before_any_mark_the_classifier_decides(client, lib):
    assert listing(client, "is:nsfw") == [lib["cousin"], lib["twin"], lib["marked"], lib["other"]]
    assert nsfw_count(client) == 4
    assert all(not c["nsfw_safe"] for c in cards(client).values())


def test_a_marked_photo_and_its_twin_are_not_flagged(client, lib):
    answer = mark(client, [lib["marked"]])
    assert answer == {"safe": True, "changed": 1, "cleared": [lib["twin"]], "flagged": []}
    # The twin is gone from the list; the cousin (0.88) stays, but last.
    assert listing(client, "is:nsfw") == [lib["other"], lib["cousin"]]
    assert nsfw_count(client) == 2
    c = cards(client)
    assert (c[lib["marked"]]["nsfw_flag"], c[lib["marked"]]["nsfw_safe"]) == (False, True)
    assert (c[lib["twin"]]["nsfw_flag"], c[lib["twin"]]["nsfw_safe"]) == (False, False)
    assert c[lib["cousin"]]["nsfw_flag"] is True and c[lib["other"]]["nsfw_flag"] is True
    # The classifier's number is still there to see.
    assert c[lib["twin"]]["nsfw"] == 0.97


def test_the_detail_says_why(client, lib):
    mark(client, [lib["marked"]])
    twin = detail(client, lib["twin"])
    assert twin["nsfw_flag"] is False
    assert twin["nsfw_like"]["id"] == lib["marked"] and twin["nsfw_like"]["cleared"] is True
    assert twin["nsfw_like"]["similarity"] == pytest.approx(0.96, abs=0.001)
    cousin = detail(client, lib["cousin"])
    assert cousin["nsfw_flag"] is True
    assert cousin["nsfw_like"]["cleared"] is False
    assert cousin["nsfw_like"]["similarity"] == pytest.approx(0.88, abs=0.001)
    assert detail(client, lib["other"])["nsfw_like"] is None
    # A marked photo is marked; it resembles nothing, as far as this goes.
    marked = detail(client, lib["marked"])
    assert marked["nsfw_safe"] is True and marked["nsfw_like"] is None


def test_taking_the_mark_back_flags_its_twin_again(client, lib):
    mark(client, [lib["marked"]])
    assert mark(client, [lib["marked"]], safe=False) == {
        "safe": False, "changed": 1, "cleared": [], "flagged": [lib["twin"]],
    }
    assert listing(client, "is:nsfw") == [lib["cousin"], lib["twin"], lib["marked"], lib["other"]]
    # Marking what is marked already changes nothing.
    assert mark(client, [lib["marked"]], safe=False)["changed"] == 0


def test_is_safe_lists_the_marks(client, lib):
    assert listing(client, "is:safe") == []
    mark(client, [lib["marked"], lib["other"]])
    assert sorted(listing(client, "is:safe")) == sorted([lib["marked"], lib["other"]])


def test_the_cut_offs_come_from_the_settings(client, lib, settings, monkeypatch):
    monkeypatch.setattr(settings, "nsfw_clear_above", 0.0)
    monkeypatch.setattr(settings, "nsfw_demote_above", 0.0)
    answer = mark(client, [lib["marked"]])
    assert answer["cleared"] == []
    assert listing(client, "is:nsfw") == [lib["cousin"], lib["twin"], lib["other"]]
    assert detail(client, lib["twin"])["nsfw_like"] is None
    # Lower: the cousin at 0.88 is cleared too.
    monkeypatch.setattr(settings, "nsfw_clear_above", 0.85)
    assert listing(client, "is:nsfw") == [lib["other"]]


def test_only_flagged_photos_are_ever_cleared(client, lib):
    """A photo the classifier does not flag has nothing to clear."""
    mark(client, [lib["marked"]])
    clean = detail(client, lib["clean"])
    assert clean["nsfw_flag"] is False and clean["nsfw_like"] is None


@pytest.mark.parametrize("body", [{"ids": []}, {}, {"ids": "1"}, {"ids": [1], "safe": "maybe"}])
def test_a_bad_request_is_refused(client, lib, body):
    assert client.post("/api/photos/safe", json=body).status_code == 422
