"""Scheduled syncs of a directory between two nodes."""

import shutil
import time

import pytest

from reifs import jobs, queries, runner, schedules

from conftest import write

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
