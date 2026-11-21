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
    fid = cur.lastrowid
    # Tags a peer sent before the file reached us.
    conn.execute(
        "INSERT OR REPLACE INTO tags(file_id, tag, present, updated, updated_by)"
        " SELECT ?, tag, present, updated, updated_by FROM orphan_tags WHERE vpath = ?",
        (fid, vpath))
    conn.execute("DELETE FROM orphan_tags WHERE vpath = ?", (vpath,))
    return fid


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


# --- change log ---------------------------------------------------------------
#
# Every fact this server is authoritative for is appended to ``changes`` with
# origin = this server and a per-origin sequence number. Peers pull the log
# (see sync.py). Kinds:
#
#   copy  {node_id, vpath, op: upsert|missing|remove, size, mtime_ns, hash}
#   node  {id, name, kind, owner_id, owner_name, owner_since, ...settings}
#   tag   {vpath, tag, present, updated, updated_by}
#   mark  {node_id, vpath, marked}


def self_id(conn: sqlite3.Connection) -> str:
    return db.get_meta(conn, "server_id") or ""


def append_change(conn: sqlite3.Connection, kind: str, data: dict,
                  origin_id: str | None = None, origin_seq: int | None = None,
                  ts: str | None = None) -> None:
    """Append to the log: our own change (origin None) or one learned from a peer."""
    if origin_id is None:
        origin_id = self_id(conn)
        origin_seq = conn.execute(
            "SELECT IFNULL(MAX(origin_seq), 0) + 1 FROM changes WHERE origin_id = ?",
            (origin_id,)).fetchone()[0]
    conn.execute(
        "INSERT OR IGNORE INTO changes(origin_id, origin_seq, kind, data, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        (origin_id, origin_seq, kind, json.dumps(data, separators=(",", ":")), ts or db.now()),
    )


def log_change(
    conn: sqlite3.Connection, node_id: str, vpath: str, op: str,
    size: int | None = None, mtime_ns: int | None = None, hash_: str | None = None,
) -> None:
    append_change(conn, "copy", {"node_id": node_id, "vpath": vpath, "op": op, "size": size,
                                 "mtime_ns": mtime_ns, "hash": hash_})


def node_payload(conn: sqlite3.Connection, node_id: str) -> dict | None:
    r = conn.execute(
        "SELECT n.*, s.name AS owner_name FROM nodes n LEFT JOIN servers s ON s.id = n.owner_id"
        " WHERE n.id = ?", (node_id,)).fetchone()
    if r is None:
        return None
    return {"id": r["id"], "name": r["name"], "kind": r["kind"], "owner_id": r["owner_id"],
            "owner_name": r["owner_name"], "owner_since": r["owner_since"],
            "case_sensitive": r["case_sensitive"], "restricted_names": r["restricted_names"],
            "priority": r["priority"], "replication_target": r["replication_target"],
            "online": r["online"], "free_bytes": r["free_bytes"],
            "total_bytes": r["total_bytes"], "ssh_root": r["ssh_root"]}


def log_node(conn: sqlite3.Connection, node_id: str) -> None:
    """Announce the current state of a node we own."""
    payload = node_payload(conn, node_id)
    if payload and payload["owner_id"] == self_id(conn):
        append_change(conn, "node", payload)


def log_tag(conn: sqlite3.Connection, fid: int, tag: str) -> None:
    r = conn.execute(
        "SELECT f.vpath, t.present, t.updated, t.updated_by FROM tags t"
        " JOIN files f ON f.id = t.file_id WHERE t.file_id = ? AND t.tag = ?",
        (fid, tag)).fetchone()
    if r:
        append_change(conn, "tag", {"vpath": r["vpath"], "tag": tag, "present": r["present"],
                                    "updated": r["updated"], "updated_by": r["updated_by"]})


def log_vdir(conn: sqlite3.Connection, vpath: str) -> None:
    r = conn.execute("SELECT present, updated, updated_by FROM vdirs WHERE vpath = ?",
                     (vpath,)).fetchone()
    if r:
        append_change(conn, "vdir", {"vpath": vpath, "present": r["present"],
                                     "updated": r["updated"], "updated_by": r["updated_by"]})


def log_dirpref(conn: sqlite3.Connection, vpath: str) -> None:
    r = conn.execute("SELECT priority, updated, updated_by FROM dirprefs WHERE vpath = ?",
                     (vpath,)).fetchone()
    if r:
        append_change(conn, "dirpref", {"vpath": vpath, "priority": r["priority"],
                                        "updated": r["updated"], "updated_by": r["updated_by"]})


def log_mark(conn: sqlite3.Connection, fid: int, node_id: str) -> None:
    r = conn.execute("SELECT marked_delete FROM copies WHERE file_id = ? AND node_id = ?",
                     (fid, node_id)).fetchone()
    if r is not None:
        append_change(conn, "mark", {"node_id": node_id, "vpath": vpath_of(conn, fid),
                                     "marked": r[0]})


def set_marked(conn: sqlite3.Connection, fid: int, node_id: str, marked: bool) -> bool:
    cur = conn.execute(
        "UPDATE copies SET marked_delete = ? WHERE file_id = ? AND node_id = ?"
        " AND marked_delete != ?", (int(marked), fid, node_id, int(marked)))
    if cur.rowcount:
        log_mark(conn, fid, node_id)
    return bool(cur.rowcount)


def owner_of(conn: sqlite3.Connection, node_id: str) -> str | None:
    r = conn.execute("SELECT owner_id FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return r[0] if r else None


def queue_peer_call(conn: sqlite3.Connection, server_id: str, path: str, body: dict) -> None:
    """Queue a POST to a peer; the sync thread delivers it (with retries)."""
    conn.execute("INSERT INTO outbox(server_id, path, body, created) VALUES (?, ?, ?, ?)",
                 (server_id, path, json.dumps(body), db.now()))


def mark_copy(conn: sqlite3.Connection, fid: int, node_id: str, marked: bool,
              expect_hash: str | None = None) -> None:
    """Set the deletion mark of a copy: directly if we own the node, otherwise by
    asking its owner (only the owner writes facts about its node)."""
    owner = owner_of(conn, node_id)
    if owner == self_id(conn):
        if expect_hash is None or conn.execute(
                "SELECT 1 FROM copies WHERE file_id = ? AND node_id = ? AND hash = ?",
                (fid, node_id, expect_hash)).fetchone():
            set_marked(conn, fid, node_id, marked)
    elif owner:
        queue_peer_call(conn, owner, "/api/peer/mark",
                        {"node_id": node_id, "vpath": vpath_of(conn, fid), "marked": marked,
                         "hash": expect_hash})


def owned_node_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT id FROM nodes WHERE owner_id = ?",
                                       (self_id(conn),))}


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
    """Keep the ``divergent`` decision for a file in sync with its copies.

    Each server raises it only for stale copies on nodes it owns (it is the
    one able to update them), so peers never raise duplicates.
    """
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
    cur = current_version(conn, fid) if len(hashes) > 1 else None
    owned = owned_node_ids(conn)
    if cur is None or not any(r["hash"] != cur.hash and r["node_id"] in owned for r in rows):
        obsolete_decisions(conn, fid, ("divergent",), statuses=("open", "dismissed"))
        return
    signature = [[r["node_id"], r["hash"]] for r in rows]
    for d in existing:
        if json.loads(d["data"]).get("signature") == signature:
            return  # unchanged since raised (or since the user kept it as is)
    obsolete_decisions(conn, fid, ("divergent",), statuses=("open", "dismissed"))
    add_decision(
        conn, "divergent", fid, None,
        {
            "signature": signature,
            "current_hash": cur.hash,
            "tie": cur.tie,
            "current_nodes": [r["node_id"] for r in rows if r["hash"] == cur.hash],
            "stale_nodes": [r["node_id"] for r in rows
                            if r["hash"] != cur.hash and r["node_id"] in owned],
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


def copy_tags(conn: sqlite3.Connection, from_fid: int, to_fid: int) -> None:
    """Copy tag state (incl. tombstones) from one file to another; newest edit wins."""
    conn.execute(
        "INSERT INTO tags(file_id, tag, present, updated, updated_by)"
        " SELECT ?, tag, present, updated, updated_by FROM tags WHERE file_id = ?"
        " ON CONFLICT(file_id, tag) DO UPDATE SET present = excluded.present,"
        " updated = excluded.updated, updated_by = excluded.updated_by"
        " WHERE excluded.updated > tags.updated",
        (to_fid, from_fid),
    )
    for (tag,) in conn.execute("SELECT tag FROM tags WHERE file_id = ?", (to_fid,)).fetchall():
        log_tag(conn, to_fid, tag)


def record_copy(conn: sqlite3.Connection, fid: int, node_id: str, size: int,
                mtime_ns: int, hash_: str) -> None:
    """Record a copy observed outside a scan (after a transfer or rename).

    ``first_scan`` is set to the node's last completed scan so the copy is
    not mistaken for the target of a user rename by the next scan.
    """
    row = conn.execute("SELECT last_scan_id FROM nodes WHERE id = ?", (node_id,)).fetchone()
    ts = db.now()
    conn.execute(
        "INSERT INTO copies(file_id, node_id, size, mtime_ns, hash, first_seen, first_scan,"
        " hashed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(file_id, node_id) DO UPDATE"
        " SET size = excluded.size, mtime_ns = excluded.mtime_ns, hash = excluded.hash,"
        " state = 'present', hashed_at = excluded.hashed_at,"
        " marked_delete = CASE WHEN copies.hash = excluded.hash THEN copies.marked_delete"
        " ELSE 0 END",
        (fid, node_id, size, mtime_ns, hash_, ts, (row[0] if row else None) or 0, ts),
    )
    log_change(conn, node_id, vpath_of(conn, fid), "upsert", size, mtime_ns, hash_)
    obsolete_decisions(conn, fid, ("missing",), node_id)
    evaluate_divergence(conn, fid)
