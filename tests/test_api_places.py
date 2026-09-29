"""Places and the map's clusters.

Five photos in one Potsdam street, two in Berlin 25 km away, one on each side
of the antimeridian in Fiji. At zoom 3 a cell is 11.25 degrees, so Potsdam and
Berlin are one cluster; at zoom 12 it is about 2.4 km, so they are two.
"""

from __future__ import annotations

import pytest
from server_helpers import (
    BLUE,
    RED,
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

POTSDAM = {"place": "Potsdam, Brandenburg, Germany", "city": "Potsdam", "region": "Brandenburg",
           "country": "Germany", "country_code": "DE"}
BERLIN = {"place": "Berlin, Berlin, Germany", "city": "Berlin", "region": "Berlin",
          "country": "Germany", "country_code": "DE"}
FIJI = {"place": "Labasa, Northern, Fiji", "city": "Labasa", "region": "Northern",
        "country": "Fiji", "country_code": "FJ"}


@pytest.fixture(scope="module")
def lib(client):
    from core.database import SessionLocal

    with SessionLocal() as s:
        p = {}
        for i in range(3):
            p[f"potsdam{i}"] = make_photo(s, taken=wall(2024, 1, 1 + i), lat=52.3900 + i * 0.0001,
                                          lon=13.0600 + i * 0.0001, **POTSDAM)
        p["face"] = make_photo(s, taken=wall(2024, 8, 1), lat=52.3905, lon=13.0605, **POTSDAM)
        # Newer, but with no thumbnail yet: not the cluster's face.
        p["no_thumb"] = make_photo(s, taken=wall(2024, 9, 1), lat=52.3904, lon=13.0604, thumb_sig="",
                                   **POTSDAM)
        p["berlin_red"] = make_photo(s, "IMG_RED.JPG", taken=wall(2023, 5, 1), lat=52.5200, lon=13.4050,
                                     **BERLIN)
        add_embedding(s, p["berlin_red"], color_vector(RED))
        p["berlin_video"] = make_photo(s, "IMG_B.MOV", taken=wall(2023, 5, 2), lat=52.5205, lon=13.4055,
                                       **BERLIN)
        add_embedding(s, p["berlin_video"], color_vector(BLUE))
        p["fiji_e"] = make_photo(s, taken=wall(2022, 1, 1), lat=-17.8, lon=179.9, **FIJI)
        p["fiji_w"] = make_photo(s, taken=wall(2022, 1, 2), lat=-16.5, lon=-179.9, thumb_sig="", **FIJI)
        p["nowhere"] = make_photo(s, taken=wall(2022, 1, 3))
        # Located, but the hidden half of a Live Photo: on no map.
        p["companion"] = make_photo(s, "IMG_C.MOV", taken=wall(2024, 1, 5), lat=52.39, lon=13.06,
                                    is_companion=True, **POTSDAM)
        s.commit()
        return {k: (v.id, v.sig) for k, v in p.items()}


def clusters(client, **params) -> dict:
    r = client.get("/api/map/clusters", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def by_count(result) -> list[int]:
    return [c["count"] for c in result["clusters"]]


# --- places -------------------------------------------------------------------


def test_places_most_photographed_first(client, lib):
    places = client.get("/api/places").json()["places"]
    assert [(p["place"], p["count"]) for p in places] == [
        ("Potsdam, Brandenburg, Germany", 5),
        ("Berlin, Berlin, Germany", 2),
        ("Labasa, Northern, Fiji", 2),
    ]
    potsdam = places[0]
    assert (potsdam["city"], potsdam["region"], potsdam["country"]) == ("Potsdam", "Brandenburg", "Germany")
    assert potsdam["lat"] == pytest.approx(52.39028, abs=1e-4)
    assert potsdam["lon"] == pytest.approx(13.06028, abs=1e-4)


def test_places_narrow_by_name(client, lib):
    assert [p["city"] for p in client.get("/api/places", params={"q": "pots"}).json()["places"]] == ["Potsdam"]
    assert len(client.get("/api/places", params={"q": "GERMANY"}).json()["places"]) == 2
    assert client.get("/api/places", params={"q": "%"}).json()["places"] == []
    assert len(client.get("/api/places", params={"limit": 1}).json()["places"]) == 1


# --- clusters -----------------------------------------------------------------


def test_the_world_at_zoom_3(client, lib):
    result = clusters(client, zoom=3)
    assert by_count(result) == [7, 1, 1]
    assert result["total"] == 9


def test_at_zoom_12_a_city_is_its_own_cluster(client, lib):
    result = clusters(client, zoom=12, bbox="13,52,14,53")
    assert by_count(result) == [5, 2]
    assert result["total"] == 7
    # Leaflet's zoom can be fractional; the grid is not.
    assert by_count(clusters(client, zoom=12.7, bbox="13,52,14,53")) == [5, 2]


def test_a_cluster_is_where_its_photos_are(client, lib):
    potsdam = clusters(client, zoom=12, bbox="13,52,14,53")["clusters"][0]
    assert potsdam["lat"] == pytest.approx(52.39028, abs=1e-4)
    assert potsdam["lon"] == pytest.approx(13.06028, abs=1e-4)
    assert potsdam["bbox"] == [13.06, 52.39, 13.0605, 52.3905]


def test_a_cluster_s_face_is_its_newest_photo_with_a_thumbnail(client, lib):
    potsdam = clusters(client, zoom=12, bbox="13,52,14,53")["clusters"][0]
    face_id, face_sig = lib["face"]
    assert potsdam["id"] == face_id
    assert potsdam["thumb"] == f"/media/thumb/{face_id}?v={face_sig}"


def test_a_cluster_with_no_thumbnail_still_names_a_photo(client, lib):
    (west,) = [c for c in clusters(client, zoom=3)["clusters"] if c["lon"] < 0]
    assert west["id"] == lib["fiji_w"][0]
    assert west["thumb"] is None


@pytest.mark.parametrize("bbox", ["179,-20,-179,-15", "179,-20,181,-15", "-181,-20,-179,-15"])
def test_a_box_across_the_antimeridian(client, lib, bbox):
    result = clusters(client, zoom=3, bbox=bbox)
    assert result["total"] == 2
    assert sorted(c["id"] for c in result["clusters"]) == sorted([lib["fiji_e"][0], lib["fiji_w"][0]])


def test_a_map_scrolled_round_the_world_is_the_world(client, lib):
    assert clusters(client, zoom=1, bbox="-540,-90,540,90")["total"] == 9


def test_filters_apply_to_the_map(client, lib):
    assert clusters(client, zoom=3, q="is:video")["total"] == 1
    assert clusters(client, zoom=3, q="year:2024")["total"] == 5
    assert clusters(client, zoom=3, q="in:fiji")["total"] == 2


def test_words_restrict_the_map_to_matches(client, lib):
    result = clusters(client, zoom=3, q="red")
    assert result["total"] == 1
    assert result["clusters"][0]["id"] == lib["berlin_red"][0]
    # similar: leaves the photo itself out, and blue is nothing like red.
    assert clusters(client, zoom=3, q=f"similar:{lib['berlin_red'][0]}")["total"] == 0
    assert clusters(client, zoom=3, q="similar:999999") == {"clusters": [], "total": 0}


@pytest.mark.parametrize("bbox", ["1,2,3", "a,b,c,d", "0,10,1,5"])
def test_a_bad_box_is_a_bad_request(client, lib, bbox):
    assert client.get("/api/map/clusters", params={"bbox": bbox, "zoom": 3}).status_code == 400
