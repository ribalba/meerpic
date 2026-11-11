"""Sync: rclone's JSON log parsed line by line, and the runner driving a fake
rclone that replays recorded output.

The recorded lines are rclone v1.74's own, from a local-to-local copy with
``--use-json-log --stats 2s --stats-log-level NOTICE -v`` (paths shortened),
plus the error lines of a missing remote and a missing source.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from collections import namedtuple
from datetime import timedelta

import pytest
from agent_helpers import (
    local_remote,
    needs_db,
    needs_rclone,
    shared_library,
    use_db,
    use_library,
)

from agent import jobs, sync
from core.config import Settings, get_settings
from core.models import DeletedFile, Job
from core.timeutil import utcnow


@pytest.fixture
def lib(tmp_path, monkeypatch):
    yield from use_library(tmp_path, monkeypatch)


@pytest.fixture
def db():
    yield from use_db()

COPIED = ('{"time":"2026-09-29T10:02:02.826783706+02:00","level":"info","msg":"Copied (new)",'
          '"size":2000,"object":"IMG_1 x\uff0fy.HEIC","objectType":"*local.Object",'
          '"source":"operations/copy.go:380"}')
STATS = json.dumps({
    "time": "2026-09-29T10:02:04.821848901+02:00", "level": "notice",
    "msg": "\nTransferred:   \t    2.029 MiB / 2.863 MiB, 71%, 1.015 MiB/s, ETA 0s\n",
    "stats": {"bytes": 2127824, "checks": 0, "deletedDirs": 0, "deletes": 0,
              "elapsedTime": 2.000389969, "errors": 0, "eta": 0, "fatalError": False, "listed": 2,
              "renames": 0, "retryError": False, "speed": 1079209.491646919, "totalBytes": 3002000,
              "totalChecks": 0, "totalTransfers": 2, "transferTime": 2.000188867,
              "transferring": [{"bytes": 2125824, "eta": 0, "group": "global_stats", "name": "a.bin",
                                "percentage": 70, "size": 3000000, "speed": 1063208.44}],
              "transfers": 1},
    "source": "accounting/stats.go:551",
})
COPIED_2 = ('{"time":"2026-09-29T10:02:05.683774892+02:00","level":"info","msg":"Copied (new)",'
            '"size":3000000,"object":"a.bin","objectType":"*local.Object","source":"operations/copy.go:380"}')
STATS_DONE = json.dumps({
    "level": "notice", "msg": "\nTransferred: ...",
    "stats": {"bytes": 3002000, "checks": 0, "errors": 0, "eta": 0, "speed": 1063892.87,
              "totalBytes": 3002000, "totalChecks": 0, "totalTransfers": 2, "transfers": 2},
})
NO_REMOTE = ('{"time":"2026-09-29T10:02:05.719633854+02:00","level":"critical","msg":"Failed to create '
             'file system for \\"nosuchremote:foo\\": didn\'t find section in config file '
             '(\\"nosuchremote\\")","source":"cmd/cmd.go:102"}')
NO_SOURCE = [
    ('{"level":"error","msg":"error reading source root directory: directory not found",'
     '"object":"Local file system at /nonexistent","objectType":"*local.Fs","source":"march/march.go:538"}'),
    '{"level":"error","msg":"Attempt 1/3 failed with 1 errors and: directory not found","source":"cmd/cmd.go:283"}',
    json.dumps({"level": "notice", "msg": "...", "stats": {
        "bytes": 0, "checks": 0, "errors": 1, "eta": None, "lastError": "directory not found",
        "speed": 0, "totalBytes": 0, "totalChecks": 0, "totalTransfers": 0, "transfers": 0}}),
    '{"level":"notice","msg":"Failed to copy: directory not found","source":"cmd/cmd.go:334"}',
]
# What an expired iCloud session looks like (rclone's iclouddrive backend).
EXPIRED = ('{"level":"critical","msg":"Failed to create file system for \\"iclouddrive:PrimarySync/All '
           'Photos\\": missing icloud trust token: try refreshing it with \\"rclone config reconnect '
           'iclouddrive:\\"","source":"cmd/cmd.go:102"}')


# --- the parser --------------------------------------------------------------------


def test_a_successful_copy():
    t = sync.Tracker()
    assert t.feed(COPIED) is True
    assert t.feed(STATS) is True
    p = t.progress
    assert (p["bytes"], p["total_bytes"], p["files"], p["total_files"]) == (2127824, 3002000, 1, 2)
    assert p["percent"] == 70.9
    assert p["eta"] == 0 and p["speed"] == pytest.approx(1079209.49)
    assert p["current"] == ["a.bin"]
    assert p["copied"] == 1 and p["errors"] == 0
    t.feed(COPIED_2)
    t.feed(STATS_DONE)
    assert t.progress["percent"] == 100.0 and t.progress["current"] == []
    assert t.copied == 2
    assert t.copied_media == 1                  # a.bin is not a photo
    assert t.take_lines() == ["copied IMG_1 x\uff0fy.HEIC", "copied a.bin"]
    assert t.take_lines() == []
    assert not t.auth and t.last_error == ""


def test_old_style_copied_line_has_the_name_in_the_message():
    t = sync.Tracker()
    t.feed('{"level":"info","msg":"IMG_9.HEIC: Copied (replaced existing)"}')
    assert t.copied == 1 and t.take_lines() == ["copied IMG_9.HEIC"]


def test_errors_are_counted_and_the_last_one_kept():
    t = sync.Tracker()
    for line in NO_SOURCE:
        t.feed(line)
    assert t.error_lines == 2
    assert t.progress["errors"] == 2
    assert t.progress["eta"] is None and t.progress["percent"] is None
    assert t.last_error == "Failed to copy: directory not found"
    lines = t.take_lines()
    assert lines[0] == "ERROR Local file system at /nonexistent: error reading source root directory: directory not found"
    assert not t.auth


def test_critical_and_plain_text_lines():
    t = sync.Tracker()
    t.feed(NO_REMOTE)
    assert t.error_lines == 1 and "didn't find section" in t.last_error
    t.feed("2026/09/29 10:00:00 NOTICE: Config file not found - using defaults")
    t.feed("Error: unknown flag: --bogus")
    t.feed("")
    t.feed("{not json")
    assert t.last_error == "Error: unknown flag: --bogus"
    assert t.take_lines()[-2:] == ["Error: unknown flag: --bogus", "{not json"]


@pytest.mark.parametrize("line", [
    EXPIRED,
    '{"level":"error","msg":"HTTP error 401 (401 Unauthorized) returned body: authentication required"}',
    '{"level":"error","msg":"Two-factor authentication required"}',
    "Failed to create file system: 403 Forbidden",
])
def test_an_expired_session_is_recognised(line):
    t = sync.Tracker()
    t.feed(line)
    assert t.auth


def test_argv_is_the_users_command_with_json_logging(lib, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "sync_args", ["--transfers", "8"])
    monkeypatch.setattr(s, "rclone", "rclone")
    assert sync.build_argv(s) == [
        "rclone", "copy", "iclouddrive:PrimarySync/All Photos", str(lib),
        "--use-json-log", "--stats", "2s", "--stats-log-level", "NOTICE", "-v", "--transfers", "8",
    ]


def test_looks_like_auth():
    assert sync.looks_like_auth(EXPIRED)
    assert not sync.looks_like_auth("Failed to deletefile: object not found")
    assert not sync.looks_like_auth("")


def test_hint_uses_the_configured_remote():
    s = Settings(sync_remote="myicloud:PrimarySync/All Photos")
    assert sync.auth_hint(s) == ("The iCloud session has expired. In a terminal run: "
                                 "rclone config reconnect myicloud:")


def test_done_message_and_describe():
    assert sync.done_message(0) == "No new photos"
    assert sync.done_message(1) == "1 new photo"
    assert sync.done_message(12) == "12 new photos"
    p = sync.empty_progress() | {"files": 3, "total_files": 10, "bytes": 1536, "total_bytes": 10 * 2**20,
                                 "speed": 2**20, "eta": 125, "errors": 1}
    assert sync.describe(p) == "3/10 files, 1.5 KB / 10.0 MB, 1.0 MB/s, ETA 0:02:05, 1 error(s)"


# --- the runner, against a fake rclone -----------------------------------------------


def fake_rclone(tmp_path, lines, *, code=0, sleep=0.0, hang=False):
    """A script that records its argv and replays ``lines`` on stderr."""
    script = tmp_path / "rclone"
    argv_file = tmp_path / "argv.json"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, sys, time
        json.dump(sys.argv[1:], open({str(argv_file)!r}, "w"))
        for line in {lines!r}:
            print(line, file=sys.stderr, flush=True)
            time.sleep({sleep!r})
        if {hang!r}:
            time.sleep(600)
        sys.exit({code!r})
        """))
    script.chmod(0o755)
    return script, argv_file


def run_sync(tmp_path, monkeypatch, lines, **kw):
    script, argv_file = fake_rclone(tmp_path, lines, **kw)
    monkeypatch.setattr(get_settings(), "rclone", str(script))
    job = jobs.create("sync", running=True)
    run = sync.SyncRun(job.id, get_settings())
    return run, job.id, argv_file


def load(db, job_id):
    db.expire_all()
    return db.get(Job, job_id)


@needs_db
def test_runner_success(db, lib, tmp_path, monkeypatch):
    run, job_id, argv_file = run_sync(tmp_path, monkeypatch, [COPIED, STATS, COPIED_2, STATS_DONE])
    assert run.run() == "done"
    job = load(db, job_id)
    assert (job.status, job.message) == ("done", "1 new photo")
    assert job.progress["copied"] == 2 and job.progress["percent"] == 100.0
    assert "copied a.bin" in job.log and job.finished_at is not None
    assert json.loads(argv_file.read_text())[:3] == ["copy", "iclouddrive:PrimarySync/All Photos", str(lib)]


@needs_db
def test_runner_failure_keeps_the_last_error(db, lib, tmp_path, monkeypatch):
    run, job_id, _ = run_sync(tmp_path, monkeypatch, NO_SOURCE, code=1)
    assert run.run() == "failed"
    job = load(db, job_id)
    assert job.status == "failed"
    assert job.error == "Failed to copy: directory not found"
    assert job.progress["errors"] == 2


@needs_db
def test_runner_expired_session_gets_the_hint(db, lib, tmp_path, monkeypatch):
    run, job_id, _ = run_sync(tmp_path, monkeypatch, [EXPIRED], code=1)
    run.run()
    job = load(db, job_id)
    assert job.error == ("The iCloud session has expired. In a terminal run: "
                         "rclone config reconnect iclouddrive:")


@needs_db
def test_runner_cancel_terminates_rclone(db, lib, tmp_path, monkeypatch):
    run, job_id, _ = run_sync(tmp_path, monkeypatch, [COPIED, STATS], hang=True)
    import threading

    thread = threading.Thread(target=run.run)
    started = time.monotonic()
    thread.start()
    time.sleep(1.5)
    load(db, job_id).cancel_requested = True
    db.commit()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert time.monotonic() - started < 15
    job = load(db, job_id)
    assert job.status == "cancelled"
    assert job.progress["copied"] == 1


@needs_db
def test_runner_without_rclone(db, lib, monkeypatch):
    monkeypatch.setattr(get_settings(), "rclone", "/nonexistent/rclone")
    job = jobs.create("sync", running=True)
    assert sync.SyncRun(job.id, get_settings()).run() == "failed"
    assert "could not be started" in load(db, job.id).error


@needs_db
def test_timer(db, lib, monkeypatch):
    s = get_settings()
    assert not sync.due(s)                          # interval 0: only when asked
    monkeypatch.setattr(s, "sync_interval", 30)
    assert sync.due(s)                              # never synced
    job = jobs.create("sync")
    assert not sync.due(s)                          # one is queued
    jobs.finish(job.id, "done")
    assert not sync.due(s)                          # asked for a moment ago
    row = load(db, job.id)
    row.created_at = utcnow() - timedelta(minutes=31)
    db.commit()
    assert sync.due(s)
    monkeypatch.setattr(s, "sync_enabled", False)
    assert not sync.due(s)


@needs_db
def test_a_database_blip_does_not_end_the_download(db, lib, tmp_path, monkeypatch):
    run, job_id, _ = run_sync(tmp_path, monkeypatch, [COPIED, STATS, COPIED_2, STATS_DONE], sleep=0.4)
    real = jobs.update_job
    calls = []

    def flaky(job_id, **values):
        calls.append(1)
        if 2 <= len(calls) <= 3:
            raise ConnectionError("database restarting")
        return real(job_id, **values)

    monkeypatch.setattr(jobs, "update_job", flaky)
    assert run.run() == "done"
    job = load(db, job_id)
    assert job.status == "done" and "copied IMG_1" in job.log and "copied a.bin" in job.log


# --- photos deleted here stay in iCloud ---------------------------------------------


def test_the_exclude_file_goes_before_the_users_own_arguments(lib, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "sync_args", ["--transfers", "8"])
    argv = sync.build_argv(s, "/tmp/x.txt")
    assert argv[argv.index("--exclude-from") + 1] == "/tmp/x.txt"
    assert argv[-2:] == ["--transfers", "8"]
    assert "--exclude-from" not in sync.build_argv(s)


@pytest.mark.parametrize("name, rule", [
    ("IMG_0001.HEIC", "/IMG_0001.HEIC"),
    ("a[1].jpg", "/a\\[1\\].jpg"),
    ("b*?.jpg", "/b\\*\\?.jpg"),
    ("d{x}.jpg", "/d\\{x\\}.jpg"),
    ("e\\f.jpg", "/e\\\\f.jpg"),
    ("#c.jpg", "/#c.jpg"),                    # not a comment: the rule starts with /
    ("IMG_0988_AbcR\uff0ftp+x.JPG", "/IMG_0988_AbcR\uff0ftp+x.JPG"),
    (" lead.jpg", None),                      # rclone trims every line
    ("two\nlines.jpg", None),
])
def test_exclude_rules_match_one_name_exactly(name, rule):
    assert sync.exclude_rule(name) == rule


@needs_db
@needs_rclone
def test_a_sync_leaves_out_what_was_deleted_here(db, lib, tmp_path, monkeypatch):
    """The real rclone, copying from a local folder standing in for iCloud."""
    remote = local_remote(tmp_path, monkeypatch)
    for name in ("keep.jpg", "gone.jpg", "odd [1].jpg", "later.jpg"):
        (remote / name).write_bytes(name.encode())
    (remote / "sub").mkdir()
    (remote / "sub" / "gone.jpg").write_bytes(b"x")          # the rule is for the top only
    db.add_all([
        DeletedFile(root="icloud", name="gone.jpg", size=8, mtime_ns=1, sha256="0" * 64),
        DeletedFile(root="icloud", name="odd [1].jpg", size=11, mtime_ns=1, sha256="1" * 64),
        # Given up: another photo has the name now.
        DeletedFile(root="icloud", name=None, size=9, mtime_ns=1, sha256="2" * 64),
        # Another root's: not this sync's business.
        DeletedFile(root="camera", name="keep.jpg", size=8, mtime_ns=1, sha256="3" * 64),
    ])
    db.commit()
    made = []
    real = sync.write_excludes
    monkeypatch.setattr(sync, "write_excludes", lambda names: made.append(real(names)) or made[-1])
    job = jobs.create("sync", running=True)
    assert sync.SyncRun(job.id, get_settings()).run() == "done"
    assert sorted(p.name for p in lib.iterdir()) == ["keep.jpg", "later.jpg", "sub"]
    assert (lib / "sub" / "gone.jpg").exists()
    assert "leaving out 2 file(s) deleted here" in load(db, job.id).log
    # The rules were written for this run only.
    assert made and made[0] and not os.path.exists(made[0])


@needs_db
def test_nothing_deleted_means_no_exclude_file(db, lib, tmp_path, monkeypatch):
    run, _, argv_file = run_sync(tmp_path, monkeypatch, [STATS_DONE])
    assert run.run() == "done"
    assert "--exclude-from" not in json.loads(argv_file.read_text())


# --- more than one remote ([sync] remotes), and the room on the disk ------------------


def fake_rclones(tmp_path, runs: dict[str, tuple[int, list[str]]]):
    """An rclone for several copies in one job: appends each argv to a file,
    and for the first ``runs`` key its source contains, replays those lines
    and exits with that status (no lines and 0 otherwise)."""
    script = tmp_path / "rclone"
    argv_log = tmp_path / "argv.jsonl"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, sys
        with open({str(argv_log)!r}, "a") as fh:
            fh.write(json.dumps(sys.argv[1:]) + "\\n")
        code, lines = next((v for k, v in {runs!r}.items() if k in sys.argv[2]), (0, []))
        for line in lines:
            print(line, file=sys.stderr, flush=True)
        sys.exit(code)
        """))
    script.chmod(0o755)
    return str(script), argv_log


def calls(argv_log) -> list[list[str]]:
    if not argv_log.exists():
        return []
    return [json.loads(line) for line in argv_log.read_text().splitlines()]


Usage = namedtuple("Usage", "total used free")


def test_argv_for_another_root_with_a_limit(lib, tmp_path, monkeypatch):
    shared, folder = shared_library(tmp_path, monkeypatch)
    s = get_settings()
    monkeypatch.setattr(s, "rclone", "rclone")
    argv = sync.build_argv(s, root="shared", max_transfer=1000)
    assert argv[:4] == ["rclone", "copy", str(shared), str(folder)]
    assert argv[argv.index("--max-transfer") + 1] == "1000B"        # a bare 1000 is KiB to rclone
    assert argv[argv.index("--cutoff-mode") + 1] == "cautious"
    assert "--max-transfer" not in sync.build_argv(s)


def test_room_is_what_is_free_above_keep_free(lib, tmp_path, monkeypatch):
    s = get_settings()
    asked = []
    monkeypatch.setattr(sync.shutil, "disk_usage", lambda path: asked.append(path) or Usage(0, 0, 30 * 10**9))
    assert sync.room(s, "icloud") is None                     # keep_free = 0: no limit
    monkeypatch.setattr(s, "sync_keep_free", 10)
    assert sync.room(s, "icloud") == 20 * 10**9
    # The first copy makes the root; until then it is its parent's disk.
    monkeypatch.setattr(s, "library_roots", {"icloud": str(tmp_path / "not" / "yet")})
    assert sync.room(s, "icloud") == 20 * 10**9
    assert asked[-1] == tmp_path
    # A --max-transfer of the user's own is theirs to keep.
    monkeypatch.setattr(s, "sync_args", ["--max-transfer=5G"])
    assert sync.room(s, "icloud") is None


@needs_db
@needs_rclone
def test_every_remote_is_copied_into_a_root_of_its_own(db, lib, tmp_path, monkeypatch):
    """The real rclone, from two local folders standing in for iCloud's
    library and its Shared Library, which reuse each other's names."""
    main = local_remote(tmp_path, monkeypatch)
    shared, folder = shared_library(tmp_path, monkeypatch)
    (main / "IMG_1.HEIC").write_bytes(b"mine")
    (main / "gone.HEIC").write_bytes(b"mine too")
    (shared / "IMG_1.HEIC").write_bytes(b"theirs, another photo")
    (shared / "IMG_2.HEIC").write_bytes(b"2")
    (shared / "gone.HEIC").write_bytes(b"g")
    # Deleted from the Shared Library here: left out there, and only there.
    db.add(DeletedFile(root="shared", name="gone.HEIC", size=1, mtime_ns=1, sha256="0" * 64))
    db.commit()
    job = jobs.create("sync", running=True)
    assert sync.SyncRun(job.id, get_settings()).run() == "done"
    assert sorted(p.name for p in lib.iterdir()) == ["IMG_1.HEIC", "gone.HEIC"]
    assert sorted(p.name for p in folder.iterdir()) == ["IMG_1.HEIC", "IMG_2.HEIC"]
    assert (lib / "IMG_1.HEIC").read_bytes() == b"mine"
    assert (folder / "IMG_1.HEIC").read_bytes() == b"theirs, another photo"
    j = load(db, job.id)
    assert j.message == "4 new photos"
    assert f"copying {shared} into shared (2/2)" in j.log
    assert "leaving out 1 file(s) deleted here" in j.log
    assert (j.progress["copied"], j.progress["source"], j.progress["steps"]) == (4, "shared", 2)


@needs_db
def test_a_second_remote_that_fails_fails_the_sync_and_names_itself(db, lib, tmp_path, monkeypatch):
    shared, _ = shared_library(tmp_path, monkeypatch)
    script, argv_log = fake_rclones(tmp_path, {"shared-remote": (1, NO_SOURCE), "": (0, [COPIED])})
    monkeypatch.setattr(get_settings(), "rclone", script)
    job = jobs.create("sync", running=True)
    assert sync.SyncRun(job.id, get_settings()).run() == "failed"
    j = load(db, job.id)
    assert j.error == f"{shared}: Failed to copy: directory not found"
    assert j.progress["copied"] == 1                            # the first one's
    assert [argv[1] for argv in calls(argv_log)] == ["iclouddrive:PrimarySync/All Photos", str(shared)]


@needs_db
def test_an_expired_session_on_the_second_remote_is_still_just_the_hint(db, lib, tmp_path, monkeypatch):
    shared_library(tmp_path, monkeypatch)
    script, _ = fake_rclones(tmp_path, {"shared-remote": (1, [EXPIRED])})
    monkeypatch.setattr(get_settings(), "rclone", script)
    job = jobs.create("sync", running=True)
    sync.SyncRun(job.id, get_settings()).run()
    assert load(db, job.id).error.startswith("The iCloud session has expired.")


@needs_db
def test_no_room_means_no_copy_and_a_message_that_says_why(db, lib, tmp_path, monkeypatch):
    s = get_settings()
    script, argv_log = fake_rclones(tmp_path, {})
    monkeypatch.setattr(s, "rclone", script)
    monkeypatch.setattr(s, "sync_keep_free", 40)
    monkeypatch.setattr(sync.shutil, "disk_usage", lambda path: Usage(0, 0, 30 * 10**9))
    job = jobs.create("sync", running=True)
    assert sync.SyncRun(job.id, s).run() == "done"
    assert calls(argv_log) == []
    assert load(db, job.id).message == ("Paused before copying anything: "
                                        "the disk is down to 40 GB free (sync.keep_free)")


@needs_db
def test_a_copy_stops_where_the_room_ends_and_the_rest_waits(db, lib, tmp_path, monkeypatch):
    """rclone ends a copy that reached --max-transfer with status 8, and
    calls that errors; for the sync it is a pause, not a failure."""
    s = get_settings()
    shared_library(tmp_path, monkeypatch)
    limit = ['{"level":"notice","msg":"max transfer limit reached as set by --max-transfer - stopping transfers"}',
             '{"level":"error","msg":"Can\'t retry any of the errors - not attempting retries"}',
             STATS_DONE]
    script, argv_log = fake_rclones(tmp_path, {"PrimarySync": (8, [COPIED, *limit])})
    monkeypatch.setattr(s, "rclone", script)
    monkeypatch.setattr(s, "sync_keep_free", 10)
    frees = iter([25 * 10**9, 9 * 10**9])                       # the first copy filled it
    monkeypatch.setattr(sync.shutil, "disk_usage", lambda path: Usage(0, 0, next(frees)))
    job = jobs.create("sync", running=True)
    assert sync.SyncRun(job.id, s).run() == "done"
    (argv,) = calls(argv_log)                                    # the Shared Library waits
    assert argv[argv.index("--max-transfer") + 1] == f"{15 * 10**9}B"
    j = load(db, job.id)
    assert j.message == "1 new photo, then paused: the disk is down to 10 GB free (sync.keep_free)"
    assert j.progress["errors"] == 0
    assert "no room left for shared on its disk" in j.log


@needs_db
def test_a_full_disk_does_not_hold_up_a_root_on_another_one(db, lib, tmp_path, monkeypatch):
    s = get_settings()
    shared, folder = shared_library(tmp_path, monkeypatch)
    script, argv_log = fake_rclones(tmp_path, {"shared-remote": (0, [COPIED])})
    monkeypatch.setattr(s, "rclone", script)
    monkeypatch.setattr(s, "sync_keep_free", 10)
    disks = {str(lib): 5 * 10**9, str(folder): 100 * 10**9}
    monkeypatch.setattr(sync.shutil, "disk_usage", lambda path: Usage(0, 0, disks[str(path)]))
    job = jobs.create("sync", running=True)
    assert sync.SyncRun(job.id, s).run() == "done"
    (argv,) = calls(argv_log)
    assert argv[1:3] == [str(shared), str(folder)]
    assert argv[argv.index("--max-transfer") + 1] == f"{90 * 10**9}B"
    j = load(db, job.id)
    assert j.message.startswith("1 new photo, then paused")
    assert "no room left for icloud on its disk" in j.log


@needs_db
@needs_rclone
def test_the_real_rclone_stops_at_the_room_in_bytes(db, lib, tmp_path, monkeypatch):
    """What rclone does with the limit, not what the argv says: one 8 MB file
    fits in 10 MB of room, the second would not. Streamed the way a download
    is (a local copy may otherwise skip rclone's accounting)."""
    s = get_settings()
    remote = local_remote(tmp_path, monkeypatch)
    for name in ("a.HEIC", "b.HEIC", "c.HEIC"):
        (remote / name).write_bytes(os.urandom(8 * 2**20))
    monkeypatch.setattr(s, "sync_args", ["--transfers", "1", "--disable", "Copy", "--multi-thread-streams", "0"])
    monkeypatch.setattr(s, "sync_keep_free", 0.001)              # 1 MB
    monkeypatch.setattr(sync.shutil, "disk_usage", lambda path: Usage(0, 0, 11 * 10**6))
    job = jobs.create("sync", running=True)
    assert sync.SyncRun(job.id, s).run() == "done", load(db, job.id).error
    assert len(list(lib.iterdir())) == 1
    assert load(db, job.id).message.startswith("1 new photo, then paused")
