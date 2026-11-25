"""A deleted or moved folder is one decision, not one per file."""

import os
import shutil

import pytest

from reifs import decisions, jobs, queries, runner, scanner

from conftest import write

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


def top(env):
    return [d for d in decisions.list_decisions(env.conn)]


@pytest.fixture
def two(env):
    _, ssd = env.add_node("ssd")
    for i in range(12):
        write(env.root / "jar" / "com" / "x" / f"C{i}.class", f"class {i}", mtime=1_000_000)
    write(env.root / "jar" / "META-INF" / "m.txt", "meta", mtime=1_000_000)
    write(env.root / "keep" / "a.txt", "a", mtime=1_000_000)
    env.scan()
    rep = jobs.create_transfer_job(env.conn, ["/jar", "/keep"], None, "ssd")
    runner.run_jobs(env.conn, env.cfg, [rep.job_id])
    env.ssd = ssd
    return env


def test_a_deleted_folder_is_one_decision(two):
    shutil.rmtree(two.root / "jar")
    two.scan()
    (d,) = top(two)
    assert d.kind == "group" and d.data["mode"] == "gone" and d.data["dir"] == "/jar"
    assert decisions.group_info(two.conn, d.id)[0] == 13
    assert queries.status(two.conn).open_decisions == 1
    assert "/jar/com/x/C0.class" in decisions.group_members(two.conn, d.id, 100)
    # One choice for all of them (the files still exist on the ssd node: restore them).
    msg = decisions.resolve(two.conn, d.id, "restore")
    assert "13 decision(s) resolved" in msg
    assert top(two) == []
    wanted = two.conn.execute("SELECT op, dst_node FROM wanted WHERE status = 'open'").fetchall()
    assert len(wanted) == 13 and {w["op"] for w in wanted} == {"copy"}


def test_few_files_stay_individual_and_many_in_one_folder_group(two):
    for i in range(3):
        os.remove(two.root / "jar" / "com" / "x" / f"C{i}.class")
    two.scan()
    assert [d.kind for d in top(two)] == ["missing"] * 3
    for i in range(3, 12):
        os.remove(two.root / "jar" / "com" / "x" / f"C{i}.class")
    two.scan()
    kinds = sorted(d.kind for d in top(two))
    assert kinds == ["group", "missing", "missing", "missing"]   # the 9 new ones are grouped
    g = next(d for d in top(two) if d.kind == "group")
    assert g.data["mode"] == "in" and g.data["dir"] == "/jar/com/x"
    assert decisions.group_info(two.conn, g.id)[0] == 9


def test_a_moved_folder_is_one_rename_decision(two):
    shutil.move(str(two.root / "jar"), str(two.root / "jar2"))
    two.scan()
    (d,) = top(two)
    assert d.kind == "group" and d.data["member_kind"] == "rename"
    assert (d.data["from"], d.data["to"]) == ("/jar", "/jar2")
    msg = decisions.resolve(two.conn, d.id, "apply")
    assert "13 decision(s) resolved" in msg
    ops = two.conn.execute("SELECT op, COUNT(*) FROM wanted WHERE status = 'open' GROUP BY op").fetchall()
    assert [tuple(r) for r in ops] == [("rename", 13)]


def test_restoring_a_group_leaves_last_copies_to_be_decided_alone(env):
    for i in range(6):
        write(env.root / "only" / f"f{i}.txt", f"{i}", mtime=1_000_000)
    env.scan()
    shutil.rmtree(env.root / "only")
    env.scan()                                   # the only copies: nothing to restore from
    (g,) = top(env)
    assert g.kind == "group"
    msg = decisions.resolve(env.conn, g.id, "restore")
    assert "0 decision(s) resolved" in msg and "6 could not be done" in msg
    assert [d.kind for d in top(env)] == ["missing"] * 6      # open again, one by one
    d = top(env)[0]
    assert list(d.options) == ["acknowledge"]


def test_forget_applies_to_last_copies_too(env):
    for i in range(6):
        write(env.root / "only" / f"f{i}.txt", f"{i}", mtime=1_000_000)
    env.scan()
    shutil.rmtree(env.root / "only")
    env.scan()
    (g,) = top(env)
    assert "6 decision(s) resolved" in decisions.resolve(env.conn, g.id, "forget")
    assert top(env) == [] and queries.lookup(env.conn, "/only/f0.txt") is None


def test_files_that_come_back_empty_the_group(two):
    shutil.rmtree(two.root / "jar")
    two.scan()
    (g,) = top(two)
    shutil.copytree(two.ssd / "jar", two.root / "jar")           # restored by hand
    two.scan()
    assert top(two) == []                                         # the group closes by itself
    assert decisions.count_decisions(two.conn) == 0


def test_grouping_does_not_look_up_the_open_groups_once_per_folder(env):
    """Thousands of folders with a vanished file each, and thousands of open groups, were a
    scan of every group for every folder (minutes). The open groups are read once."""
    import json
    from reifs import db, index
    node = env.conn.execute("SELECT id FROM nodes").fetchone()[0]
    with db.transaction(env.conn):
        old = index.add_decision(env.conn, "group", None, node,
                                 {"key": "in:/d0", "member_kind": "missing", "dir": "/d0", "mode": "in"})
        entries = []
        for i in range(300):
            did = index.add_decision(env.conn, "missing", None, node, {"vpath": f"/d{i}/x"})
            entries.append((did, f"in:/d{i % 100}", {"dir": f"/d{i % 100}", "mode": "in"}))
        seen = []
        env.conn.set_trace_callback(lambda sql: seen.append(sql) if "json_extract" in sql else None)
        grouped = index.group_decisions(env.conn, "missing", node, entries, 3)
        env.conn.set_trace_callback(None)
    assert grouped == 300 and len(seen) == 1                        # one look, not one per folder
    parents = env.conn.execute("SELECT parent_id, COUNT(*) FROM decisions WHERE parent_id IS NOT NULL"
                               " GROUP BY parent_id").fetchall()
    assert len(parents) == 100 and {r[1] for r in parents} == {3}
    assert env.conn.execute("SELECT COUNT(*) FROM decisions WHERE parent_id = ?", (old,)).fetchone()[0] == 3
