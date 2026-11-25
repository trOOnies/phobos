"""Place names from GPS (offline gazetteer), typed-in places, and people & pets."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_image
from phobos import db, places
from phobos.library import Library

BUENOS_AIRES = (-34.6037, -58.3816)
EIFFEL_TOWER = (48.8584, 2.2945)  # in "Paris 16 Passy", stored as Paris
MID_ATLANTIC = (0.0, -30.0)


def upload(client: TestClient, path: Path) -> int:
    with path.open("rb") as fh:
        r = client.post("/api/upload", files={"file": (path.name, fh)})
    assert r.status_code == 200, r.text
    return r.json()["media_id"]


def ids_of(client: TestClient, **params) -> list[int]:
    r = client.get("/api/media", params=params)
    assert r.status_code == 200, r.text
    return [i["id"] for i in r.json()["items"]]


@pytest.fixture
def located(client: TestClient, src_dir: Path) -> dict[str, int]:
    """Photos taken in Buenos Aires, Paris, at sea, and one without GPS."""
    spots = {"ba": BUENOS_AIRES, "paris": EIFFEL_TOWER, "sea": MID_ATLANTIC, "none": None}
    return {
        key: upload(client, make_image(src_dir / f"{key}.jpg", color=(i * 40, 9, 9), gps=gps))
        for i, (key, gps) in enumerate(spots.items())
    }


# ------------------------------------------------------------------ gazetteer


@pytest.mark.parametrize(
    ("lat", "lon", "city", "country"),
    [
        (*BUENOS_AIRES, "Buenos Aires", "Argentina"),
        (*EIFFEL_TOWER, "Paris", "France"),
        (40.7128, -74.0060, "New York City", "United States"),
        (64.1466, -21.9426, "Reykjavík", "Iceland"),
        (-16.5, -179.9, "Labasa", "Fiji"),  # the nearest town is across the antimeridian
    ],
)
def test_lookup_names_the_nearest_town(lat, lon, city, country):
    place = places.lookup(lat, lon)
    assert place is not None
    assert (place.city, place.country) == (city, country)


def test_lookup_far_from_everything_or_invalid():
    assert places.lookup(*MID_ATLANTIC) is None
    assert places.lookup(91, 0) is None
    assert places.lookup(None, None) is None


# ------------------------------------------------------------------ places


def test_import_names_the_place(client: TestClient, located):
    ba = client.get(f"/api/media/{located['ba']}").json()
    assert (ba["city"], ba["region"], ba["country"]) == (
        "Buenos Aires", "Buenos Aires F.D.", "Argentina",
    )  # fmt: skip
    assert ba["place_source"] == "gps"
    sea = client.get(f"/api/media/{located['sea']}").json()
    assert (sea["city"], sea["country"], sea["place_source"]) == (None, None, "gps")
    none = client.get(f"/api/media/{located['none']}").json()
    assert (none["country"], none["place_source"]) == (None, None)


def test_place_filters_and_search(client: TestClient, located):
    assert ids_of(client, country="argentina") == [located["ba"]]
    assert ids_of(client, country="France", city="paris") == [located["paris"]]
    assert ids_of(client, city="Nowhere") == []
    assert ids_of(client, q="argentina") == [located["ba"]]
    assert ids_of(client, q="Île-de-France") == [located["paris"]]  # regions too
    months = client.get("/api/timeline", params={"country": "France"}).json()
    assert sum(m["count"] for m in months) == 1


def test_places_summary(client: TestClient, located, src_dir: Path):
    upload(client, make_image(src_dir / "ba2.jpg", color="blue", gps=(-34.61, -58.39)))
    out = client.get("/api/places").json()
    assert [c["country"] for c in out] == ["Argentina", "France"]  # most items first
    assert out[0] == {
        "country": "Argentina", "count": 2,
        "cities": [{"city": "Buenos Aires", "region": "Buenos Aires F.D.", "count": 2}],
    }  # fmt: skip
    client.post("/api/media/actions", json={"ids": [located["paris"]], "action": "trash"})
    assert [c["country"] for c in client.get("/api/places").json()] == ["Argentina"]


def test_typed_in_place_and_reset(client: TestClient, located):
    mid = located["none"]
    r = client.patch(f"/api/media/{mid}", json={"place": {"city": "  Bariloche ", "country": "AR"}})
    assert r.status_code == 200
    item = r.json()
    assert (item["city"], item["region"], item["country"]) == ("Bariloche", None, "AR")
    assert item["place_source"] == "manual"
    assert ids_of(client, city="bariloche") == [mid]
    # Forgetting it: no GPS, so no place at all.
    item = client.patch(f"/api/media/{mid}", json={"place": None}).json()
    assert (item["city"], item["country"], item["place_source"]) == (None, None, None)

    # A typed-in place overrides the GPS one; forgetting it brings the GPS place back.
    ba = located["ba"]
    client.patch(f"/api/media/{ba}", json={"place": {"country": "Uruguay"}})
    assert ids_of(client, country="Argentina") == []
    item = client.patch(f"/api/media/{ba}", json={"place": None}).json()
    assert (item["city"], item["place_source"]) == ("Buenos Aires", "gps")
    # Other edits leave the place alone.
    item = client.patch(f"/api/media/{ba}", json={"favorite": True}).json()
    assert item["city"] == "Buenos Aires"


@pytest.mark.parametrize("place", [{}, {"region": "Patagonia"}, {"city": "  ", "country": ""}])
def test_place_needs_a_city_or_country(client: TestClient, located, place):
    r = client.patch(f"/api/media/{located['none']}", json={"place": place})
    assert r.status_code == 422


def test_bulk_place_actions(client: TestClient, located):
    ids = [located["ba"], located["none"]]
    body = {"ids": ids, "action": "set_place", "place": {"city": "Rosario", "country": "Argentina"}}
    assert client.post("/api/media/actions", json=body).json()["affected"] == 2
    assert sorted(ids_of(client, city="Rosario")) == sorted(ids)
    r = client.post("/api/media/actions", json={"ids": ids, "action": "set_place"})
    assert r.status_code == 422
    r = client.post("/api/media/actions", json={"ids": ids, "action": "reset_place"})
    assert r.json()["affected"] == 2
    assert ids_of(client, city="Buenos Aires") == [located["ba"]]
    assert ids_of(client, city="Rosario") == []


def test_older_items_get_their_place_filled_in(library: Library, src_dir: Path):
    gps = library.import_file(make_image(src_dir / "a.jpg", gps=BUENOS_AIRES)).media_id
    typed = library.import_file(make_image(src_dir / "b.jpg", gps=BUENOS_AIRES, color="blue"))
    with library.conn() as conn:
        # As if imported before places existed; the second one was named by hand since.
        conn.execute("UPDATE media SET city = NULL, country = NULL, place_source = NULL")
        conn.execute(
            "UPDATE media SET country = 'Narnia', place_source = 'manual' WHERE id = ?",
            (typed.media_id,),
        )
    assert library.fill_places() == 1
    assert library.fill_places() == 0  # nothing left to do
    with library.conn() as conn:
        rows = dict(conn.execute("SELECT id, country FROM media").fetchall())
    assert rows == {gps: "Argentina", typed.media_id: "Narnia"}


def test_migration_adds_places_to_an_existing_library(tmp_path: Path):
    path = tmp_path / "old.db"
    conn = db.connect(path)
    for version, script in enumerate(db.MIGRATIONS[:3], start=1):
        for statement in filter(str.strip, script.split(";")):
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {version}")
    conn.execute(
        "INSERT INTO media (sha256, kind, rel_path, original_name, mime, size, taken_at, "
        "taken_at_source, latitude, longitude, imported_at) VALUES "
        "('x', 'image', 'a.jpg', 'a.jpg', 'image/jpeg', 1, '2020-01-01T00:00:00', 'exif', "
        "-34.6, -58.4, '2020-01-01T00:00:00')"
    )
    db.migrate(conn)
    row = conn.execute("SELECT country, place_source FROM media").fetchone()
    assert tuple(row) == (None, None)  # filled in by Library.fill_places at startup
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0
    conn.close()


# ------------------------------------------------------------------ people & pets


def test_people_crud(client: TestClient):
    r = client.post("/api/people", json={"name": "  Grandma   Rosa "})
    assert r.status_code == 201
    rosa = r.json()
    assert (rosa["name"], rosa["kind"], rosa["count"], rosa["cover_id"]) == (
        "Grandma Rosa", "person", 0, None,
    )  # fmt: skip
    rex = client.post("/api/people", json={"name": "Rex", "kind": "animal"}).json()
    assert rex["kind"] == "animal"
    # Names are unique, ignoring case.
    r = client.post("/api/people", json={"name": "rex"})
    assert r.status_code == 409
    assert "Rex" in r.json()["detail"]
    assert client.post("/api/people", json={"name": "   "}).status_code == 422
    assert client.post("/api/people", json={"name": "Tom", "kind": "robot"}).status_code == 422

    assert [p["name"] for p in client.get("/api/people").json()] == ["Grandma Rosa", "Rex"]
    r = client.patch(f"/api/people/{rex['id']}", json={"name": "Rex the dog"})
    assert r.json()["name"] == "Rex the dog"
    r = client.patch(f"/api/people/{rex['id']}", json={"name": "GRANDMA ROSA"})
    assert r.status_code == 409
    assert client.patch(f"/api/people/{rex['id']}", json={"kind": "person"}).json()["kind"] == (
        "person"
    )
    assert client.delete(f"/api/people/{rex['id']}").status_code == 204
    assert client.get(f"/api/people/{rex['id']}").status_code == 404
    assert client.delete(f"/api/people/{rex['id']}").status_code == 404


def test_tagging_people_in_photos(client: TestClient, located):
    rosa = client.post("/api/people", json={"name": "Rosa"}).json()["id"]
    rex = client.post("/api/people", json={"name": "Rex", "kind": "animal"}).json()["id"]
    both, only_rex = located["ba"], located["paris"]

    def act(action, ids, person):
        body = {"ids": ids, "action": action, "person_id": person}
        return client.post("/api/media/actions", json=body)

    assert act("add_person", [both, only_rex], rex).json()["affected"] == 2
    assert act("add_person", [both], rex).json()["affected"] == 0  # already there
    assert act("add_person", [both], rosa).json()["affected"] == 1
    assert act("add_person", [both], 4242).status_code == 404
    r = client.post("/api/media/actions", json={"ids": [both], "action": "add_person"})
    assert r.status_code == 422

    item = client.get(f"/api/media/{both}").json()
    assert item["people"] == [
        {"id": rex, "name": "Rex", "kind": "animal"},
        {"id": rosa, "name": "Rosa", "kind": "person"},
    ]
    assert sorted(ids_of(client, person=rex)) == sorted([both, only_rex])
    assert ids_of(client, person=rosa) == [both]
    assert ids_of(client, q="rosa") == [both]  # search finds names too

    people = {p["id"]: p for p in client.get("/api/people").json()}
    assert people[rex]["count"] == 2
    assert people[rosa]["cover_id"] == both

    # A chosen cover wins; trashed items aren't counted or shown.
    client.patch(f"/api/people/{rex}", json={"cover_media_id": only_rex})
    assert client.get(f"/api/people/{rex}").json()["cover_id"] == only_rex
    client.post("/api/media/actions", json={"ids": [only_rex], "action": "trash"})
    p = client.get(f"/api/people/{rex}").json()
    assert (p["count"], p["cover_id"]) == (1, both)

    assert act("remove_person", [both], rex).json()["affected"] == 1
    assert ids_of(client, person=rex) == []
    # Forgetting someone keeps their photos.
    assert client.delete(f"/api/people/{rosa}").status_code == 204
    assert client.get(f"/api/media/{both}").json()["people"] == []


def test_cropped_copy_keeps_people_and_place(client: TestClient, located):
    rex = client.post("/api/people", json={"name": "Rex", "kind": "animal"}).json()["id"]
    src = located["ba"]
    client.post("/api/media/actions", json={"ids": [src], "action": "add_person", "person_id": rex})
    client.patch(f"/api/people/{rex}", json={"cover_media_id": src})
    box = {"left": 0, "top": 0, "right": 0.5, "bottom": 0.5}
    r = client.post(f"/api/media/{src}/edit", json={"crop": box, "mode": "replace"})
    assert r.status_code == 200, r.text
    new = r.json()["media"]
    detail = client.get(f"/api/media/{new['id']}").json()
    assert [p["name"] for p in detail["people"]] == ["Rex"]
    assert (detail["city"], detail["place_source"]) == ("Buenos Aires", "gps")
    assert client.get(f"/api/people/{rex}").json()["cover_media_id"] == new["id"]


def test_purging_media_removes_its_people_links(client: TestClient, located):
    rex = client.post("/api/people", json={"name": "Rex"}).json()["id"]
    mid = located["sea"]
    client.post("/api/media/actions", json={"ids": [mid], "action": "add_person", "person_id": rex})
    client.post("/api/media/actions", json={"ids": [mid], "action": "trash"})
    client.post("/api/media/actions", json={"ids": [mid], "action": "purge"})
    assert client.get(f"/api/people/{rex}").json()["count"] == 0
    with pytest.raises(sqlite3.IntegrityError):
        conn = db.connect(client.app.state.library.settings.db_path)  # type: ignore[attr-defined]
        try:
            conn.execute("INSERT INTO media_people (media_id, person_id) VALUES (?, ?)", (mid, rex))
        finally:
            conn.close()


def test_metrics_count_places_and_people(client: TestClient, located):
    rex = client.post("/api/people", json={"name": "Rex", "kind": "animal"}).json()["id"]
    ids = [located["ba"], located["none"]]
    client.post("/api/media/actions", json={"ids": ids, "action": "add_person", "person_id": rex})
    m = client.get("/api/metrics").json()
    t = m["totals"]
    assert (t["with_location"], t["with_place"], t["people"], t["with_people"]) == (3, 2, 1, 2)
    # Ties come in any order.
    assert sorted(m["countries"], key=lambda c: c["country"]) == [
        {"country": "Argentina", "count": 1},
        {"country": "France", "count": 1},
    ]
