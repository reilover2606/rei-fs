"""Scanning a node root and reconciling it with the index.

See docs/DESIGN.md section 4. Safety properties:

* A node is only scanned when its marker is present and matches, so an empty
  mount point never makes every file look deleted.
* New/changed copies are committed in batches while walking; vanished copies
  are only processed after a walk that completed, so an interrupted scan
  never reports false "missing" copies.
* Files under unreadable directories, or that failed to stat/hash, are never
  treated as vanished.
* The only filesystem modification is ``rmdir`` of directories that held a
  vanished tracked file and are now empty (``rmdir`` cannot remove anything
  non-empty). Files are never deleted.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from . import db, fsops, index, paths
from .config import Config, ScanSection
from .hashing import hash_file
from .nodes import MARKER_DIR, NodeError, marker_path, read_marker

BATCH_FILES = 500
BATCH_SECONDS = 5.0
MAX_STORED_ERRORS = 1000


class Ignorer:
    def __init__(self, scan: ScanSection):
        self.hidden_dirs = scan.ignore_hidden_dirs
        self.name_patterns = [p.casefold() for p in scan.ignore if "/" not in p]
        self.path_patterns = [p.casefold() for p in scan.ignore if "/" in p]

    def ignored(self, name: str, vpath: str, is_dir: bool) -> bool:
        if is_dir and (name == MARKER_DIR or (self.hidden_dirs and name.startswith("."))):
            return True
        n = name.casefold()
        if any(fnmatch.fnmatchcase(n, p) for p in self.name_patterns):
            return True
        v = vpath.casefold()
        return any(fnmatch.fnmatchcase(v, p) for p in self.path_patterns)

    def covers(self, vpath: str) -> bool:
        """Would a file at vpath (or one of its directories) be ignored?"""
        parts = paths.split(vpath)
        current = ""
        for i, part in enumerate(parts):
            current += "/" + part
            if self.ignored(part, current, is_dir=i < len(parts) - 1):
                return True
        return False


@dataclass
class ScanStats:
    seen: int = 0
    hashed: int = 0
    added: int = 0
    changed: int = 0
    reappeared: int = 0
    missing: int = 0
    renamed: int = 0
    forgotten: int = 0
    deleted_marked: int = 0
    verify_mismatch: int = 0
    pruned_dirs: int = 0


@dataclass
class ScanResult:
    node_name: str
    scan_id: int
    status: str
    stats: ScanStats
    errors: list[tuple[str, str]] = field(default_factory=list)


ProgressFn = Callable[[ScanStats, str], None]


def _is_junction(entry: os.DirEntry) -> bool:
    fn = getattr(entry, "is_junction", None)  # Python 3.12+
    return bool(fn and fn())


class _Walker:
    def __init__(self, root: Path, ignorer: Ignorer):
        self.root = root
        self.ignorer = ignorer
        self.errors: list[tuple[str, str]] = []
        self.error_paths: set[str] = set()
        self.unreadable: list[str] = []  # vdir prefixes

    def _error(self, vpath: str, msg: str) -> None:
        self.errors.append((vpath, msg))
        self.error_paths.add(vpath)

    def walk(self) -> Iterator[tuple[str, str, int, int]]:
        """Yield ``(vpath, physical path, size, mtime_ns)`` for every indexed file."""
        stack: list[tuple[str, str]] = [(str(self.root), "")]
        while stack:
            directory, vdir = stack.pop()
            try:
                with os.scandir(directory) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError as e:
                self._error(vdir or "/", f"cannot read directory: {e.strerror or e}")
                self.unreadable.append(vdir)
                continue
            subdirs = []
            for entry in entries:
                name = entry.name
                vpath = f"{vdir}/{name}"
                try:
                    name.encode("utf-8")
                except UnicodeEncodeError:
                    self.errors.append((vpath.encode("utf-8", "replace").decode(),
                                        "file name is not valid UTF-8; skipped"))
                    continue
                try:
                    if entry.is_symlink() or _is_junction(entry):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if self.ignorer.ignored(name, vpath, is_dir=True):
                            continue
                        if marker_path(entry.path).exists():
                            self.errors.append((vpath, "nested node root; skipped"))
                            continue
                        subdirs.append((entry.path, vpath))
                    elif entry.is_file(follow_symlinks=False):
                        if self.ignorer.ignored(name, vpath, is_dir=False):
                            continue
                        st = entry.stat(follow_symlinks=False)
                        yield vpath, entry.path, st.st_size, st.st_mtime_ns
                except OSError as e:
                    self._error(vpath, e.strerror or str(e))
            stack.extend(reversed(subdirs))

    def protected(self, vpath: str) -> bool:
        """True if we could not observe vpath, so it must not count as vanished."""
        if vpath in self.error_paths:
            return True
        return any(vpath.startswith(prefix + "/") for prefix in self.unreadable)


def scan_node(
    conn: sqlite3.Connection,
    cfg: Config,
    node_id: str,
    root: Path,
    *,
    verify: bool = False,
    progress: ProgressFn | None = None,
) -> ScanResult:
    node = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if node is None:
        raise NodeError(f"unknown node {node_id}")
    marker = read_marker(root)
    if marker is None or marker.node_id != node_id:
        raise NodeError(
            f"{root} does not hold the marker of node {node['name']!r}; refusing to scan"
        )

    with db.transaction(conn):
        scan_id = conn.execute(
            "INSERT INTO scans(node_id, started, status) VALUES (?, ?, 'running')",
            (node_id, db.now()),
        ).lastrowid

    stats = ScanStats()
    ignorer = Ignorer(cfg.scan)
    walker = _Walker(root, ignorer)
    existing: dict[str, sqlite3.Row] = {
        r["vpath"]: r
        for r in conn.execute(
            "SELECT f.vpath, c.file_id, c.size, c.mtime_ns, c.hash, c.state, c.marked_delete"
            " FROM copies c JOIN files f ON f.id = c.file_id WHERE c.node_id = ?",
            (node_id,),
        )
    }
    seen: set[str] = set()
    touched: set[int] = set()

    def finish(status: str) -> None:
        with db.transaction(conn):
            conn.execute(
                "UPDATE scans SET finished = ?, status = ?, stats = ?, errors = ? WHERE id = ?",
                (db.now(), status, json.dumps(asdict(stats)),
                 json.dumps(walker.errors[:MAX_STORED_ERRORS]), scan_id),
            )

    # --- phase 1: walk, add and update copies --------------------------------
    conn.execute("BEGIN IMMEDIATE")
    pending, last_commit = 0, time.monotonic()
    try:
        for vpath, phys, size, mtime_ns in walker.walk():
            stats.seen += 1
            seen.add(vpath)
            if _process_file(conn, node_id, scan_id, vpath, phys, size, mtime_ns,
                             existing.get(vpath), verify, stats, touched, walker):
                pending += 1
            if pending >= BATCH_FILES or time.monotonic() - last_commit > BATCH_SECONDS:
                conn.execute("COMMIT")
                if progress:
                    progress(stats, vpath)
                conn.execute("BEGIN IMMEDIATE")
                pending, last_commit = 0, time.monotonic()
        conn.execute("COMMIT")
    except BaseException as exc:
        if conn.in_transaction:
            try:
                conn.execute("COMMIT")  # keep the progress made so far
            except sqlite3.Error:
                conn.execute("ROLLBACK")
        finish("aborted" if isinstance(exc, KeyboardInterrupt) else "failed")
        raise

    # --- phase 2: vanished copies, renames, pruning ---------------------------
    with db.transaction(conn):
        _reconcile_vanished(conn, node, root, scan_id, existing, seen, walker,
                            ignorer, stats, touched)
        for fid in touched:
            if conn.execute("SELECT 1 FROM files WHERE id = ?", (fid,)).fetchone():
                index.evaluate_divergence(conn, fid)
        conn.execute(
            "UPDATE nodes SET last_scan = ?, last_scan_id = ?, online = 1, last_seen = ?"
            " WHERE id = ?",
            (db.now(), scan_id, db.now(), node_id),
        )
    finish("complete")
    if progress:
        progress(stats, "")
    return ScanResult(node["name"], scan_id, "complete", stats, walker.errors)


def _process_file(
    conn, node_id, scan_id, vpath, phys, size, mtime_ns, row, verify, stats, touched, walker,
) -> bool:
    """Reconcile one observed file. Returns True if the database was written."""
    if row is not None and not verify and row["size"] == size and row["mtime_ns"] == mtime_ns:
        if row["state"] == "missing":
            conn.execute(
                "UPDATE copies SET state = 'present' WHERE file_id = ? AND node_id = ?",
                (row["file_id"], node_id),
            )
            index.log_change(conn, node_id, vpath, "upsert", size, mtime_ns, row["hash"])
            index.obsolete_decisions(conn, row["file_id"], ("missing",), node_id)
            stats.reappeared += 1
            touched.add(row["file_id"])
            return True
        return False

    try:
        h = hash_file(phys)
    except OSError as e:
        walker._error(vpath, f"cannot hash: {e.strerror or e}")
        return False
    stats.hashed += 1
    ts = db.now()

    if row is None:
        fid = index.ensure_file(conn, vpath)
        conn.execute(
            "INSERT INTO copies(file_id, node_id, size, mtime_ns, hash, first_seen,"
            " first_scan, hashed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (fid, node_id, size, mtime_ns, h, ts, scan_id, ts),
        )
        index.log_change(conn, node_id, vpath, "upsert", size, mtime_ns, h)
        stats.added += 1
        touched.add(fid)
        return True

    fid = row["file_id"]
    content_changed = h != row["hash"]
    if content_changed and row["size"] == size and row["mtime_ns"] == mtime_ns:
        stats.verify_mismatch += 1  # same size and mtime, different bytes: corruption?
    if content_changed:
        stats.changed += 1
    if row["state"] == "missing":
        stats.reappeared += 1
        index.obsolete_decisions(conn, fid, ("missing",), node_id)
    conn.execute(
        "UPDATE copies SET size = ?, mtime_ns = ?, hash = ?, state = 'present',"
        # A changed copy is no longer the verified duplicate a move left
        # behind, so deleting it could lose data: drop the deletion mark.
        " marked_delete = CASE WHEN ? THEN 0 ELSE marked_delete END,"
        " hashed_at = ? WHERE file_id = ? AND node_id = ?",
        (size, mtime_ns, h, int(content_changed), ts, fid, node_id),
    )
    if content_changed or row["state"] == "missing" or (row["size"], row["mtime_ns"]) != (size, mtime_ns):
        index.log_change(conn, node_id, vpath, "upsert", size, mtime_ns, h)
        touched.add(fid)
    return True


def _reconcile_vanished(conn, node, root, scan_id, existing, seen, walker, ignorer,
                        stats, touched) -> None:
    node_id = node["id"]
    listings: dict[str, set[str] | None] = {}

    def exact_exists(vpath: str) -> bool | None:
        """Does a directory entry with exactly this name exist? None = unknown.

        Uses the parent's listing rather than stat so that a case-only rename
        on a case-insensitive filesystem is seen as a rename.
        """
        parent = paths.parent(vpath)
        if parent not in listings:
            try:
                listings[parent] = set(os.listdir(paths.to_physical(root, parent)))
            except (FileNotFoundError, NotADirectoryError):
                listings[parent] = set()
            except OSError:
                listings[parent] = None
        names = listings[parent]
        return None if names is None else paths.basename(vpath) in names

    def under_nested_node(vpath: str) -> bool:
        current = root
        for part in paths.split(vpath)[:-1]:
            current = current / part
            if marker_path(current).exists():
                return True
        return False

    vanished: list[str] = []
    for vpath in existing:
        if vpath in seen or walker.protected(vpath):
            continue
        exists = exact_exists(vpath)
        if exists is False:
            vanished.append(vpath)
        elif exists:
            # Still on disk but not indexed: now ignored, a symlink, or inside
            # a nested node. Stop tracking it without raising a decision.
            phys = paths.to_physical(root, vpath)
            if os.path.islink(phys) or ignorer.covers(vpath) or under_nested_node(vpath):
                index.forget_copy(conn, existing[vpath]["file_id"], node_id)
                stats.forgotten += 1

    # Rename candidates: copies that appeared since the last completed scan.
    pool: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for r in conn.execute(
        "SELECT f.vpath, c.file_id, c.hash, c.size FROM copies c JOIN files f"
        " ON f.id = c.file_id WHERE c.node_id = ? AND c.first_scan > ? AND c.state = 'present'",
        (node_id, node["last_scan_id"] or 0),
    ):
        pool.setdefault((r["hash"], r["size"]), []).append((r["vpath"], r["file_id"]))

    prune: set[str] = set()
    for vpath in vanished:
        row = existing[vpath]
        fid = row["file_id"]
        prune.add(paths.parent(vpath))
        if row["marked_delete"]:
            index.forget_copy(conn, fid, node_id)  # the user deleted it as asked
            stats.deleted_marked += 1
            continue
        if row["state"] == "missing":
            continue
        candidates = pool.get((row["hash"], row["size"]))
        if candidates:
            base = paths.basename(vpath)
            pick = next((c for c in candidates if paths.basename(c[0]) == base), candidates[0])
            candidates.remove(pick)
            _handle_rename(conn, node_id, fid, vpath, pick[1], pick[0])
            stats.renamed += 1
            continue
        conn.execute(
            "UPDATE copies SET state = 'missing' WHERE file_id = ? AND node_id = ?",
            (fid, node_id),
        )
        index.log_change(conn, node_id, vpath, "missing")
        others = conn.execute(
            "SELECT COUNT(*) FROM copies WHERE file_id = ? AND node_id != ? AND state = 'present'",
            (fid, node_id),
        ).fetchone()[0]
        index.add_decision(conn, "missing", fid, node_id,
                           {"vpath": vpath, "other_copies": others})
        stats.missing += 1
        touched.add(fid)

    for vdir in sorted(prune, key=lambda p: -p.count("/")):
        stats.pruned_dirs += fsops.prune(root, vdir)


def _handle_rename(conn, node_id: str, old_fid: int, old_vpath: str,
                   new_fid: int, new_vpath: str) -> None:
    index.copy_tags(conn, old_fid, new_fid)  # tags follow the rename
    conn.execute(
        "UPDATE files SET created = MIN(created, (SELECT created FROM files WHERE id = ?))"
        " WHERE id = ?",
        (old_fid, new_fid),
    )
    others = [
        r[0] for r in conn.execute(
            "SELECT node_id FROM copies WHERE file_id = ? AND node_id != ?", (old_fid, node_id)
        )
    ]
    index.forget_copy(conn, old_fid, node_id)
    if others:
        index.add_decision(conn, "rename", old_fid, node_id,
                           {"from": old_vpath, "to": new_vpath, "nodes": others})


def scan_all(
    conn: sqlite3.Connection, cfg: Config, names: list[str] | None = None, *,
    verify: bool = False, progress: ProgressFn | None = None,
):
    """Discover owned nodes, then scan the online ones (optionally filtered)."""
    from .nodes import discover

    disc = discover(conn, cfg)
    results: list[ScanResult] = []
    wanted = set(names or [])
    for loc in disc.online:
        if wanted and loc.name not in wanted and loc.node_id not in wanted:
            continue
        results.append(scan_node(conn, cfg, loc.node_id, loc.root,
                                 verify=verify, progress=progress))
    return disc, results
