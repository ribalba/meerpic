"""Jobs from the agent's side: claiming, reporting, orphans, reindex."""

from __future__ import annotations

import pytest
from agent_helpers import add_photo, needs_db, use_db
from sqlalchemy import select

from agent import jobs
from core.models import Job, Photo, PhotoEmbedding

pytestmark = needs_db


@pytest.fixture
def db():
    yield from use_db()


def job(db, kind="scan", **kw):
    row = Job(kind=kind, **kw)
    db.add(row)
    db.commit()
    return row.id


def fresh(db, job_id):
    db.expire_all()
    return db.get(Job, job_id)


def test_claim_takes_the_oldest_queued_job_of_the_asked_kinds(db):
    a = job(db, "sync")
    b = job(db, "scan")
    c = job(db, "scan")
    got = jobs.claim(["scan"])
    assert got.id == b and got.status == "running" and got.started_at is not None
    assert jobs.claim(["scan"]).id == c
    assert jobs.claim(["scan"]) is None
    assert fresh(db, a).status == "queued"
    assert jobs.claim([]) is None


def test_a_job_cancelled_while_queued_is_closed_not_run(db):
    a = job(db, "sync", cancel_requested=True)
    got = jobs.claim(["sync"])
    assert got.id == a and got.status == "cancelled"
    assert fresh(db, a).finished_at is not None


def test_progress_log_and_finish(db):
    a = job(db, "sync", status="running")
    jobs.update_job(a, progress={"bytes": 5}, message="working")
    jobs.append_log(a, ["one", "two\n"])
    jobs.append_log(a, [])
    jobs.finish(a, "done", message="2 new photos")
    row = fresh(db, a)
    assert row.progress == {"bytes": 5}
    assert row.log == "one\ntwo\n"
    assert (row.status, row.message) == ("done", "2 new photos")
    assert row.finished_at is not None and row.heartbeat_at is not None


def test_log_is_capped_at_a_line_boundary(db):
    a = job(db, "sync", status="running")
    jobs.append_log(a, ["an early line"])
    jobs.append_log(a, [f"copied IMG_{i:05d}.HEIC" for i in range(3000)])
    row = fresh(db, a)
    assert len(row.log) <= jobs.LOG_CAP
    assert row.log.startswith("copied IMG_")
    assert row.log.endswith("copied IMG_02999.HEIC\n")


def test_tail():
    assert jobs.tail("abc", 10) == "abc"
    assert jobs.tail("line one\nline two\n", 12) == "line two\n"
    assert jobs.tail("x" * 50, 10) == "x" * 10


def test_cancel_requested(db):
    a = job(db, "sync", status="running")
    assert not jobs.cancel_requested(a)
    fresh(db, a).cancel_requested = True
    db.commit()
    assert jobs.cancel_requested(a)


def test_orphans_from_a_previous_agent_are_failed(db):
    a = job(db, "sync", status="running")
    b = job(db, "scan", status="queued")
    assert jobs.fail_orphans() == 1
    row = fresh(db, a)
    assert (row.status, row.error) == ("failed", "agent restarted")
    assert fresh(db, b).status == "queued"


def test_active_and_latest(db):
    assert jobs.active("sync") is None and jobs.latest("sync") is None
    a = job(db, "sync", status="done")
    b = job(db, "sync", status="queued")
    assert jobs.active("sync").id == b
    assert jobs.latest("sync").id == b
    assert jobs.create("sync", {"reason": "x"}, running=True).status == "running"
    assert a


def _indexed(db, name, sig="s1", **kw):
    return add_photo(db, name, sig=sig, meta_sig=sig, thumb_sig=sig, preview_sig=sig, **kw)


def test_reindex_one_stage(db):
    a = _indexed(db, "a.jpg", error_stage="thumbs", error="x", fail_count=3)
    b = _indexed(db, "b.mov", error_stage="previews", error="y", fail_count=3)
    msg = jobs.reindex("thumbs")
    assert "thumbs: 2" in msg
    db.expire_all()
    a, b = db.get(Photo, a.id), db.get(Photo, b.id)
    assert a.thumb_sig == "" and a.meta_sig == "s1"
    assert (a.error_stage, a.fail_count) == ("", 0)
    assert (b.error_stage, b.fail_count) == ("previews", 3)     # not this stage's


def test_reindex_embeddings_and_all(db):
    from core import embed

    a = _indexed(db, "a.jpg", error_stage="meta", fail_count=1)
    db.add(PhotoEmbedding(photo_id=a.id, model="fake", sig="s1", embedding=[0.0] * embed.dim()))
    db.commit()
    assert "1 embedding(s) dropped" in jobs.reindex("embeddings")
    assert db.scalar(select(PhotoEmbedding.photo_id)) is None
    jobs.reindex("all")
    db.expire_all()
    a = db.get(Photo, a.id)
    assert (a.meta_sig, a.thumb_sig, a.preview_sig, a.fail_count) == ("", "", "", 0)


def test_reindex_story_and_nsfw(db):
    a = _indexed(db, "a.mov", story_sig="s1", story_frames=10, nsfw_sig="s1", nsfw=0.5)
    assert "story: 1" in jobs.reindex("story")
    assert "nsfw: 1" in jobs.reindex("nsfw")
    db.expire_all()
    a = db.get(Photo, a.id)
    assert (a.story_sig, a.nsfw_sig, a.thumb_sig) == ("", "", "s1")
    assert a.nsfw == 0.5                    # the old score stands until the new one
    jobs.reindex("all")
    db.expire_all()
    assert db.get(Photo, a.id).meta_sig == ""


def test_running_elsewhere(db):
    from datetime import timedelta

    from core.timeutil import utcnow

    assert not jobs.running_elsewhere(["sync"])
    a = job(db, "sync", status="running", heartbeat_at=utcnow())
    assert jobs.running_elsewhere(["sync", "albums"])
    assert not jobs.running_elsewhere(["sync"], exclude=[a])
    assert not jobs.running_elsewhere(["delete"])
    fresh(db, a).heartbeat_at = utcnow() - timedelta(minutes=10)     # a dead terminal
    db.commit()
    assert not jobs.running_elsewhere(["sync"])


def test_reindex_unknown_stage():
    with pytest.raises(ValueError, match="unknown stage"):
        jobs.reindex("everything")
