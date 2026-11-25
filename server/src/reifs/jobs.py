"""Creating and managing jobs (design section 5).

A job is expanded into items when it is created. Each item is
``(op, vpath, source node, destination node)``. Items are executed by
``runner.run_jobs``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from . import archives, db, index, nodes, paths, vdirs

TERMINAL_ITEM_STATES = ("done", "failed", "skipped", "cancelled")


class JobError(Exception):
    pass


# --- selection ----------------------------------------------------------------


def select_files(conn: sqlite3.Connection, vpaths: list[str],
                 tag: str | None = None) -> list[tuple[int, str]]:
    """Resolve selectors into ``(file id, vpath)``.

    A vpath selects that file, or every file below it if it is a directory.
    A tag selects every file carrying it. Several selectors are combined.
    """
    selected: dict[int, str] = {}
    for raw in vpaths:
        vpath = paths.normalize(raw)
        fid = index.file_id(conn, vpath)
        if fid is not None:
            selected[fid] = vpath
            continue
        packed = archives.get(conn, vpath)
        if packed is not None:      # a packed directory is copied as its archive
            selected[packed["file_id"]] = archives.archive_vpath(vpath)
            continue
        cond, args = paths.subtree_sql(vpath)
        rows = conn.execute(f"SELECT f.id, f.vpath FROM vfiles f WHERE {cond}", args).fetchall()
        if not rows:
            if vdirs.exists(conn, vpath):
                raise JobError(f"{vpath} is a folder without files, there is nothing to copy")
            raise JobError(f"not in the virtual tree: {vpath}")
        selected.update({r[0]: r[1] for r in rows})
    if tag:
        rows = conn.execute(
            "SELECT f.id, f.vpath FROM vfiles f JOIN tags t ON t.file_id = f.id"
            " WHERE t.tag = ? AND t.present = 1",
            (tag,),
        ).fetchall()
        if not rows:
            raise JobError(f"no files tagged {tag!r}")
        selected.update({r[0]: r[1] for r in rows})
    return sorted(selected.items(), key=lambda x: x[1])


class RemoteDestination(JobError):
    """The destination node belongs to another server: the job must be created
    there (the destination's owner runs transfers into its nodes)."""

    def __init__(self, node: sqlite3.Row):
        super().__init__(f"node {node['name']!r} belongs to another server")
        self.owner_id = node["owner_id"]


def destination_owner(conn: sqlite3.Connection, name: str) -> str:
    return nodes.get_node(conn, name)["owner_id"]


def _owned_node(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    node = nodes.get_node(conn, name)
    if node["owner_id"] != nodes.self_id(conn):
        raise RemoteDestination(node)
    return node


def describe(vpaths: list[str], tag: str | None) -> str:
    parts = list(vpaths) + ([f"tag:{tag}"] if tag else [])
    text = " ".join(parts)
    return text if len(text) <= 80 else text[:77] + "..."


@dataclass
class CreateReport:
    job_id: int | None
    items: int
    excluded: dict[str, list[str]] = field(default_factory=dict)  # reason -> vpaths


def create_transfer_job(conn: sqlite3.Connection, vpaths: list[str], tag: str | None,
                        dst: str, src: str | None = None, move: bool = False) -> CreateReport:
    if move and not src:
        raise JobError("a move needs a source node (--from)")
    if not vpaths and not tag:
        raise JobError("select files by path and/or --tag")
    dst_node = _owned_node(conn, dst)
    src_node = nodes.get_node(conn, src) if src else None  # may be on a paired server
    if src_node is not None and src_node["id"] == dst_node["id"]:
        raise JobError("source and destination are the same node")

    report = CreateReport(None, 0)

    def exclude(reason: str, vpath: str) -> None:
        report.excluded.setdefault(reason, []).append(vpath)

    items = []
    for fid, vpath in select_files(conn, vpaths, tag):
        version = index.current_version(conn, fid)
        if version is None:
            exclude("no present copy anywhere", vpath)
            continue
        copies = {r["node_id"]: r for r in conn.execute(
            "SELECT node_id, hash, size, state FROM copies WHERE file_id = ?", (fid,))}
        on_dst = copies.get(dst_node["id"])
        dst_current = on_dst is not None and on_dst["state"] == "present" \
            and on_dst["hash"] == version.hash
        if src_node is not None:
            c = copies.get(src_node["id"])
            if c is None or c["state"] != "present":
                exclude(f"not on {src_node['name']}", vpath)
                continue
            if c["hash"] != version.hash:
                exclude(f"the copy on {src_node['name']} is not the current version", vpath)
                continue
        if dst_current and not move:
            exclude(f"already on {dst_node['name']}", vpath)
            continue
        size = next(r["size"] for r in copies.values() if r["hash"] == version.hash)
        items.append((vpath, size))

    # What was left out for a reason that means "not copied" is kept in the job
    # as a skipped item, so it is visible there and the job does not read "done".
    left_out = [(vpath, reason) for reason, vpaths in report.excluded.items()
                if not reason.startswith("already on") for vpath in vpaths]
    if not items and not left_out:
        return report
    kind = "move" if move else "copy"
    desc = f"{kind} {describe(vpaths, tag)}" + (f" from {src_node['name']}" if src_node else "") \
        + f" to {dst_node['name']}"
    with db.transaction(conn):
        job_id = _new_job(conn, kind, desc)
        ts = db.now()
        conn.executemany(
            "INSERT INTO job_items(job_id, op, vpath, src_node, dst_node, size, mark_source,"
            " updated) VALUES (?, 'copy', ?, ?, ?, ?, ?, ?)",
            [(job_id, vpath, src_node["id"] if src_node else None, dst_node["id"], size,
              int(move), ts) for vpath, size in items],
        )
        conn.executemany(
            "INSERT INTO job_items(job_id, op, vpath, src_node, dst_node, size, mark_source,"
            " state, message, updated) VALUES (?, 'copy', ?, ?, ?, 0, ?, 'skipped', ?, ?)",
            [(job_id, vpath, src_node["id"] if src_node else None, dst_node["id"], int(move),
              f"not copied: {reason}", ts) for vpath, reason in left_out],
        )
        if not items:
            refresh_status(conn, job_id)
    report.job_id, report.items = job_id, len(items)
    return report


def server_node_id(conn: sqlite3.Connection) -> str:
    """This server's own node (where fetched files are put)."""
    row = conn.execute("SELECT id FROM nodes WHERE kind = 'server' AND owner_id = ?",
                       (nodes.self_id(conn),)).fetchone()
    if row is None:
        raise JobError("this server has no node of its own to fetch into")
    return row[0]


def create_fetch_job(conn: sqlite3.Connection, vpaths: list[str], tag: str | None) -> CreateReport:
    """Download files (or everything below a folder) into the ``fetch`` folder at the root of
    this server's node. A plain download: nothing is indexed there and rei-fs does not
    touch it otherwise. The source is any node that can be read, possibly through
    other servers (relay.py)."""
    if not vpaths and not tag:
        raise JobError("select files by path and/or --tag")
    dst = server_node_id(conn)
    report = CreateReport(None, 0)
    items, left_out = [], []
    for fid, vpath in select_files(conn, vpaths, tag):
        version = index.current_version(conn, fid)
        if version is None:
            left_out.append((vpath, "no present copy anywhere"))
            continue
        size = conn.execute("SELECT size FROM copies WHERE file_id = ? AND hash = ? LIMIT 1",
                            (fid, version.hash)).fetchone()[0]
        items.append((vpath, size))
    if not items and not left_out:
        return report
    with db.transaction(conn):
        job_id = _new_job(conn, "wanted", f"fetch {describe(vpaths, tag)}")
        ts = db.now()
        conn.executemany(
            "INSERT INTO job_items(job_id, op, vpath, dst_node, size, fetch, updated)"
            " VALUES (?, 'copy', ?, ?, ?, 1, ?)", [(job_id, v, dst, sz, ts) for v, sz in items])
        conn.executemany(
            "INSERT INTO job_items(job_id, op, vpath, dst_node, size, fetch, state, message,"
            " updated) VALUES (?, 'copy', ?, ?, 0, 1, 'skipped', ?, ?)",
            [(job_id, v, dst, f"not fetched: {why}", ts) for v, why in left_out])
        if not items:
            refresh_status(conn, job_id)
    report.job_id, report.items = job_id, len(items)
    return report


def create_relay_job(conn: sqlite3.Connection, rid: str, node_id: str, vpath: str, hash_: str,
                     size: int, requester: str) -> int:
    """Stage a file for another server (inside the caller's transaction)."""
    name = (conn.execute("SELECT name FROM servers WHERE id = ?", (requester,)).fetchone()
            or [requester[:8]])[0]
    job_id = _new_job(conn, "wanted", f"relay {vpath} for {name}")
    conn.execute(
        "INSERT INTO job_items(job_id, op, vpath, src_node, dst_node, size, allow_hash, fetch,"
        " serves, updated) VALUES (?, 'copy', ?, ?, ?, ?, ?, 2, ?, ?)",
        (job_id, vpath, node_id, server_node_id(conn), size, hash_, rid, db.now()))
    return job_id


def create_tool_job(conn: sqlite3.Connection, kind: str, description: str,
                    items: list[tuple], hidden: bool = False) -> int:
    """A job of operations that are not transfers (scan, pack, unpack, clean): each item is
    ``(op, vpath, node_id, params)`` and runs on a node of this server. The caller holds the
    transaction."""
    job_id = _new_job(conn, kind, description)
    if hidden:
        conn.execute("UPDATE jobs SET hidden = 1 WHERE id = ?", (job_id,))
    ts = db.now()
    conn.executemany(
        "INSERT INTO job_items(job_id, op, vpath, dst_node, size, params, updated)"
        " VALUES (?, ?, ?, ?, 0, ?, ?)",
        [(job_id, op, vpath, node_id, json.dumps(params), ts)
         for op, vpath, node_id, params in items])
    return job_id


def active_tool_item(conn: sqlite3.Connection, op: str, vpath: str, node_id: str) -> int | None:
    """The job already about to do the same thing, if any."""
    r = conn.execute(
        "SELECT i.job_id FROM job_items i JOIN jobs j ON j.id = i.job_id WHERE i.op = ?"
        " AND i.vpath = ? AND i.dst_node = ? AND i.state IN ('pending', 'running')"
        " AND j.status IN ('pending', 'running', 'waiting')", (op, vpath, node_id)).fetchone()
    return r[0] if r else None


def _new_job(conn: sqlite3.Connection, kind: str, description: str) -> int:
    return conn.execute(
        "INSERT INTO jobs(kind, description, created) VALUES (?, ?, ?)",
        (kind, description, db.now()),
    ).lastrowid


# --- wanted (proposed) jobs ---------------------------------------------------


def _wanted_rows(conn, ids: list[int] | None) -> list[sqlite3.Row]:
    q = ("SELECT w.*, f.vpath FROM wanted w LEFT JOIN vfiles f ON f.id = w.file_id"
         " WHERE w.status = 'open'")
    rows = conn.execute(q + " ORDER BY w.id").fetchall()
    if ids is None:
        return rows
    found = {r["id"]: r for r in rows}
    missing = [i for i in ids if i not in found]
    if missing:
        raise JobError(f"no open wanted job(s): {', '.join(map(str, missing))}")
    return [found[i] for i in ids]


APPROVE_CHUNK = 500   # proposals turned into job items per transaction


def approve_wanted(conn: sqlite3.Connection, ids: list[int] | None) -> CreateReport:
    """Turn open wanted entries (all if ids is None) into one job.

    Done in short transactions: with thousands of proposals one big transaction held the
    database's write lock for so long that everything else (the sync with the other
    servers, the API) gave up waiting for it. What is looked up for an item is read
    before the transaction opens, so it only holds the lock to write.
    """
    report = CreateReport(None, 0)
    rows = _wanted_rows(conn, ids)
    if not rows:
        return report
    job_id: int | None = None
    ts = db.now()
    for start in range(0, len(rows), APPROVE_CHUNK):
        inserts, approved, rejected = [], [], []
        for w in rows[start:start + APPROVE_CHUNK]:
            data = json.loads(w["data"])
            if w["op"] == "rename":
                vpath, new_vpath, allow = data["from"], data["to"], None
            elif w["vpath"] is None:
                rejected.append(w["id"])
                report.excluded.setdefault("file no longer indexed", []).append(f"#{w['id']}")
                continue
            else:
                vpath, new_vpath, allow = w["vpath"], None, None
            size = 0
            if w["op"] in ("copy", "overwrite") and w["file_id"] is not None:
                version = index.current_version(conn, w["file_id"])
                if version:
                    size = conn.execute(
                        "SELECT size FROM copies WHERE file_id = ? AND hash = ? LIMIT 1",
                        (w["file_id"], version.hash)).fetchone()[0]
            if w["op"] == "overwrite":
                # Only the content judged stale at approval time may be replaced.
                cur = conn.execute(
                    "SELECT hash FROM copies WHERE file_id = ? AND node_id = ?",
                    (w["file_id"], w["dst_node"])).fetchone()
                allow = cur[0] if cur else None
            inserts.append((w["op"], vpath, new_vpath,
                            w["src_node"] if w["op"] == "rename" else None,
                            w["dst_node"], size, allow, w["id"]))
            approved.append(w["id"])
        with db.transaction(conn, "approving proposals"):
            if inserts and job_id is None:
                job_id = _new_job(conn, "wanted", f"{len(rows)} approved proposal(s)")
            conn.executemany("UPDATE wanted SET status = 'rejected' WHERE id = ?",
                             [(i,) for i in rejected])
            conn.executemany(
                "INSERT INTO job_items(job_id, op, vpath, new_vpath, src_node, dst_node, size,"
                " allow_hash, wanted_id, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(job_id, op, v, nv, sn, dn, sz, al, wid, ts)
                 for op, v, nv, sn, dn, sz, al, wid in inserts])
            conn.executemany("UPDATE wanted SET status = 'approved' WHERE id = ?",
                             [(i,) for i in approved])
        report.items += len(approved)
    report.job_id = job_id
    return report


def reject_wanted(conn: sqlite3.Connection, ids: list[int] | None) -> int:
    with db.transaction(conn):
        rows = _wanted_rows(conn, ids)
        for w in rows:
            conn.execute("UPDATE wanted SET status = 'rejected' WHERE id = ?", (w["id"],))
    return len(rows)


# --- job lifecycle ------------------------------------------------------------


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise JobError(f"no job #{job_id}")
    return row


def unresolved_skips(conn: sqlite3.Connection, job_id: int) -> int:
    """Skipped items that nobody decided to skip (the user's own "skip" is an answer)."""
    return conn.execute(
        "SELECT COUNT(*) FROM job_items WHERE job_id = ? AND state = 'skipped'"
        " AND IFNULL(message, '') != 'skipped by user'", (job_id,)).fetchone()[0]


def item_counts(conn: sqlite3.Connection, job_id: int) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT state, COUNT(*) FROM job_items WHERE job_id = ? GROUP BY state", (job_id,))}


def refresh_status(conn: sqlite3.Connection, job_id: int, after_run: bool = False) -> str:
    job = get_job(conn, job_id)
    if job["status"] == "cancelled":
        return "cancelled"
    c = item_counts(conn, job_id)
    if c.get("pending") or c.get("running"):
        status = "waiting" if after_run else "pending"
    elif c.get("conflict"):
        status = "waiting"
    elif c.get("failed") or unresolved_skips(conn, job_id):
        # Finished, but not everything was done: never call that "done".
        status = "failed"
    else:
        status = "done"
    finished = db.now() if status in ("done", "failed") else None
    conn.execute("UPDATE jobs SET status = ?, finished = ? WHERE id = ?",
                 (status, finished, job_id))
    if status == "failed":      # a synchronizer job that failed is worth looking at
        conn.execute("UPDATE jobs SET hidden = 0 WHERE id = ?", (job_id,))
    return status


def cancel_job(conn: sqlite3.Connection, job_id: int) -> int:
    with db.transaction(conn):
        job = get_job(conn, job_id)
        if job["status"] in ("done", "cancelled"):
            raise JobError(f"job #{job_id} is already {job['status']}")
        n = conn.execute(
            "UPDATE job_items SET state = 'cancelled', updated = ? WHERE job_id = ?"
            " AND state IN ('pending', 'running', 'conflict')", (db.now(), job_id)).rowcount
        conn.execute(
            "UPDATE decisions SET status = 'obsolete', resolved = ? WHERE kind = 'conflict'"
            " AND status = 'open' AND json_extract(data, '$.job_id') = ?", (db.now(), job_id))
        conn.execute("UPDATE jobs SET status = 'cancelled', finished = ? WHERE id = ?",
                     (db.now(), job_id))
    return n


def retry_job(conn: sqlite3.Connection, job_id: int) -> int:
    """Put failed and skipped items back in the queue."""
    with db.transaction(conn):
        get_job(conn, job_id)
        n = conn.execute(
            "UPDATE job_items SET state = 'pending', attempts = 0, updated = ?"
            " WHERE job_id = ? AND state IN ('failed', 'skipped')", (db.now(), job_id)).rowcount
        if n:
            conn.execute("UPDATE jobs SET status = 'pending', finished = NULL WHERE id = ?",
                         (job_id,))
    return n


# --- deletion marks -----------------------------------------------------------


def marked_totals(conn: sqlite3.Connection, node: str | None = None) -> tuple[int, int]:
    q = ("SELECT COUNT(*), IFNULL(SUM(c.size), 0) FROM copies c JOIN nodes n ON n.id = c.node_id"
         " WHERE c.marked_delete = 1")
    args: list = []
    if node:
        q += " AND (n.name = ? OR n.id = ?)"
        args += [node, node]
    r = conn.execute(q, args).fetchone()
    return r[0], r[1]


def marked_copies(conn: sqlite3.Connection, node: str | None = None,
                  limit: int | None = None) -> list[sqlite3.Row]:
    q = ("SELECT f.vpath, n.name AS node_name, n.root_path, n.online, c.size, c.file_id,"
         " c.node_id FROM copies c JOIN vfiles f ON f.id = c.file_id"
         " JOIN nodes n ON n.id = c.node_id WHERE c.marked_delete = 1")
    args: list = []
    if node:
        q += " AND (n.name = ? OR n.id = ?)"
        args += [node, node]
    q += " ORDER BY n.name, f.vpath"
    if limit:
        q += " LIMIT ?"
        args.append(limit)
    return conn.execute(q, args).fetchall()


def unmark(conn: sqlite3.Connection, vpaths: list[str], node: str | None) -> int:
    """Remove deletion marks (on other servers' nodes, by asking their owner)."""
    node_id = nodes.get_node(conn, node)["id"] if node else None
    with db.transaction(conn):
        ids = {fid for fid, _ in select_files(conn, vpaths)} if vpaths else None
        n = 0
        for r in conn.execute("SELECT file_id, node_id FROM copies WHERE marked_delete = 1"
                              ).fetchall():
            if node_id and r["node_id"] != node_id:
                continue
            if ids is not None and r["file_id"] not in ids:
                continue
            index.mark_copy(conn, r["file_id"], r["node_id"], False)
            n += 1
        return n
