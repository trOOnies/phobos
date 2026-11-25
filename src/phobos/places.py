"""Offline reverse geocoding: GPS coordinates -> city, region and country.

Locations never leave the computer. The nearest town is looked up in a bundled
city list (every place with 5,000+ inhabitants, from GeoNames, CC BY 4.0 - see
``scripts/build_places.py``). It's loaded on first use (~0.5 s) and indexed in a
grid of 1-degree cells, so a lookup only measures the distance to a few dozen towns.
"""

from __future__ import annotations

import gzip
import math
import threading
from dataclasses import dataclass
from importlib import resources

# Farther than this from any town (at sea, deep in a desert...) gives no place.
MAX_KM = 100.0
NEAR_KM = 15.0
EARTH_KM = 6371.0
_KM_PER_DEG = 111.2


@dataclass(frozen=True)
class Place:
    city: str
    region: str
    country: str

    def as_dict(self) -> dict[str, str]:
        return {"city": self.city, "region": self.region, "country": self.country}


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


class Gazetteer:
    def __init__(self, rows: list[tuple[str, str, str, float, float]]):
        # (city, region, country name, lat, lon)
        self.rows = rows
        self.cells: dict[tuple[int, int], list[int]] = {}
        for i, (_, _, _, lat, lon) in enumerate(rows):
            self.cells.setdefault((math.floor(lat), math.floor(lon)), []).append(i)

    @classmethod
    def load(cls) -> Gazetteer:
        data = resources.files("phobos") / "data"
        countries = dict(
            line.split("\t", 1)
            for line in (data / "countries.tsv").read_text(encoding="utf-8").splitlines()
        )
        rows = []
        with gzip.open(data / "places.tsv.gz", "rt", encoding="utf-8") as fh:  # type: ignore[arg-type]
            for line in fh:
                city, region, code, lat, lon = line.rstrip("\n").split("\t")
                rows.append((city, region, countries.get(code, code), float(lat), float(lon)))
        return cls(rows)

    def nearest(self, lat: float, lon: float, max_km: float = MAX_KM) -> Place | None:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        # Most photos are taken in or near a town: try a small radius (few cells) first.
        if max_km > NEAR_KM and (place := self._nearest(lat, lon, NEAR_KM)):
            return place
        return self._nearest(lat, lon, max_km)

    def _nearest(self, lat: float, lon: float, max_km: float) -> Place | None:
        dlat = max_km / _KM_PER_DEG
        # Degrees of longitude shrink towards the poles.
        cos = max(math.cos(math.radians(min(abs(lat) + dlat, 89.9))), 0.01)
        dlon = min(max_km / (_KM_PER_DEG * cos), 180.0)
        best, best_km = None, max_km
        for cy in range(math.floor(lat - dlat), math.floor(lat + dlat) + 1):
            for cx in range(math.floor(lon - dlon), math.floor(lon + dlon) + 1):
                wrapped = (cx + 180) % 360 - 180  # across the antimeridian
                for i in self.cells.get((cy, wrapped), ()):
                    row = self.rows[i]
                    km = _distance_km(lat, lon, row[3], row[4])
                    if km <= best_km:
                        best, best_km = row, km
        return Place(best[0], best[1], best[2]) if best else None


_lock = threading.Lock()
_gazetteer: Gazetteer | None = None


def gazetteer() -> Gazetteer:
    global _gazetteer  # noqa: PLW0603 - loaded once, shared by every thread
    with _lock:
        if _gazetteer is None:
            _gazetteer = Gazetteer.load()
        return _gazetteer


def lookup(lat: float | None, lon: float | None) -> Place | None:
    """Nearest town to a GPS position, or None when there's none close enough."""
    if lat is None or lon is None:
        return None
    return gazetteer().nearest(lat, lon)
