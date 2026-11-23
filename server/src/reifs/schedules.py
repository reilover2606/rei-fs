"""Scheduled syncs: keep a directory of one node copied to another (design section 5.11).

A schedule says "every N minutes make the files under DIRECTORY on node A also be on node
B". It is run by the server owning the destination node, which pulls (like every transfer).
Each run creates an ordinary job: files the destination lacks are copied, and files whose
copy at the destination is an *older version* of the source's are replaced (``update_changed``,
on by default). Nothing is ever deleted at the destination, and a destination copy that is
newer than the source's is left alone (reported in the note).

A run is due when its ``next_due`` time has passed. If a node is not available then (a drive
unplugged, a computer asleep, a server unreachable) nothing is created and the schedule stays
due, so it runs as soon as both nodes are back: one catch-up run, however many were missed.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass

from . import db, index, jobs, nodes, paths, peers

MAX_ITEMS_PER_RUN = 50_000
ACTIVE_JOB = ("pending", "running", "waiting")
CHUNK = 500


class ScheduleError(Exception):
    pass


def parse_interval(text: str | int) -> int:
    """Minutes from ``90``, ``15m``, ``6h``, ``1d``, ``2w`` (or ``hourly``/``daily``/``weekly``)."""
    if isinstance(text, int):
        n = text
    else:
        t = str(text).strip().lower()
        t = {"hourly": "1h", "daily": "1d", "weekly": "1w"}.get(t, t)
        m = re.fullmatch(r"(\d+)\s*([mhdw]?)", t)
        if not m:
            raise ScheduleError(f"not an interval: {text!r} (use for example 30m, 6h, 1d, 1w)")
        n = int(m.group(1)) * {"": 1, "m": 1, "h": 60, "d": 1440, "w": 10080}[m.group(2)]
    if n < 1:
        raise ScheduleError("the interval must be at least one minute")
    return n


def describe_interval(minutes: int) -> str:
    for unit, size in (("w", 10080), ("d", 1440), ("h", 60)):
        if minutes % size == 0:
            return f"{minutes // size}{unit}"
    return f"{minutes}m"


def create(conn: sqlite3.Connection, vpath: str, src: str, dst: str, interval,
           name: str | None = None, update_changed: bool = True) -> int:
    """Define a schedule; the first run is due at once. The destination must be ours."""
    try:
        vpath = paths.normalize(vpath or "/")
    except paths.VPathError as e:
        raise ScheduleError(str(e)) from None
    src_node, dst_node = nodes.get_node(conn, src), nodes.get_node(conn, dst)
    if src_node["id"] == dst_node["id"]:
        raise ScheduleError("source and destination are the same node")
    if dst_node["owner_id"] != nodes.self_id(conn):
        raise jobs.RemoteDestination(dst_node)   # its owner runs it: the caller forwards
    minutes = parse_interval(interval)
    name = (name or f"{src_node['name']} to {dst_node['name']}: {vpath}").strip()
    if conn.execute("SELECT 1 FROM schedules WHERE name = ?", (name,)).fetchone():
        raise ScheduleError(f"a schedule named {name!r} already exists")
    with db.transaction(conn):
        return conn.execute(
            "INSERT INTO schedules(name, vpath, src_node, dst_node, interval_minutes,"
            " update_changed, created, next_due) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, vpath, src_node["id"], dst_node["id"], minutes, int(update_changed),
             db.now(), time.time())).lastrowid


def get(conn: sqlite3.Connection, schedule_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise ScheduleError(f"no schedule #{schedule_id}")
    return row


def listing(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT s.*, a.name AS src_name, b.name AS dst_name FROM schedules s"
            " JOIN nodes a ON a.id = s.src_node JOIN nodes b ON b.id = s.dst_node ORDER BY s.id"):
        job = conn.execute("SELECT status FROM jobs WHERE id = ?", (r["last_job_id"],)).fetchone() \
            if r["last_job_id"] else None
        out.append({
            "id": r["id"], "name": r["name"], "path": r["vpath"], "from": r["src_name"],
            "to": r["dst_name"], "every": describe_interval(r["interval_minutes"]),
            "every_minutes": r["interval_minutes"], "update_changed": bool(r["update_changed"]),
            "enabled": bool(r["enabled"]), "next_due": r["next_due"], "last_run": r["last_run"],
            "last_job": r["last_job_id"], "last_job_status": job[0] if job else "",
            "note": r["last_note"] or "",
            "waiting": (unavailable(conn, r) or "") if r["enabled"] and r["next_due"] <= time.time()
            else ""})
    return out


def update(conn: sqlite3.Connection, schedule_id: int, enabled=None, interval=None,
           update_changed=None, name=None) -> None:
    row = get(conn, schedule_id)
    with db.transaction(conn):
        if enabled is not None:
            conn.execute("UPDATE schedules SET enabled = ? WHERE id = ?", (int(bool(enabled)),
                                                                           schedule_id))
            if enabled and row["next_due"] > time.time() + row["interval_minutes"] * 60:
                conn.execute("UPDATE schedules SET next_due = ? WHERE id = ?",
                             (time.time(), schedule_id))
        if interval is not None:
            minutes = parse_interval(interval)
            base = row["last_run"] or time.time()
            conn.execute("UPDATE schedules SET interval_minutes = ?, next_due = ? WHERE id = ?",
                         (minutes, base + minutes * 60, schedule_id))
        if update_changed is not None:
            conn.execute("UPDATE schedules SET update_changed = ? WHERE id = ?",
                         (int(bool(update_changed)), schedule_id))
        if name:
            conn.execute("UPDATE schedules SET name = ? WHERE id = ?", (name.strip(), schedule_id))


def remove(conn: sqlite3.Connection, schedule_id: int) -> None:
    get(conn, schedule_id)
    with db.transaction(conn):
        conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))


# --- running -----------------------------------------------------------------------------


def unavailable(conn: sqlite3.Connection, sch) -> str | None:
    """Why a run cannot be made now (a node is asleep or detached), or None."""
    me = index.self_id(conn)
    peers_by_id = {p["id"]: p for p in peers.paired_peers(conn)}
    for node_id in (sch["src_node"], sch["dst_node"]):
        node = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if node is None:
            return "a node of this schedule no longer exists"
        if node["owner_id"] != me:
            peer = peers_by_id.get(node["owner_id"])
            if peer is None or not peer["online"]:
                owner = peer["name"] if peer else node["owner_id"][:8]
                return f"server {owner} (node {node['name']}) is not reachable"
        if not node["online"]:
            return f"node {node['name']} is not online"
    return None


@dataclass
class RunResult:
    ran: bool
    job_id: int | None = None
    items: int = 0
    note: str = ""


def _set_note(conn, sid: int, note: str, **cols) -> None:
    sets = ", ".join(["last_note = ?"] + [f"{k} = ?" for k in cols])
    with db.transaction(conn):
        conn.execute(f"UPDATE schedules SET {sets} WHERE id = ?", (note, *cols.values(), sid))


def build_items(conn: sqlite3.Connection, sch) -> tuple[list[tuple], int, int, bool, int]:
    """(items, to copy, to update, truncated, left alone): items are ``(op, vpath, size,
    allow_hash)``; left alone = destination copies that are newer than the source's."""
    lo, hi = paths.prefix_range(sch["vpath"]) if sch["vpath"] != "/" else ("/", "0")
    items, newer, truncated = [], 0, False
    for r in conn.execute(
            "SELECT f.id, f.vpath, sc.size, sc.hash AS shash, dc.hash AS dhash, dc.state AS dstate"
            " FROM files f CROSS JOIN copies sc ON sc.file_id = f.id AND sc.node_id = ?1"
            " AND sc.state = 'present' LEFT JOIN copies dc ON dc.file_id = f.id AND dc.node_id = ?2"
            " WHERE f.vpath >= ?3 AND f.vpath < ?4 AND (dc.file_id IS NULL OR dc.state != 'present'"
            " OR dc.hash != sc.hash) ORDER BY f.vpath",
            (sch["src_node"], sch["dst_node"], lo, hi)):
        if len(items) >= MAX_ITEMS_PER_RUN:
            truncated = True
            break
        if r["dhash"] is None or r["dstate"] != "present":
            items.append(("copy", r["vpath"], r["size"], None))
            continue
        current = index.current_version(conn, r["id"])
        if sch["update_changed"] and current is not None and current.hash == r["shash"]:
            items.append(("overwrite", r["vpath"], r["size"], r["dhash"]))   # the older one only
        else:
            newer += 1    # the destination holds the newer (or an unjudgeable) version: leave it
    return items, sum(1 for i in items if i[0] == "copy"), \
        sum(1 for i in items if i[0] == "overwrite"), truncated, newer


def run(conn: sqlite3.Connection, schedule_id: int, force: bool = False) -> RunResult:
    """Make the job for one schedule now (``force``: even if it is not due yet)."""
    sch = get(conn, schedule_id)
    now = time.time()
    if not force and (not sch["enabled"] or sch["next_due"] > now):
        return RunResult(False)
    if sch["last_job_id"]:
        job = conn.execute("SELECT status FROM jobs WHERE id = ?", (sch["last_job_id"],)).fetchone()
        if job and job[0] in ACTIVE_JOB:
            _set_note(conn, schedule_id, f"the previous run (job #{sch['last_job_id']}) is "
                                         "still going", next_due=now + 60)
            return RunResult(False, note="previous run still going")
    why = unavailable(conn, sch)
    if why:       # stays due: it runs as soon as everything is back
        _set_note(conn, schedule_id, f"waiting: {why}")
        return RunResult(False, note=why)
    items, copies, updates, truncated, newer = build_items(conn, sch)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM nodes")}
    nxt = now + sch["interval_minutes"] * 60
    extra = f"; {newer} left alone (newer at the destination)" if newer else ""
    if not items:
        _set_note(conn, schedule_id, f"up to date{extra}", next_due=nxt, last_run=now)
        return RunResult(True, note=f"up to date{extra}")
    ts = db.now()
    desc = (f"scheduled sync {sch['name']!r}: {sch['vpath']} "
            f"{names.get(sch['src_node'])} -> {names.get(sch['dst_node'])}")
    job_id = None
    for start in range(0, len(items), CHUNK):
        with db.transaction(conn, "creating a scheduled sync"):
            if job_id is None:
                job_id = jobs._new_job(conn, "wanted", desc)
            conn.executemany(
                "INSERT INTO job_items(job_id, op, vpath, src_node, dst_node, size, allow_hash,"
                " updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(job_id, op, vp, sch["src_node"], sch["dst_node"], size, allow, ts)
                 for op, vp, size, allow in items[start:start + CHUNK]])
    note = f"job #{job_id}: {copies} to copy, {updates} to update{extra}"
    if truncated:
        note += f" (the first {MAX_ITEMS_PER_RUN}; the rest in the next run)"
    _set_note(conn, schedule_id, note, last_run=now, last_job_id=job_id,
              next_due=now + (60 if truncated else sch["interval_minutes"] * 60))
    return RunResult(True, job_id, len(items), note)


def run_due(conn: sqlite3.Connection) -> list[RunResult]:
    """Run every schedule that is due (called regularly by the worker)."""
    out = []
    for r in conn.execute("SELECT id FROM schedules WHERE enabled = 1 AND next_due <= ?"
                          " ORDER BY next_due", (time.time(),)).fetchall():
        try:
            res = run(conn, r["id"])
        except Exception as e:  # noqa: BLE001 - one broken schedule must not stop the others
            _set_note(conn, r["id"], f"failed: {e}", next_due=time.time() + 300)
            continue
        if res.job_id is not None:
            out.append(res)
    return out
