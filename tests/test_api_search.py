"""Score mode: search by words, "similar to this one", and what bounds both.

The fake model (``MEERPIC_EMBED_FAKE=1``, see core/embed.py) maps an image to
its colour layout and a colour word to the vector of a solid image of that
colour, so "red" finds red photos, and a photo that is half red ranks below a
solid red one and above one with no red in it. Measured with it:

    red 1.000   red/yellow 0.753   yellow 0.575   red,red/blue 0.548
    red/blue 0.250   blue -0.500   green -0.500

against a floor of 0.2 for words and 0.5 for similar.
"""

from __future__ import annotations

import pytest
from server_helpers import (
    BLUE,
    GREEN,
    RED,
    YELLOW,
    add_embedding,
    color_vector,
    make_photo,
    needs_db,
    running_app,
    session,
    wall,
)

pytestmark = needs_db


@pytest.fixture(scope="module")
def client():
    with running_app() as c:
        yield c


@pytest.fixture
def db():
    with session() as s:
        yield s

POTSDAM = {"place": "Potsdam, Brandenburg, Germany", "city": "Potsdam", "lat": 52.39, "lon": 13.06}


@pytest.fixture(scope="module")
def lib(client):
    from core.database import SessionLocal
    from core.models import PhotoEmbedding

    with SessionLocal() as s:
        p = {}

        def add(key, colors, name=None, **kw):
            p[key] = make_photo(s, name, **kw)
            if colors:
                add_embedding(s, p[key], color_vector(*colors))

        add("r1", [RED], taken=wall(2024, 3, 1))
        add("r2", [RED], taken=wall(2024, 1, 1))
        add("r3", [RED], "IMG_0003.MOV", taken=wall(2023, 6, 1), **POTSDAM)
        add("ry", [RED, YELLOW], taken=wall(2024, 5, 1))
        add("y", [YELLOW], taken=wall(2022, 1, 1))
        add("rrb", [RED, RED, BLUE], taken=wall(2024, 6, 1))
        add("rb", [RED, BLUE], taken=wall(2021, 1, 1))
        add("b", [BLUE], taken=wall(2024, 7, 1))
        add("g", [GREEN], taken=wall(2024, 8, 1))
        add("companion", [RED], "IMG_0009.MOV", taken=wall(2024, 2, 1), is_companion=True)
        add("unindexed", None, taken=wall(2024, 2, 2))
        # A vector from another model is not comparable, whatever it says.
        add("other_model", None, taken=wall(2024, 2, 3))
        s.add(PhotoEmbedding(photo_id=p["other_model"].id, model="some/other-model",
                             sig=p["other_model"].sig, embedding=color_vector(RED)))
        s.commit()
        return {k: v.id for k, v in p.items()}


def search(client, q, **params) -> dict:
    r = client.get("/api/photos", params={"q": q, **params})
    assert r.status_code == 200, r.text
    return r.json()


def names(lib, page) -> list[str]:
    by_id = {v: k for k, v in lib.items()}
    return [by_id.get(c["id"], c["id"]) for c in page["items"]]


RED_RANKING = ["r1", "r2", "r3", "ry", "y", "rrb", "rb"]


def test_words_rank_by_score_above_the_floor(client, lib):
    page = search(client, "red")
    assert page["mode"] == "score"
    assert page["query"]["text"] == "red"
    # Three perfect matches in id order, then by falling score; blue and
    # green are below the floor, the companion is never listed, and the
    # photo without a vector (or with another model's) is not ranked at all.
    assert names(lib, page) == RED_RANKING
    assert page["total"] == len(RED_RANKING)
    scores = [c["score"] for c in page["items"]]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == pytest.approx(1.0, abs=1e-3)
    assert scores[-1] == pytest.approx(0.25, abs=1e-3)
    assert page["similar_to"] is None
    assert page["next"] is None


def test_sort_date_shows_the_same_matches_by_date(client, lib):
    page = search(client, "red sort:date")
    assert page["mode"] == "score"
    assert names(lib, page) == ["rrb", "ry", "r1", "r2", "r3", "y", "rb"]
    assert page["total"] == len(RED_RANKING)
    assert all("score" in c for c in page["items"])


def test_filters_narrow_a_search(client, lib):
    assert names(lib, search(client, "red is:video")) == ["r3"]
    assert names(lib, search(client, "red in:potsdam")) == ["r3"]
    assert names(lib, search(client, "red year:2024")) == ["r1", "r2", "ry", "rrb"]
    empty = search(client, "red year:1999")
    assert empty["items"] == [] and empty["total"] == 0


def test_a_search_pages_by_offset(client, lib):
    first = search(client, "red", limit=3)
    assert first["total"] == len(RED_RANKING)
    second = search(client, "red", limit=3, cursor=first["next"])
    third = search(client, "red", limit=3, cursor=second["next"])
    assert names(lib, first) + names(lib, second) + names(lib, third) == RED_RANKING
    assert third["next"] is None
    assert client.get("/api/photos", params={"q": "red", "cursor": "abc"}).status_code == 400


def test_similar_ranks_by_the_photo_and_leaves_it_out(client, lib):
    page = search(client, f"similar:{lib['r1']}")
    assert page["mode"] == "score"
    assert page["query"]["similar"] == lib["r1"]
    assert names(lib, page) == ["r2", "r3", "ry", "y", "rrb"]    # rb (0.25) is not similar
    assert page["total"] == 5
    assert page["similar_to"]["id"] == lib["r1"]
    assert "score" not in page["similar_to"]


def test_similar_wins_over_words(client, lib):
    page = search(client, f"cows similar:{lib['r1']}")
    assert page["query"]["errors"] == ["ignored with similar: cows"]
    assert page["query"]["text"] == ""
    assert names(lib, page) == ["r2", "r3", "ry", "y", "rrb"]


def test_similar_takes_filters(client, lib):
    assert names(lib, search(client, f"similar:{lib['r1']} before:2023")) == ["r3", "y"]


def test_similar_to_a_photo_that_is_not_there(client, lib):
    page = search(client, "similar:999999")
    assert page["items"] == [] and page["total"] == 0
    assert page["similar_to"] is None
    assert "no such photo" in page["query"]["errors"][0]


def test_similar_to_a_photo_not_yet_indexed_says_so(client, lib):
    page = search(client, f"similar:{lib['unindexed']}")
    assert page["items"] == []
    assert page["similar_to"]["id"] == lib["unindexed"]
    assert "not indexed" in page["query"]["errors"][0]


def test_a_model_that_fails_is_an_answer_not_a_500(client, lib, monkeypatch):
    import app.query

    def broken(_text):
        raise RuntimeError("download failed")

    monkeypatch.setattr(app.query.embed, "embed_text", broken)
    page = search(client, "something nobody searched for before")
    assert page["items"] == []
    assert "search is unavailable" in page["query"]["errors"][0]


def test_the_ranking_is_exact_not_the_index_s_first_candidates(client, lib, db):
    """More matches than the HNSW index's candidate list (ef_search = 40).

    An index scan would stop there without a word. The listing orders by the
    score, which the index cannot serve, so even with sequential scans
    discouraged the planner has to look at every vector.
    """
    from sqlalchemy import func, select, text

    from app.query import Scoring, listed, matches, parse_query
    from core import embed
    from core.models import Photo

    extra = []
    for i in range(60):
        extra.append(make_photo(db, taken=wall(2019, 1, 1, 0, i)))
        add_embedding(db, extra[-1], color_vector(RED))
    db.commit()
    try:
        assert search(client, "red", limit=500)["total"] == len(RED_RANKING) + 60

        db.execute(text("SET LOCAL enable_seqscan = off"))
        ranked = matches(Scoring(embed.embed_text("red"), embed.min_score()), listed(parse_query("")))
        stmt = select(func.count()).select_from(ranked)
        plan = "\n".join(r[0] for r in db.execute(text("EXPLAIN " + str(
            stmt.compile(db.get_bind(), compile_kwargs={"literal_binds": True})
        ))))
        assert "ix_emb_hnsw" not in plan
        assert db.scalar(stmt) == len(RED_RANKING) + 60
        db.rollback()
    finally:
        db.rollback()
        for p in extra:
            db.delete(db.get(Photo, p.id))
        db.commit()


def test_a_ranking_is_capped(client, lib, db):
    from sqlalchemy import delete, insert

    from app.query import SCORE_CAP
    from core import embed
    from core.media import make_sig
    from core.models import Photo, PhotoEmbedding

    n = SCORE_CAP + 50
    rows = [
        {"root": "icloud", "rel_path": f"cap/{i}.jpg", "name": f"{i}.jpg", "ext": "jpg",
         "kind": "photo", "mime": "image/jpeg", "size": 1, "mtime_ns": i,
         "sig": make_sig("icloud", f"cap/{i}.jpg", 1, i), "sort_at": wall(2018, 1, 1)}
        for i in range(n)
    ]
    new_ids = db.execute(insert(Photo).returning(Photo.id), rows).scalars().all()
    red = color_vector(RED)
    db.execute(insert(PhotoEmbedding), [
        {"photo_id": pid, "model": embed.model_name(), "sig": "x", "embedding": red} for pid in new_ids
    ])
    db.commit()
    try:
        first = search(client, "red", limit=100)
        assert first["total"] == SCORE_CAP
        last = search(client, "red", limit=100, cursor=str(SCORE_CAP - 30))
        assert len(last["items"]) == 30
        assert last["next"] is None
        assert search(client, "red", limit=100, cursor=str(SCORE_CAP))["items"] == []
    finally:
        db.execute(delete(Photo).where(Photo.id.in_(new_ids)))
        db.commit()
