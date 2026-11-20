"""Two copies of every file (design section 6).

A file is *satisfied* when at least two nodes hold its current version in a
copy that is not marked for deletion. For a file with a single good copy,
the server owning that copy proposes a second one: it is the only server
able to serve as the source, and exactly one server is responsible, so
peers never propose duplicates.

Destinations are this server's nodes and those of paired servers. A node is
skipped if it:

* is not a backup target;
* already holds some version of the file;
* cannot hold the name (restricted names, case clash);
* lacks the announced free space;
* belongs to a server that is unreachable.

The remaining nodes are ranked online first, then by priority, then by free
space.

Proposals are "wanted" jobs with reason ``replicate``. They are approved by
the user, or automatically when ``[server] auto_replicate`` is on. A
rejected proposal is not made again until the file's content changes.
Approving a proposal whose destination is on another server creates the
job there (the destination's owner runs transfers into its nodes).
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field

from . import db, index, jobs, paths, peers
from .config import Config

TARGET = 2
REASON = "replicate"
FORWARD_CHUNK = 5000


@dataclass
class PlanReport:
    single_copy: int = 0       # files with exactly one good copy (anywhere)
    responsible: int = 0       # ...whose copy is on one of our nodes
    proposed: int = 0
    already_proposed: int = 0
    declined: int = 0          # rejected earlier, content unchanged
    no_destination: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    examples: list[tuple[str, str]] = field(default_factory=list)
    auto_approved: int = 0
    finished: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def single_copy_files(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Files whose current version has exactly one good copy, with that copy."""
    return conn.execute("""
        WITH cur AS (
            SELECT file_id, hash, size FROM (
                SELECT file_id, hash, size, ROW_NUMBER() OVER (
                    PARTITION BY file_id ORDER BY mtime_ns DESC, hash) AS rn
                FROM copies WHERE state = 'present')
            WHERE rn = 1
        ),
        good AS (
            SELECT c.file_id, c.node_id, cur.hash, cur.size FROM copies c
            JOIN cur ON cur.file_id = c.file_id AND cur.hash = c.hash
            WHERE c.state = 'present' AND c.marked_delete = 0
        )
        SELECT g.file_id, MIN(g.node_id) AS node_id, g.hash, g.size, f.vpath, f.vkey, f.issues
        FROM good g JOIN files f ON f.id = g.file_id
        GROUP BY g.file_id HAVING COUNT(DISTINCT g.node_id) = 1
        ORDER BY f.vpath
    """).fetchall()


class _Planner:
    def __init__(self, conn: sqlite3.Connection, cfg: Config):
        self.conn = conn
        self.me = index.self_id(conn)
        self.margin = cfg.transfers.min_free_mb * 2**20
        peers_ = {p["id"]: p for p in peers.paired_peers(conn)}
        self.nodes = [n for n in conn.execute("SELECT * FROM nodes ORDER BY name")
                      if n["owner_id"] == self.me or n["owner_id"] in peers_]
        self.peer_online = {pid: bool(p["online"]) for pid, p in peers_.items()}
        # Space already promised to open/approved proposals.
        self.reserved: Counter = Counter()
        for r in conn.execute(
                "SELECT w.dst_node, MAX(c.size) AS size FROM wanted w JOIN copies c"
                " ON c.file_id = w.file_id WHERE w.op = 'copy' AND w.status IN"
                " ('open', 'approved') GROUP BY w.id"):
            self.reserved[r["dst_node"]] += r["size"] or 0

    def choose(self, f: sqlite3.Row) -> tuple[str | None, str]:
        holding = {r[0] for r in self.conn.execute(
            "SELECT node_id FROM copies WHERE file_id = ?", (f["file_id"],))}
        issues = paths.decode_issues(f["issues"])
        rejected: Counter = Counter()
        candidates = []
        for n in self.nodes:
            if n["id"] in holding:
                continue
            if not n["replication_target"]:
                rejected["no other node is a backup target"] += 1
                continue
            if n["owner_id"] != self.me and not self.peer_online.get(n["owner_id"]):
                rejected["the servers of the other backup nodes are unreachable"] += 1
                continue
            if n["restricted_names"] and issues & paths.RESTRICTED_NAME_ISSUES:
                rejected["its name is not allowed on the other backup nodes"] += 1
                continue
            if not n["case_sensitive"] and self.conn.execute(
                    "SELECT 1 FROM copies c JOIN files x ON x.id = c.file_id"
                    " WHERE c.node_id = ? AND x.vkey = ? AND x.vpath != ? LIMIT 1",
                    (n["id"], f["vkey"], f["vpath"])).fetchone():
                rejected["a file differing only in case is on the other backup nodes"] += 1
                continue
            free = n["free_bytes"]
            if free is not None and free - self.reserved[n["id"]] - f["size"] < self.margin:
                rejected["not enough free space on the other backup nodes"] += 1
                continue
            candidates.append(n)
        if not candidates:
            reason = rejected.most_common(1)[0][0] if rejected else "there is no other node"
            return None, reason
        candidates.sort(key=lambda n: (not n["online"], -n["priority"],
                                       -(n["free_bytes"] or 0), n["name"]))
        best = candidates[0]
        self.reserved[best["id"]] += f["size"]
        return best["id"], ""


def plan(conn: sqlite3.Connection, cfg: Config) -> PlanReport:
    """Propose a second copy for every under-replicated file we are responsible for."""
    report = PlanReport()
    singles = single_copy_files(conn)
    single_ids = {f["file_id"] for f in singles}
    planner = _Planner(conn, cfg)
    me = planner.me
    owners = {r["id"]: r["owner_id"] for r in conn.execute("SELECT id, owner_id FROM nodes")}
    with db.transaction(conn):
        # Proposals whose file is now satisfied (or gone) are done.
        for w in conn.execute("SELECT id, file_id FROM wanted WHERE reason = ? AND status IN"
                              " ('open', 'approved')", (REASON,)).fetchall():
            if w["file_id"] not in single_ids:
                conn.execute("UPDATE wanted SET status = 'done' WHERE id = ?", (w["id"],))
        active = {r[0] for r in conn.execute(
            "SELECT file_id FROM wanted WHERE reason = ? AND status IN ('open', 'approved')",
            (REASON,))}
        declined = {}
        for r in conn.execute("SELECT file_id, data FROM wanted WHERE reason = ? AND"
                              " status = 'rejected' ORDER BY id", (REASON,)):
            declined[r["file_id"]] = json.loads(r["data"]).get("hash")
        reasons: Counter = Counter()
        for f in singles:
            report.single_copy += 1
            if owners.get(f["node_id"]) != me:
                continue  # the server holding the copy proposes
            report.responsible += 1
            if f["file_id"] in active:
                report.already_proposed += 1
                continue
            if declined.get(f["file_id"]) == f["hash"]:
                report.declined += 1
                continue
            dst, why = planner.choose(f)
            if dst is None:
                report.no_destination += 1
                reasons[why] += 1
                if len(report.examples) < 20:
                    report.examples.append((f["vpath"], why))
                continue
            index.add_wanted(conn, "copy", f["file_id"], f["node_id"], dst, REASON,
                             {"hash": f["hash"]})
            report.proposed += 1
        report.reasons = dict(reasons)
    report.finished = db.now()
    db.set_meta_tx(conn, "last_plan", json.dumps(report.to_dict()))
    return report


def last_plan(conn: sqlite3.Connection) -> dict | None:
    raw = db.get_meta(conn, "last_plan")
    return json.loads(raw) if raw else None


# --- approval -------------------------------------------------------------------------


@dataclass
class ApproveResult:
    local_job: int | None = None
    items: int = 0
    remote_jobs: list[tuple[str, int | None, int]] = field(default_factory=list)  # server, job, items
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.local_job is not None:
            parts.append(f"job #{self.local_job} with {self.items} item(s)")
        for server, job, n in self.remote_jobs:
            parts.append(f"job #{job} with {n} item(s) on {server}" if job is not None
                         else f"nothing to do on {server}")
        parts += [f"error: {e}" for e in self.errors]
        return "; ".join(parts) or "nothing approved"


def approve(conn: sqlite3.Connection, ids: list[int] | None,
            reason: str | None = None) -> ApproveResult:
    """Approve open wanted jobs (all, or the given ids, optionally of one reason).

    Copies into another server's node are sent to that server as a job.
    """
    me = index.self_id(conn)
    rows = jobs._wanted_rows(conn, ids)
    if reason:
        rows = [w for w in rows if w["reason"] == reason]
    local_ids: list[int] = []
    remote: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for w in rows:
        owner = index.owner_of(conn, w["dst_node"]) if w["dst_node"] else me
        if owner == me or w["op"] != "copy" or w["vpath"] is None:
            local_ids.append(w["id"])
        else:
            remote.setdefault((owner, w["dst_node"]), []).append(w)
    result = ApproveResult()
    if local_ids:
        rep = jobs.approve_wanted(conn, local_ids)
        result.local_job, result.items = rep.job_id, rep.items
    for (owner, dst), group in remote.items():
        name = (conn.execute("SELECT name FROM servers WHERE id = ?", (owner,)).fetchone()
                or [owner[:8]])[0]
        for start in range(0, len(group), FORWARD_CHUNK):
            chunk = group[start:start + FORWARD_CHUNK]
            try:
                out = peers.forward(conn, owner, "POST", "/api/jobs",
                                    {"kind": "copy", "to": dst,
                                     "paths": [w["vpath"] for w in chunk]}, timeout=60)
            except peers.PeerError as e:
                with db.transaction(conn):
                    conn.executemany("UPDATE wanted SET message = ? WHERE id = ?",
                                     [(f"could not reach {name}: {e}", w["id"]) for w in chunk])
                result.errors.append(f"{name}: {e}")
                continue
            with db.transaction(conn):
                conn.executemany("UPDATE wanted SET status = 'approved', message = ? WHERE id = ?",
                                 [(f"job #{out.get('job_id')} on {name}", w["id"]) for w in chunk])
            result.remote_jobs.append((name, out.get("job_id"), int(out.get("items") or 0)))
    return result


def cycle(conn: sqlite3.Connection, cfg: Config) -> tuple[PlanReport, ApproveResult | None]:
    """Plan, then approve replication proposals automatically if enabled."""
    report = plan(conn, cfg)
    approved = None
    if cfg.server.auto_replicate:
        approved = approve(conn, None, reason=REASON)
        report.auto_approved = approved.items + sum(n for _, _, n in approved.remote_jobs)
        db.set_meta_tx(conn, "last_plan", json.dumps(report.to_dict()))
    return report, approved
