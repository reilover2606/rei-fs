"""Talking to other servers: pinned HTTPS client, pairing, peer authentication.

Pairing (design 7.1), between servers A (initiator) and B:

1. The user on A enters B's address. A fetches ``/api/peer/hello`` and shows
   B's certificate fingerprint; the user confirms it (acceptance on A).
2. A sends ``pair-request`` with its id, name, address, own fingerprint and a
   random nonce. B stores an incoming request.
3. The user on B accepts it. B connects to A (pinning A's fingerprint) and
   sends ``pair-confirm`` with the nonce, a token for A to use on B and
   credentials for B's rsync daemon. A checks the nonce and answers with
   its own token and rsync credentials. Both are now paired.

Peers authenticate with ``Authorization: Peer <server-id>:<token>``; each
side stores only a hash of the token it issued.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import secrets
import sqlite3
import ssl
from dataclasses import dataclass

from . import __version__, db, index, tls
from .config import Config

MAX_PENDING_IN = 20


class PeerError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def fingerprint_of_der(der: bytes) -> str:
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def split_address(address: str, default_port: int = 7420) -> tuple[str, int]:
    address = address.strip()
    if address.startswith("["):
        host, _, rest = address[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default_port
    if address.count(":") == 1:
        host, port = address.split(":")
        return host, int(port)
    return address, default_port


def join_address(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class PeerClient:
    """HTTPS JSON client that pins the server certificate fingerprint."""

    def __init__(self, address: str, fingerprint: str | None = None,
                 auth: str | None = None, timeout: float = 15):
        self.host, self.port = split_address(address)
        self.fingerprint = fingerprint
        self.auth = auth
        self.timeout = timeout
        self.seen_fingerprint: str | None = None

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # identity = pinned fingerprint, checked below
        conn = http.client.HTTPSConnection(self.host, self.port, context=ctx,
                                           timeout=self.timeout)
        try:
            try:
                conn.connect()
            except (OSError, ssl.SSLError) as e:
                raise PeerError(f"cannot connect to {join_address(self.host, self.port)}: {e}")
            der = conn.sock.getpeercert(binary_form=True)
            self.seen_fingerprint = fingerprint_of_der(der)
            if self.fingerprint and self.seen_fingerprint != self.fingerprint:
                raise PeerError(
                    f"certificate of {join_address(self.host, self.port)} does not match the "
                    f"pinned fingerprint (got {self.seen_fingerprint})", 495)
            headers = {"Content-Type": "application/json"}
            if self.auth:
                headers["Authorization"] = self.auth
            payload = json.dumps(body).encode() if body is not None else None
            try:
                conn.request(method, path, payload, headers)
                resp = conn.getresponse()
                raw = resp.read()
            except (OSError, http.client.HTTPException) as e:
                raise PeerError(f"request to {join_address(self.host, self.port)} failed: {e}")
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                raise PeerError(f"invalid response from peer (HTTP {resp.status})", resp.status)
            if resp.status >= 400:
                raise PeerError(data.get("error", f"HTTP {resp.status}"), resp.status)
            return data
        finally:
            conn.close()


# --- helpers ---------------------------------------------------------------------------


def my_fingerprint(cfg: Config) -> str:
    cert, _ = tls.ensure_certificate(cfg.home, cfg.server.name)
    return tls.fingerprint(cert)


def listen_port(cfg: Config) -> int:
    return int(cfg.server.listen.rpartition(":")[2])


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def peer_row(conn: sqlite3.Connection, server_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM servers WHERE id = ? AND paired = 1",
                       (server_id,)).fetchone()
    if row is None:
        raise PeerError(f"not paired with server {server_id}")
    return row


def find_peer(conn: sqlite3.Connection, name_or_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM servers WHERE paired = 1 AND (id = ? OR name = ?)",
                       (name_or_id, name_or_id)).fetchone()
    if row is None:
        raise PeerError(f"no paired server named {name_or_id!r}")
    return row


def client_for(conn: sqlite3.Connection, server_id: str, timeout: float = 15) -> PeerClient:
    row = peer_row(conn, server_id)
    return PeerClient(row["address"], row["fingerprint"],
                      f"Peer {index.self_id(conn)}:{row['token_out']}", timeout)


def paired_peers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM servers WHERE paired = 1 ORDER BY name").fetchall()


def authenticate(conn: sqlite3.Connection, header: str | None) -> str | None:
    """Server id of the peer presenting ``Peer <id>:<token>``, or None."""
    if not header or not header.startswith("Peer "):
        return None
    peer_id, _, token = header[5:].strip().partition(":")
    row = conn.execute("SELECT token_in_hash FROM servers WHERE id = ? AND paired = 1",
                       (peer_id,)).fetchone()
    if row is None or not row[0] or not token:
        return None
    if not secrets.compare_digest(row[0], token_hash(token)):
        return None
    return peer_id


def forward(conn: sqlite3.Connection, server_id: str, method: str, path: str,
            body: dict | None = None, timeout: float = 30) -> dict:
    return client_for(conn, server_id, timeout).request(method, path, body)


# --- pairing ----------------------------------------------------------------------------


def hello(address: str) -> tuple[dict, str]:
    """Identity and certificate fingerprint of the server at address (unpinned)."""
    c = PeerClient(address, timeout=10)
    info = c.request("GET", "/api/peer/hello")
    return info, c.seen_fingerprint or ""


def _new_rsync_credentials(peer_id: str) -> tuple[str, str]:
    return f"peer-{peer_id[:8]}", secrets.token_urlsafe(24)


def _rsync_info(cfg: Config, user: str, password: str) -> dict:
    return {"port": cfg.rsync.daemon_port, "user": user, "password": password}


def request_pairing(conn: sqlite3.Connection, cfg: Config, address: str,
                    fingerprint: str) -> dict:
    """Step 2: we (A) ask the server at address (B) to pair."""
    me = index.self_id(conn)
    client = PeerClient(address, fingerprint, timeout=10)
    info = client.request("GET", "/api/peer/hello")
    if info.get("id") == me:
        raise PeerError("that is this server")
    if conn.execute("SELECT 1 FROM servers WHERE id = ? AND paired = 1",
                    (info["id"],)).fetchone():
        raise PeerError(f"already paired with {info.get('name')}")
    nonce = secrets.token_urlsafe(24)
    with db.transaction(conn):
        conn.execute("UPDATE pair_requests SET status = 'failed', message = 'superseded'"
                     " WHERE direction = 'out' AND server_id = ? AND status = 'pending'",
                     (info["id"],))
        conn.execute(
            "INSERT INTO pair_requests(direction, server_id, name, address, fingerprint, nonce,"
            " created) VALUES ('out', ?, ?, ?, ?, ?, ?)",
            (info["id"], info.get("name", ""), address, fingerprint, nonce, db.now()))
    client.request("POST", "/api/peer/pair-request", {
        "id": me, "name": cfg.server.name, "address": cfg.server.advertise,
        "port": listen_port(cfg), "fingerprint": my_fingerprint(cfg), "nonce": nonce,
        "version": __version__,
    })
    return info


def receive_pair_request(conn: sqlite3.Connection, body: dict, client_ip: str) -> None:
    """Step 2, on B: store the incoming request for the user to accept."""
    me = index.self_id(conn)
    for k in ("id", "name", "fingerprint", "nonce"):
        if not isinstance(body.get(k), str) or not body[k]:
            raise ValueError(f"missing {k}")
    if body["id"] == me:
        raise ValueError("cannot pair with myself")
    address = body.get("address") or join_address(client_ip, int(body.get("port") or 7420))
    with db.transaction(conn):
        pending = conn.execute("SELECT COUNT(*) FROM pair_requests WHERE direction = 'in'"
                               " AND status = 'pending'").fetchone()[0]
        conn.execute("DELETE FROM pair_requests WHERE direction = 'in' AND server_id = ?"
                     " AND status = 'pending'", (body["id"],))
        if pending >= MAX_PENDING_IN:
            raise ValueError("too many pending pairing requests")
        conn.execute(
            "INSERT INTO pair_requests(direction, server_id, name, address, fingerprint, nonce,"
            " created) VALUES ('in', ?, ?, ?, ?, ?, ?)",
            (body["id"], body["name"][:64], address, body["fingerprint"], body["nonce"],
             db.now()))


def _store_peer(conn, peer_id: str, name: str, address: str, fingerprint: str,
                token_out: str, token_in: str, rsync_out: dict, rsync_in: tuple[str, str]) -> None:
    conn.execute("INSERT OR IGNORE INTO servers(id, name) VALUES (?, ?)", (peer_id, name))
    conn.execute(
        "UPDATE servers SET name = ?, address = ?, fingerprint = ?, token_out = ?,"
        " token_in_hash = ?, rsync_port = ?, rsync_user = ?, rsync_password = ?,"
        " rsync_in_user = ?, rsync_in_password = ?, paired = 1, pull_cursor = 0,"
        " last_error = NULL WHERE id = ?",
        (name, address, fingerprint, token_out, token_hash(token_in),
         int(rsync_out.get("port") or 0), rsync_out.get("user"), rsync_out.get("password"),
         rsync_in[0], rsync_in[1], peer_id))


def accept_pairing(conn: sqlite3.Connection, cfg: Config, request_id: int) -> str:
    """Step 3, on B: the user accepted; complete the handshake with A."""
    req = conn.execute("SELECT * FROM pair_requests WHERE id = ? AND direction = 'in'"
                       " AND status = 'pending'", (request_id,)).fetchone()
    if req is None:
        raise PeerError(f"no pending incoming pairing request #{request_id}")
    token_for_peer = secrets.token_urlsafe(32)
    rsync_in = _new_rsync_credentials(req["server_id"])
    client = PeerClient(req["address"], req["fingerprint"], timeout=15)
    try:
        answer = client.request("POST", "/api/peer/pair-confirm", {
            "id": index.self_id(conn), "name": cfg.server.name, "nonce": req["nonce"],
            "token": token_for_peer, "rsync": _rsync_info(cfg, *rsync_in),
        })
    except PeerError as e:
        with db.transaction(conn):
            conn.execute("UPDATE pair_requests SET message = ? WHERE id = ?", (str(e), req["id"]))
        raise
    with db.transaction(conn):
        _store_peer(conn, req["server_id"], req["name"], req["address"], req["fingerprint"],
                    answer["token"], token_for_peer, answer.get("rsync", {}), rsync_in)
        conn.execute("UPDATE pair_requests SET status = 'accepted' WHERE id = ?", (req["id"],))
    return req["name"]


def confirm_pairing(conn: sqlite3.Connection, cfg: Config, body: dict) -> dict:
    """Step 3, on A: B accepted our request; check the nonce and finish."""
    req = conn.execute(
        "SELECT * FROM pair_requests WHERE direction = 'out' AND status = 'pending'"
        " AND server_id = ?", (body.get("id"),)).fetchone()
    if req is None or not secrets.compare_digest(req["nonce"], str(body.get("nonce", ""))):
        raise PeerError("no matching pairing request", 403)
    token_for_peer = secrets.token_urlsafe(32)
    rsync_in = _new_rsync_credentials(req["server_id"])
    with db.transaction(conn):
        _store_peer(conn, req["server_id"], body.get("name") or req["name"], req["address"],
                    req["fingerprint"], str(body["token"]), token_for_peer,
                    body.get("rsync") or {}, rsync_in)
        conn.execute("UPDATE pair_requests SET status = 'accepted' WHERE id = ?", (req["id"],))
    return {"token": token_for_peer, "rsync": _rsync_info(cfg, *rsync_in)}


def reject_pairing(conn: sqlite3.Connection, request_id: int) -> None:
    req = conn.execute("SELECT * FROM pair_requests WHERE id = ? AND status = 'pending'",
                       (request_id,)).fetchone()
    if req is None:
        raise PeerError(f"no pending pairing request #{request_id}")
    with db.transaction(conn):
        conn.execute("UPDATE pair_requests SET status = 'rejected' WHERE id = ?", (request_id,))
    if req["direction"] == "in":
        try:  # let the requester know (best effort)
            PeerClient(req["address"], req["fingerprint"], timeout=5).request(
                "POST", "/api/peer/pair-reject", {"id": index.self_id(conn), "nonce": req["nonce"]})
        except PeerError:
            pass


def receive_pair_reject(conn: sqlite3.Connection, body: dict) -> None:
    with db.transaction(conn):
        for req in conn.execute("SELECT id, nonce FROM pair_requests WHERE direction = 'out'"
                                " AND status = 'pending' AND server_id = ?",
                                (body.get("id"),)).fetchall():
            if secrets.compare_digest(req["nonce"], str(body.get("nonce", ""))):
                conn.execute("UPDATE pair_requests SET status = 'rejected',"
                             " message = 'rejected by the other server' WHERE id = ?", (req["id"],))


def forget_peer(conn: sqlite3.Connection, server_id: str) -> None:
    with db.transaction(conn):
        conn.execute(
            "UPDATE servers SET paired = 0, token_out = NULL, token_in_hash = NULL,"
            " rsync_user = NULL, rsync_password = NULL, rsync_in_user = NULL,"
            " rsync_in_password = NULL, online = 0 WHERE id = ?", (server_id,))
        conn.execute("DELETE FROM outbox WHERE server_id = ?", (server_id,))


def unpair(conn: sqlite3.Connection, server_id: str) -> None:
    try:
        forward(conn, server_id, "POST", "/api/peer/unpair", {}, timeout=5)
    except PeerError:
        pass  # it will refuse our token anyway
    forget_peer(conn, server_id)


# --- remote rsync sources ------------------------------------------------------------


@dataclass
class RemoteSource:
    host: str
    port: int
    module: str
    user: str
    password: str

    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"rsync://{self.user}@{host}:{self.port}/{self.module}/"


def module_name(node_id: str) -> str:
    return f"n-{node_id}"


def remote_source(peer: sqlite3.Row, node_id: str) -> RemoteSource | None:
    if not (peer["rsync_port"] and peer["rsync_user"] and peer["rsync_password"]):
        return None
    host, _ = split_address(peer["address"])
    return RemoteSource(host, int(peer["rsync_port"]), module_name(node_id),
                        peer["rsync_user"], peer["rsync_password"])
