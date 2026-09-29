"""Scan: new, changed and gone files, and the rule that an empty or missing
library deletes nothing."""

from __future__ import annotations

# Naive datetimes on purpose: meerpic stores naive UTC and naive wall clocks.
# ruff: noqa: DTZ001
import os
import time
from datetime import datetime

import pytest
from agent_helpers import ROOT, needs_db, set_mtime, use_db, use_library
from sqlalchemy import select

from agent import scan
from core.config import get_settings
from core.media import make_sig
from core.models import UNDATED, Photo

pytestmark = needs_db


@pytest.fixture
def lib(tmp_path, monkeypatch):
    yield from use_library(tmp_path, monkeypatch)


@pytest.fixture
def db():
    yield from use_db()


def rows(db):
    db.expire_all()
    return {p.rel_path: p for p in db.scalars(select(Photo))}


def write(path, data=b"x" * 10, mtime: datetime | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime:
        set_mtime(path, mtime)
    return path


def test_new_files_get_a_row_and_a_provisional_date(db, lib):
    write(lib / "IMG_1.HEIC", mtime=datetime(2025, 6, 1, 10, 0))
    write(lib / "sub" / "dir" / "clip one.MOV", mtime=datetime(2025, 6, 2, 10, 0))
    write(lib / "odd ／+,name.jpg")
    write(lib / ".hidden.jpg")
    write(lib / ".trash" / "old.jpg")
    write(lib / "IMG_1.AAE")
    write(lib / "IMG_2.HEIC.partial")
    result = scan.scan(db, get_settings())
    assert (result.new, result.changed, result.removed) == (3, 0, 0)
    got = rows(db)
    assert set(got) == {"IMG_1.HEIC", "sub/dir/clip one.MOV", "odd ／+,name.jpg"}
    heic = got["IMG_1.HEIC"]
    assert (heic.root, heic.name, heic.ext, heic.kind, heic.mime) == (ROOT, "IMG_1.HEIC", "heic", "photo", "image/heic")
    assert heic.size == 10
    assert heic.sig == make_sig(ROOT, "IMG_1.HEIC", 10, heic.mtime_ns)
    assert heic.date_source == "mtime"
    assert heic.taken_at == datetime(2025, 6, 1, 10, 0)
    assert heic.taken_local == datetime(2025, 6, 1, 12, 0)      # Europe/Berlin
    assert heic.sort_at == heic.taken_at
    assert heic.meta_sig == "" and heic.thumb_sig == ""
    mov = got["sub/dir/clip one.MOV"]
    assert (mov.kind, mov.name) == ("video", "clip one.MOV")


def test_mtime_before_1971_sorts_as_undated(db, lib):
    write(lib / "old.jpg", mtime=datetime(1970, 1, 1, 0, 0, 1))
    scan.scan(db, get_settings())
    row = rows(db)["old.jpg"]
    assert row.sort_at == UNDATED
    assert row.taken_at is None and row.date_source == "none"


def test_symlinked_files_are_followed_and_folders_are_not(db, lib, tmp_path):
    outside = write(tmp_path / "elsewhere" / "real.jpg", data=b"y" * 7)
    os.symlink(outside, lib / "link.jpg")
    os.symlink(tmp_path / "elsewhere", lib / "linkdir")
    os.symlink(lib / "nowhere.jpg", lib / "dangling.jpg")
    scan.scan(db, get_settings())
    got = rows(db)
    assert set(got) == {"link.jpg"}
    assert got["link.jpg"].size == 7


def test_rescan_without_changes_changes_nothing(db, lib):
    write(lib / "a.jpg")
    scan.scan(db, get_settings())
    before = rows(db)["a.jpg"]
    sig, updated = before.sig, before.updated_at
    result = scan.scan(db, get_settings())
    assert (result.new, result.changed, result.removed) == (0, 0, 0)
    after = rows(db)["a.jpg"]
    assert (after.sig, after.updated_at) == (sig, updated)


def test_a_changed_file_gets_a_new_sig_and_a_clean_slate(db, lib):
    path = write(lib / "a.jpg", mtime=datetime(2025, 1, 1))
    scan.scan(db, get_settings())
    row = rows(db)["a.jpg"]
    old = row.sig
    row.meta_sig = row.thumb_sig = old
    row.error_stage, row.error, row.fail_count = "thumbs", "broken", 3
    db.commit()
    write(path, data=b"z" * 20, mtime=datetime(2025, 1, 2))
    result = scan.scan(db, get_settings())
    assert result.changed == 1
    row = rows(db)["a.jpg"]
    assert row.sig != old and row.size == 20
    assert (row.error_stage, row.error, row.fail_count) == ("", "", 0)
    assert row.meta_sig == old          # pending again: sig moved on


def test_a_deleted_file_loses_its_row(db, lib):
    write(lib / "a.jpg")
    gone = write(lib / "b.jpg")
    scan.scan(db, get_settings())
    gone.unlink()
    result = scan.scan(db, get_settings())
    assert result.removed == 1
    assert set(rows(db)) == {"a.jpg"}


def test_missing_root_deletes_nothing(db, lib):
    write(lib / "a.jpg")
    scan.scan(db, get_settings())
    (lib / "a.jpg").unlink()
    lib.rmdir()
    result = scan.scan(db, get_settings())
    assert result.removed == 0 and result.kept == 1
    assert "missing" in result.warnings[0]
    assert set(rows(db)) == {"a.jpg"}


def test_empty_root_deletes_nothing(db, lib):
    write(lib / "a.jpg")
    write(lib / "b.jpg")
    scan.scan(db, get_settings())
    for p in lib.iterdir():
        p.unlink()
    write(lib / "notes.txt")                # not media: still "empty"
    result = scan.scan(db, get_settings())
    assert result.removed == 0 and result.kept == 2
    assert "empty" in result.warnings[0]
    assert set(rows(db)) == {"a.jpg", "b.jpg"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_unreadable_root_and_folder_delete_nothing_under_them(db, lib):
    write(lib / "top.jpg")
    write(lib / "locked" / "inside.jpg")
    write(lib / "open" / "other.jpg")
    scan.scan(db, get_settings())
    (lib / "locked").chmod(0)
    (lib / "open" / "other.jpg").unlink()
    try:
        result = scan.scan(db, get_settings())
        assert result.removed == 1          # open/other.jpg really is gone
        assert result.kept == 1
        assert set(rows(db)) == {"top.jpg", "locked/inside.jpg"}
        lib.chmod(0)
        result = scan.scan(db, get_settings())
        assert result.removed == 0 and "cannot be read" in result.warnings[0]
    finally:
        lib.chmod(0o755)
        (lib / "locked").chmod(0o755)


def test_deleting_a_still_frees_its_companion(db, lib):
    write(lib / "IMG_1.HEIC")
    write(lib / "IMG_1.MOV")
    scan.scan(db, get_settings())
    got = rows(db)
    still, motion = got["IMG_1.HEIC"], got["IMG_1.MOV"]
    still.content_id = motion.content_id = "CID-1"
    still.live_video_id = motion.id
    motion.is_companion = True
    db.commit()
    (lib / "IMG_1.HEIC").unlink()
    scan.scan(db, get_settings())
    motion = rows(db)["IMG_1.MOV"]
    assert motion.is_companion is False


def test_non_utf8_names_are_skipped_not_fatal(db, lib):
    write(lib / "good.jpg")
    bad = os.fsencode(str(lib)) + b"/bad\xff.jpg"
    with open(bad, "wb") as fh:
        fh.write(b"x")
    result = scan.scan(db, get_settings())
    assert result.new == 1
    assert "not UTF-8" in result.warnings[0]


def test_a_big_unchanged_library_scans_fast(db, lib):
    for i in range(25_000):
        (lib / f"IMG_{i:05d}.HEIC").write_bytes(b"")
    scan.scan(db, get_settings())
    started = time.monotonic()
    result = scan.scan(db, get_settings())
    elapsed = time.monotonic() - started
    assert (result.new, result.changed, result.removed) == (0, 0, 0)
    assert elapsed < 2.5, f"unchanged scan of 25k files took {elapsed:.2f} s"
