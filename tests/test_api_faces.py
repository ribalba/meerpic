"""Faces: the info panel's list, ``face:`` as a ranking, and the crops.

The agent stores each face's box, crop and vector (agent/faces.py); here the
rows are written straight, with vectors made by hand so the scores are
known: one-hot for a "person", and a unit vector part way to another axis
for a less certain match of them. The match floor is 0.35 unless a test
changes it.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from PIL import Image
from server_helpers import make_photo, needs_db, running_app, wall

from app.query import parse_query

IMMUTABLE = "private, max-age=31536000, immutable"


def person(axis: int, lean: int | None = None, cos: float = 1.0) -> list[float]:
    """A unit vector on ``axis``, or leaning towards ``lean`` so that its
    similarity to the pure ``axis`` one is ``cos``."""
    v = np.zeros(512, dtype=np.float32)
    v[axis] = cos
    if lean is not None:
        v[lean] = math.sqrt(1 - cos * cos)
    return v.tolist()


# --- the parser (no database) ------------------------------------------------------


def test_face_takes_one_face_id():
    spec = parse_query("face:12 year:2024")
    assert spec.face == 12 and spec.scored and spec.mode == "score"
    assert spec.describe()["face"] == 12
    assert parse_query("face:12 face:12").face == 12
    assert parse_query("face:12 face:13").errors == ['"face:13": only one face: at a time']
    assert parse_query("face:abc").errors == ['"face:abc": face: takes a face id']
    assert parse_query("face:0").face is None


def test_face_ignores_words_and_gives_way_to_similar():
    spec = parse_query("face:12 cows")
    assert spec.words == [] and spec.errors == ["ignored with face: cows"]
    spec = parse_query("similar:3 face:12")
    assert (spec.similar, spec.face) == (3, None)
    assert spec.errors == ["face:12 ignored with similar:"]


# --- with a database ---------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    with running_app() as c:
        yield c


def add_face(db, photo, pos, vector, *, model="fake", sig=None, box=(0.1, 0.2, 0.3, 0.4), crop=True):
    from core.media import face_path
    from core.models import Face

    sig = sig or photo.sig
    face = Face(photo_id=photo.id, sig=sig, pos=pos, model=model, x=box[0], y=box[1], w=box[2], h=box[3],
                score=0.9, embedding=vector)
    db.add(face)
    db.flush()
    if crop:
        path = face_path(sig, pos)
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 16), (200, 150, 120)).save(path, "WEBP")
    return face


@pytest.fixture(scope="module")
def lib(client):
    """Anna is axis 0, Ben axis 1, a stranger axis 2, a loner axis 3."""
    from core.database import SessionLocal

    with SessionLocal() as s:
        p, f = {}, {}
        done = {}

        def photo(key, name, **cols):
            row = make_photo(s, name, **cols)
            row.faces_sig = cols.get("faces_sig", row.sig)
            p[key] = row
            return row

        photo("group", "group.jpg", taken=wall(2024, 5, 1))
        f["anna"] = add_face(s, p["group"], 0, person(0), box=(0.5, 0.25, 0.2, 0.3))
        f["ben"] = add_face(s, p["group"], 1, person(1))
        photo("anna_sure", "a1.jpg", taken=wall(2023, 1, 1))
        add_face(s, p["anna_sure"], 0, person(0))
        photo("anna_likely", "a2.jpg", taken=wall(2024, 8, 1))
        add_face(s, p["anna_likely"], 0, person(0, 2, cos=0.6))
        # Anna and Ben both: scored by the closer one.
        photo("both", "a3.jpg", taken=wall(2022, 1, 1))
        add_face(s, p["both"], 0, person(1))
        add_face(s, p["both"], 1, person(0, 2, cos=0.8))
        photo("below", "a4.jpg", taken=wall(2024, 2, 1))
        add_face(s, p["below"], 0, person(0, 2, cos=0.3))
        photo("hidden", "a5.jpg", taken=wall(2024, 3, 1), hidden=True)
        add_face(s, p["hidden"], 0, person(0))
        photo("other_model", "a6.jpg", taken=wall(2024, 4, 1))
        add_face(s, p["other_model"], 0, person(0), model="another")
        # Found in a version of the file that has since changed.
        photo("stale", "a7.jpg", taken=wall(2024, 6, 1))
        add_face(s, p["stale"], 0, person(0), sig="ffffffffffffffff", crop=False)
        photo("stranger", "s.jpg", taken=wall(2024, 7, 1))
        add_face(s, p["stranger"], 0, person(2))
        # In this one picture and no other.
        photo("loner", "l.jpg", taken=wall(2021, 7, 1))
        f["loner"] = add_face(s, p["loner"], 0, person(3))
        photo("pending", "p.jpg", taken=wall(2024, 9, 1), faces_sig="")
        photo("clip", "c.MOV", taken=wall(2024, 10, 1), duration=3.0, faces_sig="")
        photo("original", "o.jpg", taken=wall(2024, 11, 1), superseded_by=p["group"].id, faces_sig="")
        s.commit()
        done["p"] = {k: v.id for k, v in p.items()}
        done["f"] = {k: v.id for k, v in f.items()}
        done["sig"] = p["group"].sig
        return done


def names(page, lib):
    by_id = {v: k for k, v in lib["p"].items()}
    return [by_id[i["id"]] for i in page["items"]]


@needs_db
def test_the_detail_lists_the_faces_largest_first(client, lib):
    d = client.get(f"/api/photos/{lib['p']['group']}").json()
    assert d["faces_state"] == "done"
    assert [f["id"] for f in d["faces"]] == [lib["f"]["anna"], lib["f"]["ben"]]
    anna = d["faces"][0]
    assert anna["url"] == f"/media/face/{lib['f']['anna']}?v={lib['sig']}"
    assert anna["box"] == {"x": 0.5, "y": 0.25, "w": 0.2, "h": 0.3}
    assert anna["score"] == pytest.approx(0.9)


@needs_db
def test_the_detail_says_whether_faces_were_looked_for(client, lib):
    state = {k: client.get(f"/api/photos/{lib['p'][k]}").json() for k in ("pending", "clip", "original", "stale")}
    assert {k: v["faces_state"] for k, v in state.items()} == {
        "pending": "pending", "clip": "off", "original": "off", "stale": "done",
    }
    # Faces from an older version of the file are not this photo's.
    assert state["stale"]["faces"] == [] and state["pending"]["faces"] == []


@needs_db
def test_the_detail_says_off_when_faces_are_disabled(client, lib, monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "faces_enabled", False)
    assert client.get(f"/api/photos/{lib['p']['pending']}").json()["faces_state"] == "off"


@needs_db
def test_face_ranks_the_photos_with_that_person_best_first(client, lib):
    page = client.get("/api/photos", params={"q": f"face:{lib['f']['anna']}"}).json()
    assert page["mode"] == "score" and page["query"]["face"] == lib["f"]["anna"]
    # The photo it came from first (a tie with anna_sure, broken by id), then
    # the rest; not below the floor, not hidden, not from another model, not
    # from a changed file.
    assert names(page, lib) == ["group", "anna_sure", "both", "anna_likely"]
    assert [i["score"] for i in page["items"]] == pytest.approx([1.0, 1.0, 0.8, 0.6])
    assert page["total"] == 4
    assert page["similar_to"]["id"] == lib["p"]["group"]
    assert page["face"] == {
        "id": lib["f"]["anna"], "url": f"/media/face/{lib['f']['anna']}?v={lib['sig']}",
        "photo_id": lib["p"]["group"],
    }


@needs_db
def test_face_takes_filters_and_date_order(client, lib):
    anna = lib["f"]["anna"]
    by_date = client.get("/api/photos", params={"q": f"face:{anna} sort:date"}).json()
    assert names(by_date, lib) == ["anna_likely", "group", "anna_sure", "both"]
    in_2024 = client.get("/api/photos", params={"q": f"face:{anna} year:2024"}).json()
    assert names(in_2024, lib) == ["group", "anna_likely"]
    hidden = client.get("/api/photos", params={"q": f"face:{anna} is:hidden"}).json()
    assert names(hidden, lib) == ["hidden"]


@needs_db
def test_the_floor_is_the_setting(client, lib, monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "faces_min_score", 0.7)
    page = client.get("/api/photos", params={"q": f"face:{lib['f']['anna']}"}).json()
    assert names(page, lib) == ["group", "anna_sure", "both"]


@needs_db
def test_another_person_finds_theirs(client, lib):
    page = client.get("/api/photos", params={"q": f"face:{lib['f']['ben']}"}).json()
    assert names(page, lib) == ["group", "both"]


@needs_db
def test_somebody_in_one_picture_only_finds_that_picture(client, lib):
    page = client.get("/api/photos", params={"q": f"face:{lib['f']['loner']}"}).json()
    assert names(page, lib) == ["loner"] and page["total"] == 1


@needs_db
def test_a_face_that_is_gone_says_so(client, lib):
    page = client.get("/api/photos", params={"q": "face:999999"}).json()
    assert page["items"] == [] and page["face"] is None and page["similar_to"] is None
    assert page["query"]["errors"] == ["face:999999: there is no such face (any more)"]


@needs_db
def test_a_face_from_a_changed_file_says_so(client, lib):
    from core.database import SessionLocal
    from core.models import Face

    with SessionLocal() as s:
        stale = s.query(Face).filter(Face.photo_id == lib["p"]["stale"]).one()
    page = client.get("/api/photos", params={"q": f"face:{stale.id}"}).json()
    assert page["items"] == [] and page["face"] is None
    assert page["similar_to"]["id"] == lib["p"]["stale"]
    assert "no such face" in page["query"]["errors"][0]


@needs_db
def test_the_crop_is_served_and_kept_under_its_sig(client, lib):
    anna = lib["f"]["anna"]
    r = client.get(f"/media/face/{anna}?v={lib['sig']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert r.headers["cache-control"] == IMMUTABLE
    assert client.get(f"/media/face/{anna}").headers["cache-control"] == "private, no-cache"
    assert client.get("/media/face/999999").status_code == 404


@needs_db
def test_status_counts_photos_still_to_search_and_state_says_it_is_on(client, lib):
    # pending.jpg only: the video and the original behind an edit never are.
    assert client.get("/api/status").json()["index"]["faces"] == 1
    assert client.get("/api/state").json()["faces"] == {"enabled": True}


@needs_db
def test_date_pages_carry_no_face(client, lib):
    assert client.get("/api/photos").json()["face"] is None
