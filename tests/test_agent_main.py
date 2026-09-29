"""The process around the pipeline: the loop's turn, the jobs it runs, the
heartbeat, the command line, and ``--test``."""

from __future__ import annotations

import sys
import textwrap
import time

import pytest
from agent_helpers import add_photo, jpeg, needs_db, needs_exiftool, use_db, use_library
from sqlalchemy import select

from agent import checks, jobs, main, status
from agent.main import Agent
from core.config import get_settings
from core.models import Job, Photo, Setting


@pytest.fixture
def lib(tmp_path, monkeypatch):
    yield from use_library(tmp_path, monkeypatch)


@pytest.fixture
def db():
    yield from use_db()


@pytest.fixture
def agent(lib):
    a = Agent(get_settings())
    yield a
    a.pipeline.close()


def job(db, job_id):
    db.expire_all()
    return db.get(Job, job_id)


@needs_db
@needs_exiftool
def test_a_turn_scans_and_indexes(db, lib, agent):
    jpeg(lib / "a.jpg")
    for _ in range(5):
        agent.turn()
    row = db.scalar(select(Photo))
    assert row.meta_sig == row.sig == row.thumb_sig


@needs_db
def test_scan_and_reindex_jobs(db, lib, agent):
    add_photo(db, "old.jpg", thumb_sig="x")
    scan_job = jobs.create("scan")
    reindex_job = jobs.create("reindex", {"stage": "thumbs"})
    jpeg(lib / "a.jpg")
    assert agent.handle_jobs(db) == 2
    s = job(db, scan_job.id)
    # old.jpg has no file: the scan removes it before the reindex runs.
    assert (s.status, s.message) == ("done", "1 new, 0 changed, 1 removed")
    r = job(db, reindex_job.id)
    assert r.status == "done" and r.message.startswith("reindex thumbs")


@needs_db
def test_bad_and_unknown_jobs(db, lib, agent):
    # A kind this agent does not know is left for one that does (it claims
    # by kind); a job that fails in dispatch is failed, not left running.
    bogus = jobs.create("bogus")
    bad = jobs.create("reindex", {"stage": "everything"})
    agent.handle_jobs(db)
    assert job(db, bogus.id).status == "queued"
    b = job(db, bad.id)
    assert b.status == "failed" and "unknown stage" in b.error and b.finished_at is not None


@needs_db
def test_sync_job_runs_on_a_thread_and_a_scan_follows(db, lib, agent, tmp_path, monkeypatch):
    script = tmp_path / "rclone"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import sys
        print('{{"level":"info","msg":"Copied (new)","object":"a.jpg"}}', file=sys.stderr)
        """))
    script.chmod(0o755)
    agent.settings = agent.settings.model_copy(update={"rclone": str(script), "sync_albums": False})
    j = jobs.create("sync")
    agent.handle_jobs(db)
    assert agent.remote_thread is not None and agent.remote_kind == "sync"
    agent.remote_thread.join(timeout=20)
    assert job(db, j.id).status == "done" and job(db, j.id).message == "1 new photo"
    agent._scan_requested = False
    agent._last_scan = time.monotonic()
    assert agent.scan_due()                     # right after a sync
    assert not agent.scan_due()


def _remote_rclone(tmp_path):
    """An rclone that copies one photo and lists one album with it."""
    script = tmp_path / "rclone"
    listing = ('[{"Path":"a.jpg","Name":"a.jpg","Size":1,"ModTime":"2025-01-01T00:00:00Z",'
               '"IsDir":false,"Metadata":{"favorite":"true","hidden":"false",'
               '"added-time":"2025-01-02T00:00:00Z"}}]')
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import sys
        cmd = sys.argv[1]
        if cmd == "copy":
            print('{{"level":"info","msg":"Copied (new)","object":"a.jpg"}}', file=sys.stderr)
        elif cmd == "lsf":
            print("All Photos/")
        elif cmd == "lsjson":
            print({listing!r})
        """))
    script.chmod(0o755)
    return script


@needs_db
def test_a_sync_ends_with_an_album_refresh(db, lib, agent, tmp_path):
    (lib / "a.jpg").write_bytes(b"x")
    add_photo(db, "a.jpg", size=1)
    agent.settings = agent.settings.model_copy(update={"rclone": str(_remote_rclone(tmp_path))})
    j = jobs.create("sync")
    queued = jobs.create("albums")                  # asked for while the sync runs
    assert agent.handle_jobs(db) == 1               # the sync; the refresh waits for it
    agent.remote_thread.join(timeout=20)
    assert job(db, j.id).status == "done"
    refresh = job(db, queued.id)
    assert (refresh.status, refresh.message) == ("done", "1 album")
    assert db.scalars(select(Job).where(Job.kind == "albums")).all() == [refresh]
    db.expire_all()
    assert db.scalar(select(Photo.favorite))


@needs_db
def test_cli_albums(db, lib, tmp_path, monkeypatch):
    add_photo(db, "a.jpg", size=1)
    monkeypatch.setattr(get_settings(), "rclone", str(_remote_rclone(tmp_path)))
    db.rollback()
    assert main.main(["--albums"]) == 0
    assert db.scalar(select(Photo.favorite))
    db.rollback()
    other = jobs.create("delete", running=True)
    assert main.main(["--albums"]) == 1             # never beside a delete
    jobs.finish(other.id, "done")
    monkeypatch.setattr(get_settings(), "sync_enabled", False)
    assert main.main(["--albums"]) == 2


@needs_db
def test_sync_job_when_sync_is_disabled(db, lib, agent):
    agent.settings = agent.settings.model_copy(update={"sync_enabled": False})
    j = jobs.create("sync")
    agent.handle_jobs(db)
    assert job(db, j.id).status == "failed"
    assert "disabled" in job(db, j.id).error


@needs_db
def test_scan_interval_is_shorter_while_syncing(db, lib, agent):
    agent._scan_requested = False
    agent._last_scan = time.monotonic() - 40
    assert not agent.scan_due()                 # interval 60
    agent.remote_thread = type("T", (), {"is_alive": lambda self: True})()
    agent.remote_kind = "sync"
    assert agent.scan_due()                     # 30 while downloading
    assert agent.phase() == "syncing"
    agent.remote_kind = "delete"
    assert agent.phase() == "deleting"
    assert not agent.scan_due()                 # a delete is not a download


@needs_db
def test_heartbeat(db, lib, agent):
    agent.heartbeat.beat()
    value = db.get(Setting, status.KEY).value
    assert set(value) == {"version", "host", "seen_at", "phase", "workers", "model", "pending"}
    assert value["seen_at"].endswith("Z")
    assert value["model"] == "fake"
    assert set(value["pending"]) >= {"meta", "thumbs", "embeddings", "previews", "story", "nsfw"}
    agent.heartbeat.beat()                      # an update, not a second row
    assert len(db.scalars(select(Setting)).all()) == 1


@needs_db
def test_the_loop_survives_a_failing_turn(db, lib, agent, monkeypatch):
    calls = []

    def turn():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database went away")
        raise KeyboardInterrupt

    monkeypatch.setattr(agent, "turn", turn)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    orphan = jobs.create("scan", running=True)
    with pytest.raises(KeyboardInterrupt):
        agent.run_forever()
    assert len(calls) == 2
    assert job(db, orphan.id).error == "agent restarted"
    # Never refreshed from iCloud: that is queued at startup.
    refresh = db.scalar(select(Job).where(Job.kind == "albums"))
    assert refresh.status == "queued" and refresh.params == {"reason": "startup"}


@needs_db
@needs_exiftool
def test_cli_once_and_reindex(db, lib, monkeypatch):
    # main() runs init_db(), whose ALTER TABLEs wait for every open
    # transaction on photos: the test's own session ends its reads first.
    jpeg(lib / "a.jpg")
    assert main.main(["--once"]) == 0
    row = db.scalar(select(Photo))
    assert row.thumb_sig == row.sig
    db.rollback()
    assert main.main(["--reindex", "thumbs"]) == 0
    assert db.scalar(select(Photo)).thumb_sig == ""
    db.rollback()
    assert main.main(["--reindex", "story", "--once"]) == 0
    row = db.scalar(select(Photo))
    assert row.thumb_sig == row.sig and row.nsfw_sig == row.sig
    db.rollback()


def test_cli_rejects_limit_without_once(capsys):
    with pytest.raises(SystemExit):
        main.main(["--limit", "5"])
    with pytest.raises(SystemExit):
        main.main(["--reindex", "nonsense"])


def test_cli_version(capsys):
    assert main.main(["--version"]) == 0
    assert capsys.readouterr().out.strip()


# --- --test -------------------------------------------------------------------------


def test_checks_individually(lib, tmp_path):
    s = get_settings()
    assert checks.check_root(lib)[0] == checks.OK
    assert checks.check_root(tmp_path / "missing")[0] == checks.FAIL
    assert checks.check_cache(s)[0] == checks.OK
    assert checks.check_model()[0] == checks.OK
    assert checks.check_nsfw(s)[0] == checks.OK
    assert checks.check_nsfw(s.model_copy(update={"nsfw_enabled": False}))[1].startswith("disabled")
    off = s.model_copy(update={"sync_enabled": False})
    assert checks.check_rclone(off) == (checks.OK, "sync disabled, not needed")
    assert checks.check_exiftool(s.model_copy(update={"exiftool": "/nonexistent"}))[0] == checks.FAIL
    assert checks.check_ffmpeg(s.model_copy(update={"ffmpeg": "/nonexistent"}))[0][1] == checks.FAIL


def test_check_rclone_remote(lib, tmp_path):
    script = tmp_path / "rclone"
    script.write_text("#!/bin/sh\necho 'other:'\necho 'iclouddrive:'\n")
    script.chmod(0o755)
    s = get_settings().model_copy(update={"rclone": str(script)})
    assert checks.check_rclone(s)[0] == checks.OK
    s = s.model_copy(update={"sync_remote": "photos:All"})
    verdict, detail = checks.check_rclone(s)
    assert verdict == checks.FAIL and "photos:" in detail


@needs_db
def test_check_db_and_the_whole_report(lib):
    assert checks.check_db()[0] == checks.OK
    lines = []
    failures = checks.run(get_settings(), lines.append)
    assert any("database" in line for line in lines)
    assert lines[-1] in ("all checks passed", f"{failures} check(s) failed")


def test_relink_applies_the_current_pairing_to_old_rows(db, lib):
    """A library indexed before edits were paired gets its pairs from --relink."""
    from core.models import Photo

    video = add_photo(db, "IMG_9.MOV", kind="video", content_id="C9")
    original = add_photo(db, "IMG_9.HEIC", content_id="C9")
    edit = add_photo(db, "IMG_9-edited.heic", content_id="C9")
    db.commit()
    db.rollback()
    assert main.main(["--relink"]) == 0
    db.expire_all()
    assert db.get(Photo, original.id).superseded_by == edit.id
    assert db.get(Photo, edit.id).live_video_id == video.id
    assert db.get(Photo, video.id).is_companion
