import os
import shutil
import sqlite3
from collections import namedtuple

import pytest

from reifs import db, decisions, jobs, queries, runner, tags, transfer
from reifs.locking import LockError, ServerLock

from conftest import write

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


def run(env, ids=None):
    return {r.job_id: r for r in runner.run_jobs(env.conn, env.cfg, ids)}


def node_copies(env, vpath):
    info = queries.lookup(env.conn, vpath)
    return {c.node_name: c for c in info.copies} if info else {}


def items(env, job_id):
    return env.conn.execute("SELECT * FROM job_items WHERE job_id = ? ORDER BY id",
                            (job_id,)).fetchall()


@pytest.fixture
def two(env):
    """Server node with some files plus an empty storage node 'ssd'."""
    _, ssd = env.add_node("ssd")
    write(env.root / "docs" / "a.txt", "alpha", mtime=1_000_000)
    write(env.root / "docs" / "deep" / "b.txt", "beta", mtime=1_000_000)
    write(env.root / "other" / "c.txt", "gamma", mtime=1_000_000)
    env.scan()
    env.ssd = ssd
    return env


def test_copy_creates_minimal_dirs_and_records_copy(two):
    rep = jobs.create_transfer_job(two.conn, ["/docs/deep/b.txt"], None, "ssd")
    r = run(two)[rep.job_id]
    assert (r.status, r.done) == ("done", 1)
    files = sorted(str(p.relative_to(two.ssd)) for p in two.ssd.rglob("*")
                   if ".rei-fs" not in p.parts)
    assert files == ["docs", os.path.join("docs", "deep"),
                     os.path.join("docs", "deep", "b.txt")]
    dst = two.ssd / "docs" / "deep" / "b.txt"
    assert dst.read_text() == "beta" and dst.stat().st_mtime == 1_000_000
    c = node_copies(two, "/docs/deep/b.txt")
    assert c["ssd"].current and queries.lookup(two.conn, "/docs/deep/b.txt").good_copies == 2
    # The next scan agrees with what the job recorded.
    s = two.scan()["ssd"].stats
    assert (s.added, s.hashed, s.changed, s.renamed) == (0, 0, 0, 0)


def test_copy_directory_and_exclusions(two):
    jobs.create_transfer_job(two.conn, ["/docs/a.txt"], None, "ssd")
    run(two)
    rep = jobs.create_transfer_job(two.conn, ["/docs"], None, "ssd")
    assert rep.items == 1 and "already on ssd" in rep.excluded
    with pytest.raises(jobs.JobError):
        jobs.create_transfer_job(two.conn, ["/nope"], None, "ssd")
    with pytest.raises(jobs.JobError):
        jobs.create_transfer_job(two.conn, ["/docs"], None, "ssd", move=True)  # no --from


def test_move_by_tag_marks_sources(two):
    ids = tags.resolve_targets(two.conn, "/docs/a.txt", False) + \
        tags.resolve_targets(two.conn, "/other/c.txt", False)
    tags.set_tag(two.conn, ids, "example", True, two.server_id)
    rep = jobs.create_transfer_job(two.conn, [], "example", "ssd", src="srv", move=True)
    assert rep.items == 2
    run(two)
    assert (two.ssd / "docs" / "a.txt").exists() and (two.ssd / "other" / "c.txt").exists()
    assert not (two.ssd / "docs" / "deep").exists()     # untagged file not copied
    assert node_copies(two, "/docs/a.txt")["srv"].marked_delete
    assert (two.root / "docs" / "a.txt").exists()       # never deleted by the server
    assert len(jobs.marked_copies(two.conn)) == 2
    # Marked copies don't count towards the goal.
    assert queries.lookup(two.conn, "/docs/a.txt").good_copies == 1

    (two.root / "docs" / "a.txt").unlink()
    assert jobs.unmark(two.conn, ["/other"], None) == 1
    two.scan()
    assert set(node_copies(two, "/docs/a.txt")) == {"ssd"}
    assert two.decisions() == []


def test_move_when_already_on_destination(two):
    shutil.copy2(two.root / "docs" / "a.txt", two.ssd / "a-elsewhere")  # unrelated
    (two.ssd / "docs").mkdir()
    shutil.copy2(two.root / "docs" / "a.txt", two.ssd / "docs" / "a.txt")
    rep = jobs.create_transfer_job(two.conn, ["/docs/a.txt"], None, "ssd", src="srv", move=True)
    r = run(two)[rep.job_id]
    assert r.done == 1 and items(two, rep.job_id)[0]["message"] == "already present"
    assert node_copies(two, "/docs/a.txt")["srv"].marked_delete


def test_conflict_overwrite_and_skip(two):
    write(two.ssd / "docs" / "a.txt", "something else")
    write(two.ssd / "other" / "c.txt", "also else")
    rep = jobs.create_transfer_job(two.conn, ["/docs/a.txt", "/other/c.txt"], None, "ssd")
    r = run(two)[rep.job_id]
    assert (r.status, r.conflicts) == ("waiting", 2)
    by_path = {decisions.get_decision(two.conn, d["id"]).vpath: d["id"]
               for d in two.decisions("conflict")}
    decisions.resolve(two.conn, by_path["/other/c.txt"], "skip")
    decisions.resolve(two.conn, by_path["/docs/a.txt"], "overwrite")
    r = run(two)[rep.job_id]
    assert r.status == "done"
    assert (two.ssd / "docs" / "a.txt").read_text() == "alpha"
    assert (two.ssd / "other" / "c.txt").read_text() == "also else"


def test_conflict_overwrite_refused_if_destination_changed_again(two):
    write(two.ssd / "docs" / "a.txt", "v-other")
    rep = jobs.create_transfer_job(two.conn, ["/docs/a.txt"], None, "ssd")
    run(two)
    [d] = two.decisions("conflict")
    decisions.resolve(two.conn, d["id"], "overwrite")
    write(two.ssd / "docs" / "a.txt", "changed after the decision")
    r = run(two)[rep.job_id]
    assert r.conflicts == 1
    assert (two.ssd / "docs" / "a.txt").read_text() == "changed after the decision"


def test_restricted_names_skipped_but_legal_nodes_ok(two):
    write(two.root / "odd" / "what?.txt", "q")
    two.scan()
    two.conn.execute("UPDATE nodes SET restricted_names = 1 WHERE name = 'ssd'")
    rep = jobs.create_transfer_job(two.conn, ["/odd"], None, "ssd")
    r = run(two)[rep.job_id]
    assert r.skipped == 1 and "not allowed" in items(two, rep.job_id)[0]["message"]
    two.conn.execute("UPDATE nodes SET restricted_names = 0 WHERE name = 'ssd'")
    rep = jobs.create_transfer_job(two.conn, ["/odd"], None, "ssd")
    assert run(two)[rep.job_id].done == 1


def test_case_insensitive_directory_clash(two):
    two.conn.execute("UPDATE nodes SET case_sensitive = 0 WHERE name = 'ssd'")
    (two.ssd / "Docs").mkdir()
    rep = jobs.create_transfer_job(two.conn, ["/docs/a.txt"], None, "ssd")
    r = run(two)[rep.job_id]
    assert r.skipped == 1 and "collide" in items(two, rep.job_id)[0]["message"]
    assert list((two.ssd / "Docs").iterdir()) == []


def test_offline_destination_waits_then_resumes(two):
    rep = jobs.create_transfer_job(two.conn, ["/docs"], None, "ssd")
    away = two.tmp / "unplugged"
    two.ssd.rename(away)
    r = run(two)[rep.job_id]
    assert (r.status, r.waiting, r.done) == ("waiting", 2, 0)
    away.rename(two.ssd)
    r = run(two)[rep.job_id]
    assert (r.status, r.done) == ("done", 2)


def test_interrupted_run_resumes(two, monkeypatch):
    two.cfg.transfers.batch_files = 1
    rep = jobs.create_transfer_job(two.conn, ["/docs", "/other"], None, "ssd")
    real = transfer.run_batch
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real(*a, **kw)

    monkeypatch.setattr(transfer, "run_batch", flaky)
    with pytest.raises(KeyboardInterrupt):
        run(two)
    states = [i["state"] for i in items(two, rep.job_id)]
    assert states == ["done", "pending", "pending"]
    assert jobs.get_job(two.conn, rep.job_id)["status"] == "pending"
    monkeypatch.setattr(transfer, "run_batch", real)
    r = run(two)[rep.job_id]
    assert (r.status, r.done) == ("done", 2)


def test_crash_leftover_running_items_are_requeued(two):
    rep = jobs.create_transfer_job(two.conn, ["/docs/a.txt"], None, "ssd")
    two.conn.execute("UPDATE job_items SET state = 'running'")
    two.conn.execute("UPDATE jobs SET status = 'running'")
    assert run(two)[rep.job_id].status == "done"


def test_rsync_failure_retries_then_fails(two):
    two.cfg.rsync.path = str(two.tmp / "no-such-rsync")
    two.cfg.transfers.max_attempts = 2
    rep = jobs.create_transfer_job(two.conn, ["/docs/a.txt"], None, "ssd")
    r = run(two)[rep.job_id]
    assert (r.retry, r.status) == (1, "waiting")
    r = run(two)[rep.job_id]
    assert (r.failed, r.status) == (1, "failed")
    assert "cannot run rsync" in items(two, rep.job_id)[0]["message"]
    assert run(two) == {}                       # failed jobs are not re-run...
    two.cfg.rsync.path = "rsync"
    assert jobs.retry_job(two.conn, rep.job_id) == 1
    assert run(two)[rep.job_id].status == "done"  # ...until retried


def test_verification_failure_is_transient(two, monkeypatch):
    def corrupt(cfg, src_root, dst_root, rel_paths, progress=None, remote=None):
        for rel in rel_paths:
            write(dst_root / rel, "corrupted!")
        return transfer.BatchResult(0, "")

    monkeypatch.setattr(transfer, "run_batch", corrupt)
    rep = jobs.create_transfer_job(two.conn, ["/docs/a.txt"], None, "ssd")
    r = run(two)[rep.job_id]
    assert r.retry == 1 and "verification failed" in r.messages[0][1]
    assert "ssd" not in node_copies(two, "/docs/a.txt")


def test_disk_full_waits(two, monkeypatch):
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(runner.shutil, "disk_usage", lambda p: Usage(10, 10, 0))
    rep = jobs.create_transfer_job(two.conn, ["/docs"], None, "ssd")
    r = run(two)[rep.job_id]
    assert (r.waiting, r.status) == (2, "waiting")
    assert "free space" in items(two, rep.job_id)[0]["message"]
    assert items(two, rep.job_id)[0]["attempts"] == 0


def test_source_changed_since_scan_is_reindexed(two):
    write(two.root / "docs" / "a.txt", "alpha v2", mtime=2_000_000)
    rep = jobs.create_transfer_job(two.conn, ["/docs/a.txt"], None, "ssd")
    run(two)
    assert (two.ssd / "docs" / "a.txt").read_text() == "alpha v2"
    c = node_copies(two, "/docs/a.txt")
    assert c["srv"].hash == c["ssd"].hash and c["ssd"].current


def test_cancel(two):
    rep = jobs.create_transfer_job(two.conn, ["/docs"], None, "ssd")
    assert jobs.cancel_job(two.conn, rep.job_id) == 2
    assert run(two) == {}
    with pytest.raises(jobs.JobError):
        jobs.cancel_job(two.conn, rep.job_id)


def test_wanted_restore(two):
    jobs.create_transfer_job(two.conn, ["/docs/a.txt"], None, "ssd")
    run(two)
    (two.ssd / "docs" / "a.txt").unlink()
    two.scan()
    [d] = two.decisions("missing")
    decisions.resolve(two.conn, d["id"], "restore")
    rep = jobs.approve_wanted(two.conn, None)
    assert rep.items == 1
    assert run(two)[rep.job_id].done == 1
    assert (two.ssd / "docs" / "a.txt").read_text() == "alpha"
    assert two.conn.execute("SELECT status FROM wanted").fetchone()[0] == "done"


def test_wanted_overwrite_stale(two):
    jobs.create_transfer_job(two.conn, ["/docs/a.txt"], None, "ssd")
    run(two)
    write(two.root / "docs" / "a.txt", "alpha newer", mtime=3_000_000)
    two.scan()
    [d] = two.decisions("divergent")
    decisions.resolve(two.conn, d["id"], "update")
    rep = jobs.approve_wanted(two.conn, None)
    assert run(two)[rep.job_id].done == 1
    assert (two.ssd / "docs" / "a.txt").read_text() == "alpha newer"
    assert two.decisions("divergent") == []


def test_wanted_rename_on_other_node(two):
    jobs.create_transfer_job(two.conn, ["/docs/a.txt"], None, "ssd")
    run(two)
    tags.set_tag(two.conn, tags.resolve_targets(two.conn, "/docs/a.txt", False), "t", True,
                 two.server_id)
    (two.root / "renamed").mkdir()
    (two.root / "docs" / "a.txt").rename(two.root / "renamed" / "a2.txt")
    two.scan()
    [d] = two.decisions("rename")
    decisions.resolve(two.conn, d["id"], "apply")
    rep = jobs.approve_wanted(two.conn, None)
    r = run(two)[rep.job_id]
    assert r.done == 1
    assert (two.ssd / "renamed" / "a2.txt").read_text() == "alpha"
    assert not (two.ssd / "docs").exists()          # old directory pruned
    assert queries.lookup(two.conn, "/docs/a.txt") is None
    info = queries.lookup(two.conn, "/renamed/a2.txt")
    assert info.good_copies == 2 and info.tags == ["t"]


def test_rename_never_replaces(two):
    jobs.create_transfer_job(two.conn, ["/docs/a.txt"], None, "ssd")
    run(two)
    (two.root / "docs" / "a.txt").rename(two.root / "docs" / "z.txt")
    two.scan()
    [d] = two.decisions("rename")
    decisions.resolve(two.conn, d["id"], "apply")
    write(two.ssd / "docs" / "z.txt", "precious")
    rep = jobs.approve_wanted(two.conn, None)
    r = run(two)[rep.job_id]
    assert r.skipped == 1
    assert (two.ssd / "docs" / "z.txt").read_text() == "precious"
    assert (two.ssd / "docs" / "a.txt").exists()


def test_reject_wanted(two):
    jobs.create_transfer_job(two.conn, ["/docs/a.txt"], None, "ssd")
    run(two)
    (two.ssd / "docs" / "a.txt").unlink()
    two.scan()
    [d] = two.decisions("missing")
    decisions.resolve(two.conn, d["id"], "restore")
    assert jobs.reject_wanted(two.conn, None) == 1
    assert jobs.approve_wanted(two.conn, None).job_id is None


@pytest.mark.parametrize("style,path,expected", [
    ("native", "/home/me/x", "/home/me/x"),
    ("cygwin", "C:\\Users\\me\\rei", "/cygdrive/c/Users/me/rei"),
    ("msys", "D:\\data", "/d/data"),
    ("cygwin", "\\\\nas\\share\\x", "//nas/share/x"),
])
def test_rsync_path(style, path, expected, monkeypatch):
    if "\\" in path:
        import ntpath
        monkeypatch.setattr(transfer.os, "path", ntpath)
    assert transfer.rsync_path(path, style) == expected


def test_server_lock(tmp_path):
    with ServerLock(tmp_path):
        with pytest.raises(LockError):
            ServerLock(tmp_path).acquire()
    with ServerLock(tmp_path):
        pass


def test_migration_from_v1(tmp_path):
    path = tmp_path / "old.db"
    conn = db.connect(path)
    conn.executescript(f"BEGIN;\n{db.SCHEMA}\nPRAGMA user_version = 1;\nCOMMIT;")
    conn.close()
    conn = db.open_existing(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.execute("SELECT * FROM job_items")
