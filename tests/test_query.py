"""The filter language: pure parsing, and the SQL it turns into.

No database here. What is checked against Postgres (that the clauses select
the right rows) is in test_api_photos.py and test_api_places.py; this file is
about reading the bar's text the way a person meant it, and about every value
from that text reaching SQL as a bound parameter and never as SQL.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.query import (
    IS_FLAGS,
    NEAR_DEFAULT_KM,
    Token,
    filter_clauses,
    like_pattern,
    listed,
    normalize_bbox,
    parse_period,
    parse_query,
    tokenize,
    wrap_lon,
)
from core.models import Photo


def midnight(y, m, d):
    return datetime.combine(date(y, m, d), time())


def labels(q: str) -> list[str]:
    return parse_query(q).describe()["filters"]


def errors(q: str) -> list[str]:
    return parse_query(q).errors


# --- tokens -------------------------------------------------------------------


def test_whitespace_separates_and_quotes_group():
    assert [t.text for t in tokenize('cows  "on the beach"\tsunset')] == ["cows", "on the beach", "sunset"]


def test_a_key_with_a_quoted_value_is_one_token():
    assert tokenize('in:"New York" x') == [Token("in:New York"), Token("x")]


def test_a_token_quoted_as_a_whole_is_marked():
    assert tokenize('"in:the woods"') == [Token("in:the woods", quoted=True)]


def test_an_unclosed_quote_runs_to_the_end_while_typing():
    assert tokenize('dog "on the') == [Token("dog"), Token("on the", quoted=True)]


def test_apostrophes_and_backslashes_are_just_text():
    # shlex would call both of these a syntax error.
    assert [t.text for t in tokenize(r"Mc'Donald's C:\photos")] == ["Mc'Donald's", r"C:\photos"]


def test_typographic_quotes_group_like_straight_ones():
    assert [t.text for t in tokenize("\u201cred car\u201d")] == ["red car"]


def test_empty_input_and_empty_quotes_give_nothing():
    assert tokenize("") == []
    assert tokenize("   ") == []
    assert tokenize('""') == []
    assert tokenize(None) == []


# --- words --------------------------------------------------------------------


def test_bare_words_are_the_search_text():
    spec = parse_query("red  car")
    assert spec.words == ["red", "car"]
    assert spec.text == "red car"
    assert spec.mode == "score"
    assert not spec.filters and not spec.errors


def test_an_empty_query_is_the_timeline():
    spec = parse_query("")
    assert spec.mode == "date"
    assert not spec.scored
    assert spec.describe() == {"text": "", "similar": None, "face": None, "filters": [], "errors": []}


def test_a_quoted_filter_is_searched_for_as_words():
    spec = parse_query('"in:the woods"')
    assert spec.words == ["in:the woods"]
    assert not spec.filters


def test_a_colon_after_digits_is_not_a_key():
    assert parse_query("12:30").words == ["12:30"]


# --- filters ------------------------------------------------------------------


def test_filters_in_any_order_with_words():
    spec = parse_query("is:video cows in:Potsdam camera:iPhone file:IMG_1")
    assert spec.words == ["cows"]
    assert [f.label for f in spec.filters] == ["is:video", "in:Potsdam", "camera:iPhone", "file:IMG_1"]


def test_keys_are_case_insensitive_and_values_keep_their_case():
    assert labels("IN:Potsdam IS:Video") == ["in:Potsdam", "is:video"]


def test_labels_quote_values_with_spaces():
    assert labels('in:"New York"') == ['in:"New York"']


def test_every_is_flag_is_known():
    spec = parse_query(" ".join(f"is:{f}" for f in IS_FLAGS))
    assert [f.value for f in spec.filters] == list(IS_FLAGS)
    assert not spec.errors


def test_favourite_is_a_favorite():
    assert labels("is:favourite IS:Favorite") == ["is:favorite", "is:favorite"]


def test_an_album_takes_a_quoted_name():
    (f,) = parse_query('album:"Urlaub Polen mit Kindern" sunset').filters
    assert (f.key, f.value, f.label) == ("album", "Urlaub Polen mit Kindern", 'album:"Urlaub Polen mit Kindern"')
    assert labels("album:WhatsApp") == ["album:WhatsApp"]
    # Quoted as a whole it is words, like any other filter.
    assert parse_query('"album:Hühner"').words == ["album:Hühner"]


def test_an_unknown_is_flag_is_an_error_not_a_filter():
    spec = parse_query("is:unicorn")
    assert not spec.filters
    assert spec.errors and "is:unicorn" in spec.errors[0]


def test_an_unknown_key_is_reported_and_ignored():
    spec = parse_query("dog colour:red")
    assert spec.words == ["dog"]
    assert spec.errors == ['unknown filter "colour:red"']


def test_a_key_with_nothing_after_it_is_somebody_typing():
    spec = parse_query("dog in:")
    assert spec.words == ["dog"]
    assert not spec.filters and not spec.errors


def test_a_space_after_the_colon_still_gives_the_key_its_value():
    # It used to drop the key and search for the value as a word.
    spec = parse_query("text: rinderpass sort:date")
    assert spec.words == [] and spec.sort == "date" and not spec.errors
    assert labels("text: rinderpass sort:date") == ["text:rinderpass"]
    assert labels('text: "opening hours" cows') == ['text:"opening hours"']
    assert labels("in: Potsdam 2024") == ["in:Potsdam", "year:2024"]
    assert labels("year: 2024") == ["year:2024"]


def test_a_dangling_key_does_not_swallow_the_next_filter_or_prose():
    spec = parse_query("text: sort:date")
    assert not spec.filters and spec.sort == "date" and not spec.errors
    assert labels("text: in:Berlin") == ["in:Berlin"]
    # An unknown key's colon is prose: the word stays a word.
    spec = parse_query("Rezept: Kuchen")
    assert spec.words == ["Kuchen"] and spec.errors == ['unknown filter "Rezept:"']


# --- dates --------------------------------------------------------------------


def taken(q: str):
    (f,) = parse_query(q).filters
    assert f.key == "taken"
    return f.value


def test_year_month_and_on_are_half_open_ranges():
    assert taken("year:2024") == (midnight(2024, 1, 1), midnight(2025, 1, 1))
    assert taken("month:2024-07") == (midnight(2024, 7, 1), midnight(2024, 8, 1))
    assert taken("month:2024-12") == (midnight(2024, 12, 1), midnight(2025, 1, 1))
    assert taken("on:2024-02-29") == (midnight(2024, 2, 29), midnight(2024, 3, 1))


def test_after_includes_the_start_and_before_includes_the_end_of_the_period():
    assert taken("after:2024") == (midnight(2024, 1, 1), None)
    assert taken("after:2024-07") == (midnight(2024, 7, 1), None)
    assert taken("after:2024-07-22") == (midnight(2024, 7, 22), None)
    # "before July" includes July, as "before 2024" includes 2024: the named
    # period is the boundary, and it is on the inside.
    assert taken("before:2024-07") == (None, midnight(2024, 8, 1))
    assert taken("before:2024-07-22") == (None, midnight(2024, 7, 23))


def test_a_bare_year_is_a_year_filter():
    spec = parse_query("2019 beach")
    assert spec.words == ["beach"]
    assert [f.label for f in spec.filters] == ["year:2019"]


def test_a_bare_number_outside_the_years_is_a_word():
    assert parse_query("1899").words == ["1899"]
    assert parse_query("2101").words == ["2101"]
    assert parse_query("12345").words == ["12345"]
    assert parse_query('"2019"').words == ["2019"]


def test_dates_must_have_the_shape_their_key_names():
    assert errors("year:2024-07")
    assert errors("month:2024")
    assert errors("on:2024-07")
    assert errors("on:2024-02-30")
    assert errors("month:2024-13")
    assert errors("year:24")
    assert errors("after:yesterday")


def test_date_labels_are_canonical():
    assert labels("month:2024-7 before:2021-6-3") == ["month:2024-07", "before:2021-06-03"]


@pytest.mark.parametrize("text,expected", [
    ("2024", (midnight(2024, 1, 1), midnight(2025, 1, 1))),
    ("2024-2", (midnight(2024, 2, 1), midnight(2024, 3, 1))),
    ("2023-02-28", (midnight(2023, 2, 28), midnight(2023, 3, 1))),
    ("1900", (midnight(1900, 1, 1), midnight(1901, 1, 1))),
    ("1899", None),
    ("2024-00", None),
    ("2023-02-29", None),
    ("20240", None),
    ("", None),
])
def test_parse_period(text, expected):
    assert parse_period(text) == expected


# --- places -------------------------------------------------------------------


def test_near_defaults_to_a_kilometre():
    (f,) = parse_query("near:52.39,13.06").filters
    assert f.value == (52.39, 13.06, NEAR_DEFAULT_KM)
    assert f.label == "near:52.39,13.06"


def test_near_takes_a_radius_with_or_without_km():
    assert parse_query("near:52.39,13.06,5").filters[0].value == (52.39, 13.06, 5.0)
    assert parse_query("near:52.39,13.06,0.5km").filters[0].value == (52.39, 13.06, 0.5)
    assert parse_query('near:"52.39, 13.06, 2"').filters[0].value == (52.39, 13.06, 2.0)


@pytest.mark.parametrize("value", ["52.39", "a,b", "91,0", "0,181", "52,13,0", "52,13,-1", "nan,1", "1,2,3,4"])
def test_near_refuses_what_is_not_a_place(value):
    spec = parse_query(f"near:{value}")
    assert not spec.filters
    assert spec.errors


def test_bbox_in_order():
    (f,) = parse_query("bbox:13.0,52.3,13.2,52.5").filters
    assert f.value == (13.0, 52.3, 13.2, 52.5)
    assert f.label == "bbox:13,52.3,13.2,52.5"


def test_bbox_across_the_antimeridian_stays_as_given():
    (f,) = parse_query("bbox:170,-20,-170,-10").filters
    assert f.value == (170.0, -20.0, -170.0, -10.0)


def test_bbox_from_a_map_scrolled_round_the_world_is_wrapped():
    (f,) = parse_query("bbox:170,-20,190,-10").filters
    assert f.value == (170.0, -20.0, -170.0, -10.0)


@pytest.mark.parametrize("value", ["1,2,3", "0,10,1,5", "a,b,c,d", "190,0,-190,1"])
def test_bbox_refuses_what_is_not_a_box(value):
    spec = parse_query(f"bbox:{value}")
    assert not spec.filters
    assert spec.errors


def test_normalize_bbox():
    assert normalize_bbox(-200, -10, 200, 10) == (-180, -10, 180, 10)       # the whole world
    assert normalize_bbox(-190, -10, -170, 10) == (170, -10, -170, 10)      # wrapped, crossing
    assert normalize_bbox(185, 0, 190, 1) == (-175, 0, -170, 1)             # wrapped, not crossing
    assert normalize_bbox(0, -100, 10, 100) == (0, -90, 10, 90)             # latitudes clamped
    assert normalize_bbox(0, 10, 10, 0) is None                             # south of north


def test_wrap_lon():
    assert wrap_lon(180) == 180
    assert wrap_lon(-180) == -180
    assert wrap_lon(190) == -170
    assert wrap_lon(-190) == 170
    assert wrap_lon(540) == -180


# --- similar and sort -----------------------------------------------------------


def test_similar_is_a_ranking_by_itself():
    spec = parse_query("similar:42")
    assert spec.similar == 42
    assert spec.mode == "score"
    assert spec.describe()["similar"] == 42
    assert spec.describe()["filters"] == []


def test_similar_wins_over_words_and_says_so():
    spec = parse_query("cows similar:42 in:Potsdam")
    assert spec.similar == 42
    assert spec.words == []
    assert spec.text == ""
    assert spec.errors == ["ignored with similar: cows"]
    assert [f.label for f in spec.filters] == ["in:Potsdam"]


@pytest.mark.parametrize("value", ["abc", "0", "-3", "1.5"])
def test_similar_needs_a_photo_id(value):
    spec = parse_query(f"similar:{value}")
    assert spec.similar is None
    assert spec.errors


def test_two_different_similars_keep_the_first():
    spec = parse_query("similar:1 similar:2")
    assert spec.similar == 1
    assert spec.errors


def test_sort_date_orders_a_ranking_by_date():
    spec = parse_query("cows sort:date")
    assert spec.sort == "date"
    assert spec.by_date
    assert spec.mode == "score"
    # Without anything to rank there is nothing to re-order.
    assert not parse_query("sort:date").by_date


def test_an_unknown_sort_is_an_error():
    assert errors("sort:size")


def test_nsfw_on_its_own_is_a_ranking_by_the_classifier():
    spec = parse_query("is:nsfw")
    assert spec.nsfw_ranked and not spec.scored
    assert spec.mode == "score"
    assert not spec.by_date
    # sort:date re-orders it like any ranking; it is still one.
    dated = parse_query("is:nsfw sort:date")
    assert dated.mode == "score" and dated.by_date
    # With words, the words rank and is:nsfw is only a filter.
    worded = parse_query("red is:nsfw")
    assert worded.scored and not worded.nsfw_ranked
    assert parse_query("is:favorite").mode == "date"


# --- SQL ----------------------------------------------------------------------


def compiled(clauses):
    stmt = select(Photo.id).where(*clauses)
    return stmt.compile(dialect=postgresql.dialect())


def test_user_text_only_ever_reaches_sql_as_a_parameter():
    hostile = "x'); DROP TABLE photos; --"
    spec = parse_query(f'in:"{hostile}" camera:"{hostile}" file:"{hostile}" album:"{hostile}"')
    c = compiled(filter_clauses(spec))
    assert "DROP" not in str(c)
    assert any("DROP TABLE" in str(v) for v in c.params.values())


def test_every_filter_compiles():
    spec = parse_query(
        "in:Potsdam camera:Apple file:IMG is:live is:located near:52,13,5 "
        "bbox:170,-20,-170,-10 year:2024 after:2020 before:2025-06 album:Hühner "
        + " ".join(f"is:{f}" for f in IS_FLAGS)
    )
    sql = str(compiled(filter_clauses(spec)))
    assert "ILIKE" in sql
    assert "asin" in sql                              # the great-circle distance
    assert " OR " in sql                              # the antimeridian's two ranges
    assert "live_video_id IS NOT NULL" in sql
    assert "lower(albums.name) = lower(" in sql       # album: and is:whatsapp
    assert "photos.nsfw >=" in sql


def test_the_nsfw_threshold_is_read_when_the_query_is_built(monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "nsfw_threshold", 0.42)
    assert 0.42 in compiled(filter_clauses(parse_query("is:nsfw"))).params.values()


def test_listed_always_leaves_out_companions_the_way_the_index_is_written():
    # ix_photos_timeline is partial on `WHERE NOT is_companion`; the query has
    # to say it the same way for the planner to use it (see query.listed).
    sql = str(compiled(listed(parse_query(""))))
    assert "NOT photos.is_companion" in sql


def test_listed_leaves_out_what_icloud_hides_and_what_is_being_deleted():
    sql = str(compiled(listed(parse_query(""))))
    assert "photos.superseded_by IS NULL" in sql
    assert "photos.trashed_at IS NULL" in sql
    assert "NOT photos.hidden" in sql
    assert "NOT photos.icloud_deleted" in sql


def test_is_hidden_and_is_deleted_lift_their_own_exclusion_only():
    hidden = str(compiled(listed(parse_query("is:hidden"))))
    assert "NOT photos.hidden" not in hidden and "photos.hidden IS true" in hidden
    assert "NOT photos.icloud_deleted" in hidden
    deleted = str(compiled(listed(parse_query("is:deleted"))))
    assert "NOT photos.icloud_deleted" not in deleted and "photos.icloud_deleted IS true" in deleted
    assert "NOT photos.hidden" in deleted
    # Neither ever brings back an edit's original or a photo being deleted.
    for sql in (hidden, deleted):
        assert "photos.superseded_by IS NULL" in sql and "photos.trashed_at IS NULL" in sql


def test_like_patterns_take_wildcards_literally():
    assert like_pattern("IMG_1") == "%IMG\\_1%"
    assert like_pattern("100%") == "%100\\%%"
    assert like_pattern("a\\b") == "%a\\\\b%"
