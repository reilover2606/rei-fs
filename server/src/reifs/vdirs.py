"""Directories made by the user in the virtual tree (design section 5.6).

A directory normally exists only as the prefix of file paths. An *explicit*
directory is recorded in the index (and replicated like a tag), so it stays
when it is empty. It is purely virtual: nothing is created on any node.
"""

from __future__ import annotations

import sqlite3

from . import db, index, paths


class VDirError(Exception):
    pass


def _set(conn: sqlite3.Connection, vpath: str, present: bool) -> None:
    conn.execute(
        "INSERT INTO vdirs(vpath, present, updated, updated_by) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(vpath) DO UPDATE SET present = excluded.present,"
        " updated = excluded.updated, updated_by = excluded.updated_by",
        (vpath, int(present), db.now(), index.self_id(conn)))
    index.log_vdir(conn, vpath)


def is_file(conn: sqlite3.Connection, vpath: str) -> bool:
    return conn.execute("SELECT 1 FROM files WHERE vpath = ?", (vpath,)).fetchone() is not None


def has_files(conn: sqlite3.Connection, vdir: str) -> bool:
    lo, hi = paths.prefix_range(vdir)
    return conn.execute("SELECT 1 FROM files WHERE vpath >= ? AND vpath < ? LIMIT 1",
                        (lo, hi)).fetchone() is not None


def under(conn: sqlite3.Connection, vdir: str, include_self: bool = True) -> list[str]:
    """Explicit directories at or below ``vdir``."""
    lo, hi = ("/", "0") if vdir == "/" else paths.prefix_range(vdir)
    rows = [r[0] for r in conn.execute(
        "SELECT vpath FROM vdirs WHERE present = 1 AND vpath >= ? AND vpath < ?", (lo, hi))]
    if include_self and vdir != "/" and conn.execute(
            "SELECT 1 FROM vdirs WHERE present = 1 AND vpath = ?", (vdir,)).fetchone():
        rows.append(vdir)
    return sorted(rows)


def exists(conn: sqlite3.Connection, vpath: str) -> bool:
    """Is vpath a directory: it holds files, or it is (or holds) an explicit directory."""
    return vpath == "/" or has_files(conn, vpath) or bool(under(conn, vpath))


def _ancestor_file(conn: sqlite3.Connection, vpath: str) -> str | None:
    parts = paths.split(vpath)
    for i in range(1, len(parts) + 1):
        p = "/" + "/".join(parts[:i])
        if is_file(conn, p):
            return p
    return None


def make(conn: sqlite3.Connection, vpath: str) -> str:
    """Create a directory (and, implicitly, the ones above it)."""
    try:
        vpath = paths.normalize(vpath)
    except paths.VPathError as e:
        raise VDirError(str(e)) from None
    if vpath == "/":
        raise VDirError("the root already exists")
    blocker = _ancestor_file(conn, vpath)
    if blocker:
        raise VDirError(f"{blocker} is a file")
    with db.transaction(conn):
        if exists(conn, vpath):
            raise VDirError(f"{vpath} already exists")
        _set(conn, vpath, True)
    return vpath


def remove(conn: sqlite3.Connection, vpath: str) -> int:
    """Remove an empty directory (and the empty directories made inside it).

    Directories that hold files are not removed: move or delete the files first.
    """
    try:
        vpath = paths.normalize(vpath)
    except paths.VPathError as e:
        raise VDirError(str(e)) from None
    with db.transaction(conn):
        if vpath == "/" or not exists(conn, vpath):
            raise VDirError(f"{vpath} is not a directory")
        if has_files(conn, vpath):
            raise VDirError(f"{vpath} is not empty (it holds files)")
        dirs = under(conn, vpath)
        for d in dirs:
            _set(conn, d, False)
        return len(dirs)


def move(conn: sqlite3.Connection, old: str, new: str) -> int:
    """Re-home the explicit directories at and below ``old`` to ``new``."""
    n = 0
    for d in under(conn, old):
        _set(conn, d, False)
        _set(conn, new + d[len(old):], True)
        n += 1
    return n
