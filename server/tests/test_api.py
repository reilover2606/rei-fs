import hashlib
import json
import shutil
import sys
import ssl
import threading
import time
import urllib.error
import urllib.request

import pytest

ODD_NAME = "b.txt" if sys.platform == "win32" else "b?.txt"

from reifs import api, auth, tls
from reifs.service import Worker

from conftest import write


class Client:
    def __init__(self, port: int):
        self.base = f"https://127.0.0.1:{port}"
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self.token = None

    def call(self, method, path, body=None, expect=200):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=10) as r:
                status, payload = r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            status, payload = e.code, json.loads(e.read())
        assert status == expect, payload
        return payload

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body or {}, **kw)


@pytest.fixture
def server(env):
    env.cfg.server.password_hash = auth.hash_password("pw")
    env.cfg.scan.interval_minutes = 0
    _, ssd = env.add_node("ssd")
    env.ssd = ssd
    write(env.root / "docs" / "a.txt", "alpha", mtime=1_000_000)
    # Windows cannot create such names; elsewhere they are flagged non-portable.
    write(env.root / "docs" / ODD_NAME, "beta", mtime=1_000_000)
    write(env.root / "top.txt", "top", mtime=1_000_000)
    env.scan()
    worker = Worker(env.cfg)
    srv = api.make_server(env.cfg, worker, "127.0.0.1:0")
    worker.start()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    client = Client(srv.server_address[1])
    yield env, client, srv
    srv.shutdown()
    srv.server_close()
    worker.close()


def login(client, pw="pw"):
    out = client.post("/api/login", {"password": pw})
    client.token = out["token"]
    return out


def wait_idle(client, timeout=20):
    deadline = time.time() + timeout
    time.sleep(0.2)
    while time.time() < deadline:
        a = client.get("/api/status")["activity"]
        if a["state"] == "idle" and a["queued"] == 0:
            return a
        time.sleep(0.1)
    raise AssertionError("worker did not become idle")


def test_auth(server):
    env, client, _ = server
    client.get("/api/status", expect=401)
    client.post("/api/login", {"password": "nope"}, expect=401)
    out = login(client)
    assert out["server"]["name"] == "srv"
    assert client.get("/api/status")["files"] == 3
    client.post("/api/logout")
    client.get("/api/status", expect=401)
    client.token = "forged"
    client.get("/api/status", expect=401)


def test_certificate_fingerprint_matches(server):
    env, client, srv = server
    pem = ssl.get_server_certificate(("127.0.0.1", srv.server_address[1]))
    der = ssl.PEM_cert_to_DER_cert(pem)
    seen = hashlib.sha256(der).hexdigest().upper()
    cert, _ = tls.paths(env.cfg.home)
    assert tls.fingerprint(cert).replace(":", "") == seen


def test_browse(server):
    env, client, _ = server
    login(client)
    root = client.get("/api/tree?path=/")
    assert [(e["name"], e["dir"]) for e in root["entries"]] == [("docs", True), ("top.txt", False)]
    docs = client.get("/api/tree?path=/docs")
    names = {e["name"]: e for e in docs["entries"]}
    if sys.platform != "win32":
        assert names[ODD_NAME]["issues"] == ["chars"]
    client.get("/api/tree?path=/nope", expect=404)
    f = client.get("/api/file?path=" + urllib.request.quote("/docs/a.txt"))
    assert f["good_copies"] == 1 and f["copies"][0]["node"] == "srv"
    found = client.get("/api/find?q=*.txt&issues=1")
    expected = [] if sys.platform == "win32" else ["/docs/b?.txt"]
    assert [x["path"] for x in found["files"]] == expected
    nodes = client.get("/api/nodes")["nodes"]
    assert {n["name"] for n in nodes} == {"srv", "ssd"}
    client.post("/api/nodes/ssd", {"priority": 5})
    assert {n["name"]: n["priority"] for n in client.get("/api/nodes")["nodes"]}["ssd"] == 5


def test_tags(server):
    env, client, _ = server
    login(client)
    client.post("/api/tags", {"action": "add", "tag": "x", "paths": ["/docs"]}, expect=400)
    assert client.post("/api/tags", {"action": "add", "tag": "x", "paths": ["/docs"],
                                     "recursive": True})["changed"] == 2
    assert client.get("/api/tags")["tags"] == [{"tag": "x", "files": 2}]
    assert len(client.get("/api/find?tag=x")["files"]) == 2


def test_job_runs_in_background(server):
    env, client, _ = server
    if shutil.which("rsync") is None:
        pytest.skip("rsync not installed")
    login(client)
    out = client.post("/api/jobs", {"kind": "copy", "paths": ["/docs"], "to": "ssd"})
    assert out["items"] == 2
    wait_idle(client)
    job = client.get(f"/api/jobs/{out['job_id']}?all=1")
    assert job["status"] == "done" and job["done"] == 2
    assert (env.ssd / "docs" / "a.txt").read_text() == "alpha"
    assert client.get("/api/status")["satisfied"] == 2
    # Move then marks; unmark through the API.
    out = client.post("/api/jobs", {"kind": "move", "paths": ["/top.txt"], "from": "srv",
                                    "to": "ssd"})
    wait_idle(client)
    marked = client.get("/api/marked")["marked"]
    assert [m["path"] for m in marked] == ["/top.txt"]
    assert client.post("/api/unmark", {"paths": ["/top.txt"]})["unmarked"] == 1
    client.post("/api/jobs", {"kind": "copy", "paths": ["/nope"], "to": "ssd"}, expect=400)


def test_scan_and_decisions(server):
    env, client, _ = server
    login(client)
    (env.root / "top.txt").unlink()
    client.post("/api/scan")
    wait_idle(client)
    [d] = client.get("/api/decisions")["decisions"]
    assert d["kind"] == "missing" and [o["key"] for o in d["options"]] == ["acknowledge"]
    client.post(f"/api/decisions/{d['id']}", {"option": "bogus"}, expect=400)
    client.post(f"/api/decisions/{d['id']}", {"option": "acknowledge"})
    assert client.get("/api/decisions")["decisions"] == []


def test_wanted(server):
    env, client, _ = server
    if shutil.which("rsync") is None:
        pytest.skip("rsync not installed")
    login(client)
    client.post("/api/jobs", {"kind": "copy", "paths": ["/top.txt"], "to": "ssd"})
    wait_idle(client)
    (env.ssd / "top.txt").unlink()
    client.post("/api/scan")
    wait_idle(client)
    [d] = client.get("/api/decisions")["decisions"]
    client.post(f"/api/decisions/{d['id']}", {"option": "restore"})
    [w] = [x for x in client.get("/api/wanted")["wanted"] if x["reason"] == "restore"]
    assert (w["op"], w["to"]) == ("copy", "ssd")
    client.post("/api/wanted", {"action": "approve", "ids": [w["id"]]})
    wait_idle(client)
    assert (env.ssd / "top.txt").exists()


def test_stop(server):
    env, client, _ = server
    login(client)
    client.post("/api/stop")
    assert client.get("/api/status")["activity"]["queued"] == 0


def test_unknown_endpoint(server):
    _, client, _ = server
    login(client)
    client.get("/api/nope", expect=404)


def test_worker_scans_at_start_even_soon_after_boot(env, monkeypatch):
    """The first periodic scan must not wait for 'interval' after boot."""
    import time as _time

    from reifs import service

    write(env.root / "boot.txt", "x")
    env.cfg.scan.interval_minutes = 60
    monkeypatch.setattr(service.time, "monotonic", lambda: 5.0)  # booted 5 s ago
    worker = Worker(env.cfg)
    worker.start()
    try:
        deadline = _time.time() + 15
        while _time.time() < deadline:
            if env.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]:
                break
            _time.sleep(0.2)
        assert env.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    finally:
        worker.close()
