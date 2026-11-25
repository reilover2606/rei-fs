"""Tags. They belong to the vpath (file) and follow renames.

Removal keeps a tombstone (present = 0) with a timestamp so that edits made on
different servers merge by last-writer-wins per (file, tag).
"""

from __future__ import annotations

import sqlite3

from . import archives, db, index, paths


class TagError(Exception):
    pass


def validate(tag: str) -> str:
    tag = tag.strip()
    if not tag or any(c in tag for c in "\n\r\t,") or tag != tag.strip():
        raise TagError(f"invalid tag: {tag!r}")
    return tag


def resolve_targets(conn: sqlite3.Connection, vpath: str, recursive: bool) -> list[int]:
    """File ids for a vpath: the file itself, or every file below a directory."""
    vpath = paths.normalize(vpath)
    fid = index.file_id(conn, vpath)
    if fid is not None:
        return [fid]
    packed = archives.get(conn, vpath)
    if packed is not None:          # a packed directory is tagged as its archive
        return [packed["file_id"]]
    cond, args = paths.subtree_sql(vpath, "d.path")
    ids = [r[0] for r in conn.execute(
        f"SELECT f.id FROM files f JOIN dirs d ON d.id = f.dir_id WHERE {cond}", args)]
    if not ids:
        raise TagError(f"no such file or directory in the virtual tree: {vpath}")
    if not recursive:
        raise TagError(f"{vpath} is a directory; use -r to tag every file below it")
    return ids


def set_tag(conn: sqlite3.Connection, file_ids: list[int], tag: str, present: bool,
            server_id: str) -> int:
    tag = validate(tag)
    ts = db.now()
    changed = 0
    with db.transaction(conn):
        for fid in file_ids:
            if present:
                cur = conn.execute(
                    "INSERT INTO tags(file_id, tag, present, updated, updated_by)"
                    " VALUES (?, ?, 1, ?, ?) ON CONFLICT(file_id, tag) DO UPDATE SET"
                    " present = 1, updated = excluded.updated,"
                    " updated_by = excluded.updated_by WHERE tags.present = 0",
                    (fid, tag, ts, server_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE tags SET present = 0, updated = ?, updated_by = ?"
                    " WHERE file_id = ? AND tag = ? AND present = 1",
                    (ts, server_id, fid, tag),
                )
            if cur.rowcount:
                index.log_tag(conn, fid, tag)
            changed += cur.rowcount
    return changed


def tags_of(conn: sqlite3.Connection, fid: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT tag FROM tags WHERE file_id = ? AND present = 1 ORDER BY tag", (fid,))]


def all_tags(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT tag, COUNT(*) FROM tags WHERE present = 1 GROUP BY tag ORDER BY tag")]
