"""Delete: the plan the person confirmed, run exactly, here only.

rclone cannot delete in iCloud Photos, so a Delete moves the files into the
trash and remembers the ones from iCloud (core.models.DeletedFile), which is
what keeps the next sync from bringing them back. ``sync.remote`` points at
a local folder standing in for iCloud, only to show that nothing there is
touched. The plans are made by core/deletion.py, as the server makes them,
and the photos marked ``trashed_at`` as the server marks them.
"""

from __future__ import annotations

import hashlib
import os
import threading
from datetime import timedelta

import pytest
from agent_helpers import add_photo, local_remote, needs_db, use_db, use_library
from sqlalchemy import select, update

from agent import delete, jobs, scan
from agent.main import Agent
from core import deletion
from core.config import get_settings
from core.media import TRASH_DIR, display_path, story_path, thumb_path
from core.models import DeletedFile, Job, Photo
from core.timeutil import utcnow

pytestmark = needs_db


@pytest.fixture
def lib(tmp_path, monkeypatch):
    yield from use_library(tmp_path, monkeypatch)


@pytest.fixture
def db():
    yield from use_db()


@pytest.fixture
def remote(lib, tmp_path, monkeypatch):
    return local_remote(tmp_path, monkeypatch)


def both(lib, remote, name, data=b"x"):
    """A file in iCloud (the remote folder) and its copy here."""
    (remote / name).write_bytes(data)
    (lib / name).write_bytes(data)


def live_edit(db, lib, remote):
    """IMG_1: a Live Photo (still + MOV) with an edit, all four in iCloud."""
    for name in ("IMG_1.HEIC", "IMG_1-edited.heic", "IMG_1.MOV"):
        both(lib, remote, name, name.encode())
    motion = add_photo(db, "IMG_1.MOV", is_companion=True)
    edit = add_photo(db, "IMG_1-edited.heic", live_video_id=motion.id)
    original = add_photo(db, "IMG_1.HEIC", live_video_id=motion.id, superseded_by=edit.id)
    return edit, original, motion


def start(db, ids):
    """What the server does on Delete: plan, mark, queue."""
    plan = deletion.plan(db, ids, get_settings())
    db.execute(update(Photo).where(Photo.id.in_(plan.photo_ids)).values(trashed_at=utcnow()))
    db.commit()
    return jobs.create("delete", {"photo_ids": ids, "plan": plan.to_json()}, running=True), plan


def run(job):
    return delete.DeleteRun(job.id, get_settings(), job.params).run()


def load(db, job_id):
    db.expire_all()
    return db.get(Job, job_id)


def names(db):
    db.expire_all()
    return {p.name: p for p in db.scalars(select(Photo))}


def notes(db):
    db.expire_all()
    return {n.name: n for n in db.scalars(select(DeletedFile))}


def trash_day(lib):
    days = list((lib / TRASH_DIR).iterdir())
    assert len(days) == 1
    return days[0]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- the happy paths -------------------------------------------------------------------------


def test_a_live_photo_with_an_edit_goes_into_the_trash_and_stays_in_icloud(db, lib, remote):
    edit, original, motion = live_edit(db, lib, remote)
    keep = add_photo(db, "IMG_2.JPG")
    both(lib, remote, "IMG_2.JPG")
    sigs = [row.sig for row in (edit, original, motion)]
    for sig in sigs:
        for path in (thumb_path(sig), story_path(sig), display_path(sig)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"cache")
    job, plan = start(db, [edit.id])
    assert len(plan.moves) == 3 and len(plan.excluded) == 3

    assert run(job) == "done"
    row = load(db, job.id)
    assert (row.message, row.error) == ("Moved 1 to the trash", "")
    assert row.progress == {"done": 1, "total": 1, "failed": 0}
    # iCloud (here: the folder standing in for it) is not touched.
    assert sorted(p.name for p in remote.iterdir()) == ["IMG_1-edited.heic", "IMG_1.HEIC", "IMG_1.MOV", "IMG_2.JPG"]
    day = trash_day(lib)
    assert sorted(p.name for p in day.iterdir()) == ["IMG_1-edited.heic", "IMG_1.HEIC", "IMG_1.MOV"]
    assert set(names(db)) == {"IMG_2.JPG"} and keep.id
    assert not any(p.exists() for sig in sigs for p in (thumb_path(sig), story_path(sig), display_path(sig)))
    assert (lib / "IMG_2.JPG").exists()
    # Each file from iCloud is remembered: name, size, content, where it went.
    kept = notes(db)
    assert set(kept) == {"IMG_1-edited.heic", "IMG_1.HEIC", "IMG_1.MOV"}
    for name, note in kept.items():
        assert (note.root, note.size, note.sha256) == ("icloud", len(name.encode()), sha(name.encode()))
        assert note.trash == str(day / name)
        assert note.mtime_ns == os.stat(day / name).st_mtime_ns


def test_files_not_in_icloud_are_only_moved(db, lib, remote):
    (lib / "imports").mkdir()
    (lib / "imports" / "a.jpg").write_bytes(b"x")
    row = add_photo(db, "a.jpg", rel_path="imports/a.jpg")
    job, plan = start(db, [row.id])
    assert plan.excluded == []
    assert run(job) == "done"
    assert load(db, job.id).message == "Moved 1 to the trash"
    assert (trash_day(lib) / "imports" / "a.jpg").read_bytes() == b"x"
    assert notes(db) == {}


def test_the_trash_never_overwrites(db, lib, remote):
    both(lib, remote, "a.jpg", b"new")
    row = add_photo(db, "a.jpg")
    job, plan = start(db, [row.id])
    first = plan.groups[0].steps[0].trash
    (lib / TRASH_DIR / first.rsplit("/", 2)[-2]).mkdir(parents=True)
    with open(first, "wb") as fh:
        fh.write(b"old")
    assert run(job) == "done"
    day = trash_day(lib)
    assert (day / "a.jpg").read_bytes() == b"old" and (day / "a (2).jpg").read_bytes() == b"new"
    assert notes(db)["a.jpg"].trash == str(day / "a (2).jpg")


def test_a_row_gone_since_the_plan_is_skipped(db, lib, remote):
    both(lib, remote, "a.jpg")
    row = add_photo(db, "a.jpg")
    job, _ = start(db, [row.id])
    db.delete(db.get(Photo, row.id))
    db.commit()
    assert run(job) == "done"
    assert (lib / "a.jpg").exists() and notes(db) == {}


def test_a_file_already_gone_is_still_remembered_by_name(db, lib):
    row = add_photo(db, "a.jpg", size=1234)
    job, _ = start(db, [row.id])
    assert run(job) == "done"
    note = notes(db)["a.jpg"]
    assert (note.size, note.sha256, note.trash) == (1234, None, None)
    assert "no file here" in load(db, job.id).log


# --- failures ---------------------------------------------------------------------------------


def test_a_move_that_fails_puts_the_group_back_and_the_rest_goes_on(db, lib, remote, monkeypatch):
    edit, _, _ = live_edit(db, lib, remote)
    both(lib, remote, "good.jpg")
    good = add_photo(db, "good.jpg")
    real = delete._rename

    def fail_on_the_motion(src, dest):
        if src.name == "IMG_1.MOV" and ".meerpic-trash" in str(dest):
            raise PermissionError(13, "Permission denied")
        real(src, dest)

    monkeypatch.setattr(delete, "_rename", fail_on_the_motion)
    job, _ = start(db, [edit.id, good.id])
    assert run(job) == "failed"
    row = load(db, job.id)
    assert row.message == "Moved 1 of 2 to the trash"
    assert row.error.startswith("IMG_1-edited.heic: PermissionError")
    assert row.progress == {"done": 1, "total": 2, "failed": 1}
    # The two stills that had moved are back where they were, as are the rows.
    for name in ("IMG_1.HEIC", "IMG_1-edited.heic", "IMG_1.MOV"):
        assert (lib / name).read_bytes() == name.encode()
    rows = names(db)
    assert set(rows) == {"IMG_1.HEIC", "IMG_1-edited.heic", "IMG_1.MOV"}
    assert all(p.trashed_at is None for p in rows.values())
    assert set(notes(db)) == {"good.jpg"}
    assert sorted(p.name for p in trash_day(lib).iterdir()) == ["good.jpg"]


@pytest.mark.parametrize("tamper, why", [
    (lambda s: setattr(s, "local", "/etc/passwd"), "is not this photo's"),
    (lambda s: setattr(s, "trash", "/tmp/elsewhere/a.jpg"), "is not in"),
    (lambda s: setattr(s, "excluded", "someone-elses.jpg"), "does not match"),
    (lambda s: setattr(s, "excluded", None), "does not match"),
])
def test_a_step_that_is_not_a_plans_is_refused(db, lib, remote, tamper, why):
    both(lib, remote, "a.jpg")
    row = add_photo(db, "a.jpg")
    plan = deletion.plan(db, [row.id], get_settings())
    tamper(plan.groups[0].steps[0])
    job = jobs.create("delete", {"photo_ids": [row.id], "plan": plan.to_json()}, running=True)
    assert run(job) == "failed"
    error = load(db, job.id).error
    assert "refused" in error and why in error
    assert "moved" not in load(db, job.id).log                # nothing was moved
    assert (lib / "a.jpg").exists() and set(names(db)) == {"a.jpg"} and notes(db) == {}


def test_delete_turned_off(db, lib, remote, monkeypatch):
    both(lib, remote, "a.jpg")
    row = add_photo(db, "a.jpg")
    job, _ = start(db, [row.id])
    monkeypatch.setattr(get_settings(), "delete_enabled", False)
    assert run(job) == "failed"
    assert "turned off" in load(db, job.id).error
    assert names(db)["a.jpg"].trashed_at is None and (lib / "a.jpg").exists()


def test_a_cancel_between_photos(db, lib, remote):
    both(lib, remote, "a.jpg")
    row = add_photo(db, "a.jpg")
    job, _ = start(db, [row.id])
    j = load(db, job.id)
    j.cancel_requested = True
    db.commit()
    assert run(job) == "cancelled"
    assert names(db)["a.jpg"].trashed_at is None and (lib / "a.jpg").exists()


def test_a_job_without_a_plan(db, lib):
    job = jobs.create("delete", {"photo_ids": [1], "plan": {"groups": [{"steps": []}]}}, running=True)
    assert run(job) == "failed"
    assert "cannot be read" in load(db, job.id).error


def test_a_job_from_when_delete_was_in_icloud_is_not_run(db, lib, remote):
    """Queued before this version: agreed to as a delete in iCloud. It fails,
    and its photo comes back into the grid, file untouched."""
    both(lib, remote, "a.jpg")
    row = add_photo(db, "a.jpg", trashed_at=utcnow())
    step = {"photo_id": row.id, "name": "a.jpg", "local": str(lib / "a.jpg"),
            "trash": str(lib / TRASH_DIR / "2026-09-29" / "a.jpg"),
            "remote": f"{remote}/a.jpg", "argv": ["rclone", "deletefile", f"{remote}/a.jpg"],
            "command": f"rclone deletefile {remote}/a.jpg"}
    plan = {"groups": [{"photo_id": row.id, "steps": [step]}], "commands": [step["command"]],
            "moves": [], "missing": []}
    job = jobs.create("delete", {"photo_ids": [row.id], "plan": plan}, running=True)
    assert run(job) == "failed"
    assert "cannot be read" in load(db, job.id).error
    assert names(db)["a.jpg"].trashed_at is None
    assert (lib / "a.jpg").exists() and (remote / "a.jpg").exists() and notes(db) == {}


# --- coming back ---------------------------------------------------------------------------


def deleted(db, lib, remote, name="a.jpg", data=b"abc"):
    """``name`` deleted here, as a Delete leaves it."""
    both(lib, remote, name, data)
    row = add_photo(db, name, size=len(data))
    job, _ = start(db, [row.id])
    assert run(job) == "done"
    return notes(db)[name]


def test_a_deleted_photo_back_under_a_new_name_goes_to_the_trash_again(db, lib, remote):
    """iCloud renamed it (another IMG_0001 arrived), so the sync's exclude
    did not match, and the copy brought it down."""
    deleted(db, lib, remote)
    (lib / "a_AbCdEf.jpg").write_bytes(b"abc")
    result = scan.scan(db, get_settings())
    assert result.returned == 1 and result.new == 0
    assert names(db) == {}
    assert not (lib / "a_AbCdEf.jpg").exists()
    back = trash_day(lib) / "a_AbCdEf.jpg"
    assert back.read_bytes() == b"abc"
    # Remembered under the new name from now on.
    (note,) = notes(db).values()
    assert (note.name, note.trash) == ("a_AbCdEf.jpg", str(back))


def test_a_deleted_photo_moved_back_out_of_the_trash_is_shown_again(db, lib, remote):
    note = deleted(db, lib, remote)
    os.rename(note.trash, lib / "a.jpg")
    result = scan.scan(db, get_settings())
    assert result.returned == 0 and result.new == 1
    assert set(names(db)) == {"a.jpg"} and notes(db) == {}


def test_the_same_size_with_other_content_is_a_new_photo(db, lib, remote):
    deleted(db, lib, remote)
    (lib / "b.jpg").write_bytes(b"xyz")
    assert scan.scan(db, get_settings()).new == 1
    assert set(names(db)) == {"b.jpg"} and set(notes(db)) == {"a.jpg"}


def test_after_the_trash_is_emptied_it_still_goes_back(db, lib, remote):
    trashed = deleted(db, lib, remote).trash
    db.execute(update(DeletedFile).values(trash=None))
    db.commit()
    os.unlink(trashed)
    (lib / "a.jpg").write_bytes(b"abc")                         # an rclone copy run by hand
    assert scan.scan(db, get_settings()).returned == 1
    assert names(db) == {} and not (lib / "a.jpg").exists()


# --- the trash -----------------------------------------------------------------------------


def test_purge_empties_old_days_only(db, lib):
    s = get_settings()
    trash = lib / TRASH_DIR
    today = delete.today(s)
    for day in (today, today - timedelta(days=30), today - timedelta(days=31), today - timedelta(days=400)):
        (trash / day.isoformat()).mkdir(parents=True)
        (trash / day.isoformat() / "a.jpg").write_bytes(b"x")
    (trash / "notes").mkdir()
    old = str(trash / (today - timedelta(days=31)).isoformat() / "a.jpg")
    new = str(trash / today.isoformat() / "a.jpg")
    db.add_all([DeletedFile(root="icloud", name="old.jpg", size=1, mtime_ns=1, trash=old),
                DeletedFile(root="icloud", name="new.jpg", size=1, mtime_ns=1, trash=new)])
    db.commit()
    assert delete.purge_trash(s) == 2
    assert sorted(p.name for p in trash.iterdir()) == sorted(
        [today.isoformat(), (today - timedelta(days=30)).isoformat(), "notes"])
    assert delete.purge_trash(s) == 0
    # The notes stay (the photos stay out of every sync); only the purged
    # trash copy is marked gone, which is not "taken back out".
    kept = notes(db)
    assert kept["old.jpg"].trash is None and kept["new.jpg"].trash == new


# --- in the agent ----------------------------------------------------------------------------


def test_a_delete_waits_while_a_sync_runs_and_then_runs(db, lib, remote):
    both(lib, remote, "a.jpg")
    row = add_photo(db, "a.jpg")
    plan = deletion.plan(db, [row.id], get_settings())
    j = jobs.create("delete", {"photo_ids": [row.id], "plan": plan.to_json()})
    agent = Agent(get_settings())
    try:
        release = threading.Event()
        agent.remote_thread = threading.Thread(target=release.wait, daemon=True)
        agent.remote_kind = "sync"
        agent.remote_thread.start()
        agent.handle_jobs(db)
        assert load(db, j.id).status == "queued"
        assert agent.phase() == "syncing"
        release.set()
        agent.remote_thread.join(timeout=5)
        # A --sync in a terminal holds the remote as well.
        other = jobs.create("sync", running=True)
        agent.handle_jobs(db)
        assert load(db, j.id).status == "queued"
        jobs.finish(other.id, "done")
        agent.handle_jobs(db)
        assert agent.remote_kind == "delete"
        agent.remote_thread.join(timeout=30)
    finally:
        agent.pipeline.close()
    assert load(db, j.id).status == "done"
    assert not (lib / "a.jpg").exists() and (remote / "a.jpg").exists()
