from reifs import queries, replication, tolerance

from conftest import write
from test_replication import set_node, wanted


def diverged(env):
    _, ssd = env.add_node("ssd")
    write(env.root / "notes" / "doc.txt", "v1", mtime=1_000_000)
    write(ssd / "notes" / "doc.txt", "v1", mtime=1_000_000)
    env.scan()
    write(env.root / "notes" / "doc.txt", "v2 newer", mtime=2_000_000)
    env.scan()
    return ssd


# --- 4: files and directories that may have several versions --------------------------


def test_a_conflict_is_asked_unless_the_path_is_tolerant(env):
    diverged(env)
    assert len(env.decisions("divergent")) == 1
    tolerance.set_tolerance(env.conn, "/notes", True)            # a directory: inherited
    assert env.decisions("divergent") == []                      # the open question went
    write(env.root / "notes" / "doc.txt", "v3 newest", mtime=3_000_000)
    env.scan()
    assert env.decisions("divergent") == []                      # and none is raised
    info = queries.lookup(env.conn, "/notes/doc.txt")
    assert info.tolerant and not any(c.state == "present" and not c.current for c in info.copies)
    assert info.good_copies == 2                                 # both versions count


def test_the_nearest_setting_decides(env):
    tolerance.set_tolerance(env.conn, "/a", True)
    tolerance.set_tolerance(env.conn, "/a/b", False)
    assert tolerance.get(env.conn, "/a/x.txt")["tolerant"]
    assert not tolerance.get(env.conn, "/a/b/x.txt")["tolerant"]
    tolerance.set_tolerance(env.conn, "/a/b", None)              # back to inheriting
    r = tolerance.get(env.conn, "/a/b/x.txt")
    assert r["tolerant"] and r["set_on"] == "/a" and r["inherited"]
    assert not tolerance.get(env.conn, "/other").get("tolerant")


def test_tolerant_files_are_not_planned_for_overwriting(env):
    diverged(env)
    tolerance.set_tolerance(env.conn, "/notes/doc.txt", True)
    stats, _ = queries.coverage_stats(env.conn, replication.policy(env.cfg))
    assert stats.under_replicated == 0                           # two nodes hold it: goal met


def test_the_setting_follows_a_replicated_change(env):
    from reifs import sync
    other = diverged(env)
    tolerance.set_tolerance(env.conn, "/notes", True)
    sent = [c for c in sync.export_changes(env.conn, 0)["changes"] if c["kind"] == "tolerance"]
    assert sent and sent[-1]["data"]["value"] == 1


# --- 5: node flags -----------------------------------------------------------------------


def test_a_mirrored_drive_counts_as_two_copies(env):
    env.add_node("raid")
    write(env.root / "a.txt", "alpha", mtime=1_000_000)
    env.scan()
    pol = replication.policy(env.cfg)
    assert queries.coverage_stats(env.conn, pol)[0].single_copy == 1
    set_node(env, "srv", mirrored=1)                             # the server's own drive is mirrored
    info = queries.lookup(env.conn, "/a.txt")
    assert info.good_copies == 2
    replication.plan(env.conn, env.cfg)
    assert wanted(env) == []                                     # nothing to back up: already two


def test_backup_first_nodes_are_filled_first(env):
    env.add_node("ssd")
    env.add_node("usb")
    write(env.root / "a.txt", "alpha", mtime=1_000_000)
    env.scan()
    set_node(env, "ssd", priority=9)                             # normally the preferred target
    set_node(env, "usb", backup_first=1)
    replication.plan(env.conn, env.cfg)
    assert {w["dst"] for w in wanted(env)} == {"usb"}


def test_a_mirrored_target_satisfies_the_goal_alone(env):
    env.add_node("raid")
    write(env.root / "a.txt", "alpha", mtime=1_000_000)
    env.scan()
    env.cfg.replication.copies = 3
    set_node(env, "raid", mirrored=1)
    replication.plan(env.conn, env.cfg)
    assert {w["dst"] for w in wanted(env)} == {"raid"}           # 1 here + 2 on the mirror = 3
