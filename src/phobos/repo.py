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
    "description, favorite, imported_at, deleted_at"
)


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
    tag: str | None = None
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
        if self.tag:
            clauses.append(
                "EXISTS (SELECT 1 FROM media_tags mt JOIN tags t ON t.id = mt.tag_id "
                "WHERE mt.media_id = m.id AND t.name = ?)"
            )
            params.append(self.tag)
        if self.q:
            for word in self.q.split():
                like = f"%{_escape_like(word)}%"
                clauses.append(
                    "(m.original_name LIKE ? ESCAPE '\\' OR m.description LIKE ? ESCAPE '\\' "
                    "OR m.camera_make LIKE ? ESCAPE '\\' OR m.camera_model LIKE ? ESCAPE '\\' "
                    "OR m.taken_at LIKE ? ESCAPE '\\' "
                    "OR EXISTS (SELECT 1 FROM media_tags mt JOIN tags t ON t.id = mt.tag_id "
                    "WHERE mt.media_id = m.id AND t.name LIKE ? ESCAPE '\\'))"
                )
                params.extend([like] * 6)
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
    cols = ", ".join(f"m.{c.strip()}" for c in MEDIA_COLUMNS.split(","))
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
    cols = ", ".join(f"m.{c.strip()}" for c in MEDIA_COLUMNS.split(","))
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
