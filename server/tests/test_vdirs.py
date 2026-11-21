"""Directories made by the user in the virtual tree."""

import pytest

from reifs import db, queries, runner, sync, vdirs, vmove

from conftest import write


def names(env, vdir="/"):
    entries = queries.list_dir(env.conn, vdir)
    return None if entries is None else [(e.name, e.is_dir, e.files) for e in entries]


def test_an_empty_directory_exists_until_removed(env):
    write(env.root / "docs" / "a.txt", "a", mtime=1_000_000)
    env.scan()
    vdirs.make(env.conn, "/new/deeper")
    assert names(env) == [("docs", True, 1), ("new", True, 0)]
    assert names(env, "/new") == [("deeper", True, 0)] and names(env, "/new/deeper") == []
    env.scan()                                     # a scan does not touch it
    assert names(env, "/new/deeper") == []
    with pytest.raises(vdirs.VDirError):
        vdirs.make(env.conn, "/new/deeper")         # exists
    with pytest.raises(vdirs.VDirError):
        vdirs.make(env.conn, "/docs/a.txt/x")       # below a file
    with pytest.raises(vdirs.VDirError):
        vdirs.remove(env.conn, "/docs")             # holds files
    assert vdirs.remove(env.conn, "/new") == 1
    assert names(env, "/new") is None and names(env) == [("docs", True, 1)]


def test_rename_and_move_carry_empty_directories(env):
    write(env.root / "docs" / "a.txt", "a", mtime=1_000_000)
    env.scan()
    vdirs.make(env.conn, "/docs/empty")
    vdirs.make(env.conn, "/loose")
    plan = vmove.plan_move(env.conn, ["/docs", "/loose"], "/archive")
    assert len(plan.dir_moves) == 2
    vmove.apply_plan(env.conn, plan, "move")
    runner.run_jobs(env.conn, env.cfg)
    env.scan()
    assert names(env) == [("archive", True, 1)]
    assert names(env, "/archive") == [("docs", True, 1), ("loose", True, 0)]
    assert names(env, "/archive/docs") == [("empty", True, 0), ("a.txt", False, 1)] or \
        sorted(n for n, _, _ in names(env, "/archive/docs")) == ["a.txt", "empty"]
    # Renaming an empty directory needs no files at all.
    plan = vmove.plan_move(env.conn, ["/archive/loose"], "/archive", name="tidy")
    vmove.apply_plan(env.conn, plan, "rename")
    assert [n for n, _, _ in names(env, "/archive")] == ["docs", "tidy"]
    # An existing empty directory is a name to keep or merge with.
    vdirs.make(env.conn, "/archive/tidy2")
    plan = vmove.plan_move(env.conn, ["/archive/tidy"], "/archive", name="tidy2",
                           on_conflict="preserve")
    assert plan.renamed == [("tidy2", "tidy2 (2)")]


def test_directories_replicate_last_writer_wins(env):
    me = env.conn.execute("SELECT value FROM meta WHERE key = 'server_id'").fetchone()[0]
    entry = lambda seq, present, ts: {"origin_id": "peer", "origin_seq": seq, "kind": "vdir",
                                      "ts": ts, "data": {"vpath": "/x", "present": present,
                                                         "updated": ts, "updated_by": "peer"}}
    sync.apply_changes(env.conn, [entry(1, 1, "2030-01-01T00:00:00+00:00")])
    assert names(env) == [("x", True, 0)]
    sync.apply_changes(env.conn, [entry(2, 0, "2029-01-01T00:00:00+00:00")])  # older: ignored
    assert names(env) == [("x", True, 0)]
    sync.apply_changes(env.conn, [entry(3, 0, "2031-01-01T00:00:00+00:00")])
    assert names(env, "/x") is None
    assert me  # our own changes are logged for peers
    vdirs.make(env.conn, "/mine")
    kinds = [r[0] for r in env.conn.execute("SELECT kind FROM changes WHERE origin_id = ?", (me,))]
    assert "vdir" in kinds
