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


class Entries(list):
    """The listing of a directory; ``more`` = files beyond the limit that are not listed."""
    more: int = 0


MAX_FILES_LISTED = 2000   # a folder of a million files is not worth sending whole
HERE_CAP = 10001          # "has a copy here" is counted up to this many files


def list_dir(conn: sqlite3.Connection, vdir: str,
             policy: GoalPolicy = DEFAULT_POLICY) -> Entries | None:
    """Immediate children of a virtual directory. None if it does not exist.

    The index is walked by jumping: after the first file of a subdirectory, its files are
    counted with one range query and the walk continues behind it. The cost is the number
    of entries listed, not the number of files below them (a directory holding millions of
    files is listed in milliseconds). ``here`` says how many files below an entry have a copy
    on this server's own node, counted up to HERE_CAP.
    """
    vdir = paths.normalize(vdir)
    lo, hi = paths.prefix_range(vdir) if vdir != "/" else ("/", "0")
    me = index.self_id(conn)
    dirs: dict[str, tuple[int, int]] = {}
    files: list[tuple[int, str]] = []
    more = 0
    cur = lo
    while True:
        jumped = False
        for fid, vpath in conn.execute(
                "SELECT id, vpath FROM files WHERE vpath >= ? AND vpath < ? ORDER BY vpath",
                (cur, hi)):
            rest = vpath[len(lo):]
            head, sep, _ = rest.partition("/")
            if not sep:
                if len(files) < MAX_FILES_LISTED:
                    files.append((fid, rest))
                else:
                    more += 1
                continue
            clo, chi = paths.prefix_range(lo + head)
            n = conn.execute("SELECT COUNT(*) FROM files WHERE vpath >= ? AND vpath < ?",
                             (clo, chi)).fetchone()[0]
            here = conn.execute(
                # CROSS JOIN fixes the order: walk the files of this folder and look each up, never
                # the other way round (that reads every copy of the node for each folder).
                "SELECT COUNT(*) FROM (SELECT 1 FROM files f CROSS JOIN copies c"
                " ON c.file_id = f.id AND c.node_id = ? AND c.state = 'present'"
                " WHERE f.vpath >= ? AND f.vpath < ? LIMIT ?)",
                (me, clo, chi, HERE_CAP)).fetchone()[0]
            dirs[head] = (n, here)
            cur, jumped = chi, True
            break
        if not jumped:
            break
    # Directories made by the user show even when empty.
    for d in vdirs.under(conn, vdir, include_self=False):
        head = d[len(lo):].partition("/")[0]
        dirs.setdefault(head, (0, 0))
    if not dirs and not files and vdir != "/" and not vdirs.under(conn, vdir):
        return None
    out = Entries(DirEntry(name, True, n, here=h) for name, (n, h) in sorted(dirs.items()))
    entries = []
    for fid, name in files:
        here = conn.execute("SELECT 1 FROM copies WHERE file_id = ? AND node_id = ?"
                            " AND state = 'present'", (fid, me)).fetchone() is not None
        entries.append(DirEntry(name, False, 1, file_info(conn, fid, policy), int(here)))
    out.extend(sorted(entries, key=lambda e: e.name))
    out.more = more
    return out


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


def _filters(tag: str | None, node: str | None, issues_only: bool, under: bool,
             pattern: str | None = None) -> tuple[str, list]:
    """FROM/WHERE of the files passing the filters (and, with a pattern, whose name matches)."""
    q = " FROM files f"
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
    if under:
        where.append("f.id IN (SELECT file_id FROM temp.under_ids)")
    if pattern is not None:
        # SQLite cuts the candidates down with the longest literal part of the pattern
        # (vkey is the casefolded path); the exact match is checked on those few.
        literal = _longest_literal(pattern)
        if literal:
            where.append("instr(f.vkey, ?) > 0")
            args.append(literal)
    if where:
        q += " WHERE " + " AND ".join(where)
    return q, args


def _longest_literal(pat: str) -> str:
    """The longest run of the pattern without wildcards (and not part of a [class])."""
    best, cur, in_class = "", "", False
    for ch in pat:
        if in_class:
            in_class = ch != "]"
        elif ch in "*?":
            best, cur = (cur, "") if len(cur) > len(best) else (best, "")
        elif ch == "[":
            best, cur, in_class = (cur, "", True) if len(cur) > len(best) else (best, "", True)
        else:
            cur += ch
    return cur if len(cur) > len(best) else best


def _name_matches(pat: str, vpath: str) -> bool:
    target = vpath if "/" in pat else paths.basename(vpath)
    return fnmatch.fnmatchcase(target.casefold(), pat)


_leaf_cache: dict[str, tuple[int, float, float, dict[str, int]]] = {}
_leaf_lock = threading.Lock()


def _leaf_dir_counts(conn: sqlite3.Connection, tag, node, issues_only, under) -> dict[str, int]:
    """Files per folder that directly holds files, passing the filters. Without filters
    (a search by name only) it is cached: on a big index it takes seconds and the answer
    only changes as files come and go."""
    plain, pargs = _filters(tag, node, issues_only, under)
    dir_expr = ("substr(f.vpath, 1, length(f.vpath) - length(substr(f.vpath, "
                "length(rtrim(f.vpath, replace(f.vpath, '/', ''))) + 1)) - 1)")
    unfiltered = not (tag or node or issues_only or under)
    seq = conn.execute("SELECT MAX(seq) FROM changes").fetchone()[0] or 0
    key = _db_file(conn)
    if unfiltered:
        with _leaf_lock:
            hit = _leaf_cache.get(key)
        if hit and (hit[0] == seq or (hit[2] > 0.5 and time.monotonic() - hit[1] < 120)):
            return hit[3]
    started = time.monotonic()
    leaf = {r[0]: r[1] for r in conn.execute(
        f"SELECT {dir_expr} AS d, COUNT(DISTINCT f.id)" + plain + " GROUP BY d", pargs)}
    if unfiltered and not conn.in_transaction:
        with _leaf_lock:
            _leaf_cache[key] = (seq, time.monotonic(), time.monotonic() - started, leaf)
    return leaf


def search(conn: sqlite3.Connection, pattern: str | None = None, tag: str | None = None,
           node: str | None = None, issues_only: bool = False, under_replicated: bool = False,
           policy: GoalPolicy = DEFAULT_POLICY) -> tuple[list[int], list[tuple[str, int]]]:
    """Matching files (ids, in path order) and matching folders.

    A folder matches when its name (or its path, if the pattern contains a
    "/") matches, like for files. Each folder comes with the number of files
    below it that pass the other filters. Without a pattern there are no
    folders (every folder would match).

    The work is done by SQLite (one pass over the files, counting per folder
    holding files), not by Python over every file: millions of files must not
    stall the server.
    """
    if under_replicated:
        fill_under_ids(conn, policy)
    pat = name_pattern(pattern)
    base, args = _filters(tag, node, issues_only, under_replicated, pat)
    rows = conn.execute("SELECT DISTINCT f.id, f.vpath" + base + " ORDER BY f.vpath", args)
    if pat is None:
        return [r[0] for r in rows], []
    ids = [fid for fid, vpath in rows if _name_matches(pat, vpath)]
    # Folders: count the files passing the other filters per folder that holds files,
    # then fold the counts upwards (a few thousand folders, not millions of files).
    leaf = _leaf_dir_counts(conn, tag, node, issues_only, under_replicated)
    counts: dict[str, int] = {}
    matching: dict[str, bool] = {}
    for d0, n in leaf.items():
        d = d0 or "/"
        while d != "/":
            if d not in matching:
                matching[d] = _name_matches(pat, d)
            if matching[d]:
                counts[d] = counts.get(d, 0) + n
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
    coverage_stale: bool = False      # the coverage figures are a little old (a big database)
    coverage_seconds: float = 0.0     # how long computing them takes


# --- how well the files are covered ----------------------------------------------------
#
# "How many good copies does each file have, against its goal" needs every copy.
# It is aggregated by SQLite in one pass (a couple of seconds for millions of
# files; building a Python dict per file took a minute and gigabytes), kept in a
# cache keyed by the last sequence number of the change log (everything it depends
# on is logged), and on a big database refreshed in the background: a request
# is answered with the figures we have and never waits for the computation.


@dataclass
class CoverageStats:
    files: int                  # files with a present copy
    bytes: int                  # their current versions
    satisfied: int
    under_replicated: int
    single_copy: int
    node_files: dict[str, int]  # copies per node (any state)
    node_bytes: dict[str, int]
    seq: int
    policy: GoalPolicy
    seconds: float              # how long it took to compute
    at: float                   # when (monotonic)


FAST_SECONDS = 0.3              # below this a database is exact on every request
MIN_REFRESH_SECONDS = 5.0       # on a big one, at most this often and 3x its computing time
_stats_cache: dict[str, CoverageStats] = {}
_stats_lock = threading.Lock()
_stats_compute = threading.Lock()
_refreshing: set[str] = set()


def prepare_goal_overrides(conn: sqlite3.Connection, policy: GoalPolicy) -> None:
    """temp.goal_override: files whose tags raise their goal above the default."""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS goal_override ("
                 "file_id INTEGER PRIMARY KEY, goal INTEGER NOT NULL)")
    conn.execute("DELETE FROM temp.goal_override")
    if not policy.tag_copies:
        return
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS tag_goal (tag TEXT PRIMARY KEY, goal INTEGER)")
    conn.execute("DELETE FROM temp.tag_goal")
    conn.executemany("INSERT INTO temp.tag_goal VALUES (?, ?)", list(policy.tag_copies))
    conn.execute(
        "INSERT INTO temp.goal_override SELECT t.file_id, MAX(g.goal) FROM tags t"
        " JOIN temp.tag_goal g ON g.tag = t.tag WHERE t.present = 1 GROUP BY t.file_id"
        " HAVING MAX(g.goal) > ?", (policy.copies,))


def exact_good(conn: sqlite3.Connection, fid: int) -> tuple[int, int, str] | None:
    """(good copies, size, hash) of a file with several versions (its current one counts)."""
    v = index.current_version(conn, fid)
    if v is None:
        return None
    good = conn.execute("SELECT COUNT(*) FROM copies WHERE file_id = ? AND state = 'present'"
                        " AND hash = ? AND marked_delete = 0", (fid, v.hash)).fetchone()[0]
    size = conn.execute("SELECT MAX(size) FROM copies WHERE file_id = ? AND hash = ?"
                        " AND state = 'present'", (fid, v.hash)).fetchone()[0] or 0
    return good, size, v.hash


def _compute_stats(conn: sqlite3.Connection, policy: GoalPolicy, seq: int) -> CoverageStats:
    started = time.monotonic()
    prepare_goal_overrides(conn, policy)
    files = nbytes = satisfied = under = single = 0
    multi: list[int] = []
    for kind, a, b, c, d, e in conn.execute("""
        WITH g AS MATERIALIZED (
            SELECT file_id, MIN(hash) = MAX(hash) AS one, SUM(marked_delete = 0) AS good,
                   MAX(size) AS sz
            FROM copies WHERE state = 'present' GROUP BY file_id)
        SELECT 1, COUNT(*), IFNULL(SUM(sz), 0),
               IFNULL(SUM(good >= COALESCE(o.goal, ?1)), 0),
               IFNULL(SUM(good < COALESCE(o.goal, ?1)), 0),
               IFNULL(SUM(good = 1 AND COALESCE(o.goal, ?1) > 1), 0)
        FROM g LEFT JOIN temp.goal_override o ON o.file_id = g.file_id WHERE g.one
        UNION ALL
        SELECT 0, file_id, 0, 0, 0, 0 FROM g WHERE NOT g.one""", (policy.copies,)):
        if kind == 1:
            files, nbytes, satisfied, under, single = a, b, c, d, e
        else:
            multi.append(a)
    for fid in multi:   # files with several versions: the exact (and slower) way, few of them
        got = exact_good(conn, fid)
        if got is None:
            continue
        good, size, _ = got
        goal = conn.execute("SELECT goal FROM temp.goal_override WHERE file_id = ?",
                            (fid,)).fetchone()
        goal = goal[0] if goal else policy.copies
        files += 1
        nbytes += size
        if good >= goal:
            satisfied += 1
        else:
            under += 1
        if good == 1 and goal > 1:
            single += 1
    node_files, node_bytes = {}, {}
    for node_id, n, size in conn.execute(
            "SELECT node_id, COUNT(*), IFNULL(SUM(size), 0) FROM copies GROUP BY node_id"):
        node_files[node_id], node_bytes[node_id] = n, size
    return CoverageStats(files, nbytes, satisfied, under, single, node_files, node_bytes, seq,
                         policy, time.monotonic() - started, time.monotonic())


def _db_file(conn: sqlite3.Connection) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2] or ":memory:"


def _refresh_in_background(db_file: str, policy: GoalPolicy) -> None:
    def run() -> None:
        from . import db   # local: db knows nothing of queries

        conn = None
        try:
            conn = db.open_existing(db_file)
            seq = conn.execute("SELECT MAX(seq) FROM changes").fetchone()[0] or 0
            stats = _compute_stats(conn, policy, seq)
            with _stats_lock:
                _stats_cache[db_file] = stats
        except Exception:  # noqa: BLE001 - the next request tries again
            pass
        finally:
            if conn is not None:
                conn.close()
            with _stats_lock:
                _refreshing.discard(db_file)

    with _stats_lock:
        if db_file in _refreshing:
            return
        _refreshing.add(db_file)
    threading.Thread(target=run, name="reifs-coverage", daemon=True).start()


def coverage_stats(conn: sqlite3.Connection, policy: GoalPolicy = DEFAULT_POLICY
                   ) -> tuple[CoverageStats, bool]:
    """The coverage figures and whether they are a little old (big databases only)."""
    seq = conn.execute("SELECT MAX(seq) FROM changes").fetchone()[0] or 0
    db_file = _db_file(conn)
    with _stats_lock:
        hit = _stats_cache.get(db_file)
    if hit is not None and hit.policy == policy:
        if hit.seq == seq:
            return hit, False
        if hit.seconds > FAST_SECONDS and not conn.in_transaction:
            if time.monotonic() - hit.at >= max(MIN_REFRESH_SECONDS, 3 * hit.seconds):
                _refresh_in_background(db_file, policy)
            return hit, True
    with _stats_compute:   # one at a time; whoever waited finds the result of the one who ran
        with _stats_lock:
            hit = _stats_cache.get(db_file)
        if hit is not None and hit.policy == policy and hit.seq == seq:
            return hit, False
        stats = _compute_stats(conn, policy, seq)
        if not conn.in_transaction:   # never cache uncommitted state
            with _stats_lock:
                _stats_cache[db_file] = stats
        return stats, False


def fill_under_ids(conn: sqlite3.Connection, policy: GoalPolicy) -> None:
    """temp.under_ids: the files below their goal (for the "under-replicated" filter)."""
    prepare_goal_overrides(conn, policy)
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS under_ids (file_id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM temp.under_ids")
    multi: list[int] = []
    conn.execute("""
        INSERT OR IGNORE INTO temp.under_ids
        SELECT g.file_id FROM (
            SELECT file_id, MIN(hash) = MAX(hash) AS one, SUM(marked_delete = 0) AS good
            FROM copies WHERE state = 'present' GROUP BY file_id) g
        LEFT JOIN temp.goal_override o ON o.file_id = g.file_id
        WHERE g.one AND g.good < COALESCE(o.goal, ?)""", (policy.copies,))
    for (fid,) in conn.execute("SELECT file_id FROM copies WHERE state = 'present'"
                               " GROUP BY file_id HAVING MIN(hash) != MAX(hash)").fetchall():
        multi.append(fid)
    for fid in multi:
        got = exact_good(conn, fid)
        goal = conn.execute("SELECT goal FROM temp.goal_override WHERE file_id = ?",
                            (fid,)).fetchone()
        if got is not None and got[0] < (goal[0] if goal else policy.copies):
            conn.execute("INSERT OR IGNORE INTO temp.under_ids VALUES (?)", (fid,))


def decisions_visible() -> str:
    from .decisions import VISIBLE   # decisions imports queries' siblings: import late

    return VISIBLE


def status(conn: sqlite3.Connection, policy: GoalPolicy = DEFAULT_POLICY) -> Status:
    cov, stale = coverage_stats(conn, policy)
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
        coverage_stale=stale, coverage_seconds=cov.seconds,
    )
