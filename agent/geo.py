"""Coordinates to a town, a region and a country, offline.

``reverse_geocode`` ships GeoNames' populated places (every town over 1,000
inhabitants) and answers from a KD-tree built once per process, so naming
16,000 locations is a fraction of a second and nothing about where you have
been leaves the machine. The answer is "the nearest town", which is what a
person means by "photos in Potsdam" far more often than a street address.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# The nearest town is always *some* town, even from a ship in mid-Atlantic
# (it answers "Mumford, Ghana" for 0.5, 0.5). Past this distance a name would
# be a claim the data cannot back, so the photo keeps its coordinates and no
# place: the map still shows it where it was.
MAX_DISTANCE_KM = 150.0


@dataclass(frozen=True)
class Place:
    city: str
    region: str
    country: str
    country_code: str

    @property
    def place(self) -> str:
        return ", ".join(p for p in (self.city, self.region, self.country) if p)

    def columns(self) -> dict:
        return {
            "city": self.city[:200],
            "region": self.region[:200],
            "country": self.country[:200],
            "country_code": self.country_code[:8],
            "place": self.place,
        }


EMPTY = Place("", "", "", "")


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance, haversine."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def lookup(coords: Sequence[tuple[float, float]]) -> list[Place]:
    """One Place per coordinate pair, in order; EMPTY where nothing is near."""
    if not coords:
        return []
    import reverse_geocode  # the KD-tree loads on first use, ~0.3 s

    out: list[Place] = []
    for (lat, lon), hit in zip(coords, reverse_geocode.search(list(coords))):
        # The library hands back its own shared dicts; read, never keep.
        if distance_km(lat, lon, float(hit["latitude"]), float(hit["longitude"])) > MAX_DISTANCE_KM:
            out.append(EMPTY)
            continue
        out.append(Place(
            city=str(hit.get("city") or ""),
            region=str(hit.get("state") or ""),
            country=str(hit.get("country") or ""),
            country_code=str(hit.get("country_code") or ""),
        ))
    return out
