"""Moving files and directories inside the virtual tree (design section 5.5).

A virtual move changes the *vpath* of files. It renames the copies on every
node that holds them (``rename`` job items, run by the owner of each node),
so every node ends up with the new structure. Nothing is copied and nothing
is deleted: a rename never replaces an existing file, and redundant
duplicates are only marked for deletion.

When the destination already holds something with the same name:

``merge`` (default)
    Directories are merged (a virtual directory is only a prefix, so this is
    natural). A file that meets a file of the same name becomes one file with
    the copies of both: identical copies on the same node are redundant and the
    moved one is marked for deletion; different content becomes a ``divergent``
    file (newest version wins, the user decides what to update), exactly as if
    two nodes held different versions. If a single node holds both files with
    different content they cannot be merged and the moved file keeps both by
    taking a free name.
``preserve``
    Nothing is mixed: the moved item takes a free name (``name (2)``,
    ``name (3)``, ...), so both stay.

A file moved onto a directory name (or the reverse) always takes a free name.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import archives, db, index, jobs, nodes, paths, peers, priority, schedules, vdirs

MODES = ("merge", "preserve")


class MoveError(Exception):
    pass


@dataclass
class Move:
    fid: int
    old: str
    new: str
    note: str = ""            # why the name differs from the plain move, if it does
    merged: bool = False      # joins a file that already exists at ``new``
    duplicates: list[str] = field(default_factory=list)  # nodes holding an identical file
    nodes: list[str] = field(default_factory=list)       # nodes whose copy is renamed


@dataclass
class Plan:
    moves: list[Move] = field(default_factory=list)
    # Existing names at the destination that the moved items met.
    conflicts: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (vpath, reason)
    renamed: list[tuple[str, str]] = field(default_factory=list)  # (wanted, given) names
    dir_moves: list[tuple[str, str]] = field(default_factory=list)  # (old, new) directories

    @property
    def files(self) -> int:
        return len(self.moves)

    @property
    def merged(self) -> int:
        return sum(1 for m in self.moves if m.merged)

    @property
    def duplicates(self) -> int:
        return sum(len(m.duplicates) for m in self.moves)

    def summary(self) -> dict:
        return {"files": self.files, "dirs": len(self.dir_moves),
                "conflicts": sorted(set(self.conflicts)), "merged": self.merged,
                "duplicates": self.duplicates,
                "renamed": [{"wanted": a, "given": b} for a, b in self.renamed],
                "skipped": [{"path": p, "reason": r} for p, r in self.skipped[:50]]}


# --- the index as a tree --------------------------------------------------------


def _is_file(conn: sqlite3.Connection, vpath: str) -> bool:
    return index.file_id(conn, vpath) is not None


def _is_dir(conn: sqlite3.Connection, vpath: str) -> bool:
    return vdirs.exists(conn, vpath)


def _files_under(conn: sqlite3.Connection, vdir: str) -> list[tuple[int, str]]:
    cond, args = paths.subtree_sql(vdir)
    return [(r[0], r[1]) for r in conn.execute(
        f"SELECT f.id, f.vpath FROM vfiles f WHERE {cond} ORDER BY f.vpath", args)]


def _present(conn: sqlite3.Connection, fid: int) -> dict[str, sqlite3.Row]:
    return {r["node_id"]: r for r in conn.execute(
        "SELECT node_id, hash FROM copies WHERE file_id = ? AND state = 'present'", (fid,))}


def _free_name(conn: sqlite3.Connection, vpath: str, taken: set[str], is_file: bool) -> str:
    """vpath with `` (2)``, `` (3)``... added to its name (before the extension of a file)."""
    parent, name = paths.parent(vpath), paths.basename(vpath)
    stem, ext = name, ""
    if is_file and "." in name.strip("."):
        stem, _, tail = name.rpartition(".")
        ext = "." + tail
    n = 2
    while True:
        candidate = paths.join(parent, f"{stem} ({n}){ext}")
        if candidate not in taken and not _is_file(conn, candidate) \
                and not _is_dir(conn, candidate):
            return candidate
        n += 1


def _check_name(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise MoveError(f"not a valid name: {name!r}")
    return name


def _ancestor_file(conn: sqlite3.Connection, vpath: str) -> str | None:
    """The first ancestor of vpath that is a file (nothing can be put below it)."""
    parts = paths.split(vpath)
    for i in range(1, len(parts)):
        p = "/" + "/".join(parts[:i])
        if _is_file(conn, p):
            return p
    return None


# --- planning -----------------------------------------------------------------------


def plan_move(conn: sqlite3.Connection, sources: list[str], dest: str,
              name: str | None = None, on_conflict: str = "merge") -> Plan:
    """Work out the renames that move ``sources`` into the directory ``dest``.

    With ``name`` (one source only) the item is renamed as well.
    """
    if on_conflict not in MODES:
        raise MoveError(f"on_conflict must be one of: {', '.join(MODES)}")
    if not sources:
        raise MoveError("nothing selected")
    try:
        dest = paths.normalize(dest)
        srcs = sorted({paths.normalize(s) for s in sources})
    except paths.VPathError as e:
        raise MoveError(str(e)) from None
    if "/" in srcs:
        raise MoveError("the root cannot be moved")
    # An item inside another selected item moves with it.
    srcs = [s for s in srcs if not any(s.startswith(o.rstrip("/") + "/") for o in srcs if o != s)]
    if name is not None:
        if len(srcs) != 1:
            raise MoveError("a new name can only be given for a single item")
        _check_name(name)
    blocker = _ancestor_file(conn, paths.join(dest, "x")) if dest != "/" else None
    if blocker or (dest != "/" and _is_file(conn, dest)):
        raise MoveError(f"{blocker or dest} is a file, not a directory")

    inside = archives.packed_dir_of(conn, dest) if dest != "/" else None
    if inside:
        raise MoveError(f"{dest} is inside the packed directory {inside}")
    for src in srcs:
        outer = archives.packed_dir_of(conn, src)
        if outer or archives.contains_packed(conn, src):
            raise MoveError(f"{src} is or holds a packed directory ({outer or src}): unpack it "
                            "first, a packed directory cannot be moved or renamed")
        fid = index.file_id(conn, src)
        if fid is not None and fid in archives.archive_file_ids(conn):
            raise MoveError(f"{src} is the archive of a packed directory and cannot be moved")

    plan = Plan()
    taken: set[str] = set()   # vpaths given to files in this plan
    taken_tops: set[str] = set()
    for src in srcs:
        is_file = _is_file(conn, src)
        if not is_file and not _is_dir(conn, src):
            raise MoveError(f"not in the virtual tree: {src}")
        if not is_file and (dest == src or dest.startswith(src + "/")):
            raise MoveError(f"cannot move {src} into itself")
        top = paths.join(dest, name if name is not None else paths.basename(src))
        if top == src:
            continue  # already there
        top = _resolve_top(conn, plan, top, is_file, on_conflict, taken_tops)
        taken_tops.add(top)
        if not is_file:
            plan.dir_moves.append((src, top))
        pairs = [(fid, p, top + p[len(src):]) for fid, p in
                 ([(index.file_id(conn, src), src)] if is_file else _files_under(conn, src))]
        for fid, old, new in pairs:
            _plan_file(conn, plan, fid, old, new, on_conflict, taken)
    return plan


def _resolve_top(conn, plan: Plan, top: str, is_file: bool, mode: str,
                 taken_tops: set[str]) -> str:
    """The vpath the item itself will have, avoiding what it must not mix with."""
    file_there, dir_there = _is_file(conn, top), _is_dir(conn, top)
    twice = top in taken_tops  # two moved items with one name: keep both
    if not (file_there or dir_there or twice):
        return top
    plan.conflicts.append(top)
    same_kind = file_there if is_file else dir_there
    other_kind = dir_there if is_file else file_there
    if same_kind and not other_kind and not twice and mode == "merge":
        return top  # directories merge; files are decided one by one in _plan_file
    new = _free_name(conn, top, taken_tops, is_file)
    plan.renamed.append((paths.basename(top), paths.basename(new)))
    return new


def _plan_file(conn, plan: Plan, fid: int, old: str, new: str, mode: str,
               taken: set[str]) -> None:
    mine = _present(conn, fid)
    if not mine:
        plan.skipped.append((old, "no present copy"))
        return
    blocker = _ancestor_file(conn, new)
    if blocker:
        plan.skipped.append((old, f"{blocker} is a file"))
        return
    note, merged, dups = "", False, []
    if new in taken or _is_dir(conn, new):
        note = "name taken"
        given = _free_name(conn, new, taken, True)
        plan.renamed.append((paths.basename(new), paths.basename(given)))
        new = given
    elif _is_file(conn, new):
        other_fid = index.file_id(conn, new)
        theirs = _present(conn, other_fid)
        both = set(mine) & set(theirs)
        if mode == "merge" and all(mine[n]["hash"] == theirs[n]["hash"] for n in both):
            merged, dups = True, sorted(both)
            plan.conflicts.append(new)
        else:
            note = "kept both"
            plan.conflicts.append(new)
            given = _free_name(conn, new, taken, True)
            plan.renamed.append((paths.basename(new), paths.basename(given)))
            new = given
    taken.add(new)
    plan.moves.append(Move(fid, old, new, note, merged, dups,
                           sorted(n for n in mine if n not in dups)))


# --- carrying it out ---------------------------------------------------------------------


@dataclass
class Result:
    job_id: int | None = None
    items: int = 0
    remote_jobs: list[tuple[str, int | None, int]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    marked: int = 0


def describe(sources: list[str], dest: str, name: str | None) -> str:
    what = jobs.describe(sources, None)
    return f"move {what} to {dest}" + (f" as {name}" if name else "")


def apply_plan(conn: sqlite3.Connection, plan: Plan, description: str) -> Result:
    """Create the rename job for our nodes, ask the other servers to rename on theirs
    and mark the redundant duplicates for deletion."""
    me = nodes.self_id(conn)
    local: list[tuple[Move, str]] = []
    remote: dict[tuple[str, str], list[tuple[str, str]]] = {}
    marks: list[tuple[int, str, str]] = []
    for m in plan.moves:
        for node_id in m.nodes:
            owner = index.owner_of(conn, node_id)
            if owner == me:
                local.append((m, node_id))
            elif owner:
                remote.setdefault((owner, node_id), []).append((m.old, m.new))
        for node_id in m.duplicates:
            marks.append((m.fid, node_id, m.old))
    result = Result()
    with db.transaction(conn):
        if local:
            result.job_id = jobs._new_job(conn, "wanted", description)
            ts = db.now()
            conn.executemany(
                "INSERT INTO job_items(job_id, op, vpath, new_vpath, src_node, dst_node, size,"
                " updated) VALUES (?, 'rename', ?, ?, ?, ?, 0, ?)",
                [(result.job_id, m.old, m.new, node_id, node_id, ts) for m, node_id in local])
            result.items = len(local)
        for old, new in plan.dir_moves:   # directories made by the user move at once
            vdirs.move(conn, old, new)
            priority.move(conn, old, new)   # and their update priority
            schedules.rewrite_paths(conn, old, new)   # and the scheduled syncs of it
            for peer in peers.paired_peers(conn):     # schedules live on the server owning the backup
                index.queue_peer_call(conn, peer["id"], "/api/peer/dir-moved",
                                      {"old": old, "new": new})
        for fid, node_id, _ in marks:
            hash_ = conn.execute("SELECT hash FROM copies WHERE file_id = ? AND node_id = ?",
                                 (fid, node_id)).fetchone()
            index.mark_copy(conn, fid, node_id, True, hash_[0] if hash_ else None)
            result.marked += 1
    for (owner, node_id), pairs in remote.items():
        label = (conn.execute("SELECT name FROM servers WHERE id = ?", (owner,)).fetchone()
                 or [owner[:8]])[0]
        try:
            out = peers.forward(conn, owner, "POST", "/api/jobs",
                                {"kind": "rename", "node": node_id, "renames": pairs,
                                 "description": description}, timeout=60)
        except peers.PeerError as e:
            result.errors.append(f"{label}: {e}")
            continue
        result.remote_jobs.append((label, out.get("job_id"), int(out.get("items") or 0)))
    return result


def create_rename_job(conn: sqlite3.Connection, node: str, renames: list, description: str
                      ) -> jobs.CreateReport:
    """A job renaming files on one of our nodes (asked for by the server that planned it)."""
    node_row = jobs._owned_node(conn, node)
    pairs = []
    for pair in renames:
        try:
            old, new = paths.normalize(str(pair[0])), paths.normalize(str(pair[1]))
        except (paths.VPathError, IndexError, TypeError):
            raise jobs.JobError("renames must be [from, to] pairs of virtual paths") from None
        if old != new:
            pairs.append((old, new))
    report = jobs.CreateReport(None, 0)
    if not pairs:
        return report
    with db.transaction(conn):
        job_id = jobs._new_job(conn, "wanted", description[:200])
        ts = db.now()
        conn.executemany(
            "INSERT INTO job_items(job_id, op, vpath, new_vpath, src_node, dst_node, size,"
            " updated) VALUES (?, 'rename', ?, ?, ?, ?, 0, ?)",
            [(job_id, o, n, node_row["id"], node_row["id"], ts) for o, n in pairs])
    report.job_id, report.items = job_id, len(pairs)
    return report
