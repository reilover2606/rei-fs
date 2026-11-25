"""Files and directories that may exist in several versions.

By default copies of a file that differ are a conflict: a decision is asked and the stale copies
are overwritten once the user says so. For a path marked tolerant that is not wanted (a
database kept differently on two nodes, a folder of notes edited in two places): the differing
copies are left alone and no decision is raised. The setting of a directory is inherited by
what is below it; the nearest setting decides, and a setting of 0 switches an inherited one off.
"""

from __future__ import annotations

import sqlite3

from . import db, index, paths


class ToleranceError(Exception):
    pass


def load(conn: sqlite3.Connection) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT vpath, value FROM tolerance WHERE value IS NOT NULL")}


def source(conn: sqlite3.Connection, vpath: str) -> tuple[bool, str | None]:
    """(tolerant?, the path the setting was made on) for a file or directory."""
    parts = paths.split(vpath)
    cands = ["/" + "/".join(parts[:i]) for i in range(len(parts), 0, -1)] + ["/"]
    rows = {r[0]: r[1] for r in conn.execute(
        f"SELECT vpath, value FROM tolerance WHERE value IS NOT NULL AND vpath IN"
        f" ({','.join('?' * len(cands))})", cands)}
    for c in cands:                      # nearest first
        if c in rows:
            return bool(rows[c]), c
    return False, None


def is_tolerant(conn: sqlite3.Connection, vpath: str) -> bool:
    return source(conn, vpath)[0]


def get(conn: sqlite3.Connection, vpath: str) -> dict:
    vpath = paths.normalize(vpath)
    value, src = source(conn, vpath)
    return {"path": vpath, "tolerant": value, "set_on": src, "inherited": src != vpath}


def log(conn: sqlite3.Connection, vpath: str) -> None:
    r = conn.execute("SELECT value, updated, updated_by FROM tolerance WHERE vpath = ?",
                     (vpath,)).fetchone()
    if r:
        index.append_change(conn, "tolerance", {"vpath": vpath, "value": r["value"],
                                                "updated": r["updated"],
                                                "updated_by": r["updated_by"]})


def _put(conn: sqlite3.Connection, vpath: str, value: int | None) -> None:
    conn.execute(
        "INSERT INTO tolerance(vpath, value, updated, updated_by) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(vpath) DO UPDATE SET value = excluded.value, updated = excluded.updated,"
        " updated_by = excluded.updated_by",
        (vpath, value, db.now(), index.self_id(conn)))
    log(conn, vpath)


def set_tolerance(conn: sqlite3.Connection, vpath: str, value: bool | None) -> dict:
    """Mark a file or directory tolerant (True), not tolerant (False, even if a parent is), or
    back to inheriting (None). Decisions already asked for files that are tolerant now go."""
    try:
        vpath = paths.normalize(vpath)
    except paths.VPathError as e:
        raise ToleranceError(str(e)) from None
    with db.transaction(conn, "setting tolerance"):
        _put(conn, vpath, None if value is None else int(value))
        _drop_decisions(conn, vpath)
    return get(conn, vpath)


def _drop_decisions(conn: sqlite3.Connection, vpath: str) -> None:
    """Open or dismissed conflict decisions of the files below vpath that are tolerant now."""
    fid = index.file_id(conn, vpath)
    if fid is not None:
        ids = [fid]
    else:
        cond, args = paths.subtree_sql(vpath, "d.path")
        ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT dec.file_id FROM decisions dec JOIN files f ON f.id = dec.file_id"
            f" JOIN dirs d ON d.id = f.dir_id WHERE dec.kind = 'divergent' AND {cond}", args)]
    for fid in ids:
        if is_tolerant(conn, index.vpath_of(conn, fid)):
            index.obsolete_decisions(conn, fid, ("divergent",), statuses=("open", "dismissed"))


def move(conn: sqlite3.Connection, old: str, new: str) -> int:
    """The settings of a moved path (and of what is inside it) move with it."""
    n = 0
    for vpath, value in sorted(load(conn).items()):
        if vpath == old or vpath.startswith(old.rstrip("/") + "/"):
            _put(conn, vpath, None)
            _put(conn, new + vpath[len(old):], value)
            n += 1
    return n


def apply_change(conn: sqlite3.Connection, d: dict) -> None:
    row = conn.execute("SELECT updated, updated_by FROM tolerance WHERE vpath = ?",
                       (d["vpath"],)).fetchone()
    if row is None or (d["updated"], d["updated_by"]) > (row[0], row[1]):
        conn.execute("INSERT OR REPLACE INTO tolerance(vpath, value, updated, updated_by)"
                     " VALUES (?, ?, ?, ?)",
                     (d["vpath"], d["value"], d["updated"], d["updated_by"]))
        _drop_decisions(conn, d["vpath"])
