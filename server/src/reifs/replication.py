"""Enough copies of every file (design section 6).

Every file should have ``[replication] copies`` good copies (default 2), or
more if it carries a tag listed in ``tag_copies`` (the highest goal
applies). A good copy is a copy of the current version that is not marked
for deletion.

**Who proposes.** For a file below its goal, one server is responsible: the
owner of its good copy with the lowest node id. That server can serve as the
source, and the choice is the same on every server, so peers never propose
duplicates.

**Destinations.** They are this server's nodes and those of paired servers.
A node is skipped if it:

* is not a backup target;
* already holds some version of the file, or has a pending proposal for it;
* cannot hold the name (restricted names, case clash);
* lacks the announced free space;
* belongs to a server that is unreachable.

The remaining nodes are ranked online first, then by priority, then by free
space. One proposal is made per missing copy, each to a different node.

**Proposals.** They are "wanted" jobs with reason ``replicate``, approved by
the user or automatically (``[server] auto_replicate``). A rejected proposal
is not made again until the file's content changes. Approving a proposal
whose destination is on another server creates the job there (the
destination's owner runs transfers into its nodes).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from dataclasses import asdict, dataclass, field

from . import db, index, jobs, paths, peers, queries
from .config import Config
from .queries import GoalPolicy
from .tags import tags_of

REASON = "replicate"
FORWARD_CHUNK = 5000


def policy(cfg: Config) -> GoalPolicy:
    return GoalPolicy.from_config(cfg)


@dataclass
class PlanReport:
    copies: int = 2            # the default goal when planned
    under_replicated: int = 0  # files below their goal (anywhere)
    single_copy: int = 0       # ...of which with a single good copy
    responsible: int = 0       # ...that this server must plan
    proposed: int = 0          # proposals made now (one per missing copy)
    already_proposed: int = 0  # files whose missing copies are all proposed
    declined: int = 0          # rejected earlier, content unchanged
    no_destination: int = 0    # files with at least one copy that cannot be placed
    reasons: dict[str, int] = field(default_factory=dict)
    examples: list[tuple[str, str]] = field(default_factory=list)
    auto_approved: int = 0
    examined: int = 0          # files looked at in this run
    open_proposals: int = 0    # replicate proposals waiting for a decision when planning began
    truncated: bool = False    # stopped at the limits: the rest is planned in later runs
    finished: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class UnderFile:
    file_id: int
    vpath: str
    vkey: str
    issues: str
    hash: str
    size: int
    good_nodes: list[str]
    goal: int


def good_nodes_of(conn: sqlite3.Connection, fid: int) -> tuple[list[str], str, int] | None:
    """(nodes holding the current version, not marked for deletion; its hash; its size)."""
    rows = conn.execute("SELECT node_id, hash, size, mtime_ns, marked_delete FROM copies"
                        " WHERE file_id = ? AND state = 'present'", (fid,)).fetchall()
    if not rows:
        return None
    top = min(rows, key=lambda r: (-r["mtime_ns"], r["hash"]))   # index.current_version
    return (sorted(r["node_id"] for r in rows
                   if r["hash"] == top["hash"] and not r["marked_delete"]),
            top["hash"], top["size"])


def under_file(conn: sqlite3.Connection, fid: int, pol: GoalPolicy) -> UnderFile | None:
    """The file if it has a good copy but fewer than its goal (looked at on its own)."""
    got = good_nodes_of(conn, fid)
    f = conn.execute("SELECT vpath, issues FROM files WHERE id = ?", (fid,)).fetchone()
    if got is None or f is None or not got[0]:
        return None
    goal = pol.goal(tags_of(conn, fid))
    if len(got[0]) >= goal:
        return None
    return UnderFile(fid, f["vpath"], paths.key(f["vpath"]), f["issues"], got[1], got[2], got[0],
                     goal)


class _Planner:
    """Chooses destinations. Everything about a file is looked up when the file comes
    up (a few indexed queries), never preloaded: with millions of files that would
    take minutes and gigabytes."""

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

    def _holding(self, fid: int) -> set[str]:
        return {r[0] for r in self.conn.execute(
            "SELECT node_id FROM copies WHERE file_id = ?", (fid,))}

    def _case_clash(self, node_id: str, f: UnderFile) -> bool:
        # Found by the (indexed) number of the casefolded path, then compared for real.
        return any(paths.key(r[0]) == f.vkey for r in self.conn.execute(
            "SELECT x.vpath FROM files x JOIN copies c ON c.file_id = x.id WHERE x.vhash = ?"
            " AND c.node_id = ? AND x.vpath != ?", (db.key_hash(f.vpath), node_id, f.vpath)))

    def choose(self, f: UnderFile, exclude: set[str]) -> tuple[str | None, str]:
        holding = self._holding(f.file_id) | exclude
        issues = paths.decode_issues(f.issues)
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
            if not n["case_sensitive"] and self._case_clash(n["id"], f):
                rejected["a file differing only in case is on the other backup nodes"] += 1
                continue
            free = n["free_bytes"]
            if free is not None and free - self.reserved[n["id"]] - f.size < self.margin:
                rejected["not enough free space on the other backup nodes"] += 1
                continue
            candidates.append(n)
        if not candidates:
            reason = rejected.most_common(1)[0][0] if rejected else "there are not enough nodes"
            return None, reason
        candidates.sort(key=lambda n: (not n["online"], -n["priority"],
                                       -(n["free_bytes"] or 0), n["name"]))
        best = candidates[0]
        self.reserved[best["id"]] += f.size
        return best["id"], ""


def plan(conn: sqlite3.Connection, cfg: Config) -> PlanReport:
    """Propose the missing copies of the under-replicated files we are responsible for.

    Built to be bounded: the totals come from one SQL aggregation, the files are
    walked in id order and looked at one by one, and at most
    ``[replication] max_proposals`` proposals are open at a time (and at most
    ``max_examined`` files looked at per run): with millions of files below their goal,
    a plan is made in pieces, the next one when these have been approved or rejected.
    """
    pol = policy(cfg)
    report = PlanReport(copies=pol.copies)
    stats, _stale = queries.coverage_stats(conn, pol)
    report.under_replicated, report.single_copy = stats.under_replicated, stats.single_copy
    planner = _Planner(conn, cfg)
    me = planner.me
    owners = {r["id"]: r["owner_id"] for r in conn.execute("SELECT id, owner_id FROM nodes")}

    # Proposals that are no longer needed: the file reached its goal or the destination has it.
    active: dict[int, set[str]] = {}
    open_count, finished = 0, []
    for w in conn.execute("SELECT id, file_id, dst_node, status FROM wanted WHERE reason = ?"
                          " AND status IN ('open', 'approved')", (REASON,)).fetchall():
        f = under_file(conn, w["file_id"], pol)
        if f is None or w["dst_node"] in f.good_nodes:
            finished.append(w["id"])
        else:
            active.setdefault(w["file_id"], set()).add(w["dst_node"])
            open_count += w["status"] == "open"
    for i in range(0, len(finished), 1000):
        with db.transaction(conn):
            conn.executemany("UPDATE wanted SET status = 'done' WHERE id = ?",
                             [(x,) for x in finished[i:i + 1000]])
    budget = max(0, cfg.replication.max_proposals - open_count)
    report.open_proposals = open_count
    if budget == 0 and report.under_replicated:
        report.truncated = True   # at the limit: decide some of the open ones to get more
    reasons: Counter = Counter()
    pending_rows: list[tuple] = []

    def flush() -> None:
        # Proposals are written in chunks so the database is never locked
        # for long (scans, syncs and the API keep writing meanwhile).
        if pending_rows:
            with db.transaction(conn):
                for args in pending_rows:
                    index.add_wanted(conn, *args)
            pending_rows.clear()

    queries.fill_under_ids(conn, pol)
    last_id, examined, batch = 0, 0, []
    while budget > 0 and examined < cfg.replication.max_examined and not report.truncated:
        batch = [r[0] for r in conn.execute(
            "SELECT file_id FROM temp.under_ids WHERE file_id > ? ORDER BY file_id LIMIT 500",
            (last_id,))]
        if not batch:
            break
        last_id = batch[-1]
        for fid in batch:
            f = under_file(conn, fid, pol)
            examined += 1
            if f is None:
                continue
            if owners.get(f.good_nodes[0]) != me:
                continue  # the server holding the first good copy proposes
            report.responsible += 1
            pending = active.get(f.file_id, set())
            need = f.goal - len(f.good_nodes) - len(pending)
            if need <= 0:
                report.already_proposed += 1
                continue
            declined = conn.execute(
                "SELECT data FROM wanted WHERE file_id = ? AND reason = ? AND status = 'rejected'"
                " ORDER BY id DESC LIMIT 1", (fid, REASON)).fetchone()
            if declined is not None and json.loads(declined[0]).get("hash") == f.hash:
                report.declined += 1
                continue
            chosen: set[str] = set()
            source = next((n for n in f.good_nodes if owners.get(n) == me), f.good_nodes[0])
            for _ in range(need):
                dst, why = planner.choose(f, pending | chosen)
                if dst is None:
                    report.no_destination += 1
                    reasons[why] += 1
                    if len(report.examples) < 20:
                        report.examples.append((f.vpath, why))
                    break
                chosen.add(dst)
                pending_rows.append(("copy", f.file_id, source, dst, REASON,
                                     {"hash": f.hash, "goal": f.goal}))
                report.proposed += 1
            if len(pending_rows) >= 2000:
                flush()
            if report.proposed >= budget:
                report.truncated = True
                break
        time.sleep(0)   # let the API threads run: this loop is plain Python
    flush()
    report.examined = examined
    report.truncated = report.truncated or (
        examined >= cfg.replication.max_examined and bool(batch))
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
