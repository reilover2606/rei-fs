"""Synchronous files and directories (design section 5.18).

A path whose update priority is "synchronous" is kept the same on every node that holds it:
when a copy changes (the node is scanned every few seconds), the newest version is copied over
the older ones at once, without a decision being asked and without the jobs filling the job
list (they are hidden unless they fail). Applies to:

* a file or directory with the setting (a directory's setting is inherited by what is below it
  unless a nearer one says otherwise),
* the nodes that *hold* it: for a directory, the nodes with at least one file below it and the
  nodes an application registered an instance on (so that a new, still empty instance gets
  the files).

The newest modification time wins (ties by hash, the same everywhere). Only a destination copy
that is present and older is overwritten, and only if it still has the content judged stale
(``allow_hash``); a missing or deleted copy is not brought back, and nothing is ever deleted.
Each server makes the jobs for the nodes it owns (it pulls, like every transfer), so the
servers need no coordination. A path that is tolerant of several versions is left alone.
"""

from __future__ import annotations

import logging
import sqlite3

from . import db, index, jobs, paths, priority, tolerance

log = logging.getLogger("reifs.synchronous")

MAX_ITEMS_PER_RUN = 5000
KEEP_HIDDEN_SECONDS = 3600   # finished hidden jobs are kept this long
FULL_PASS_SECONDS = 300


def any_synchronous(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM dirprefs WHERE priority = 0 LIMIT 1").fetchone() is not None


def _holders(conn: sqlite3.Connection, root: str) -> set[str]:
    """Nodes that hold the path, or that an application registered an instance of it on."""
    fid = index.file_id(conn, root)
    if fid is not None:
        nodes_ = {r[0] for r in conn.execute(
            "SELECT node_id FROM copies WHERE file_id = ? AND state = 'present'", (fid,))}
    else:
        cond, args = paths.subtree_sql(root, "d.path")
        nodes_ = {r[0] for r in conn.execute(
            "SELECT DISTINCT c.node_id FROM files f JOIN dirs d ON d.id = f.dir_id"
            f" JOIN copies c ON c.file_id = f.id AND c.state = 'present' WHERE {cond}", args)}
    cond, args = paths.subtree_sql(root, "vpath")
    nodes_ |= {r[0] for r in conn.execute(
        f"SELECT node_id FROM apps WHERE present = 1 AND mode = 'instance' AND {cond}", args)}
    return nodes_


def _candidates(conn: sqlite3.Connection, full: bool, since: int) -> tuple[set[int], int]:
    """File ids to look at: everything under the synchronous paths (a full pass), or the
    files the change log mentions after ``since``."""
    top = conn.execute("SELECT IFNULL(MAX(seq), 0) FROM changes").fetchone()[0]
    ids: set[int] = set()
    if full:
        for (root,) in conn.execute("SELECT vpath FROM dirprefs WHERE priority = 0").fetchall():
            fid = index.file_id(conn, root)
            if fid is not None:
                ids.add(fid)
                continue
            cond, args = paths.subtree_sql(root, "d.path")
            ids |= {r[0] for r in conn.execute(
                f"SELECT f.id FROM files f JOIN dirs d ON d.id = f.dir_id WHERE {cond}", args)}
    else:
        ids = {r[0] for r in conn.execute(
            "SELECT DISTINCT ref_file FROM changes WHERE seq > ? AND seq <= ?"
            " AND ref_file IS NOT NULL", (since, top))}
    return ids, top


def run(conn: sqlite3.Connection, cfg, full: bool = False) -> int:
    """Create the (hidden) job that brings the nodes we own up to date. Returns its items."""
    if not any_synchronous(conn):
        return 0
    since = int(db.get_meta(conn, "sync_seq") or 0)
    if db.get_meta(conn, "sync_full") == "1":
        full = True
        db.set_meta_tx(conn, "sync_full", "0")
    if full:
        since = 0
    ids, top = _candidates(conn, full, since)
    own = index.owned_node_ids(conn)
    online = {r[0] for r in conn.execute("SELECT id FROM nodes WHERE online = 1")}
    mine = own & online
    items: list[tuple] = []
    holders_cache: dict[str, set[str]] = {}
    active = {(r[0], r[1]) for r in conn.execute(
        "SELECT i.vpath, i.dst_node FROM job_items i JOIN jobs j ON j.id = i.job_id"
        " WHERE j.hidden = 1 AND i.state IN ('pending', 'running')")}
    for fid in sorted(ids):
        if len(items) >= MAX_ITEMS_PER_RUN:
            top = since        # not finished: the rest is looked at next time
            break
        row = conn.execute("SELECT vpath FROM vfiles WHERE id = ?", (fid,)).fetchone()
        if row is None:
            continue
        vpath = row["vpath"]
        root = priority.sync_root(conn, vpath)
        if root is None or tolerance.is_tolerant(conn, vpath):
            continue
        cur = index.current_version(conn, fid)
        if cur is None:
            continue
        have = {r["node_id"]: r for r in conn.execute(
            "SELECT node_id, hash, state, marked_delete, size FROM copies WHERE file_id = ?",
            (fid,))}
        if root not in holders_cache:
            holders_cache[root] = _holders(conn, root)
        size = max((r["size"] for r in have.values() if r["hash"] == cur.hash), default=0)
        for dst in sorted(holders_cache[root] & mine):
            if (vpath, dst) in active:
                continue
            c = have.get(dst)
            if c is None:
                items.append(("copy", vpath, dst, size, None))
            elif c["state"] == "present" and not c["marked_delete"] and c["hash"] != cur.hash:
                items.append(("overwrite", vpath, dst, size, c["hash"]))
    created = 0
    if items:
        ts = db.now()
        with db.transaction(conn, "synchronizing"):
            job_id = jobs._new_job(conn, "wanted", f"synchronous: {len(items)} file(s)")
            conn.execute("UPDATE jobs SET hidden = 1 WHERE id = ?", (job_id,))
            conn.executemany(
                "INSERT INTO job_items(job_id, op, vpath, src_node, dst_node, size, allow_hash,"
                " updated) VALUES (?, ?, ?, NULL, ?, ?, ?, ?)",
                [(job_id, op, v, dst, sz, allow, ts) for op, v, dst, sz, allow in items])
        created = len(items)
    db.set_meta_tx(conn, "sync_seq", str(top))
    _tidy(conn)
    return created


def _tidy(conn: sqlite3.Connection) -> None:
    """Finished hidden jobs are not worth keeping; one whose destination changed meanwhile
    (a conflict) is dropped and the files are looked at again."""
    stuck = [r[0] for r in conn.execute(
        "SELECT DISTINCT j.id FROM jobs j JOIN job_items i ON i.job_id = j.id"
        " WHERE j.hidden = 1 AND i.state = 'conflict'")]
    for job_id in stuck:
        jobs.cancel_job(conn, job_id)
    if stuck:
        db.set_meta_tx(conn, "sync_full", "1")
    cutoff = db.now() - KEEP_HIDDEN_SECONDS * 1_000_000
    with db.transaction(conn, "tidying synchronous jobs"):
        conn.execute("DELETE FROM jobs WHERE hidden = 1 AND status IN ('done', 'cancelled')"
                     " AND IFNULL(finished, 0) < ?", (cutoff,))
