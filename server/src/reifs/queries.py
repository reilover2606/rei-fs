"""Read-only queries over the index (tree listing, lookup, search, status)."""

from __future__ import annotations

import fnmatch
import threading
import time
import sqlite3
from dataclasses import dataclass, field

from . import index, paths, vdirs
from .tags import tags_of

DEFAULT_COPIES = 2


@dataclass(frozen=True)
class GoalPolicy:
    """How many good copies a file should have: ``copies``, or more for files
    carrying a tag listed in ``tag_copies`` (the highest goal applies)."""

    copies: int = DEFAULT_COPIES
    tag_copies: tuple[tuple[str, int], ...] = ()

    @classmethod
    def from_config(cls, cfg) -> "GoalPolicy":
        r = cfg.replication
        return cls(max(1, r.copies), tuple(sorted((t, max(1, n)) for t, n in r.tag_copies.items())))

    def goal(self, tags) -> int:
        extra = dict(self.tag_copies)
        return max([self.copies] + [extra[t] for t in tags if t in extra])


DEFAULT_POLICY = GoalPolicy()


@dataclass
class CopyInfo:
    node_id: str
    node_name: str
    online: bool
    size: int
    mtime_ns: int
    hash: str
    state: str
    marked_delete: bool
    current: bool


@dataclass
class FileInfo:
    id: int
    vpath: str
    issues: set[str]
    tags: list[str]
    copies: list[CopyInfo] = field(default_factory=list)
    goal: int = DEFAULT_COPIES

    @property
    def good_copies(self) -> int:
        """Copies that count towards the two-copy goal."""
        return len({c.node_id for c in self.copies
                    if c.state == "present" and c.current and not c.marked_delete})

    @property
    def size(self) -> int:
        cur = [c for c in self.copies if c.current]
        return cur[0].size if cur else 0


def file_info(conn: sqlite3.Connection, fid: int,
              policy: GoalPolicy = DEFAULT_POLICY) -> FileInfo:
    f = conn.execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
    version = index.current_version(conn, fid)
    file_tags = tags_of(conn, fid)
    info = FileInfo(fid, f["vpath"], paths.decode_issues(f["issues"]), file_tags,
                    goal=policy.goal(file_tags))
    for c in conn.execute(
        "SELECT c.*, n.name, n.online FROM copies c JOIN nodes n ON n.id = c.node_id"
        " WHERE c.file_id = ? ORDER BY n.name",
        (fid,),
    ):
        info.copies.append(CopyInfo(
            c["node_id"], c["name"], bool(c["online"]), c["size"], c["mtime_ns"], c["hash"],
            c["state"], bool(c["marked_delete"]),
            current=bool(version and c["state"] == "present" and c["hash"] == version.hash),
        ))
    return info


def lookup(conn: sqlite3.Connection, vpath: str,
           policy: GoalPolicy = DEFAULT_POLICY) -> FileInfo | None:
    fid = index.file_id(conn, paths.normalize(vpath))
    return file_info(conn, fid, policy) if fid is not None else None


@dataclass
class DirEntry:
    name: str
    is_dir: bool
    files: int                 # files at or below this entry
    file: FileInfo | None = None
    here: int = 0              # of them, files with a copy on this server's own node


def list_dir(conn: sqlite3.Connection, vdir: str,
             policy: GoalPolicy = DEFAULT_POLICY) -> list[DirEntry] | None:
    """Immediate children of a virtual directory. None if it does not exist."""
    vdir = paths.normalize(vdir)
    lo, hi = paths.prefix_range(vdir) if vdir != "/" else ("/", "0")
    dirs: dict[str, tuple[int, int]] = {}
    files: list[DirEntry] = []
    me = index.self_id(conn)
    # The counting is done by SQLite: a directory with a million files below it
    # must not be pulled through Python for every refresh of the client.
    # "here" = has a copy on this server's own node.
    start = len(lo) + 1   # 1-based position of the part after the directory
    join = (" LEFT JOIN copies c ON c.file_id = f.id AND c.node_id = ?4 AND c.state = 'present'")
    for head, n, here in conn.execute(
        "SELECT substr(f.vpath, ?1, instr(substr(f.vpath, ?1), '/') - 1) AS head, COUNT(*),"
        " COUNT(c.file_id) FROM files f" + join +
        " WHERE f.vpath >= ?2 AND f.vpath < ?3 AND instr(substr(f.vpath, ?1), '/') > 0"
        " GROUP BY head", (start, lo, hi, me)):
        dirs[head] = (n, here)
    for r in conn.execute(
        "SELECT f.id, f.vpath, c.file_id IS NOT NULL AS here FROM files f" + join +
        " WHERE f.vpath >= ?2 AND f.vpath < ?3 AND instr(substr(f.vpath, ?1), '/') = 0",
        (start, lo, hi, me)):
        files.append(DirEntry(r["vpath"][len(lo):], False, 1, file_info(conn, r["id"], policy),
                              r["here"]))
    # Directories made by the user show even when empty.
    for d in vdirs.under(conn, vdir, include_self=False):
        head = d[len(lo):].partition("/")[0]
        dirs.setdefault(head, (0, 0))
    if not dirs and not files and vdir != "/" and not vdirs.under(conn, vdir):
        return None
    out = [DirEntry(name, True, n, here=h) for name, (n, h) in sorted(dirs.items())]
    return out + sorted(files, key=lambda e: e.name)


def name_pattern(pattern: str | None) -> str | None:
    """The glob to match names (or paths, if it contains "/") against.

    Without wildcards the text means "contains it", ignoring case, so that
    typing ``report`` finds ``Annual Report.pdf``. With ``*``, ``?`` or ``[``
    the pattern is a glob that must match the whole name.
    """
    if not pattern:
        return None
    pat = pattern.casefold()
    if not any(c in pat for c in "*?["):
        pat = f"*{pat}*"
    return pat


def _candidates(conn: sqlite3.Connection, tag: str | None, node: str | None,
                issues_only: bool, under_replicated: bool,
                policy: GoalPolicy) -> list[tuple[int, str]]:
    """``(id, vpath)`` of the files passing every filter except the name, in path order."""
    q = "SELECT DISTINCT f.id, f.vpath FROM files f"
    where, args = [], []
    if tag:
        q += " JOIN tags t ON t.file_id = f.id AND t.present = 1"
        where.append("t.tag = ?")
        args.append(tag)
    if node:
        q += " JOIN copies c ON c.file_id = f.id JOIN nodes n ON n.id = c.node_id"
        where.append("(n.name = ? OR n.id = ?)")
        args += [node, node]
    if issues_only:
        where.append("f.issues != ''")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY f.vpath"
    cov = coverage(conn, policy) if under_replicated else None
    return [(r["id"], r["vpath"]) for r in conn.execute(q, args)
            if cov is None or cov.is_under(r["id"], policy.copies)]


def _name_matches(pat: str, vpath: str) -> bool:
    target = vpath if "/" in pat else paths.basename(vpath)
    return fnmatch.fnmatchcase(target.casefold(), pat)


def search(conn: sqlite3.Connection, pattern: str | None = None, tag: str | None = None,
           node: str | None = None, issues_only: bool = False, under_replicated: bool = False,
           policy: GoalPolicy = DEFAULT_POLICY) -> tuple[list[int], list[tuple[str, int]]]:
    """Matching files (ids, in path order) and matching folders.

    A folder matches when its name (or its path, if the pattern contains a
    "/") matches, like for files. Each folder comes with the number of files
    below it that pass the other filters. Without a pattern there are no
    folders (every folder would match).
    """
    candidates = _candidates(conn, tag, node, issues_only, under_replicated, policy)
    pat = name_pattern(pattern)
    if pat is None:
        return [fid for fid, _ in candidates], []
    ids = [fid for fid, vpath in candidates if _name_matches(pat, vpath)]
    matching: dict[str, bool] = {}
    counts: dict[str, int] = {}
    for _, vpath in candidates:
        d = paths.parent(vpath)
        while d != "/":
            if d not in matching:
                matching[d] = _name_matches(pat, d)
            if matching[d]:
                counts[d] = counts.get(d, 0) + 1
            d = paths.parent(d)
    return ids, sorted(counts.items())


def find_ids(conn: sqlite3.Connection, pattern: str | None = None, tag: str | None = None,
             node: str | None = None, issues_only: bool = False,
             under_replicated: bool = False,
             policy: GoalPolicy = DEFAULT_POLICY) -> list[int]:
    """Ids of matching files, in path order (cheap: no per-file details)."""
    return search(conn, pattern, tag, node, issues_only, under_replicated, policy)[0]


def find(conn: sqlite3.Connection, pattern: str | None = None, tag: str | None = None,
         node: str | None = None, issues_only: bool = False,
         under_replicated: bool = False, policy: GoalPolicy = DEFAULT_POLICY,
         limit: int | None = None) -> list[FileInfo]:
    ids = find_ids(conn, pattern, tag, node, issues_only, under_replicated, policy)
    if limit is not None:
        ids = ids[:limit]
    return [file_info(conn, fid, policy) for fid in ids]


@dataclass
class Status:
    files: int
    bytes: int
    satisfied: int
    under_replicated: int
    single_copy: int
    divergent: int
    missing_copies: int
    marked_delete: int
    non_portable: int
    open_decisions: int
    open_wanted: int


@dataclass
class Coverage:
    """How many good copies each file has, against its goal."""
    bytes: int
    good: dict[int, int]     # file id -> good copies (files with a present copy)
    goals: dict[int, int]
    satisfied: int
    under_replicated: int
    single_copy: int

    def is_under(self, fid: int, default_goal: int) -> bool:
        return self.good.get(fid, 0) < self.goals.get(fid, default_goal)


# Computing coverage reads every copy (about half a second per 100k files),
# and clients poll status every couple of seconds. Everything it depends on
# (copies, deletion marks, tags) is recorded in the change log, so the log's
# last sequence number is a perfect cache key. While a job runs, though, the log
# grows with every file, so the key changes constantly: a display (the status
# line) may therefore accept a result a few seconds old (``max_age``), and
# only one computation runs at a time, the others reuse its result.
STATUS_MAX_AGE = 10.0   # seconds a status may be behind while data keeps changing
_coverage_cache: dict[str, tuple[tuple, Coverage, float]] = {}
_coverage_lock = threading.Lock()
_coverage_compute = threading.Lock()


def coverage(conn: sqlite3.Connection, policy: GoalPolicy = DEFAULT_POLICY,
             max_age: float = 0.0) -> Coverage:
    seq = conn.execute("SELECT MAX(seq) FROM changes").fetchone()[0]
    db_file = conn.execute("PRAGMA database_list").fetchone()[2] or ":memory:"
    key = (seq, policy)

    def cached() -> Coverage | None:
        with _coverage_lock:
            hit = _coverage_cache.get(db_file)
        if hit is None or conn.in_transaction:
            return None
        if hit[0] == key or (max_age and hit[0][1] == policy
                             and time.monotonic() - hit[2] < max_age):
            return hit[1]
        return None

    found = cached()
    if found is not None:
        return found
    with _coverage_compute:   # whoever waited finds the result of the one who computed
        found = cached()
        if found is not None:
            return found
        return _compute_coverage(conn, policy, db_file, key)


def _compute_coverage(conn: sqlite3.Connection, policy: GoalPolicy, db_file: str,
                      key: tuple) -> Coverage:
    rows = conn.execute("""
        WITH cur AS (
            SELECT file_id, hash, size FROM (
                SELECT file_id, hash, size, ROW_NUMBER() OVER (
                    PARTITION BY file_id ORDER BY mtime_ns DESC, hash) AS rn
                FROM copies WHERE state = 'present')
            WHERE rn = 1
        )
        SELECT cur.file_id, cur.size,
               (SELECT COUNT(DISTINCT c.node_id) FROM copies c
                 WHERE c.file_id = cur.file_id AND c.state = 'present'
                   AND c.hash = cur.hash AND c.marked_delete = 0) AS good
        FROM cur
    """).fetchall()
    file_tags: dict[int, list[str]] = {}
    if policy.tag_copies:
        for fid, tag in conn.execute("SELECT file_id, tag FROM tags WHERE present = 1"):
            file_tags.setdefault(fid, []).append(tag)
    good = {r["file_id"]: r["good"] for r in rows}
    goals = {fid: policy.goal(file_tags.get(fid, ())) for fid in good}
    cov = Coverage(
        bytes=sum(r["size"] for r in rows), good=good, goals=goals,
        satisfied=sum(1 for fid, g in good.items() if g >= goals[fid]),
        under_replicated=sum(1 for fid, g in good.items() if g < goals[fid]),
        single_copy=sum(1 for fid, g in good.items() if g == 1 and goals[fid] > 1),
    )
    if not conn.in_transaction:  # never cache uncommitted state
        with _coverage_lock:
            _coverage_cache[db_file] = (key, cov, time.monotonic())
    return cov


def decisions_visible() -> str:
    from .decisions import VISIBLE   # decisions imports queries' siblings: import late

    return VISIBLE


def status(conn: sqlite3.Connection, policy: GoalPolicy = DEFAULT_POLICY) -> Status:
    cov = coverage(conn, policy, max_age=STATUS_MAX_AGE)
    one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
    return Status(
        files=one("SELECT COUNT(*) FROM files"),
        bytes=cov.bytes,
        satisfied=cov.satisfied,
        under_replicated=cov.under_replicated,
        single_copy=cov.single_copy,
        divergent=one("SELECT COUNT(*) FROM decisions WHERE kind = 'divergent' AND status = 'open'"),
        missing_copies=one("SELECT COUNT(*) FROM copies WHERE state = 'missing'"),
        marked_delete=one("SELECT COUNT(*) FROM copies WHERE marked_delete = 1"),
        non_portable=one("SELECT COUNT(*) FROM files WHERE issues != ''"),
        open_decisions=one("SELECT COUNT(*) FROM decisions d WHERE d.status = 'open' AND "
                           + decisions_visible()),
        open_wanted=one("SELECT COUNT(*) FROM wanted WHERE status = 'open'"),
    )
