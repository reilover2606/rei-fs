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
        fd = os.open(path, os.O_RDONLY)
        try:
            _drop_cache(fd)
            n, end = 0, time.monotonic() + seconds
            while time.monotonic() < end:
                os.pread(fd, 4096, random.randrange(0, size - 4096) & ~4095)
                n += 1
            iops = n / seconds
            _drop_cache(fd)
            t = time.monotonic()
            got = 0
            while got < 128 << 20 and time.monotonic() - t < seconds * 2:
                data = os.pread(fd, 1 << 20, got % max(size - (1 << 20), 1))
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


def sqlite_deletes(folder: Path, rows: int, cache_mb: int | None) -> dict:
    """Insert then delete ``rows`` rows of a table shaped like ``copies`` (a key, and two indexes
    one of them ordered by a hash), 500 per transaction, in a scratch database in ``folder``."""
    path = folder / "speedtest.db"
    for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        p.unlink(missing_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        if cache_mb is not None:
            conn.execute(f"PRAGMA cache_size = -{cache_mb * 1024}")
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
        t = time.monotonic()
        while True:
            conn.execute("BEGIN")
            n = conn.execute("DELETE FROM c WHERE file_id IN (SELECT file_id FROM c WHERE node = 'n'"
                             " LIMIT 500)").rowcount
            conn.execute("COMMIT")
            if n < 500:
                break
        dele = rows / (time.monotonic() - t)
        return {"insert_per_s": ins, "delete_per_s": dele, "size_mb": path.stat().st_size / 1e6}
    finally:
        conn.close()
        for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            p.unlink(missing_ok=True)


def run(db_path: Path, quick: bool = False, out=print, cache_mb: int = 128) -> None:
    folder = db_path.parent
    out(f"Testing the disk of {folder} ({'quick' if quick else 'about a minute'})...")
    d = disk(db_path, 2.0 if quick else 4.0)
    out(f"  random 4 KiB reads: {d['random_reads_per_s']:,.0f} per second")
    out(f"  sequential reads:   {d['sequential_mb_s']:,.0f} MB/s")
    rows = 40_000 if quick else 200_000
    a = sqlite_deletes(folder, rows, None)
    b = sqlite_deletes(folder, rows, cache_mb)
    out(f"  SQLite, {rows:,} rows in a table like the copies (database {a['size_mb']:.0f} MB):")
    out(f"    default cache (2 MB):     insert {a['insert_per_s']:,.0f}/s, delete {a['delete_per_s']:,.0f}/s")
    out(f"    {cache_mb} MB cache:           insert {b['insert_per_s']:,.0f}/s, delete {b['delete_per_s']:,.0f}/s")
    out("")
    iops = d["random_reads_per_s"]
    if iops < 1000:
        out(f"Verdict: slow for this work ({iops:,.0f} random reads/s is a spinning disk or network "
            "storage). Forgetting or changing rows of a database bigger than the cache costs a seek or "
            "two for each: expect the speed of the 'delete' line above at best, and less on a big "
            "database. The cache helps (see [database] cache_mb) and so does keeping the database on an "
            "SSD (the database folder is the server's home folder).")
    elif iops < 10000:
        out(f"Verdict: middling ({iops:,.0f} random reads/s, a SATA SSD or a busy disk). A bigger "
            "cache helps for databases larger than memory.")
    else:
        out(f"Verdict: fast ({iops:,.0f} random reads/s): the disk is not what makes big jobs slow.")
