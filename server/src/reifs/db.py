"""SQLite schema and connection helpers."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2

# Version 1 schema; later versions are applied as MIGRATIONS.
SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Servers known to this server (including itself).
CREATE TABLE servers (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    address   TEXT,
    is_self   INTEGER NOT NULL DEFAULT 0,
    paired    INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT
);

-- Nodes. A server node has the same id as its server.
CREATE TABLE nodes (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL UNIQUE,
    kind               TEXT NOT NULL CHECK (kind IN ('server', 'storage')),
    owner_id           TEXT NOT NULL REFERENCES servers(id),
    root_path          TEXT,              -- last known root (owned nodes only)
    case_sensitive     INTEGER,
    restricted_names   INTEGER,
    priority           INTEGER NOT NULL DEFAULT 0,
    replication_target INTEGER NOT NULL DEFAULT 1,
    online             INTEGER NOT NULL DEFAULT 0,
    last_seen          TEXT,
    last_scan          TEXT,
    last_scan_id       INTEGER            -- last *completed* scan
);

-- Files: virtual paths with at least one tracked copy.
CREATE TABLE files (
    id      INTEGER PRIMARY KEY,
    vpath   TEXT NOT NULL UNIQUE,
    vkey    TEXT NOT NULL,                 -- casefolded vpath
    issues  TEXT NOT NULL DEFAULT '',      -- portability issue codes
    created TEXT NOT NULL
);
CREATE INDEX files_vkey ON files(vkey);

CREATE TABLE copies (
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    node_id       TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    size          INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    hash          TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'present' CHECK (state IN ('present', 'missing')),
    marked_delete INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL,
    first_scan    INTEGER NOT NULL,
    hashed_at     TEXT NOT NULL,
    PRIMARY KEY (file_id, node_id)
);
CREATE INDEX copies_node ON copies(node_id);
CREATE INDEX copies_hash ON copies(hash, size);

-- Tags belong to the vpath (file). Removed tags are kept as tombstones
-- (present = 0) so last-writer-wins merges work across servers.
CREATE TABLE tags (
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    tag        TEXT NOT NULL,
    present    INTEGER NOT NULL,
    updated    TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY (file_id, tag)
);
CREATE INDEX tags_tag ON tags(tag) WHERE present = 1;

-- The decision stack.
CREATE TABLE decisions (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    file_id    INTEGER REFERENCES files(id) ON DELETE CASCADE,
    node_id    TEXT REFERENCES nodes(id) ON DELETE CASCADE,
    data       TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL DEFAULT 'open'
               CHECK (status IN ('open', 'resolved', 'dismissed', 'obsolete')),
    resolution TEXT,
    created    TEXT NOT NULL,
    resolved   TEXT
);
CREATE INDEX decisions_status ON decisions(status, kind);
CREATE INDEX decisions_file ON decisions(file_id);

-- Proposed jobs waiting for approval (executed from milestone 2 on).
CREATE TABLE wanted (
    id       INTEGER PRIMARY KEY,
    op       TEXT NOT NULL CHECK (op IN ('copy', 'rename', 'overwrite')),
    file_id  INTEGER REFERENCES files(id) ON DELETE CASCADE,
    src_node TEXT REFERENCES nodes(id) ON DELETE CASCADE,
    dst_node TEXT REFERENCES nodes(id) ON DELETE CASCADE,
    data     TEXT NOT NULL DEFAULT '{}',
    reason   TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'open'
             CHECK (status IN ('open', 'approved', 'rejected', 'done')),
    created  TEXT NOT NULL
);

-- Append-only log of facts about owned nodes, replicated to peers.
CREATE TABLE changes (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id  TEXT NOT NULL,
    vpath    TEXT NOT NULL,
    op       TEXT NOT NULL CHECK (op IN ('upsert', 'missing', 'remove')),
    size     INTEGER,
    mtime_ns INTEGER,
    hash     TEXT,
    ts       TEXT NOT NULL
);

CREATE TABLE scans (
    id         INTEGER PRIMARY KEY,
    node_id    TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    started    TEXT NOT NULL,
    finished   TEXT,
    status     TEXT NOT NULL CHECK (status IN ('running', 'complete', 'aborted', 'failed')),
    stats      TEXT NOT NULL DEFAULT '{}',
    errors     TEXT NOT NULL DEFAULT '[]'
);

-- Nodes owned by another server, seen attached to this machine.
CREATE TABLE foreign_sightings (
    node_id  TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    path     TEXT NOT NULL,
    seen     TEXT NOT NULL
);
"""


MIGRATIONS: dict[int, str] = {
    2: """
-- Transfer jobs (milestone 2). Items refer to files by vpath so they survive
-- re-keying of files.
CREATE TABLE jobs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('copy', 'move', 'wanted')),
    description TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'waiting', 'done', 'failed', 'cancelled')),
    created     TEXT NOT NULL,
    started     TEXT,
    finished    TEXT
);

CREATE TABLE job_items (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    op          TEXT NOT NULL CHECK (op IN ('copy', 'overwrite', 'rename')),
    vpath       TEXT NOT NULL,
    new_vpath   TEXT,                       -- rename target
    src_node    TEXT REFERENCES nodes(id) ON DELETE SET NULL,  -- NULL: any current copy
    dst_node    TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    size        INTEGER NOT NULL DEFAULT 0,
    mark_source INTEGER NOT NULL DEFAULT 0, -- move: mark the source copy for deletion
    allow_hash  TEXT,                       -- destination content we may overwrite
    wanted_id   INTEGER REFERENCES wanted(id) ON DELETE SET NULL,
    state       TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'running', 'done', 'failed', 'skipped',
                                 'conflict', 'cancelled')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    message     TEXT,
    updated     TEXT NOT NULL
);
CREATE INDEX job_items_job ON job_items(job_id, state);
""",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def create(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    # executescript() manages its own transaction; keep schema + version atomic.
    conn.executescript(f"BEGIN;\n{SCHEMA}\nPRAGMA user_version = 1;\nCOMMIT;")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for target in range(version + 1, SCHEMA_VERSION + 1):
        conn.executescript(
            f"BEGIN;\n{MIGRATIONS[target]}\nPRAGMA user_version = {target};\nCOMMIT;"
        )


def open_existing(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if 1 <= version < SCHEMA_VERSION:
        migrate(conn)
    elif version != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"{path}: database schema version {version}, expected {SCHEMA_VERSION}"
        )
    return conn


class transaction:
    """``with transaction(conn):`` -- BEGIN IMMEDIATE / COMMIT / ROLLBACK.

    Nested use joins the outer transaction.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.outer = False

    def __enter__(self) -> sqlite3.Connection:
        if self.conn.in_transaction:
            self.outer = True
        else:
            self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.outer:
            return
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
