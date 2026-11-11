"""Offloading old videos: which ones, the deletion and its checks, the scan
and the sync around them, fetching one back, and their small copies.

iCloud is a local folder here (``local_remote``), copied with the real
rclone where a test says so. The small copies are bytes unless a test is
about them: whether one plays is ffprobe's to say, and most tests are not.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from agent_helpers import (
    ROOT,
    add_photo,
    local_remote,
    make_video,
    needs_db,
    needs_ffmpeg,
    needs_h264_encoder,
    needs_rclone,
    set_mtime,
    shared_library,
    use_db,
    use_library,
)
from sqlalchemy import select, update

from agent import albums, jobs, offload, rclone, scan, sync, video
from agent.albums import Entry
from agent.delete import DeleteRun
from agent.previews import Previews
from core import deletion
from core.config import get_settings
from core.database import SessionLocal
from core.media import OFFLOAD_DIR, TRASH_DIR, lowres_path, make_sig, offload_path, preview_path
from core.models import DeletedFile, Job, Photo
from core.offloading import due, offloaded, remote_of
from core.timeutil import utcnow

OLD = datetime(2022, 5, 1, 12, 0)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    yield from use_library(tmp_path, monkeypatch)


@pytest.fixture
def db():
    yield from use_db()


@pytest.fixture
def remote(lib, tmp_path, monkeypatch):
    return local_remote(tmp_path, monkeypatch)


@pytest.fixture
def on(lib, monkeypatch):
    """Offloading on: videos taken more than a year ago."""
    monkeypatch.setattr(get_settings(), "offload_after", 365)


@pytest.fixture
def plays(monkeypatch):
    """Every small copy stands in for its original (ffprobe is not asked)."""
    monkeypatch.setattr(offload, "lowres_problem", lambda path, row, ffprobe: None)


def old_video(db, lib, remote, name="IMG_1.MOV", data=b"v" * 4000, small=b"s" * 100, root=ROOT,
              **cols):
    """An old video, indexed through and through, here and (unless
    ``remote`` is None) in iCloud with the same size and time, as rclone
    copy leaves it. ``small`` is its small copy, or None for none yet."""
    path = lib / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    set_mtime(path, OLD)
    if remote is not None:
        shutil.copy2(path, remote / name)
    st = path.stat()
    sig = make_sig(root, name, st.st_size, st.st_mtime_ns)
    values = dict(
        root=root, size=st.st_size, mtime_ns=st.st_mtime_ns, sig=sig, taken_at=OLD, taken_local=OLD,
        sort_at=OLD, date_source="quicktime", duration=2.0, meta_sig=sig, thumb_sig=sig,
        story_sig=sig, ocr_sig=sig, nsfw_sig=sig,
    )
    if small is not None:
        values["lowres_sig"] = sig
        copy = lowres_path(root, name, sig)
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(small)
    values.update(cols)
    return add_photo(db, name, **values)


def reload(db, row):
    db.expire_all()
    return db.get(Photo, row.id)


def load_job(db, job_id):
    db.expire_all()
    return db.get(Job, job_id)


def run_offload(params=None, **kwargs):
    job = jobs.create("offload", params or {}, running=True)
    run = offload.OffloadRun(job.id, get_settings(), job.params, **kwargs)
    return job, run.run()


def run_fetch(row):
    job = jobs.create("fetch", {"photo_id": row.id}, running=True)
    return job, offload.FetchRun(job.id, get_settings(), job.params).run()


def due_names(db):
    return set(db.scalars(select(Photo.name).where(*due(get_settings()))))


# --- which videos ------------------------------------------------------------------------


@needs_db
def test_only_old_videos_from_icloud_are_due(db, lib, on):
    recent = utcnow() - timedelta(days=3)
    long_ago = utcnow() - timedelta(days=90)            # past offload.keep_fetched (30)
    dated = dict(taken_at=OLD, taken_local=OLD, sort_at=OLD, date_source="quicktime")
    add_photo(db, "old.MOV", **dated)
    add_photo(db, "fetched-long-ago.MOV", fetched_at=long_ago, **dated)
    add_photo(db, "new.MOV", taken_at=recent, taken_local=recent, sort_at=recent, date_source="quicktime")
    add_photo(db, "motion.MOV", is_companion=True, **dated)
    add_photo(db, "still.HEIC", **dated)
    add_photo(db, "sub/folder.MOV", **dated)
    add_photo(db, "deleted.MOV", icloud_deleted=True, **dated)
    add_photo(db, "trashed.MOV", trashed_at=utcnow(), **dated)
    add_photo(db, "fetched-lately.MOV", fetched_at=recent, **dated)
    add_photo(db, "undated.MOV")
    assert due_names(db) == {"old.MOV", "fetched-long-ago.MOV"}


@needs_db
def test_nothing_is_due_when_offloading_is_off(db, lib, monkeypatch):
    add_photo(db, "old.MOV", taken_at=OLD, taken_local=OLD, sort_at=OLD, date_source="quicktime")
    assert due_names(db) == set()
    monkeypatch.setattr(get_settings(), "offload_after", 365)
    monkeypatch.setattr(get_settings(), "sync_enabled", False)
    assert due_names(db) == set()


# --- the offload ---------------------------------------------------------------------------


@needs_db
@needs_rclone
def test_an_offload_deletes_the_original_and_keeps_the_rest(db, lib, remote, on, plays):
    row = old_video(db, lib, remote)
    preview = preview_path(row.sig)
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"720p")
    with SessionLocal() as other:
        other.execute(update(Photo).where(Photo.id == row.id).values(preview_sig=row.sig))
        other.commit()

    job, status = run_offload()
    assert status == "done"
    assert load_job(db, job.id).message.startswith("Offloaded 1 video(s)")
    assert not (lib / "IMG_1.MOV").exists()
    assert (remote / "IMG_1.MOV").exists()                          # iCloud keeps it
    assert lowres_path(ROOT, "IMG_1.MOV", row.sig).read_bytes() == b"s" * 100
    assert not preview.exists()                                     # the small copy is its preview now
    sig = row.sig
    row = reload(db, row)
    assert row.offloaded_at is not None and row.preview_sig == ""
    assert row.sig == row.thumb_sig == row.story_sig == sig        # everything derived still its own
    assert offloaded(row)


@needs_db
@needs_rclone
def test_an_original_icloud_does_not_hold_stays(db, lib, remote, on, plays):
    missing = old_video(db, lib, None, "missing.MOV")
    bigger = old_video(db, lib, remote, "bigger.MOV")
    (remote / "bigger.MOV").write_bytes(b"v" * 5000)
    set_mtime(remote / "bigger.MOV", OLD)
    other_day = old_video(db, lib, remote, "other-day.MOV")
    set_mtime(remote / "other-day.MOV", OLD + timedelta(days=1))
    changed = old_video(db, lib, remote, "changed.MOV")
    (lib / "changed.MOV").write_bytes(b"w" * 4000)                  # since it was indexed

    job, status = run_offload()
    assert status == "done"
    log = load_job(db, job.id).log
    assert "kept missing.MOV: iCloud lists no file of this name" in log
    assert "kept bigger.MOV: iCloud's file of this name is not this one" in log
    assert "kept other-day.MOV: iCloud's file of this name is not this one" in log
    assert "kept changed.MOV: the file changed since it was indexed" in log
    for row in (missing, bigger, other_day, changed):
        assert (lib / row.name).exists()
        assert reload(db, row).offloaded_at is None


@needs_db
@needs_rclone
def test_a_dry_run_deletes_nothing_and_says_what_would_go(db, lib, remote, on, plays):
    ready = old_video(db, lib, remote, "ready.MOV")
    waiting = old_video(db, lib, remote, "waiting.MOV", small=None)
    job, status = run_offload({"dry_run": True})
    assert status == "done"
    j = load_job(db, job.id)
    assert j.message.startswith("Would offload 1 video(s)")
    assert "would offload ready.MOV" in j.log
    assert "kept waiting.MOV: its small copy is not made yet" in j.log
    for row in (ready, waiting):
        assert (lib / row.name).exists()
        assert reload(db, row).offloaded_at is None


@needs_db
def test_a_small_copy_that_saves_little_costs_no_listing(db, lib, on, monkeypatch):
    old_video(db, lib, None, "tiny.MOV", data=b"v" * 150, small=b"s" * 100)
    monkeypatch.setattr(rclone, "run", lambda *a, **k: pytest.fail("listed iCloud for nothing"))
    assert offload.candidates(db, get_settings()) == []
    assert not offload.has_work(db, get_settings())
    _, status = run_offload()
    assert status == "done"


@needs_db
def test_an_offload_after_a_refresh_uses_its_listing(db, lib, on, plays, monkeypatch):
    row = old_video(db, lib, None)
    listed = [Entry(name=row.name, size=row.size, mtime=row.mtime_ns // 1_000_000_000)]
    monkeypatch.setattr(rclone, "run", lambda *a, **k: pytest.fail("listed iCloud a second time"))
    _, status = run_offload(listed={ROOT: listed})
    assert status == "done"
    assert reload(db, row).offloaded_at is not None
    assert not (lib / row.name).exists()


@needs_db
def test_an_original_that_cannot_be_deleted_leaves_the_row_unmarked(db, lib, on, plays, monkeypatch):
    row = old_video(db, lib, None)
    listed = [Entry(name=row.name, size=row.size, mtime=row.mtime_ns // 1_000_000_000)]
    real = os.unlink

    def unlink(path, *a, **k):
        if str(path).endswith(row.name):
            raise PermissionError(13, "Permission denied")
        return real(path, *a, **k)

    monkeypatch.setattr(os, "unlink", unlink)
    job, status = run_offload(listed={ROOT: listed})
    assert status == "done"
    assert "kept IMG_1.MOV" in load_job(db, job.id).log
    assert reload(db, row).offloaded_at is None
    assert (lib / row.name).exists()


@needs_ffmpeg
def test_a_small_copy_must_play_as_long_as_its_original(tmp_path):
    small = make_video(tmp_path / "small.mp4", duration=1.0, size=(64, 48))
    row = offload.Candidate(id=1, root=ROOT, rel_path="a.MOV", name="a.MOV", kind="video",
                            size=small.stat().st_size * 10, mtime_ns=0, sig="0" * 16, duration=3.0,
                            lowres_sig="0" * 16, offloaded_at=None)
    assert "1.0 s long, the original 3.0 s" in offload.lowres_problem(small, row, "ffprobe")
    assert offload.lowres_problem(small, replace(row, duration=1.0), "ffprobe") is None
    assert "too little" in offload.lowres_problem(small, replace(row, size=small.stat().st_size), "ffprobe")


# --- the scan ------------------------------------------------------------------------------


@needs_db
def test_the_scan_keeps_an_offloaded_video_and_drops_a_deleted_one(db, lib, on):
    (lib / "keep.jpg").write_bytes(b"jpg")
    st = (lib / "keep.jpg").stat()
    add_photo(db, "keep.jpg", size=st.st_size, mtime_ns=st.st_mtime_ns,
              sig=make_sig(ROOT, "keep.jpg", st.st_size, st.st_mtime_ns))
    kept = add_photo(db, "offloaded.MOV", offloaded_at=utcnow())
    gone = add_photo(db, "gone.MOV")
    result = scan.scan(db, get_settings())
    assert result.removed == 1
    assert reload(db, kept) is not None
    assert reload(db, gone) is None


@needs_db
def test_a_video_offloaded_while_the_scan_walked_is_not_lost(db, lib, on, monkeypatch):
    """The scan read the rows, then the offload marked one and deleted its
    file, then the scan walked: its delete checks the mark again."""
    (lib / "keep.jpg").write_bytes(b"jpg")
    st = (lib / "keep.jpg").stat()
    add_photo(db, "keep.jpg", size=st.st_size, mtime_ns=st.st_mtime_ns,
              sig=make_sig(ROOT, "keep.jpg", st.st_size, st.st_mtime_ns))
    row = old_video(db, lib, None)
    real = scan.walk

    def walk(root):
        with SessionLocal() as other:
            other.execute(update(Photo).where(Photo.id == row.id).values(offloaded_at=utcnow()))
            other.commit()
        (lib / row.name).unlink()
        return real(root)

    monkeypatch.setattr(scan, "walk", walk)
    result = scan.scan(db, get_settings())
    assert result.removed == 0
    assert reload(db, row) is not None


@needs_db
def test_another_file_where_an_offloaded_video_was_is_that_file(db, lib, on):
    row = old_video(db, lib, None)
    sig = row.sig
    offload_by_hand(db, lib, row)
    (lib / row.name).write_bytes(b"another video" * 10)
    result = scan.scan(db, get_settings())
    assert result.changed == 1
    fresh = reload(db, row)
    assert fresh.offloaded_at is None and fresh.sig != sig


# --- the sync ------------------------------------------------------------------------------


@needs_db
@needs_rclone
def test_a_sync_leaves_offloaded_videos_in_icloud(db, lib, remote, on):
    for name in ("old.MOV", "new.jpg"):
        (remote / name).write_bytes(name.encode())
    add_photo(db, "old.MOV", offloaded_at=utcnow())
    job = jobs.create("sync", running=True)
    assert sync.SyncRun(job.id, get_settings()).run() == "done"
    assert sorted(p.name for p in lib.iterdir()) == ["new.jpg"]
    assert "leaving out 1 video(s) offloaded" in load_job(db, job.id).log


# --- fetching one back ---------------------------------------------------------------------


def offload_by_hand(db, lib, row):
    (lib / row.name).unlink()
    with SessionLocal() as other:
        other.execute(update(Photo).where(Photo.id == row.id).values(offloaded_at=utcnow(), preview_sig=""))
        other.commit()
    db.expire_all()


@needs_db
@needs_rclone
def test_a_fetch_brings_the_original_back_as_it_was(db, lib, remote, on):
    row = old_video(db, lib, remote)
    offload_by_hand(db, lib, row)
    assert row.name in due_names(db)

    job, status = run_fetch(row)
    assert status == "done", load_job(db, job.id).error
    st = (lib / row.name).stat()
    assert (st.st_size, st.st_mtime_ns) == (row.size, row.mtime_ns)       # the same sig
    fresh = reload(db, row)
    assert fresh.offloaded_at is None and fresh.fetched_at is not None
    assert row.name not in due_names(db)                                   # stays a while
    assert not list((offload_path(ROOT) / offload.FETCHING).iterdir())    # nothing left behind
    queued = db.scalars(select(Job).where(Job.kind == "preview")).all()
    assert [j.params["photo_id"] for j in queued] == [row.id]


@needs_db
@needs_rclone
def test_a_fetch_refuses_a_different_file_under_the_name(db, lib, remote, on):
    row = old_video(db, lib, remote)
    offload_by_hand(db, lib, row)
    (remote / row.name).write_bytes(b"x" * 10)                             # another video now
    job, status = run_fetch(row)
    assert status == "failed"
    assert "is not the video kept here" in load_job(db, job.id).error
    assert not (lib / row.name).exists()
    assert reload(db, row).offloaded_at is not None
    assert not list((offload_path(ROOT) / offload.FETCHING).iterdir())


@needs_db
def test_a_fetch_needs_somewhere_to_fetch_from(db, lib, on):
    row = add_photo(db, "sub/IMG_2.MOV", offloaded_at=utcnow())
    job, status = run_fetch(row)
    assert status == "failed"
    assert "did not come from iCloud" in load_job(db, job.id).error


@needs_db
def test_an_offloaded_video_is_no_720p_work_even_when_no_longer_due(db, lib, on, monkeypatch):
    """Offloading turned off since: its original is still in iCloud only."""
    row = old_video(db, lib, None)
    offload_by_hand(db, lib, row)
    monkeypatch.setattr(get_settings(), "offload_after", 0)
    assert Previews(get_settings())._next_any(db) is None


@needs_db
def test_no_720p_copy_is_made_of_an_offloaded_original(db, lib, on):
    row = old_video(db, lib, None)
    offload_by_hand(db, lib, row)
    job = jobs.create("preview", {"photo_id": row.id}, running=True)
    Previews(get_settings()).request(db, job.id, job.params)
    j = load_job(db, job.id)
    assert j.status == "failed" and "in iCloud only" in j.error


# --- the small copies ----------------------------------------------------------------------


@needs_db
def test_old_videos_get_a_small_copy_instead_of_720p(db, lib, on, monkeypatch):
    recent = utcnow() - timedelta(days=3)
    old = old_video(db, lib, None, small=None)
    new = add_photo(db, "new.MOV", taken_at=recent, taken_local=recent, sort_at=recent,
                    date_source="quicktime", meta_sig=make_sig(ROOT, "new.MOV", 100, 1))
    previews = Previews(get_settings())
    assert previews._next_any(db) == (db.get(Photo, new.id), "preview")
    assert previews._next_background(db).id == new.id
    with SessionLocal() as other:
        other.execute(update(Photo).where(Photo.id == new.id).values(preview_sig=new.sig))
        other.commit()
    db.expire_all()
    # The old one is never a 720p job, and is a small copy one whatever
    # video.previews says.
    assert previews._next_background(db) is None
    assert previews._next_any(db) == (db.get(Photo, old.id), "lowres")
    monkeypatch.setattr(get_settings(), "video_previews", "on-demand")
    assert previews._next_any(db) == (db.get(Photo, old.id), "lowres")


@needs_db
@needs_ffmpeg
@needs_h264_encoder()
def test_a_small_copy_is_small_and_lives_in_the_library(db, lib, on, monkeypatch):
    monkeypatch.setattr(get_settings(), "offload_height", 120)
    src = make_video(lib / "IMG_9.MOV", duration=1.0, size=(320, 240))
    set_mtime(src, OLD)
    st = src.stat()
    sig = make_sig(ROOT, src.name, st.st_size, st.st_mtime_ns)
    row = add_photo(db, src.name, size=st.st_size, mtime_ns=st.st_mtime_ns, sig=sig, meta_sig=sig,
                    taken_at=OLD, taken_local=OLD, sort_at=OLD, date_source="quicktime",
                    duration=1.0, video_codec="mpeg4")
    previews = Previews(get_settings())
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        previews.tick(db)
        if not previews.running and reload(db, row).lowres_sig:
            break
        time.sleep(0.1)
    fresh = reload(db, row)
    assert fresh.lowres_sig == sig, fresh.error
    out = lowres_path(ROOT, src.name, sig)
    assert out.parent == lib / OFFLOAD_DIR
    info = video.stream_info(video.probe(str(out)))
    assert (info.codec, min(info.width, info.height)) == ("h264", 120)
    assert fresh.preview_sig == ""                                   # no 720p copy was made


# --- names iCloud gives to someone else ----------------------------------------------------


@needs_db
def test_an_offloaded_video_steps_aside_when_icloud_gives_its_name_away(db, lib, on):
    row = add_photo(db, "IMG_1.MOV", size=1000, mtime_ns=1_600_000_000 * 10**9, offloaded_at=utcnow())
    same = add_photo(db, "IMG_2.MOV", size=1000, mtime_ns=1_600_000_000 * 10**9, offloaded_at=utcnow())
    entries = [Entry("IMG_1.MOV", 2000, 1_700_000_000), Entry("IMG_2.MOV", 1000, 1_600_000_000)]
    assert albums.step_aside(db, get_settings(), entries) == 1
    db.commit()
    moved = reload(db, row)
    assert moved.rel_path == f"{OFFLOAD_DIR}/IMG_1.{moved.sig}.mp4"
    assert offloaded(moved)
    assert reload(db, same).rel_path == "IMG_2.MOV"
    assert sync.offloaded_names(get_settings()) == ["IMG_2.MOV"]       # the new IMG_1 may come


# --- delete --------------------------------------------------------------------------------


@needs_db
def test_deleting_an_offloaded_video_moves_its_small_copy_to_the_trash(db, lib, on):
    row = old_video(db, lib, None)
    offload_by_hand(db, lib, row)
    small = lowres_path(ROOT, row.name, row.sig)
    plan = deletion.plan(db, [row.id], get_settings())
    (step,) = plan.groups[0].steps
    assert step.local == str(small) and step.excluded == row.name
    job = jobs.create("delete", {"photo_ids": [row.id], "plan": plan.to_json()}, running=True)
    assert DeleteRun(job.id, get_settings(), job.params).run() == "done"
    assert not small.exists()
    (day,) = (lib / TRASH_DIR).iterdir()
    assert [p.name for p in day.iterdir()] == [small.name]
    (note,) = db.scalars(select(DeletedFile)).all()
    assert (note.name, note.size, note.mtime_ns, note.sha256) == (row.name, row.size, row.mtime_ns, None)


# --- the Shared Library ([sync] remotes) ----------------------------------------------


@needs_db
def test_videos_from_the_shared_library_are_offloaded_too(db, lib, on, tmp_path, monkeypatch):
    shared, folder = shared_library(tmp_path, monkeypatch)
    row = old_video(db, folder, shared, root="shared")
    assert row.name in due_names(db)
    assert remote_of(get_settings(), "shared") == str(shared)
    monkeypatch.setattr(get_settings(), "sync_enabled", False)
    assert remote_of(get_settings(), "shared") is None


@needs_db
@needs_rclone
def test_an_offload_checks_each_library_against_its_own_remote(db, lib, remote, on, plays, tmp_path,
                                                               monkeypatch):
    """Both libraries have an IMG_1.MOV, two different videos. The refresh
    handed over the main library's listing; the Shared Library is listed."""
    shared, folder = shared_library(tmp_path, monkeypatch)
    mine = old_video(db, lib, remote)
    theirs = old_video(db, folder, shared, data=b"t" * 5000, root="shared")
    listed = {ROOT: [Entry(name=mine.name, size=mine.size, mtime=mine.mtime_ns // 1_000_000_000)]}
    asked = []
    real = rclone.run
    monkeypatch.setattr(rclone, "run", lambda argv, **k: asked.append(argv[1:]) or real(argv, **k))
    job, status = run_offload(listed=listed)
    assert status == "done", load_job(db, job.id).log
    assert asked == [["lsjson", "--files-only", str(shared)]]
    assert reload(db, mine).offloaded_at is not None and reload(db, theirs).offloaded_at is not None
    assert not (lib / mine.name).exists() and not (folder / theirs.name).exists()


@needs_db
@needs_rclone
def test_a_fetch_brings_a_shared_librarys_original_back_into_its_folder(db, lib, on, tmp_path,
                                                                        monkeypatch):
    shared, folder = shared_library(tmp_path, monkeypatch)
    row = old_video(db, folder, shared, root="shared")
    offload_by_hand(db, folder, row)
    job, status = run_fetch(row)
    assert status == "done", load_job(db, job.id).error
    assert (folder / row.name).stat().st_size == row.size
    assert not (lib / row.name).exists()
    assert reload(db, row).offloaded_at is None


@needs_db
def test_an_empty_listing_holds_back_its_own_library_only(db, lib, remote, on, plays, tmp_path,
                                                          monkeypatch):
    """A Shared Library that lists nothing (left, emptied, or a session gone
    wrong) keeps its videos, and the main library is offloaded as usual."""
    shared, folder = shared_library(tmp_path, monkeypatch)
    mine = old_video(db, lib, remote)
    theirs = old_video(db, folder, None, root="shared")
    listed = {ROOT: [Entry(name=mine.name, size=mine.size, mtime=mine.mtime_ns // 1_000_000_000)],
              "shared": []}
    job, status = run_offload(listed=listed)
    assert status == "done"
    assert reload(db, mine).offloaded_at is not None
    assert reload(db, theirs).offloaded_at is None and (folder / theirs.name).exists()
    assert "kept 1 video(s) in shared: iCloud listed no photos there" in load_job(db, job.id).log
