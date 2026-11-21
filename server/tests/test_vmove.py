"""Moving files and directories in the virtual tree (vmove)."""

import os
import shutil

import pytest

from reifs import index, jobs, queries, runner, tags, vmove

from conftest import write

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


def move(env, sources, dest, name=None, mode="merge"):
    plan = vmove.plan_move(env.conn, sources, dest, name, mode)
    res = vmove.apply_plan(env.conn, plan, "test move")
    reports = runner.run_jobs(env.conn, env.cfg) if res.job_id else []
    env.scan()
    return plan, res, reports


def vpaths(env):
    return sorted(r[0] for r in env.conn.execute("SELECT vpath FROM files"))


def on_disk(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and ".rei-fs" not in p.parts)


@pytest.fixture
def two(env):
    """Files on the server node, all of them also copied to 'ssd'."""
    _, ssd = env.add_node("ssd")
    write(env.root / "docs" / "a.txt", "alpha", mtime=1_000_000)
    write(env.root / "docs" / "deep" / "b.txt", "beta", mtime=1_000_000)
    write(env.root / "other" / "c.txt", "gamma", mtime=1_000_000)
    env.scan()
    rep = jobs.create_transfer_job(env.conn, ["/docs", "/other"], None, "ssd")
    runner.run_jobs(env.conn, env.cfg, [rep.job_id])
    env.ssd = ssd
    return env


def test_move_a_file_renames_it_on_every_node(two):
    tags.set_tag(two.conn, [index.file_id(two.conn, "/docs/a.txt")], "keep", True, index.self_id(two.conn))
    plan, res, reports = move(two, ["/docs/a.txt"], "/new/place")
    assert reports[0].status == "done" and res.items == 2
    assert "/new/place/a.txt" in vpaths(two) and "/docs/a.txt" not in vpaths(two)
    for root in (two.root, two.ssd):
        assert "new/place/a.txt" in on_disk(root) and "docs/a.txt" not in on_disk(root)
    info = queries.lookup(two.conn, "/new/place/a.txt")
    assert {c.node_name for c in info.copies} >= {"ssd"} and info.good_copies == 2
    assert tags.tags_of(two.conn, index.file_id(two.conn, "/new/place/a.txt")) == ["keep"]


def test_move_a_directory_and_rename_it(two):
    move(two, ["/docs"], "/", name="papers")
    assert vpaths(two) == ["/other/c.txt", "/papers/a.txt", "/papers/deep/b.txt"]
    for root in (two.root, two.ssd):
        assert on_disk(root) == ["other/c.txt", "papers/a.txt", "papers/deep/b.txt"]
        assert not (root / "docs").exists()  # emptied directories are removed


def test_merging_directories_combines_their_contents(two):
    write(two.root / "other" / "deep" / "x.txt", "extra", mtime=1_000_000)
    two.scan()
    plan, _, _ = move(two, ["/docs"], "/other")  # /other/docs
    plan, res, reports = move(two, ["/other/docs/deep"], "/other", mode="merge")
    assert plan.conflicts == ["/other/deep"] and plan.renamed == []
    assert "/other/deep/b.txt" in vpaths(two) and "/other/deep/x.txt" in vpaths(two)


def test_preserve_keeps_both_directories(two):
    write(two.root / "other" / "docs" / "a.txt", "different", mtime=1_000_000)
    two.scan()
    plan, _, _ = move(two, ["/docs"], "/other", mode="preserve")
    assert plan.renamed == [{"wanted": "docs", "given": "docs (2)"}] or \
        plan.renamed == [("docs", "docs (2)")]
    got = vpaths(two)
    assert "/other/docs/a.txt" in got and "/other/docs (2)/a.txt" in got
    assert "/other/docs (2)/deep/b.txt" in got and not any(v.startswith("/docs/") for v in got)


def test_merging_identical_files_marks_the_duplicate_for_deletion(two):
    write(two.root / "other" / "a.txt", "alpha", mtime=1_000_000)  # same content as /docs/a.txt
    two.scan()
    jobs_rep = jobs.create_transfer_job(two.conn, ["/other/a.txt"], None, "ssd")
    runner.run_jobs(two.conn, two.cfg, [jobs_rep.job_id])
    plan, res, _ = move(two, ["/docs/a.txt"], "/other", mode="merge")
    assert plan.merged == 1 and plan.duplicates == 2 and res.job_id is None
    marked = {(r["vpath"], r["node_name"]) for r in jobs.marked_copies(two.conn)}
    assert {v for v, _ in marked} == {"/docs/a.txt"} and len(marked) == 2
    assert (two.root / "docs" / "a.txt").exists()  # nothing was deleted


def test_a_node_holding_both_different_files_keeps_both(two):
    write(two.root / "other" / "a.txt", "newer alpha", mtime=2_000_000)  # only on the server node
    two.scan()
    plan, _, _ = move(two, ["/docs/a.txt"], "/other", mode="merge")
    assert plan.merged == 0
    assert "/other/a (2).txt" in vpaths(two) and "/other/a.txt" in vpaths(two)


def test_merging_different_content_makes_one_file_with_two_versions(two):
    write(two.root / "docs" / "z.txt", "yy", mtime=1_000_000)       # server node only
    write(two.ssd / "other" / "z.txt", "zz", mtime=2_000_000)       # ssd only
    two.scan()
    plan, res, _ = move(two, ["/docs/z.txt"], "/other", mode="merge")
    assert plan.merged == 1 and plan.duplicates == 0
    assert "/docs/z.txt" not in vpaths(two) and "/other/z (2).txt" not in vpaths(two)
    info = queries.lookup(two.conn, "/other/z.txt")
    assert len(info.copies) == 2 and len({c.hash for c in info.copies}) == 2
    assert len(two.decisions("divergent")) == 1


def test_preserve_gives_files_a_free_name(two):
    write(two.root / "other" / "a.txt", "different", mtime=1_000_000)
    two.scan()
    plan, _, _ = move(two, ["/docs/a.txt"], "/other", mode="preserve")
    assert "/other/a.txt" in vpaths(two) and "/other/a (2).txt" in vpaths(two)


def test_errors(two):
    with pytest.raises(vmove.MoveError):
        vmove.plan_move(two.conn, ["/docs"], "/docs/deep")
    with pytest.raises(vmove.MoveError):
        vmove.plan_move(two.conn, ["/"], "/x")
    with pytest.raises(vmove.MoveError):
        vmove.plan_move(two.conn, ["/nope"], "/x")
    with pytest.raises(vmove.MoveError):
        vmove.plan_move(two.conn, ["/docs", "/other"], "/x", name="y")
    with pytest.raises(vmove.MoveError):
        vmove.plan_move(two.conn, ["/docs"], "/x", name="a/b")
    with pytest.raises(vmove.MoveError):
        vmove.plan_move(two.conn, ["/docs/deep"], "/other/c.txt")  # a file is no directory
    assert vmove.plan_move(two.conn, ["/docs"], "/").files == 0  # already there
