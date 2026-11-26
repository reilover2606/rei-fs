"""Opaque directories: one row of the index for the whole directory (design section 5.22).

For a directory of millions of small files that nobody needs tracked one by one (a decompiled
program, a build tree, a cache) the index holds ONE row, as if the directory were a single
file (see ``collections_``): no row, hash or decision for any file inside. Everything that
works on files works on the directory as a whole: copying and backing it up, moving and
renaming it, versions, tags, schedules, "synchronous". The number of files in it is kept on the
side. In the tree it shows as one folder entry; a *deep* search still finds names inside it, by
reading the disk of the nodes it is on at that moment (names only).

The setting is made on a directory and inherited by what is below it; the nearest setting
decides (a setting of 0 switches an inherited one off), but inside an opaque directory nothing
is looked at, so a setting made there has no effect. It is replicated like the directory
priorities, so every server leaves the directory out when it scans its own nodes.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from . import db, index, nodes, paths
from .config import Config


class OpaqueError(Exception):
    pass


def load(conn: sqlite3.Connection) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT vpath, value FROM opaque WHERE value IS NOT NULL")}


def roots(conn: sqlite3.Connection) -> list[str]:
    """The directories that are opaque."""
    return sorted(v for v, val in load(conn).items() if val)


def source(conn: sqlite3.Connection, vpath: str) -> tuple[bool, str | None]:
    parts = paths.split(vpath)
    cands = ["/" + "/".join(parts[:i]) for i in range(len(parts), 0, -1)] + ["/"]
    rows = {r[0]: r[1] for r in conn.execute(
        f"SELECT vpath, value FROM opaque WHERE value IS NOT NULL AND vpath IN"
        f" ({','.join('?' * len(cands))})", cands)}
    # The outermost opaque directory is what counts: nothing inside it is ever looked at.
    for c in reversed(cands):
        if rows.get(c):
            return True, c
    for c in cands:
        if c in rows:
            return bool(rows[c]), c
    return False, None


def is_opaque(conn: sqlite3.Connection, vpath: str) -> bool:
    return source(conn, vpath)[0]


def get(conn: sqlite3.Connection, vpath: str) -> dict:
    vpath = paths.normalize(vpath)
    value, src = source(conn, vpath)
    return {"path": vpath, "opaque": value, "set_on": src, "inherited": src != vpath}


def log(conn: sqlite3.Connection, vpath: str) -> None:
    r = conn.execute("SELECT value, updated, updated_by FROM opaque WHERE vpath = ?",
                     (vpath,)).fetchone()
    if r:
        index.append_change(conn, "opaque", {"vpath": vpath, "value": r["value"],
                                             "updated": r["updated"],
                                             "updated_by": r["updated_by"]})


def _put(conn: sqlite3.Connection, vpath: str, value: int | None) -> None:
    conn.execute(
        "INSERT INTO opaque(vpath, value, updated, updated_by) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(vpath) DO UPDATE SET value = excluded.value, updated = excluded.updated,"
        " updated_by = excluded.updated_by",
        (vpath, value, db.now(), index.self_id(conn)))
    log(conn, vpath)


def set_opaque(conn: sqlite3.Connection, vpath: str, value: bool | None) -> dict:
    """Make a directory opaque (True), not opaque (False, even below an opaque one: no effect
    inside it) or inherit (None). What the index holds below an opaque directory is forgotten
    by the worker (``forget_indexed``), here and on every server."""
    try:
        vpath = paths.normalize(vpath)
    except paths.VPathError as e:
        raise OpaqueError(str(e)) from None
    if vpath == "/":
        raise OpaqueError("the whole tree cannot be opaque")
    if index.file_id(conn, vpath) is not None and not is_opaque(conn, vpath):
        raise OpaqueError(f"{vpath} is a file; opaque is a setting of directories")
    with db.transaction(conn, "setting opaque"):
        _put(conn, vpath, None if value is None else int(value))
    return get(conn, vpath)


def move(conn: sqlite3.Connection, old: str, new: str) -> int:
    n = 0
    for vpath, value in sorted(load(conn).items()):
        if vpath == old or vpath.startswith(old.rstrip("/") + "/"):
            _put(conn, vpath, None)
            _put(conn, new + vpath[len(old):], value)
            n += 1
    return n


def apply_change(conn: sqlite3.Connection, d: dict) -> None:
    row = conn.execute("SELECT updated, updated_by FROM opaque WHERE vpath = ?",
                       (d["vpath"],)).fetchone()
    if row is None or (d["updated"], d["updated_by"]) > (row[0], row[1]):
        conn.execute("INSERT OR REPLACE INTO opaque(vpath, value, updated, updated_by)"
                     " VALUES (?, ?, ?, ?)", (d["vpath"], d["value"], d["updated"], d["updated_by"]))


TX_ROWS = 500


def stale_collections(conn: sqlite3.Connection) -> list[int]:
    """Rows that stand for a directory that is not opaque any more (the setting was removed)."""
    live = set(roots(conn))
    out = []
    for fid, in conn.execute("SELECT DISTINCT file_id FROM collection_stats").fetchall():
        row = conn.execute("SELECT vpath FROM vfiles WHERE id = ?", (fid,)).fetchone()
        if row is None or row["vpath"] not in live:
            out.append(fid)
    return out


def indexed_below(conn: sqlite3.Connection) -> bool:
    """Is there anything to tidy: files indexed below an opaque directory, or the row of a
    directory that is not opaque any more?"""
    if stale_collections(conn):
        return True
    for root in roots(conn):
        cond, args = paths.subtree_sql(root, "d.path")
        if conn.execute("SELECT 1 FROM files f JOIN dirs d ON d.id = f.dir_id"
                        f" WHERE {cond} LIMIT 1", args).fetchone():
            return True
    return False


def forget_indexed(conn: sqlite3.Connection, say=None, stop=None) -> int:
    """Forget what the index holds below the opaque directories: their files, copies and the
    change-log pointers to them, TX_ROWS files per transaction. No removal is logged per file:
    every server does the same when it learns the setting (and the scanner of the node's owner
    would not announce them either: it no longer looks)."""
    from .control import check

    total = 0
    for fid in stale_collections(conn):      # no longer opaque: the directory is scanned as usual
        with db.transaction(conn, "forgetting the row of a directory that is not opaque"):
            conn.execute("DELETE FROM files WHERE id = ?", (fid,))
    for root in roots(conn):
        cond, args = paths.subtree_sql(root, "d.path")
        while True:
            check(stop)
            fids = [r[0] for r in conn.execute(
                "SELECT f.id FROM files f JOIN dirs d ON d.id = f.dir_id"
                f" WHERE {cond} LIMIT ?", (*args, TX_ROWS))]
            if not fids:
                break
            marks = ",".join("?" * len(fids))
            with db.transaction(conn, "forgetting what is below an opaque directory"):
                dir_ids = {r[0] for r in conn.execute(
                    f"SELECT dir_id FROM files WHERE id IN ({marks})", fids)}
                conn.execute(f"DELETE FROM copies WHERE file_id IN ({marks})", fids)
                conn.execute(f"DELETE FROM changes WHERE ref_file IN ({marks})", fids)
                conn.execute(f"DELETE FROM files WHERE id IN ({marks})", fids)
                for did in dir_ids:
                    index.prune_dirs(conn, did)
            total += len(fids)
            if say:
                say(f"forgetting what is below {root}: {total:,} files")
    return total


# --- deep search ---------------------------------------------------------------------


def search_names(conn: sqlite3.Connection, cfg: Config, pattern: str | None, limit: int,
                 seconds: float = 20.0) -> tuple[list[dict], bool]:
    """Names inside the opaque directories that match, read from the disk of this server's
    nodes now. (hits, stopped early: the limit or the time ran out)."""
    from . import queries

    pat = queries.name_pattern(pattern)
    if pat is None:
        return [], False
    deadline = time.monotonic() + seconds
    locs = {loc.node_id: loc.root for loc in nodes.discover(conn, cfg).online}
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM nodes")}
    hits: list[dict] = []
    for root in roots(conn):
        for node_id, node_root in sorted(locs.items()):
            base = paths.to_physical(node_root, root)
            if not base.is_dir():
                continue
            for here, dirs, files in os.walk(base):
                rel = os.path.relpath(here, base).replace(os.sep, "/")
                prefix = root if rel == "." else f"{root}/{rel}"
                for name, is_dir in [(n, True) for n in dirs] + [(n, False) for n in files]:
                    vpath = f"{prefix}/{name}"
                    if queries._name_matches(pat, vpath):
                        hits.append({"vpath": vpath, "opaque": root, "node": names.get(node_id, ""),
                                     "dir": is_dir})
                        if len(hits) >= limit:
                            return hits, True
                if time.monotonic() > deadline:
                    return hits, True
    return hits, False
