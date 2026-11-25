"""Packed directories: a directory of thousands of small files kept as one squashfs file.

The archive is an ordinary file of the virtual tree, ``<dir>.sqsh`` next to the directory, so
it is copied, backed up and scheduled like any other file. The ``archives`` table says which
directory it stands for and ``archive_members`` lists what is inside. In the tree a packed
directory shows as one opaque entry; a search sees inside only when asked to ("deep").

Packing runs on the server that owns the node holding the files, and needs the squashfs tools
(``mksquashfs``, ``unsquashfs``: Linux). Everywhere else the archive is just a file.
Nothing is deleted behind the user's back: the original files are removed only when that is
asked for (and only after the archive was checked), and an archive that is unpacked or dropped
is marked for deletion like any copy, so that the user confirms it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from . import db, index, nodes, paths
from .config import Config
from .control import StopFn, check
from .hashing import hash_file

log = logging.getLogger(__name__)

SUFFIX = ".sqsh"
CHUNK = 1000                 # members per change-log entry
TX_ROWS = 500                # rows changed per transaction: the write lock is never held for long
MEMBER_LIMIT = 200_000       # a directory with more files than this is refused (the list is kept)

ProgressFn = Callable[[str], None]


class ArchiveError(Exception):
    pass


# --- what the database knows ------------------------------------------------------


def archive_vpath(vdir: str) -> str:
    return vdir + SUFFIX


def get(conn: sqlite3.Connection, vdir: str, present_only: bool = True) -> sqlite3.Row | None:
    q = "SELECT * FROM archives WHERE dir_path = ?"
    if present_only:
        q += " AND present = 1"
    return conn.execute(q, (vdir,)).fetchone()


def packed_dirs(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT dir_path FROM archives WHERE present = 1")]


def packed_dir_of(conn: sqlite3.Connection, vpath: str) -> str | None:
    """The packed directory that is ``vpath`` or contains it, if any."""
    parts = paths.split(vpath)
    cands = ["/" + "/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    if not cands:
        return None
    marks = ",".join("?" * len(cands))
    row = conn.execute(f"SELECT dir_path FROM archives WHERE present = 1 AND dir_path IN ({marks})"
                       " ORDER BY length(dir_path) LIMIT 1", cands).fetchone()
    return row[0] if row else None


def archive_file_ids(conn: sqlite3.Connection) -> set[int]:
    return {r[0] for r in conn.execute("SELECT file_id FROM archives WHERE present = 1")}


def contains_packed(conn: sqlite3.Connection, vdir: str) -> list[str]:
    """Packed directories at or below vdir."""
    cond, args = paths.subtree_sql(vdir, "dir_path")
    return [r[0] for r in conn.execute(
        f"SELECT dir_path FROM archives WHERE present = 1 AND {cond}", args)]


# --- the change log ---------------------------------------------------------------


def _log_pack(conn: sqlite3.Connection, row: sqlite3.Row,
              members: list[tuple[str, int, int]]) -> None:
    me = index.self_id(conn)
    index.append_change(conn, "archive", {
        "op": "pack", "dir": row["dir_path"], "file": archive_vpath(row["dir_path"]),
        "files": row["files"], "bytes": row["bytes"], "updated": row["updated"],
        "updated_by": me, "cleanup": row["cleanup"], "targets": json.loads(row["targets"])})
    _log_members(conn, row, members)


def _log_members(conn: sqlite3.Connection, row: sqlite3.Row,
                 members: list[tuple[str, int, int]]) -> None:
    for i in range(0, len(members), CHUNK):
        index.append_change(conn, "archive", {
            "op": "members", "dir": row["dir_path"], "updated": row["updated"],
            "updated_by": index.self_id(conn),
            "entries": [[p, s, h] for p, s, h in members[i:i + CHUNK]]})


def forget_copies(conn: sqlite3.Connection, pairs: list[tuple[int, str]]) -> None:
    """Stop tracking these (file, node) copies, a few hundred per transaction."""
    for i in range(0, len(pairs), TX_ROWS):
        with db.transaction(conn, "forgetting copies"):
            for fid, node_id in pairs[i:i + TX_ROWS]:
                index.forget_copy(conn, fid, node_id)


def _log_remove(conn: sqlite3.Connection, vdir: str, ts: int) -> None:
    index.append_change(conn, "archive", {"op": "remove", "dir": vdir, "updated": ts,
                                          "updated_by": index.self_id(conn)})


def apply_change(conn: sqlite3.Connection, d: dict) -> None:
    """A fact about an archive from another server (see ``sync``)."""
    op = d["op"]
    row = get(conn, d["dir"], present_only=False)
    stamp = (d["updated"], d["updated_by"])
    if op == "members":
        if row is not None and (row["updated"], row["updated_by"]) == stamp and row["present"]:
            conn.executemany(
                "INSERT OR REPLACE INTO archive_members(archive_id, path, size, hash)"
                " VALUES (?, ?, ?, ?)",
                [(row["id"], e[0], e[1], e[2] if len(e) > 2 else None) for e in d["entries"]])
        return
    if row is not None and stamp <= (row["updated"], row["updated_by"]):
        return                                  # we know something newer
    if op == "pack":
        fid = index.ensure_file(conn, d["file"])
        targets = json.dumps(d.get("targets") or [])
        cleanup = int(bool(d.get("cleanup")))
        if row is None:
            conn.execute("INSERT INTO archives(dir_path, file_id, present, files, bytes, updated,"
                         " updated_by, cleanup, targets) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)",
                         (d["dir"], fid, d["files"], d["bytes"], *stamp, cleanup, targets))
        else:
            conn.execute("UPDATE archives SET file_id = ?, present = 1, files = ?, bytes = ?,"
                         " updated = ?, updated_by = ?, cleanup = ?, targets = ? WHERE id = ?",
                         (fid, d["files"], d["bytes"], *stamp, cleanup, targets, row["id"]))
            conn.execute("DELETE FROM archive_members WHERE archive_id = ?", (row["id"],))
        if cleanup:
            _todo(conn, d["dir"], d.get("targets") or [])
    elif op == "remove" and row is not None:
        conn.execute("UPDATE archives SET present = 0, updated = ?, updated_by = ? WHERE id = ?",
                     (*stamp, row["id"]))
        conn.execute("DELETE FROM archive_todo WHERE dir_path = ?", (d["dir"],))   # members: purge_members


def _todo(conn: sqlite3.Connection, vdir: str, targets: list[str]) -> None:
    """What this server must still do for a directory packed everywhere: copy the archive to
    its nodes that held the directory, and remove the originals there."""
    mine = index.owned_node_ids(conn)
    for node_id in targets:
        if node_id in mine:
            conn.execute("INSERT OR IGNORE INTO archive_todo(dir_path, node_id, stage, updated)"
                         " VALUES (?, ?, 'copy', ?)", (vdir, node_id, db.now()))


# --- the tools ------------------------------------------------------------------------


def tools() -> tuple[str, str] | None:
    mk, un = shutil.which("mksquashfs"), shutil.which("unsquashfs")
    return (mk, un) if mk and un else None


def _need_tools() -> tuple[str, str]:
    t = tools()
    if t is None:
        raise ArchiveError(
            "the squashfs tools are not installed on this server (mksquashfs and unsquashfs; on "
            "Debian and Ubuntu: apt install squashfs-tools). Only a server that has them can pack "
            "or unpack a directory.")
    return t


def _run(cmd: list[str], stop: StopFn | None, what: str) -> tuple[int, str]:
    """Run a tool, polling for a stop request (then the tool is ended)."""
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err)
        try:
            while proc.poll() is None:
                time.sleep(0.2)
                check(stop)
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        err.seek(0)
        text = err.read().decode("utf-8", "replace").strip()
    return proc.returncode, text


def _listing(unsquashfs: str, archive: Path) -> set[str]:
    out = subprocess.run([unsquashfs, "-l", "-d", "x", str(archive)], capture_output=True,
                         check=False)
    if out.returncode != 0:
        raise ArchiveError("the archive cannot be read back: "
                           + out.stderr.decode("utf-8", "replace").strip()[-300:])
    names = set()
    for line in out.stdout.decode("utf-8", "surrogateescape").splitlines():
        if line.startswith("x/"):
            names.add(line[2:])
    return names


def _walk(src: Path) -> list[tuple[str, int]]:
    """Regular files below src as (relative path with '/', size)."""
    out: list[tuple[str, int]] = []
    for base, _dirs, files in os.walk(src):
        for name in files:
            full = os.path.join(base, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            rel = os.path.relpath(full, src).replace(os.sep, "/")
            out.append((rel, st.st_size))
            if len(out) > MEMBER_LIMIT:
                raise ArchiveError(f"more than {MEMBER_LIMIT:,} files: split it into folders")
    return out


# --- finding the node -----------------------------------------------------------------


def _local_roots(conn: sqlite3.Connection, cfg: Config) -> dict[str, Path]:
    return {loc.node_id: loc.root for loc in nodes.discover(conn, cfg).online}


def holders(conn: sqlite3.Connection, vdir: str) -> list[sqlite3.Row]:
    """Nodes holding a present copy of every file below vdir (best first)."""
    cond, args = paths.subtree_sql(vdir, "d.path")
    total = conn.execute(
        f"SELECT COUNT(*) FROM files f JOIN dirs d ON d.id = f.dir_id WHERE {cond}",
        args).fetchone()[0]
    if not total:
        return []
    rows = conn.execute(
        "SELECT n.*, COUNT(*) AS have FROM files f JOIN dirs d ON d.id = f.dir_id"
        f" JOIN copies c ON c.file_id = f.id AND c.state = 'present' JOIN nodes n ON n.id = c.node_id"
        f" WHERE {cond} GROUP BY n.id HAVING have = ? ORDER BY n.priority DESC, n.name",
        (*args, total)).fetchall()
    return list(rows)


def choose_node(conn: sqlite3.Connection, cfg: Config, vdir: str,
                node: str | None) -> sqlite3.Row:
    cands = holders(conn, vdir)
    if not cands:
        if not conn.execute("SELECT 1 FROM files f JOIN dirs d ON d.id = f.dir_id WHERE "
                            + paths.subtree_sql(vdir, "d.path")[0] + " LIMIT 1",
                            paths.subtree_sql(vdir, "d.path")[1]).fetchone():
            raise ArchiveError(f"no files below {vdir}")
        raise ArchiveError(
            f"no single node holds every file below {vdir}; copy them to one node first "
            "(a directory is packed on one node)")
    if node:
        row = nodes.get_node(conn, node)
        if row["id"] not in {c["id"] for c in cands}:
            raise ArchiveError(f"node {row['name']} does not hold every file below {vdir}")
        return row
    # On a node of the server asked, if one holds it; otherwise on any online one (the
    # request is then handed to the server that owns it).
    online = [c for c in cands if c["online"]]
    if not online:
        raise ArchiveError(f"no node that holds every file below {vdir} is online")
    me = index.self_id(conn)
    mine = [c for c in online if c["owner_id"] == me]
    return (mine or online)[0]


# --- packing --------------------------------------------------------------------------


def check_packable(conn: sqlite3.Connection, vdir: str) -> None:
    if vdir == "/":
        raise ArchiveError("the whole tree cannot be packed")
    if get(conn, vdir):
        raise ArchiveError(f"{vdir} is already packed")
    outer = packed_dir_of(conn, vdir)
    if outer:
        raise ArchiveError(f"{vdir} is inside the packed directory {outer}")
    if contains_packed(conn, vdir):
        raise ArchiveError(f"{vdir} contains a packed directory: unpack that first")
    if index.file_id(conn, archive_vpath(vdir)) is not None:
        raise ArchiveError(f"{archive_vpath(vdir)} exists already")


def member_hash(path: Path | str) -> int:
    """A 64-bit stand-in for a file's content (the start of its blake2b hash), as the member
    lists carry it: enough to check a loose file against the archive before removing it."""
    n = int(hash_file(path).split(":", 1)[1][:16], 16)
    return n - (1 << 64) if n >= 1 << 63 else n


def _hashed_walk(src: Path, stop: StopFn | None, say: ProgressFn) -> list[tuple[str, int, int]]:
    members = []
    walked = _walk(src)
    for i, (rel, size) in enumerate(walked):
        check(stop)
        if i % 200 == 0:
            say(f"reading the files: {i:,} of {len(walked):,}")
        try:
            members.append((rel, size, member_hash(src / rel)))
        except OSError as e:
            raise ArchiveError(f"cannot read {rel}: {e}") from None
    return members


def pack(conn: sqlite3.Connection, cfg: Config, vdir: str, node: str | None = None,
         remove_originals: bool = False, progress: ProgressFn | None = None,
         stop: StopFn | None = None, everywhere: bool = False) -> dict:
    """Pack ``vdir`` on a node of this server. The caller holds the server lock.

    ``everywhere``: the archive is then copied to every node that held the directory and the
    originals are removed there too, each node's own server doing it once the archive has
    arrived and each file has been checked against it (see ``followup``). A stop request
    before the archive is registered leaves nothing behind; after that it only
    ends the optional removal of the originals.
    """
    say = progress or (lambda _m: None)
    mksquashfs, unsquashfs = _need_tools()
    vdir = paths.normalize(vdir)
    check_packable(conn, vdir)
    target = choose_node(conn, cfg, vdir, node)
    if target["owner_id"] != index.self_id(conn):
        raise ArchiveError(f"node {target['name']} belongs to another server")
    root = _local_roots(conn, cfg).get(target["id"])
    if root is None:
        raise ArchiveError(f"node {target['name']} is offline")
    src = paths.to_physical(root, vdir)
    dst = paths.to_physical(root, archive_vpath(vdir))
    if not src.is_dir():
        raise ArchiveError(f"{src} is not a directory")
    if dst.exists():
        raise ArchiveError(f"{dst} exists already")
    part = dst.with_name(dst.name + ".part")
    purge_members(conn)         # old lists of archives that are gone: not in the way of this one
    say(f"packing {vdir}: listing the files")
    members = _hashed_walk(src, stop, lambda m: say(f"packing {vdir}: {m}"))
    if not members:
        raise ArchiveError(f"{vdir} holds no files")
    total = sum(s for _, s, _ in members)
    try:
        say(f"packing {vdir}: {len(members):,} files, {total:,} bytes")
        for comp in ("zstd", "xz", "gzip"):
            part.unlink(missing_ok=True)
            code, err = _run([mksquashfs, str(src), str(part), "-comp", comp, "-noappend",
                              "-no-progress", "-quiet"], stop, "mksquashfs")
            if code == 0:
                break
            if "ompressor" not in err and "not supported" not in err:
                raise ArchiveError(f"mksquashfs failed: {err[-300:]}")
        else:
            raise ArchiveError(f"mksquashfs failed: {err[-300:]}")
        say(f"packing {vdir}: checking the archive")
        listed = _listing(unsquashfs, part)
        missing = [rel for rel, _, _ in members if rel not in listed]
        if missing:
            raise ArchiveError(f"the archive lacks {len(missing)} file(s), e.g. {missing[0]}; "
                               "nothing was changed")
        check(stop)
        os.replace(part, dst)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    say(f"packing {vdir}: recording the archive")
    st = dst.stat()
    digest = hash_file(dst)
    # The nodes that hold the directory now (any server's) are where the archive must go.
    cond, args = paths.subtree_sql(vdir, "d.path")
    targets = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT c.node_id FROM files f JOIN dirs d ON d.id = f.dir_id"
        f" JOIN copies c ON c.file_id = f.id WHERE {cond}", args)} - {target["id"]}) \
        if everywhere else []
    # In short transactions (a directory can hold hundreds of thousands of files): the archive
    # and its first entry, then the member list in pieces, then the loose files are forgotten.
    with db.transaction(conn, "packing a directory"):
        fid = index.ensure_file(conn, archive_vpath(vdir))
        ts = db.now()
        me = index.self_id(conn)
        conn.execute("DELETE FROM archives WHERE dir_path = ?", (vdir,))
        conn.execute("INSERT INTO archives(dir_path, file_id, present, files, bytes, updated,"
                     " updated_by, cleanup, targets) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)",
                     (vdir, fid, len(members), total, ts, me, int(everywhere),
                      json.dumps(targets)))
        row = get(conn, vdir)
        index.record_copy(conn, fid, target["id"], st.st_size, st.st_mtime_ns, digest)
        _log_pack(conn, row, [])               # the entry that announces it
        if everywhere:
            _todo(conn, vdir, targets)
    try:
        for i in range(0, len(members), TX_ROWS):
            part = members[i:i + TX_ROWS]
            with db.transaction(conn, "packing a directory: member list"):
                conn.executemany("INSERT INTO archive_members(archive_id, path, size, hash)"
                                 " VALUES (?, ?, ?, ?)", [(row["id"], p, s, h) for p, s, h in part])
                _log_members(conn, row, part)
    except BaseException:
        # A member list that is only part of it is worse than none: take the archive back
        # (its file stays, as a plain file).
        with db.transaction(conn, "packing a directory: undoing"):
            _retire(conn, row)
        raise
    cond, args = paths.subtree_sql(vdir, "d.path")
    mine = index.owned_node_ids(conn)
    loose = [(r["fid"], r["nid"]) for r in conn.execute(
        "SELECT f.id AS fid, c.node_id AS nid FROM files f JOIN dirs d ON d.id = f.dir_id"
        f" JOIN copies c ON c.file_id = f.id WHERE {cond}", args).fetchall() if r["nid"] in mine]
    forget_copies(conn, loose)       # the loose files are not tracked any more
    say(f"packing {vdir}: giving the database's free space back")
    reclaimed = db.reclaim(conn)
    removed = kept = 0
    if remove_originals or everywhere:
        say(f"packing {vdir}: removing the original files (each is checked against the archive)")
        removed, kept = remove_verified(src, members, stop)
    say(f"packed {vdir}")
    return {"dir": vdir, "archive": archive_vpath(vdir), "node": target["name"],
            "files": len(members), "bytes": total, "archive_bytes": st.st_size,
            "originals_removed": removed, "originals_kept": not (remove_originals or everywhere),
            "originals_left": kept, "targets": len(targets), "database_reclaimed": reclaimed}


def remove_verified(src: Path, members: list[tuple[str, int, int | None]],
                    stop: StopFn | None = None) -> tuple[int, int]:
    """Delete the files of ``members`` found below ``src`` that are exactly what the archive
    holds (same size, same content hash), then the folders left empty. A file that differs,
    or is not in the archive, stays. Stopping between two files is safe: what is gone was
    checked, what is left is simply still there. Returns (removed, left)."""
    removed = left = 0
    for rel, size, h in members:
        check(stop)
        f = src / rel
        try:
            if not f.is_file() or f.is_symlink():
                continue                   # not there (a node holds some of the files only)
            if f.stat().st_size != size or h is None or member_hash(f) != h:
                left += 1
                continue
            f.unlink()
            removed += 1
        except OSError as e:
            log.warning("could not remove %s: %s", f, e)
            left += 1
    for base, _dirs, _files in os.walk(src, topdown=False):
        try:
            os.rmdir(base)          # refuses unless empty: only what the archive replaced goes
        except OSError:
            pass
    return removed, left


# --- the other nodes of a directory packed everywhere ----------------------------------


def cleanup_node(conn: sqlite3.Connection, cfg: Config, vdir: str, node_id: str,
                 progress: ProgressFn | None = None, stop: StopFn | None = None) -> dict:
    """On a node of this server that holds the archive: remove the loose files it still has
    (checked one by one against the archive), forget them in the index and give the
    database's free space back."""
    say = progress or (lambda _m: None)
    row = get(conn, vdir)
    if row is None:
        raise ArchiveError(f"{vdir} is no longer packed")
    root = _local_roots(conn, cfg).get(node_id)
    if root is None:
        raise ArchiveError("the node is offline")
    cur = index.current_version(conn, row["file_id"])
    mine = conn.execute("SELECT hash FROM copies WHERE file_id = ? AND node_id = ?"
                        " AND state = 'present'", (row["file_id"], node_id)).fetchone()
    if cur is None or mine is None or mine["hash"] != cur.hash:
        raise ArchiveError("the archive has not arrived on this node (or differs): "
                           "nothing was removed")
    members = [(r["path"], r["size"], r["hash"]) for r in conn.execute(
        "SELECT path, size, hash FROM archive_members WHERE archive_id = ? ORDER BY path",
        (row["id"],))]
    if any(h is None for _, _, h in members):
        raise ArchiveError("the archive was made by an older version (no checksums): "
                           "unpack it and pack it again")
    src = paths.to_physical(root, vdir)
    say(f"removing the originals of {vdir} (checked against the archive)")
    removed = left = 0
    if src.is_dir():
        removed, left = remove_verified(src, members, stop)
    cond, args = paths.subtree_sql(vdir, "d.path")
    forget_copies(conn, [(r[0], node_id) for r in conn.execute(
        "SELECT f.id FROM files f JOIN dirs d ON d.id = f.dir_id JOIN copies c"
        f" ON c.file_id = f.id WHERE c.node_id = ? AND {cond}", (node_id, *args)).fetchall()])
    reclaimed = db.reclaim(conn)
    return {"dir": vdir, "removed": removed, "left": left, "database_reclaimed": reclaimed}


def followup(conn: sqlite3.Connection, cfg: Config) -> int:
    """Advance what is owed for directories packed everywhere, for this server's nodes:
    copy the archive to a node that lacks it, then remove the originals there. Makes the
    (visible, cancelable) jobs; returns how many it made."""
    from . import jobs

    made = 0
    online = {loc.node_id for loc in nodes.discover(conn, cfg).online}
    for t in conn.execute("SELECT * FROM archive_todo ORDER BY dir_path").fetchall():
        row = get(conn, t["dir_path"])
        if row is None or t["node_id"] not in online:
            if row is None:
                conn.execute("DELETE FROM archive_todo WHERE dir_path = ? AND node_id = ?",
                             (t["dir_path"], t["node_id"]))
            continue
        vdir, node_id = t["dir_path"], t["node_id"]
        name = conn.execute("SELECT name FROM nodes WHERE id = ?", (node_id,)).fetchone()[0]
        if t["stage"] == "clean":
            job = conn.execute("SELECT status FROM jobs WHERE id = ?", (t["job_id"],)).fetchone()
            if job is None or job["status"] == "done":
                conn.execute("DELETE FROM archive_todo WHERE dir_path = ? AND node_id = ?",
                             (vdir, node_id))
            elif job["status"] in ("failed", "cancelled"):
                conn.execute("UPDATE archive_todo SET stage = 'copy', job_id = NULL WHERE"
                             " dir_path = ? AND node_id = ?", (vdir, node_id))
            continue
        cur = index.current_version(conn, row["file_id"])
        have = conn.execute("SELECT hash FROM copies WHERE file_id = ? AND node_id = ?"
                            " AND state = 'present'", (row["file_id"], node_id)).fetchone()
        if cur is not None and have is not None and have["hash"] == cur.hash:
            with db.transaction(conn, "removing the originals"):
                job_id = jobs.create_tool_job(
                    conn, "pack", f"remove the original files of {vdir} on {name}",
                    [("clean", vdir, node_id, {})])
                conn.execute("UPDATE archive_todo SET stage = 'clean', job_id = ?, updated = ?"
                             " WHERE dir_path = ? AND node_id = ?",
                             (job_id, db.now(), vdir, node_id))
            made += 1
        elif not conn.execute(
                "SELECT 1 FROM job_items i JOIN jobs j ON j.id = i.job_id WHERE i.vpath = ?"
                " AND i.dst_node = ? AND i.op = 'copy' AND i.state IN ('pending', 'running')"
                " AND j.status IN ('pending', 'running', 'waiting')",
                (archive_vpath(vdir), node_id)).fetchone():
            try:
                rep = jobs.create_transfer_job(conn, [archive_vpath(vdir)], None, name, None)
            except jobs.JobError as e:
                log.warning("cannot copy the archive of %s to %s: %s", vdir, name, e)
                continue
            made += rep.job_id is not None
    return made


# --- unpacking and dropping -----------------------------------------------------------


def _mark_archive_for_deletion(conn: sqlite3.Connection, fid: int) -> None:
    for r in conn.execute("SELECT node_id FROM copies WHERE file_id = ?", (fid,)).fetchall():
        index.mark_copy(conn, fid, r["node_id"], True)


def _retire(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    ts = db.now()
    # The member list of a retired archive is deleted afterwards, in pieces (purge_members).
    conn.execute("UPDATE archives SET present = 0, updated = ?, updated_by = ? WHERE id = ?",
                 (ts, index.self_id(conn), row["id"]))
    conn.execute("DELETE FROM archive_todo WHERE dir_path = ?", (row["dir_path"],))
    _log_remove(conn, row["dir_path"], ts)
    _mark_archive_for_deletion(conn, row["file_id"])


def purge_members(conn: sqlite3.Connection) -> int:
    """Delete the member lists of archives that are gone, TX_ROWS rows per transaction."""
    total = 0
    for (aid,) in conn.execute("SELECT DISTINCT a.id FROM archives a JOIN archive_members m"
                               " ON m.archive_id = a.id WHERE a.present = 0").fetchall():
        while True:
            with db.transaction(conn, "forgetting an archive's file list"):
                n = conn.execute(
                    "DELETE FROM archive_members WHERE archive_id = ? AND path IN"
                    " (SELECT path FROM archive_members WHERE archive_id = ? LIMIT ?)",
                    (aid, aid, TX_ROWS)).rowcount
            total += n
            if n < TX_ROWS:
                break
    return total


def drop(conn: sqlite3.Connection, vdir: str) -> dict:
    """Forget the archive without unpacking it (its file is marked for deletion)."""
    vdir = paths.normalize(vdir)
    row = get(conn, vdir)
    if row is None:
        raise ArchiveError(f"{vdir} is not a packed directory")
    with db.transaction(conn, "dropping an archive"):
        _retire(conn, row)
    purge_members(conn)
    db.reclaim(conn)
    return {"dir": vdir, "archive": archive_vpath(vdir)}


def unpack(conn: sqlite3.Connection, cfg: Config, vdir: str, node: str | None = None,
           progress: ProgressFn | None = None, stop: StopFn | None = None) -> dict:
    """Extract the archive into the directory it stands for. The caller holds the server lock."""
    say = progress or (lambda _m: None)
    _, unsquashfs = _need_tools()
    vdir = paths.normalize(vdir)
    row = get(conn, vdir)
    if row is None:
        raise ArchiveError(f"{vdir} is not a packed directory")
    roots = _local_roots(conn, cfg)
    cands = [r for r in conn.execute(
        "SELECT c.node_id AS nid, n.name FROM copies c JOIN nodes n ON n.id = c.node_id"
        " WHERE c.file_id = ? AND c.state = 'present'", (row["file_id"],)) if r["nid"] in roots]
    if node:
        want = nodes.get_node(conn, node)["id"]
        cands = [c for c in cands if c["nid"] == want]
    if not cands:
        raise ArchiveError(
            f"this server holds no copy of {archive_vpath(vdir)}; copy it to a node of this "
            "server first (or unpack on the server that has it)")
    nid, nname = cands[0]["nid"], cands[0]["name"]
    root = roots[nid]
    archive = paths.to_physical(root, archive_vpath(vdir))
    if not archive.is_file():
        raise ArchiveError(f"{archive} is missing")
    dest = paths.to_physical(root, vdir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".reifs-unpack-", dir=dest.parent))
    try:
        say(f"unpacking {vdir}")
        out = tmp / "x"
        code, err = _run([unsquashfs, "-no-progress", "-d", str(out), str(archive)], stop,
                         "unsquashfs")
        if code != 0:
            raise ArchiveError(f"unsquashfs failed: {err[-300:]}")
        # All or nothing: nothing of the user's is overwritten.
        clashes = []
        for base, _dirs, files in os.walk(out):
            rel = os.path.relpath(base, out)
            for name in files:
                src = Path(base) / name
                tgt = dest / rel / name if rel != "." else dest / name
                if os.path.lexists(tgt):
                    same = (src.is_file() and not src.is_symlink() and tgt.is_file()
                            and not tgt.is_symlink() and src.stat().st_size == tgt.stat().st_size)
                    if not same:
                        clashes.append(str(tgt))
        if clashes:
            raise ArchiveError(f"{len(clashes)} file(s) in {dest} differ from the archive's, "
                               f"e.g. {clashes[0]}; nothing was changed")
        for base, dirs, files in os.walk(out):
            rel = os.path.relpath(base, out)
            here = dest if rel == "." else dest / rel
            here.mkdir(parents=True, exist_ok=True)
            for name in files:
                tgt = here / name
                if not os.path.lexists(tgt):
                    os.replace(Path(base) / name, tgt)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with db.transaction(conn, "unpacking a directory"):
        _retire(conn, row)
    purge_members(conn)
    say(f"unpacked {vdir}; indexing the files")
    from . import scanner
    scanner.scan_all(conn, cfg, [nid], progress=None, stop=stop, full=True)
    return {"dir": vdir, "node": nname, "files": row["files"]}


# --- searching inside -------------------------------------------------------------------


def search_members(conn: sqlite3.Connection, pattern: str | None, limit: int) -> list[dict]:
    """Archive members whose name (or path, if the pattern contains a "/") matches."""
    from . import queries
    pat = queries.name_pattern(pattern)
    if pat is None:
        return []
    literal = queries._longest_literal(pat)
    where, args = "", []
    if literal and literal.isascii():
        where = " AND (instr(lower(m.path), ?) > 0 OR m.path GLOB '*[^ -~]*')"
        args.append(literal)
    elif literal:
        where = " AND m.path GLOB '*[^ -~]*'"
    out: list[dict] = []
    for r in conn.execute(
            "SELECT a.dir_path, m.path, m.size FROM archive_members m JOIN archives a"
            f" ON a.id = m.archive_id WHERE a.present = 1{where} ORDER BY a.dir_path, m.path",
            args):
        full = r["dir_path"] + "/" + r["path"]
        if queries._name_matches(pat, full):
            out.append({"vpath": full, "archive": r["dir_path"], "size": r["size"]})
            if len(out) >= limit:
                break
    return out
