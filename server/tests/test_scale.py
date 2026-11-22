"""Behaviour that keeps a very large index usable: bounded planning, cached figures, jumps."""

import os
import time

import pytest

from reifs import db, queries, replication

from conftest import write


def test_planning_is_done_in_pieces(env):
    env.add_node("ssd")
    env.conn.execute("UPDATE nodes SET replication_target = 1 WHERE name = 'ssd'")
    for i in range(10):
        write(env.root / "d" / f"f{i}.txt", str(i), mtime=1_000_000)
    env.scan()
    env.cfg.replication.max_proposals = 4
    rep = replication.plan(env.conn, env.cfg)
    assert (rep.proposed, rep.truncated, rep.under_replicated) == (4, True, 10)
    again = replication.plan(env.conn, env.cfg)           # at the limit: nothing more until decided
    assert again.proposed == 0 and again.truncated and again.open_proposals == 4
    env.cfg.replication.max_proposals = 100
    rest = replication.plan(env.conn, env.cfg)
    assert rest.proposed == 6 and not rest.truncated        # the four are not proposed twice


def test_a_listing_stops_at_its_limit_and_says_so(env, monkeypatch):
    for i in range(7):
        write(env.root / "big" / f"f{i}.txt", str(i), mtime=1_000_000)
    write(env.root / "big" / "sub" / "x.txt", "x", mtime=1_000_000)
    env.scan()
    monkeypatch.setattr(queries, "MAX_FILES_LISTED", 3)
    out = queries.list_dir(env.conn, "/big")
    assert [e.name for e in out if e.is_dir] == ["sub"]
    assert len([e for e in out if not e.is_dir]) == 3 and out.more == 4


def test_a_big_database_answers_with_figures_that_are_a_little_old(env, monkeypatch):
    write(env.root / "a.txt", "a", mtime=1_000_000)
    env.scan()
    s1 = queries.status(env.conn)
    assert not s1.coverage_stale and s1.files == 1
    monkeypatch.setattr(queries, "FAST_SECONDS", -1.0)     # "this database is big"
    monkeypatch.setattr(queries, "MIN_REFRESH_SECONDS", 0.0)
    queries._stats_cache[queries._db_file(env.conn)].seconds = 0.0001
    write(env.root / "b.txt", "b", mtime=1_000_000)
    env.scan()
    s2 = queries.status(env.conn)
    assert s2.coverage_stale and s2.satisfied + s2.under_replicated == 1   # the old figures
    for _ in range(100):                                    # the background refresh catches up
        time.sleep(0.05)
        s3 = queries.status(env.conn)
        if not s3.coverage_stale:
            break
    assert s3.satisfied + s3.under_replicated == 2


def test_name_search_with_wildcards_and_classes(env):
    for n in ("alpha.txt", "beta.txt", "gamma.jpg", "ab1.dat", "ac2.dat", "ad3.dat"):
        write(env.root / "s" / n, n, mtime=1_000_000)
    env.scan()

    def names(pat):
        ids, _ = queries.search(env.conn, pat)
        return sorted(os.path.basename(queries.file_info(env.conn, i).vpath) for i in ids)

    assert names("*.txt") == ["alpha.txt", "beta.txt"]
    assert names("a[bc]?.dat") == ["ab1.dat", "ac2.dat"]
    assert names("a[!bc]?.dat") == ["ad3.dat"]
    assert names("MMA") == ["gamma.jpg"]                    # contains, ignoring case
    assert names("*") == sorted(["alpha.txt", "beta.txt", "gamma.jpg", "ab1.dat", "ac2.dat", "ad3.dat"])


def test_sqlite_temporary_files_go_next_to_the_database(tmp_path, monkeypatch):
    monkeypatch.delenv("SQLITE_TMPDIR", raising=False)
    db.create(tmp_path / "home" / "reifs.db") if (tmp_path / "home").mkdir() is None else None
    assert os.environ["SQLITE_TMPDIR"] == str(tmp_path / "home" / "tmp")
    monkeypatch.delenv("SQLITE_TMPDIR")


def test_approving_many_proposals_uses_short_transactions(env, monkeypatch):
    from reifs import jobs
    env.add_node("ssd")
    env.conn.execute("UPDATE nodes SET replication_target = 1 WHERE name = 'ssd'")
    for i in range(7):
        write(env.root / "d" / f"f{i}.txt", str(i), mtime=1_000_000)
    env.scan()
    replication.plan(env.conn, env.cfg)
    monkeypatch.setattr(jobs, "APPROVE_CHUNK", 3)
    opened = []
    real = db.transaction.__enter__
    monkeypatch.setattr(db.transaction, "__enter__",
                        lambda self: (opened.append(1), real(self))[1])
    res = replication.approve(env.conn, None)
    assert res.items == 7 and len(opened) >= 3                 # 7 proposals, chunks of 3
    assert env.conn.execute("SELECT COUNT(*) FROM job_items WHERE job_id = ?",
                            (res.local_job,)).fetchone()[0] == 7
    assert env.conn.execute("SELECT COUNT(*) FROM wanted WHERE status = 'approved'").fetchone()[0] == 7
