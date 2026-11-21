"""The audit of a node, and jobs that must not claim to be done when they are not."""

import os
import shutil
import sys

import pytest

from reifs import audit, index, jobs, runner

from conftest import write

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


def test_audit_explains_what_is_not_indexed(env):
    write(env.root / "ok.txt", "ok", mtime=1_000_000)
    write(env.root / "gone.txt", "gone", mtime=1_000_000)
    env.scan()
    os.remove(env.root / "gone.txt")
    write(env.root / "new.txt", "new", mtime=1_000_000)
    write(env.root / "Thumbs.db", "x")
    write(env.root / ".hidden" / "a.txt", "h")
    write(env.root / ".hidden" / "b.txt", "h")
    if sys.platform != "win32":
        os.symlink(env.root / "ok.txt", env.root / "link")
    out = audit.audit_node(env.conn, env.cfg, index.self_id(env.conn), env.root)
    reasons = {i["reason"]: i["count"] for i in out["not_in_index"]}
    assert reasons["not scanned yet (new or recently changed)"] == 1
    assert reasons["ignored by the [scan] ignore list"] == 1
    assert reasons["inside a hidden directory (name starts with '.')"] == 2
    if sys.platform != "win32":
        assert reasons["symbolic link (not followed)"] == 1
    assert out["listed_but_not_on_disk"] == {"count": 1, "examples": ["/gone.txt"]}


def test_a_job_with_files_it_could_not_copy_is_not_done(env):
    _, ssd = env.add_node("ssd")
    write(env.root / "docs" / "a.txt", "alpha", mtime=1_000_000)
    write(env.root / "docs" / "b.txt", "beta", mtime=1_000_000)
    env.scan()
    # b.txt is on the source node only on paper: its file vanished after the scan.
    os.remove(env.root / "docs" / "b.txt")
    rep = jobs.create_transfer_job(env.conn, ["/docs"], None, "ssd")
    r = runner.run_jobs(env.conn, env.cfg, [rep.job_id])[0]
    assert r.done == 1
    assert jobs.get_job(env.conn, rep.job_id)["status"] != "done"
    assert (ssd / "docs" / "a.txt").exists()


def test_files_that_disappear_after_the_job_are_noticed(env, monkeypatch):
    _, ssd = env.add_node("ssd")
    write(env.root / "a.txt", "alpha", mtime=1_000_000)
    write(env.root / "b.txt", "beta", mtime=1_000_000)
    env.scan()
    rep = jobs.create_transfer_job(env.conn, ["/a.txt", "/b.txt"], None, "ssd")
    real = runner._Runner._verify_done

    def vanish_then_verify(self, job_id, report):
        os.remove(ssd / "b.txt")          # something removes it while the job was running
        real(self, job_id, report)

    monkeypatch.setattr(runner._Runner, "_verify_done", vanish_then_verify)
    r = runner.run_jobs(env.conn, env.cfg, [rep.job_id])[0]
    assert r.done == 1 and r.status == "waiting"
    assert any("missing after the job finished" in m for _, m in r.messages)
    monkeypatch.undo()
    r = runner.run_jobs(env.conn, env.cfg, [rep.job_id])[0]       # and it is copied again
    assert (ssd / "b.txt").read_text() == "beta"
    assert jobs.get_job(env.conn, rep.job_id)["status"] == "done"


def test_left_out_files_are_kept_in_the_job_as_skipped(env):
    _, ssd = env.add_node("ssd")
    write(env.root / "docs" / "a.txt", "alpha", mtime=1_000_000)
    env.scan()
    os.remove(env.root / "docs" / "a.txt")
    env.scan()                           # now missing everywhere: nothing to copy from
    rep = jobs.create_transfer_job(env.conn, ["/docs"], None, "ssd")
    assert rep.items == 0 and rep.job_id is not None
    job = jobs.get_job(env.conn, rep.job_id)
    assert job["status"] == "failed"
    (item,) = env.conn.execute("SELECT state, message FROM job_items WHERE job_id = ?",
                               (rep.job_id,)).fetchall()
    assert item["state"] == "skipped" and "no present copy anywhere" in item["message"]
