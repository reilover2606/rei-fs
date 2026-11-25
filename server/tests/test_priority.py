"""Update priority of directories and what it does to scanning."""

import os
import time

import pytest

from reifs import priority, queries, scanner, sync, vmove, runner

from conftest import write


def due(env, node_id, **kw):
    return priority.due_classes(env.conn, env.cfg, node_id, priority.Prefs(priority.load(env.conn)),
                                **kw)


def test_priority_is_inherited_by_subdirectories(env):
    write(env.root / "a" / "f.txt", "x", mtime=1_000_000)
    env.scan()
    assert priority.get(env.conn, "/a/b")["priority"] == priority.DEFAULT == 2
    priority.set_priority(env.conn, "/a", 3)
    info = priority.get(env.conn, "/a/b/c")
    assert (info["priority"], info["name"], info["set_on"], info["inherited"]) == \
        (3, "archival", "/a", True)
    priority.set_priority(env.conn, "/a/b", priority.parse("frequent"))
    assert priority.get(env.conn, "/a/b/c/d")["priority"] == 1
    assert priority.get(env.conn, "/a/x")["priority"] == 3
    priority.set_priority(env.conn, "/a/b", None)                 # inherit again
    assert priority.get(env.conn, "/a/b")["priority"] == 3
    # a file can have a setting of its own, which wins over its directory's
    priority.set_priority(env.conn, "/a/f.txt", 0)
    prefs = priority.Prefs(priority.load(env.conn))
    assert prefs.effective_file("/a/f.txt") == 0 and prefs.effective_file("/a/g.txt") == 3
    with pytest.raises(priority.PriorityError):
        priority.parse(7)
    assert [priority.parse(v) for v in ("0", 1, "Archival", None, "inherit")] == [0, 1, 3, None, None]


def test_only_directories_due_are_scanned(env):
    for name, content in (("hot", "h"), ("cold", "c"), ("normal", "n")):
        write(env.root / name / "f.txt", content, mtime=1_000_000)
    env.scan()
    priority.set_priority(env.conn, "/hot", 1)
    priority.set_priority(env.conn, "/cold", 3)
    node = env.conn.execute("SELECT id FROM nodes").fetchone()[0]

    def auto():
        _, results = scanner.scan_all(env.conn, env.cfg, full=False)
        return results

    assert auto() == []                       # just scanned everything: nothing is due
    # Everything changes on disk.
    for name in ("hot", "cold", "normal"):
        write(env.root / name / "f.txt", name + " changed", mtime=2_000_000)
    write(env.root / "cold" / "new.txt", "new", mtime=2_000_000)
    assert auto() == []

    def age(cls, minutes):
        env.conn.execute("UPDATE scan_state SET scanned = scanned - ? WHERE node_id = ?"
                         " AND priority = ?", (minutes * 60, node, cls))

    age(1, 11)                                # frequent: every 10 minutes
    assert due(env, node) == {1}
    (res,) = auto()
    assert res.stats.changed == 1 and res.stats.added == 0 and res.stats.skipped_dirs == 2
    assert due(env, node) == set()
    age(3, 10_000)                            # archival: never again by itself
    assert due(env, node) == set()
    age(2, 61)                                # infrequent: every 60 minutes
    (res,) = auto()
    assert res.stats.changed == 1 and res.stats.added == 0   # /normal only
    # What was not visited is untouched, not "missing"; a full scan catches up.
    (cold,) = (queries.lookup(env.conn, "/cold/f.txt").copies)
    assert cold.state == "present" and queries.lookup(env.conn, "/cold/new.txt") is None
    os.remove(env.root / "cold" / "f.txt")
    age(2, 61)
    auto()
    assert queries.lookup(env.conn, "/cold/f.txt").copies[0].state == "present"
    full = env.scan()
    (r,) = full.values()
    assert r.stats.added == 1 and r.stats.missing == 1 and r.stats.skipped_dirs == 0


def test_a_never_scanned_node_gets_everything(env):
    write(env.root / "x" / "f.txt", "x", mtime=1_000_000)
    write(env.root / "y" / "g.txt", "y", mtime=1_000_000)
    priority.set_priority(env.conn, "/y", 3)
    _, results = scanner.scan_all(env.conn, env.cfg, full=False)
    assert results[0].stats.added == 2 and results[0].stats.skipped_dirs == 0


def test_nested_priorities(env):
    write(env.root / "arch" / "old.txt", "o", mtime=1_000_000)
    write(env.root / "arch" / "live" / "doc.txt", "d", mtime=1_000_000)
    env.scan()
    priority.set_priority(env.conn, "/arch", 3)
    priority.set_priority(env.conn, "/arch/live", 0)       # a hot spot inside an archive
    node = env.conn.execute("SELECT id FROM nodes").fetchone()[0]
    env.conn.execute("UPDATE scan_state SET scanned = scanned - 120 WHERE priority = 0")
    write(env.root / "arch" / "live" / "doc.txt", "edited", mtime=2_000_000)
    write(env.root / "arch" / "old.txt", "edited too", mtime=2_000_000)
    assert due(env, node) == {0}
    (res,) = scanner.scan_all(env.conn, env.cfg, full=False)[1]
    assert res.stats.changed == 1                          # only the live part
    assert queries.lookup(env.conn, "/arch/old.txt").copies[0].hash != \
        queries.lookup(env.conn, "/arch/live/doc.txt").copies[0].hash


def test_priority_replicates_and_follows_a_move(env):
    entry = lambda seq, value, ts: {"origin_id": "peer", "origin_seq": seq, "kind": "dirpref",
                                    "ts": ts, "data": {"vpath": "/p", "priority": value,
                                                       "updated": ts, "updated_by": "peer"}}
    sync.apply_changes(env.conn, [entry(1, 3, "2030-01-01T00:00:00+00:00")])
    assert priority.get(env.conn, "/p/x")["priority"] == 3
    sync.apply_changes(env.conn, [entry(2, 0, "2029-01-01T00:00:00+00:00")])   # older: ignored
    assert priority.get(env.conn, "/p")["priority"] == 3
    sync.apply_changes(env.conn, [entry(3, None, "2031-01-01T00:00:00+00:00")])  # cleared
    assert priority.get(env.conn, "/p")["priority"] == 2

    write(env.root / "docs" / "a.txt", "a", mtime=1_000_000)
    env.scan()
    priority.set_priority(env.conn, "/docs", 3)
    plan = vmove.plan_move(env.conn, ["/docs"], "/", name="papers")
    vmove.apply_plan(env.conn, plan, "rename")
    runner.run_jobs(env.conn, env.cfg)
    env.scan()
    assert priority.get(env.conn, "/papers")["priority"] == 3
    assert priority.get(env.conn, "/docs")["priority"] == 2


def test_new_only_scan_only_discovers_new_files(env):
    write(env.root / "a.txt", "one", mtime=1_000_000)
    write(env.root / "b.txt", "two", mtime=1_000_000)
    env.scan()
    write(env.root / "a.txt", "one changed", mtime=2_000_000)     # changed
    os.remove(env.root / "b.txt")                                 # gone
    write(env.root / "sub" / "c.txt", "three", mtime=1_000_000)   # new
    (_, results) = scanner.scan_all(env.conn, env.cfg, new_only=True)
    s = results[0].stats
    assert (s.added, s.hashed, s.changed, s.missing, s.renamed) == (1, 1, 0, 0, 0)
    assert queries.lookup(env.conn, "/sub/c.txt") is not None
    old_hash = queries.lookup(env.conn, "/a.txt").copies[0].hash
    assert old_hash != scanner.hash_file(env.root / "a.txt")      # the change was not looked at
    assert queries.lookup(env.conn, "/b.txt").copies[0].state == "present"   # not "missing"
    full = env.scan()
    (r,) = full.values()
    assert (r.stats.changed, r.stats.missing, r.stats.added) == (1, 1, 0)
