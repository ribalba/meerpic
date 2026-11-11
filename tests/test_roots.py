"""Which library folders are there right now (core/roots.py).

No database: what is tested is the check itself. That is a folder that
can be listed counting as there and anything else not, the deadline on a
check that hangs, and answers that are kept without making anybody wait.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from core import roots


@pytest.fixture(autouse=True)
def clean():
    roots.forget()
    yield
    roots.forget()


def config(**folders):
    """Just what roots.offline reads off the settings."""
    return SimpleNamespace(roots=folders)


def test_a_folder_that_can_be_listed_is_there_even_empty(tmp_path):
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "a.jpg").write_bytes(b"x")
    (tmp_path / "empty").mkdir()
    (tmp_path / "file").write_bytes(b"x")
    s = config(full=tmp_path / "full", empty=tmp_path / "empty",
               missing=tmp_path / "missing", file=tmp_path / "file")
    assert roots.offline(s) == {"missing", "file"}


def test_an_answer_is_kept_until_asked_fresh(tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    s = config(nas=share)
    assert roots.offline(s) == set()
    share.rmdir()
    assert roots.offline(s) == set()                 # believed for CHECK_EVERY
    assert roots.offline(s, fresh=True) == {"nas"}
    share.mkdir()
    assert roots.offline(s) == {"nas"}
    assert roots.offline(s, fresh=True) == set()


def test_a_check_that_hangs_is_not_there_by_its_deadline(tmp_path, monkeypatch):
    """A share that went away while mounted hangs instead of failing."""
    monkeypatch.setattr(roots, "DEADLINE", 0.2)
    release = threading.Event()
    real = roots.listable
    monkeypatch.setattr(roots, "listable", lambda path: release.wait(10) and real(path))
    s = config(nas=tmp_path)
    started = time.monotonic()
    assert roots.offline(s, fresh=True) == {"nas"}
    assert time.monotonic() - started < 1.0
    # Still hanging: the answer at once, and no second check piled on top.
    started = time.monotonic()
    assert roots.offline(s, fresh=True) == {"nas"}
    assert time.monotonic() - started < 0.1
    release.set()
    for _ in range(100):                             # the late answer lands
        if roots.offline(s) == set():
            break
        time.sleep(0.02)
    assert roots.offline(s) == set()


def test_an_old_answer_is_handed_out_while_the_next_check_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(roots, "CHECK_EVERY", 0.0)
    s = config(nas=tmp_path)
    assert roots.offline(s) == set()
    release = threading.Event()
    monkeypatch.setattr(roots, "listable", lambda path: release.wait(10) and False)
    started = time.monotonic()
    assert roots.offline(s) == set()                 # the old answer, no waiting
    assert time.monotonic() - started < 0.1
    release.set()
    for _ in range(100):
        if roots.offline(s) == {"nas"}:
            break
        time.sleep(0.02)
    assert roots.offline(s) == {"nas"}


def test_reachable_is_no_clause_while_everything_is_there(tmp_path):
    assert roots.reachable(config(icloud=tmp_path)) == []
    clauses = roots.reachable(config(icloud=tmp_path, nas=tmp_path / "away"))
    assert len(clauses) == 1
    sql = str(clauses[0].compile(compile_kwargs={"literal_binds": True}))
    assert "NOT IN ('nas')" in sql
