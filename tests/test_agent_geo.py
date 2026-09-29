"""Reverse geocoding: the nearest town, and no town when nothing is near."""

from __future__ import annotations

import pytest

from agent import geo


def test_potsdam_and_friends():
    potsdam, sydney, nyc = geo.lookup([(52.39, 13.06), (-33.86, 151.2), (40.71, -74.0)])
    assert potsdam.city == "Potsdam"
    assert potsdam.region == "Brandenburg"
    assert potsdam.country == "Germany"
    assert potsdam.country_code == "DE"
    assert potsdam.place == "Potsdam, Brandenburg, Germany"
    assert sydney.country_code == "AU"
    assert nyc.country == "United States"
    cols = potsdam.columns()
    assert set(cols) == {"city", "region", "country", "country_code", "place"}


def test_open_sea_names_nothing():
    (sea,) = geo.lookup([(0.5, 0.5)])       # the Gulf of Guinea
    assert sea == geo.EMPTY
    assert sea.place == ""


def test_empty_input():
    assert geo.lookup([]) == []


def test_results_do_not_leak_between_calls():
    first = geo.lookup([(52.39, 13.06)])[0]
    geo.lookup([(48.85, 2.35)])
    assert first.city == "Potsdam"


def test_distance():
    assert geo.distance_km(52.52, 13.405, 48.8566, 2.3522) == pytest.approx(878, abs=5)
    assert geo.distance_km(1, 1, 1, 1) == 0
