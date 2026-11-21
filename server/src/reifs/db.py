"""SQLite schema and connection helpers."""

from __future__ import annotations

import itertools
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 7

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
    3: """
-- Pairing and peers (milestone 4).
ALTER TABLE servers ADD COLUMN fingerprint TEXT;      -- pinned certificate of the peer
ALTER TABLE servers ADD COLUMN token_out TEXT;        -- token we present to the peer
ALTER TABLE servers ADD COLUMN token_in_hash TEXT;    -- sha256 of the token the peer presents
ALTER TABLE servers ADD COLUMN rsync_port INTEGER;    -- the peer's rsync daemon
ALTER TABLE servers ADD COLUMN rsync_user TEXT;       -- our credentials on that daemon
ALTER TABLE servers ADD COLUMN rsync_password TEXT;
ALTER TABLE servers ADD COLUMN rsync_in_user TEXT;    -- the peer's credentials on ours
ALTER TABLE servers ADD COLUMN rsync_in_password TEXT;
ALTER TABLE servers ADD COLUMN pull_cursor INTEGER NOT NULL DEFAULT 0;
ALTER TABLE servers ADD COLUMN online INTEGER NOT NULL DEFAULT 0;
ALTER TABLE servers ADD COLUMN last_sync TEXT;
ALTER TABLE servers ADD COLUMN last_error TEXT;

ALTER TABLE nodes ADD COLUMN owner_since TEXT NOT NULL DEFAULT '';
UPDATE nodes SET owner_since = COALESCE(last_seen, '2000-01-01T00:00:00+00:00');

CREATE TABLE pair_requests (
    id          INTEGER PRIMARY KEY,
    direction   TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    server_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    address     TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    nonce       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'accepted', 'rejected', 'failed')),
    message     TEXT,
    created     TEXT NOT NULL
);

-- The change log becomes a replicated, typed log. Every entry carries its
-- origin server and a per-origin sequence number; entries learned from peers
-- are appended too, so they are forwarded to other peers.
CREATE TABLE changes_v3 (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_id  TEXT NOT NULL,
    origin_seq INTEGER NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('copy', 'node', 'tag', 'mark')),
    data       TEXT NOT NULL,
    ts         TEXT NOT NULL,
    UNIQUE (origin_id, origin_seq)
);
INSERT INTO changes_v3(seq, origin_id, origin_seq, kind, data, ts)
    SELECT seq, (SELECT value FROM meta WHERE key = 'server_id'), seq, 'copy',
           json_object('node_id', node_id, 'vpath', vpath, 'op', op, 'size', size,
                       'mtime_ns', mtime_ns, 'hash', hash),
           ts
    FROM changes;
DROP TABLE changes;
ALTER TABLE changes_v3 RENAME TO changes;

-- Highest origin_seq applied per origin server.
CREATE TABLE origin_progress (
    origin_id TEXT PRIMARY KEY,
    last_seq  INTEGER NOT NULL
);

-- Calls to peers that must eventually happen (delivered by the sync thread).
CREATE TABLE outbox (
    id         INTEGER PRIMARY KEY,
    server_id  TEXT NOT NULL,
    path       TEXT NOT NULL,
    body       TEXT NOT NULL,
    created    TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

-- Tags received for a vpath not (yet) in the index.
CREATE TABLE orphan_tags (
    vpath      TEXT NOT NULL,
    tag        TEXT NOT NULL,
    present    INTEGER NOT NULL,
    updated    TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY (vpath, tag)
);

-- Snapshot existing owned nodes and tags into the log so peers learn them.
INSERT INTO changes(origin_id, origin_seq, kind, data, ts)
    SELECT m.value,
           (SELECT IFNULL(MAX(origin_seq), 0) FROM changes) + ROW_NUMBER() OVER (ORDER BY n.id),
           'node',
           json_object('id', n.id, 'name', n.name, 'kind', n.kind, 'owner_id', n.owner_id,
                       'owner_name', (SELECT name FROM servers WHERE id = n.owner_id),
                       'owner_since', n.owner_since, 'case_sensitive', n.case_sensitive,
                       'restricted_names', n.restricted_names, 'priority', n.priority,
                       'replication_target', n.replication_target, 'online', n.online),
           strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
    FROM nodes n JOIN meta m ON m.key = 'server_id' AND n.owner_id = m.value;
INSERT INTO changes(origin_id, origin_seq, kind, data, ts)
    SELECT m.value,
           (SELECT IFNULL(MAX(origin_seq), 0) FROM changes) + ROW_NUMBER() OVER (ORDER BY t.file_id, t.tag),
           'tag',
           json_object('vpath', f.vpath, 'tag', t.tag, 'present', t.present,
                       'updated', t.updated, 'updated_by', t.updated_by),
           strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
    FROM tags t JOIN files f ON f.id = t.file_id JOIN meta m ON m.key = 'server_id';
""",
    4: """
-- Replication (milestone 5): nodes announce their free space so any server
-- can pick backup destinations; wanted jobs can carry a note (e.g. why
-- forwarding an approval failed).
ALTER TABLE nodes ADD COLUMN free_bytes INTEGER;
ALTER TABLE nodes ADD COLUMN total_bytes INTEGER;
ALTER TABLE wanted ADD COLUMN message TEXT;
CREATE INDEX wanted_status ON wanted(status, reason);
""",
    5: """
-- rsync over SSH between servers: what each peer offers, and how we pull
-- from it ('daemon' or 'ssh'); nodes announce the path served over SSH.
ALTER TABLE servers ADD COLUMN transport TEXT NOT NULL DEFAULT 'daemon';
ALTER TABLE servers ADD COLUMN ssh_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE servers ADD COLUMN ssh_host TEXT;
ALTER TABLE servers ADD COLUMN ssh_port INTEGER;
ALTER TABLE servers ADD COLUMN ssh_user TEXT;
ALTER TABLE servers ADD COLUMN ssh_pubkey TEXT;
ALTER TABLE nodes ADD COLUMN ssh_root TEXT;
""",
    6: """
-- Directories made by the user in the virtual tree. Without this a directory
-- exists only as the prefix of file paths and vanishes with its last file. Like
-- tags, an entry is a last-writer-wins record (present = 0 is a tombstone).
CREATE TABLE vdirs (
    vpath      TEXT PRIMARY KEY,
    present    INTEGER NOT NULL,
    updated    TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

CREATE TABLE changes_v6 (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_id  TEXT NOT NULL,
    origin_seq INTEGER NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('copy', 'node', 'tag', 'mark', 'vdir')),
    data       TEXT NOT NULL,
    ts         TEXT NOT NULL,
    UNIQUE (origin_id, origin_seq)
);
INSERT INTO changes_v6(seq, origin_id, origin_seq, kind, data, ts)
    SELECT seq, origin_id, origin_seq, kind, data, ts FROM changes;
DROP TABLE changes;
ALTER TABLE changes_v6 RENAME TO changes;
""",
    7: """
-- Update priority of directories (0 synchronous, 1 frequent, 2 infrequent,
-- 3 archival). The nearest directory at or above a path that has a value
-- decides; NULL is a tombstone (inherit again). Replicated like tags.
CREATE TABLE dirprefs (
    vpath      TEXT PRIMARY KEY,
    priority   INTEGER CHECK (priority IS NULL OR priority BETWEEN 0 AND 3),
    updated    TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

-- When each priority class of a node was last scanned (local, not replicated).
CREATE TABLE scan_state (
    node_id  TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    priority INTEGER NOT NULL,
    scanned  REAL NOT NULL,
    PRIMARY KEY (node_id, priority)
);

-- New kinds of log entries need no table change from now on.
CREATE TABLE changes_v7 (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_id  TEXT NOT NULL,
    origin_seq INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    data       TEXT NOT NULL,
    ts         TEXT NOT NULL,
    UNIQUE (origin_id, origin_seq)
);
INSERT INTO changes_v7(seq, origin_id, origin_seq, kind, data, ts)
    SELECT seq, origin_id, origin_seq, kind, data, ts FROM changes;
DROP TABLE changes;
ALTER TABLE changes_v7 RENAME TO changes;
""",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def connect(path: Path | str, timeout: float = 30) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=timeout)
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


def open_existing(path: Path | str, timeout: float = 30) -> sqlite3.Connection:
    conn = connect(path, timeout)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if 1 <= version < SCHEMA_VERSION:
        migrate(conn)
    elif version != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"{path}: database schema version {version}, expected {SCHEMA_VERSION}"
        )
    return conn


# --- who is using the database ---------------------------------------------------
#
# SQLite allows one writer at a time (readers never wait for it). Every write
# transaction goes through ``transaction`` below, which records who holds the
# write lock and who waits for it, so the client can show it.

_state_lock = threading.Lock()
_holders: dict[int, dict] = {}
_waiters: dict[int, dict] = {}
_tokens = itertools.count(1)
_busy_errors = 0
_local = threading.local()


def set_label(label: str | None) -> None:
    """Name what the current thread is doing (shown while it holds the lock)."""
    _local.label = label


def _who(label: str | None) -> str:
    return label or getattr(_local, "label", None) or threading.current_thread().name


def note_busy_error() -> None:
    global _busy_errors
    with _state_lock:
        _busy_errors += 1


def lock_activity() -> dict:
    """Snapshot: holders of the write lock, waiting writers, busy errors so far."""
    now = time.monotonic()
    with _state_lock:
        holders = [{"who": h["who"], "seconds": round(now - h["since"], 2)}
                   for h in _holders.values()]
        waiting = [{"who": w["who"], "seconds": round(now - w["since"], 2)}
                   for w in _waiters.values()]
        return {"holders": holders, "waiting": waiting, "busy_errors": _busy_errors}


def probe_lock(path: Path | str) -> bool:
    """Is the write lock free right now? True = free. Takes it for an instant.

    Catches a lock held by another process (a CLI command), which the
    in-process records above cannot see.
    """
    conn = sqlite3.connect(path, isolation_level=None, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


class transaction:
    """``with transaction(conn):`` -- BEGIN IMMEDIATE / COMMIT / ROLLBACK.

    Nested use joins the outer transaction. ``label`` says what the
    transaction is for (default: what the thread declared with set_label, or
    the thread's name).
    """

    def __init__(self, conn: sqlite3.Connection, label: str | None = None):
        self.conn = conn
        self.outer = False
        self.label = label
        self.token = 0

    def __enter__(self) -> sqlite3.Connection:
        if self.conn.in_transaction:
            self.outer = True
            return self.conn
        self.token = next(_tokens)
        who = _who(self.label)
        with _state_lock:
            _waiters[self.token] = {"who": who, "since": time.monotonic()}
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            with _state_lock:
                _waiters.pop(self.token, None)
            raise
        with _state_lock:
            _waiters.pop(self.token, None)
            _holders[self.token] = {"who": who, "since": time.monotonic()}
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.outer:
            return
        try:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
        finally:
            with _state_lock:
                _holders.pop(self.token, None)


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta_tx(conn: sqlite3.Connection, key: str, value: str) -> None:
    with transaction(conn):
        set_meta(conn, key, value)


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
