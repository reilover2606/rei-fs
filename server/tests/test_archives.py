import shutil

import pytest

from conftest import write
from reifs import archives, db, index, queries, sync, vmove

pytestmark = pytest.mark.skipif(archives.tools() is None, reason="needs squashfs-tools")


def tree(env):
    for i, name in enumerate(("a.c", "sub/b.c", "sub/deep/c.txt", ".git/objects/x")):
        write(env.root / "src" / "foo" / name, f"content {i}" * 10, mtime=1_000_000)
    write(env.root / "src" / "keep.txt", "k", mtime=1_000_000)
    env.scan()


def vpaths(env):
    return sorted(r[0] for r in env.conn.execute("SELECT vpath FROM vfiles"))


def pack(env, **kw):
    return archives.pack(env.conn, env.cfg, "/src/foo", **kw)


def test_pack_makes_one_archive_and_the_directory_is_opaque(env):
    tree(env)
    res = pack(env)
    assert res["files"] == 4 and res["originals_kept"]
    # the archive is a file of the tree; the loose files are no longer tracked
    assert vpaths(env) == ["/src/foo.sqsh", "/src/keep.txt"]
    assert (env.root / "src" / "foo.sqsh").is_file()
    assert (env.root / "src" / "foo" / "a.c").exists()          # nothing was deleted
    entries = queries.list_dir(env.conn, "/src")
    assert [(e.name, e.is_dir, bool(e.packed), e.files) for e in entries] == [
        ("foo", True, True, 4), ("keep.txt", False, False, 1)]
    # it can still be looked into (read-only), from the member list
    inside = queries.list_dir(env.conn, "/src/foo")
    assert inside.inside == "/src/foo"
    assert [(e.name, e.is_dir, e.files, e.member_size) for e in inside] == [
        (".git", True, 1, None), ("sub", True, 2, None), ("a.c", False, 1, 90)]
    deeper = queries.list_dir(env.conn, "/src/foo/sub")
    assert [(e.name, e.is_dir) for e in deeper] == [("deep", True), ("b.c", False)]
    assert queries.list_dir(env.conn, "/src/foo/sub/deep")[0].name == "c.txt"
    assert queries.list_dir(env.conn, "/src/foo/nothing") is None
    # a scan does not take the loose files up again
    env.scan()
    assert vpaths(env) == ["/src/foo.sqsh", "/src/keep.txt"]


def test_search_sees_inside_only_when_deep(env):
    tree(env)
    pack(env)
    assert queries.search(env.conn, "c.txt")[0] == []
    found = archives.search_members(env.conn, "c.txt", 100)
    assert [(m["vpath"], m["archive"]) for m in found] == [("/src/foo/sub/deep/c.txt", "/src/foo")]
    assert [m["vpath"] for m in archives.search_members(env.conn, "*.c", 100)] == [
        "/src/foo/a.c", "/src/foo/sub/b.c"]
    assert archives.search_members(env.conn, "objects/x", 100)[0]["vpath"] == "/src/foo/.git/objects/x"


def test_removing_the_originals_only_removes_what_is_in_the_archive(env):
    tree(env)
    write(env.root / "src" / "foo" / "link-target", "t")
    res = pack(env, remove_originals=True)
    assert res["originals_removed"] == 5
    assert not (env.root / "src" / "foo").exists()
    assert (env.root / "src" / "keep.txt").exists()


def test_unpack_restores_the_files_and_marks_the_archive(env):
    tree(env)
    pack(env, remove_originals=True)
    res = archives.unpack(env.conn, env.cfg, "/src/foo")
    assert res["files"] == 4
    assert (env.root / "src" / "foo" / "sub" / "deep" / "c.txt").read_text() == "content 2" * 10
    assert vpaths(env) == ["/src/foo.sqsh", "/src/foo/a.c", "/src/foo/sub/b.c",
                           "/src/foo/sub/deep/c.txt", "/src/keep.txt"]   # .git is not tracked
    assert archives.get(env.conn, "/src/foo") is None
    marked = env.conn.execute("SELECT marked_delete FROM copies c JOIN vfiles f ON f.id = c.file_id"
                              " WHERE f.vpath = '/src/foo.sqsh'").fetchone()[0]
    assert marked == 1                                            # the user confirms the deletion
    assert (env.root / "src" / "foo.sqsh").exists()


def test_unpack_does_not_overwrite_changed_files(env):
    tree(env)
    pack(env)
    (env.root / "src" / "foo" / "a.c").write_text("I changed this")
    with pytest.raises(archives.ArchiveError, match="differ"):
        archives.unpack(env.conn, env.cfg, "/src/foo")
    assert (env.root / "src" / "foo" / "a.c").read_text() == "I changed this"
    assert archives.get(env.conn, "/src/foo") is not None


def test_drop_forgets_the_archive_and_marks_its_file(env):
    tree(env)
    pack(env)
    archives.drop(env.conn, "/src/foo")
    assert archives.get(env.conn, "/src/foo") is None
    assert archives.search_members(env.conn, "c.txt", 10) == []
    assert env.conn.execute("SELECT COUNT(*) FROM archive_members").fetchone()[0] == 0


def test_what_cannot_be_packed(env):
    tree(env)
    with pytest.raises(archives.ArchiveError, match="no files"):
        archives.pack(env.conn, env.cfg, "/nothing")
    pack(env)
    with pytest.raises(archives.ArchiveError, match="already packed"):
        pack(env)
    with pytest.raises(archives.ArchiveError, match="holds a packed|contains a packed"):
        archives.pack(env.conn, env.cfg, "/src")
    with pytest.raises(vmove.MoveError, match="packed"):
        vmove.plan_move(env.conn, ["/src/foo"], "/other")
    with pytest.raises(vmove.MoveError, match="archive"):
        vmove.plan_move(env.conn, ["/src/foo.sqsh"], "/other")


def test_a_directory_must_be_on_one_node(env):
    tree(env)
    _, ssd = env.add_node("ssd")
    write(ssd / "src" / "foo" / "extra.c", "e")                   # a file only the other node has
    env.scan()
    with pytest.raises(archives.ArchiveError, match="no single node"):
        pack(env)


def test_a_packed_directory_is_copied_as_its_archive(env):
    from reifs import jobs
    tree(env)
    pack(env)
    assert [v for _, v in jobs.select_files(env.conn, ["/src/foo"], None)] == ["/src/foo.sqsh"]


def test_peers_learn_of_the_archive(env, tmp_path):
    tree(env)
    pack(env)
    sent = [c for c in sync.export_changes(env.conn, 0)["changes"] if c["kind"] == "archive"]
    assert [c["data"]["op"] for c in sent] == ["pack", "members"]
    other = db.create(tmp_path / "other.db")
    other.execute("INSERT INTO servers(id, name, is_self) VALUES ('me', 'me', 1)")
    other.execute("INSERT INTO meta(key, value) VALUES ('server_id', 'me')")
    other.execute("INSERT INTO servers(id, name) VALUES (?, 'origin')", (env.server_id,))
    sync.apply_changes(other, [dict(c, ts=db.to_iso(db.now())) for c in sent])
    row = archives.get(other, "/src/foo")
    assert row["files"] == 4
    assert {m["path"] for m in other.execute("SELECT path FROM archive_members")} == {
        "a.c", "sub/b.c", "sub/deep/c.txt", ".git/objects/x"}
    assert [e.name for e in queries.list_dir(other, "/src") if e.packed] == ["foo"]
    # and of its removal
    archives.drop(env.conn, "/src/foo")
    removal = [c for c in sync.export_changes(env.conn, 0)["changes"]
               if c["kind"] == "archive" and c["data"]["op"] == "remove"]
    sync.apply_changes(other, [dict(c, ts=db.to_iso(db.now())) for c in removal])
    assert archives.get(other, "/src/foo") is None


def test_a_packed_directory_is_tagged_as_its_archive(env):
    from reifs import tags
    tree(env)
    pack(env)
    ids = tags.resolve_targets(env.conn, "/src/foo", False)
    assert ids == [index.file_id(env.conn, "/src/foo.sqsh")]


def test_the_database_gives_back_the_space_of_the_files_it_forgot(env):
    assert db.is_incremental(env.conn)
    for i in range(1500):
        write(env.root / "big" / "tiny" / f"file-with-a-longish-name-{i:05d}.txt", f"x{i}", mtime=1_000_000)
    env.scan()
    env.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    pages = env.conn.execute("PRAGMA page_count").fetchone()[0]
    res = archives.pack(env.conn, env.cfg, "/big/tiny")
    assert res["database_reclaimed"] > 0
    assert env.conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
    # the rows of 1500 files and copies (and the change-log entries that pointed at them) are gone
    assert env.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    assert env.conn.execute("PRAGMA page_count").fetchone()[0] <= pages


def test_an_old_database_is_converted_once(tmp_path, capsys):
    conn = db.connect(tmp_path / "old.db")
    conn.execute("PRAGMA auto_vacuum = NONE")
    conn.executescript("CREATE TABLE t(x); INSERT INTO t VALUES (1);")
    assert not db.is_incremental(conn)
    db.make_incremental(conn, tmp_path / "old.db")
    assert db.is_incremental(conn)
    assert conn.execute("SELECT x FROM t").fetchone()[0] == 1


# --- packing everywhere, and the jobs ---------------------------------------------------


def two_nodes(env):
    _, ssd = env.add_node("ssd")
    for base in (env.root, ssd):
        for i, name in enumerate(("a.c", "sub/b.c", "sub/deep/c.txt")):
            write(base / "src" / "foo" / name, f"content {i}" * 10, mtime=1_000_000)
    env.scan()
    return ssd


def run_jobs(env):
    from reifs import runner
    return runner.run_jobs(env.conn, env.cfg)


def test_pack_everywhere_copies_the_archive_and_removes_the_originals_after_checking(env):
    ssd = two_nodes(env)
    write(ssd / "src" / "foo" / "sub" / "b.c", "changed on the other node")    # differs
    env.scan()
    res = archives.pack(env.conn, env.cfg, "/src/foo", "srv", everywhere=True)
    assert res["originals_removed"] == 3 and res["targets"] == 1
    assert not (env.root / "src" / "foo").exists()
    assert (env.root / "src" / "foo.sqsh").exists()
    # the other server (here: the same one) owes a copy of the archive, then a removal
    assert env.conn.execute("SELECT stage FROM archive_todo").fetchone()[0] == "copy"
    assert archives.followup(env.conn, env.cfg) == 1
    run_jobs(env)                                                  # copies the archive to ssd
    assert (ssd / "src" / "foo.sqsh").exists()
    write(ssd / "src" / "foo" / "mine.txt", "made there meanwhile")           # not in the archive
    assert archives.followup(env.conn, env.cfg) == 1
    assert env.conn.execute("SELECT stage FROM archive_todo").fetchone()[0] == "clean"
    run_jobs(env)                                                  # removes what is verified
    # identical files are gone; the changed one and the extra one are kept
    assert (ssd / "src" / "foo" / "sub" / "b.c").read_text() == "changed on the other node"
    assert (ssd / "src" / "foo" / "mine.txt").exists()
    assert not (ssd / "src" / "foo" / "a.c").exists()
    assert not (ssd / "src" / "foo" / "sub" / "deep").exists()
    archives.followup(env.conn, env.cfg)
    assert env.conn.execute("SELECT COUNT(*) FROM archive_todo").fetchone()[0] == 0
    # the index no longer tracks the loose files, on any node
    assert vpaths(env) == ["/src/foo.sqsh"]


def test_a_removal_is_refused_where_the_archive_has_not_arrived(env):
    two_nodes(env)
    archives.pack(env.conn, env.cfg, "/src/foo", "srv", everywhere=True)
    ssd = [r["id"] for r in env.conn.execute("SELECT id FROM nodes WHERE name = 'ssd'")][0]
    with pytest.raises(archives.ArchiveError, match="not arrived"):
        archives.cleanup_node(env.conn, env.cfg, "/src/foo", ssd)


def test_the_server_asked_packs_on_its_own_node_when_it_holds_the_directory(env):
    two_nodes(env)
    assert archives.choose_node(env.conn, env.cfg, "/src/foo", None)["name"] == "srv"


def test_a_pack_job_can_be_cancelled_in_the_middle_and_leaves_nothing(env, monkeypatch):
    from reifs import jobs, runner
    monkeypatch.setattr(runner, "CANCEL_LOOK_SECONDS", 0)
    tree(env)
    node = env.conn.execute("SELECT id FROM nodes WHERE name = 'srv'").fetchone()[0]
    with db.transaction(env.conn):
        job_id = jobs.create_tool_job(env.conn, "pack", "pack /src/foo",
                                      [("pack", "/src/foo", node, {})])
    real = archives._run

    def cancel_then_run(cmd, stop, what):
        jobs.cancel_job(env.conn, job_id)          # the user presses c while mksquashfs runs
        return real(cmd, stop, what)

    monkeypatch.setattr(archives, "_run", cancel_then_run)
    run_jobs(env)
    state = env.conn.execute("SELECT state FROM job_items WHERE job_id = ?", (job_id,)).fetchone()[0]
    assert state == "cancelled"
    assert not (env.root / "src" / "foo.sqsh").exists()
    assert not list((env.root / "src").glob("*.part"))
    assert archives.get(env.conn, "/src/foo") is None
    assert (env.root / "src" / "foo" / "a.c").exists()               # nothing was touched
    assert env.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == "cancelled"


def test_a_scan_job_can_be_cancelled_before_it_runs_and_while_it_runs(env, monkeypatch):
    from reifs import jobs, runner, scanner
    monkeypatch.setattr(runner, "CANCEL_LOOK_SECONDS", 0)
    for i in range(30):
        write(env.root / "d" / f"f{i}.txt", str(i), mtime=1_000_000)
    node = env.conn.execute("SELECT id FROM nodes WHERE name = 'srv'").fetchone()[0]
    with db.transaction(env.conn):
        first = jobs.create_tool_job(env.conn, "scan", "scan srv", [("scan", "/", node, {"full": True})])
    jobs.cancel_job(env.conn, first)
    run_jobs(env)
    assert env.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0     # never ran
    with db.transaction(env.conn):
        second = jobs.create_tool_job(env.conn, "scan", "scan srv", [("scan", "/", node, {"full": True})])
    real = scanner.hash_file
    seen = []

    def cancel_midway(*a, **kw):
        seen.append(1)
        if len(seen) == 5:
            jobs.cancel_job(env.conn, second)       # the user presses c while it hashes
        return real(*a, **kw)

    monkeypatch.setattr(scanner, "hash_file", cancel_midway)
    run_jobs(env)
    assert env.conn.execute("SELECT state FROM job_items WHERE job_id = ?", (second,)).fetchone()[0] == "cancelled"
    assert len(seen) < 30                                   # it stopped
    assert env.conn.execute("SELECT COUNT(*) FROM copies WHERE state = 'missing'").fetchone()[0] == 0


def test_an_unpack_job_runs(env):
    from reifs import jobs
    tree(env)
    pack(env, remove_originals=True)
    node = env.conn.execute("SELECT id FROM nodes WHERE name = 'srv'").fetchone()[0]
    with db.transaction(env.conn):
        job_id = jobs.create_tool_job(env.conn, "unpack", "unpack /src/foo",
                                      [("unpack", "/src/foo", node, {})])
    run_jobs(env)
    assert env.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == "done"
    assert (env.root / "src" / "foo" / "a.c").exists() and archives.get(env.conn, "/src/foo") is None
