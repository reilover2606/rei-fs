"""Low-level index operations: files, copies, change log, decisions.

All functions expect to run inside a transaction (see ``db.transaction``).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import db, paths

# --- files -------------------------------------------------------------------


def file_id(conn: sqlite3.Connection, vpath: str) -> int | None:
    row = conn.execute("SELECT id FROM files WHERE vpath = ?", (vpath,)).fetchone()
    return row[0] if row else None


def ensure_file(conn: sqlite3.Connection, vpath: str) -> int:
    fid = file_id(conn, vpath)
    if fid is not None:
        return fid
    vkey = paths.key(vpath)
    cur = conn.execute(
        "INSERT INTO files(vpath, vkey, issues, created) VALUES (?, ?, ?, ?)",
        (vpath, vkey, paths.encode_issues(paths.portability_issues(vpath)), db.now()),
    )
    refresh_case_flags(conn, vkey)
    return cur.lastrowid


def refresh_case_flags(conn: sqlite3.Connection, vkey: str) -> None:
    rows = conn.execute("SELECT id, vpath, issues FROM files WHERE vkey = ?", (vkey,)).fetchall()
    collide = len(rows) > 1
    for r in rows:
        issues = paths.decode_issues(r["issues"])
        new = (issues | {paths.ISSUE_CASE}) if collide else (issues - {paths.ISSUE_CASE})
        if new != issues:
            conn.execute(
                "UPDATE files SET issues = ? WHERE id = ?", (paths.encode_issues(new), r["id"])
            )


def drop_file_if_orphan(conn: sqlite3.Connection, fid: int) -> bool:
    """Forget a file once no copy of it is tracked anywhere."""
    if conn.execute("SELECT 1 FROM copies WHERE file_id = ?", (fid,)).fetchone():
        return False
    row = conn.execute("SELECT vkey FROM files WHERE id = ?", (fid,)).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM files WHERE id = ?", (fid,))
    refresh_case_flags(conn, row["vkey"])
    return True


# --- change log --------------------------------------------------------------


def log_change(
    conn: sqlite3.Connection, node_id: str, vpath: str, op: str,
    size: int | None = None, mtime_ns: int | None = None, hash_: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO changes(node_id, vpath, op, size, mtime_ns, hash, ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (node_id, vpath, op, size, mtime_ns, hash_, db.now()),
    )


def vpath_of(conn: sqlite3.Connection, fid: int) -> str:
    return conn.execute("SELECT vpath FROM files WHERE id = ?", (fid,)).fetchone()[0]


def forget_copy(conn: sqlite3.Connection, fid: int, node_id: str) -> None:
    """Stop tracking a copy (it is physically gone or no longer wanted)."""
    vpath = vpath_of(conn, fid)
    conn.execute("DELETE FROM copies WHERE file_id = ? AND node_id = ?", (fid, node_id))
    log_change(conn, node_id, vpath, "remove")
    obsolete_decisions(conn, fid, ("missing",), node_id)
    if not drop_file_if_orphan(conn, fid):
        evaluate_divergence(conn, fid)


# --- decisions ---------------------------------------------------------------


def add_decision(
    conn: sqlite3.Connection, kind: str, fid: int | None, node_id: str | None,
    data: dict | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO decisions(kind, file_id, node_id, data, created) VALUES (?, ?, ?, ?, ?)",
        (kind, fid, node_id, json.dumps(data or {}), db.now()),
    )
    return cur.lastrowid


def obsolete_decisions(
    conn: sqlite3.Connection, fid: int, kinds: tuple[str, ...], node_id: str | None = None,
    statuses: tuple[str, ...] = ("open",),
) -> None:
    q = (
        f"UPDATE decisions SET status = 'obsolete', resolved = ? WHERE file_id = ?"
        f" AND kind IN ({','.join('?' * len(kinds))})"
        f" AND status IN ({','.join('?' * len(statuses))})"
    )
    args: list = [db.now(), fid, *kinds, *statuses]
    if node_id is not None:
        q += " AND node_id = ?"
        args.append(node_id)
    conn.execute(q, args)


# --- versions ----------------------------------------------------------------


@dataclass
class Version:
    hash: str
    mtime_ns: int
    tie: bool  # several hashes share the newest mtime


def current_version(conn: sqlite3.Connection, fid: int) -> Version | None:
    rows = conn.execute(
        "SELECT hash, mtime_ns FROM copies WHERE file_id = ? AND state = 'present'"
        " ORDER BY mtime_ns DESC, hash",
        (fid,),
    ).fetchall()
    if not rows:
        return None
    top = rows[0]
    tie = any(r["mtime_ns"] == top["mtime_ns"] and r["hash"] != top["hash"] for r in rows)
    return Version(top["hash"], top["mtime_ns"], tie)


def evaluate_divergence(conn: sqlite3.Connection, fid: int) -> None:
    """Keep the ``divergent`` decision for a file in sync with its copies."""
    rows = conn.execute(
        "SELECT node_id, hash FROM copies WHERE file_id = ? AND state = 'present'"
        " ORDER BY node_id",
        (fid,),
    ).fetchall()
    hashes = {r["hash"] for r in rows}
    existing = conn.execute(
        "SELECT id, data FROM decisions WHERE file_id = ? AND kind = 'divergent'"
        " AND status IN ('open', 'dismissed')",
        (fid,),
    ).fetchall()
    if len(hashes) <= 1:
        obsolete_decisions(conn, fid, ("divergent",), statuses=("open", "dismissed"))
        return
    signature = [[r["node_id"], r["hash"]] for r in rows]
    for d in existing:
        if json.loads(d["data"]).get("signature") == signature:
            return  # unchanged since raised (or since the user kept it as is)
    obsolete_decisions(conn, fid, ("divergent",), statuses=("open", "dismissed"))
    cur = current_version(conn, fid)
    assert cur is not None
    add_decision(
        conn, "divergent", fid, None,
        {
            "signature": signature,
            "current_hash": cur.hash,
            "tie": cur.tie,
            "current_nodes": [r["node_id"] for r in rows if r["hash"] == cur.hash],
            "stale_nodes": [r["node_id"] for r in rows if r["hash"] != cur.hash],
        },
    )


def add_wanted(
    conn: sqlite3.Connection, op: str, fid: int, src: str | None, dst: str | None,
    reason: str, data: dict | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO wanted(op, file_id, src_node, dst_node, data, reason, created)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (op, fid, src, dst, json.dumps(data or {}), reason, db.now()),
    )
    return cur.lastrowid
