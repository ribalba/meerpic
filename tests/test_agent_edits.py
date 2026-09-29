"""Edited photos: which original an ``-edited`` file replaces, kept right as
files come and go."""

from __future__ import annotations

# Naive datetimes on purpose: meerpic stores naive UTC and naive wall clocks.
# ruff: noqa: DTZ001
from datetime import datetime

import pytest
from agent_helpers import (
    add_photo,
    jpeg,
    needs_db,
    needs_exiftool,
    set_mtime,
    use_db,
    use_library,
)
from sqlalchemy import select

from agent import edits, scan
from agent.pipeline import Pipeline
from core.config import get_settings
from core.models import Photo

T = datetime(2024, 11, 10, 11, 2, 33)


# --- names ---------------------------------------------------------------------------


@pytest.mark.parametrize("name, stem", [
    ("IMG_1925-edited.heic", "IMG_1925"),
    ("IMG_1925-EDITED.JPG", "IMG_1925"),
    ("IMG_4201-edited_0B3A8D78-DF04-427F-9B16-D03A5CA2C2DC.heic", "IMG_4201"),
    ("08bb1632-7a3b-4805-a253-25d49ef5b053-edited.jpg", "08bb1632-7a3b-4805-a253-25d49ef5b053"),
    ("IMG_1925.HEIC", None),
    ("my-edited-photo.jpg", None),
    ("-edited.jpg", None),
])
def test_edit_stem(name, stem):
    assert edits.edit_stem(name) == stem


def test_candidate_stems_and_matches():
    assert edits.candidate_stems("IMG_0010_AQgX／l.HEIC") == {"img_0010_aqgx／l", "img_0010", "img"}
    assert edits.candidate_stems("photo.jpg") == {"photo"}
    assert edits.matches("IMG_0010", "IMG_0010.HEIC")
    assert edits.matches("IMG_0010", "img_0010_AQgX／l+x.heic")
    assert not edits.matches("IMG_0010", "IMG_00101.HEIC")
    assert not edits.matches("IMG_0010", "IMG_0010-edited.heic")


def _row(id, content_id="", taken_at=None):
    return edits._Row(id=id, root="icloud", rel_path=f"{id}.jpg", name=f"{id}.jpg", kind="photo",
                      content_id=content_id, taken_at=taken_at, superseded_by=None)


def test_pick_prefers_the_identifier_then_the_nearest_time():
    e = _row(1, "CID", T)
    near, same_cid = _row(2, "OTHER", T), _row(3, "CID", datetime(2020, 1, 1))
    assert edits.pick([e], [near, same_cid]) == {3: 1}
    assert edits.pick([_row(1, "", T)], [near, same_cid]) == {2: 1}
    # Two edits, two originals: each takes its own, neither takes both.
    e2 = _row(4, "", datetime(2023, 2, 19, 7, 49))
    old = _row(5, "", datetime(2023, 2, 19, 7, 49))
    assert edits.pick([_row(1, "", T), e2], [near, old]) == {2: 1, 5: 4}
    # No time to go by: still the only candidate.
    assert edits.pick([_row(1)], [_row(2)]) == {2: 1}
    assert edits.pick([], [near]) == {}


# --- the table -----------------------------------------------------------------------


@pytest.fixture
def db():
    yield from use_db()


def get(db, row):
    db.expire_all()
    return db.get(Photo, row.id)


@needs_db
def test_an_edit_supersedes_its_original(db):
    original = add_photo(db, "IMG_1.HEIC", taken_at=T)
    edit = add_photo(db, "IMG_1-edited.heic", taken_at=T)
    other = add_photo(db, "IMG_2.HEIC", taken_at=T)
    elsewhere = add_photo(db, "IMG_1.JPG", rel_path="imports/IMG_1.JPG", taken_at=T)
    assert edits.relink(db, [edit]) == 1
    db.commit()
    assert get(db, original).superseded_by == edit.id
    assert get(db, other).superseded_by is None
    assert get(db, elsewhere).superseded_by is None          # another folder
    assert get(db, edit).superseded_by is None
    # Idempotent, and the same from the original's side.
    assert edits.relink(db, [edit, original, other]) == 0


@needs_db
def test_suffixed_originals_go_by_identifier_then_time(db):
    # Two assets named IMG_0010 in "All Photos": both carry an iCloud suffix.
    a = add_photo(db, "IMG_0010_ATYP.HEIC", taken_at=datetime(2021, 11, 18, 9, 44), content_id="X")
    b = add_photo(db, "IMG_0010_AQgX.HEIC", taken_at=T, content_id="Y")
    edit = add_photo(db, "IMG_0010-edited.heic", taken_at=T, content_id="Z")
    edits.relink(db, [edit])
    db.commit()
    assert get(db, b).superseded_by == edit.id and get(db, a).superseded_by is None
    # The edit's identifier matches the other one after all: that wins.
    e = get(db, edit)
    e.content_id = "X"
    db.commit()
    edits.relink(db, [e])
    db.commit()
    assert get(db, a).superseded_by == edit.id and get(db, b).superseded_by is None


@needs_db
def test_a_deleted_edit_frees_its_original_for_another(db):
    original = add_photo(db, "IMG_4201_A.HEIC", taken_at=T)
    first = add_photo(db, "IMG_4201-edited_1.heic", taken_at=T)
    second = add_photo(db, "IMG_4201-edited_2.heic", taken_at=datetime(2025, 1, 1))
    edits.relink(db, [first, second])
    db.commit()
    assert get(db, original).superseded_by == first.id
    db.delete(get(db, first))
    db.commit()
    assert get(db, original).superseded_by is None             # the foreign key
    edits.relink(db, [original])
    db.commit()
    assert get(db, original).superseded_by == second.id


@needs_db
def test_videos_and_edits_are_never_originals(db):
    video = add_photo(db, "IMG_7.MOV", taken_at=T)
    older_edit = add_photo(db, "IMG_7-edited.jpg", taken_at=T)
    video_edit = add_photo(db, "IMG_7-edited.mov", taken_at=T)
    edits.relink(db, [older_edit, video_edit, video])
    db.commit()
    assert get(db, video).superseded_by is None
    assert get(db, older_edit).superseded_by is None
    assert get(db, video_edit).superseded_by is None


@needs_db
def test_an_original_claimed_by_another_group_is_left_to_it(db):
    original = add_photo(db, "IMG_0010.HEIC", taken_at=T)
    specific = add_photo(db, "IMG_0010-edited.heic", taken_at=datetime(2000, 1, 1))
    broad = add_photo(db, "IMG-edited.heic", taken_at=T)
    edits.relink(db, [specific])
    db.commit()
    assert get(db, original).superseded_by == specific.id
    edits.relink(db, [broad, original])
    db.commit()
    assert get(db, original).superseded_by == specific.id


# --- through the scan and the meta stage ------------------------------------------------


@pytest.fixture
def lib(tmp_path, monkeypatch):
    yield from use_library(tmp_path, monkeypatch)


@needs_db
@needs_exiftool
def test_meta_links_an_edit_and_the_scan_unlinks_it(db, lib):
    jpeg(lib / "IMG_5.JPG", date="2024:07:01 12:00:00", color=(0, 0, 200))
    jpeg(lib / "IMG_5-edited.jpg", date="2024:07:01 12:00:00", color=(0, 0, 250))
    jpeg(lib / "IMG_6.JPG", date="2023:01:01 12:00:00", color=(0, 200, 0))
    for path in lib.iterdir():
        set_mtime(path, datetime(2025, 1, 1))
    scan.scan(db, get_settings())
    pipe = Pipeline(get_settings(), once=True)
    try:
        pipe.meta_batch(db)
    finally:
        pipe.close()
    db.expire_all()
    rows = {p.name: p for p in db.scalars(select(Photo))}
    assert rows["IMG_5.JPG"].superseded_by == rows["IMG_5-edited.jpg"].id
    assert rows["IMG_6.JPG"].superseded_by is None
    (lib / "IMG_5-edited.jpg").unlink()
    scan.scan(db, get_settings())
    db.expire_all()
    assert db.scalar(select(Photo.superseded_by).where(Photo.name == "IMG_5.JPG")) is None
