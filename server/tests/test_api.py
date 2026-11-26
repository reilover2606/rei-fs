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
    # Folders are searched too, with the number of files below them.
    out = client.get("/api/find?q=doc")
    assert out["dirs"] == [{"path": "/docs", "name": "docs", "files": 2}]
    assert out["dirs_total"] == 1 and out["files"] == []
    assert client.get("/api/find?q=top")["dirs"] == []
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


def test_add_and_rename_a_node(server):
    env, client, _ = server
    login(client)
    drive = env.tmp / "usb-stick"
    drive.mkdir()
    write(drive / "photo.jpg", "pixels", mtime=1_000_000)
    out = client.post("/api/nodes", {"path": str(drive), "name": "usb"})
    assert out["name"] == "usb"
    from reifs import nodes as nodes_mod
    marker = nodes_mod.read_marker(drive)
    assert marker.name == "usb" and marker.node_id == out["id"]
    names = {n["name"]: n for n in client.get("/api/nodes")["nodes"]}
    assert names["usb"]["kind"] == "storage" and names["usb"]["online"]
    wait_idle(client)                       # the new node is scanned right away
    assert client.get("/api/file?path=/photo.jpg")["copies"][0]["node"] == "usb"

    # The name defaults to the folder's name.
    other = env.tmp / "Backup Disk"
    other.mkdir()
    assert client.post("/api/nodes", {"path": str(other)})["name"] == "Backup Disk"

    # Mistakes are explained, not crashes.
    err = lambda body: client.post("/api/nodes", body, expect=400)["error"]  # noqa: E731
    assert "absolute" in err({"path": "relative/dir", "name": "x"})
    assert "not a folder" in err({"path": str(env.tmp / "missing"), "name": "x"})
    assert "overlaps node 'usb'" in err({"path": str(drive), "name": "other"})  # same drive
    d2 = env.tmp / "d2"
    d2.mkdir()
    assert "already exists" in err({"path": str(d2), "name": "usb"})
    assert "needs a name" in err({"path": str(d2), "name": "   "})
    assert "without slashes" in err({"path": str(d2), "name": "a/b"})
    (drive / "sub").mkdir()
    assert "overlaps node 'usb'" in err({"path": str(drive / "sub"), "name": "nested"})
    assert not (d2 / ".rei-fs").exists()      # failed attempts leave no marker behind

    # Rename: database and the drive's own ID file.
    client.post("/api/nodes/usb", {"name": "stick"})
    assert nodes_mod.read_marker(drive).name == "stick"
    assert "stick" in {n["name"] for n in client.get("/api/nodes")["nodes"]}
    assert "already exists" in client.post("/api/nodes/stick", {"name": "ssd"}, expect=400)["error"]
    assert "named after the server" in client.post("/api/nodes/srv", {"name": "x"},
                                                   expect=400)["error"]


def test_status_shows_database_lock_and_scan_progress(server, monkeypatch):
    import sqlite3

    from reifs import db, hashing, scanner

    env, client, srv = server
    srv.api.busy_timeout = 1
    login(client)
    st = client.get("/api/status")["database"]
    assert st["lock"] == "free" and st["holders"] == [] and st["waiting"] == []

    # A writer inside this server: reported as the holder, with its label.
    conn = db.open_existing(env.cfg.db_path)
    with db.transaction(conn, "test: saving things"):
        st = client.get("/api/status")["database"]
        assert st["lock"] == "held"
        assert [h["who"] for h in st["holders"]] == ["test: saving things"]
    conn.close()

    # Locked by something this server does not know about (another process).
    outside = sqlite3.connect(env.cfg.db_path, isolation_level=None)
    outside.execute("BEGIN IMMEDIATE")
    try:
        st = client.get("/api/status")["database"]
        assert st["lock"] == "locked" and st["holders"] == []
        # ...and a write from the client fails fast, with an explanation.
        err = client.post("/api/nodes/ssd", {"priority": 3}, expect=503)["error"]
        assert "busy" in err
        assert client.get("/api/status")["database"]["busy_errors"] >= 1
    finally:
        outside.execute("ROLLBACK")
        outside.close()

    # A long hash: the scan says which file it is reading, and the database
    # lock is free meanwhile.
    write(env.root / "huge.bin", "x" * (17 << 20), mtime=2_000_000)
    real = hashing.hash_file
    monkeypatch.setattr(scanner, "hash_file",
                        lambda p: (time.sleep(3), real(p))[1] if str(p).endswith("huge.bin") else real(p))
    client.post("/api/scan")
    seen = None
    deadline = time.time() + 10
    while time.time() < deadline:
        status = client.get("/api/status")
        detail = status["activity"]["detail"]
        if "huge.bin" in detail:
            seen = (detail, status["database"]["lock"])
            break
        time.sleep(0.1)
    assert seen is not None, "the scan never reported the big file"
    assert "srv:" in seen[0] and "hashing /huge.bin (17 MiB)" in seen[0]
    assert seen[1] == "free"
    wait_idle(client)


def test_virtual_move_through_the_api(server):
    env, client, _ = server
    login(client)
    write(env.root / "other" / "a.txt", "different", mtime=1_000_000)
    client.post("/api/scan")
    wait_idle(client)
    probe = client.post("/api/vmove", {"paths": ["/docs/a.txt"], "to": "/other", "dry_run": True})
    assert probe["dry_run"] and probe["conflicts"] == ["/other/a.txt"] and probe["files"] == 1
    assert (env.root / "docs" / "a.txt").exists()  # a dry run changes nothing
    out = client.post("/api/vmove", {"paths": ["/docs/a.txt"], "to": "/other",
                                     "on_conflict": "preserve"})
    assert out["renamed"] == [{"wanted": "a.txt", "given": "a (2).txt"}] and out["job_id"]
    wait_idle(client)
    assert (env.root / "other" / "a (2).txt").read_text() == "alpha"
    assert (env.root / "other" / "a.txt").read_text() == "different"
    client.post("/api/vmove", {"paths": ["/docs"], "to": "/docs/x"}, expect=400)
    client.post("/api/vmove", {"paths": ["/docs"], "to": "/z", "on_conflict": "bogus"}, expect=400)


def test_update_priority_through_the_api(server):
    env, client, _ = server
    login(client)
    assert client.get("/api/priority?path=/docs")["priority"] == 2
    out = client.post("/api/priority", {"path": "/docs", "priority": "archival"})
    assert (out["priority"], out["inherited"]) == (3, False)
    assert client.get("/api/priority?path=/docs/deep/er")["set_on"] == "/docs"
    tree = client.get("/api/tree?path=/")
    assert tree["priority"] == 2
    assert [(e["name"], e["priority"], e["set_here"]) for e in tree["entries"] if e["dir"]] == \
        [("docs", 3, True)]
    assert client.post("/api/priority", {"path": "/docs/a.txt", "priority": 1})["priority"] == 1  # a file too
    client.post("/api/priority", {"path": "/docs", "priority": 9}, expect=400)
    assert client.post("/api/priority", {"path": "/docs", "priority": None})["priority"] == 2
    client.post("/api/scan", {"due": True})        # an automatic-style scan is accepted
    wait_idle(client)


def test_long_lists_are_bounded(server):
    env, client, _ = server
    login(client)
    me = env.conn.execute("SELECT id FROM nodes WHERE name = 'srv'").fetchone()[0]
    fids = [r[0] for r in env.conn.execute("SELECT id FROM files")]
    with env.conn:
        for fid in fids:
            env.conn.execute("INSERT INTO decisions(kind, file_id, node_id, data, created)"
                             " VALUES ('missing', ?, ?, '{}', '2026-01-01T00:00:00+00:00')",
                             (fid, me))
        env.conn.execute("UPDATE copies SET marked_delete = 1")
    out = client.get("/api/decisions?limit=2")
    assert len(out["decisions"]) == 2 and out["total"] == len(fids)
    assert len(client.get("/api/decisions")["decisions"]) == len(fids)       # no limit: all
    marked = client.get("/api/marked?limit=1")
    assert len(marked["marked"]) == 1 and marked["total"] == len(fids) and marked["total_size"] > 0


def test_tree_says_what_this_servers_node_holds(server):
    env, client, _ = server
    login(client)
    out = client.get("/api/tree?path=/")
    assert out["this_node"] == "srv"
    assert {e["name"]: e["here"] for e in out["entries"] if e["dir"]} == {"docs": 2}
    # A file only the ssd node holds is not "here".
    write(env.ssd / "only-ssd" / "x.txt", "x", mtime=1_000_000)
    client.post("/api/scan")
    wait_idle(client)
    out = client.get("/api/tree?path=/")
    assert {e["name"]: e["here"] for e in out["entries"] if e["dir"]} == {"docs": 2, "only-ssd": 0}
    top = client.get("/api/tree?path=/docs")
    assert all(e["here"] for e in top["entries"] if not e["dir"])


def test_a_null_body_means_no_body(server):
    env, client, srv = server
    login(client)
    import urllib.request
    req = urllib.request.Request(client.base + "/api/jobs/run", data=b"null", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {client.token}")
    with urllib.request.urlopen(req, context=client.ctx, timeout=10) as r:
        assert r.status == 200
    client.post("/api/jobs/run", [1], expect=400)           # a list is still refused


def test_a_request_that_cannot_open_the_database_is_answered_and_not_leaked(server, monkeypatch):
    env, client, srv = server
    login(client)
    from reifs import db as dbmod
    real = dbmod.open_existing

    def locked(*a, **k):
        raise dbmod.DatabaseLocked("held by someone else")

    monkeypatch.setattr(dbmod, "open_existing", locked)
    out = client.get("/api/nodes", expect=503)
    assert "busy" in out["error"]
    assert srv.api._requests == {}                      # nothing is left "being served"
    monkeypatch.setattr(dbmod, "open_existing", real)
    assert client.get("/api/status")["connections"]["handshake_failed"] >= 0


def test_routine_connection_endings_are_not_logged_or_counted(caplog):
    import logging
    from reifs import api as apimod
    before = dict(apimod.CONNECTION_STATS)
    with caplog.at_level(logging.INFO, logger="reifs.api"):
        apimod._connection_problem("dropped", "127.0.0.1", BrokenPipeError("Broken pipe"))
        apimod._connection_problem("handshake_failed", "127.0.0.1", ConnectionResetError())
    assert apimod.CONNECTION_STATS == before and caplog.records == []
    with caplog.at_level(logging.INFO, logger="reifs.api"):
        apimod._last_summary[0] = float("-inf")
        apimod._connection_problem("dropped", "10.0.0.5", OSError("something odd"))
        apimod._connection_problem("dropped", "10.0.0.5", OSError("again"))
    assert apimod.CONNECTION_STATS["dropped"] == before["dropped"] + 2
    assert len(caplog.records) == 1      # at most one summary a minute


def test_network_figures_and_the_node_tree_data(server):
    env, client, _ = server
    login(client)
    net = client.get("/api/network")
    assert net["goal"] == 2 and net["files"] == 3 and sum(net["hist"]) == 3
    assert {n["name"] for n in net["nodes"]} == {"srv", "ssd"}
    nodes_ = client.get("/api/nodes")["nodes"]
    assert all("owner_id" in n and n["owner_online"] is True for n in nodes_)     # for the tree


def test_times_are_text_over_the_api(server):
    env, client, _ = server
    login(client)
    ssd = {n["name"]: n for n in client.get("/api/nodes")["nodes"]}["ssd"]
    assert ssd["last_scan"].endswith("+00:00") and ssd["last_scan"][:2] == "20"
    assert env.conn.execute("SELECT typeof(last_scan) FROM nodes WHERE name = 'ssd'").fetchone()[0] == "integer"


@pytest.mark.skipif(sys.platform == "win32", reason="the squashfs tools are Linux only")
def test_pack_search_deep_and_unpack_over_the_api(server):
    from reifs import archives
    if archives.tools() is None:
        pytest.skip("needs squashfs-tools")
    env, client, _ = server
    login(client)

    def wait(pred, what):
        end = time.time() + 30
        while time.time() < end:
            if pred():
                return
            time.sleep(0.2)
        raise AssertionError(f"timed out: {what}")

    out = client.post("/api/archives/pack", {"vpath": "/docs"})
    assert out["queued"]
    wait(lambda: any(e.get("packed") for e in client.get("/api/tree?path=/")["entries"]), "packed")
    entry = [e for e in client.get("/api/tree?path=/")["entries"] if e["name"] == "docs"][0]
    assert entry["packed"] and entry["archive"] == "/docs.sqsh" and entry["files"] == 2
    inside = client.get("/api/tree?path=/docs")                          # can be looked into
    assert inside["inside_archive"] == "/docs"
    assert [(e["name"], e["in_archive"]) for e in inside["entries"]][0] == ("a.txt", True)
    assert client.get("/api/find?q=a.txt")["total"] == 0                  # opaque to a search
    deep = client.get("/api/find?q=a.txt&deep=1")
    assert deep["total"] == 0 and [m["vpath"] for m in deep["members"]] == ["/docs/a.txt"]
    client.post("/api/archives/pack", {"vpath": "/docs"}, expect=400)       # already packed
    client.post("/api/archives/unpack", {"vpath": "/docs"})
    wait(lambda: not any(e.get("packed") for e in client.get("/api/tree?path=/")["entries"]),
         "unpacked")
    assert client.get("/api/find?q=a.txt")["total"] == 1


def test_holders_say_which_nodes_have_the_selected_files(server):
    env, client, _ = server
    login(client)
    out = client.post("/api/holders", {"paths": ["/docs", "/top.txt"]})
    have = {n["name"]: n["have"] for n in out["nodes"]}
    assert out["total"] == 3 and have["ssd"] == 0 and max(have.values()) == 3
    client.post("/api/holders", {"paths": ["/nope"]}, expect=400)


def test_node_flags_and_tolerance_over_the_api(server):
    env, client, _ = server
    login(client)
    client.post("/api/nodes/ssd", {"mirrored": True, "backup_first": True})
    ssd = {n["name"]: n for n in client.get("/api/nodes")["nodes"]}["ssd"]
    assert ssd["mirrored"] and ssd["backup_first"]
    out = client.post("/api/tolerance", {"path": "/docs", "tolerant": True})
    assert out["tolerant"] and not out["inherited"]
    entry = [e for e in client.get("/api/tree?path=/")["entries"] if e["name"] == "docs"][0]
    assert entry["tolerant"]
    assert client.get("/api/file?path=/docs/a.txt")["tolerant"]


def test_an_app_token_reaches_the_app_endpoints_and_nothing_else(server):
    import base64
    env, client, _ = server
    login(client)
    out = client.post("/api/apps", {"name": "demo", "mode": "storage", "node": "ssd"})
    token = out["token"]
    assert out["dir"] == "/apps/demo" and any(a["name"] == "demo" for a in client.get("/api/apps")["apps"])
    client.token = token
    info = client.get("/api/app/info")
    assert info["name"] == "demo" and info["mode"] == "storage"
    data = base64.b64encode(b"hi").decode()
    assert client.post("/api/app/write", {"path": "a.txt", "data": data})["size"] == 2
    got = client.get("/api/app/read?path=a.txt")
    assert base64.b64decode(got["data"]) == b"hi" and got["eof"]
    assert [e["name"] for e in client.get("/api/app/list")["entries"]] == ["a.txt"]
    client.post("/api/app/write", {"path": "../x", "data": data}, expect=400)
    client.get("/api/nodes", expect=401)                     # not an admin token
    client.post("/api/scan", {}, expect=401)
    client.token = "reifsapp_nonsense"
    client.get("/api/app/info", expect=401)
    login(client)
    client.get("/api/app/info", expect=401)                  # and a login is not an app token
    client.post("/api/apps/remove", {"name": "demo"})
    client.token = token
    client.get("/api/app/info", expect=401)                  # the token died with the registration


def test_opaque_directories_and_reset_over_the_api(server):
    import time as _t
    env, client, _ = server
    login(client)
    out = client.post("/api/opaque", {"path": "/docs", "opaque": True})
    assert out["opaque"] and not out["inherited"]
    end = _t.time() + 20
    while _t.time() < end:                       # the worker forgets what was indexed below it
        if env.conn.execute("SELECT COUNT(*) FROM files f JOIN dirs d ON d.id = f.dir_id"
                            " WHERE d.path = '/docs'").fetchone()[0] == 0:
            break
        _t.sleep(0.2)
    entries = {e["name"]: e for e in client.get("/api/tree?path=/")["entries"]}
    assert entries["docs"]["opaque"] and entries["docs"]["dir"]
    assert client.get("/api/find?q=a.txt")["total"] == 0
    found = client.get("/api/find?q=a.txt&deep=1")
    assert [n["vpath"] for n in found["opaque_names"]] == ["/docs/a.txt"]
    client.post("/api/opaque", {"path": "/top.txt", "opaque": True}, expect=400)       # a file
    # reset: a scan job that forgets the node first
    jid = client.post("/api/nodes/srv/reset", {})["job_id"]
    end = _t.time() + 30
    while _t.time() < end:
        if env.conn.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()[0] == "done":
            break
        _t.sleep(0.2)
    assert env.conn.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()[0] == "done"
    assert env.conn.execute("SELECT COUNT(*) FROM files f JOIN dirs d ON d.id = f.dir_id"
                            " WHERE d.path = '/docs'").fetchone()[0] == 0          # still opaque
    assert client.get("/api/file?path=/top.txt")["size"] == 3                       # indexed again
