"""SQLite storage.

SQLite (stdlib ``sqlite3``) fits a single-user local app: zero setup, one file
to back up, and fast enough for hundreds of thousands of items. WAL mode lets
the import job write while the web UI reads.

Schema changes are applied as numbered migrations tracked in ``PRAGMA user_version``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1 - initial schema
    """
    CREATE TABLE media (
        id              INTEGER PRIMARY KEY,
        sha256          TEXT NOT NULL UNIQUE,
        kind            TEXT NOT NULL CHECK (kind IN ('image', 'video')),
        rel_path        TEXT NOT NULL UNIQUE,
        original_name   TEXT NOT NULL,
        mime            TEXT NOT NULL,
        size            INTEGER NOT NULL,
        width           INTEGER,
        height          INTEGER,
        duration        REAL,
        taken_at        TEXT NOT NULL,
        taken_at_source TEXT NOT NULL,
        camera_make     TEXT,
        camera_model    TEXT,
        latitude        REAL,
        longitude       REAL,
        description     TEXT NOT NULL DEFAULT '',
        favorite        INTEGER NOT NULL DEFAULT 0,
        imported_at     TEXT NOT NULL,
        deleted_at      TEXT
    );
    CREATE INDEX media_timeline ON media (taken_at DESC, id DESC) WHERE deleted_at IS NULL;
    CREATE INDEX media_trash ON media (deleted_at) WHERE deleted_at IS NOT NULL;

    CREATE TABLE albums (
        id             INTEGER PRIMARY KEY,
        name           TEXT NOT NULL,
        description    TEXT NOT NULL DEFAULT '',
        cover_media_id INTEGER REFERENCES media (id) ON DELETE SET NULL,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );
    CREATE TABLE album_media (
        album_id INTEGER NOT NULL REFERENCES albums (id) ON DELETE CASCADE,
        media_id INTEGER NOT NULL REFERENCES media (id) ON DELETE CASCADE,
        added_at TEXT NOT NULL,
        PRIMARY KEY (album_id, media_id)
    );
    CREATE INDEX album_media_media ON album_media (media_id);

    CREATE TABLE tags (
        id   INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE
    );
    CREATE TABLE media_tags (
        media_id INTEGER NOT NULL REFERENCES media (id) ON DELETE CASCADE,
        tag_id   INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
        PRIMARY KEY (media_id, tag_id)
    );
    CREATE INDEX media_tags_tag ON media_tags (tag_id);
    """,
    # 2 - import history (which folder was imported when) and the duplicates each run found
    """
    CREATE TABLE imports (
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL CHECK (kind IN ('import', 'rescan', 'upload')),
        path        TEXT NOT NULL,
        recursive   INTEGER NOT NULL DEFAULT 1,
        status      TEXT NOT NULL,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        total       INTEGER NOT NULL DEFAULT 0,
        imported    INTEGER NOT NULL DEFAULT 0,
        duplicates  INTEGER NOT NULL DEFAULT 0,
        skipped     INTEGER NOT NULL DEFAULT 0,
        failed      INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX imports_started ON imports (started_at DESC, id DESC);

    CREATE TABLE import_duplicates (
        import_id INTEGER NOT NULL REFERENCES imports (id) ON DELETE CASCADE,
        source    TEXT NOT NULL,
        media_id  INTEGER NOT NULL REFERENCES media (id) ON DELETE CASCADE,
        PRIMARY KEY (import_id, source)
    );
    CREATE INDEX import_duplicates_media ON import_duplicates (media_id);
    """,
    # 3 - cached image analysis for the cleanup tool (black borders, blank shots)
    """
    CREATE TABLE media_analysis (
        media_id    INTEGER PRIMARY KEY REFERENCES media (id) ON DELETE CASCADE,
        sha256      TEXT NOT NULL,
        version     INTEGER NOT NULL,
        crop_left   REAL,
        crop_top    REAL,
        crop_right  REAL,
        crop_bottom REAL,
        mean        REAL,
        stddev      REAL,
        error       TEXT,
        analyzed_at TEXT NOT NULL
    );
    """,
    # 4 - place names (from GPS via the offline gazetteer, or typed in) and people & pets
    """
    ALTER TABLE media ADD COLUMN city TEXT;
    ALTER TABLE media ADD COLUMN region TEXT;
    ALTER TABLE media ADD COLUMN country TEXT;
    -- gps: looked up from the coordinates (all three NULL when nowhere near a town)
    -- manual: typed in by the user, never overwritten by a lookup, NULL: not looked up yet
    ALTER TABLE media ADD COLUMN place_source TEXT;
    CREATE INDEX media_place ON media (country, city) WHERE deleted_at IS NULL;

    CREATE TABLE people (
        id             INTEGER PRIMARY KEY,
        name           TEXT NOT NULL UNIQUE COLLATE NOCASE,
        kind           TEXT NOT NULL DEFAULT 'person' CHECK (kind IN ('person', 'animal')),
        cover_media_id INTEGER REFERENCES media (id) ON DELETE SET NULL,
        created_at     TEXT NOT NULL
    );
    CREATE TABLE media_people (
        media_id  INTEGER NOT NULL REFERENCES media (id) ON DELETE CASCADE,
        person_id INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
        PRIMARY KEY (media_id, person_id)
    );
    CREATE INDEX media_people_person ON media_people (person_id);
    """,
    # 5 - a built-in "You" that can't be removed, so the user can tag themselves.
    # Someone already called "You" becomes that person (keeping their photos).
    """
    ALTER TABLE people ADD COLUMN is_self INTEGER NOT NULL DEFAULT 0;
    UPDATE people SET is_self = 1, name = 'You', kind = 'person' WHERE name = 'You' COLLATE NOCASE;
    INSERT INTO people (name, kind, is_self, created_at)
        SELECT 'You', 'person', 1, strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')
        WHERE NOT EXISTS (SELECT 1 FROM people WHERE is_self = 1);
    CREATE UNIQUE INDEX people_self ON people (is_self) WHERE is_self = 1;
    """,
]


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
        with transaction(conn):
            for statement in filter(str.strip, script.split(";")):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction (the connection runs in autocommit mode otherwise)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()


def backup(db_path: Path, dest: Path) -> None:
    """Consistent online copy of the database (safe while the app runs)."""
    src = connect(db_path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
