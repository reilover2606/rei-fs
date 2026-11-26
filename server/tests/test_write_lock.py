import sqlite3
import threading
import time

import pytest

from reifs import db


def test_writers_are_served_in_order_and_interactive_ones_first(tmp_path):
    path = tmp_path / "x.db"
    db.create(path).close()
    order = []
    release = threading.Event()

    def holder():
        c = db.open_existing(path)
        with db.transaction(c, "holder"):
            release.wait(5)

    def writer(name, label, delay):
        time.sleep(delay)
        c = db.open_existing(path)
        with db.transaction(c, label):
            order.append(name)
        c.close()

    t0 = threading.Thread(target=holder)
    t0.start()
    time.sleep(0.2)
    ts = [threading.Thread(target=writer, args=("bg1", "scan", 0.0)),
          threading.Thread(target=writer, args=("bg2", "scan", 0.1)),
          threading.Thread(target=writer, args=("api", "POST /api/x", 0.3))]
    for t in ts:
        t.start()
    time.sleep(0.6)
    release.set()
    for t in [t0, *ts]:
        t.join()
    assert order == ["api", "bg1", "bg2"]          # the person waiting for an answer, then first come


def test_a_second_connection_of_the_same_thread_is_not_a_deadlock(tmp_path):
    path = tmp_path / "x.db"
    db.create(path).close()
    a, b = db.open_existing(path), db.connect(path, timeout=0.2)
    with db.transaction(a):
        with pytest.raises(sqlite3.OperationalError):      # SQLite says busy, as ever
            with db.transaction(b):
                pass


def test_an_interrupted_waiter_does_not_block_the_others(tmp_path):
    lock = db._FairLock()
    assert lock.acquire(1)
    errors = []

    def waiter():
        try:
            lock.acquire(1)
        except KeyboardInterrupt:
            errors.append("interrupted")

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.2)
    # simulate the interrupt: raise inside the waiting thread's wait
    import ctypes
    ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(t.ident), ctypes.py_object(KeyboardInterrupt))
    with lock.cond:
        lock.cond.notify_all()
    t.join(2)
    assert not lock.queue                                   # it left the queue
    lock.release()
    assert lock.acquire(1)                                  # and the lock is not stuck


def test_free_room_is_given_back_only_when_it_is_worth_it(tmp_path):
    c = db.create(tmp_path / "x.db")
    assert not db.reclaim_due(c, 10, 0)                     # nothing is free
    c.executescript("CREATE TABLE junk(x); BEGIN;")
    c.executemany("INSERT INTO junk VALUES (?)", [("x" * 500,)] * 4000)
    c.execute("COMMIT")
    c.execute("DELETE FROM junk")
    assert db.reclaim_due(c, 10, 0) and not db.reclaim_due(c, 10, 10 ** 9)   # big enough, or not
    seen = []
    c.set_trace_callback(lambda s: seen.append(s) if "incremental_vacuum" in s else None)
    assert db.reclaim(c, chunk=100) > 0 and len(seen) > 2                  # in steps
    assert c.execute("PRAGMA freelist_count").fetchone()[0] == 0
    assert db.lock_stats() and any("free space back" in r["who"] for r in db.lock_stats())


def test_the_speed_test_runs_and_leaves_nothing_behind(tmp_path):
    from reifs import speedtest
    (tmp_path / "x.db").write_bytes(b"0" * 5000)
    lines = []
    speedtest.run(tmp_path / "x.db", quick=True, out=lines.append, cache_mb=8)
    text = "\n".join(lines)
    assert "random 4 KiB reads" in text and "delete" in text and "Verdict" in text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.db"]
