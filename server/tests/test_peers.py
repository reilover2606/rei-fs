"""Several servers in one process: pairing, metadata sync, remote transfers."""

import shutil
import socket
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from reifs import (api, auth, db, decisions, jobs, nodes, peers, queries, runner, scanner,
                   tags)
from reifs.server import init_server
from reifs.service import Syncer, Worker, sync_peer

from conftest import write


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Srv:
    name: str
    cfg: object
    conn: object
    root: Path
    tmp: Path
    httpd: object
    syncer: Syncer
    worker: Worker

    @property
    def id(self) -> str:
        return nodes.self_id(self.conn)

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.httpd.server_address[1]}"

    def add_node(self, name: str) -> Path:
        path = self.tmp / f"{self.name}-{name}"
        path.mkdir()
        nodes.register_node(self.conn, path, name)
        return path

    def scan(self):
        return scanner.scan_all(self.conn, self.cfg)[1]

    def run_jobs(self):
        return {r.job_id: r for r in runner.run_jobs(self.conn, self.cfg)}

    def sync(self, other: "Srv") -> int:
        return sync_peer(self.conn, other.id)

    def lookup(self, vpath):
        return queries.lookup(self.conn, vpath)

    def copies(self, vpath):
        info = self.lookup(vpath)
        return {c.node_name: c for c in info.copies} if info else {}


@pytest.fixture
def cluster(tmp_path):
    made: list[Srv] = []

    def make(name: str) -> Srv:
        root = tmp_path / f"{name}-root"
        cfg = init_server(tmp_path / f"{name}-home", name, root)
        cfg.server.password_hash = auth.hash_password("pw")
        cfg.discovery.paths = [str(tmp_path / "nomounts")]
        cfg.scan.interval_minutes = 0
        cfg.rsync.daemon_port = free_port()
        syncer = Syncer(cfg)
        worker = Worker(cfg, syncer)
        httpd = api.make_server(cfg, worker, "127.0.0.1:0", syncer)
        cfg.server.listen = f"127.0.0.1:{httpd.server_address[1]}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        srv = Srv(name, cfg, db.open_existing(cfg.db_path), root, tmp_path, httpd, syncer, worker)
        made.append(srv)
        return srv

    yield make
    for s in made:
        s.httpd.shutdown()
        s.httpd.server_close()
        s.syncer.daemon.stop()
        s.conn.close()


def pair(a: Srv, b: Srv) -> None:
    info, fp = peers.hello(b.address)
    assert info["id"] == b.id
    peers.request_pairing(a.conn, a.cfg, b.address, fp)
    [req] = b.conn.execute("SELECT * FROM pair_requests WHERE direction = 'in'"
                           " AND status = 'pending'").fetchall()
    assert req["server_id"] == a.id
    peers.accept_pairing(b.conn, b.cfg, req["id"])
    for s in (a, b):
        s.syncer.daemon.update(s.conn)


def test_pairing_handshake(cluster):
    a, b = cluster("alpha"), cluster("beta")
    pair(a, b)
    assert peers.find_peer(a.conn, "beta")["id"] == b.id
    assert peers.find_peer(b.conn, "alpha")["id"] == a.id
    # Peer authentication works both ways; forged tokens do not.
    assert peers.forward(a.conn, b.id, "GET", "/api/peer/changes?since=0")["changes"] is not None
    assert peers.forward(b.conn, a.id, "GET", "/api/status")["server"]["name"] == "alpha"
    bad = peers.PeerClient(b.address, peers.peer_row(a.conn, b.id)["fingerprint"],
                           f"Peer {a.id}:forged")
    with pytest.raises(peers.PeerError) as e:
        bad.request("GET", "/api/peer/changes?since=0")
    assert e.value.status == 401
    # Peers cannot use client-only endpoints (pairing management).
    with pytest.raises(peers.PeerError):
        peers.forward(b.conn, a.id, "GET", "/api/peers")


def test_pairing_wrong_fingerprint_and_reject(cluster):
    a, b, c = cluster("alpha"), cluster("beta"), cluster("gamma")
    with pytest.raises(peers.PeerError):
        peers.request_pairing(a.conn, a.cfg, b.address, "AA:" * 31 + "AA")
    _, fp = peers.hello(c.address)
    peers.request_pairing(a.conn, a.cfg, c.address, fp)
    [req] = c.conn.execute("SELECT id FROM pair_requests WHERE direction = 'in'").fetchall()
    peers.reject_pairing(c.conn, req["id"])
    out = a.conn.execute("SELECT status FROM pair_requests WHERE direction = 'out'").fetchone()
    assert out["status"] == "rejected"
    # A forged confirmation (wrong nonce) is refused.
    with pytest.raises(peers.PeerError):
        peers.PeerClient(a.address).request("POST", "/api/peer/pair-confirm",
                                            {"id": c.id, "nonce": "guess", "token": "x"})


def test_sync_files_nodes_tags(cluster):
    a, b = cluster("alpha"), cluster("beta")
    write(a.root / "docs" / "a.txt", "alpha", mtime=1_000_000)
    a.scan()
    tags.set_tag(a.conn, tags.resolve_targets(a.conn, "/docs/a.txt", False), "work", True, a.id)
    pair(a, b)
    assert b.sync(a) > 0
    info = b.lookup("/docs/a.txt")
    assert info.tags == ["work"]
    assert {c.node_name for c in info.copies} == {"alpha"}
    node = b.conn.execute("SELECT * FROM nodes WHERE name = 'alpha'").fetchone()
    assert node["owner_id"] == a.id and node["kind"] == "server"
    # Tag edits on B flow back to A; the newest edit wins.
    tags.set_tag(b.conn, [info.id], "urgent", True, b.id)
    tags.set_tag(b.conn, [info.id], "work", False, b.id)
    a.sync(b)
    assert a.lookup("/docs/a.txt").tags == ["urgent"]
    # Removals propagate.
    (a.root / "docs" / "a.txt").unlink()
    a.scan()
    [d] = a.conn.execute("SELECT id FROM decisions WHERE status = 'open'").fetchall()
    decisions.resolve(a.conn, d["id"], "acknowledge")
    b.sync(a)
    assert b.lookup("/docs/a.txt") is None


def test_changes_are_forwarded(cluster):
    a, b, c = cluster("alpha"), cluster("beta"), cluster("gamma")
    write(a.root / "x.txt", "x")
    a.scan()
    pair(a, b)
    pair(b, c)
    b.sync(a)
    c.sync(b)
    assert {cp.node_name for cp in c.lookup("/x.txt").copies} == {"alpha"}
    # C's own facts reach A through B, and nothing is applied twice.
    write(c.root / "y.txt", "y")
    c.scan()
    b.sync(c)
    a.sync(b)
    assert a.lookup("/y.txt") is not None
    assert b.sync(c) == 0 and a.sync(b) == 0


def test_node_name_clash(cluster):
    a, b = cluster("alpha"), cluster("beta")
    a.add_node("ssd")
    b.add_node("ssd")
    pair(a, b)
    b.sync(a)
    names = {r["name"] for r in b.conn.execute("SELECT name FROM nodes")}
    assert names == {"alpha", "beta", "ssd", "ssd@alpha"}


needs_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


@needs_rsync
def test_remote_copy_pulls_through_rsync_daemon(cluster):
    a, b = cluster("alpha"), cluster("beta")
    write(a.root / "photos" / "p1.jpg", "picture one", mtime=1_000_000)
    write(a.root / "photos" / "p2.jpg", "picture two", mtime=1_000_000)
    a.scan()
    pair(a, b)
    b.sync(a)
    a.sync(b)
    rep = jobs.create_transfer_job(b.conn, ["/photos"], None, "beta")
    r = b.run_jobs()[rep.job_id]
    assert (r.status, r.done) == ("done", 2), r.messages
    assert (b.root / "photos" / "p1.jpg").read_text() == "picture one"
    assert (b.root / "photos" / "p1.jpg").stat().st_mtime == 1_000_000
    assert b.lookup("/photos/p1.jpg").good_copies == 2
    a.sync(b)
    assert a.lookup("/photos/p2.jpg").good_copies == 2
    # The daemon is read-only: B cannot push into A.
    remote = peers.remote_source(peers.peer_row(b.conn, a.id), a.id)
    import os
    import subprocess
    write(b.tmp / "evil.txt", "x")
    result = subprocess.run(["rsync", str(b.tmp / "evil.txt"), remote.url()],
                            env=dict(os.environ, RSYNC_PASSWORD=remote.password),
                            capture_output=True, text=True)
    assert result.returncode != 0 and not (a.root / "evil.txt").exists()


@needs_rsync
def test_remote_move_marks_source_on_its_owner(cluster):
    a, b = cluster("alpha"), cluster("beta")
    write(a.root / "m.txt", "move me", mtime=1_000_000)
    a.scan()
    pair(a, b)
    b.sync(a)
    a.sync(b)
    rep = jobs.create_transfer_job(b.conn, ["/m.txt"], None, "beta", src="alpha", move=True)
    assert b.run_jobs()[rep.job_id].done == 1
    # B may not mark A's copy itself: it asked A (outbox), delivered on sync.
    assert not b.copies("/m.txt")["alpha"].marked_delete
    assert b.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    b.sync(a)
    assert b.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert a.copies("/m.txt")["alpha"].marked_delete
    b.sync(a)
    assert b.copies("/m.txt")["alpha"].marked_delete
    assert (a.root / "m.txt").exists()  # never deleted


@needs_rsync
def test_api_forwards_jobs_and_merges_lists(cluster):
    import json
    import ssl
    import urllib.request

    a, b = cluster("alpha"), cluster("beta")
    write(a.root / "f.txt", "file", mtime=1_000_000)
    a.scan()
    pair(a, b)
    b.sync(a)
    a.sync(b)

    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE

    def call(method, path, body=None, token=None):
        req = urllib.request.Request(f"https://{a.address}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, context=ctx, timeout=20) as r:
            return json.loads(r.read())

    token = call("POST", "/api/login", {"password": "pw"})["token"]
    # Copy to B's node through A: the job is created on (and run by) B.
    out = call("POST", "/api/jobs", {"kind": "copy", "paths": ["/f.txt"], "to": "beta"}, token)
    assert out["server"] == b.id and out["items"] == 1
    jobs_everywhere = call("GET", "/api/jobs?everywhere=1", token=token)["jobs"]
    assert [(j["server_name"], j["status"]) for j in jobs_everywhere] == [("beta", "pending")]
    b.run_jobs()
    # A decision on B, resolved from A.
    (b.root / "f.txt").unlink()
    b.scan()
    [d] = call("GET", "/api/decisions?everywhere=1", token=token)["decisions"]
    assert d["server_name"] == "beta" and d["kind"] == "missing"
    call("POST", f"/api/decisions/{d['id']}", {"option": "forget", "server": b.id}, token)
    assert call("GET", "/api/decisions?everywhere=1", token=token)["decisions"] == []
    peers_info = call("GET", "/api/peers", token=token)
    assert [p["name"] for p in peers_info["peers"]] == ["beta"]


def test_adopt_drive_moved_to_another_server(cluster):
    a, b = cluster("alpha"), cluster("beta")
    ssd = a.add_node("ssd")
    write(ssd / "s.txt", "on the ssd")
    a.scan()
    pair(a, b)
    b.sync(a)
    # The drive is plugged into B's machine.
    b.cfg.discovery.paths = [str(ssd.parent)]
    b.cfg.discovery.max_depth = 1
    nodes.discover(b.conn, b.cfg)
    assert "ssd" in [r["name"] for r in b.conn.execute("SELECT name FROM foreign_sightings")]
    nodes.adopt_node(b.conn, "ssd")
    assert nodes.read_marker(ssd).owner_id == b.id
    b.scan()
    a.sync(b)
    row = a.conn.execute("SELECT owner_id, root_path FROM nodes WHERE name = 'ssd'").fetchone()
    assert row["owner_id"] == b.id and row["root_path"] is None
    assert "ssd" not in [loc.name for loc in nodes.discover(a.conn, a.cfg).online]
    assert {c.node_name for c in a.lookup("/s.txt").copies} == {"ssd"}
    # A's former announcements do not take the node back.
    b.sync(a)
    assert b.conn.execute("SELECT owner_id FROM nodes WHERE name = 'ssd'").fetchone()[0] == b.id


def test_divergence_raised_by_owner_of_stale_copy(cluster):
    a, b = cluster("alpha"), cluster("beta")
    write(a.root / "d.txt", "v1", mtime=1_000_000)
    write(b.root / "d.txt", "v1", mtime=1_000_000)
    a.scan()
    b.scan()
    pair(a, b)
    write(a.root / "d.txt", "v2 newer", mtime=2_000_000)
    a.scan()
    b.sync(a)
    a.sync(b)
    open_kinds = lambda s: [r[0] for r in s.conn.execute(  # noqa: E731
        "SELECT kind FROM decisions WHERE status = 'open'")]
    assert open_kinds(b) == ["divergent"]   # B owns the stale copy
    assert open_kinds(a) == []


def test_unpair(cluster):
    a, b = cluster("alpha"), cluster("beta")
    pair(a, b)
    peers.unpair(a.conn, b.id)
    assert a.conn.execute("SELECT paired FROM servers WHERE id = ?", (b.id,)).fetchone()[0] == 0
    assert b.conn.execute("SELECT paired FROM servers WHERE id = ?", (a.id,)).fetchone()[0] == 0
    with pytest.raises(peers.PeerError):
        peers.forward(b.conn, a.id, "GET", "/api/status")
