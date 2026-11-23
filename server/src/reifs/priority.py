"""Update priority of directories (design section 4.5).

0 synchronous, 1 frequent, 2 infrequent (the default), 3 archival. The value is
set per directory and applies to everything below it unless a subdirectory has
a value of its own. It decides how often a directory is scanned for changes
(archival ones only when asked), and later how closely it is watched.
"""

from __future__ import annotations

import sqlite3
import time

from . import db, index, paths
from .config import Config

SYNCHRONOUS, FREQUENT, INFREQUENT, ARCHIVAL = range(4)
DEFAULT = INFREQUENT
CLASSES = (SYNCHRONOUS, FREQUENT, INFREQUENT, ARCHIVAL)
NAMES = {0: "synchronous", 1: "frequent", 2: "infrequent", 3: "archival"}


class PriorityError(Exception):
    pass


def parse(value) -> int | None:
    """A priority from user input (number or name); None clears the setting."""
    if value is None or value in ("", "inherit"):
        return None
    if isinstance(value, str) and value.casefold() in NAMES.values():
        return next(k for k, v in NAMES.items() if v == value.casefold())
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise PriorityError(f"priority must be 0-3 or one of {', '.join(NAMES.values())}") from None
    if n not in NAMES:
        raise PriorityError("priority must be 0 (synchronous), 1 (frequent), "
                            "2 (infrequent) or 3 (archival)")
    return n


def load(conn: sqlite3.Connection) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT vpath, priority FROM dirprefs WHERE priority IS NOT NULL")}


class Prefs:
    """The settings of every directory, for looking up what a path inherits."""

    def __init__(self, prefs: dict[str, int]):
        self.prefs = prefs

    def source(self, vdir: str) -> tuple[int, str | None]:
        """(priority, directory it was set on) for a directory."""
        d = vdir
        while True:
            if d in self.prefs:
                return self.prefs[d], d
            if d == "/":
                return DEFAULT, None
            d = paths.parent(d)

    def effective(self, vdir: str) -> int:
        return self.source(vdir)[0]


def get(conn: sqlite3.Connection, vdir: str) -> dict:
    vdir = paths.normalize(vdir)
    prio, src = Prefs(load(conn)).source(vdir)
    return {"path": vdir, "priority": prio, "name": NAMES[prio], "set_on": src,
            "inherited": src != vdir}


def set_priority(conn: sqlite3.Connection, vpath: str, priority: int | None) -> dict:
    """Set (or, with None, clear) the priority of a directory."""
    try:
        vpath = paths.normalize(vpath)
    except paths.VPathError as e:
        raise PriorityError(str(e)) from None
    if index.file_id(conn, vpath) is not None:
        raise PriorityError(f"{vpath} is a file; the priority belongs to a directory")
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO dirprefs(vpath, priority, updated, updated_by) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(vpath) DO UPDATE SET priority = excluded.priority,"
            " updated = excluded.updated, updated_by = excluded.updated_by",
            (vpath, priority, db.now(), index.self_id(conn)))
        index.log_dirpref(conn, vpath)
    return get(conn, vpath)


def move(conn: sqlite3.Connection, old: str, new: str) -> int:
    """The settings of a moved directory (and of those inside it) move with it."""
    n = 0
    for vpath, prio in sorted(load(conn).items()):
        if vpath == old or vpath.startswith(old.rstrip("/") + "/"):
            for target, value in ((vpath, None), (new + vpath[len(old):], prio)):
                conn.execute(
                    "INSERT INTO dirprefs(vpath, priority, updated, updated_by)"
                    " VALUES (?, ?, ?, ?) ON CONFLICT(vpath) DO UPDATE SET"
                    " priority = excluded.priority, updated = excluded.updated,"
                    " updated_by = excluded.updated_by",
                    (target, value, db.now(), index.self_id(conn)))
                index.log_dirpref(conn, target)
            n += 1
    return n


# --- scheduling -----------------------------------------------------------------


def classes_in_use(prefs: Prefs) -> set[int]:
    """Priority classes that cover at least some directory."""
    return {prefs.effective("/"), *prefs.prefs.values()}


def due_classes(conn: sqlite3.Connection, cfg: Config, node_id: str,
                prefs: Prefs, now: float | None = None) -> set[int]:
    """Classes of a node due for an automatic scan: never scanned, or their
    interval has passed (an interval of 0 means never again)."""
    now = time.time() if now is None else now
    last = {r[0]: r[1] for r in conn.execute(
        "SELECT priority, scanned FROM scan_state WHERE node_id = ?", (node_id,))}
    due = set()
    for p in classes_in_use(prefs):
        minutes = cfg.scan.minutes(p)
        if p not in last or (minutes > 0 and now - last[p] >= minutes * 60):
            due.add(p)
    return due


def record_scan(conn: sqlite3.Connection, node_id: str, classes, when: float) -> None:
    conn.executemany(
        "INSERT INTO scan_state(node_id, priority, scanned) VALUES (?, ?, ?)"
        " ON CONFLICT(node_id, priority) DO UPDATE SET scanned = excluded.scanned",
        [(node_id, p, when) for p in classes])


class Schedule:
    """Which directories one scan visits: those whose class is in ``due``.

    ``due`` None = everything (a scan the user asked for).
    """

    def __init__(self, prefs: Prefs, due: set[int] | None):
        self.prefs, self.due = prefs, due
        # Directories above a due setting must be entered to reach it.
        self.hot: set[str] = set()
        if due is not None:
            for vpath, prio in prefs.prefs.items():
                if prio in due:
                    d = paths.parent(vpath)
                    while d not in self.hot:
                        self.hot.add(d)
                        if d == "/":
                            break
                        d = paths.parent(d)

    def wants(self, vdir_priority: int) -> bool:
        return self.due is None or vdir_priority in self.due

    def enter(self, vdir: str, priority: int) -> bool:
        """Is anything in this directory's tree to be scanned?"""
        return self.due is None or priority in self.due or vdir in self.hot

    def covers_file(self, vpath: str) -> bool:
        return self.wants(self.prefs.effective(paths.parent(vpath)))
