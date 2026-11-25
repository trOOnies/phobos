"""Build the offline gazetteer Phobos uses to name the places photos were taken.

Phobos never sends your photos' locations anywhere, so GPS coordinates are turned
into place names with a city list bundled in the package. This script rebuilds
that list from GeoNames (https://www.geonames.org, CC BY 4.0):

    curl -O https://download.geonames.org/export/dump/cities5000.zip
    curl -O https://download.geonames.org/export/dump/admin1CodesASCII.txt
    curl -O https://download.geonames.org/export/dump/countryInfo.txt
    python scripts/build_places.py <folder with those files>

It writes ``src/phobos/data/places.tsv.gz`` (city, region, country code, lat, lon)
and ``src/phobos/data/countries.tsv`` (country code, name).
"""

from __future__ import annotations

import csv
import gzip
import io
import re
import sys
import zipfile
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "phobos" / "data"
CITIES = "cities5000"


def main(src: Path) -> None:
    countries: dict[str, str] = {}
    for line in (src / "countryInfo.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            cols = line.split("\t")
            countries[cols[0]] = cols[4]

    regions: dict[str, str] = {}
    for line in (src / "admin1CodesASCII.txt").read_text(encoding="utf-8").splitlines():
        cols = line.split("\t")
        if len(cols) >= 2:
            regions[cols[0]] = cols[1]

    places = []
    with zipfile.ZipFile(src / f"{CITIES}.zip") as zf:
        text = io.TextIOWrapper(zf.open(f"{CITIES}.txt"), encoding="utf-8")
        for cols in csv.reader(text, delimiter="\t", quoting=csv.QUOTE_NONE):
            name, lat, lon, feature, code, admin1, admin2, pop = (
                cols[1], cols[4], cols[5], cols[7], cols[8], cols[10], cols[11], cols[14]
            )  # fmt: skip
            # PPLX are sections of a bigger city (neighbourhoods); the city itself is listed.
            if code not in countries or feature == "PPLX":
                continue
            places.append([name, code, admin1, admin2, int(pop or 0), lat, lon])

    # Numbered districts named after their city ("Paris 16 Passy", "Marseille 05") are
    # renamed to the city, keeping their position so the whole city area is covered.
    biggest: dict[tuple[str, str, str], str] = {}
    for name, code, admin1, admin2, _, _, _ in sorted(places, key=lambda p: p[4]):
        if admin2:
            biggest[(code, admin1, admin2)] = name
    for p in places:
        city = biggest.get((p[1], p[2], p[3]))
        if city and p[0] != city and re.match(rf"{re.escape(city)} \d", p[0]):
            p[0] = city

    rows = [
        (name, regions.get(f"{code}.{admin1}", ""), code, f"{float(lat):.4f}", f"{float(lon):.4f}")
        for name, code, admin1, _, _, lat, lon in places
    ]
    rows.sort(key=lambda r: (r[2], r[1], r[0], r[3], r[4]))

    OUT.mkdir(parents=True, exist_ok=True)
    # mtime=0 keeps the file byte-identical between rebuilds of the same data.
    with (
        (OUT / "places.tsv.gz").open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as gz,
    ):
        gz.write("".join("\t".join(r) + "\n" for r in rows).encode("utf-8"))
    used = sorted({r[2] for r in rows} | set(countries))
    (OUT / "countries.tsv").write_text(
        "".join(f"{c}\t{countries[c]}\n" for c in used), encoding="utf-8"
    )
    print(f"{len(rows)} places, {len(used)} countries -> {OUT}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
