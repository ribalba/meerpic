"""Delete: the plan the dialog shows, and doing exactly that plan.

What a Delete takes with it (a Live Photo's motion, an edit's original) is
core.deletion's, tested there. Here: the plan is only read, a Delete must
carry the moves that were shown and is refused when they are no longer what
it would do, and a Delete that goes ahead takes every file of the plan off
every listing at once and leaves the plan for the agent. Nothing here moves
a file: the server only ever writes the plan down.
"""

from __future__ import annotations

import pytest
from server_helpers import make_photo, needs_db, running_app, session, truncate, wall

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


@pytest.fixture
def lib(client):
    """A fresh library per test: every test here changes it."""
    from core.database import SessionLocal

    truncate()
    with SessionLocal() as s:
        p = {}
        p["plain"] = make_photo(s, "IMG_0001.HEIC", taken=wall(2024, 6, 1))
        p["other"] = make_photo(s, "IMG_0002.HEIC", taken=wall(2024, 6, 2))
        p["motion"] = make_photo(s, "IMG_0003.MOV", taken=wall(2024, 6, 3), is_companion=True)
        p["live"] = make_photo(s, "IMG_0003.HEIC", taken=wall(2024, 6, 3), live_video_id=p["motion"].id)
        # An edit of a Live Photo: both stills point at the one motion.
        p["edit_motion"] = make_photo(s, "IMG_0004.MOV", taken=wall(2024, 6, 4), is_companion=True)
        p["edit"] = make_photo(s, "IMG_0004-edited.HEIC", taken=wall(2024, 6, 4),
                               live_video_id=p["edit_motion"].id)
        p["original"] = make_photo(s, "IMG_0004.HEIC", taken=wall(2024, 6, 4),
                                   live_video_id=p["edit_motion"].id, superseded_by=p["edit"].id)
        # Not from iCloud: in a subfolder of the library.
        p["import"] = make_photo(s, "scan.jpg", rel_path="imports/scan.jpg", taken=wall(2024, 6, 5))
        s.commit()
        ids = {k: v.id for k, v in p.items()}
    yield ids
    truncate()


def get_plan(client, ids) -> dict:
    r = client.post("/api/photos/delete/plan", json={"ids": ids})
    assert r.status_code == 200, r.text
    return r.json()


def confirm(client, ids, shown: dict | None = None, **extra):
    shown = shown if shown is not None else get_plan(client, ids)
    return client.post("/api/photos/delete", json={"ids": ids, "moves": shown["moves"], **extra})


def trashed(db, lib) -> set[str]:
    from core.models import Photo

    db.expire_all()
    by_id = {v: k for k, v in lib.items()}
    return {by_id[p.id] for p in db.query(Photo).filter(Photo.trashed_at.is_not(None))}


def delete_jobs(db) -> list:
    from core.models import Job

    db.expire_all()
    return db.query(Job).filter(Job.kind == "delete").order_by(Job.id).all()


def listed(client) -> list[int]:
    return [c["id"] for c in client.get("/api/photos").json()["items"]]


# --- the plan -----------------------------------------------------------------


def test_the_plan_says_every_file_and_where_it_goes(client, lib, db):
    p = get_plan(client, [lib["live"], lib["plain"]])
    assert set(p) == {"groups", "moves", "excluded", "missing", "count"}
    assert [g["photo_id"] for g in p["groups"]] == [lib["live"], lib["plain"]]
    assert p["count"] == 3                                        # the still, its motion, the other
    # Later syncs leave all three in iCloud, which keeps them.
    assert sorted(p["excluded"]) == sorted(["IMG_0003.HEIC", "IMG_0003.MOV", "IMG_0001.HEIC"])
    assert len(p["moves"]) == 3 and all("/.meerpic-trash/" in m for m in p["moves"])
    assert p["missing"] == []
    step = p["groups"][0]["steps"][0]
    assert set(step) == {"photo_id", "name", "local", "trash", "excluded"}
    # Only read: nothing is marked, nothing is queued.
    assert trashed(db, lib) == set()
    assert delete_jobs(db) == []


def test_the_plan_of_an_edit_takes_its_original_and_their_motion(client, lib):
    p = get_plan(client, [lib["edit"]])
    assert sorted(s["photo_id"] for s in p["groups"][0]["steps"]) == sorted(
        [lib["edit"], lib["original"], lib["edit_motion"]]
    )


def test_a_file_that_is_not_in_icloud_is_only_moved(client, lib):
    p = get_plan(client, [lib["import"]])
    assert p["excluded"] == []
    assert p["count"] == 1 and len(p["moves"]) == 1
    assert p["groups"][0]["steps"][0]["excluded"] is None


def test_the_plan_names_what_is_not_there(client, lib):
    p = get_plan(client, [lib["plain"], 999999])
    assert p["missing"] == [999999]
    assert p["count"] == 1


# --- doing it -----------------------------------------------------------------


def test_a_delete_does_the_plan_it_was_shown(client, lib, db):
    shown = get_plan(client, [lib["live"], lib["plain"]])
    r = confirm(client, [lib["live"], lib["plain"]], shown)
    assert r.status_code == 200, r.text
    answer = r.json()
    assert answer["count"] == 3
    job = answer["job"]
    assert (job["kind"], job["status"]) == ("delete", "queued")
    # The browser gets the ids; the plan is in the job for the agent.
    assert job["params"] == {"photo_ids": [s["photo_id"] for g in shown["groups"] for s in g["steps"]]}
    (row,) = delete_jobs(db)
    assert row.params["plan"]["moves"] == shown["moves"]
    assert row.params["plan"]["excluded"] == shown["excluded"]
    assert trashed(db, lib) == {"live", "motion", "plain"}
    assert lib["live"] not in listed(client) and lib["plain"] not in listed(client)
    assert lib["other"] in listed(client)
    # The status poll lists it like any job, and without the plan.
    polled = next(j for j in client.get("/api/status").json()["jobs"] if j["id"] == job["id"])
    assert "plan" not in polled["params"]


def test_a_delete_of_an_original_takes_its_edit(client, lib, db):
    assert confirm(client, [lib["original"]]).status_code == 200
    assert trashed(db, lib) == {"original", "edit", "edit_motion"}


def test_a_delete_that_was_not_shown_is_refused(client, lib, db):
    r = client.post("/api/photos/delete", json={"ids": [lib["plain"]], "moves": []})
    assert r.status_code == 409
    fresh = r.json()
    assert len(fresh["moves"]) == 1 and fresh["excluded"] == ["IMG_0001.HEIC"]
    assert fresh["count"] == 1 and "changed" in fresh["detail"]
    assert trashed(db, lib) == set()
    assert delete_jobs(db) == []
    # Nor is anything deleted without the moves at all, the old way included.
    assert client.post("/api/photos/delete", json={"ids": [lib["plain"]]}).status_code == 422
    assert client.post("/api/photos/delete", json={"ids": [lib["plain"]], "commands": []}).status_code == 422


def test_a_plan_that_changed_since_the_dialog_is_shown_again(client, lib, db):
    from core.models import Photo

    shown = get_plan(client, [lib["plain"]])
    # Meanwhile the agent paired it with a motion it had not seen yet.
    motion = make_photo(db, "IMG_0001.MOV", taken=wall(2024, 6, 1), is_companion=True)
    db.get(Photo, lib["plain"]).live_video_id = motion.id
    db.commit()
    r = confirm(client, [lib["plain"]], shown)
    assert r.status_code == 409
    assert "IMG_0001.MOV" in r.json()["excluded"]
    assert trashed(db, lib) == set()
    # Shown the new plan, the same click goes through.
    assert confirm(client, [lib["plain"]], r.json()).status_code == 200
    db.expire_all()
    assert db.get(Photo, motion.id).trashed_at is not None


def test_moves_and_names_must_be_the_ones_shown(client, lib, db):
    shown = get_plan(client, [lib["plain"]])
    assert confirm(client, [lib["plain"]], shown, moves=["/somewhere/else -> /trash"]).status_code == 409
    assert confirm(client, [lib["plain"]], shown, excluded=["IMG_9999.HEIC"]).status_code == 409
    assert trashed(db, lib) == set()
    assert confirm(client, [lib["plain"]], shown, excluded=shown["excluded"]).status_code == 200
    assert trashed(db, lib) == {"plain"}


def test_a_second_click_deletes_nothing_twice(client, lib, db):
    shown = get_plan(client, [lib["plain"]])
    assert confirm(client, [lib["plain"]], shown).status_code == 200
    again = confirm(client, [lib["plain"]], shown)
    assert again.status_code == 409
    assert again.json()["missing"] == [lib["plain"]] and again.json()["count"] == 0
    assert len(delete_jobs(db)) == 1


def test_nothing_to_delete_is_a_404(client, lib, db):
    r = client.post("/api/photos/delete", json={"ids": [999999], "moves": []})
    assert r.status_code == 404
    assert delete_jobs(db) == []


def test_a_failed_delete_can_be_asked_for_again(client, lib, db):
    from sqlalchemy import update

    from core.models import Photo

    first = confirm(client, [lib["plain"]]).json()["job"]
    # What the agent does when iCloud says no: the job fails and the photo
    # comes back.
    delete_jobs(db)[0].status = "failed"
    db.execute(update(Photo).values(trashed_at=None))
    db.commit()
    assert lib["plain"] in listed(client)
    again = confirm(client, [lib["plain"]]).json()
    assert again["job"]["id"] != first["id"] and again["count"] == 1
    assert trashed(db, lib) == {"plain"}


@pytest.mark.parametrize("body", [{"ids": []}, {"ids": list(range(1, 502))}, {}, {"ids": "1"}])
@pytest.mark.parametrize("path", ["/api/photos/delete/plan", "/api/photos/delete"])
def test_nothing_or_too_much_is_refused(client, lib, body, path):
    assert client.post(path, json={**body, "moves": []}).status_code == 422


def test_without_delete_turned_on_there_is_no_delete(client, lib, db, settings, monkeypatch):
    shown = get_plan(client, [lib["plain"]])
    monkeypatch.setattr(settings, "delete_enabled", False)
    assert client.post("/api/photos/delete/plan", json={"ids": [lib["plain"]]}).status_code == 400
    r = confirm(client, [lib["plain"]], shown)
    assert r.status_code == 400
    assert "delete" in r.json()["detail"].lower()
    assert trashed(db, lib) == set()
    assert delete_jobs(db) == []


def test_with_sync_off_delete_still_moves_and_excludes_nothing(client, lib, db, settings, monkeypatch):
    monkeypatch.setattr(settings, "sync_enabled", False)
    shown = get_plan(client, [lib["plain"]])
    assert shown["excluded"] == [] and len(shown["moves"]) == 1
    assert confirm(client, [lib["plain"]], shown).status_code == 200
    assert trashed(db, lib) == {"plain"}
