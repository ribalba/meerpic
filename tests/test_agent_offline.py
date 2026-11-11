"""The agent and a library folder that is not there (core/roots.py): no work
is picked from it, and a failure because it went away is not counted."""

from __future__ import annotations

import pytest
from agent_helpers import ROOT, needs_db, use_db, use_library
from sqlalchemy import select

from agent import scan, stages
from core import roots
from core.config import get_settings
from core.models import Photo

pytestmark = needs_db


@pytest.fixture
def lib(tmp_path, monkeypatch):
    yield from use_library(tmp_path, monkeypatch)


@pytest.fixture
def db():
    yield from use_db()


@pytest.fixture
def share(lib, tmp_path, monkeypatch):
    """A second root, "nas", indexed while it was there."""
    folder = tmp_path / "nas"
    folder.mkdir()
    s = get_settings()
    monkeypatch.setattr(s, "library_roots", {**s.library_roots, "nas": str(folder)})
    roots.forget()
    yield folder
    roots.forget()


def pending_meta(db):
    picked = db.execute(stages.newest(
        stages.base_columns().where(Photo.meta_sig != Photo.sig), None, 100,
    )).all()
    return {row.root for row in picked}


def test_no_work_is_picked_from_a_folder_that_is_away(db, lib, share, tmp_path):
    (lib / "a.jpg").write_bytes(b"x" * 10)
    (share / "b.jpg").write_bytes(b"x" * 10)
    scan.scan(db, get_settings())
    assert pending_meta(db) == {ROOT, "nas"}
    share.rename(tmp_path / "unmounted")
    roots.forget()
    assert pending_meta(db) == {ROOT}


def test_a_failure_because_the_folder_went_away_is_not_counted(db, lib, share, tmp_path):
    (lib / "a.jpg").write_bytes(b"x" * 10)
    (share / "b.jpg").write_bytes(b"x" * 10)
    scan.scan(db, get_settings())
    share.rename(tmp_path / "unmounted")            # in the middle of a batch
    for row in db.scalars(select(Photo)).all():
        stages.failed(db, row.id, row.sig, "meta", "No such file or directory")
    db.commit()
    db.expire_all()
    counts = {p.root: p.fail_count for p in db.scalars(select(Photo))}
    assert counts == {ROOT: 1, "nas": 0}
