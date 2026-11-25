"""Replicating the change log between paired servers (design section 7).

Each server pulls the log of each peer from a cursor (the peer's local
sequence number). Entries carry their origin server and per-origin sequence
number. Applied entries are appended to our own log too, so they are passed
on to servers that never talk to the origin directly. An entry is applied
once: ``origin_progress`` remembers the last sequence applied per origin.

Authority rules:

* ``copy`` and ``mark`` entries are applied only if their origin owns the
  node (the owner is the only one that observes it);
* ``node`` entries are applied if they come from the node's owner; an
  ownership change wins if its ``owner_since`` is newer;
* ``tag`` entries merge by last writer wins (timestamp, then server id).
"""

from __future__ import annotations

import json
import sqlite3

from . import apps, archives, db, index, tolerance

BATCH = 2000


def export_changes(conn: sqlite3.Connection, since: int, limit: int = BATCH,
                   exclude_origin: str | None = None,
                   have: dict[str, int] | None = None) -> dict:
    """Log entries after ``since`` that the asker lacks.

    ``have`` is what the asker already holds per origin server: entries it has are not
    sent. That matters when servers are paired in a triangle: the new partner already got
    everything through the common one, and without this the whole log (millions of entries
    for a big index) would be transferred just to be skipped.
    """
    where, args = ["seq > ?"], [since]
    if exclude_origin:
        where.append("origin_id != ?")
        args.append(exclude_origin)
    for origin, seq in (have or {}).items():
        where.append("NOT (origin_id = ? AND origin_seq <= ?)")
        args += [origin, int(seq)]
    rows = conn.execute(
        f"SELECT * FROM changes WHERE {' AND '.join(where)} ORDER BY seq LIMIT ?",
        (*args, limit + 1)).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    if rows and more:
        last = rows[-1]["seq"]
    else:   # nothing (more) to send: everything up to the end of the log was looked at
        last = conn.execute("SELECT IFNULL(MAX(seq), 0) FROM changes").fetchone()[0]
        last = max(last, since)
    out = []
    for r in rows:
        if r["ref_file"] is not None:
            data = _copy_state(conn, r["ref_file"], r["ref_node"])
            if data is None:
                continue    # the copy is gone since: a later entry says so
        else:
            data = json.loads(r["data"])
        out.append({"origin_id": r["origin_id"], "origin_seq": r["origin_seq"],
                    "kind": r["kind"], "data": data, "ts": r["ts"]})
    return {"changes": out, "last_seq": last, "more": more}


def _copy_state(conn: sqlite3.Connection, fid: int, node_id: str) -> dict | None:
    """What a log entry that points at a copy says: its state now."""
    r = conn.execute(
        "SELECT f.vpath, c.size, c.mtime_ns, c.hash, c.state FROM copies c JOIN vfiles f"
        " ON f.id = c.file_id WHERE c.file_id = ? AND c.node_id = ?", (fid, node_id)).fetchone()
    if r is None:
        return None
    return {"node_id": node_id, "vpath": r["vpath"],
            "op": "upsert" if r["state"] == "present" else "missing",
            "size": r["size"], "mtime_ns": r["mtime_ns"], "hash": r["hash"]}


def progress_vector(conn: sqlite3.Connection) -> dict[str, int]:
    """What we hold of each origin's log (including our own entries)."""
    have = {r[0]: r[1] for r in conn.execute("SELECT origin_id, last_seq FROM origin_progress")}
    me = index.self_id(conn)
    have[me] = conn.execute("SELECT IFNULL(MAX(origin_seq), 0) FROM changes WHERE origin_id = ?",
                            (me,)).fetchone()[0]
    return have


APPLY_CHUNK = 200   # entries per transaction: the write lock is never held for long


def apply_changes(conn: sqlite3.Connection, entries: list[dict]) -> int:
    """Apply a batch from a peer. Returns how many entries were new.

    In small transactions (progress per origin is stored inside each, so a
    stop halfway just resumes): one big one starved every other writer.
    """
    return sum(_apply_chunk(conn, entries[i:i + APPLY_CHUNK])
               for i in range(0, len(entries), APPLY_CHUNK))


def _apply_chunk(conn: sqlite3.Connection, entries: list[dict]) -> int:
    me = index.self_id(conn)
    applied = 0
    touched: set[int] = set()
    with db.transaction(conn, "applying changes from a peer"):
        progress = {r[0]: r[1] for r in conn.execute("SELECT * FROM origin_progress")}
        for e in entries:
            e = db.times_from_iso(e)       # peers send times as text; here they are numbers
            origin, seq = e["origin_id"], int(e["origin_seq"])
            if origin == me or seq <= progress.get(origin, 0):
                continue
            kind, data = e["kind"], e["data"]
            fid = None
            if kind == "node":
                _apply_node(conn, me, origin, data)
            elif kind == "copy":
                fid = _apply_copy(conn, me, origin, data)
                if fid is not None:
                    touched.add(fid)
            elif kind == "mark":
                _apply_mark(conn, me, origin, data)
            elif kind == "tag":
                _apply_tag(conn, data)
            elif kind == "vdir":
                _apply_vdir(conn, data)
            elif kind == "dirpref":
                _apply_dirpref(conn, data)
            elif kind == "archive":
                archives.apply_change(conn, data)
            elif kind == "tolerance":
                tolerance.apply_change(conn, data)
            elif kind == "app":
                apps.apply_change(conn, data)
            ref = None
            if kind == "copy" and data.get("op") in ("upsert", "missing") and fid is not None \
                    and conn.execute("SELECT 1 FROM copies WHERE file_id = ? AND node_id = ?",
                                     (fid, data["node_id"])).fetchone():
                ref = (fid, data["node_id"])   # forwarded as a pointer to what we now hold
            index.append_change(conn, kind, data, origin_id=origin, origin_seq=seq,
                                ts=e.get("ts"), ref=ref)
            progress[origin] = seq
            conn.execute(
                "INSERT INTO origin_progress(origin_id, last_seq) VALUES (?, ?)"
                " ON CONFLICT(origin_id) DO UPDATE SET last_seq = excluded.last_seq",
                (origin, seq))
            applied += 1
        for fid in touched:
            if conn.execute("SELECT 1 FROM files WHERE id = ?", (fid,)).fetchone():
                index.evaluate_divergence(conn, fid)
    return applied


# --- per kind ---------------------------------------------------------------------


def _ensure_server(conn, server_id: str, name: str | None) -> None:
    conn.execute("INSERT OR IGNORE INTO servers(id, name) VALUES (?, ?)",
                 (server_id, name or server_id[:8]))
    if name:
        conn.execute("UPDATE servers SET name = ? WHERE id = ? AND is_self = 0",
                     (name, server_id))


def _unique_name(conn, name: str, node_id: str, owner_name: str | None) -> str:
    """Node names are unique; disambiguate another server's node by its owner."""
    candidate = name
    n = 1
    while conn.execute("SELECT 1 FROM nodes WHERE name = ? AND id != ?",
                       (candidate, node_id)).fetchone():
        candidate = f"{name}@{owner_name or 'peer'}" + (f"-{n}" if n > 1 else "")
        n += 1
    return candidate


def _ensure_node(conn, node_id: str, owner: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        # A copy arrived before the node's announcement: placeholder until then.
        _ensure_server(conn, owner, None)
        conn.execute(
            "INSERT INTO nodes(id, name, kind, owner_id, owner_since) VALUES (?, ?, 'storage', ?, 0)",
            (node_id, _unique_name(conn, f"node-{node_id[:8]}", node_id, None), owner))
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return row


def _apply_node(conn, me: str, origin: str, d: dict) -> None:
    if d.get("owner_id") != origin:
        return  # only the owner announces its node
    _ensure_server(conn, origin, d.get("owner_name"))
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (d["id"],)).fetchone()
    name = _unique_name(conn, d["name"], d["id"], d.get("owner_name"))
    fields = (name, d.get("kind", "storage"), d["owner_id"], d.get("owner_since") or 0,
              d.get("case_sensitive"), d.get("restricted_names"), int(d.get("priority") or 0),
              int(d.get("replication_target", 1)), int(d.get("online") or 0),
              d.get("free_bytes"), d.get("total_bytes"), d.get("ssh_root"),
              int(d.get("mirrored") or 0), int(d.get("backup_first") or 0))
    if row is None:
        conn.execute(
            "INSERT INTO nodes(name, kind, owner_id, owner_since, case_sensitive,"
            " restricted_names, priority, replication_target, online, free_bytes, total_bytes,"
            " ssh_root, mirrored, backup_first, id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*fields, d["id"]))
        return
    if row["owner_id"] != origin and (d.get("owner_since") or 0) <= (row["owner_since"] or 0):
        return  # stale announcement from a former owner
    if row["owner_id"] == me and origin != me:
        # Another server adopted our node (newer ownership): we stop managing it.
        conn.execute("UPDATE nodes SET root_path = NULL WHERE id = ?", (d["id"],))
    conn.execute(
        "UPDATE nodes SET name = ?, kind = ?, owner_id = ?, owner_since = ?,"
        " case_sensitive = ?, restricted_names = ?, priority = ?, replication_target = ?,"
        " online = ?, free_bytes = ?, total_bytes = ?, ssh_root = ?, mirrored = ?,"
        " backup_first = ? WHERE id = ?",
        (*fields, d["id"]))


def _authoritative(conn, me: str, origin: str, node_id: str) -> bool:
    row = _ensure_node(conn, node_id, origin)
    return row["owner_id"] == origin and row["owner_id"] != me


def _apply_copy(conn, me: str, origin: str, d: dict) -> int | None:
    node_id, vpath, op = d["node_id"], d["vpath"], d["op"]
    if not _authoritative(conn, me, origin, node_id):
        return None
    if op == "upsert":
        fid = index.ensure_file(conn, vpath)
        ts = db.now()
        conn.execute(
            "INSERT INTO copies(file_id, node_id, size, mtime_ns, hash_b, first_seen, first_scan,"
            " hashed_at) VALUES (?, ?, ?, ?, hb(?), ?, 0, ?) ON CONFLICT(file_id, node_id)"
            " DO UPDATE SET size = excluded.size, mtime_ns = excluded.mtime_ns,"
            " hash_b = excluded.hash_b, state = 'present', hashed_at = excluded.hashed_at,"
            # Same rule as on the owner: changed content loses its deletion mark.
            " marked_delete = CASE WHEN copies.hash_b = excluded.hash_b THEN copies.marked_delete"
            " ELSE 0 END",
            (fid, node_id, d["size"], d["mtime_ns"], d["hash"], ts, ts))
        return fid
    fid = index.file_id(conn, vpath)
    if fid is None:
        return None
    if op == "missing":
        conn.execute("UPDATE copies SET state = 'missing' WHERE file_id = ? AND node_id = ?",
                     (fid, node_id))
        return fid
    if op == "remove":
        conn.execute("DELETE FROM copies WHERE file_id = ? AND node_id = ?", (fid, node_id))
        if index.drop_file_if_orphan(conn, fid):
            return None
        return fid
    return None


def _apply_mark(conn, me: str, origin: str, d: dict) -> None:
    if not _authoritative(conn, me, origin, d["node_id"]):
        return
    fid = index.file_id(conn, d["vpath"])
    if fid is not None:
        conn.execute("UPDATE copies SET marked_delete = ? WHERE file_id = ? AND node_id = ?",
                     (int(d["marked"]), fid, d["node_id"]))


def _newer(a: tuple[str, str], b: tuple[str, str]) -> bool:
    return a > b  # (timestamp, server id): deterministic everywhere


def _apply_dirpref(conn, d: dict) -> None:
    row = conn.execute("SELECT updated, updated_by FROM dirprefs WHERE vpath = ?",
                       (d["vpath"],)).fetchone()
    if row is None or _newer((d["updated"], d["updated_by"]), (row[0], row[1])):
        conn.execute("INSERT OR REPLACE INTO dirprefs(vpath, priority, updated, updated_by)"
                     " VALUES (?, ?, ?, ?)",
                     (d["vpath"], d["priority"], d["updated"], d["updated_by"]))


def _apply_vdir(conn, d: dict) -> None:
    row = conn.execute("SELECT updated, updated_by FROM vdirs WHERE vpath = ?",
                       (d["vpath"],)).fetchone()
    if row is None or _newer((d["updated"], d["updated_by"]), (row[0], row[1])):
        conn.execute("INSERT OR REPLACE INTO vdirs(vpath, present, updated, updated_by)"
                     " VALUES (?, ?, ?, ?)",
                     (d["vpath"], int(d["present"]), d["updated"], d["updated_by"]))


def _apply_tag(conn, d: dict) -> None:
    incoming = (d["updated"], d["updated_by"])
    fid = index.file_id(conn, d["vpath"])
    if fid is None:
        row = conn.execute("SELECT updated, updated_by FROM orphan_tags WHERE vpath = ? AND tag = ?",
                           (d["vpath"], d["tag"])).fetchone()
        if row is None or _newer(incoming, (row[0], row[1])):
            conn.execute(
                "INSERT OR REPLACE INTO orphan_tags(vpath, tag, present, updated, updated_by)"
                " VALUES (?, ?, ?, ?, ?)",
                (d["vpath"], d["tag"], int(d["present"]), d["updated"], d["updated_by"]))
        return
    row = conn.execute("SELECT updated, updated_by FROM tags WHERE file_id = ? AND tag = ?",
                       (fid, d["tag"])).fetchone()
    if row is None or _newer(incoming, (row[0], row[1])):
        conn.execute(
            "INSERT OR REPLACE INTO tags(file_id, tag, present, updated, updated_by)"
            " VALUES (?, ?, ?, ?, ?)",
            (fid, d["tag"], int(d["present"]), d["updated"], d["updated_by"]))
