"""Live Photos: a still and its video, paired by ContentIdentifier."""

from __future__ import annotations

import pytest
from agent_helpers import add_photo, needs_db, use_db

from agent import live
from core.models import Photo

pytestmark = needs_db


@pytest.fixture
def db():
    yield from use_db()


def get(db, row):
    db.expire_all()
    return db.get(Photo, row.id)


def test_one_still_and_its_video_pair_up(db):
    still = add_photo(db, "IMG_1.HEIC", content_id="A")
    motion = add_photo(db, "IMG_1.MOV", content_id="A")
    other = add_photo(db, "IMG_2.HEIC", content_id="B")
    assert live.relink(db, {"A", "B"}) == 2
    db.commit()
    assert get(db, still).live_video_id == motion.id
    assert get(db, motion).is_companion is True
    assert get(db, other).live_video_id is None
    # Idempotent: nothing to change the second time.
    assert live.relink(db, {"A", "B"}) == 0


def test_every_still_of_the_group_gets_the_motion(db):
    # An edit and its original share the identifier: both are the moment
    # the video belongs to (the original is superseded, not a second photo).
    a = add_photo(db, "IMG_1.HEIC", content_id="A")
    b = add_photo(db, "IMG_1-edited.heic", content_id="A")
    motion = add_photo(db, "IMG_1.MOV", content_id="A")
    assert live.relink(db, {"A"}) == 3
    db.commit()
    assert get(db, a).live_video_id == motion.id and get(db, b).live_video_id == motion.id
    assert get(db, motion).is_companion is True
    assert live.relink(db, {"A"}) == 0


def test_an_originals_video_is_the_motion_before_an_edits(db):
    still = add_photo(db, "IMG_1.HEIC", content_id="A")
    edited = add_photo(db, "IMG_1-edited.mov", content_id="A")
    motion = add_photo(db, "IMG_1.MOV", content_id="A")
    live.relink(db, {"A"})
    db.commit()
    assert get(db, still).live_video_id == motion.id
    assert get(db, motion).is_companion is True
    assert get(db, edited).is_companion is False


def test_the_group_unpairs_when_its_last_still_goes(db):
    a = add_photo(db, "IMG_1.HEIC", content_id="A")
    b = add_photo(db, "IMG_1-edited.heic", content_id="A")
    motion = add_photo(db, "IMG_1.MOV", content_id="A")
    live.relink(db, {"A"})
    db.commit()
    db.delete(get(db, b))
    db.commit()
    live.relink(db, {"A"})
    db.commit()
    assert get(db, motion).is_companion is True            # one still is enough
    db.delete(get(db, a))
    db.commit()
    live.relink(db, {"A"})
    db.commit()
    assert get(db, motion).is_companion is False


def test_the_first_of_two_videos_is_the_motion(db):
    still = add_photo(db, "IMG_1.HEIC", content_id="A")
    first = add_photo(db, "IMG_1.MOV", content_id="A")
    second = add_photo(db, "IMG_1 (1).MOV", content_id="A")
    live.relink(db, {"A"})
    db.commit()
    assert get(db, still).live_video_id == first.id
    assert get(db, first).is_companion is True
    assert get(db, second).is_companion is False


def test_a_video_whose_still_is_gone_is_visible_again(db):
    still = add_photo(db, "IMG_1.HEIC", content_id="A")
    motion = add_photo(db, "IMG_1.MOV", content_id="A")
    live.relink(db, {"A"})
    db.commit()
    db.delete(get(db, still))
    db.commit()
    live.relink(db, {"A"})
    db.commit()
    assert get(db, motion).is_companion is False


def test_a_changed_identifier_unpairs_both_sides(db):
    still = add_photo(db, "IMG_1.HEIC", content_id="A")
    motion = add_photo(db, "IMG_1.MOV", content_id="A")
    live.relink(db, {"A"})
    db.commit()
    s = get(db, still)
    s.content_id = "B"
    db.commit()
    live.relink(db, {"A", "B"}, [still.id])
    db.commit()
    assert get(db, still).live_video_id is None
    assert get(db, motion).is_companion is False


def test_a_lost_identifier_unlinks(db):
    still = add_photo(db, "IMG_1.HEIC", content_id="A")
    motion = add_photo(db, "IMG_1.MOV", content_id="A")
    live.relink(db, {"A"})
    db.commit()
    m = get(db, motion)
    m.content_id = ""
    db.commit()
    live.relink(db, {"A"}, [motion.id])
    db.commit()
    assert get(db, motion).is_companion is False
    assert get(db, still).live_video_id is None


def test_nothing_to_do():
    assert live.relink(None, set(), []) == 0
