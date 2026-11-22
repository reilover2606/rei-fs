"""HTTPS JSON API used by the client (``reifs serve``).

Every endpoint lives under ``/api``. Apart from ``POST /api/login`` every
request needs ``Authorization: Bearer <token>``. Responses are JSON objects;
errors are ``{"error": "..."}`` with a 4xx/5xx status.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import secrets
import socket
import sqlite3
import ssl
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from concurrent.futures import ThreadPoolExecutor

from . import (__version__, audit, auth, db, decisions, index, jobs, nodes, paths, peers,
               priority, queries, replication, ssh, sync, tags, tls, vdirs, vmove)
from .config import Config
from .locking import LockError, ServerLock
from .paths import to_physical
from .service import Syncer, Worker

log = logging.getLogger("reifs.api")

TOKEN_TTL = 12 * 3600
MAX_BODY = 1 << 20
FIND_LIMIT = 1000


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


USER_ERRORS = (peers.PeerError, jobs.JobError, decisions.DecisionError, tags.TagError, nodes.NodeError,
               paths.VPathError, ValueError, KeyError)


# --- serialization --------------------------------------------------------------


def _secs(ns: int) -> int:
    return ns // 1_000_000_000


def file_json(info: queries.FileInfo, full: bool = True) -> dict:
    cur = next((c for c in info.copies if c.current), None)
    flags = []
    if any(c.state == "present" and not c.current for c in info.copies):
        flags.append("stale")
    if any(c.state == "missing" for c in info.copies):
        flags.append("missing")
    if any(c.marked_delete for c in info.copies):
        flags.append("marked")
    out = {
        "path": info.vpath,
        "name": paths.basename(info.vpath),
        "size": info.size,
        "mtime": _secs(cur.mtime_ns) if cur else 0,
        "good_copies": info.good_copies,
        "goal": info.goal,
        "tags": info.tags,
        "issues": sorted(info.issues),
        "flags": flags,
        "nodes": [c.node_name for c in info.copies if c.state == "present"],
    }
    if full:
        out["copies"] = [{
            "node": c.node_name, "node_id": c.node_id, "online": c.online, "size": c.size,
            "mtime": _secs(c.mtime_ns), "hash": c.hash.split(":", 1)[-1][:16],
            "state": c.state, "current": c.current, "marked_delete": c.marked_delete,
        } for c in info.copies]
    return out


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def decision_json(d: decisions.Decision, names: dict[str, str],
                  conn: sqlite3.Connection | None = None) -> dict:
    members = 0
    kind = d.kind
    if d.kind == "group":
        members, size = decisions.group_info(conn, d.id) if conn is not None else (0, 0)
        mk, where = d.data.get("member_kind", ""), d.node_name
        kind = {"missing": "gone files", "rename": "moved folder"}.get(mk, "group")
        if mk == "missing" and d.data.get("mode") == "gone":
            kind = "gone folder"
            summary = f"Folder {d.data['dir']}/ vanished from {where}: {members} file(s)"
        elif mk == "missing":
            summary = f"{members} file(s) vanished from {d.data['dir']}/ on {where}"
        else:
            summary = (f"Folder moved on {where}: {d.data['from']}/ -> {d.data['to']}/ "
                       f"({members} file(s)); still at the old path on other nodes")
        if members:
            summary += f", {_human(size)}"
    elif d.kind == "missing":
        summary = f"Copy vanished from {d.node_name}"
        if d.data.get("other_copies") == 0:
            summary += " (it was the last copy)"
    elif d.kind == "rename":
        others = ", ".join(names.get(n, n) for n in d.data.get("nodes", []))
        summary = f"Renamed on {d.node_name} to {d.data['to']}; still at the old path on {others}"
    elif d.kind == "divergent":
        stale = ", ".join(names.get(n, n) for n in d.data.get("stale_nodes", []))
        summary = f"Different versions; stale on {stale}"
        if d.data.get("tie"):
            summary += " (several versions share the newest mtime!)"
    elif d.kind == "conflict":
        summary = (f"Job #{d.data.get('job_id')} found a different file at this path "
                   f"on {d.node_name}")
    else:
        summary = d.kind
    path = d.vpath or d.data.get("vpath") or (d.data.get("dir") or d.data.get("from"))
    out = {"id": d.id, "kind": kind, "path": path,
           "node": d.node_name, "summary": summary, "created": d.created,
           "options": [{"key": k, "label": v} for k, v in d.options.items()]}
    if d.kind == "group":
        out["group"] = True
        out["members"] = members
    return out


def job_json(conn: sqlite3.Connection, j: sqlite3.Row) -> dict:
    counts = jobs.item_counts(conn, j["id"])
    return {"id": j["id"], "kind": j["kind"], "status": j["status"],
            "description": j["description"], "created": j["created"],
            "started": j["started"], "finished": j["finished"], "counts": counts,
            "total": sum(counts.values()), "done": counts.get("done", 0),
            # Items not done and not going to be without the user: never hide them.
            "issues": sum(counts.get(k, 0) for k in ("failed", "conflict"))
            + jobs.unresolved_skips(conn, j["id"])}


# --- the API ------------------------------------------------------------------------


# Access levels: public (no auth), admin (the client, or a paired server acting
# for it), client (the client only: login session management, pairing), peer
# (paired servers only).
Route = tuple[str, re.Pattern, Callable, str]


class Api:
    def __init__(self, cfg: Config, worker: Worker | None, syncer: Syncer | None = None):
        self.cfg = cfg
        self.worker = worker
        self.syncer = syncer
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="reifs-peer")
        self.busy_timeout = 10.0   # seconds a request waits for the write lock
        self._request_ids = itertools.count(1)
        self._requests: dict[int, float] = {}      # requests being served now -> start
        self._requests_lock = threading.Lock()
        self.tokens: dict[str, float] = {}
        self.tokens_lock = threading.Lock()
        self.server_id = ""
        self._tree_cache: dict[str, tuple] = {}
        self._tree_lock = threading.Lock()
        self._tree_compute = threading.Lock()
        self.routes: list[Route] = []
        r = self._route
        r("POST", r"/api/login", self.login, "public")
        r("POST", r"/api/logout", self.logout, "client")
        r("GET", r"/api/status", self.status)
        r("GET", r"/api/nodes", self.list_nodes)
        r("POST", r"/api/nodes", self.add_node)
        r("POST", r"/api/nodes/(?P<node>[^/]+)/adopt", self.adopt_node)
        r("POST", r"/api/nodes/(?P<node>[^/]+)/root", self.move_node_root)
        r("GET", r"/api/nodes/(?P<node>[^/]+)/audit", self.audit_node)
        r("POST", r"/api/nodes/(?P<node>[^/]+)", self.update_node)
        r("POST", r"/api/scan", self.scan)
        r("POST", r"/api/stop", self.stop)
        r("GET", r"/api/tree", self.tree)
        r("GET", r"/api/file", self.file)
        r("GET", r"/api/find", self.find)
        r("GET", r"/api/tags", self.list_tags)
        r("POST", r"/api/tags", self.edit_tags)
        r("GET", r"/api/decisions", self.list_decisions)
        r("GET", r"/api/decisions/(?P<id>\d+)/members", self.decision_members)
        r("POST", r"/api/decisions/(?P<id>\d+)", self.resolve_decision)
        r("GET", r"/api/wanted", self.list_wanted)
        r("POST", r"/api/wanted", self.act_wanted)
        r("GET", r"/api/jobs", self.list_jobs)
        r("POST", r"/api/jobs", self.create_job)
        r("POST", r"/api/vmove", self.vmove)
        r("GET", r"/api/priority", self.get_priority)
        r("POST", r"/api/priority", self.set_priority)
        r("POST", r"/api/mkdir", self.mkdir)
        r("POST", r"/api/rmdir", self.rmdir)
        r("POST", r"/api/jobs/run", self.run_jobs)
        r("GET", r"/api/jobs/(?P<id>\d+)", self.show_job)
        r("POST", r"/api/jobs/(?P<id>\d+)/(?P<action>cancel|retry)", self.job_action)
        r("GET", r"/api/replication", self.replication_status)
        r("POST", r"/api/replication", self.replication_settings)
        r("POST", r"/api/replication/plan", self.replication_plan)
        r("GET", r"/api/marked", self.list_marked)
        r("POST", r"/api/unmark", self.unmark)
        # pairing management (client only)
        r("GET", r"/api/peers", self.list_peers, "client")
        r("POST", r"/api/peers/probe", self.probe_peer, "client")
        r("POST", r"/api/peers/pair", self.pair_peer, "client")
        r("POST", r"/api/peers/sync", self.sync_now, "client")
        r("POST", r"/api/peers/requests/(?P<id>\d+)/(?P<action>accept|reject)",
          self.pair_request_action, "client")
        r("POST", r"/api/peers/(?P<peer>[^/]+)/unpair", self.unpair_peer, "client")
        r("POST", r"/api/peers/(?P<peer>[^/]+)/transport", self.set_transport, "client")
        r("POST", r"/api/peers/authorize", self.ssh_authorize, "client")
        # server-to-server
        r("GET", r"/api/peer/hello", self.peer_hello, "public")
        r("POST", r"/api/peer/pair-request", self.peer_pair_request, "public")
        r("POST", r"/api/peer/pair-confirm", self.peer_pair_confirm, "public")
        r("POST", r"/api/peer/pair-reject", self.peer_pair_reject, "public")
        r("GET", r"/api/peer/changes", self.peer_changes, "peer")
        r("GET", r"/api/peer/info", self.peer_info, "peer")
        r("POST", r"/api/peer/mark", self.peer_mark, "peer")
        r("POST", r"/api/peer/wanted", self.peer_wanted, "peer")
        r("POST", r"/api/peer/poke", self.peer_poke, "peer")
        r("POST", r"/api/peer/unpair", self.peer_unpair, "peer")

    def _route(self, method: str, pattern: str, fn: Callable, access: str = "admin") -> None:
        self.routes.append((method, re.compile(pattern + "$"), fn, access))

    # --- auth -----------------------------------------------------------------------

    def check_token(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        token = header[7:].strip()
        now = time.time()
        with self.tokens_lock:
            expiry = self.tokens.get(token)
            if expiry is None or expiry < now:
                self.tokens.pop(token, None)
                return False
            self.tokens[token] = now + TOKEN_TTL  # sliding expiry
        return True

    def login(self, conn, q, body, **_):
        stored = self.cfg.server.password_hash
        if not stored:
            raise ApiError(403, "no admin password is set on this server (run 'reifs passwd')")
        if not auth.verify_password(str(body.get("password", "")), stored):
            time.sleep(1.0)  # slow down guessing
            raise ApiError(401, "wrong password")
        token = secrets.token_urlsafe(32)
        with self.tokens_lock:
            now = time.time()
            self.tokens = {t: e for t, e in self.tokens.items() if e > now}
            self.tokens[token] = now + TOKEN_TTL
        return {"token": token, "server": self._server(conn), "version": __version__}

    def logout(self, conn, q, body, token=None, **_):
        with self.tokens_lock:
            self.tokens.pop(token, None)
        return {"ok": True}

    def _server(self, conn) -> dict:
        return {"id": nodes.self_id(conn), "name": self.cfg.server.name}

    # --- status and nodes -----------------------------------------------------------

    def database_status(self) -> dict:
        """What the database is doing: write lock, waiting writers, requests."""
        act = db.lock_activity()
        free = db.probe_lock(self.cfg.db_path)
        if act["holders"]:
            lock = "held"            # by something in this server
        elif not free:
            lock = "locked"          # by another process (e.g. a reifs command)
        else:
            lock = "free"
        now = time.monotonic()
        with self._requests_lock:
            others = sorted((now - t for t in self._requests.values()), reverse=True)[:-1] \
                if self._requests else []
        return {"lock": lock, "holders": act["holders"], "waiting": act["waiting"],
                "busy_errors": act["busy_errors"],
                "requests": len(others), "slowest_request": round(others[0], 2) if others else 0}

    def status(self, conn, q, body, **_):
        s = queries.status(conn, replication.policy(self.cfg))
        online, total = conn.execute(
            "SELECT COALESCE(SUM(online), 0), COUNT(*) FROM nodes").fetchone()
        return {
            "server": self._server(conn),
            "activity": self.worker.activity() if self.worker else {"state": "idle"},
            "nodes_online": online, "nodes_total": total,
            "files": s.files, "bytes": s.bytes, "satisfied": s.satisfied,
            "under_replicated": s.under_replicated, "single_copy": s.single_copy,
            "divergent": s.divergent, "missing_copies": s.missing_copies,
            "marked_delete": s.marked_delete, "non_portable": s.non_portable,
            "open_decisions": s.open_decisions, "open_wanted": s.open_wanted,
            "coverage_stale": s.coverage_stale,
            "active_jobs": conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('pending', 'running', 'waiting')"
            ).fetchone()[0],
            "peers_total": conn.execute("SELECT COUNT(*) FROM servers WHERE paired = 1"
                                        ).fetchone()[0],
            "peers_online": conn.execute("SELECT COUNT(*) FROM servers WHERE paired = 1"
                                         " AND online = 1").fetchone()[0],
            "auto_replicate": self.cfg.server.auto_replicate,
            "database": self.database_status(),
            "connections": dict(CONNECTION_STATS),
            "pair_requests": conn.execute(
                "SELECT COUNT(*) FROM pair_requests WHERE direction = 'in' AND status = 'pending'"
            ).fetchone()[0],
        }

    def list_nodes(self, conn, q, body, **_):
        me = nodes.self_id(conn)
        out = []
        # The totals per node come with the coverage figures (one pass, cached), not from
        # a scan of every copy for every node on every request.
        cov, _stale = queries.coverage_stats(conn, replication.policy(self.cfg))
        for r in conn.execute(
            "SELECT n.*, s.name AS owner, s.online AS owner_online"
            " FROM nodes n JOIN servers s ON s.id = n.owner_id ORDER BY n.name"
        ):
            out.append({
                "id": r["id"], "name": r["name"], "kind": r["kind"], "online": bool(r["online"]),
                "owner": "self" if r["owner_id"] == me else r["owner"],
                "owner_id": r["owner_id"],
                "reachable": bool(r["online"]) and (r["owner_id"] == me or bool(r["owner_online"])),
                "files": cov.node_files.get(r["id"], 0), "bytes": cov.node_bytes.get(r["id"], 0),
                "case_sensitive": bool(r["case_sensitive"]),
                "restricted_names": bool(r["restricted_names"]), "priority": r["priority"],
                "replication_target": bool(r["replication_target"]),
                "root": r["root_path"] or "", "last_scan": r["last_scan"] or "",
            })
        foreign = [{"node_id": f["node_id"], "name": f["name"], "path": f["path"],
                    "owner": (conn.execute("SELECT name FROM servers WHERE id = ?",
                                           (f["owner_id"],)).fetchone() or [f["owner_id"][:8]])[0],
                    "seen": f["seen"]}
                   for f in conn.execute("SELECT * FROM foreign_sightings ORDER BY name")]
        return {"nodes": out, "foreign": foreign}

    def update_node(self, conn, q, body, node, **_):
        row = nodes.get_node(conn, unquote(node))
        if row["owner_id"] != nodes.self_id(conn):
            # Settings of a node are facts its owner publishes.
            return peers.forward(conn, row["owner_id"], "POST", f"/api/nodes/{row['id']}", body)
        if "name" in body:
            nodes.rename_node(conn, row["id"], str(body["name"]))
        with db.transaction(conn):
            if "priority" in body:
                conn.execute("UPDATE nodes SET priority = ? WHERE id = ?",
                             (int(body["priority"]), row["id"]))
            if "replication_target" in body:
                conn.execute("UPDATE nodes SET replication_target = ? WHERE id = ?",
                             (int(bool(body["replication_target"])), row["id"]))
            index.log_node(conn, row["id"])
        self._poke()
        return {"ok": True}

    def add_node(self, conn, q, body, **_):
        """Register a storage node (an external drive, a folder) on this machine."""
        raw = str(body.get("path", "")).strip()
        if not raw:
            raise ApiError(400, "give the path of the drive's folder on the server machine")
        path = Path(os.path.expanduser(raw))
        if not path.is_absolute():
            raise ApiError(400, f"{raw!r} is not an absolute path (it is read on the server)")
        if not path.is_dir():
            raise ApiError(400, f"{path} is not a folder on the server machine "
                                "(is the drive mounted?)")
        name = nodes.clean_name(str(body.get("name") or path.name))
        node_id = nodes.register_node(conn, path, name)
        if self.worker:
            self.worker.request_scan([name])
        if self.syncer:
            self.syncer.request_daemon_update()
            self.syncer.request_poke()
        return {"ok": True, "id": node_id, "name": name}

    def audit_node(self, conn, q, body, node, **_):
        """Compare a node's folder with the index (reads only; can take a while)."""
        row = nodes.get_node(conn, unquote(node))
        if row["owner_id"] != nodes.self_id(conn):
            return peers.forward(conn, row["owner_id"], "GET", f"/api/nodes/{row['id']}/audit",
                                 timeout=600)
        root = next((loc.root for loc in nodes.discover(conn, self.cfg).online
                     if loc.node_id == row["id"]), None)
        if root is None:
            raise ApiError(409, f"node {row['name']!r} is not attached")
        out = audit.audit_node(conn, self.cfg, row["id"], root)
        out["node"] = row["name"]
        return out

    def move_node_root(self, conn, q, body, node, **_):
        row = nodes.get_node(conn, unquote(node))
        if row["owner_id"] != nodes.self_id(conn):
            return peers.forward(conn, row["owner_id"], "POST", f"/api/nodes/{row['id']}/root",
                                 body)
        try:
            with ServerLock(self.cfg.home):   # not while a scan or job uses the node
                root = nodes.move_root(conn, self.cfg, row["id"], str(body.get("path", "")),
                                       bool(body.get("move")))
        except LockError as e:
            raise ApiError(409, f"{e}; try again when it is done") from None
        if self.worker:
            self.worker.request_scan([row["id"]], full=False)
        if self.syncer:
            self.syncer.request_daemon_update()
            self.syncer.request_poke()
        return {"ok": True, "path": str(root)}

    def adopt_node(self, conn, q, body, node, **_):
        name = nodes.adopt_node(conn, unquote(node))
        if self.worker:
            self.worker.request_scan([name])
        if self.syncer:
            self.syncer.request_daemon_update()
            self.syncer.request_poke()
        return {"ok": True, "name": name}

    def scan(self, conn, q, body, **_):
        me = nodes.self_id(conn)
        local, remote = [], {}
        for name in body.get("nodes") or []:
            row = nodes.get_node(conn, name)
            if row["owner_id"] == me:
                local.append(row["id"])
            else:
                remote.setdefault(row["owner_id"], []).append(row["id"])
        for owner, ids in remote.items():
            peers.forward(conn, owner, "POST", "/api/scan",
                          {"nodes": ids, "verify": bool(body.get("verify")),
                           "due": bool(body.get("due")),
                           "new_only": bool(body.get("new_only"))})
        if local or not remote:
            self._need_worker().request_scan(local or None, bool(body.get("verify")),
                                             full=not body.get("due"),
                                             new_only=bool(body.get("new_only")))
        return {"ok": True, "queued": True}

    def _poke(self) -> None:
        if self.syncer:
            self.syncer.request_poke()

    def stop(self, conn, q, body, **_):
        self._need_worker().request_stop()
        return {"ok": True}

    def _need_worker(self) -> Worker:
        if self.worker is None:
            raise ApiError(503, "background worker not running")
        return self.worker

    # --- browsing ---------------------------------------------------------------------

    def tree(self, conn, q, body, **_):
        vdir = paths.normalize(q.get("path", "/"))
        # Listing a directory with millions of files below it takes seconds: such a
        # listing may be a few seconds old while background work changes the index
        # (our own changes clear the cache, so the user always sees their actions).
        seq = conn.execute("SELECT MAX(seq) FROM changes").fetchone()[0] or 0
        now = time.monotonic()
        with self._tree_lock:
            hit = self._tree_cache.get(vdir)
        if hit is not None and (hit[0] == seq or (hit[2] > 0.3 and now - hit[3] < max(10, 3 * hit[2]))):
            return hit[1]
        with self._tree_compute:   # one at a time; others reuse the result
            with self._tree_lock:
                hit = self._tree_cache.get(vdir)
            if hit is not None and hit[0] == seq:
                return hit[1]
            started = time.monotonic()
            payload = self._tree_payload(conn, vdir)
            took = time.monotonic() - started
            with self._tree_lock:
                self._tree_cache[vdir] = (seq, payload, took, time.monotonic())
                if len(self._tree_cache) > 200:
                    self._tree_cache.pop(next(iter(self._tree_cache)))
            return payload

    def _tree_payload(self, conn, vdir: str) -> dict:
        entries = queries.list_dir(conn, vdir, replication.policy(self.cfg))
        if entries is None:
            raise ApiError(404, f"not a directory in the virtual tree: {vdir}")
        prefs = priority.Prefs(priority.load(conn))
        here = prefs.effective(vdir)
        out = []
        for e in entries:
            if e.is_dir:
                child = paths.join(vdir, e.name)
                out.append({"name": e.name, "dir": True, "files": e.files, "here": e.here,
                            "priority": prefs.prefs.get(child, here),
                            "set_here": child in prefs.prefs})
            else:
                f = file_json(e.file, full=False)
                f["dir"] = False
                f["here"] = bool(e.here)
                out.append(f)
        mine = conn.execute("SELECT name FROM nodes WHERE id = ?", (nodes.self_id(conn),)).fetchone()
        return {"path": vdir, "entries": out, "priority": here, "more_files": entries.more,
                "this_node": mine[0] if mine else ""}

    def file(self, conn, q, body, **_):
        info = queries.lookup(conn, q.get("path", ""), replication.policy(self.cfg))
        if info is None:
            raise ApiError(404, f"not a file in the virtual tree: {q.get('path')}")
        return file_json(info)

    def find(self, conn, q, body, **_):
        limit = min(int(q.get("limit", FIND_LIMIT)), FIND_LIMIT)
        pol = replication.policy(self.cfg)
        ids, dirs = queries.search(conn, q.get("q") or None, tag=q.get("tag") or None,
                                   node=q.get("node") or None, issues_only=q.get("issues") == "1",
                                   under_replicated=q.get("under") == "1", policy=pol)
        return {"total": len(ids), "truncated": len(ids) > limit,
                "files": [file_json(queries.file_info(conn, fid, pol), full=False)
                          for fid in ids[:limit]],
                "dirs_total": len(dirs),
                "dirs": [{"path": d, "name": paths.basename(d), "files": n}
                         for d, n in dirs[:FIND_LIMIT]]}

    # --- tags -----------------------------------------------------------------------------

    def list_tags(self, conn, q, body, **_):
        return {"tags": [{"tag": t, "files": n} for t, n in tags.all_tags(conn)]}

    def edit_tags(self, conn, q, body, **_):
        action = body.get("action")
        if action not in ("add", "remove"):
            raise ApiError(400, "action must be 'add' or 'remove'")
        me = nodes.self_id(conn)
        changed = 0
        for vpath in body.get("paths", []):
            ids = tags.resolve_targets(conn, vpath, bool(body.get("recursive")))
            changed += tags.set_tag(conn, ids, str(body.get("tag", "")), action == "add", me)
        return {"changed": changed}

    # --- decisions and wanted ---------------------------------------------------------

    def list_decisions(self, conn, q, body, caller=None, **_):
        names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM nodes")}
        limit = min(int(q.get("limit", 0) or 0), 5000) or None   # None: all (the CLI, scripts)
        items = [decision_json(d, names, conn)
                 for d in decisions.list_decisions(conn, limit=limit)]
        total = decisions.count_decisions(conn)
        path = "/api/decisions" + (f"?limit={limit}" if limit else "")
        result = self._everywhere(conn, q, caller, path, "decisions", items)
        result["total"] = total
        return result

    def decision_members(self, conn, q, body, id, **_):
        """Some of the paths a group decision stands for."""
        n, _ = decisions.group_info(conn, int(id))
        return {"members": n, "paths": decisions.group_members(conn, int(id),
                                                              min(int(q.get("limit", 50)), 500))}

    def resolve_decision(self, conn, q, body, id, **_):
        summary = decisions.resolve(conn, int(id), str(body.get("option", "")))
        if self.worker and body.get("option") == "overwrite":
            self.worker.request_jobs()
        return {"ok": True, "summary": summary}

    def list_wanted(self, conn, q, body, caller=None, **_):
        limit = min(int(q.get("limit", 1000)), 5000)
        total = conn.execute("SELECT COUNT(*) FROM wanted WHERE status = 'open'").fetchone()[0]
        out = []
        for r in conn.execute(
            "SELECT w.*, f.vpath, s.name AS src, d.name AS dst FROM wanted w"
            " LEFT JOIN files f ON f.id = w.file_id LEFT JOIN nodes s ON s.id = w.src_node"
            " LEFT JOIN nodes d ON d.id = w.dst_node WHERE w.status = 'open' ORDER BY w.id"
            " LIMIT ?", (limit,)
        ):
            data = json.loads(r["data"])
            path = f"{data['from']} -> {data['to']}" if r["op"] == "rename" else r["vpath"]
            out.append({"id": r["id"], "op": r["op"], "path": path or "",
                        "from": r["src"] or "", "to": r["dst"] or "", "reason": r["reason"],
                        "message": r["message"] or ""})
        result = self._everywhere(conn, q, caller, f"/api/wanted?limit={limit}", "wanted", out)
        result["total"] = total
        return result

    def act_wanted(self, conn, q, body, **_):
        ids = None if body.get("all") else [int(i) for i in body.get("ids", [])]
        if ids is not None and not ids:
            raise ApiError(400, "give 'ids' or 'all'")
        if body.get("action") == "approve":
            res = replication.approve(conn, ids)
            if res.local_job is not None and self.worker:
                self.worker.request_jobs()
            return {"job_id": res.local_job, "items": res.items + sum(
                        n for _, _, n in res.remote_jobs),
                    "remote_jobs": len(res.remote_jobs), "errors": res.errors,
                    "summary": res.summary()}
        if body.get("action") == "reject":
            return {"rejected": jobs.reject_wanted(conn, ids)}
        raise ApiError(400, "action must be 'approve' or 'reject'")

    # --- jobs ------------------------------------------------------------------------

    def list_jobs(self, conn, q, body, caller=None, **_):
        sql = "SELECT * FROM jobs"
        if q.get("all") != "1":
            sql += " WHERE status NOT IN ('done', 'cancelled')"
        sql += " ORDER BY id DESC LIMIT 200"
        items = [job_json(conn, j) for j in conn.execute(sql).fetchall()]
        path = "/api/jobs" + ("?all=1" if q.get("all") == "1" else "")
        return self._everywhere(conn, q, caller, path, "jobs", items)

    def show_job(self, conn, q, body, id, **_):
        j = jobs.get_job(conn, int(id))
        out = job_json(conn, j)
        sql = ("SELECT i.*, s.name AS src, d.name AS dst FROM job_items i"
               " LEFT JOIN nodes s ON s.id = i.src_node JOIN nodes d ON d.id = i.dst_node"
               " WHERE i.job_id = ?")
        if q.get("all") != "1":
            sql += " AND i.state != 'done'"
        out["items"] = [{
            "id": i["id"], "op": i["op"], "state": i["state"], "path": i["vpath"],
            "new_path": i["new_vpath"] or "", "from": i["src"] or "", "to": i["dst"],
            "size": i["size"], "attempts": i["attempts"], "message": i["message"] or "",
        } for i in conn.execute(sql + " ORDER BY i.id LIMIT 1000", (j["id"],))]
        return out

    def create_job(self, conn, q, body, **_):
        kind = body.get("kind")
        if kind == "rename":  # from the server that planned a virtual move
            try:
                rep = vmove.create_rename_job(conn, str(body.get("node", "")),
                                              list(body.get("renames", [])),
                                              str(body.get("description") or "rename"))
            except (jobs.JobError, nodes.NodeError) as e:
                raise ApiError(409, str(e)) from None
            if rep.job_id is not None and self.worker:
                self.worker.request_jobs()
            return {"job_id": rep.job_id, "items": rep.items, "excluded": {}}
        if kind not in ("copy", "move"):
            raise ApiError(400, "kind must be 'copy' or 'move'")
        try:
            rep = jobs.create_transfer_job(conn, list(body.get("paths", [])),
                                           body.get("tag") or None, str(body.get("to", "")),
                                           body.get("from") or None, move=kind == "move")
        except jobs.RemoteDestination as e:
            # The destination's owner runs transfers into its nodes.
            out = self._forward_job(conn, e.owner_id, body)
            out["server"] = e.owner_id
            return out
        if rep.job_id is not None and self.worker:
            self.worker.request_jobs()
        return {"job_id": rep.job_id, "items": rep.items,
                "excluded": {k: len(v) for k, v in rep.excluded.items()}}

    def _forward_job(self, conn, owner_id: str, body: dict) -> dict:
        """Ask the server owning the destination node to create the job. It selects the
        files in ITS copy of the file list, which may be a little behind ours (the lists
        are kept in step by syncing): then it is asked to catch up and we try once more."""
        try:
            return peers.forward(conn, owner_id, "POST", "/api/jobs", body)
        except peers.PeerError as first:
            if not any(m in str(first) for m in ("not in the virtual tree", "unknown node")):
                raise
        name = (conn.execute("SELECT name FROM servers WHERE id = ?", (owner_id,)).fetchone()
                or [owner_id[:8]])[0]
        try:
            peers.forward(conn, owner_id, "POST", "/api/peer/poke", {}, timeout=10)
        except peers.PeerError:
            pass
        time.sleep(4)
        try:
            return peers.forward(conn, owner_id, "POST", "/api/jobs", body)
        except peers.PeerError as second:
            raise ApiError(
                409, f"server {name} does not have that in its list of files yet ({second}). "
                     "Its list is behind this server's; it was asked to catch up, so try again "
                     "in a moment (Servers tab: sync) and check the servers are syncing.") from None

    def get_priority(self, conn, q, body, **_):
        return priority.get(conn, q.get("path", "/"))

    def set_priority(self, conn, q, body, **_):
        try:
            value = priority.parse(body.get("priority"))
            return priority.set_priority(conn, str(body.get("path", "")), value)
        except priority.PriorityError as e:
            raise ApiError(400, str(e)) from None

    def mkdir(self, conn, q, body, **_):
        try:
            return {"path": vdirs.make(conn, str(body.get("path", "")))}
        except vdirs.VDirError as e:
            raise ApiError(409, str(e)) from None

    def rmdir(self, conn, q, body, **_):
        try:
            return {"removed": vdirs.remove(conn, str(body.get("path", "")))}
        except vdirs.VDirError as e:
            raise ApiError(409, str(e)) from None

    def vmove(self, conn, q, body, **_):
        """Move files/directories within the virtual tree (renames on every node).

        With ``dry_run`` only report what would happen (the client uses it to
        ask merge-or-preserve when the names clash).
        """
        sources = [str(p) for p in body.get("paths", [])]
        dest = str(body.get("to", "/"))
        name = body.get("name") or None
        try:
            plan = vmove.plan_move(conn, sources, dest, name,
                                   str(body.get("on_conflict") or "merge"))
        except vmove.MoveError as e:
            raise ApiError(400, str(e)) from None
        out = plan.summary()
        if body.get("dry_run"):
            out["dry_run"] = True
            return out
        res = vmove.apply_plan(conn, plan, vmove.describe(sources, dest, name))
        if res.job_id is not None and self.worker:
            self.worker.request_jobs()
        out.update({"job_id": res.job_id, "items": res.items + sum(n for _, _, n in
                                                                   res.remote_jobs),
                    "remote_jobs": len(res.remote_jobs), "marked": res.marked,
                    "errors": res.errors})
        return out

    def run_jobs(self, conn, q, body, **_):
        self._need_worker().request_jobs()
        return {"ok": True}

    def job_action(self, conn, q, body, id, action, **_):
        if action == "cancel":
            n = jobs.cancel_job(conn, int(id))
        else:
            n = jobs.retry_job(conn, int(id))
            if n and self.worker:
                self.worker.request_jobs()
        return {"ok": True, "items": n}

    # --- replication ----------------------------------------------------------------

    def replication_status(self, conn, q, body, **_):
        s = queries.status(conn, replication.policy(self.cfg))
        return {"copies": self.cfg.replication.copies,
                "tag_copies": self.cfg.replication.tag_copies,
                "auto_replicate": self.cfg.server.auto_replicate,
                "satisfied": s.satisfied, "under_replicated": s.under_replicated,
                "single_copy": s.single_copy,
                "open_proposals": conn.execute(
                    "SELECT COUNT(*) FROM wanted WHERE status = 'open' AND reason = ?",
                    (replication.REASON,)).fetchone()[0],
                "last_plan": replication.last_plan(conn)}

    def replication_settings(self, conn, q, body, **_):
        if "copies" in body:
            n = int(body["copies"])
            if n < 1:
                raise ApiError(400, "copies must be at least 1")
            self.cfg.replication.copies = n
        if "tag_copies" in body:
            tc = body["tag_copies"]
            if not isinstance(tc, dict) or not all(
                    isinstance(k, str) and isinstance(v, int) and v >= 1 for k, v in tc.items()):
                raise ApiError(400, "tag_copies must map tags to numbers >= 1")
            self.cfg.replication.tag_copies = dict(tc)
        if "auto_replicate" in body:
            self.cfg.server.auto_replicate = bool(body["auto_replicate"])
        self.cfg.save()
        if self.worker:
            self.worker.request_plan()
        return {"auto_replicate": self.cfg.server.auto_replicate,
                "copies": self.cfg.replication.copies,
                "tag_copies": self.cfg.replication.tag_copies}

    def replication_plan(self, conn, q, body, **_):
        self._need_worker().request_plan()
        return {"ok": True, "queued": True}

    # --- deletion marks ------------------------------------------------------------

    def list_marked(self, conn, q, body, **_):
        limit = min(int(q.get("limit", 0) or 0), 20000) or None
        rows = jobs.marked_copies(conn, q.get("node") or None, limit)
        total, size = jobs.marked_totals(conn, q.get("node") or None)
        return {"total": total, "total_size": size, "marked": [{
            "path": r["vpath"], "node": r["node_name"], "online": bool(r["online"]),
            "size": r["size"],
            "physical": str(to_physical(r["root_path"], r["vpath"])) if r["root_path"] else "",
        } for r in rows]}

    def unmark(self, conn, q, body, **_):
        paths_ = [] if body.get("all") else list(body.get("paths", []))
        if not paths_ and not body.get("all"):
            raise ApiError(400, "give 'paths' or 'all'")
        return {"unmarked": jobs.unmark(conn, paths_, body.get("node") or None)}

    # --- several servers ------------------------------------------------------------

    def _everywhere(self, conn, q, caller, path: str, key: str, items: list) -> dict:
        """Merge a list from every paired server when the client asks for it."""
        me = nodes.self_id(conn)
        for item in items:
            item["server"], item["server_name"] = me, self.cfg.server.name
        out = {key: items, "unreachable": []}
        if q.get("everywhere") != "1" or caller != "client":
            return out
        # A server the sync thread already found unreachable is not asked again
        # (every refresh would wait for the connection to time out).
        targets = []
        for p in peers.paired_peers(conn):
            if p["online"]:
                targets.append((p["id"], p["name"], peers.client_for(conn, p["id"], timeout=4)))
            else:
                out["unreachable"].append(p["name"])

        def fetch(target):
            sid, name, client = target
            try:
                return sid, name, client.request("GET", path)[key]
            except (peers.PeerError, KeyError) as e:
                return sid, name, e

        for sid, name, result in self.pool.map(fetch, targets):
            if isinstance(result, Exception):
                out["unreachable"].append(name)
                continue
            for item in result:
                item["server"], item["server_name"] = sid, name
                items.append(item)
        return out

    # --- pairing (client) ---------------------------------------------------------

    def list_peers(self, conn, q, body, **_):
        rows = peers.paired_peers(conn)
        reqs = conn.execute("SELECT * FROM pair_requests WHERE status = 'pending'"
                            " ORDER BY id").fetchall()
        return {
            "self": {"id": nodes.self_id(conn), "name": self.cfg.server.name,
                     "fingerprint": peers.my_fingerprint(self.cfg),
                     "ssh_enabled": self.cfg.ssh.enabled,
                     "ssh_public_key": ssh.ensure_key(self.cfg.home, self.cfg.server.name)},
            "peers": [{"id": r["id"], "name": r["name"], "address": r["address"],
                       "online": bool(r["online"]), "last_sync": r["last_sync"] or "",
                       "last_error": r["last_error"] or "", "sync_note": r["sync_note"] or "",
                       "fingerprint": r["fingerprint"],
                       "transport": r["transport"], "ssh_offered": bool(r["ssh_enabled"]),
                       "outbox": conn.execute("SELECT COUNT(*) FROM outbox WHERE server_id = ?",
                                              (r["id"],)).fetchone()[0]}
                      for r in rows],
            "requests": [{"id": r["id"], "direction": r["direction"], "name": r["name"],
                          "address": r["address"], "fingerprint": r["fingerprint"],
                          "created": r["created"], "message": r["message"] or ""}
                         for r in reqs],
        }

    def probe_peer(self, conn, q, body, **_):
        info, fp = peers.hello(str(body.get("address", "")))
        return {"id": info.get("id"), "name": info.get("name"), "fingerprint": fp,
                "version": info.get("version")}

    def pair_peer(self, conn, q, body, **_):
        info = peers.request_pairing(conn, self.cfg, str(body.get("address", "")),
                                     str(body.get("fingerprint", "")))
        return {"ok": True, "name": info.get("name")}

    def pair_request_action(self, conn, q, body, id, action, **_):
        if action == "reject":
            peers.reject_pairing(conn, int(id))
            return {"ok": True}
        name = peers.accept_pairing(conn, self.cfg, int(id))
        if self.syncer:
            self.syncer.request_daemon_update()
            self.syncer.request_sync()
        return {"ok": True, "name": name}

    def unpair_peer(self, conn, q, body, peer, **_):
        row = peers.find_peer(conn, unquote(peer))
        peers.unpair(conn, row["id"])
        if self.syncer:
            self.syncer.request_daemon_update()
        return {"ok": True}

    def set_transport(self, conn, q, body, peer, **_):
        row = peers.find_peer(conn, unquote(peer))
        transport = body.get("transport")
        if transport not in ("daemon", "ssh"):
            raise ApiError(400, "transport must be 'daemon' or 'ssh'")
        if transport == "ssh" and not row["ssh_enabled"]:
            raise ApiError(409, f"{row['name']} does not offer SSH ([ssh] enabled there)")
        with db.transaction(conn):
            conn.execute("UPDATE servers SET transport = ? WHERE id = ?", (transport, row["id"]))
        return {"ok": True, "transport": transport}

    def ssh_authorize(self, conn, q, body, **_):
        lines, rrsync, missing = ssh.authorize_lines(conn)
        out = {"lines": lines, "restricted": bool(rrsync), "missing": missing}
        if body.get("apply"):
            path, added = ssh.install(lines)
            out.update(file=str(path), added=added)
        return out

    def sync_now(self, conn, q, body, **_):
        if self.syncer is None:
            raise ApiError(503, "sync not running")
        self.syncer.request_sync()
        return {"ok": True}

    # --- server to server ------------------------------------------------------------

    def peer_hello(self, conn, q, body, **_):
        return {"id": nodes.self_id(conn), "name": self.cfg.server.name, "version": __version__}

    def peer_pair_request(self, conn, q, body, client_ip="", **_):
        peers.receive_pair_request(conn, body, client_ip)
        return {"ok": True, "message": "waiting for the administrator to accept"}

    def peer_pair_confirm(self, conn, q, body, **_):
        out = peers.confirm_pairing(conn, self.cfg, body)
        if self.syncer:
            self.syncer.request_daemon_update()
            self.syncer.request_sync()
        return out

    def peer_pair_reject(self, conn, q, body, **_):
        peers.receive_pair_reject(conn, body)
        return {"ok": True}

    def peer_info(self, conn, q, body, **_):
        return {"id": nodes.self_id(conn), "name": self.cfg.server.name,
                "rsync_port": self.cfg.rsync.daemon_port, "ssh": ssh.offer(self.cfg)}

    def peer_changes(self, conn, q, body, caller=None, **_):
        have = {}
        for part in filter(None, q.get("have", "").split(",")):   # "origin:seq,origin:seq"
            origin, _, seq = part.rpartition(":")
            if origin and seq.isdigit():
                have[origin] = int(seq)
        return sync.export_changes(conn, int(q.get("since", 0)),
                                   min(int(q.get("limit", sync.BATCH)), sync.BATCH),
                                   exclude_origin=caller, have=have)

    def _owned_copy(self, conn, body) -> tuple[int, str]:
        node_id = str(body.get("node_id", ""))
        if index.owner_of(conn, node_id) != nodes.self_id(conn):
            raise ApiError(409, "that node is not owned by this server")
        fid = index.file_id(conn, str(body.get("vpath", "")))
        if fid is None or not conn.execute(
                "SELECT 1 FROM copies WHERE file_id = ? AND node_id = ?", (fid, node_id)).fetchone():
            raise ApiError(404, "no such copy")
        return fid, node_id

    def peer_mark(self, conn, q, body, **_):
        with db.transaction(conn):
            fid, node_id = self._owned_copy(conn, body)
            if body.get("marked") and body.get("hash") and not conn.execute(
                    "SELECT 1 FROM copies WHERE file_id = ? AND node_id = ? AND hash = ?",
                    (fid, node_id, body["hash"])).fetchone():
                raise ApiError(409, "the copy changed since it was copied; not marking it")
            index.set_marked(conn, fid, node_id, bool(body.get("marked")))
        return {"ok": True}

    def peer_wanted(self, conn, q, body, caller=None, **_):
        if body.get("op") != "rename":
            raise ApiError(400, "only renames can be proposed")
        with db.transaction(conn):
            fid, node_id = self._owned_copy(conn, {"node_id": body.get("node_id"),
                                                   "vpath": body.get("from")})
            index.add_wanted(conn, "rename", fid, node_id, node_id,
                             str(body.get("reason") or "rename"),
                             {"from": body["from"], "to": paths.normalize(str(body["to"]))})
        return {"ok": True}

    def peer_poke(self, conn, q, body, **_):
        if self.syncer:
            self.syncer.request_sync()
        return {"ok": True}

    def peer_unpair(self, conn, q, body, caller=None, **_):
        peers.forget_peer(conn, caller)
        if self.syncer:
            self.syncer.request_daemon_update()
        return {"ok": True}

    # --- dispatch ---------------------------------------------------------------------

    def dispatch(self, method: str, raw_path: str, headers, body_bytes: bytes,
                 client_ip: str = "") -> tuple[int, dict]:
        parts = urlsplit(raw_path)
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        route = None
        for m, pattern, fn, access in self.routes:
            match = pattern.match(parts.path)
            if match and m == method:
                route = (fn, access, match)
                break
        if route is None:
            return 404, {"error": f"no such endpoint: {method} {parts.path}"}
        fn, access, match = route
        try:
            body = json.loads(body_bytes or b"{}")
            if body is None:       # "null" is how a client says "no body"
                body = {}
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except ValueError as e:
            return 400, {"error": f"bad JSON body: {e}"}
        if method == "POST":
            with self._tree_lock:
                self._tree_cache.clear()   # the user's own changes show at once
        # A client waits for the answer: report a busy database after 10 s, not 30.
        db.set_label(f"{method} {parts.path}")
        token_id = next(self._request_ids)
        with self._requests_lock:
            self._requests[token_id] = time.monotonic()
        conn = None
        try:
            conn = db.open_existing(self.cfg.db_path, timeout=self.busy_timeout)
            # Who is calling: the client (bearer token) or a paired server.
            header = headers.get("Authorization")
            token, caller = None, None
            if self.check_token(header):
                token, caller = header[7:].strip(), "client"
            else:
                caller = peers.authenticate(conn, header)  # peer server id
            if access == "client" and caller != "client":
                return 401, {"error": "not logged in"}
            if access == "admin" and caller is None:
                return 401, {"error": "not logged in"}
            if access == "peer" and (caller is None or caller == "client"):
                return 401, {"error": "peer authentication required"}

            # The client may address another server: forward the request.
            target = query.pop("server", None) if method == "GET" else body.pop("server", None)
            if target and access == "admin" and caller == "client" \
                    and target != nodes.self_id(conn):
                path = parts.path + ("?" + urlencode(query) if query else "")
                try:
                    return 200, peers.forward(conn, target, method, path,
                                              body if method == "POST" else None)
                except peers.PeerError as e:
                    return (e.status if 400 <= e.status < 500 else 502), {"error": str(e)}

            return 200, fn(conn, query, body, token=token, caller=caller,
                           client_ip=client_ip, **match.groupdict())
        except ApiError as e:
            return e.status, {"error": str(e)}
        except peers.PeerError as e:
            return (e.status if 400 <= e.status < 500 else 502), {"error": str(e)}
        except USER_ERRORS as e:
            return 400, {"error": str(e).strip("'\"")}
        except (sqlite3.OperationalError, db.DatabaseLocked) as e:
            if "disk is full" not in str(e):
                db.note_busy_error()
                log.warning("database busy: %s", e)
            if "disk is full" in str(e):
                log.error("SQLite ran out of room for its temporary files (%s): they go to %s;"
                          " set SQLITE_TMPDIR to a folder with space", e,
                          os.environ.get("SQLITE_TMPDIR") or "the system temp directory")
                return 500, {"error": f"the database ran out of room for temporary files ({e}); "
                                      "set SQLITE_TMPDIR to a folder with more space"}
            return 503, {"error": "the server is busy (its database is locked by another operation); "
                                   f"try again in a moment ({e})"}
        finally:
            if conn is not None:
                conn.close()
            db.set_label(None)
            with self._requests_lock:
                self._requests.pop(token_id, None)


# --- HTTP plumbing -------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = f"rei-fs/{__version__}"
    protocol_version = "HTTP/1.1"
    timeout = 120

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send(413, {"error": "request too large"})
            return
        body = self.rfile.read(length) if length else b""
        try:
            status, payload = self.server.api.dispatch(method, self.path, self.headers, body,
                                                       self.client_address[0])
        except Exception as e:  # noqa: BLE001
            log.exception("internal error")
            status, payload = 500, {"error": f"internal error: {e}"}
        self._send(status, payload)

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s %s", self.address_string(), fmt % args)


# Why connections end early, for the log and the status (a client that "cannot connect").
CONNECTION_STATS = {"handshake_failed": 0, "dropped": 0}
_last_summary = [float("-inf")]   # monotonic time counts from boot: 0 would mean "just now"
_ROUTINE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError,
            ssl.SSLEOFError, ssl.SSLZeroReturnError)


def _connection_problem(kind: str, address: str, error: Exception) -> None:
    """A connection that ended early. A client closing its side (it gave up waiting,
    retried on a fresh connection, quit) is routine: debug level and not counted. Anything
    else is counted for the status, and logged at most once a minute."""
    log.debug("connection from %s: %s: %s", address, kind, error)
    if isinstance(error, _ROUTINE):
        return
    CONNECTION_STATS[kind] += 1
    now = time.monotonic()
    if now - _last_summary[0] >= 60:
        _last_summary[0] = now
        log.info("connection problems so far: %d TLS handshake(s) failed, %d dropped "
                 "(latest from %s: %s); 'reifs serve -v' logs each one",
                 CONNECTION_STATS["handshake_failed"], CONNECTION_STATS["dropped"],
                 address, error)


class TLSServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128   # the default of 5 drops connections when several arrive at once

    def __init__(self, address, api: Api, context: ssl.SSLContext):
        family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.address_family = family
        super().__init__(address, _Handler)
        self.api = api
        self.context = context

    def finish_request(self, request, client_address) -> None:
        # TLS handshake in the request thread, so a slow client cannot block accept().
        request.settimeout(30)
        try:
            request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            request.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        try:
            tls_sock = self.context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError) as e:
            # A client that does not trust our certificate yet hangs up after the
            # handshake: routine. Other failures are counted and summarised.
            _connection_problem("handshake_failed", client_address[0], e)
            return
        try:
            super().finish_request(tls_sock, client_address)
        except (ConnectionError, ssl.SSLError, TimeoutError) as e:
            # Clients that reject our certificate hang up right after the
            # handshake; idle keep-alive connections time out. Both are normal.
            _connection_problem("dropped", client_address[0], e)
        finally:
            try:
                tls_sock.close()
            except OSError:
                pass


def parse_listen(listen: str) -> tuple[str, int]:
    host, sep, port = listen.rpartition(":")
    if not sep:
        raise ValueError(f"listen must be HOST:PORT, got {listen!r}")
    return host.strip("[]") or "0.0.0.0", int(port)


def make_server(cfg: Config, worker: Worker | None, listen: str | None = None,
                syncer: Syncer | None = None) -> TLSServer:
    cert, key = tls.ensure_certificate(cfg.home, cfg.server.name)
    api = Api(cfg, worker, syncer)
    return TLSServer(parse_listen(listen or cfg.server.listen), api,
                     tls.server_context(cert, key))
