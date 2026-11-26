import shutil

from reifs import db, index, jobs, nodes, opaque, queries, runner, sync

from conftest import write


def vpaths(env):
    return sorted(r[0] for r in env.conn.execute("SELECT vpath FROM vfiles"))


# --- reset node -------------------------------------------------------------------------


def test_reset_forgets_everything_without_asking_about_what_is_gone(env):
    for i in range(30):
        write(env.root / "gone" / f"f{i}.txt", str(i), mtime=1_000_000)
    write(env.root / "kept.txt", "k", mtime=1_000_000)
    env.scan()
    shutil.rmtree(env.root / "gone")              # a big deletion
    n = nodes.reset_node(env.conn, "srv")
    assert n == 31
    assert env.conn.execute("SELECT COUNT(*) FROM copies").fetchone()[0] == 0
    assert env.decisions() == []                 # and none was raised about the deleted files
    env.scan()
    assert vpaths(env) == ["/kept.txt"]
    assert env.decisions() == []
    assert env.conn.execute("SELECT last_scan_id FROM nodes WHERE name = 'srv'").fetchone()[0]


def test_reset_is_announced_and_drops_the_copies_on_other_servers(env, tmp_path):
    write(env.root / "a.txt", "a", mtime=1_000_000)
    env.scan()
    other = db.create(tmp_path / "o.db")
    other.execute("INSERT INTO servers(id, name, is_self) VALUES ('me', 'me', 1)")
    other.execute("INSERT INTO meta(key, value) VALUES ('server_id', 'me')")
    other.execute("INSERT INTO servers(id, name) VALUES (?, 'origin')", (env.server_id,))

    def carry():
        sync.apply_changes(other, [dict(c, ts=db.to_iso(db.now()))
                                   for c in sync.export_changes(env.conn, 0)["changes"]])

    carry()
    assert other.execute("SELECT COUNT(*) FROM copies").fetchone()[0] == 1
    nodes.reset_node(env.conn, "srv")
    carry()
    assert other.execute("SELECT COUNT(*) FROM copies").fetchone()[0] == 0
    assert other.execute("SELECT COUNT(*) FROM nodes WHERE name = 'srv'").fetchone()[0] == 1   # the node stays


def test_a_reset_runs_as_a_scan_job(env):
    write(env.root / "a.txt", "a", mtime=1_000_000)
    env.scan()
    node = env.conn.execute("SELECT id FROM nodes WHERE name = 'srv'").fetchone()[0]
    (env.root / "a.txt").unlink()
    with db.transaction(env.conn):
        job = jobs.create_tool_job(env.conn, "scan", "reset", [("scan", "/", node, {"reset": True, "full": True})])
    runner.run_jobs(env.conn, env.cfg)
    assert env.conn.execute("SELECT status FROM jobs WHERE id = ?", (job,)).fetchone()[0] == "done"
    assert vpaths(env) == [] and env.decisions() == []


# --- opaque directories -----------------------------------------------------------------


def big_tree(env):
    for i in range(20):
        write(env.root / "build" / f"d{i % 4}" / f"obj{i}.o", str(i), mtime=1_000_000)
    write(env.root / "keep.txt", "k", mtime=1_000_000)
    env.scan()


def test_the_scanner_indexes_nothing_inside_an_opaque_directory(env):
    opaque.set_opaque(env.conn, "/build", True)
    big_tree(env)
    assert vpaths(env) == ["/keep.txt"]
    write(env.root / "build" / "new.o", "n")
    env.scan()
    assert vpaths(env) == ["/keep.txt"]


def test_what_was_indexed_is_forgotten_in_pieces_and_without_a_decision_each(env, monkeypatch):
    big_tree(env)
    assert len(vpaths(env)) == 21
    monkeypatch.setattr(opaque, "TX_ROWS", 6)
    opaque.set_opaque(env.conn, "/build", True)
    assert opaque.indexed_below(env.conn)
    seen = []
    env.conn.set_trace_callback(lambda s: seen.append(1) if s.strip().upper().startswith("COMMIT") else None)
    assert opaque.forget_indexed(env.conn) == 20
    assert len(seen) >= 4
    assert vpaths(env) == ["/keep.txt"] and not opaque.indexed_below(env.conn)
    assert env.conn.execute("SELECT COUNT(*) FROM dirs").fetchone()[0] == 1       # only the root
    assert env.decisions() == []
    env.scan()                                      # and the scan does not bring them back
    assert vpaths(env) == ["/keep.txt"]


def test_an_opaque_directory_shows_in_the_tree_as_one_entry(env):
    opaque.set_opaque(env.conn, "/build", True)
    big_tree(env)
    entries = queries.list_dir(env.conn, "/")
    assert [(e.name, e.is_dir, e.opaque) for e in entries] == [("build", True, True), ("keep.txt", False, False)]
    assert queries.list_dir(env.conn, "/build") == []
    assert queries.list_dir(env.conn, "/build/d0") is None


def test_the_setting_is_inherited_and_the_outermost_opaque_wins(env):
    opaque.set_opaque(env.conn, "/a", True)
    opaque.set_opaque(env.conn, "/a/b", False)          # inside an opaque directory: no effect
    assert opaque.get(env.conn, "/a/b/c.txt")["opaque"]
    opaque.set_opaque(env.conn, "/a", None)
    assert not opaque.get(env.conn, "/a/b/c.txt")["opaque"]


def test_a_deep_search_finds_names_inside_opaque_directories(env):
    opaque.set_opaque(env.conn, "/build", True)
    big_tree(env)
    write(env.root / "build" / "deep" / "er" / "target.jar", "j")
    hits, partial = opaque.search_names(env.conn, env.cfg, "target", 100)
    assert [(h["vpath"], h["opaque"], h["dir"]) for h in hits] == [("/build/deep/er/target.jar", "/build", False)]
    assert not partial
    names = sorted(h["vpath"] for h in opaque.search_names(env.conn, env.cfg, "d*", 100)[0])
    assert "/build/d0" in names and "/build/deep" in names
    assert len(opaque.search_names(env.conn, env.cfg, "*.o", 5)[0]) == 5          # the limit
    assert opaque.search_names(env.conn, env.cfg, None, 5) == ([], False)


def test_the_setting_is_replicated(env, tmp_path):
    opaque.set_opaque(env.conn, "/build", True)
    other = db.create(tmp_path / "o.db")
    other.execute("INSERT INTO servers(id, name, is_self) VALUES ('me', 'me', 1)")
    other.execute("INSERT INTO meta(key, value) VALUES ('server_id', 'me')")
    other.execute("INSERT INTO servers(id, name) VALUES (?, 'origin')", (env.server_id,))
    sync.apply_changes(other, [dict(c, ts=db.to_iso(db.now()))
                               for c in sync.export_changes(env.conn, 0)["changes"] if c["kind"] == "opaque"])
    assert opaque.roots(other) == ["/build"]
