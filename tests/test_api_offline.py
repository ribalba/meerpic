"""A library folder that is not there right now (core/roots.py).

A network share that answers only at home: while it is away its photos are
in no listing and no count, the sidebar says how many that hides, nothing of
it is pending, and its originals answer at once instead of hanging on the
share. Its rows stay as they are, so it is all back with the folder.
"""

from __future__ import annotations

import pytest
from server_helpers import library, make_photo, needs_db, running_app, session

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
def share(tmp_path, monkeypatch):
    """A second root, "nas", whose folder does not exist yet."""
    from core import roots
    from core.config import get_settings

    folder = tmp_path / "nas"
    monkeypatch.setattr(get_settings(), "library_roots", {"icloud": str(library()), "nas": str(folder)})
    roots.forget()
    yield folder
    roots.forget()


@pytest.fixture
def seeded(client, db, share):
    from server_helpers import truncate

    truncate()
    home = make_photo(db, "IMG_1.JPG")
    away = [make_photo(db, f"DSC_{n}.JPG", root="nas") for n in range(3)]
    # Not a photo of its own: not counted as hidden either.
    make_photo(db, "DSC_9.MOV", root="nas", is_companion=True)
    db.commit()
    return home, away


def listed_ids(client, q=""):
    r = client.get("/api/photos", params={"q": q, "limit": 100})
    assert r.status_code == 200, r.text
    return {item["id"] for item in r.json()["items"]}


def test_photos_in_a_folder_that_is_away_are_not_listed(client, seeded):
    home, away = seeded
    assert listed_ids(client) == {home.id}
    assert listed_ids(client, f"file:{away[0].name}") == set()
    assert client.get("/api/state").json()["counts"]["all"] == 1
    status = client.get("/api/status").json()
    assert status["offline"] == [{"root": "nas", "path": status["offline"][0]["path"], "photos": 3}]
    assert status["offline"][0]["path"].endswith("/nas")
    assert status["index"]["total"] == 1


def test_they_are_back_with_the_folder(client, seeded, share):
    from core import roots

    home, away = seeded
    share.mkdir()
    roots.forget()
    assert listed_ids(client) == {home.id, *(p.id for p in away)}
    assert client.get("/api/state").json()["counts"]["all"] == 4
    assert client.get("/api/status").json()["offline"] == []


def test_nothing_in_it_is_pending(client, db, share):
    from server_helpers import truncate

    truncate()
    make_photo(db, "DSC_1.JPG", root="nas", meta_sig="", thumb_sig="")
    db.commit()
    index = client.get("/api/status").json()["index"]
    assert (index["meta"], index["thumbs"], index["total"]) == (0, 0, 0)


def test_its_originals_answer_at_once(client, seeded):
    _, away = seeded
    for path in ("original", "display", "export"):
        r = client.get(f"/media/{path}/{away[0].id}")
        assert r.status_code == 503, (path, r.text)
        assert "not reachable" in r.json()["detail"]
