"""The sidebar's side of the API: state, preferences, status, sync and jobs."""

from __future__ import annotations

from datetime import timedelta

import pytest
from server_helpers import (
    add_embedding,
    color_vector,
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
    """The live settings object, for a test to change with monkeypatch."""
    from core.config import get_settings

    return get_settings()


@pytest.fixture
def empty(client):
    truncate()
    yield
    truncate()


def iso_z(dt) -> str:
    return dt.isoformat(timespec="seconds") + "Z"


# --- state and prefs ----------------------------------------------------------


def test_state_says_what_the_client_needs_first(client, empty, db):
    from core.version import VERSION

    make_photo(db, "IMG_1.HEIC", lat=52.4, lon=13.1)
    make_photo(db, "IMG_2.MOV", is_companion=True)
    make_photo(db, "IMG_3.MOV")
    still = make_photo(db, "IMG_4.HEIC")
    make_photo(db, "Screenshot.png", is_screenshot=True, taken=None)
    companion = make_photo(db, "IMG_4.MOV", is_companion=True)
    still.live_video_id = companion.id
    db.commit()

    s = client.get("/api/state").json()
    assert s["version"] == VERSION
    assert s["timezone"] == "Europe/Berlin"
    assert s["roots"] == ["icloud"]
    assert set(s["map"]) == {"tile_url", "attribution", "max_zoom"}
    assert s["sync"] == {"enabled": True, "remote": "iclouddrive:PrimarySync/All Photos"}
    assert s["search"] == {"model": "fake", "ready": True}
    assert s["prefs"] == {}
    assert s["nsfw"] == {"enabled": True, "threshold": 0.7, "blur": True}
    assert s["delete"] is True
    # Companions are not photos you took; a Live Photo is one photo.
    assert s["counts"] == {"all": 4, "photos": 3, "videos": 1, "live": 1, "screenshots": 1,
                           "located": 1, "undated": 1, "favorites": 0, "whatsapp": 0,
                           "saved": 2, "nsfw": 0, "hidden": 0, "deleted": 0}


def test_state_says_whether_there_is_a_delete_button(client, empty, settings, monkeypatch):
    monkeypatch.setattr(settings, "delete_enabled", False)
    assert client.get("/api/state").json()["delete"] is False
    # Delete is local, so it does not need sync.
    monkeypatch.setattr(settings, "delete_enabled", True)
    monkeypatch.setattr(settings, "sync_enabled", False)
    assert client.get("/api/state").json()["delete"] is True


def test_prefs_are_kept_and_replaced_whole(client, empty):
    assert client.put("/api/prefs", json={"value": {"size": 220, "view": "map"}}).json() == {"ok": True}
    assert client.get("/api/state").json()["prefs"] == {"size": 220, "view": "map"}
    client.put("/api/prefs", json={"value": {"size": 180}})
    assert client.get("/api/state").json()["prefs"] == {"size": 180}


def test_prefs_must_be_an_object(client, empty):
    assert client.put("/api/prefs", json={"value": [1, 2]}).status_code == 422
    assert client.put("/api/prefs", json={}).status_code == 422


def test_version(client):
    from core.version import VERSION

    assert client.get("/api/version").json() == {"version": VERSION}


# --- status -------------------------------------------------------------------


def status(client) -> dict:
    r = client.get("/api/status")
    assert r.status_code == 200, r.text
    return r.json()


def test_status_of_an_empty_install(client, empty):
    s = status(client)
    assert s["agent"] is None
    assert s["sync"] is None
    assert s["last_sync"] is None
    assert s["jobs"] == []
    assert s["index"] == {"total": 0, "meta": 0, "thumbs": 0, "embeddings": 0, "previews": 0,
                          "story": 0, "nsfw": 0, "faces": 0, "ocr": 0, "failed": 0}
    assert s["latest"] is None


def test_the_agent_is_alive_while_it_beats(client, empty, db):
    from core.models import Setting
    from core.timeutil import utcnow

    beat = {"version": "0.1.0", "host": "laptop", "seen_at": iso_z(utcnow()), "phase": "thumbs",
            "workers": 8, "model": "fake", "pending": {"meta": 1}}
    db.add(Setting(key="agent.status", value=beat))
    db.commit()
    agent = status(client)["agent"]
    assert agent["alive"] is True
    assert {k: agent[k] for k in ("version", "host", "phase", "workers", "model")} == {
        "version": "0.1.0", "host": "laptop", "phase": "thumbs", "workers": 8, "model": "fake",
    }
    assert agent["seen_at"].endswith("Z")

    row = db.get(Setting, "agent.status")
    row.value = {**beat, "seen_at": iso_z(utcnow() - timedelta(seconds=45))}
    db.commit()
    assert status(client)["agent"]["alive"] is False

    row.value = {**beat, "seen_at": "not a time"}
    db.commit()
    assert status(client)["agent"]["alive"] is False


def test_what_is_pending_is_counted_per_stage(client, empty, db, settings, monkeypatch):
    from core.models import PhotoEmbedding

    make_photo(db, "IMG_1.HEIC", meta_sig="", thumb_sig="")          # nothing done yet
    make_photo(db, "IMG_2.HEIC")                                       # needs its vector
    embedded = make_photo(db, "IMG_3.HEIC")
    add_embedding(db, embedded, color_vector((255, 0, 0)))
    stale = make_photo(db, "IMG_4.HEIC")                               # a vector of an older version
    add_embedding(db, stale, color_vector((255, 0, 0)))
    db.get(PhotoEmbedding, stale.id).sig = "0000000000000000"
    video = make_photo(db, "IMG_5.MOV")                                # needs a preview
    make_photo(db, "IMG_6.MOV", is_companion=True)                     # never embedded, never listed
    make_photo(db, "IMG_7.HEIC", meta_sig="", fail_count=3, error="bad file", error_stage="meta")
    db.commit()

    idx = status(client)["index"]
    assert idx["total"] == 6                 # the companion is not a photo of its own
    assert idx["meta"] == 1                  # fresh; parked is parked, not pending
    assert idx["thumbs"] == 1
    assert idx["embeddings"] == 4            # fresh, thumbed, stale and the video
    assert idx["previews"] == 2              # the video and the companion
    assert idx["failed"] == 1

    # On demand, a video is not pending until somebody asks for it.
    monkeypatch.setattr(settings, "video_previews", "on-demand")
    assert status(client)["index"]["previews"] == 0
    client.post(f"/api/photos/{video.id}/preview")
    assert status(client)["index"]["previews"] == 1


def test_storyboards_and_nsfw_scores_are_pending_until_made(client, empty, db, settings, monkeypatch):
    make_photo(db, "IMG_1.HEIC")                                        # no score yet
    scored = make_photo(db, "IMG_2.HEIC", nsfw=0.01)
    scored.nsfw_sig = scored.sig
    video = make_photo(db, "IMG_3.MOV", duration=10.0)                 # no strip, no score
    done = make_photo(db, "IMG_4.MOV", duration=10.0, story_frames=10, nsfw=0.0)
    done.story_sig = done.nsfw_sig = done.sig
    make_photo(db, "IMG_5.MOV", is_companion=True)                     # neither, ever
    parked = make_photo(db, "IMG_6.MOV", fail_count=3, error="no decoder", error_stage="story")
    db.commit()
    assert video.story_sig != video.sig and parked.story_sig != parked.sig

    idx = status(client)["index"]
    assert idx["story"] == 1                 # the video; the companion and the parked one are not
    assert idx["nsfw"] == 2                  # the unscored photo and video
    monkeypatch.setattr(settings, "nsfw_enabled", False)
    assert status(client)["index"]["nsfw"] == 0


def test_files_are_pending_for_text_until_read(client, empty, db, settings, monkeypatch):
    make_photo(db, "IMG_1.HEIC")                                        # not read yet
    read = make_photo(db, "IMG_2.HEIC")
    read.ocr_sig = read.sig
    make_photo(db, "IMG_3.MOV", duration=10.0)                         # videos are read too
    make_photo(db, "IMG_4.MOV", is_companion=True)                     # its still is read instead
    make_photo(db, "IMG_5.HEIC", superseded_by=read.id)                # the edit is read instead
    make_photo(db, "IMG_6.HEIC", fail_count=3, error="bad", error_stage="ocr")
    db.commit()

    assert status(client)["index"]["ocr"] == 2
    monkeypatch.setattr(settings, "ocr_videos", False)
    assert status(client)["index"]["ocr"] == 1
    monkeypatch.setattr(settings, "ocr_enabled", False)
    assert status(client)["index"]["ocr"] == 0


def test_latest_is_the_newest_listed_photo(client, empty, db):
    from core.timeutil import utcnow

    make_photo(db, "IMG_1.HEIC", taken=wall(2024, 1, 1))
    newest = make_photo(db, "IMG_2.HEIC", taken=wall(2024, 6, 1, 14))
    make_photo(db, "IMG_3.MOV", taken=wall(2025, 1, 1), is_companion=True)
    # Newer still, and none of them on the grid, so none of them is "new".
    make_photo(db, "IMG_4.HEIC", taken=wall(2025, 2, 1), hidden=True)
    make_photo(db, "IMG_5.HEIC", taken=wall(2025, 3, 1), icloud_deleted=True)
    make_photo(db, "IMG_6.HEIC", taken=wall(2025, 4, 1), trashed_at=utcnow())
    make_photo(db, "IMG_7.HEIC", taken=wall(2025, 5, 1), superseded_by=newest.id)
    db.commit()
    assert status(client)["latest"] == {"id": newest.id, "sort_at": "2024-06-01T12:00:00Z"}


def test_jobs_are_the_active_ones_and_the_last_five_finished(client, empty, db):
    from core.models import Job
    from core.timeutil import utcnow

    now = utcnow()
    for i in range(7):
        db.add(Job(kind="scan", status="done", finished_at=now - timedelta(minutes=10 - i)))
    running = Job(kind="sync", status="running", started_at=now, log="\n".join(f"line {i}" for i in range(100)))
    queued = Job(kind="preview", status="queued", params={"photo_id": 1})
    db.add_all([running, queued])
    db.commit()
    s = status(client)
    kinds = [(j["kind"], j["status"]) for j in s["jobs"]]
    assert kinds[:2] == [("sync", "running"), ("preview", "queued")]
    assert kinds[2:] == [("scan", "done")] * 5
    job = s["jobs"][0]
    assert set(job) >= {"id", "kind", "status", "params", "progress", "message", "error",
                        "created_at", "started_at", "finished_at", "log_tail"}
    assert job["created_at"].endswith("Z") and job["started_at"].endswith("Z")
    assert job["finished_at"] is None
    assert job["log_tail"].splitlines() == [f"line {i}" for i in range(60, 100)]
    assert s["sync"]["id"] == running.id


def test_last_sync_is_when_the_last_good_one_finished(client, empty, db):
    from core.models import Job

    db.add_all([
        Job(kind="sync", status="done", finished_at=wall(2026, 9, 1, 10)),
        Job(kind="sync", status="done", finished_at=wall(2026, 9, 2, 10)),
        Job(kind="sync", status="failed", finished_at=wall(2026, 9, 3, 10), error="The iCloud session has expired"),
    ])
    db.commit()
    s = status(client)
    assert s["last_sync"] == "2026-09-02T10:00:00Z"
    assert s["sync"]["status"] == "failed"
    assert "expired" in s["sync"]["error"]


# --- sync and cancel ----------------------------------------------------------


def test_sync_is_queued_once(client, empty, db):
    from core.models import Job

    first = client.post("/api/sync").json()["job"]
    assert (first["kind"], first["status"]) == ("sync", "queued")
    assert client.post("/api/sync").json()["job"]["id"] == first["id"]

    job = db.get(Job, first["id"])
    job.status = "running"
    db.commit()
    assert client.post("/api/sync").json()["job"]["id"] == first["id"]

    job.status = "done"
    db.commit()
    assert client.post("/api/sync").json()["job"]["id"] != first["id"]


def test_sync_says_why_it_will_not(client, empty, settings, monkeypatch):
    monkeypatch.setattr(settings, "sync_enabled", False)
    r = client.post("/api/sync")
    assert r.status_code == 400
    assert "enabled" in r.json()["detail"]


def test_cancelling_a_queued_job_cancels_it_at_once(client, empty):
    job = client.post("/api/sync").json()["job"]
    cancelled = client.post(f"/api/jobs/{job['id']}/cancel").json()["job"]
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_requested"] is True
    assert cancelled["finished_at"].endswith("Z")


def test_cancelling_a_running_job_asks_the_agent(client, empty, db):
    from core.models import Job

    job = client.post("/api/sync").json()["job"]
    db.get(Job, job["id"]).status = "running"
    db.commit()
    answer = client.post(f"/api/jobs/{job['id']}/cancel").json()["job"]
    assert answer["status"] == "running"
    assert answer["cancel_requested"] is True


def test_cancelling_a_finished_job_changes_nothing(client, empty, db):
    from core.models import Job

    done = Job(kind="scan", status="done", finished_at=wall(2026, 9, 1))
    db.add(done)
    db.commit()
    answer = client.post(f"/api/jobs/{done.id}/cancel").json()["job"]
    assert (answer["status"], answer["cancel_requested"]) == ("done", False)


def test_cancelling_nothing_is_a_404(client, empty):
    assert client.post("/api/jobs/999999/cancel").status_code == 404
