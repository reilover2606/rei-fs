"""``reifs db speed``: how fast is the disk the database is on, for what rei-fs asks of it?

Big jobs (a reset, a first scan, forgetting a node) change rows of tables that have indexes
ordered by a hash: every row touches pages all over the file. With the file in memory that is
nothing; on a spinning disk or network storage each one can be a seek, and a few hundred per
second is all there is. This measures that, on the disk of the database itself, without
touching the database: the reads are of its file, the writes of a scratch database next to it.
"""

from __future__ import annotations

import os
import random
import sqlite3
import time
from pathlib import Path


def _pread(fd: int, n: int, offset: int) -> bytes:
    """``os.pread``, which Windows does not have."""
    if hasattr(os, "pread"):
        return os.pread(fd, n, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, n)


def _drop_cache(fd: int) -> None:
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except (AttributeError, OSError):
        pass


def disk(path: Path, seconds: float = 4.0) -> dict:
    """Random 4 KiB reads per second and sequential read speed of ``path`` (or, if it is small,
    a scratch file beside it)."""
    scratch = None
    if path.stat().st_size < 256 << 20:
        scratch = path.with_name("speedtest.tmp")
        with open(scratch, "wb") as f:
            block = os.urandom(1 << 20)
            for _ in range(256):
                f.write(block)
            f.flush()
            os.fsync(f.fileno())
        path = scratch
    try:
        size = path.stat().st_size
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            _drop_cache(fd)
            n, end = 0, time.monotonic() + seconds
            while time.monotonic() < end:
                _pread(fd, 4096, random.randrange(0, size - 4096) & ~4095)
                n += 1
            iops = n / seconds
            _drop_cache(fd)
            t = time.monotonic()
            got = 0
            while got < 128 << 20 and time.monotonic() - t < seconds * 2:
                data = _pread(fd, 1 << 20, got % max(size - (1 << 20), 1))
                if not data:
                    break
                got += len(data)
            mbs = got / (time.monotonic() - t) / 1e6
        finally:
            os.close(fd)
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)
    return {"random_reads_per_s": iops, "sequential_mb_s": mbs}


def sqlite_deletes(folder: Path, rows: int, cache_mb: int | None, drop_hash_index: bool = False) -> dict:
    """Insert ``rows`` rows of a table shaped like ``copies`` (a key, and two indexes, one of them
    ordered by a hash), 500 per transaction, in a scratch database in ``folder``; then, with the
    file cache of the operating system emptied (a database bigger than memory is read from the
    disk), delete a part of them 500 at a time. ``drop_hash_index``: without the index that is
    ordered by the hash (what a big reset now does)."""
    path = folder / "speedtest.db"

    def clean() -> None:
        for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            p.unlink(missing_ok=True)

    def connect() -> sqlite3.Connection:
        c = sqlite3.connect(path, isolation_level=None)
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA synchronous = NORMAL")
        if cache_mb is not None:
            c.execute(f"PRAGMA cache_size = -{cache_mb * 1024}")
        return c

    clean()
    conn = connect()
    try:
        conn.executescript("""CREATE TABLE c(file_id INTEGER, node TEXT, size INTEGER, hash BLOB,
            PRIMARY KEY (file_id, node)) WITHOUT ROWID;
            CREATE INDEX c_hash ON c(hash, size); CREATE INDEX c_node ON c(node, size);""")
        t = time.monotonic()
        for start in range(0, rows, 1000):
            conn.execute("BEGIN")
            conn.executemany("INSERT INTO c VALUES (?, 'n', ?, ?)",
                             [(i, random.randrange(1 << 30), os.urandom(32))
                              for i in range(start, min(start + 1000, rows))])
            conn.execute("COMMIT")
        ins = rows / (time.monotonic() - t)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        size = path.stat().st_size
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            os.fsync(fd)
            _drop_cache(fd)                 # what a big database is: not in memory
        finally:
            os.close(fd)
        conn = connect()
        if drop_hash_index:
            conn.execute("DROP INDEX c_hash")
        part = min(rows, 20_000)
        t = time.monotonic()
        done = 0
        while done < part:
            conn.execute("BEGIN")
            n = conn.execute("DELETE FROM c WHERE file_id IN (SELECT file_id FROM c WHERE node = 'n'"
                             " LIMIT 500)").rowcount
            conn.execute("COMMIT")
            done += n
            if n < 500:
                break
        return {"insert_per_s": ins, "delete_per_s": done / (time.monotonic() - t),
                "size_mb": size / 1e6}
    finally:
        conn.close()
        clean()


def run(db_path: Path, quick: bool = False, out=print, cache_mb: int = 128) -> None:
    folder = db_path.parent
    out(f"Testing the disk of {folder} ({'quick' if quick else 'a minute or two'})...")
    d = disk(db_path, 2.0 if quick else 4.0)
    out(f"  random 4 KiB reads: {d['random_reads_per_s']:,.0f} per second"
        f"  (a spinning disk gives 100-200 over a big file; this reads a {'database' if db_path.stat().st_size >= 256 << 20 else '256 MB scratch'} file)")
    out(f"  sequential reads:   {d['sequential_mb_s']:,.0f} MB/s")
    rows = 60_000 if quick else 300_000
    a = sqlite_deletes(folder, rows, None)
    b = sqlite_deletes(folder, rows, cache_mb)
    c = sqlite_deletes(folder, rows, cache_mb, drop_hash_index=True)
    out(f"  SQLite, {rows:,} rows in a table like the copies ({a['size_mb']:.0f} MB), deleting with the "
        "file cache emptied:")
    out(f"    default cache (2 MB):                  {a['delete_per_s']:>9,.0f} rows/s")
    out(f"    {cache_mb} MB cache:                        {b['delete_per_s']:>9,.0f} rows/s")
    out(f"    {cache_mb} MB cache, hash index dropped:    {c['delete_per_s']:>9,.0f} rows/s"
        "   (what a big reset does now)")
    out("")
    for label, rate in (("as it was", a["delete_per_s"]), ("with the cache", b["delete_per_s"]),
                        ("without the hash index", c["delete_per_s"])):
        out(f"  forgetting 2,000,000 copies, {label}: about {2_000_000 / max(rate, 1) / 3600:.1f} hours")
    out("(a database much bigger than this test is slower still: more of it is out of the caches)")
