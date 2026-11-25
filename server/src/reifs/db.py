"""SQLite schema and connection helpers."""

from __future__ import annotations

import hashlib
import itertools
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 18

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


MIGRATIONS: dict[int, "str | Callable"] = {
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
    8: """
-- The status line counts these on every poll: let SQLite count the few rows
-- instead of reading every copy and file.
CREATE INDEX copies_marked ON copies(file_id) WHERE marked_delete = 1;
CREATE INDEX copies_missing ON copies(file_id) WHERE state = 'missing';
CREATE INDEX files_issues ON files(id) WHERE issues != '';
""",
    9: """
-- A folder that vanished or moved holds thousands of files: their decisions are
-- gathered under one 'group' decision (their parent) instead of flooding the stack.
ALTER TABLE decisions ADD COLUMN parent_id INTEGER REFERENCES decisions(id) ON DELETE CASCADE;
CREATE INDEX decisions_parent ON decisions(parent_id) WHERE parent_id IS NOT NULL;
""",
    10: """
-- Big databases (millions of files): look proposals up by file, and a node's copies by node.
CREATE INDEX wanted_file ON wanted(file_id);
DROP INDEX copies_node;
CREATE INDEX copies_node ON copies(node_id, size);
""",
    11: """
-- What a peer's sync is doing right now ("catching up: N entries"), shown next to its state.
ALTER TABLE servers ADD COLUMN sync_note TEXT;
""",
    12: """
-- Fetching (a plain download into <server node>/fetch, nothing indexed) and relaying
-- (a file passed through an intermediate server that both are paired with).
ALTER TABLE job_items ADD COLUMN fetch INTEGER NOT NULL DEFAULT 0;  -- 1 fetch, 2 relay staging
ALTER TABLE job_items ADD COLUMN relay_via TEXT;                    -- server relaying it for us
ALTER TABLE job_items ADD COLUMN relay_id TEXT;                     -- our request on that server
ALTER TABLE job_items ADD COLUMN serves TEXT;                       -- fetch = 2: the request we stage for

-- On the intermediate server: files it fetches from where it can reach them for another.
CREATE TABLE relay_requests (
    id        TEXT PRIMARY KEY,
    requester TEXT NOT NULL,
    node_id   TEXT NOT NULL,
    vpath     TEXT NOT NULL,
    hash      TEXT NOT NULL,
    size      INTEGER NOT NULL DEFAULT 0,
    state     TEXT NOT NULL DEFAULT 'queued'
              CHECK (state IN ('queued', 'ready', 'failed', 'done')),
    message   TEXT,
    created   TEXT NOT NULL,
    updated   TEXT NOT NULL
);
""",
    13: """
-- Scheduled syncs: keep a directory of one node copied to another at regular intervals.
CREATE TABLE schedules (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    vpath            TEXT NOT NULL,                 -- the directory ('/' for everything)
    src_node         TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    dst_node         TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    interval_minutes INTEGER NOT NULL CHECK (interval_minutes >= 1),
    update_changed   INTEGER NOT NULL DEFAULT 1,    -- also replace older copies with newer ones
    enabled          INTEGER NOT NULL DEFAULT 1,
    created          TEXT NOT NULL,
    next_due         REAL NOT NULL,                 -- epoch seconds; in the past = run when possible
    last_run         REAL,
    last_job_id      INTEGER,
    last_note        TEXT
);
""",
    14: """
-- A smaller database (design 5.13).
-- (1) The change log keeps no copy of what the tables hold: an entry about a copy only
--     points at it (file, node), and the current state is read when it is sent. At most one
--     entry per copy is kept.
ALTER TABLE changes ADD COLUMN ref_file INTEGER;
ALTER TABLE changes ADD COLUMN ref_node TEXT;
UPDATE changes SET
    ref_file = (SELECT id FROM files WHERE vpath = json_extract(changes.data, '$.vpath')),
    ref_node = json_extract(data, '$.node_id')
  WHERE kind = 'copy' AND json_extract(data, '$.op') IN ('upsert', 'missing');
DELETE FROM changes WHERE ref_node IS NOT NULL AND ref_file IS NULL;
UPDATE changes SET data = '' WHERE ref_file IS NOT NULL;
CREATE INDEX changes_ref ON changes(ref_node, ref_file) WHERE ref_file IS NOT NULL;
DELETE FROM changes WHERE ref_file IS NOT NULL AND seq < (
    SELECT MAX(c2.seq) FROM changes c2
    WHERE c2.ref_node = changes.ref_node AND c2.ref_file = changes.ref_file);
-- (2) Hashes are stored as 32 bytes; the 'hash' column is computed from them for reading.
ALTER TABLE copies ADD COLUMN hash_b BLOB;
UPDATE copies SET hash_b = hb(hash);
DROP INDEX copies_hash;
ALTER TABLE copies DROP COLUMN hash;
ALTER TABLE copies ADD COLUMN hash TEXT GENERATED ALWAYS AS (hx(hash_b)) VIRTUAL;
CREATE INDEX copies_hash ON copies(hash_b, size);
-- (3) Instead of the casefolded path (and its index), a 64-bit number of it.
ALTER TABLE files ADD COLUMN vhash INTEGER;
UPDATE files SET vhash = vh(vpath);
DROP INDEX files_vkey;
ALTER TABLE files DROP COLUMN vkey;
CREATE INDEX files_vhash ON files(vhash);
""",
}


def _migration_15(conn: sqlite3.Connection) -> None:
    """Directories become rows: ``dirs(id, parent_id, name, path)``, and a file is
    ``(dir_id, name)`` instead of a full path string (design 5.14). ``vfiles`` is the view
    that gives the full path back. The directory part of a path used to be stored again in
    every file's row and in two indexes."""
    conn.execute("""CREATE TABLE dirs (
        id        INTEGER PRIMARY KEY,
        parent_id INTEGER REFERENCES dirs(id),
        name      TEXT NOT NULL,
        path      TEXT NOT NULL UNIQUE,      -- '/a/b'; the root is ''
        UNIQUE (parent_id, name))""")
    conn.execute("INSERT INTO dirs(id, parent_id, name, path) VALUES (1, NULL, '', '')")
    found: set[str] = set()
    for (vpath,) in conn.execute("SELECT vpath FROM files"):
        d = vpath.rpartition("/")[0]
        while d and d not in found:
            found.add(d)
            d = d.rpartition("/")[0]
    ids = {"": 1}
    for d in sorted(found, key=lambda x: (x.count("/"), x)):
        parent, _, name = d.rpartition("/")
        cur = conn.execute("INSERT INTO dirs(parent_id, name, path) VALUES (?, ?, ?)",
                           (ids[parent], name, d))
        ids[d] = cur.lastrowid
    conn.create_function("dirid", 1, lambda vp: ids[vp.rpartition("/")[0]], deterministic=True)
    conn.create_function("basename", 1, lambda vp: vp.rpartition("/")[2], deterministic=True)
    conn.execute("""CREATE TABLE files_new (
        id      INTEGER PRIMARY KEY,
        dir_id  INTEGER NOT NULL REFERENCES dirs(id),
        name    TEXT NOT NULL,
        vhash   INTEGER,
        issues  TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL)""")
    conn.execute("INSERT INTO files_new(id, dir_id, name, vhash, issues, created)"
                 " SELECT id, dirid(vpath), basename(vpath), vhash, issues, created FROM files")
    conn.execute("DROP TABLE files")
    conn.execute("ALTER TABLE files_new RENAME TO files")
    conn.execute("CREATE UNIQUE INDEX files_dirname ON files(dir_id, name)")
    conn.execute("CREATE INDEX files_vhash ON files(vhash)")
    conn.execute("CREATE INDEX files_issues ON files(id) WHERE issues != ''")
    conn.execute(VFILES_VIEW)


VFILES_VIEW = """CREATE VIEW vfiles AS
    SELECT f.id AS id, f.dir_id AS dir_id, f.name AS name, d.path AS dir_path,
           d.path || '/' || f.name AS vpath, f.vhash AS vhash, f.issues AS issues,
           f.created AS created
    FROM files f JOIN dirs d ON d.id = f.dir_id"""

MIGRATIONS[15] = _migration_15

# Columns that held a time as text and now hold microseconds since the epoch (design 5.15).
TIME_COLUMNS = {
    "servers": ("last_seen", "last_sync"),
    "nodes": ("last_seen", "last_scan", "owner_since"),
    "copies": ("first_seen", "hashed_at"),
    "tags": ("updated",),
    "decisions": ("created", "resolved"),
    "wanted": ("created",),
    "scans": ("started", "finished"),
    "foreign_sightings": ("seen",),
    "jobs": ("created", "started", "finished"),
    "job_items": ("updated",),
    "pair_requests": ("created",),
    "outbox": ("created",),
    "orphan_tags": ("updated",),
    "vdirs": ("updated",),
    "dirprefs": ("updated",),
    "changes": ("ts",),
    "relay_requests": ("created", "updated"),
    "schedules": ("created",),
    "files": ("created",),
}


def _migration_16(conn: sqlite3.Connection) -> None:
    def lenient(text):
        try:
            return from_iso(text)
        except ValueError:
            return 0       # not a time at all: keep the row, forget the value
    conn.create_function("us", 1, lenient, deterministic=True)
    conn.execute("DROP VIEW IF EXISTS vfiles")
    for table, cols in TIME_COLUMNS.items():
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                           (table,)).fetchone()[0]
        indexes = [r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ?"
            " AND sql IS NOT NULL", (table,))]
        names = [r[1] for r in conn.execute(f"PRAGMA table_xinfo({table})") if r[6] == 0]
        sql = re.sub(r'^CREATE TABLE\s+("?)\w+\1', f"CREATE TABLE {table}_t16", sql, count=1)
        for c in cols:
            sql, n = re.subn(rf"(\b{c}\s+)TEXT\b( NOT NULL)?( DEFAULT '')?",
                             lambda m: m.group(1) + "INTEGER" + (m.group(2) or "")
                             + (" DEFAULT 0" if m.group(3) else ""), sql, count=1)
            assert n == 1, (table, c)
        conn.execute(sql)
        sel = ", ".join(f"us({n})" if n in cols else n for n in names)
        conn.execute(f"INSERT INTO {table}_t16({', '.join(names)}) SELECT {sel} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_t16 RENAME TO {table}")
        for ix in indexes:
            conn.execute(ix)
    conn.execute(VFILES_VIEW)


MIGRATIONS[16] = _migration_16

MIGRATIONS[17] = """
-- Packed directories (design 5.16): a directory kept as one squashfs file. The archive is an
-- ordinary file of the virtual tree (``<dir>.sqsh``); this table says which directory it stands
-- for, and ``archive_members`` lists what is inside so that a "deep" search can find it.
CREATE TABLE archives (
    id         INTEGER PRIMARY KEY,
    dir_path   TEXT NOT NULL UNIQUE,         -- the packed directory, '/src/foo'
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    present    INTEGER NOT NULL DEFAULT 1,   -- 0: unpacked or removed (a tombstone for peers)
    files      INTEGER NOT NULL DEFAULT 0,   -- members
    bytes      INTEGER NOT NULL DEFAULT 0,   -- their size before packing
    updated    INTEGER NOT NULL,
    updated_by TEXT NOT NULL
);
CREATE TABLE archive_members (
    archive_id INTEGER NOT NULL REFERENCES archives(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,                -- relative to the packed directory
    size       INTEGER NOT NULL,
    PRIMARY KEY (archive_id, path)
) WITHOUT ROWID;
"""

MIGRATIONS[18] = """
-- Files and directories whose copies may differ (design 5.17): no decision is asked. A
-- directory's setting is inherited; NULL clears it (like dirprefs).
CREATE TABLE tolerance (
    vpath      TEXT PRIMARY KEY,
    value      INTEGER,
    updated    INTEGER NOT NULL,
    updated_by TEXT NOT NULL
);
-- Node flags: a mirrored drive counts as two copies; a backup-first node is filled first.
ALTER TABLE nodes ADD COLUMN mirrored INTEGER NOT NULL DEFAULT 0;
ALTER TABLE nodes ADD COLUMN backup_first INTEGER NOT NULL DEFAULT 0;
"""


def now() -> int:
    """The current time as the database keeps it: microseconds since the Unix epoch (UTC)."""
    return time.time_ns() // 1000


def to_iso(us: int) -> str:
    """Microseconds since the epoch as ``2026-11-23T23:04:08.123456+00:00`` (what peers and the
    clients see; 0 is "never" and gives an empty text)."""
    if not us:
        return ""
    return (datetime.fromtimestamp(us // 1_000_000, timezone.utc)
            .replace(microsecond=us % 1_000_000).isoformat(timespec="microseconds"))


def from_iso(text) -> int | None:
    """The reverse of ``to_iso``; numbers pass through, empty text is 0, None stays None."""
    if text is None or isinstance(text, int):
        return text
    if not text:
        return 0
    d = datetime.fromisoformat(text)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    delta = d - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


# Names of the fields that hold such a time in what goes over the API and the wire.
TIME_KEYS = frozenset({"created", "updated", "last_seen", "last_sync", "last_scan", "owner_since",
                       "started", "finished", "seen", "since", "resolved", "first_seen",
                       "hashed_at", "ts"})
_MIN_TIME = 10 ** 14        # in microseconds: 1973; no other number in those fields is this big


def times_to_iso(obj):
    """A copy of ``obj`` (JSON-like) with the time fields as text."""
    if isinstance(obj, dict):
        return {k: (to_iso(v) if k in TIME_KEYS and isinstance(v, int) and not isinstance(v, bool)
                    and (v >= _MIN_TIME or (v == 0 and k == "owner_since"))
                    else times_to_iso(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [times_to_iso(v) for v in obj]
    return obj


def times_from_iso(obj):
    """The reverse, for what arrives from a peer."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in TIME_KEYS and isinstance(v, str):
                try:
                    out[k] = from_iso(v)
                    continue
                except ValueError:
                    pass
            out[k] = times_from_iso(v)
        return out
    if isinstance(obj, list):
        return [times_from_iso(v) for v in obj]
    return obj


def _use_a_roomy_temp_dir(path: Path | str) -> None:
    """Big queries (sorting, grouping millions of rows) spill into temporary files. SQLite
    puts them in /var/tmp or /tmp, which is often a small RAM disk: that ends in
    "database or disk is full" with plenty of space on the real disk. Use a folder next to
    the database instead, unless the user chose one (SQLITE_TMPDIR)."""
    if os.environ.get("SQLITE_TMPDIR"):
        return
    tmp = Path(path).resolve().parent / "tmp"
    try:
        tmp.mkdir(exist_ok=True)
        os.environ["SQLITE_TMPDIR"] = str(tmp)
    except OSError:
        pass


_HASH_RE = re.compile(r"^blake2b:[0-9a-f]{64}$")


def hash_to_blob(text: str | None) -> bytes | None:
    """``blake2b:<64 hex>`` -> its 32 bytes (what the database stores). Anything else is kept
    as it is, after a zero byte."""
    if text is None:
        return None
    if isinstance(text, (bytes, bytearray, memoryview)):    # not what we write, but be lenient
        text = bytes(text).decode("utf-8", "surrogateescape")
    elif not isinstance(text, str):
        text = str(text)
    if _HASH_RE.match(text):
        return bytes.fromhex(text[8:])
    return b"\x00" + text.encode("utf-8", "surrogatepass")


def blob_to_hash(blob: bytes | None) -> str | None:
    if blob is None:
        return None
    blob = bytes(blob)
    if len(blob) == 32:
        return "blake2b:" + blob.hex()
    return blob[1:].decode("utf-8", "surrogatepass") if blob[:1] == b"\x00" else blob.hex()


def key_hash(vpath: str) -> int:
    """A 64-bit number for the casefolded path: a small, indexable stand-in for it. Two paths
    that differ only in case have the same number (so do, rarely, others: callers check)."""
    if isinstance(vpath, (bytes, bytearray, memoryview)):
        vpath = bytes(vpath).decode("utf-8", "surrogateescape")
    elif not isinstance(vpath, str):
        vpath = str(vpath)
    digest = hashlib.blake2b(vpath.casefold().encode("utf-8", "surrogatepass"),
                             digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def register_functions(conn: sqlite3.Connection) -> None:
    """The SQL functions the schema uses: ``hx`` (the hash column is computed from the
    stored 32 bytes), ``hb`` and ``vh`` (to write them)."""
    conn.create_function("hx", 1, blob_to_hash, deterministic=True)
    conn.create_function("hb", 1, hash_to_blob, deterministic=True)
    conn.create_function("vh", 1, key_hash, deterministic=True)


def connect(path: Path | str, timeout: float = 30, new: bool = False) -> sqlite3.Connection:
    _use_a_roomy_temp_dir(path)
    conn = sqlite3.connect(path, isolation_level=None, timeout=timeout)
    conn.row_factory = sqlite3.Row
    register_functions(conn)
    if new:
        # Free pages can then be given back to the disk a little at a time (see ``reclaim``);
        # this has to be chosen before the file is used at all (before WAL mode, too).
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def create(path: Path | str) -> sqlite3.Connection:
    conn = connect(path, new=True)
    # executescript() manages its own transaction; keep schema + version atomic.
    conn.executescript(f"BEGIN;\n{SCHEMA}\nPRAGMA user_version = 1;\nCOMMIT;")
    migrate(conn)
    return conn


class DatabaseLocked(Exception):
    """Another process holds the database (a server still running, a stuck command)."""


def holders_of(path: Path | str) -> list[tuple[int, str]]:
    """Other processes that have the database file open (Linux; best effort)."""
    out: list[tuple[int, str]] = []
    try:
        target = {os.path.realpath(path), os.path.realpath(str(path) + "-wal"),
                  os.path.realpath(str(path) + "-shm")}
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit() or int(pid_dir.name) == os.getpid():
                continue
            try:
                for fd in (pid_dir / "fd").iterdir():
                    if os.path.realpath(fd) in target:
                        cmd = (pid_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                            "utf-8", "replace").strip()
                        out.append((int(pid_dir.name), cmd[:120]))
                        break
            except OSError:
                continue
    except OSError:
        pass
    return out


def locked_message(path: Path | str) -> str:
    who = holders_of(path)
    text = f"the database {path} is locked by another process"
    if who:
        text += ":\n" + "\n".join(f"  pid {pid}: {cmd}" for pid, cmd in who)
        text += ("\nIf that is a rei-fs server or command you no longer want, stop it "
                 "(Ctrl+C, or kill the pid) and try again.")
    else:
        text += " (a server, a scan or job run, or a backup tool). Try again in a moment."
    return text


def _run_python_migration(conn: sqlite3.Connection, target: int, step) -> None:
    """A migration written in Python. A table is swapped, so foreign keys are off meanwhile
    (dropping a table would otherwise delete the rows that reference it) and checked after."""
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("PRAGMA user_version").fetchone()[0] >= target:
            conn.execute("ROLLBACK")      # another process did it while we waited
            return
        step(conn)
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise sqlite3.IntegrityError(f"the upgrade left {len(bad)} broken references")
        conn.execute(f"PRAGMA user_version = {target}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def migrate(conn: sqlite3.Connection) -> None:
    sqlite3.enable_callback_tracebacks(True)    # a failing SQL function then says why
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for target in range(version + 1, SCHEMA_VERSION + 1):
        try:
            step = MIGRATIONS[target]
            if callable(step):
                _run_python_migration(conn, target, step)
                continue
            conn.executescript(
                f"BEGIN;\n{step}\nPRAGMA user_version = {target};\nCOMMIT;"
            )
        except sqlite3.OperationalError as e:
            # Another process migrated while we waited for the write lock: fine.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if conn.execute("PRAGMA user_version").fetchone()[0] >= target:
                continue
            raise sqlite3.OperationalError(
                f"{e} (while upgrading the database to version {target})") from e


def table_sizes(conn: sqlite3.Connection) -> list[tuple[str, int]] | None:
    """(table or index, bytes) largest first, or None when SQLite was built without dbstat."""
    try:
        return [(r[0], r[1]) for r in conn.execute(
            "SELECT name, SUM(pgsize) AS s FROM dbstat GROUP BY name ORDER BY s DESC")]
    except sqlite3.OperationalError:
        return None


def free_space_ratio(conn: sqlite3.Connection) -> float:
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    return conn.execute("PRAGMA freelist_count").fetchone()[0] / pages if pages else 0.0


def vacuum(conn: sqlite3.Connection, path: Path | str) -> tuple[int, int]:
    """Rebuild the file so that free pages are given back to the disk (needs about the size of
    the database in free space, and exclusive use of it for the time it takes).
    Returns (bytes before, bytes after)."""
    path = Path(path)

    def size() -> int:
        return sum(p.stat().st_size for p in (path, Path(str(path) + "-wal")) if p.exists())

    before = size()
    free = shutil.disk_usage(path.parent).free
    if free < before * 1.1:
        raise DatabaseLocked(f"not enough free disk space to compact the database: it takes "
                             f"about {before // 2**20} MiB and {free // 2**20} MiB are free")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")     # takes effect with the rebuild
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return before, size()


def is_incremental(conn: sqlite3.Connection) -> bool:
    return conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2


def reclaim(conn: sqlite3.Connection, seconds: float = 20.0, chunk: int = 4000) -> int:
    """Give free pages back to the disk after many rows were deleted, a little at a time so that
    writers wait for a moment only. Needs ``auto_vacuum = INCREMENTAL`` (databases made by this
    version; older ones get it with ``vacuum``). Returns the bytes freed (0 if nothing could be)."""
    if not is_incremental(conn):
        return 0
    page = conn.execute("PRAGMA page_size").fetchone()[0]
    start = conn.execute("PRAGMA freelist_count").fetchone()[0]
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        left = conn.execute("PRAGMA freelist_count").fetchone()[0]
        if not left:
            break
        conn.execute(f"PRAGMA incremental_vacuum({chunk})").fetchall()
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")      # the file itself shrinks at a checkpoint
    return (start - conn.execute("PRAGMA freelist_count").fetchone()[0]) * page


def make_incremental(conn: sqlite3.Connection, path: Path | str) -> None:
    """Databases made by older versions give no space back: convert them once (a full rebuild,
    like the compaction after an upgrade). Called when the server starts."""
    if is_incremental(conn):
        return
    print("rei-fs: preparing the database to give space back after deletions "
          "(one-time rebuild of the file; a few minutes if it is big)", file=sys.stderr, flush=True)
    try:
        b, a = vacuum(conn, path)
        print(f"rei-fs: database {b // 2**20} MiB -> {a // 2**20} MiB", file=sys.stderr, flush=True)
    except (DatabaseLocked, sqlite3.Error) as e:
        print(f"rei-fs: could not rebuild it now ({e}); 'reifs db vacuum' does it",
              file=sys.stderr, flush=True)


def _free(path: Path | str) -> str:
    try:
        u = shutil.disk_usage(path)
        return f"{u.free // 2**20} MiB free of {u.total // 2**20} MiB"
    except OSError:
        return "unknown"


def _space_report(path: Path | str) -> str:
    """Where an upgrade writes, and how much room each place has."""
    db_dir = Path(path).resolve().parent
    tmp = os.environ.get("SQLITE_TMPDIR") or "/var/tmp or /tmp"
    lines = [f"  database folder {db_dir}: {_free(db_dir)}"]
    if os.environ.get("SQLITE_TMPDIR"):
        lines.append(f"  temporary files {tmp}: {_free(tmp)}")
    else:
        lines.append(f"  temporary files {tmp}: {_free('/var/tmp')} / {_free('/tmp')}")
    return "\n".join(lines)


def open_existing(path: Path | str, timeout: float = 30,
                  migration_wait: float = 120) -> sqlite3.Connection:
    conn = connect(path, timeout)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if 1 <= version < SCHEMA_VERSION:
            # Rebuilding the change log of a big database takes a while, and a server
            # that is still running may be writing: be patient.
            conn.execute(f"PRAGMA busy_timeout = {int(migration_wait * 1000)}")
            size = Path(path).stat().st_size
            if size > 32 * 2**20:
                print(f"rei-fs: upgrading the database ({size // 2**20} MiB) from version "
                      f"{version} to {SCHEMA_VERSION}; this can take several minutes and "
                      f"needs about {size * 2 // 2**20} MiB of free space for a while:\n"
                      f"{_space_report(path)}", file=sys.stderr, flush=True)
            try:
                migrate(conn)
            except sqlite3.OperationalError as e:
                if "full" in str(e):
                    raise RuntimeError(
                        f"the database upgrade ran out of space ({e}). SQLite says this both for "
                        f"a full disk and for a full temporary folder.\n{_space_report(path)}\n"
                        "Free some space there (or set SQLITE_TMPDIR to a roomy folder) and "
                        "start again: nothing was changed.") from e
                raise
            if version < 16 and Path(path).stat().st_size > 32 * 2**20:
                # The upgrade rewrote the tables in place, leaving slack in every page: rebuild
                # the file so the space goes back (once; can take minutes on a big database).
                msg = "rei-fs: the database was made smaller; compacting the file (a few minutes)"
                print(msg, file=sys.stderr, flush=True)
                try:
                    b, a = vacuum(conn, path)
                    print(f"rei-fs: database {b // 2**20} MiB -> {a // 2**20} MiB",
                          file=sys.stderr, flush=True)
                except (DatabaseLocked, sqlite3.Error) as e:
                    print(f"rei-fs: could not compact it now ({e}); 'reifs db vacuum' does it",
                          file=sys.stderr, flush=True)
            conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        elif version != SCHEMA_VERSION:
            conn.close()
            raise RuntimeError(
                f"{path}: database schema version {version}, expected {SCHEMA_VERSION}"
            )
    except sqlite3.OperationalError as e:
        conn.close()
        if "locked" in str(e) or "busy" in str(e):
            raise DatabaseLocked(locked_message(path)) from e
        raise
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
