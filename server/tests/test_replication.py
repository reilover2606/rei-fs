import json

import pytest

from reifs import jobs, queries, replication, runner, tags
from reifs.config import Config

from conftest import write
from test_peers import cluster, needs_rsync, pair  # noqa: F401  (fixture)


def wanted(env, status="open"):
    return env.conn.execute(
        "SELECT w.*, n.name AS dst FROM wanted w JOIN nodes n ON n.id = w.dst_node"
        " WHERE w.status = ? AND w.reason = 'replicate' ORDER BY w.id", (status,)).fetchall()


def set_node(env, name, **fields):
    for k, v in fields.items():
        env.conn.execute(f"UPDATE nodes SET {k} = ? WHERE name = ?", (v, name))


@pytest.fixture
def three(env):
    """Server node with files, plus storage nodes 'ssd' and 'usb'."""
    env.add_node("ssd")
    env.add_node("usb")
    write(env.root / "a.txt", "alpha", mtime=1_000_000)
    write(env.root / "dir" / "b.txt", "beta", mtime=1_000_000)
    env.scan()
    return env


def test_proposes_second_copy_by_priority(three):
    set_node(three, "usb", priority=5)
    report = replication.plan(three.conn, three.cfg)
    assert (report.single_copy, report.responsible, report.proposed) == (2, 2, 2)
    assert {w["dst"] for w in wanted(three)} == {"usb"}
    # Planning again does not duplicate.
    report = replication.plan(three.conn, three.cfg)
    assert (report.proposed, report.already_proposed) == (0, 2)


def test_free_space_breaks_ties_and_excludes(three):
    set_node(three, "ssd", free_bytes=10 * 2**30)
    set_node(three, "usb", free_bytes=20 * 2**30)
    replication.plan(three.conn, three.cfg)
    assert {w["dst"] for w in wanted(three)} == {"usb"}
    for w in wanted(three):
        three.conn.execute("UPDATE wanted SET status = 'rejected' WHERE id = ?", (w["id"],))
    write(three.root / "c.txt", "gamma")
    three.scan()
    set_node(three, "usb", free_bytes=1)       # full
    set_node(three, "ssd", free_bytes=1)
    report = replication.plan(three.conn, three.cfg)
    assert report.no_destination == 1
    assert "free space" in next(iter(report.reasons))


def test_targets_and_legal_names(three):
    set_node(three, "ssd", replication_target=0)
    write(three.root / "odd?.txt", "q")
    three.scan()
    set_node(three, "usb", restricted_names=1)
    report = replication.plan(three.conn, three.cfg)
    dests = {queries.file_info(three.conn, w["file_id"]).vpath: w["dst"] for w in wanted(three)}
    assert dests == {"/a.txt": "usb", "/dir/b.txt": "usb"}
    assert report.no_destination == 1
    assert report.examples[0][0] == "/odd?.txt"


def test_offline_nodes_come_last(three):
    set_node(three, "usb", priority=9, online=0)
    replication.plan(three.conn, three.cfg)
    assert {w["dst"] for w in wanted(three)} == {"ssd"}


def test_declined_until_content_changes(three):
    replication.plan(three.conn, three.cfg)
    jobs.reject_wanted(three.conn, None)
    report = replication.plan(three.conn, three.cfg)
    assert (report.proposed, report.declined) == (0, 2)
    write(three.root / "a.txt", "alpha v2", mtime=2_000_000)
    three.scan()
    report = replication.plan(three.conn, three.cfg)
    assert report.proposed == 1


def test_approve_run_and_done(three):
    replication.plan(three.conn, three.cfg)
    res = replication.approve(three.conn, None)
    assert res.items == 2
    r = runner.run_jobs(three.conn, three.cfg)[0]
    assert r.done == 2
    assert queries.status(three.conn).under_replicated == 0
    replication.plan(three.conn, three.cfg)
    assert len(wanted(three, "done")) == 2
    assert wanted(three, "approved") == []


def test_marked_copies_do_not_count_and_are_not_destinations(three):
    jobs.create_transfer_job(three.conn, ["/a.txt"], None, "ssd")
    runner.run_jobs(three.conn, three.cfg)
    # Move a.txt away from the server node: its copy there is marked.
    jobs.create_transfer_job(three.conn, ["/a.txt"], None, "usb", src="srv", move=True)
    runner.run_jobs(three.conn, three.cfg)
    assert queries.lookup(three.conn, "/a.txt").good_copies == 2
    replication.plan(three.conn, three.cfg)
    paths_proposed = {queries.file_info(three.conn, w["file_id"]).vpath for w in wanted(three)}
    assert "/a.txt" not in paths_proposed


def test_auto_mode(three):
    three.cfg.server.auto_replicate = True
    report, approved = replication.cycle(three.conn, three.cfg)
    assert report.auto_approved == 2 and approved.local_job is not None
    assert runner.run_jobs(three.conn, three.cfg)[0].done == 2
    stored = replication.last_plan(three.conn)
    assert stored["auto_approved"] == 2


def test_auto_mode_leaves_other_proposals(three):
    """Restores/renames/overwrites come from the user's decisions; auto mode
    only approves replication proposals."""
    three.conn.execute("UPDATE nodes SET replication_target = 0")
    from reifs import index
    fid = index.file_id(three.conn, "/a.txt")
    index.add_wanted(three.conn, "copy", fid, None,
                     three.conn.execute("SELECT id FROM nodes WHERE name='ssd'").fetchone()[0],
                     "restore")
    three.cfg.server.auto_replicate = True
    replication.cycle(three.conn, three.cfg)
    assert three.conn.execute("SELECT status FROM wanted WHERE reason = 'restore'"
                              ).fetchone()[0] == "open"


def test_cli_report(three, capsys):
    from reifs import cli
    base = ["--home", str(three.cfg.home)]
    # The CLI loads its own config; keep discovery away from real mounts.
    cfg = Config.load(three.cfg.home)
    cfg.discovery.paths = three.cfg.discovery.paths
    cfg.save()
    assert cli.main(base + ["replication", "plan"]) == 0
    out = capsys.readouterr().out
    assert "proposed now:                   2" in out
    assert cli.main(base + ["wanted", "approve", "--all"]) == 0
    assert "2 item(s)" in capsys.readouterr().out
    assert cli.main(base + ["replication", "auto", "on"]) == 0
    assert Config.load(three.cfg.home).server.auto_replicate


# --- several servers -------------------------------------------------------------


def test_only_the_holder_plans(cluster):  # noqa: F811
    a, b = cluster("alpha"), cluster("beta")
    write(a.root / "x.txt", "x")
    a.scan()
    pair(a, b)
    b.sync(a)
    rb = replication.plan(b.conn, b.cfg)
    assert (rb.single_copy, rb.responsible, rb.proposed) == (1, 0, 0)
    ra = replication.plan(a.conn, a.cfg)
    assert ra.reasons == {"there is no other node": 1}  # alpha does not know beta's node yet
    a.sync(b)
    ra = replication.plan(a.conn, a.cfg)
    assert ra.proposed == 1
    [w] = a.conn.execute("SELECT * FROM wanted").fetchall()
    assert w["dst_node"] == b.id  # the only other node: beta's own storage


@needs_rsync
def test_remote_destination_job_created_on_its_server(cluster):  # noqa: F811
    a, b = cluster("alpha"), cluster("beta")
    write(a.root / "p.jpg", "photo", mtime=1_000_000)
    a.scan()
    pair(a, b)
    b.sync(a)
    a.sync(b)
    replication.plan(a.conn, a.cfg)
    res = replication.approve(a.conn, None)
    assert res.local_job is None and res.remote_jobs and not res.errors
    [w] = a.conn.execute("SELECT status, message FROM wanted").fetchall()
    assert w["status"] == "approved" and "beta" in w["message"]
    assert b.run_jobs()  # beta pulls from alpha
    assert (b.root / "p.jpg").read_text() == "photo"
    a.sync(b)
    assert queries.lookup(a.conn, "/p.jpg").good_copies == 2
    replication.plan(a.conn, a.cfg)
    assert a.conn.execute("SELECT status FROM wanted").fetchone()[0] == "done"


def test_unreachable_server_not_chosen(cluster):  # noqa: F811
    a, b = cluster("alpha"), cluster("beta")
    write(a.root / "q.txt", "q")
    a.scan()
    pair(a, b)
    a.sync(b)
    a.conn.execute("UPDATE servers SET online = 0 WHERE id = ?", (b.id,))
    report = replication.plan(a.conn, a.cfg)
    assert report.no_destination == 1
    assert "unreachable" in next(iter(report.reasons))


def test_free_space_is_announced(cluster):  # noqa: F811
    a, b = cluster("alpha"), cluster("beta")
    pair(a, b)
    b.sync(a)
    row = b.conn.execute("SELECT free_bytes, total_bytes FROM nodes WHERE name = 'alpha'"
                         ).fetchone()
    assert row["free_bytes"] and row["total_bytes"]
    payload = json.loads(a.conn.execute("SELECT data FROM changes WHERE kind = 'node'"
                                        " ORDER BY seq DESC LIMIT 1").fetchone()[0])
    assert "free_bytes" in payload
