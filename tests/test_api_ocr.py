"""Text in pictures: ``text:`` as a filter, and the info panel's lines.

The agent stores what each file says (agent/ocr.py); here the rows are
written straight, so the matches are known: every word of a ``text:``, each
anywhere in the text and in any case, umlauts included.
"""

from __future__ import annotations

import pytest
from server_helpers import make_photo, needs_db, running_app, wall

from app.query import parse_query


def test_text_is_a_filter_that_keeps_its_words_together():
    spec = parse_query('text:"Opening Hours" 2024 cows')
    assert spec.words == ["cows"] and spec.errors == []
    assert spec.describe()["filters"] == ['text:"Opening Hours"', "year:2024"]
    assert parse_query("text:rechnung").mode == "date"
    # Half typed: nothing yet, and no complaint.
    assert parse_query("text:").filters == [] and parse_query("text:").errors == []


# --- with a database ---------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    with running_app() as c:
        yield c


def said(db, photo, *lines, sig=None):
    from core.models import PhotoText

    db.add(PhotoText(photo_id=photo.id, sig=sig or photo.sig, model="fake", text="\n".join(lines),
                     lines=[{"t": t, "s": 0.9, "b": [0.1, 0.2 + i / 10, 0.5, 0.05]} for i, t in enumerate(lines)]))


@pytest.fixture(scope="module")
def lib(client):
    from core.database import SessionLocal

    with SessionLocal() as s:
        p = {}

        def photo(key, name, *lines, **cols):
            row = make_photo(s, name, **cols)
            row.ocr_sig = cols.get("ocr_sig", row.sig)
            p[key] = row
            if lines:
                said(s, row, *lines)
            return row

        photo("letter", "letter.jpg", "Der Oberbürgermeister", "Stadt Dessau-Roßlau", "Rechnung Nr. 42",
              taken=wall(2024, 5, 1))
        photo("sign", "sign.jpg", "OPENING", "HOURS", "Mon-Fri 9-17", taken=wall(2023, 7, 1))
        photo("receipt", "receipt.jpg", "Kassenbon", "RECHNUNG", taken=wall(2025, 2, 1))
        photo("clip", "clip.MOV", "Rechnungsprüfung", taken=wall(2024, 9, 1), duration=4.0)
        photo("blank", "blank.jpg", taken=wall(2024, 6, 1))
        photo("hidden", "hidden.jpg", "Rechnung", taken=wall(2024, 3, 1), hidden=True)
        photo("pending", "pending.jpg", taken=wall(2024, 4, 1), ocr_sig="")
        s.commit()
        return {k: v.id for k, v in p.items()}


def names(client, lib, q):
    by_id = {v: k for k, v in lib.items()}
    page = client.get("/api/photos", params={"q": q}).json()
    return [by_id[i["id"]] for i in page["items"]]


@needs_db
def test_text_finds_the_word_in_any_case_newest_first(client, lib):
    assert names(client, lib, "text:rechnung") == ["receipt", "clip", "letter"]
    assert names(client, lib, "text:RECHN") == ["receipt", "clip", "letter"]


@needs_db
def test_umlauts_and_sharp_s_match_as_written(client, lib):
    assert names(client, lib, "text:oberbürgermeister") == ["letter"]
    assert names(client, lib, "text:OBERBÜRGERMEISTER") == ["letter"]
    assert names(client, lib, "text:roßlau") == ["letter"]


@needs_db
def test_every_word_must_be_there_on_any_line(client, lib):
    assert names(client, lib, 'text:"opening hours"') == ["sign"]
    assert names(client, lib, 'text:"hours opening"') == ["sign"]
    assert names(client, lib, 'text:"opening sunday"') == []
    assert names(client, lib, "text:rechnung text:kassenbon") == ["receipt"]


@needs_db
def test_text_combines_with_other_filters_and_keeps_hidden_out(client, lib):
    assert names(client, lib, "text:rechnung is:video") == ["clip"]
    assert names(client, lib, "text:rechnung year:2024") == ["clip", "letter"]
    assert names(client, lib, "text:rechnung is:hidden") == ["hidden"]


@needs_db
def test_like_characters_are_taken_literally(client, lib):
    assert names(client, lib, "text:%") == []
    assert names(client, lib, "text:_") == []
    assert names(client, lib, "text:9-17") == ["sign"]


@needs_db
def test_the_detail_carries_the_lines_and_the_state(client, lib):
    d = client.get(f"/api/photos/{lib['letter']}").json()
    assert d["text_state"] == "done"
    assert [line["t"] for line in d["text_lines"]] == ["Der Oberbürgermeister", "Stadt Dessau-Roßlau",
                                                        "Rechnung Nr. 42"]
    assert d["text_lines"][0]["b"] == [0.1, 0.2, 0.5, 0.05]
    blank = client.get(f"/api/photos/{lib['blank']}").json()
    assert (blank["text_state"], blank["text_lines"]) == ("done", [])
    pending = client.get(f"/api/photos/{lib['pending']}").json()
    assert (pending["text_state"], pending["text_lines"]) == ("pending", [])


@needs_db
def test_text_read_from_an_older_version_is_not_shown(client, lib):
    from core.database import SessionLocal
    from core.models import Photo, PhotoText

    with SessionLocal() as s:
        row = s.get(PhotoText, lib["sign"])
        row.sig = "ffffffffffffffff"
        s.commit()
        try:
            assert client.get(f"/api/photos/{lib['sign']}").json()["text_lines"] == []
        finally:
            row.sig = s.get(Photo, lib["sign"]).sig
            s.commit()


@needs_db
def test_the_state_is_off_where_nothing_will_read(client, lib, monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "ocr_videos", False)
    assert client.get(f"/api/photos/{lib['clip']}").json()["text_state"] == "off"
    monkeypatch.setattr(get_settings(), "ocr_enabled", False)
    assert client.get(f"/api/photos/{lib['letter']}").json()["text_state"] == "off"
