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
import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from . import archives, collections_, db, fsops, index, opaque, paths, priority
from .config import Config, ScanSection
from .control import Stopped, StopFn, check
from .hashing import hash_file
from .nodes import MARKER_DIR, NodeError, marker_path, read_marker

BATCH_FILES = 500
log = logging.getLogger("reifs.scanner")
PARALLEL_MIN = 256 * 1024   # smaller files are hashed in line: handing them to a thread costs more
VERSION_CHUNK = 500
RECONCILE_CHUNK = 500       # vanished (or no longer indexed) copies handled per transaction         # files whose versions are compared per transaction
BATCH_SECONDS = 1.0
REPORT_SECONDS = 0.3        # progress callback at least this often
BIG_FILE = 16 << 20         # announce files from this size on before hashing them
MAX_STORED_ERRORS = 1000


def reserved_dirs(conn: sqlite3.Connection, cfg: Config, node_id: str) -> tuple[str, ...]:
    """Folders at the root of a node that are not part of the virtual tree: the "fetch"
    folder of this server's own node, where fetched files are put. Unless the node
    already tracks files there (a folder of the user's with that name): those stay."""
    packed = tuple(archives.packed_dirs(conn))   # kept as an archive: not tracked loose
    row = conn.execute("SELECT kind FROM nodes WHERE id = ?", (node_id,)).fetchone()
    name = cfg.storage.fetch_dir.strip("/")
    if row is None or row["kind"] != "server" or not name:
        return packed
    cond, args = paths.subtree_sql("/" + name, "d.path")
    # Directory first: starting from the node's copies reads every one of them to find none.
    if conn.execute("SELECT 1 FROM dirs d CROSS JOIN files f ON f.dir_id = d.id"
                    " CROSS JOIN copies c ON c.file_id = f.id AND c.node_id = ?"
                    f" WHERE {cond} LIMIT 1", (node_id, *args)).fetchone():
        return packed
    return ("/" + name, *packed)


class Ignorer:
    def __init__(self, scan: ScanSection, reserved: tuple[str, ...] = (),
                 opaque_dirs: tuple[str, ...] = ()):
        self.reserved = set(reserved)
        self.opaque = set(opaque_dirs)    # each is ONE row; what is inside is not indexed
        self.hidden_dirs = scan.ignore_hidden_dirs
        self.name_patterns = [p.casefold() for p in scan.ignore if "/" not in p]
        self.path_patterns = [p.casefold() for p in scan.ignore if "/" in p]

    def ignored(self, name: str, vpath: str, is_dir: bool) -> bool:
        if is_dir and vpath in self.reserved:
            return True
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
            inside = i < len(parts) - 1
            if inside and current in self.opaque:
                return True               # a file inside an opaque directory: not indexed
            if self.ignored(part, current, is_dir=inside):
                return True
        return False


@dataclass
class ScanStats:
    node: str = ""
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
    skipped_dirs: int = 0    # directories left out: not due for an automatic scan


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


_COPY_COLUMNS = ("SELECT f.vpath, c.file_id, c.size, c.mtime_ns, c.hash, c.state, c.marked_delete"
                 " FROM copies c JOIN vfiles f ON f.id = c.file_id WHERE c.node_id = ?")
# The same for the files below one directory: it starts from the directories (an index range),
# not from every copy of the node, which with half a million files is most of a scan's cost.
_DIR_COPY_COLUMNS = ("SELECT d.path || '/' || f.name AS vpath, c.file_id, c.size, c.mtime_ns,"
                     " c.hash, c.state, c.marked_delete FROM dirs d CROSS JOIN files f"
                     " ON f.dir_id = d.id CROSS JOIN copies c ON c.file_id = f.id"
                     " AND c.node_id = ? WHERE ")


def _known_copies(conn: sqlite3.Connection, node_id: str, classes: set[int] | None,
                  prefs: priority.Prefs, schedule: priority.Schedule) -> dict[str, sqlite3.Row]:
    """The index's copies on a node that this scan looks at, by path.

    A scan of everything reads them all. An automatic scan of some update priorities (a
    synchronous directory every few seconds) must not: with millions of files indexed it
    would read and sift all of them to look at a few, and keep a core busy for ever. It
    reads the directories (and files) whose own setting is one of those priorities, below
    which everything is that priority unless a nearer setting says otherwise."""
    if classes is None or prefs.effective("/") in classes:
        rows = conn.execute(_COPY_COLUMNS, (node_id,))
    else:
        rows = []
        for vpath, prio in prefs.prefs.items():
            if prio not in classes:
                continue
            fid = index.file_id(conn, vpath)
            if fid is not None:                                  # a file with a setting of its own
                rows += conn.execute(_COPY_COLUMNS + " AND c.file_id = ?", (node_id, fid)).fetchall()
            cond, args = paths.subtree_sql(vpath, "d.path")
            rows += conn.execute(_DIR_COPY_COLUMNS + cond, (node_id, *args)).fetchall()
    return {r["vpath"]: r for r in rows
            if classes is None or schedule.covers_file(r["vpath"])}   # only what this scan visits


class _Walker:
    def __init__(self, root: Path, ignorer: Ignorer, schedule: priority.Schedule | None = None,
                 skipped: list[str] | None = None):
        self.root = root
        self.ignorer = ignorer
        self.schedule = schedule or priority.Schedule(priority.Prefs({}), None)
        self.skipped = skipped if skipped is not None else []
        self.errors: list[tuple[str, str]] = []
        self.error_paths: set[str] = set()
        self.unreadable: list[str] = []  # vdir prefixes

    def _error(self, vpath: str, msg: str) -> None:
        self.errors.append((vpath, msg))
        self.error_paths.add(vpath)

    def walk(self) -> Iterator[tuple[str, str, int, int]]:
        """Yield ``(vpath, physical path, size, mtime_ns)`` for every indexed file."""
        prefs = self.schedule.prefs
        stack: list[tuple[str, str, int]] = [(str(self.root), "", prefs.effective("/"))]
        while stack:
            directory, vdir, prio = stack.pop()
            wanted = self.schedule.wants(prio)
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
                        if vpath in self.ignorer.opaque:
                            # one row for the whole directory (the loop reads its fingerprint)
                            if self.schedule.wants(prefs.prefs.get(vpath, prio)) \
                                    and not marker_path(entry.path).exists():
                                yield vpath, entry.path, None, None
                            continue
                        if self.ignorer.ignored(name, vpath, is_dir=True):
                            continue
                        if marker_path(entry.path).exists():
                            self.errors.append((vpath, "nested node root; skipped"))
                            continue
                        child_prio = prefs.prefs.get(vpath, prio)
                        if self.schedule.enter(vpath, child_prio):
                            subdirs.append((entry.path, vpath, child_prio))
                        else:
                            self.skipped.append(vpath)   # not due: not even read
                    elif entry.is_file(follow_symlinks=False):
                        if not wanted or self.ignorer.ignored(name, vpath, is_dir=False):
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
        if not self.schedule.covers_file(vpath):
            return True    # in a directory this scan did not visit
        return any(vpath.startswith(prefix + "/") for prefix in self.unreadable)


def scan_node(
    conn: sqlite3.Connection,
    cfg: Config,
    node_id: str,
    root: Path,
    *,
    verify: bool = False,
    progress: ProgressFn | None = None,
    stop: StopFn | None = None,
    classes: set[int] | None = None,
    new_only: bool = False,
) -> ScanResult:
    """Scan a node. ``classes``: only the directories of these update priorities
    (an automatic scan); None scans everything (one the user asked for).

    ``new_only`` only discovers files the index does not know yet: nothing
    already indexed is looked at again (no re-hashing of changed files, and
    nothing is reported missing or renamed), so it is the cheapest scan.
    """
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

    started_at = time.time()
    stats = ScanStats(node=node["name"])
    ignorer = Ignorer(cfg.scan, reserved_dirs(conn, cfg, node_id), tuple(opaque.roots(conn)))
    prefs = priority.Prefs(priority.load(conn))
    schedule = priority.Schedule(prefs, classes)
    walker = _Walker(root, ignorer, schedule)
    existing = _known_copies(conn, node_id, classes, prefs, schedule)
    seen: set[str] = set()
    touched: set[int] = set()
    coll: dict[str, tuple[int, int]] = {}      # opaque directories read: files, bytes

    def finish(status: str) -> None:
        with db.transaction(conn):
            conn.execute(
                "UPDATE scans SET finished = ?, status = ?, stats = ?, errors = ? WHERE id = ?",
                (db.now(), status, json.dumps(asdict(stats)),
                 json.dumps(walker.errors[:MAX_STORED_ERRORS]), scan_id),
            )

    # --- phase 1: walk, add and update copies --------------------------------
    # Hashing (and directory reading) happens with NO database transaction
    # open: reading a huge file or a stalled disk takes minutes and would block
    # every other writer (the API, the sync thread) for that long. Results
    # are collected and written in short batches.
    batch: list[tuple] = []
    last_flush = last_report = time.monotonic()

    def flush(current: str = "") -> None:
        nonlocal last_flush
        if batch:
            with db.transaction(conn, "scan: saving results"):
                for args in batch:
                    _process_file(conn, node_id, scan_id, *args, stats, touched)
                    if args[0] in coll:
                        fid = index.file_id(conn, args[0])
                        if fid is not None:
                            collections_.record_stats(conn, fid, node_id, *coll.pop(args[0]))
            batch.clear()
            if progress:
                progress(stats, current)
        last_flush = last_report = time.monotonic()

    threads = max(1, int(cfg.scan.hash_threads))
    pool = ThreadPoolExecutor(threads, thread_name_prefix="reifs-hash") if threads > 1 else None
    pending: deque = deque()          # (future, vpath, size, mtime_ns, row): hashes being read

    def drain(limit: int) -> None:
        """Take the finished hashes, in the order the files were met, until at most ``limit``
        are still pending."""
        while len(pending) > limit:
            fut, vp, sz, mt, rw = pending.popleft()
            try:
                digest = fut.result()
            except OSError as e:
                walker._error(vp, f"cannot hash: {e.strerror or e}")
                continue
            stats.hashed += 1
            batch.append((vp, sz, mt, rw, digest))

    try:
        for vpath, phys, size, mtime_ns in walker.walk():
            check(stop)
            stats.seen += 1
            seen.add(vpath)
            row = existing.get(vpath)
            if size is None:        # an opaque directory: one row for all that is in it
                if new_only and row is not None:
                    continue
                if progress:
                    progress(stats, f"reading {vpath}")
                fp = collections_.fingerprint(
                    phys, stop, lambda n, v=vpath: progress and progress(stats, f"reading {v}: {n:,} files"))
                if row is not None and row["state"] == "present" and row["hash"] == fp.hash \
                        and not verify:
                    continue
                coll[vpath] = (fp.files, fp.bytes)
                stats.hashed += 1
                batch.append((vpath, fp.bytes, fp.newest_mtime_ns, row, fp.hash))
                if len(batch) >= BATCH_FILES:
                    flush(vpath)
                continue
            if new_only and row is not None:
                continue
            if row is not None and not verify and row["size"] == size \
                    and row["mtime_ns"] == mtime_ns:
                if row["state"] == "missing":
                    batch.append((vpath, size, mtime_ns, row, None))   # it came back
                h = None
            else:
                if progress and size >= BIG_FILE:   # a long silent read: say so
                    progress(stats, f"hashing {vpath} ({size / 2**20:,.0f} MiB)")
                if pool is not None and size >= PARALLEL_MIN:   # read by several threads at once
                    pending.append((pool.submit(hash_file, phys), vpath, size, mtime_ns, row))
                    drain(threads * 16)
                else:
                    try:
                        h = hash_file(phys)
                    except OSError as e:
                        walker._error(vpath, f"cannot hash: {e.strerror or e}")
                        continue
                    stats.hashed += 1
                    batch.append((vpath, size, mtime_ns, row, h))
            if len(batch) >= BATCH_FILES or (batch and time.monotonic() - last_flush > BATCH_SECONDS):
                flush(vpath)
            elif progress and time.monotonic() - last_report > REPORT_SECONDS:
                progress(stats, vpath)
        drain(0)
        flush()
    except BaseException as exc:
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        try:
            flush()                      # keep the progress made so far
        except sqlite3.Error:
            pass
        finish("aborted" if isinstance(exc, (KeyboardInterrupt, Stopped)) else "failed")
        raise
    if pool is not None:
        pool.shutdown()

    if new_only:
        with db.transaction(conn, "scan: saving results"):
            for fid in touched:
                if conn.execute("SELECT 1 FROM files WHERE id = ?", (fid,)).fetchone():
                    index.evaluate_divergence(conn, fid)
        finish("complete")
        if progress:
            progress(stats, "")
        return ScanResult(node["name"], scan_id, "complete", stats, walker.errors)

    # --- phase 2: vanished copies, renames, pruning ---------------------------
    stats.skipped_dirs = len(walker.skipped)
    vanished, to_forget = _find_vanished(root, existing, seen, walker, ignorer)
    # Done in pieces, each its own short transaction (thousands of files can go at once: a
    # packed directory, a deleted folder).
    prune = _reconcile_vanished(conn, node, existing, vanished, to_forget, stats, touched,
                                root, cfg.scan.group_decisions_min)
    # Looking at the versions of every file the scan touched (all of them, on a first scan) is
    # done a few hundred at a time: it is the long part, and the lock must not be held for it.
    pending = sorted(touched)
    for i in range(0, len(pending), VERSION_CHUNK):
        check(stop)
        with db.transaction(conn, "scan: checking versions"):
            for fid in pending[i:i + VERSION_CHUNK]:
                if conn.execute("SELECT 1 FROM files WHERE id = ?", (fid,)).fetchone():
                    index.evaluate_divergence(conn, fid)
    with db.transaction(conn, "scan: finishing"):
        conn.execute(
            "UPDATE nodes SET last_scan = ?, last_scan_id = ?, online = 1, last_seen = ?"
            " WHERE id = ?",
            (db.now(), scan_id, db.now(), node_id),
        )
        priority.record_scan(conn, node_id, priority.CLASSES if classes is None else classes,
                             started_at)
    for vdir in sorted(prune, key=lambda p: -p.count("/")):
        stats.pruned_dirs += fsops.prune(root, vdir)   # filesystem only, no lock held
    finish("complete")
    if progress:
        progress(stats, "")
    return ScanResult(node["name"], scan_id, "complete", stats, walker.errors)


def _process_file(conn, node_id, scan_id, vpath, size, mtime_ns, row, h, stats, touched) -> None:
    """Write one observed file to the index (inside a transaction).

    ``h`` is its hash, computed beforehand without holding the lock; None
    means the file is unchanged and only a copy marked missing came back.
    """
    if h is None:
        conn.execute(
            "UPDATE copies SET state = 'present' WHERE file_id = ? AND node_id = ?",
            (row["file_id"], node_id),
        )
        index.log_change(conn, node_id, vpath, "upsert", size, mtime_ns, row["hash"],
                         fid=row["file_id"])
        index.obsolete_decisions(conn, row["file_id"], ("missing",), node_id)
        stats.reappeared += 1
        touched.add(row["file_id"])
        return
    ts = db.now()

    if row is None:
        fid = index.ensure_file(conn, vpath)
        conn.execute(
            "INSERT INTO copies(file_id, node_id, size, mtime_ns, hash_b, first_seen,"
            " first_scan, hashed_at) VALUES (?, ?, ?, ?, hb(?), ?, ?, ?)",
            (fid, node_id, size, mtime_ns, h, ts, scan_id, ts),
        )
        index.log_change(conn, node_id, vpath, "upsert", size, mtime_ns, h, fid=fid)
        stats.added += 1
        touched.add(fid)
        return

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
        "UPDATE copies SET size = ?, mtime_ns = ?, hash_b = hb(?), state = 'present',"
        # A changed copy is no longer the verified duplicate a move left
        # behind, so deleting it could lose data: drop the deletion mark.
        " marked_delete = CASE WHEN ? THEN 0 ELSE marked_delete END,"
        " hashed_at = ? WHERE file_id = ? AND node_id = ?",
        (size, mtime_ns, h, int(content_changed), ts, fid, node_id),
    )
    if content_changed or row["state"] == "missing" or (row["size"], row["mtime_ns"]) != (size, mtime_ns):
        index.log_change(conn, node_id, vpath, "upsert", size, mtime_ns, h, fid=fid)
        touched.add(fid)


def _find_vanished(root, existing, seen, walker, ignorer) -> tuple[list[str], list[str]]:
    """Which tracked copies are gone, and which are still there but no longer
    indexed (now ignored, a symlink, inside a nested node)?

    Only reads the filesystem, so it runs before the write transaction is
    opened: a slow or stalled disk must never hold the database lock.
    """
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
    to_forget: list[str] = []
    for vpath in existing:
        if vpath in seen or walker.protected(vpath):
            continue
        exists = exact_exists(vpath)
        if exists is False:
            vanished.append(vpath)
        elif exists:
            phys = paths.to_physical(root, vpath)
            if os.path.islink(phys) or ignorer.covers(vpath) or under_nested_node(vpath):
                to_forget.append(vpath)
    return vanished, to_forget


def _reconcile_vanished(conn, node, existing, vanished, to_forget, stats, touched,
                        root: Path | None = None, group_min: int = 0) -> set[str]:
    """Update the index for vanished copies (inside a transaction, database
    work only). Returns the virtual directories to prune afterwards."""
    node_id = node["id"]
    started = time.monotonic()
    for i in range(0, len(to_forget), RECONCILE_CHUNK):
        with db.transaction(conn, "scan: forgetting files that are no longer indexed"):
            for vpath in to_forget[i:i + RECONCILE_CHUNK]:
                # Still on disk but not indexed any more: stop tracking it quietly.
                index.forget_copy(conn, existing[vpath]["file_id"], node_id)
                stats.forgotten += 1

    # Rename candidates: copies that appeared since the last completed scan (only worth
    # looking for when something vanished: on a first scan that is every file of the node).
    pool: dict[tuple[str, int], _Candidates] = {}
    for r in () if not vanished else conn.execute(
        "SELECT f.vpath, c.file_id, c.hash, c.size FROM copies c JOIN vfiles f"
        " ON f.id = c.file_id WHERE c.node_id = ? AND c.first_scan > ? AND c.state = 'present'",
        (node_id, node["last_scan_id"] or 0),
    ):
        pool.setdefault((r["hash"], r["size"]), _Candidates()).add(r["vpath"], r["file_id"])

    prune: set[str] = set()
    new_missing: list[tuple[int, str]] = []
    new_renames: list[int] = []
    for i in range(0, len(vanished), RECONCILE_CHUNK):
        with db.transaction(conn, "scan: reconciling changes"):
            for vpath in vanished[i:i + RECONCILE_CHUNK]:
                _reconcile_one(conn, node_id, existing[vpath], vpath, pool, stats, touched,
                               prune, new_missing, new_renames)
    with db.transaction(conn, "scan: grouping decisions"):
        _group_missing(conn, node_id, root, new_missing, group_min)
        _group_renames(conn, node_id, new_renames, group_min)
    took = time.monotonic() - started
    if took > 5:
        log.info("scan: reconciled %d vanished and %d no longer indexed copies of %s in %.1f s",
                 len(vanished), len(to_forget), node["name"], took)
    return prune


class _Candidates:
    """The new copies with one content: the one with the same file name is preferred when a
    vanished copy is matched to one. Taking is O(1) however many files are identical (thousands
    of empty or boilerplate files were a quadratic search)."""

    def __init__(self) -> None:
        self.order: list[tuple[str, int]] = []
        self.by_name: dict[str, deque[tuple[str, int]]] = {}
        self.used: set[tuple[str, int]] = set()
        self.next = 0

    def add(self, vpath: str, fid: int) -> None:
        c = (vpath, fid)
        self.order.append(c)
        self.by_name.setdefault(paths.basename(vpath), deque()).append(c)

    def take(self, name: str) -> tuple[str, int] | None:
        same = self.by_name.get(name)
        while same:
            c = same.popleft()
            if c not in self.used:
                self.used.add(c)
                return c
        while self.next < len(self.order):
            c = self.order[self.next]
            self.next += 1
            if c not in self.used:
                self.used.add(c)
                return c
        return None


def _reconcile_one(conn, node_id: str, row, vpath: str, pool, stats, touched, prune,
                   new_missing, new_renames) -> None:
    """One copy that is no longer on the disk."""
    fid = row["file_id"]
    prune.add(paths.parent(vpath))
    if row["marked_delete"]:
        index.forget_copy(conn, fid, node_id)  # the user deleted it as asked
        stats.deleted_marked += 1
        return
    if row["state"] == "missing":
        return
    candidates = pool.get((row["hash"], row["size"]))
    pick = candidates.take(paths.basename(vpath)) if candidates else None
    if pick is not None:
        rid = _handle_rename(conn, node_id, fid, vpath, pick[1], pick[0])
        if rid is not None:
            new_renames.append(rid)
        stats.renamed += 1
        return
    conn.execute(
        "UPDATE copies SET state = 'missing' WHERE file_id = ? AND node_id = ?",
        (fid, node_id),
    )
    index.log_change(conn, node_id, vpath, "missing", fid=fid)
    others = conn.execute(
        "SELECT COUNT(*) FROM copies WHERE file_id = ? AND node_id != ? AND state = 'present'",
        (fid, node_id),
    ).fetchone()[0]
    did = index.add_decision(conn, "missing", fid, node_id,
                             {"vpath": vpath, "other_copies": others})
    new_missing.append((did, vpath))
    stats.missing += 1
    touched.add(fid)


def _handle_rename(conn, node_id: str, old_fid: int, old_vpath: str,
                   new_fid: int, new_vpath: str) -> int | None:
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
        return index.add_decision(conn, "rename", old_fid, node_id,
                                  {"from": old_vpath, "to": new_vpath, "nodes": others})
    return None


def _group_missing(conn, node_id: str, root: Path | None, new_missing, group_min: int) -> None:
    """Files that vanished together are one decision: a folder that is gone (the
    topmost vanished folder), or many files gone from one folder."""
    if not new_missing or root is None or group_min <= 0:
        return
    exists: dict[str, bool] = {}

    def is_dir(vdir: str) -> bool:
        if vdir not in exists:
            exists[vdir] = os.path.isdir(paths.to_physical(root, vdir))
        return exists[vdir]

    entries = []
    for did, vpath in new_missing:
        parts = paths.split(vpath)
        key, info = None, {}
        for i in range(1, len(parts)):
            top = "/" + "/".join(parts[:i])
            if not is_dir(top):
                key, info = "gone:" + top, {"dir": top, "mode": "gone"}
                break
        if key is None:
            parent = paths.parent(vpath)
            key, info = "in:" + parent, {"dir": parent, "mode": "in"}
        entries.append((did, key, info))
    index.group_decisions(conn, "missing", node_id, entries, group_min)


def moved_prefixes(old: str, new: str) -> tuple[str, str]:
    """The folder pair a rename implies: what is left of both paths once the
    trailing parts they share are removed (/a/b/x.txt -> /n/b/x.txt is /a -> /n)."""
    a, b = paths.split(old), paths.split(new)
    while len(a) > 1 and len(b) > 1 and a[-1] == b[-1]:
        a.pop()
        b.pop()
    return "/" + "/".join(a), "/" + "/".join(b)


def _group_renames(conn, node_id: str, new_renames: list[int], group_min: int) -> None:
    if not new_renames or group_min <= 0:
        return
    entries = []
    for did in new_renames:
        d = json.loads(conn.execute("SELECT data FROM decisions WHERE id = ?", (did,)).fetchone()[0])
        src, dst = moved_prefixes(d["from"], d["to"])
        entries.append((did, f"moved:{src}>{dst}", {"from": src, "to": dst, "mode": "moved"}))
    index.group_decisions(conn, "rename", node_id, entries, group_min)


def scan_all(
    conn: sqlite3.Connection, cfg: Config, names: list[str] | None = None, *,
    verify: bool = False, progress: ProgressFn | None = None, stop: StopFn | None = None,
    full: bool = True, new_only: bool = False,
):
    """Discover owned nodes, then scan the online ones (optionally filtered).

    ``full=False`` is an automatic scan: each node only gets the directories
    whose update priority is due, and a node with none due is left alone.
    """
    from .nodes import discover

    disc = discover(conn, cfg)
    results: list[ScanResult] = []
    wanted = set(names or [])
    for loc in disc.online:
        if wanted and loc.name not in wanted and loc.node_id not in wanted:
            continue
        classes = None
        if not full:
            classes = priority.due_classes(conn, cfg, loc.node_id,
                                           priority.Prefs(priority.load(conn)))
            if not classes:
                continue
        results.append(scan_node(conn, cfg, loc.node_id, loc.root,
                                 verify=verify, progress=progress, stop=stop, classes=classes,
                                 new_only=new_only))
    return disc, results
