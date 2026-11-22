"""Fetching files into the fetch folder, and passing files through an intermediate server."""

import shutil

import pytest

from reifs import jobs, peers, queries, runner, scanner
from reifs.service import sync_peer

from conftest import write
from test_peers import cluster, pair  # noqa: F401  (the cluster fixture)

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


def test_fetch_downloads_into_the_fetch_folder_and_indexes_nothing(env):
    _, ssd = env.add_node("ssd")
    write(ssd / "docs" / "a.txt", "alpha", mtime=1_000_000)
    write(ssd / "docs" / "deep" / "b.txt", "beta", mtime=1_000_000)
    env.scan()
    rep = jobs.create_fetch_job(env.conn, ["/docs"], None)
    assert rep.items == 2
    r = runner.run_jobs(env.conn, env.cfg, [rep.job_id])[0]
    assert (r.status, r.done) == ("done", 2)
    assert (env.root / "fetch" / "docs" / "a.txt").read_text() == "alpha"
    assert (env.root / "fetch" / "docs" / "deep" / "b.txt").read_text() == "beta"
    # Nothing is registered, and the scan leaves the folder alone.
    assert {c.node_name for c in queries.lookup(env.conn, "/docs/a.txt").copies} == {"ssd"}
    stats = env.scan()["srv"].stats
    assert (stats.added, stats.missing, stats.forgotten) == (0, 0, 0)
    assert queries.lookup(env.conn, "/fetch/docs/a.txt") is None
    # Again: already there. A different file in the way is never replaced.
    again = jobs.create_fetch_job(env.conn, ["/docs/a.txt"], None)
    assert runner.run_jobs(env.conn, env.cfg, [again.job_id])[0].status == "done"
    (env.root / "fetch" / "docs" / "a.txt").write_text("my own edit")
    third = jobs.create_fetch_job(env.conn, ["/docs/a.txt"], None)
    runner.run_jobs(env.conn, env.cfg, [third.job_id])
    assert jobs.get_job(env.conn, third.job_id)["status"] == "failed"
    assert (env.root / "fetch" / "docs" / "a.txt").read_text() == "my own edit"


def test_a_folder_of_the_users_called_fetch_stays_indexed(env):
    env.cfg.storage.fetch_dir = "downloads"            # while the folder was not reserved...
    write(env.root / "fetch" / "mine.txt", "keep", mtime=1_000_000)
    env.scan()
    assert queries.lookup(env.conn, "/fetch/mine.txt") is not None
    env.cfg.storage.fetch_dir = "fetch"                # ...it is tracked: it must not be forgotten
    stats = env.scan()["srv"].stats
    assert queries.lookup(env.conn, "/fetch/mine.txt") is not None and stats.forgotten == 0


def settle(servers, rounds=2):
    for _ in range(rounds):
        for s in servers:
            for p in peers.paired_peers(s.conn):
                sync_peer(s.conn, p["id"])


def test_a_file_is_passed_through_an_intermediate_server(cluster):  # noqa: F811
    a, b, c = cluster("alpha"), cluster("beta"), cluster("gamma")
    pair(a, b)
    pair(b, c)                         # alpha and gamma are NOT paired
    write(c.root / "docs" / "x.txt", "from gamma", mtime=1_000_000)
    scanner.scan_all(c.conn, c.cfg)
    settle([a, b, c], rounds=3)        # the file lists reach alpha through beta
    assert queries.lookup(a.conn, "/docs/x.txt") is not None
    rep = jobs.create_fetch_job(a.conn, ["/docs/x.txt"], None)

    runner.run_jobs(a.conn, a.cfg, [rep.job_id])            # alpha asks beta
    item = a.conn.execute("SELECT * FROM job_items WHERE job_id = ?", (rep.job_id,)).fetchone()
    assert item["state"] == "pending" and item["relay_via"] == b.id
    assert "asked server beta" in item["message"]

    runner.run_jobs(b.conn, b.cfg)                          # beta fetches it from gamma
    assert b.conn.execute("SELECT state FROM relay_requests").fetchone()[0] == "ready"
    staged = list((b.cfg.home / "relay").glob("*/docs/x.txt"))
    assert len(staged) == 1 and staged[0].read_text() == "from gamma"

    r = runner.run_jobs(a.conn, a.cfg, [rep.job_id])[0]     # alpha pulls it from beta
    assert r.status == "done", r.messages
    assert (a.root / "fetch" / "docs" / "x.txt").read_text() == "from gamma"
    assert not list((b.cfg.home / "relay").glob("*/docs/x.txt"))        # beta cleaned up
    assert b.conn.execute("SELECT state FROM relay_requests").fetchone()[0] == "done"


def test_without_any_route_the_job_says_so(cluster):  # noqa: F811
    a, b, c = cluster("alpha"), cluster("beta"), cluster("gamma")
    pair(a, b)
    pair(a, c)
    write(c.root / "docs" / "x.txt", "x", mtime=1_000_000)
    scanner.scan_all(c.conn, c.cfg)
    settle([a, b, c], rounds=3)
    c.conn.execute("UPDATE servers SET online = 0")         # gamma cannot be reached by anyone
    a.conn.execute("UPDATE servers SET online = 0 WHERE id = ?", (c.id,))
    rep = jobs.create_fetch_job(a.conn, ["/docs/x.txt"], None)
    runner.run_jobs(a.conn, a.cfg, [rep.job_id])
    msg = a.conn.execute("SELECT message FROM job_items WHERE job_id = ?", (rep.job_id,)
                         ).fetchone()[0]
    assert "gamma" in msg and ("unreachable" in msg or "cannot reach" in msg or "can reach" in msg)
