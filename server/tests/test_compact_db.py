"""The smaller database: pointer log entries, 32-byte hashes, the number for case-folded paths."""

import json
import sqlite3
import sys

import pytest

from reifs import db, index, queries, scanner, sync

from conftest import write


def test_hash_encoding_round_trips():
    h = "blake2b:" + "ab" * 32
    assert db.hash_to_blob(h) == bytes.fromhex("ab" * 32) and db.blob_to_hash(db.hash_to_blob(h)) == h
    assert db.blob_to_hash(db.hash_to_blob("not-a-blake-hash")) == "not-a-blake-hash"
    assert db.hash_to_blob(None) is None and db.blob_to_hash(None) is None
    assert db.key_hash("/A/B.TXT") == db.key_hash("/a/b.txt") != db.key_hash("/a/c.txt")


def test_the_hash_is_read_as_text_and_stored_as_32_bytes(env):
    write(env.root / "a.txt", "alpha", mtime=1_000_000)
    env.scan()
    row = env.conn.execute("SELECT hash, hash_b FROM copies").fetchone()
    assert row["hash"].startswith("blake2b:") and len(row["hash"]) == 72
    assert isinstance(row["hash_b"], bytes) and len(row["hash_b"]) == 32
    assert env.conn.execute("SELECT 1 FROM copies WHERE hash = ?", (row["hash"],)).fetchone()


def entries(env, **where):
    return env.conn.execute("SELECT * FROM changes WHERE kind = 'copy' ORDER BY seq").fetchall()


def test_the_log_holds_one_pointer_per_copy_and_sends_the_current_state(env):
    write(env.root / "a.txt", "v1", mtime=1_000_000)
    env.scan()
    write(env.root / "a.txt", "version two", mtime=2_000_000)
    env.scan()
    write(env.root / "a.txt", "third", mtime=3_000_000)
    env.scan()
    rows = entries(env)
    assert len(rows) == 1 and rows[0]["data"] == "" and rows[0]["ref_file"] is not None
    sent = sync.export_changes(env.conn, 0)["changes"]
    copy = next(c for c in sent if c["kind"] == "copy")["data"]
    assert copy["op"] == "upsert" and copy["vpath"] == "/a.txt" and copy["size"] == 5
    assert copy["hash"] == env.conn.execute("SELECT hash FROM copies").fetchone()[0]
    # A copy that has since gone: the pointer is skipped and the removal is sent with its path.
    (env.root / "a.txt").unlink()
    env.scan()
    env.conn.execute("DELETE FROM copies")           # forgotten (as after the user's decision)
    index.log_change(env.conn, "n", "/a.txt", "remove")
    sent = sync.export_changes(env.conn, 0)["changes"]
    ops = [c["data"].get("op") for c in sent if c["kind"] == "copy"]
    assert ops == ["remove"]


def test_forgetting_a_copy_replaces_its_pointer_with_a_removal(env):
    write(env.root / "a.txt", "x", mtime=1_000_000)
    env.scan()
    fid = index.file_id(env.conn, "/a.txt")
    node = env.conn.execute("SELECT node_id FROM copies").fetchone()[0]
    with db.transaction(env.conn):
        index.forget_copy(env.conn, fid, node)
    rows = entries(env)
    assert [r["ref_file"] for r in rows] == [None]
    assert json.loads(rows[0]["data"])["op"] == "remove"


def test_a_peers_entries_are_forwarded_as_pointers_to_what_we_hold(env):
    d = {"node_id": "peer-node", "vpath": "/p/x.txt", "op": "upsert", "size": 3,
         "mtime_ns": 5, "hash": "blake2b:" + "01" * 32}
    env.conn.execute("INSERT INTO servers(id, name) VALUES ('peer', 'peer')")
    sync.apply_changes(env.conn, [
        {"origin_id": "peer", "origin_seq": 1, "kind": "node", "ts": "t",
         "data": {"id": "peer-node", "name": "pn", "kind": "storage", "owner_id": "peer",
                  "owner_name": "peer", "owner_since": "", "case_sensitive": 1,
                  "restricted_names": 0, "priority": 0, "replication_target": 1, "online": 1}},
        {"origin_id": "peer", "origin_seq": 2, "kind": "copy", "data": d, "ts": "t"}])
    row = env.conn.execute("SELECT * FROM changes WHERE origin_id = 'peer' AND kind = 'copy'").fetchone()
    assert row["data"] == "" and row["ref_node"] == "peer-node"
    sent = [c for c in sync.export_changes(env.conn, 0)["changes"] if c["kind"] == "copy"]
    assert sent[0]["data"] == d | {"op": "upsert"} and sent[0]["origin_id"] == "peer"


@pytest.mark.skipif(sys.platform in ("win32", "darwin"),
                    reason="the file system is not case-sensitive: the two files cannot coexist")
def test_case_collisions_are_still_found(env):
    write(env.root / "Photos" / "A.txt", "1", mtime=1_000_000)
    write(env.root / "photos" / "a.txt", "2", mtime=1_000_000)
    env.scan()
    flagged = {r["vpath"] for r in env.conn.execute("SELECT vpath FROM files WHERE issues LIKE '%case%'")}
    assert flagged == {"/Photos/A.txt", "/photos/a.txt"}


def test_search_finds_names_whose_case_folding_is_not_ascii(env):
    write(env.root / "Kelvin.txt", "kelvin sign", mtime=1_000_000)      # KELVIN SIGN folds to k
    write(env.root / "Report.txt", "r", mtime=1_000_000)
    env.scan()
    names = lambda pat: sorted(queries.file_info(env.conn, i).vpath for i in queries.search(env.conn, pat)[0])
    assert names("elvin") == ["/Kelvin.txt"]
    assert names("kelvin") == ["/Kelvin.txt"]       # the Kelvin sign is a k
    assert names("REPORT") == ["/Report.txt"]


def test_the_upgrade_converts_an_old_database(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "SCHEMA_VERSION", 13)          # the layout the last release used
    db.create(path).close()
    monkeypatch.undo()
    c = sqlite3.connect(path)
    h1, h2 = "blake2b:" + "11" * 32, "blake2b:" + "22" * 32
    with c:
        c.execute("INSERT INTO servers(id, name, is_self) VALUES ('s', 'me', 1)")
        c.execute("INSERT INTO nodes(id, name, kind, owner_id, online, case_sensitive,"
                  " restricted_names, owner_since) VALUES ('n', 'n', 'storage', 's', 1, 1, 0, '')")
        for fid, vp in ((1, "/Docs/A.txt"), (2, "/docs/b.txt")):
            c.execute("INSERT INTO files(id, vpath, vkey, issues, created) VALUES (?, ?, ?, '', 't')",
                      (fid, vp, vp.casefold()))
            c.execute("INSERT INTO copies(file_id, node_id, size, mtime_ns, hash, first_seen,"
                      " first_scan, hashed_at) VALUES (?, 'n', 5, 7, ?, 't', 0, 't')",
                      (fid, h1 if fid == 1 else h2))

        def log(seq, data):
            c.execute("INSERT INTO changes(origin_id, origin_seq, kind, data, ts)"
                      " VALUES ('s', ?, 'copy', ?, 't')", (seq, json.dumps(data)))
        base = {"node_id": "n", "size": 5, "mtime_ns": 7}
        log(1, base | {"vpath": "/Docs/A.txt", "op": "upsert", "hash": h1})
        log(2, base | {"vpath": "/Docs/A.txt", "op": "upsert", "hash": h1})   # supersedes #1
        log(3, base | {"vpath": "/docs/b.txt", "op": "upsert", "hash": h2})
        log(4, base | {"vpath": "/gone.txt", "op": "upsert", "hash": h2})     # no such file now
        log(5, {"node_id": "n", "vpath": "/old.txt", "op": "remove", "size": None,
                "mtime_ns": None, "hash": None})
        c.execute("PRAGMA user_version = 13")
    c.close()
    conn = db.open_existing(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert [r["hash"] for r in conn.execute("SELECT hash FROM copies ORDER BY file_id")] == [h1, h2]
    assert "vkey" not in [r[1] for r in conn.execute("PRAGMA table_info(files)")]
    assert conn.execute("SELECT vhash FROM files WHERE id = 1").fetchone()[0] == db.key_hash("/Docs/A.txt")
    log = conn.execute("SELECT seq, origin_seq, ref_file, data FROM changes ORDER BY seq").fetchall()
    assert [(r["origin_seq"], r["ref_file"]) for r in log] == [(2, 1), (3, 2), (5, None)]
    assert [r["data"] for r in log][:2] == ["", ""]
    sent = sync.export_changes(conn, 0)["changes"]
    assert [(c["data"]["op"], c["data"]["vpath"]) for c in sent] == [
        ("upsert", "/Docs/A.txt"), ("upsert", "/docs/b.txt"), ("remove", "/old.txt")]
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_vacuum_reports_sizes(tmp_path):
    path = tmp_path / "v.db"
    conn = db.create(path)
    with conn:
        conn.execute("CREATE TABLE junk(x TEXT)")
        conn.executemany("INSERT INTO junk VALUES (?)", [("x" * 500,)] * 20000)
    with conn:
        conn.execute("DELETE FROM junk")
    assert db.free_space_ratio(conn) > 0.3
    before, after = db.vacuum(conn, path)
    assert after < before and db.free_space_ratio(conn) < 0.05
    assert db.table_sizes(conn) is None or db.table_sizes(conn)[0][1] > 0
