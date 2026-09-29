"""The delete plan: what the confirmation shows is what the agent runs.

Delete is local (rclone cannot delete in iCloud Photos): every file moves to
the trash, and the ones from iCloud are named for later syncs to leave out."""

import os
from datetime import date

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("MEERPIC_TEST_DB"), reason="MEERPIC_TEST_DB is not set")


@pytest.fixture
def db():
    from sqlalchemy import text

    from core.database import SessionLocal, engine, init_db

    init_db()
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE photos RESTART IDENTITY CASCADE"))
    session = SessionLocal()
    yield session
    session.close()


def _add(db, name, kind="photo", root="icloud", **kw):
    from core.media import make_sig
    from core.models import Photo

    rel = kw.pop("rel_path", name)
    p = Photo(root=root, rel_path=rel, name=name, ext=name.rsplit(".", 1)[-1].lower(), kind=kind,
              mime="x", size=1, mtime_ns=1, sig=make_sig(root, rel, 1, 1), **kw)
    db.add(p)
    db.flush()
    return p


def test_a_live_photo_with_an_edit_goes_as_one_group(db):
    from core.config import get_settings
    from core.deletion import Plan, plan

    video = _add(db, "IMG_1.MOV", kind="video", is_companion=True)
    edit = _add(db, "IMG_1-edited.heic", live_video_id=video.id)
    original = _add(db, "IMG_1.HEIC", live_video_id=video.id, superseded_by=edit.id)
    other = _add(db, "IMG_2.JPG")
    db.commit()

    settings = get_settings()
    p = plan(db, [edit.id, other.id, 999], settings, today=date(2026, 9, 29))
    assert p.missing == [999]
    assert [g.photo_id for g in p.groups] == [edit.id, other.id]
    assert sorted(p.photo_ids) == sorted([edit.id, original.id, video.id, other.id])
    assert sorted(p.excluded) == sorted(["IMG_1-edited.heic", "IMG_1.HEIC", "IMG_1.MOV", "IMG_2.JPG"])
    assert len(p.moves) == 4
    assert all("/.meerpic-trash/2026-09-29/" in m for m in p.moves)
    assert "commands" not in p.to_json()
    # Round trip through the job's params, as the agent reads it.
    again = Plan.from_json(p.to_json())
    assert again.moves == p.moves and again.excluded == p.excluded and again.photo_ids == p.photo_ids


def test_a_plan_from_when_delete_was_in_icloud_is_refused(db):
    """A job queued by the old version holds rclone commands and was agreed to
    as a delete in iCloud; it is not quietly run as something else."""
    from core.deletion import Plan

    old = {"groups": [{"photo_id": 1, "steps": [{
        "photo_id": 1, "name": "a.jpg", "local": "/x/a.jpg", "trash": "/x/.meerpic-trash/d/a.jpg",
        "remote": "iclouddrive:PrimarySync/All Photos/a.jpg",
        "argv": ["rclone", "deletefile", "iclouddrive:PrimarySync/All Photos/a.jpg"],
        "command": "rclone deletefile 'iclouddrive:PrimarySync/All Photos/a.jpg'",
    }]}], "commands": ["..."], "moves": ["..."], "missing": []}
    with pytest.raises(TypeError):
        Plan.from_json(old)


def test_choosing_the_original_takes_its_edit_and_nothing_twice(db):
    from core.config import get_settings
    from core.deletion import plan

    edit = _add(db, "IMG_3-edited.jpg")
    original = _add(db, "IMG_3.JPG", superseded_by=edit.id)
    db.commit()
    p = plan(db, [original.id, edit.id], get_settings())
    assert len(p.groups) == 1 and sorted(p.photo_ids) == sorted([original.id, edit.id])


def test_files_outside_icloud_are_only_moved(db):
    from core.config import get_settings
    from core.deletion import plan

    sub = _add(db, "a.jpg", rel_path="imports/a.jpg")
    db.commit()
    p = plan(db, [sub.id], get_settings())
    assert p.excluded == [] and len(p.moves) == 1


def test_with_sync_off_nothing_is_excluded(db, monkeypatch):
    from core.config import get_settings
    from core.deletion import plan

    row = _add(db, "a.jpg")
    db.commit()
    monkeypatch.setattr(get_settings(), "sync_enabled", False)
    p = plan(db, [row.id], get_settings())
    assert p.excluded == [] and len(p.moves) == 1


def test_already_trashed_is_missing(db):
    from core.config import get_settings
    from core.deletion import plan
    from core.timeutil import utcnow

    p1 = _add(db, "x.jpg", trashed_at=utcnow())
    db.commit()
    assert plan(db, [p1.id], get_settings()).missing == [p1.id]
