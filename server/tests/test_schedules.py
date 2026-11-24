"""Scheduled syncs of a directory between two nodes."""

import shutil
import time

import pytest

from reifs import jobs, queries, runner, schedules

from conftest import write
from test_peers import cluster  # noqa: F401  (the fixture)

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


def test_intervals():
    assert [schedules.parse_interval(x) for x in ("90", "15m", "6h", "1d", "2w", "hourly", "weekly")] \
        == [90, 15, 360, 1440, 20160, 60, 10080]
    assert schedules.describe_interval(360) == "6h" and schedules.describe_interval(90) == "90m"
    for bad in ("", "abc", "0", "-5m"):
        with pytest.raises(schedules.ScheduleError):
            schedules.parse_interval(bad)


@pytest.fixture
def two(env):
    _, ssd = env.add_node("ssd")
    write(env.root / "docs" / "a.txt", "alpha", mtime=1_000_000)
    write(env.root / "docs" / "deep" / "b.txt", "beta", mtime=1_000_000)
    write(env.root / "other" / "c.txt", "gamma", mtime=1_000_000)
    env.scan()
    env.ssd = ssd
    return env


def finish(env, res):
    assert res.job_id is not None
    runner.run_jobs(env.conn, env.cfg, [res.job_id])
    env.scan()


def test_a_sync_copies_the_directory_and_waits_for_the_next_interval(two):
    sid = schedules.create(two.conn, "/docs", "srv", "ssd", "6h")
    res = schedules.run_due(two.conn)
    assert len(res) == 1 and res[0].items == 2                   # only /docs, not /other
    finish(two, res[0])
    assert (two.ssd / "docs" / "deep" / "b.txt").read_text() == "beta"
    assert not (two.ssd / "other").exists()
    row = schedules.get(two.conn, sid)
    assert 5.9 * 3600 < row["next_due"] - time.time() <= 6 * 3600
    assert schedules.run_due(two.conn) == []                     # not due again yet
    assert schedules.listing(two.conn)[0]["note"].startswith("job #")


def test_changed_files_replace_the_older_copy_but_a_newer_one_is_left_alone(two):
    sid = schedules.create(two.conn, "/docs", "srv", "ssd", "1h")
    finish(two, schedules.run_due(two.conn)[0])
    write(two.root / "docs" / "a.txt", "alpha v2", mtime=2_000_000)        # changed at the source
    write(two.ssd / "docs" / "deep" / "b.txt", "edited on the backup", mtime=3_000_000)  # newer there
    two.scan()
    res = schedules.run(two.conn, sid, force=True)
    assert res.items == 1 and "1 to update" in res.note and "1 left alone" in res.note
    finish(two, res)
    assert (two.ssd / "docs" / "a.txt").read_text() == "alpha v2"
    assert (two.ssd / "docs" / "deep" / "b.txt").read_text() == "edited on the backup"
    # With update_changed off nothing is replaced.
    schedules.update(two.conn, sid, update_changed=False)
    write(two.root / "docs" / "a.txt", "alpha v3", mtime=4_000_000)
    two.scan()
    res = schedules.run(two.conn, sid, force=True)
    assert res.job_id is None and "up to date" in res.note and "left alone" in res.note


def test_a_sleeping_node_means_a_catch_up_run_when_it_is_back(two):
    sid = schedules.create(two.conn, "/docs", "srv", "ssd", "1h")
    finish(two, schedules.run_due(two.conn)[0])
    write(two.root / "docs" / "new.txt", "new", mtime=2_000_000)
    two.scan()
    # Three intervals pass while the drive is unplugged.
    two.conn.execute("UPDATE schedules SET next_due = ? WHERE id = ?", (time.time() - 3 * 3600, sid))
    two.conn.execute("UPDATE nodes SET online = 0 WHERE name = 'ssd'")
    assert schedules.run_due(two.conn) == []
    row = schedules.listing(two.conn)[0]
    assert "not online" in row["note"] and row["waiting"]       # waiting, and it says why
    assert not (two.ssd / "docs" / "new.txt").exists()
    two.conn.execute("UPDATE nodes SET online = 1 WHERE name = 'ssd'")   # the drive is back
    res = schedules.run_due(two.conn)
    assert len(res) == 1                                         # ONE catch-up run, not three
    finish(two, res[0])
    assert (two.ssd / "docs" / "new.txt").read_text() == "new"
    assert schedules.run_due(two.conn) == []


def test_a_run_does_not_pile_up_while_the_last_one_is_going(two):
    sid = schedules.create(two.conn, "/docs", "srv", "ssd", "1h")
    first = schedules.run_due(two.conn)[0]                       # not run yet: still pending
    two.conn.execute("UPDATE schedules SET next_due = ? WHERE id = ?", (time.time() - 1, sid))
    assert schedules.run_due(two.conn) == []
    assert "still going" in schedules.listing(two.conn)[0]["note"]
    runner.run_jobs(two.conn, two.cfg, [first.job_id])
    two.conn.execute("UPDATE schedules SET next_due = ? WHERE id = ?", (time.time() - 1, sid))
    assert schedules.run_due(two.conn) == []                     # finished and nothing new: quietly up to date


def test_disabled_schedules_do_not_run_and_the_destination_must_be_ours(two):
    sid = schedules.create(two.conn, "/", "srv", "ssd", "1h", name="all")
    schedules.update(two.conn, sid, enabled=False)
    assert schedules.run_due(two.conn) == []
    with pytest.raises(schedules.ScheduleError):
        schedules.create(two.conn, "/", "ssd", "ssd", "1h")
    with pytest.raises(schedules.ScheduleError):
        schedules.create(two.conn, "/", "srv", "ssd", "1h", name="all")      # name taken
    schedules.remove(two.conn, sid)
    assert schedules.listing(two.conn) == []


def test_a_schedule_follows_its_directory_when_it_is_renamed_or_moved(two):
    from reifs import vmove
    sid = schedules.create(two.conn, "/docs", "srv", "ssd", "1h")
    deep = schedules.create(two.conn, "/docs/deep", "srv", "ssd", "1h", name="deep")
    other = schedules.create(two.conn, "/other", "srv", "ssd", "1h", name="other")
    plan = vmove.plan_move(two.conn, ["/docs"], "/", name="papers")
    vmove.apply_plan(two.conn, plan, "rename")
    assert schedules.get(two.conn, sid)["vpath"] == "/papers"
    assert schedules.get(two.conn, deep)["vpath"] == "/papers/deep"      # inside it: follows too
    assert schedules.get(two.conn, other)["vpath"] == "/other"           # unrelated: untouched
    assert "followed /docs to /papers" in schedules.get(two.conn, sid)["last_note"]
    runner.run_jobs(two.conn, two.cfg)
    two.scan()
    res = schedules.run(two.conn, sid, force=True)                       # it keeps working
    assert res.note.startswith("job #") or res.note.startswith("up to date")
    assert queries.lookup(two.conn, "/papers/a.txt") is not None


def test_a_directory_renamed_outside_rei_fs_is_reported_and_can_be_repointed(two):
    sid = schedules.create(two.conn, "/gone", "srv", "ssd", "1h")
    res = schedules.run(two.conn, sid, force=True)
    assert res.job_id is None and "nothing under /gone" in schedules.get(two.conn, sid)["last_note"]
    schedules.update(two.conn, sid, path="/docs")
    res = schedules.run(two.conn, sid, force=True)
    assert res.items == 2


def test_a_move_on_another_server_updates_this_servers_schedule(cluster):
    from reifs import vmove
    from reifs.service import sync_peer
    from test_peers import pair
    a, b = cluster("alpha"), cluster("beta")
    pair(a, b)
    write(a.root / "docs" / "x.txt", "x", mtime=1_000_000)
    from reifs import scanner
    scanner.scan_all(a.conn, a.cfg)
    for s_, o in ((b, a), (a, b), (b, a)):
        sync_peer(s_.conn, o.id)
    sid = schedules.create(b.conn, "/docs", "alpha", "beta", "1h")       # beta backs up alpha
    plan = vmove.plan_move(a.conn, ["/docs"], "/", name="papers")        # moved on alpha
    vmove.apply_plan(a.conn, plan, "rename")
    sync_peer(a.conn, b.id)                                              # delivers the notice
    assert schedules.get(b.conn, sid)["vpath"] == "/papers"
