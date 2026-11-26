"""Opaque directories as one row (design section 5.22).

An opaque directory is a single entry of the index, as if it were one file: one ``files`` row
at its own path and one ``copies`` row per node that holds it, so that everything that works on
files (copies and backups, moves and renames, versions, tags, schedules, "synchronous",
fetching, marking for deletion) works on the whole directory without a row for each of its
files. What stands in for a file's size, modification time and hash:

* size: the total size of the files inside; mtime: the newest modification time inside;
* hash: a *fingerprint* of the whole tree: its names, sizes and modification times (to two
  seconds, which is what FAT keeps), read with a ``stat`` each and no file opened. The entries
  are combined in an order-independent way, so it takes no memory however many there are.

How many files a directory holds is not part of the schema of files (almost every row there is a
file): it lives in ``collection_stats``, per node.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import opaque
from .control import StopFn, check

MASK = (1 << 64) - 1
HALF = 2_000_000_000          # modification times are compared to two seconds


@dataclass
class Fingerprint:
    files: int
    bytes: int
    newest_mtime_ns: int
    hash: str


@dataclass
class Described:
    """What a path is to the index: a file, or a directory standing for all of its files."""
    size: int
    mtime_ns: int
    hash: str | None
    files: int | None = None      # only for a directory


def fingerprint(path: Path | str, stop: StopFn | None = None, progress=None) -> Fingerprint:
    files = nbytes = newest = entries = 0
    total = xor = 0
    stack = [(str(path), "")]
    while stack:
        check(stop)
        directory, rel = stack.pop()
        try:
            it = os.scandir(directory)
        except OSError:
            continue                      # a directory we cannot read counts for nothing
        with it:
            for e in it:
                r = f"{rel}/{e.name}" if rel else e.name
                try:
                    if e.is_symlink():
                        token = f"l\0{r}\0{os.readlink(e.path)}"
                    elif e.is_dir(follow_symlinks=False):
                        stack.append((e.path, r))
                        token = f"d\0{r}"
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        files += 1
                        nbytes += st.st_size
                        newest = max(newest, st.st_mtime_ns)
                        token = f"f\0{r}\0{st.st_size}\0{st.st_mtime_ns // HALF}"
                    else:
                        continue
                except OSError:
                    continue
                h = int.from_bytes(hashlib.blake2b(token.encode("utf-8", "surrogatepass"),
                                                   digest_size=8).digest(), "big")
                total = (total + h) & MASK
                xor ^= h
                entries += 1
        if progress and entries % 5000 < 64:
            progress(files)
    digest = hashlib.blake2b(f"{entries}\0{files}\0{nbytes}\0{total}\0{xor}".encode(),
                             digest_size=32).hexdigest()
    return Fingerprint(files, nbytes, newest, "blake2b:" + digest)


def describe(path: Path | str, collection: bool, with_hash: bool = True,
             stop: StopFn | None = None) -> Described:
    """The size, modification time and hash the index keeps for what is at ``path``."""
    from .hashing import hash_file

    if collection:
        fp = fingerprint(path, stop)
        return Described(fp.bytes, fp.newest_mtime_ns, fp.hash, fp.files)
    st = os.stat(path)
    return Described(st.st_size, st.st_mtime_ns, hash_file(path) if with_hash else None)


def is_collection(conn: sqlite3.Connection, vpath: str) -> bool:
    """Is this path an opaque directory (one row standing for all that is in it)?"""
    on, src = opaque.source(conn, vpath)
    return on and src == vpath


def record_stats(conn: sqlite3.Connection, fid: int, node_id: str, files: int, nbytes: int) -> None:
    conn.execute("INSERT OR REPLACE INTO collection_stats(file_id, node_id, files, bytes)"
                 " VALUES (?, ?, ?, ?)", (fid, node_id, files, nbytes))


def file_count(conn: sqlite3.Connection, fid: int) -> int | None:
    """How many files the directory holds (the largest figure of the nodes that hold it)."""
    r = conn.execute("SELECT MAX(files) FROM collection_stats WHERE file_id = ?", (fid,)).fetchone()
    return r[0]
