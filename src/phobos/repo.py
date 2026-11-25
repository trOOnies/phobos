"""Query functions over the SQLite schema (see :mod:`phobos.db`)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from phobos.db import transaction

MEDIA_COLUMNS = (
    "id, sha256, kind, rel_path, original_name, mime, size, width, height, duration, "
    "taken_at, taken_at_source, camera_make, camera_model, latitude, longitude, "
    "city, region, country, place_source, description, favorite, imported_at, deleted_at"
)
_M_COLUMNS = ", ".join(f"m.{c.strip()}" for c in MEDIA_COLUMNS.split(","))


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    if "favorite" in d:
        d["favorite"] = bool(d["favorite"])
    return d


# --------------------------------------------------------------------------- media


def insert_media(conn: sqlite3.Connection, record: dict[str, Any]) -> int:
    cols = list(record)
    sql = f"INSERT INTO media ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    return conn.execute(sql, [record[c] for c in cols]).lastrowid


def get_media(conn: sqlite3.Connection, media_id: int) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {MEDIA_COLUMNS} FROM media WHERE id = ?", (media_id,)).fetchone()
    item = _row(row)
    if item is not None:
        item["tags"] = media_tags(conn, media_id)
        item["people"] = media_people(conn, media_id)
        item["albums"] = [
            dict(r)
            for r in conn.execute(
                "SELECT a.id, a.name FROM albums a JOIN album_media am ON am.album_id = a.id "
                "WHERE am.media_id = ? ORDER BY a.name COLLATE NOCASE",
                (media_id,),
            )
        ]
    return item


def find_by_sha(conn: sqlite3.Connection, sha256: str) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {MEDIA_COLUMNS} FROM media WHERE sha256 = ?", (sha256,)).fetchone()
    return _row(row)


def all_rel_paths(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT rel_path FROM media")}


@dataclass
class MediaFilter:
    trash: bool = False
    kind: str | None = None
    favorite: bool | None = None
    album_id: int | None = None
    # True: only items in at least one album; False: only items in no album.
    in_album: bool | None = None
    tag: str | None = None
    person_id: int | None = None
    country: str | None = None
    city: str | None = None
    q: str | None = None
    date_from: str | None = None  # inclusive, ISO date/datetime
    date_to: str | None = None  # exclusive

    def where(self) -> tuple[str, list[Any]]:
        clauses = ["m.deleted_at IS NOT NULL" if self.trash else "m.deleted_at IS NULL"]
        params: list[Any] = []
        if self.kind:
            clauses.append("m.kind = ?")
            params.append(self.kind)
        if self.favorite is not None:
            clauses.append("m.favorite = ?")
            params.append(int(self.favorite))
        if self.album_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM album_media am WHERE am.media_id = m.id AND am.album_id = ?)"
            )
            params.append(self.album_id)
        if self.in_album is not None:
            clauses.append(
                ("" if self.in_album else "NOT ")
                + "EXISTS (SELECT 1 FROM album_media am WHERE am.media_id = m.id)"
            )
        if self.tag:
            clauses.append(
                "EXISTS (SELECT 1 FROM media_tags mt JOIN tags t ON t.id = mt.tag_id "
                "WHERE mt.media_id = m.id AND t.name = ?)"
            )
            params.append(self.tag)
        if self.person_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM media_people mp WHERE mp.media_id = m.id "
                "AND mp.person_id = ?)"
            )
            params.append(self.person_id)
        if self.country:
            clauses.append("m.country = ? COLLATE NOCASE")
            params.append(self.country)
        if self.city:
            clauses.append("m.city = ? COLLATE NOCASE")
            params.append(self.city)
        if self.q:
            for word in self.q.split():
                like = f"%{_escape_like(word)}%"
                clauses.append(
                    "(m.original_name LIKE ? ESCAPE '\\' OR m.description LIKE ? ESCAPE '\\' "
                    "OR m.camera_make LIKE ? ESCAPE '\\' OR m.camera_model LIKE ? ESCAPE '\\' "
                    "OR m.taken_at LIKE ? ESCAPE '\\' OR m.city LIKE ? ESCAPE '\\' "
                    "OR m.region LIKE ? ESCAPE '\\' OR m.country LIKE ? ESCAPE '\\' "
                    "OR EXISTS (SELECT 1 FROM media_tags mt JOIN tags t ON t.id = mt.tag_id "
                    "WHERE mt.media_id = m.id AND t.name LIKE ? ESCAPE '\\') "
                    "OR EXISTS (SELECT 1 FROM media_people mp JOIN people p ON p.id = mp.person_id "
                    "WHERE mp.media_id = m.id AND p.name LIKE ? ESCAPE '\\'))"
                )
                params.extend([like] * 10)
        if self.date_from:
            clauses.append("m.taken_at >= ?")
            params.append(self.date_from)
        if self.date_to:
            clauses.append("m.taken_at < ?")
            params.append(self.date_to)
        return " AND ".join(clauses), params


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def encode_cursor(item: dict[str, Any]) -> str:
    return f"{item['taken_at']}|{item['id']}"


def decode_cursor(cursor: str) -> tuple[str, int]:
    taken_at, _, media_id = cursor.rpartition("|")
    if not taken_at:
        raise ValueError("bad cursor")
    return taken_at, int(media_id)


def list_media(
    conn: sqlite3.Connection, flt: MediaFilter, *, cursor: str | None = None, limit: int = 200
) -> tuple[list[dict[str, Any]], str | None]:
    """Newest first, keyset-paginated on ``(taken_at, id)``."""
    where, params = flt.where()
    if cursor:
        taken_at, media_id = decode_cursor(cursor)
        where += " AND (m.taken_at, m.id) < (?, ?)"
        params += [taken_at, media_id]
    cols = _M_COLUMNS
    rows = conn.execute(
        f"SELECT {cols} FROM media m WHERE {where} ORDER BY m.taken_at DESC, m.id DESC LIMIT ?",
        [*params, limit + 1],
    ).fetchall()
    items = [_row(r) for r in rows[:limit]]
    next_cursor = encode_cursor(items[-1]) if len(rows) > limit else None
    return items, next_cursor


def timeline(conn: sqlite3.Connection, flt: MediaFilter) -> list[dict[str, Any]]:
    where, params = flt.where()
    rows = conn.execute(
        f"SELECT substr(m.taken_at, 1, 7) AS month, COUNT(*) AS count FROM media m "
        f"WHERE {where} GROUP BY month ORDER BY month DESC",
        params,
    )
    return [dict(r) for r in rows]


def update_media(conn: sqlite3.Connection, media_id: int, **fields: Any) -> bool:
    allowed = {"favorite", "description", "taken_at", "taken_at_source"}
    fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not fields:
        return conn.execute("SELECT 1 FROM media WHERE id = ?", (media_id,)).fetchone() is not None
    sets = ", ".join(f"{k} = ?" for k in fields)
    cur = conn.execute(f"UPDATE media SET {sets} WHERE id = ?", [*fields.values(), media_id])
    return cur.rowcount > 0


def _in(ids: Sequence[int]) -> str:
    return ", ".join("?" * len(ids))


def set_favorite(conn: sqlite3.Connection, ids: Sequence[int], value: bool) -> int:
    if not ids:
        return 0
    sql = f"UPDATE media SET favorite = ? WHERE id IN ({_in(ids)})"
    return conn.execute(sql, [int(value), *ids]).rowcount


def trash(conn: sqlite3.Connection, ids: Sequence[int]) -> int:
    if not ids:
        return 0
    sql = f"UPDATE media SET deleted_at = ? WHERE deleted_at IS NULL AND id IN ({_in(ids)})"
    return conn.execute(sql, [now_iso(), *ids]).rowcount


def restore(conn: sqlite3.Connection, ids: Sequence[int]) -> int:
    if not ids:
        return 0
    sql = f"UPDATE media SET deleted_at = NULL WHERE id IN ({_in(ids)})"
    return conn.execute(sql, list(ids)).rowcount


def trashed(conn: sqlite3.Connection, ids: Sequence[int] | None = None) -> list[dict[str, Any]]:
    sql = f"SELECT {MEDIA_COLUMNS} FROM media WHERE deleted_at IS NOT NULL"
    params: list[Any] = []
    if ids is not None:
        if not ids:
            return []
        sql += f" AND id IN ({_in(ids)})"
        params = list(ids)
    return [_row(r) for r in conn.execute(sql, params)]


def delete_rows(conn: sqlite3.Connection, ids: Sequence[int]) -> None:
    if ids:
        conn.execute(f"DELETE FROM media WHERE id IN ({_in(ids)})", list(ids))
        _prune_tags(conn)


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT "
        " COALESCE(SUM(kind = 'image' AND deleted_at IS NULL), 0) AS photos,"
        " COALESCE(SUM(kind = 'video' AND deleted_at IS NULL), 0) AS videos,"
        " COALESCE(SUM(favorite AND deleted_at IS NULL), 0) AS favorites,"
        " COALESCE(SUM(deleted_at IS NOT NULL), 0) AS trash,"
        " COALESCE(SUM(size), 0) AS bytes"
        " FROM media"
    ).fetchone()
    out = dict(row)
    out["albums"] = conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
    return out


# --------------------------------------------------------------------------- tags


def normalize_tag(name: str) -> str:
    return " ".join(name.strip().split())[:64]


def media_tags(conn: sqlite3.Connection, media_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT t.name FROM tags t JOIN media_tags mt ON mt.tag_id = t.id "
        "WHERE mt.media_id = ? ORDER BY t.name COLLATE NOCASE",
        (media_id,),
    )
    return [r[0] for r in rows]


def _tag_id(conn: sqlite3.Connection, name: str) -> int:
    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
    return conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]


def _prune_tags(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM media_tags)")


def set_tags(conn: sqlite3.Connection, media_id: int, names: Iterable[str]) -> list[str]:
    clean = {normalize_tag(n).casefold(): normalize_tag(n) for n in names if normalize_tag(n)}
    with transaction(conn):
        conn.execute("DELETE FROM media_tags WHERE media_id = ?", (media_id,))
        for name in clean.values():
            conn.execute(
                "INSERT OR IGNORE INTO media_tags (media_id, tag_id) VALUES (?, ?)",
                (media_id, _tag_id(conn, name)),
            )
        _prune_tags(conn)
    return media_tags(conn, media_id)


def add_tag(conn: sqlite3.Connection, ids: Sequence[int], name: str) -> int:
    name = normalize_tag(name)
    if not name or not ids:
        return 0
    with transaction(conn):
        tag_id = _tag_id(conn, name)
        conn.executemany(
            "INSERT OR IGNORE INTO media_tags (media_id, tag_id) VALUES (?, ?)",
            [(i, tag_id) for i in ids],
        )
    return len(ids)


def remove_tag(conn: sqlite3.Connection, ids: Sequence[int], name: str) -> int:
    if not ids:
        return 0
    with transaction(conn):
        cur = conn.execute(
            f"DELETE FROM media_tags WHERE media_id IN ({_in(ids)}) "
            "AND tag_id = (SELECT id FROM tags WHERE name = ?)",
            [*ids, normalize_tag(name)],
        )
        _prune_tags(conn)
    return cur.rowcount


def list_tags(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT t.name, COUNT(m.id) AS count FROM tags t "
        "JOIN media_tags mt ON mt.tag_id = t.id "
        "JOIN media m ON m.id = mt.media_id AND m.deleted_at IS NULL "
        "GROUP BY t.id ORDER BY t.name COLLATE NOCASE"
    )
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- places


def normalize_place(text: str | None) -> str:
    return " ".join((text or "").split())[:100]


def set_place(
    conn: sqlite3.Connection,
    ids: Sequence[int],
    place: dict[str, str] | None,
    source: str | None,
) -> int:
    """Store a place (``{"city", "region", "country"}``, or None for none) on items."""
    if not ids:
        return 0
    p = {k: normalize_place((place or {}).get(k)) or None for k in ("city", "region", "country")}
    return conn.execute(
        f"UPDATE media SET city = ?, region = ?, country = ?, place_source = ? "
        f"WHERE id IN ({_in(ids)})",
        [p["city"], p["region"], p["country"], source, *ids],
    ).rowcount


def pending_places(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    """Items with GPS coordinates whose place hasn't been looked up yet."""
    rows = conn.execute(
        "SELECT id, latitude, longitude FROM media "
        "WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND place_source IS NULL "
        "LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


def list_places(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Countries with their cities and how many items each has, most items first."""
    rows = conn.execute(
        "SELECT country, city, MIN(region) AS region, COUNT(*) AS count FROM media "
        "WHERE deleted_at IS NULL AND country IS NOT NULL "
        "GROUP BY country COLLATE NOCASE, city COLLATE NOCASE "
        "ORDER BY country COLLATE NOCASE, count DESC, city COLLATE NOCASE"
    )
    countries: dict[str, dict[str, Any]] = {}
    for r in rows:
        c = countries.setdefault(
            r["country"].casefold(), {"country": r["country"], "count": 0, "cities": []}
        )
        c["count"] += r["count"]
        if r["city"]:
            c["cities"].append({"city": r["city"], "region": r["region"], "count": r["count"]})
    return sorted(countries.values(), key=lambda c: (-c["count"], c["country"].casefold()))


# --------------------------------------------------------------------------- people & pets

PERSON_KINDS = ("person", "animal")

_PERSON_SELECT = """
SELECT p.id, p.name, p.kind, p.created_at, p.cover_media_id,
       (SELECT COUNT(*) FROM media_people mp JOIN media m ON m.id = mp.media_id
         WHERE mp.person_id = p.id AND m.deleted_at IS NULL) AS count,
       COALESCE(
         (SELECT m.id FROM media m JOIN media_people mp ON mp.media_id = m.id
           WHERE m.id = p.cover_media_id AND mp.person_id = p.id AND m.deleted_at IS NULL),
         (SELECT m.id FROM media_people mp JOIN media m ON m.id = mp.media_id
           WHERE mp.person_id = p.id AND m.deleted_at IS NULL
           ORDER BY m.taken_at DESC, m.id DESC LIMIT 1)
       ) AS cover_id
FROM people p
"""


def normalize_name(name: str) -> str:
    return " ".join(name.strip().split())[:100]


def list_people(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(_PERSON_SELECT + " ORDER BY p.name COLLATE NOCASE")]


def get_person(conn: sqlite3.Connection, person_id: int) -> dict[str, Any] | None:
    row = conn.execute(_PERSON_SELECT + " WHERE p.id = ?", (person_id,)).fetchone()
    return dict(row) if row else None


def find_person(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute(
        _PERSON_SELECT + " WHERE p.name = ? COLLATE NOCASE", (normalize_name(name),)
    ).fetchone()
    return dict(row) if row else None


def create_person(conn: sqlite3.Connection, name: str, kind: str = "person") -> int:
    """Raises ``sqlite3.IntegrityError`` when the name is taken (names ignore case)."""
    return conn.execute(
        "INSERT INTO people (name, kind, created_at) VALUES (?, ?, ?)",
        (normalize_name(name), kind, now_iso()),
    ).lastrowid


def update_person(conn: sqlite3.Connection, person_id: int, **fields: Any) -> bool:
    allowed = {"name", "kind", "cover_media_id"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if "name" in fields:
        fields["name"] = normalize_name(fields["name"])
    if not fields:
        return get_person(conn, person_id) is not None
    sets = ", ".join(f"{k} = ?" for k in fields)
    cur = conn.execute(f"UPDATE people SET {sets} WHERE id = ?", [*fields.values(), person_id])
    return cur.rowcount > 0


def delete_person(conn: sqlite3.Connection, person_id: int) -> bool:
    """Forget a person or pet; their photos and videos stay."""
    return conn.execute("DELETE FROM people WHERE id = ?", (person_id,)).rowcount > 0


def media_people(conn: sqlite3.Connection, media_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT p.id, p.name, p.kind FROM people p JOIN media_people mp ON mp.person_id = p.id "
        "WHERE mp.media_id = ? ORDER BY p.name COLLATE NOCASE",
        (media_id,),
    )
    return [dict(r) for r in rows]


def add_person(conn: sqlite3.Connection, ids: Sequence[int], person_id: int) -> int:
    """Mark that someone is in these items; returns how many weren't marked yet."""
    if not ids:
        return 0
    with transaction(conn):
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO media_people (media_id, person_id) "
            "SELECT id, ? FROM media WHERE id = ?",
            [(person_id, i) for i in ids],
        )
        return conn.total_changes - before


def remove_person(conn: sqlite3.Connection, ids: Sequence[int], person_id: int) -> int:
    if not ids:
        return 0
    return conn.execute(
        f"DELETE FROM media_people WHERE person_id = ? AND media_id IN ({_in(ids)})",
        [person_id, *ids],
    ).rowcount


# --------------------------------------------------------------------------- albums

_ALBUM_SELECT = """
SELECT a.id, a.name, a.description, a.created_at, a.updated_at, a.cover_media_id,
       (SELECT COUNT(*) FROM album_media am JOIN media m ON m.id = am.media_id
         WHERE am.album_id = a.id AND m.deleted_at IS NULL) AS count,
       COALESCE(
         (SELECT m.id FROM media m WHERE m.id = a.cover_media_id AND m.deleted_at IS NULL),
         (SELECT m.id FROM album_media am JOIN media m ON m.id = am.media_id
           WHERE am.album_id = a.id AND m.deleted_at IS NULL
           ORDER BY m.taken_at DESC, m.id DESC LIMIT 1)
       ) AS cover_id,
       (SELECT MIN(m.taken_at) FROM album_media am JOIN media m ON m.id = am.media_id
         WHERE am.album_id = a.id AND m.deleted_at IS NULL) AS first_taken_at,
       (SELECT MAX(m.taken_at) FROM album_media am JOIN media m ON m.id = am.media_id
         WHERE am.album_id = a.id AND m.deleted_at IS NULL) AS last_taken_at
FROM albums a
"""


def list_albums(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(_ALBUM_SELECT + " ORDER BY a.updated_at DESC, a.id DESC")
    return [dict(r) for r in rows]


def get_album(conn: sqlite3.Connection, album_id: int) -> dict[str, Any] | None:
    row = conn.execute(_ALBUM_SELECT + " WHERE a.id = ?", (album_id,)).fetchone()
    return dict(row) if row else None


def create_album(conn: sqlite3.Connection, name: str, description: str = "") -> int:
    ts = now_iso()
    return conn.execute(
        "INSERT INTO albums (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name, description, ts, ts),
    ).lastrowid


def update_album(conn: sqlite3.Connection, album_id: int, **fields: Any) -> bool:
    allowed = {"name", "description", "cover_media_id"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    sets = ", ".join(f"{k} = ?" for k in [*fields, "updated_at"])
    cur = conn.execute(
        f"UPDATE albums SET {sets} WHERE id = ?", [*fields.values(), now_iso(), album_id]
    )
    return cur.rowcount > 0


def delete_album(conn: sqlite3.Connection, album_id: int) -> bool:
    return conn.execute("DELETE FROM albums WHERE id = ?", (album_id,)).rowcount > 0


def add_to_album(conn: sqlite3.Connection, album_id: int, ids: Sequence[int]) -> int:
    ts = now_iso()
    with transaction(conn):
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO album_media (album_id, media_id, added_at) "
            "SELECT ?, id, ? FROM media WHERE id = ?",
            [(album_id, ts, i) for i in ids],
        )
        added = conn.total_changes - before
        conn.execute("UPDATE albums SET updated_at = ? WHERE id = ?", (ts, album_id))
    return added


def remove_from_album(conn: sqlite3.Connection, album_id: int, ids: Sequence[int]) -> int:
    if not ids:
        return 0
    with transaction(conn):
        cur = conn.execute(
            f"DELETE FROM album_media WHERE album_id = ? AND media_id IN ({_in(ids)})",
            [album_id, *ids],
        )
        conn.execute("UPDATE albums SET updated_at = ? WHERE id = ?", (now_iso(), album_id))
    return cur.rowcount


# --------------------------------------------------------------------------- import history

IMPORT_COUNTERS = ("total", "imported", "duplicates", "skipped", "failed")
_RESULT_COUNTER = {"imported": "imported", "duplicate": "duplicates", "skipped": "skipped"}


def create_import(conn: sqlite3.Connection, kind: str, path: str, *, recursive: bool = True) -> int:
    return conn.execute(
        "INSERT INTO imports (kind, path, recursive, status, started_at) VALUES (?, ?, ?, ?, ?)",
        (kind, path, int(recursive), "running", now_iso()),
    ).lastrowid


def update_import(conn: sqlite3.Connection, import_id: int, **fields: Any) -> None:
    allowed = {*IMPORT_COUNTERS, "status", "finished_at"}
    if not fields or set(fields) - allowed:
        raise ValueError(f"cannot update {sorted(set(fields) - allowed)}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE imports SET {sets} WHERE id = ?", [*fields.values(), import_id])


def count_import_result(conn: sqlite3.Connection, import_id: int, status: str) -> None:
    """Add one processed file to an import's counters (browser uploads)."""
    column = _RESULT_COUNTER.get(status, "failed")
    conn.execute(
        f"UPDATE imports SET total = total + 1, {column} = {column} + 1 WHERE id = ?",
        (import_id,),
    )


def add_import_duplicate(
    conn: sqlite3.Connection, import_id: int, source: str, media_id: int
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO import_duplicates (import_id, source, media_id) VALUES (?, ?, ?)",
        (import_id, source, media_id),
    )


def get_import(conn: sqlite3.Connection, import_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
    return _import_row(row) if row else None


def list_imports(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM imports ORDER BY started_at DESC, id DESC LIMIT ?", (limit,))
    return [_import_row(r) for r in rows]


def _import_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["recursive"] = bool(d["recursive"])
    return d


def import_duplicates(
    conn: sqlite3.Connection, import_id: int, *, offset: int = 0, limit: int = 200
) -> tuple[int, list[dict[str, Any]]]:
    """Files an import skipped as duplicates, each with the library item it matched.

    Rows whose library item was later deleted for good disappear (ON DELETE CASCADE).
    """
    total = conn.execute(
        "SELECT COUNT(*) FROM import_duplicates WHERE import_id = ?", (import_id,)
    ).fetchone()[0]
    cols = _M_COLUMNS
    rows = conn.execute(
        f"SELECT d.source, {cols} FROM import_duplicates d JOIN media m ON m.id = d.media_id "
        "WHERE d.import_id = ? ORDER BY d.source LIMIT ? OFFSET ?",
        (import_id, limit, offset),
    )
    out = []
    for r in rows:
        media = _row(r)
        assert media is not None
        out.append({"source": media.pop("source"), "media": media})
    return total, out


def interrupt_unfinished_imports(conn: sqlite3.Connection) -> int:
    """Close out runs a previous server process never finished (crash, Ctrl+C, closed tab)."""
    return conn.execute(
        "UPDATE imports SET status = 'interrupted', finished_at = ? WHERE finished_at IS NULL",
        (now_iso(),),
    ).rowcount


# --------------------------------------------------------------------------- editing


def copy_media_details(
    conn: sqlite3.Connection, src_id: int, dst_id: int, *, take_over_covers: bool = False
) -> None:
    """Give an edited copy the original's date, description, favorite, place, tags,
    people and albums."""
    copied = (
        "taken_at, taken_at_source, description, favorite, camera_make, camera_model, "
        "latitude, longitude, city, region, country, place_source"
    )
    with transaction(conn):
        conn.execute(
            f"UPDATE media SET ({copied}) = (SELECT {copied} FROM media WHERE id = ?) WHERE id = ?",
            (src_id, dst_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO media_tags (media_id, tag_id) "
            "SELECT ?, tag_id FROM media_tags WHERE media_id = ?",
            (dst_id, src_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO media_people (media_id, person_id) "
            "SELECT ?, person_id FROM media_people WHERE media_id = ?",
            (dst_id, src_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO album_media (album_id, media_id, added_at) "
            "SELECT album_id, ?, ? FROM album_media WHERE media_id = ?",
            (dst_id, now_iso(), src_id),
        )
        if take_over_covers:
            conn.execute(
                "UPDATE albums SET cover_media_id = ? WHERE cover_media_id = ?", (dst_id, src_id)
            )
            conn.execute(
                "UPDATE people SET cover_media_id = ? WHERE cover_media_id = ?", (dst_id, src_id)
            )


# --------------------------------------------------------------------------- analysis

_PENDING_ANALYSIS = (
    "FROM media m LEFT JOIN media_analysis a ON a.media_id = m.id "
    "WHERE m.kind = 'image' AND m.deleted_at IS NULL AND m.size > 0 "
    "AND (a.media_id IS NULL OR a.sha256 != m.sha256 OR a.version != ?)"
)


def pending_analysis(conn: sqlite3.Connection, version: int, limit: int) -> list[dict[str, Any]]:
    cols = _M_COLUMNS
    rows = conn.execute(
        f"SELECT {cols} {_PENDING_ANALYSIS} ORDER BY m.taken_at DESC, m.id DESC LIMIT ?",
        (version, limit),
    )
    return [_row(r) for r in rows]  # type: ignore[misc]


def count_pending_analysis(conn: sqlite3.Connection, version: int) -> int:
    return conn.execute(f"SELECT COUNT(*) {_PENDING_ANALYSIS}", (version,)).fetchone()[0]


def save_analysis(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    version: int,
    *,
    crop: Sequence[float] | None = None,
    mean: float | None = None,
    stddev: float | None = None,
    error: str | None = None,
) -> None:
    left, top, right, bottom = crop if crop else (None, None, None, None)
    conn.execute(
        "INSERT OR REPLACE INTO media_analysis (media_id, sha256, version, crop_left, crop_top, "
        "crop_right, crop_bottom, mean, stddev, error, analyzed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item["id"], item["sha256"], version, left, top, right, bottom, mean, stddev, error,
         now_iso()),
    )  # fmt: skip


def get_analysis(conn: sqlite3.Connection, item: dict[str, Any], version: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM media_analysis WHERE media_id = ? AND sha256 = ? AND version = ?",
        (item["id"], item["sha256"], version),
    ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- cleanup

# id -> (label, description, extra join?, where, order). ``:small`` and ``:version``
# are bound per request. Trashed items never show up.
_VALID_ANALYSIS = (
    "JOIN media_analysis a ON a.media_id = m.id AND a.sha256 = m.sha256 AND a.version = :version"
)
CLEANUP: dict[str, tuple[str, str, str, str, str]] = {
    "croppable": (
        "Black borders",
        "Screenshots and photos with a black background around them. "
        "Crop it away to keep just the picture.",
        _VALID_ANALYSIS,
        "a.crop_left IS NOT NULL",
        "(a.crop_right - a.crop_left) * (a.crop_bottom - a.crop_top) ASC",
    ),
    "blank": (
        "Blank or black shots",
        "Almost a single colour - often taken by accident in a pocket or with the lens covered.",
        _VALID_ANALYSIS,
        "a.error IS NULL AND a.stddev < :blank",
        "a.stddev ASC",
    ),
    "broken": (
        "Can't be opened",
        "Files Phobos couldn't read. They may be damaged or only partly copied.",
        _VALID_ANALYSIS,
        "a.error IS NOT NULL",
        "m.taken_at DESC",
    ),
    "empty": (
        "Empty files",
        "Files with nothing in them (0 bytes).",
        "",
        "m.size = 0",
        "m.taken_at DESC",
    ),
    "small": (
        "Very small files",
        "Tiny files are usually thumbnails, stickers or low-quality copies.",
        "",
        "m.size > 0 AND m.size < :small",
        "m.size ASC",
    ),
    "lowres": (
        "Low resolution",
        "Photos under 0.3 megapixels, like images saved from chats or old phones.",
        "",
        "m.kind = 'image' AND m.width * m.height < 300000",
        "m.width * m.height ASC",
    ),
    "short": (
        "Very short videos",
        "Videos under 2 seconds, usually started by accident.",
        "",
        "m.kind = 'video' AND m.duration < 2",
        "m.duration ASC",
    ),
    "largest": (
        "Largest files",
        "The 50 files taking the most space. Worth a look when the disk fills up.",
        "",
        "1",
        "m.size DESC",
    ),
}
LARGEST_LIMIT = 50


def _cleanup_sql(category: str) -> tuple[str, str]:
    _, _, join, where, order = CLEANUP[category]
    return f"FROM media m {join} WHERE m.deleted_at IS NULL AND {where}", order


def cleanup_count(conn: sqlite3.Connection, category: str, params: dict[str, Any]) -> int:
    base, _ = _cleanup_sql(category)
    n = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
    return min(n, LARGEST_LIMIT) if category == "largest" else n


def cleanup_items(
    conn: sqlite3.Connection, category: str, params: dict[str, Any], limit: int
) -> list[dict[str, Any]]:
    base, order = _cleanup_sql(category)
    if category == "largest":
        limit = min(limit, LARGEST_LIMIT)
    cols = _M_COLUMNS
    extra = ", a.crop_left, a.crop_top, a.crop_right, a.crop_bottom, a.mean, a.stddev, a.error"
    if not CLEANUP[category][2]:
        extra = ""
    rows = conn.execute(
        f"SELECT {cols}{extra} {base} ORDER BY {order}, m.id DESC LIMIT :limit",
        {**params, "limit": limit},
    )
    return [_row(r) for r in rows]  # type: ignore[misc]


# --------------------------------------------------------------------------- metrics


def metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    live = "deleted_at IS NULL"
    totals = dict(
        conn.execute(
            "SELECT"
            f" COALESCE(SUM(kind = 'image' AND {live}), 0) AS photos,"
            f" COALESCE(SUM(kind = 'video' AND {live}), 0) AS videos,"
            f" COALESCE(SUM(CASE WHEN kind = 'image' AND {live} THEN size END), 0) AS photo_bytes,"
            f" COALESCE(SUM(CASE WHEN kind = 'video' AND {live} THEN size END), 0) AS video_bytes,"
            f" COALESCE(SUM(favorite AND {live}), 0) AS favorites,"
            f" COALESCE(SUM(latitude IS NOT NULL AND {live}), 0) AS with_location,"
            f" COALESCE(SUM(country IS NOT NULL AND {live}), 0) AS with_place,"
            f" COALESCE(SUM(description != '' AND {live}), 0) AS described,"
            " COALESCE(SUM(deleted_at IS NOT NULL), 0) AS trash,"
            " COALESCE(SUM(CASE WHEN deleted_at IS NOT NULL THEN size END), 0) AS trash_bytes,"
            f" MIN(CASE WHEN {live} THEN taken_at END) AS oldest,"
            f" MAX(CASE WHEN {live} THEN taken_at END) AS newest"
            " FROM media"
        ).fetchone()
    )
    totals["in_albums"] = conn.execute(
        "SELECT COUNT(DISTINCT am.media_id) FROM album_media am "
        "JOIN media m ON m.id = am.media_id WHERE m.deleted_at IS NULL"
    ).fetchone()[0]
    totals["albums"] = conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
    totals["tags"] = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    totals["people"] = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    totals["with_people"] = conn.execute(
        "SELECT COUNT(DISTINCT mp.media_id) FROM media_people mp "
        "JOIN media m ON m.id = mp.media_id WHERE m.deleted_at IS NULL"
    ).fetchone()[0]
    totals["tagged"] = conn.execute(
        "SELECT COUNT(DISTINCT mt.media_id) FROM media_tags mt "
        "JOIN media m ON m.id = mt.media_id WHERE m.deleted_at IS NULL"
    ).fetchone()[0]

    def rows(sql: str) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(sql)]

    return {
        "totals": totals,
        "by_year": rows(
            "SELECT substr(taken_at, 1, 4) AS year, SUM(kind = 'image') AS photos, "
            f"SUM(kind = 'video') AS videos, SUM(size) AS bytes FROM media WHERE {live} "
            "GROUP BY year ORDER BY year"
        ),
        "by_type": rows(
            f"SELECT mime, kind, COUNT(*) AS count, SUM(size) AS bytes FROM media WHERE {live} "
            "GROUP BY mime ORDER BY count DESC"
        ),
        "cameras": rows(
            "SELECT TRIM(COALESCE(camera_make, '') || ' ' || COALESCE(camera_model, '')) "
            "AS camera, "
            f"COUNT(*) AS count FROM media WHERE {live} AND camera_model IS NOT NULL "
            "GROUP BY camera ORDER BY count DESC LIMIT 8"
        ),
        "countries": rows(
            f"SELECT MIN(country) AS country, COUNT(*) AS count FROM media WHERE {live} "
            "AND country IS NOT NULL GROUP BY country COLLATE NOCASE ORDER BY count DESC LIMIT 8"
        ),
        "date_sources": rows(
            f"SELECT taken_at_source AS source, COUNT(*) AS count FROM media WHERE {live} "
            "GROUP BY source ORDER BY count DESC"
        ),
    }
