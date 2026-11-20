"""HTTPS JSON API used by the client (``reifs serve``).

Every endpoint lives under ``/api``. Apart from ``POST /api/login`` every
request needs ``Authorization: Bearer <token>``. Responses are JSON objects;
errors are ``{"error": "..."}`` with a 4xx/5xx status.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import socket
import sqlite3
import ssl
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from . import (__version__, auth, db, decisions, jobs, nodes, paths, queries, tags,
               tls)
from .config import Config
from .paths import to_physical
from .service import Worker

log = logging.getLogger("reifs.api")

TOKEN_TTL = 12 * 3600
MAX_BODY = 1 << 20
FIND_LIMIT = 1000


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


USER_ERRORS = (jobs.JobError, decisions.DecisionError, tags.TagError, nodes.NodeError,
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


def decision_json(d: decisions.Decision, names: dict[str, str]) -> dict:
    if d.kind == "missing":
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
    return {"id": d.id, "kind": d.kind, "path": d.vpath or d.data.get("vpath"),
            "node": d.node_name, "summary": summary, "created": d.created,
            "options": [{"key": k, "label": v} for k, v in d.options.items()]}


def job_json(conn: sqlite3.Connection, j: sqlite3.Row) -> dict:
    counts = jobs.item_counts(conn, j["id"])
    return {"id": j["id"], "kind": j["kind"], "status": j["status"],
            "description": j["description"], "created": j["created"],
            "started": j["started"], "finished": j["finished"], "counts": counts,
            "total": sum(counts.values()), "done": counts.get("done", 0)}


# --- the API ------------------------------------------------------------------------


Route = tuple[str, re.Pattern, Callable, bool]


class Api:
    def __init__(self, cfg: Config, worker: Worker | None):
        self.cfg = cfg
        self.worker = worker
        self.tokens: dict[str, float] = {}
        self.tokens_lock = threading.Lock()
        self.server_id = ""
        self.routes: list[Route] = []
        r = self._route
        r("POST", r"/api/login", self.login, public=True)
        r("POST", r"/api/logout", self.logout)
        r("GET", r"/api/status", self.status)
        r("GET", r"/api/nodes", self.list_nodes)
        r("POST", r"/api/nodes/(?P<node>[^/]+)", self.update_node)
        r("POST", r"/api/scan", self.scan)
        r("POST", r"/api/stop", self.stop)
        r("GET", r"/api/tree", self.tree)
        r("GET", r"/api/file", self.file)
        r("GET", r"/api/find", self.find)
        r("GET", r"/api/tags", self.list_tags)
        r("POST", r"/api/tags", self.edit_tags)
        r("GET", r"/api/decisions", self.list_decisions)
        r("POST", r"/api/decisions/(?P<id>\d+)", self.resolve_decision)
        r("GET", r"/api/wanted", self.list_wanted)
        r("POST", r"/api/wanted", self.act_wanted)
        r("GET", r"/api/jobs", self.list_jobs)
        r("POST", r"/api/jobs", self.create_job)
        r("POST", r"/api/jobs/run", self.run_jobs)
        r("GET", r"/api/jobs/(?P<id>\d+)", self.show_job)
        r("POST", r"/api/jobs/(?P<id>\d+)/(?P<action>cancel|retry)", self.job_action)
        r("GET", r"/api/marked", self.list_marked)
        r("POST", r"/api/unmark", self.unmark)

    def _route(self, method: str, pattern: str, fn: Callable, public: bool = False) -> None:
        self.routes.append((method, re.compile(pattern + "$"), fn, public))

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

    def status(self, conn, q, body, **_):
        s = queries.status(conn)
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
            "active_jobs": conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('pending', 'running', 'waiting')"
            ).fetchone()[0],
        }

    def list_nodes(self, conn, q, body, **_):
        me = nodes.self_id(conn)
        out = []
        for r in conn.execute(
            "SELECT n.*, s.name AS owner, (SELECT COUNT(*) FROM copies c WHERE c.node_id = n.id)"
            " AS files, (SELECT COALESCE(SUM(size), 0) FROM copies c WHERE c.node_id = n.id)"
            " AS bytes FROM nodes n JOIN servers s ON s.id = n.owner_id ORDER BY n.name"
        ):
            out.append({
                "id": r["id"], "name": r["name"], "kind": r["kind"], "online": bool(r["online"]),
                "owner": "self" if r["owner_id"] == me else r["owner"],
                "files": r["files"], "bytes": r["bytes"],
                "case_sensitive": bool(r["case_sensitive"]),
                "restricted_names": bool(r["restricted_names"]), "priority": r["priority"],
                "replication_target": bool(r["replication_target"]),
                "root": r["root_path"] or "", "last_scan": r["last_scan"] or "",
            })
        return {"nodes": out}

    def update_node(self, conn, q, body, node, **_):
        row = nodes.get_node(conn, unquote(node))
        if "priority" in body:
            conn.execute("UPDATE nodes SET priority = ? WHERE id = ?",
                         (int(body["priority"]), row["id"]))
        if "replication_target" in body:
            conn.execute("UPDATE nodes SET replication_target = ? WHERE id = ?",
                         (int(bool(body["replication_target"])), row["id"]))
        return {"ok": True}

    def scan(self, conn, q, body, **_):
        self._need_worker().request_scan(body.get("nodes") or None, bool(body.get("verify")))
        return {"ok": True, "queued": True}

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
        entries = queries.list_dir(conn, vdir)
        if entries is None:
            raise ApiError(404, f"not a directory in the virtual tree: {vdir}")
        out = []
        for e in entries:
            if e.is_dir:
                out.append({"name": e.name, "dir": True, "files": e.files})
            else:
                f = file_json(e.file, full=False)
                f["dir"] = False
                out.append(f)
        return {"path": vdir, "entries": out}

    def file(self, conn, q, body, **_):
        info = queries.lookup(conn, q.get("path", ""))
        if info is None:
            raise ApiError(404, f"not a file in the virtual tree: {q.get('path')}")
        return file_json(info)

    def find(self, conn, q, body, **_):
        limit = min(int(q.get("limit", FIND_LIMIT)), FIND_LIMIT)
        results = queries.find(conn, q.get("q") or None, tag=q.get("tag") or None,
                               node=q.get("node") or None, issues_only=q.get("issues") == "1",
                               under_replicated=q.get("under") == "1")
        return {"total": len(results), "truncated": len(results) > limit,
                "files": [file_json(f, full=False) for f in results[:limit]]}

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

    def list_decisions(self, conn, q, body, **_):
        names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM nodes")}
        return {"decisions": [decision_json(d, names)
                              for d in decisions.list_decisions(conn)]}

    def resolve_decision(self, conn, q, body, id, **_):
        summary = decisions.resolve(conn, int(id), str(body.get("option", "")))
        if self.worker and body.get("option") == "overwrite":
            self.worker.request_jobs()
        return {"ok": True, "summary": summary}

    def list_wanted(self, conn, q, body, **_):
        out = []
        for r in conn.execute(
            "SELECT w.*, f.vpath, s.name AS src, d.name AS dst FROM wanted w"
            " LEFT JOIN files f ON f.id = w.file_id LEFT JOIN nodes s ON s.id = w.src_node"
            " LEFT JOIN nodes d ON d.id = w.dst_node WHERE w.status = 'open' ORDER BY w.id"
        ):
            data = json.loads(r["data"])
            path = f"{data['from']} -> {data['to']}" if r["op"] == "rename" else r["vpath"]
            out.append({"id": r["id"], "op": r["op"], "path": path or "",
                        "from": r["src"] or "", "to": r["dst"] or "", "reason": r["reason"]})
        return {"wanted": out}

    def act_wanted(self, conn, q, body, **_):
        ids = None if body.get("all") else [int(i) for i in body.get("ids", [])]
        if ids is not None and not ids:
            raise ApiError(400, "give 'ids' or 'all'")
        if body.get("action") == "approve":
            rep = jobs.approve_wanted(conn, ids)
            if rep.job_id is not None and self.worker:
                self.worker.request_jobs()
            return {"job_id": rep.job_id, "items": rep.items}
        if body.get("action") == "reject":
            return {"rejected": jobs.reject_wanted(conn, ids)}
        raise ApiError(400, "action must be 'approve' or 'reject'")

    # --- jobs ------------------------------------------------------------------------

    def list_jobs(self, conn, q, body, **_):
        sql = "SELECT * FROM jobs"
        if q.get("all") != "1":
            sql += " WHERE status NOT IN ('done', 'cancelled')"
        sql += " ORDER BY id DESC LIMIT 200"
        return {"jobs": [job_json(conn, j) for j in conn.execute(sql).fetchall()]}

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
        if kind not in ("copy", "move"):
            raise ApiError(400, "kind must be 'copy' or 'move'")
        rep = jobs.create_transfer_job(conn, list(body.get("paths", [])), body.get("tag") or None,
                                       str(body.get("to", "")), body.get("from") or None,
                                       move=kind == "move")
        if rep.job_id is not None and self.worker:
            self.worker.request_jobs()
        return {"job_id": rep.job_id, "items": rep.items,
                "excluded": {k: len(v) for k, v in rep.excluded.items()}}

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

    # --- deletion marks ------------------------------------------------------------

    def list_marked(self, conn, q, body, **_):
        rows = jobs.marked_copies(conn, q.get("node") or None)
        return {"marked": [{
            "path": r["vpath"], "node": r["node_name"], "online": bool(r["online"]),
            "size": r["size"],
            "physical": str(to_physical(r["root_path"], r["vpath"])) if r["root_path"] else "",
        } for r in rows]}

    def unmark(self, conn, q, body, **_):
        paths_ = [] if body.get("all") else list(body.get("paths", []))
        if not paths_ and not body.get("all"):
            raise ApiError(400, "give 'paths' or 'all'")
        return {"unmarked": jobs.unmark(conn, paths_, body.get("node") or None)}

    # --- dispatch ---------------------------------------------------------------------

    def dispatch(self, method: str, raw_path: str, headers, body_bytes: bytes) -> tuple[int, dict]:
        parts = urlsplit(raw_path)
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        for m, pattern, fn, public in self.routes:
            match = pattern.match(parts.path)
            if not match:
                continue
            if m != method:
                continue
            token = None
            if not public:
                header = headers.get("Authorization")
                if not self.check_token(header):
                    return 401, {"error": "not logged in"}
                token = header[7:].strip()
            try:
                body = json.loads(body_bytes or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
            except ValueError as e:
                return 400, {"error": f"bad JSON body: {e}"}
            conn = db.open_existing(self.cfg.db_path)
            try:
                return 200, fn(conn, query, body, token=token, **match.groupdict())
            except ApiError as e:
                return e.status, {"error": str(e)}
            except USER_ERRORS as e:
                return 400, {"error": str(e).strip("'\"")}
            except sqlite3.OperationalError as e:
                log.warning("database busy: %s", e)
                return 503, {"error": f"database busy, try again ({e})"}
            finally:
                conn.close()
        return 404, {"error": f"no such endpoint: {method} {parts.path}"}


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
            status, payload = self.server.api.dispatch(method, self.path, self.headers, body)
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


class TLSServer(ThreadingHTTPServer):
    daemon_threads = True

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
            tls_sock = self.context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError) as e:
            log.debug("TLS handshake with %s failed: %s", client_address, e)
            return
        try:
            super().finish_request(tls_sock, client_address)
        except (ConnectionError, ssl.SSLError, TimeoutError) as e:
            # Clients that reject our certificate hang up right after the
            # handshake; idle keep-alive connections time out. Both are normal.
            log.debug("connection from %s ended: %s", client_address, e)
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


def make_server(cfg: Config, worker: Worker | None, listen: str | None = None) -> TLSServer:
    cert, key = tls.ensure_certificate(cfg.home, cfg.server.name)
    api = Api(cfg, worker)
    return TLSServer(parse_listen(listen or cfg.server.listen), api,
                     tls.server_context(cert, key))
