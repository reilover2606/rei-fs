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


def test_an_opaque_directory_is_one_row_and_its_files_are_counted_on_the_side(env):
    from reifs import collections_
    opaque.set_opaque(env.conn, "/build", True)
    big_tree(env)
    assert vpaths(env) == ["/build", "/keep.txt"]                # no row for any file inside
    fid = index.file_id(env.conn, "/build")
    assert collections_.file_count(env.conn, fid) == 20
    c = env.conn.execute("SELECT size, hash FROM copies WHERE file_id = ?", (fid,)).fetchone()
    assert c["size"] == sum(len(str(i)) for i in range(20)) and c["hash"].startswith("blake2b:")
    assert queries.lookup(env.conn, "/build").files == 20
    # a change inside is a new version of the one row, and the count follows
    write(env.root / "build" / "new.o", "n")
    env.scan()
    assert vpaths(env) == ["/build", "/keep.txt"]
    assert collections_.file_count(env.conn, fid) == 21
    assert env.conn.execute("SELECT hash FROM copies WHERE file_id = ?", (fid,)).fetchone()[0] != c["hash"]
    # unchanged: nothing is written
    before = env.conn.execute("SELECT MAX(seq) FROM changes").fetchone()[0]
    env.scan()
    assert env.conn.execute("SELECT MAX(seq) FROM changes").fetchone()[0] == before


def test_what_was_indexed_loose_is_forgotten_in_pieces_and_the_directory_becomes_one_row(env, monkeypatch):
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
    env.scan()                                      # the scan then makes the one row
    assert vpaths(env) == ["/build", "/keep.txt"]
    assert env.decisions() == []


def test_an_opaque_directory_shows_in_the_tree_as_a_folder_with_its_file_count(env):
    opaque.set_opaque(env.conn, "/build", True)
    big_tree(env)
    entries = queries.list_dir(env.conn, "/")
    assert [(e.name, e.is_dir, e.opaque, e.files, e.here) for e in entries] == [
        ("build", True, True, 20, 20), ("keep.txt", False, False, 1, 1)]
    assert queries.list_dir(env.conn, "/build") == []
    assert queries.list_dir(env.conn, "/build/d0") is None


def two(env):
    _, ssd = env.add_node("ssd")
    opaque.set_opaque(env.conn, "/build", True)
    big_tree(env)
    return ssd


def copy_job(env, op="copy", vpath="/build", dst="ssd"):
    node = env.conn.execute("SELECT id FROM nodes WHERE name = ?", (dst,)).fetchone()[0]
    fid = index.file_id(env.conn, vpath)
    allow = None
    if op == "overwrite":
        allow = env.conn.execute("SELECT hash FROM copies WHERE file_id = ? AND node_id = ?",
                                 (fid, node)).fetchone()[0]
    with db.transaction(env.conn):
        jid = jobs.create_tool_job(env.conn, "copy", "c", [])
        env.conn.execute("INSERT INTO job_items(job_id, op, vpath, dst_node, size, allow_hash, updated)"
                         " VALUES (?, ?, ?, ?, 0, ?, ?)", (jid, op, vpath, node, allow, db.now()))
    runner.run_jobs(env.conn, env.cfg)
    return env.conn.execute("SELECT state, message FROM job_items WHERE job_id = ?", (jid,)).fetchone()


def test_copying_an_opaque_directory_copies_everything_in_it(env):
    ssd = two(env)
    item = copy_job(env)
    assert item["state"] == "done", item["message"]
    assert sorted(p.name for p in (ssd / "build" / "d1").iterdir()) == [f"obj{i}.o" for i in (1, 13, 17, 5, 9)]
    fid = index.file_id(env.conn, "/build")
    assert env.conn.execute("SELECT COUNT(*) FROM copies WHERE file_id = ?", (fid,)).fetchone()[0] == 2
    hashes = {r[0] for r in env.conn.execute("SELECT hash FROM copies WHERE file_id = ?", (fid,))}
    assert len(hashes) == 1                                  # the same version on both nodes
    from reifs import collections_
    assert env.conn.execute("SELECT files FROM collection_stats WHERE file_id = ? AND node_id = (SELECT id FROM nodes WHERE name = 'ssd')", (fid,)).fetchone()[0] == 20
    assert env.decisions() == []


def test_an_older_copy_of_an_opaque_directory_is_made_equal(env):
    ssd = two(env)
    copy_job(env)
    write(ssd / "build" / "stale.txt", "only on the old copy", mtime=500_000)
    write(env.root / "build" / "d0" / "added.o", "new")                 # the source is newer
    (env.root / "build" / "d0" / "obj0.o").unlink()
    env.scan()                                       # both nodes: the old copy is the older one
    item = copy_job(env, "overwrite")
    assert item["state"] == "done", item["message"]
    assert (ssd / "build" / "d0" / "added.o").exists()
    assert not (ssd / "build" / "d0" / "obj0.o").exists()
    assert not (ssd / "build" / "stale.txt").exists()        # what the new version lacks goes
    env.scan()
    fid = index.file_id(env.conn, "/build")
    assert len({r[0] for r in env.conn.execute("SELECT hash FROM copies WHERE file_id = ?", (fid,))}) == 1


def test_renaming_an_opaque_directory_moves_the_directory_and_its_setting(env):
    from reifs import vmove
    opaque.set_opaque(env.conn, "/build", True)
    big_tree(env)
    plan = vmove.plan_move(env.conn, ["/build"], "/", name="out")
    vmove.apply_plan(env.conn, plan, "rename")
    runner.run_jobs(env.conn, env.cfg)
    assert (env.root / "out" / "d0").is_dir() and not (env.root / "build").exists()
    assert opaque.roots(env.conn) == ["/out"]
    env.scan()
    assert vpaths(env) == ["/keep.txt", "/out"]


def test_the_directory_fingerprint_sees_names_sizes_and_times_and_no_content_is_read(env, tmp_path):
    from reifs import collections_
    d = tmp_path / "t"
    write(d / "a" / "x.txt", "1", mtime=1_000_000)
    base = collections_.fingerprint(d)
    assert (base.files, base.bytes) == (1, 1)
    assert collections_.fingerprint(d).hash == base.hash                  # stable
    write(d / "a" / "x.txt", "2", mtime=1_000_000)                        # same size and time: same
    assert collections_.fingerprint(d).hash == base.hash
    write(d / "a" / "x.txt", "22", mtime=1_000_000)
    assert collections_.fingerprint(d).hash != base.hash                  # size
    write(d / "a" / "x.txt", "2", mtime=1_000_100)
    assert collections_.fingerprint(d).hash != base.hash                  # time
    (d / "a" / "x.txt").rename(d / "a" / "y.txt")
    write(d / "a" / "y.txt", "2", mtime=1_000_100)
    assert collections_.fingerprint(d).hash != base.hash                  # name
    (d / "empty").mkdir()
    assert collections_.fingerprint(d).hash != collections_.fingerprint(d / "a").hash


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


def test_making_a_directory_indexed_again_drops_its_row_and_scans_the_files(env):
    opaque.set_opaque(env.conn, "/build", True)
    big_tree(env)
    assert vpaths(env) == ["/build", "/keep.txt"]
    opaque.set_opaque(env.conn, "/build", False)           # allowed: it is the opaque directory itself
    assert opaque.indexed_below(env.conn)                  # the row of the directory is to go
    opaque.forget_indexed(env.conn)
    assert vpaths(env) == ["/keep.txt"]
    env.scan()
    assert len(vpaths(env)) == 21 and "/build" not in vpaths(env)
    assert env.decisions() == []
