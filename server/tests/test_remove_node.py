import pytest

from reifs import db, index, nodes, queries, sync

from conftest import write


def test_removing_a_node_forgets_its_copies_and_leaves_its_files(env):
    _, ssd = env.add_node("ssd")
    write(env.root / "both.txt", "b", mtime=1_000_000)
    write(ssd / "both.txt", "b", mtime=1_000_000)
    write(ssd / "only-ssd.txt", "s", mtime=1_000_000)
    env.scan()
    out = nodes.remove_node(env.conn, "ssd")
    assert out["copies_forgotten"] == 2
    assert env.conn.execute("SELECT COUNT(*) FROM nodes WHERE name = 'ssd'").fetchone()[0] == 0
    assert index.file_id(env.conn, "/only-ssd.txt") is None            # nobody holds it now
    assert queries.lookup(env.conn, "/both.txt").good_copies == 1       # the server's copy stays
    assert (ssd / "only-ssd.txt").exists()                              # the drive is untouched
    # the folder can be added again, and is the same node
    nodes.register_node(env.conn, ssd, "ssd")
    assert env.conn.execute("SELECT COUNT(*) FROM nodes WHERE name = 'ssd'").fetchone()[0] == 1


def test_the_servers_own_node_cannot_be_removed(env):
    with pytest.raises(nodes.NodeError, match="own node"):
        nodes.remove_node(env.conn, "srv")


def test_other_servers_learn_of_the_removal(env, tmp_path):
    env.add_node("ssd")
    write(env.root / "a.txt", "a", mtime=1_000_000)
    env.scan()
    other = db.create(tmp_path / "o.db")
    other.execute("INSERT INTO servers(id, name, is_self) VALUES ('me', 'me', 1)")
    other.execute("INSERT INTO meta(key, value) VALUES ('server_id', 'me')")
    other.execute("INSERT INTO servers(id, name) VALUES (?, 'origin')", (env.server_id,))

    def carry():
        new = [c for c in sync.export_changes(env.conn, 0)["changes"]]
        sync.apply_changes(other, [dict(c, ts=db.to_iso(db.now())) for c in new])

    carry()
    assert other.execute("SELECT COUNT(*) FROM nodes WHERE name = 'ssd'").fetchone()[0] == 1
    nodes.remove_node(env.conn, "ssd")
    carry()
    assert other.execute("SELECT COUNT(*) FROM nodes WHERE name = 'ssd'").fetchone()[0] == 0


def old_server_with_a_node(env):
    """A server that was reset: its old identity is gone, its node is still in the index."""
    env.conn.execute("INSERT INTO servers(id, name, paired, online) VALUES ('old', 'box', 1, 0)")
    env.conn.execute("INSERT INTO nodes(id, name, kind, owner_id, online, case_sensitive,"
                     " restricted_names, owner_since) VALUES ('old', 'box', 'server', 'old', 0, 1, 0, 0)")
    fid = index.ensure_file(env.conn, "/x.txt")
    env.conn.execute("INSERT INTO copies(file_id, node_id, size, mtime_ns, hash_b, first_seen,"
                     " first_scan, hashed_at) VALUES (?, 'old', 1, 1, hb(?), 0, 0, 0)",
                     (fid, "blake2b:" + "11" * 32))


def test_the_node_of_a_server_that_was_reset_can_be_removed_and_the_server_goes_with_it(env):
    old_server_with_a_node(env)
    out = nodes.remove_node(env.conn, "box")
    assert out["server_forgotten"] == "box"
    assert env.conn.execute("SELECT COUNT(*) FROM servers WHERE id = 'old'").fetchone()[0] == 0
    assert index.file_id(env.conn, "/x.txt") is None


def test_a_node_of_a_server_that_is_online_is_refused_unless_forced(env):
    old_server_with_a_node(env)
    env.conn.execute("UPDATE servers SET online = 1 WHERE id = 'old'")
    with pytest.raises(nodes.NodeError, match="online"):
        nodes.remove_node(env.conn, "box")
    assert nodes.remove_node(env.conn, "box", force=True)["name"] == "box"


def commits(conn):
    seen = []
    conn.set_trace_callback(lambda sql: seen.append(sql) if sql.strip().upper().startswith("COMMIT") else None)
    return seen


def test_removing_a_big_node_is_done_in_pieces(env, monkeypatch):
    monkeypatch.setattr(nodes, "TX_ROWS", 3)
    _, ssd = env.add_node("ssd")
    for i in range(10):
        write(ssd / "d" / f"f{i}.txt", str(i), mtime=1_000_000)
    env.scan()
    seen = commits(env.conn)
    out = nodes.remove_node(env.conn, "ssd")
    assert out["copies_forgotten"] == 10
    assert len(seen) >= 4                                   # 10 copies, 3 at a time, and the rest
    assert env.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert env.conn.execute("SELECT COUNT(*) FROM copies").fetchone()[0] == 0
