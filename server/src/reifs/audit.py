"""Does the index account for everything on a node? (design section 5.7)

Walks a node's folder without hashing anything and lists what a transfer
cannot know about: files on disk that are not in the index, with the reason,
and files the index lists that are not on disk. Reading only; nothing changes.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from . import paths
from .config import Config
from .control import StopFn, check
from .nodes import MARKER_DIR, marker_path
from .scanner import Ignorer

EXAMPLES = 20


class _Tally:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.examples: dict[str, list[str]] = {}

    def add(self, reason: str, vpath: str) -> None:
        self.counts[reason] = self.counts.get(reason, 0) + 1
        ex = self.examples.setdefault(reason, [])
        if len(ex) < EXAMPLES:
            ex.append(vpath)

    def out(self) -> list[dict]:
        return [{"reason": r, "count": n, "examples": self.examples[r]}
                for r, n in sorted(self.counts.items(), key=lambda kv: -kv[1])]


def audit_node(conn: sqlite3.Connection, cfg: Config, node_id: str, root: Path,
               stop: StopFn | None = None) -> dict:
    ignorer = Ignorer(cfg.scan)
    indexed = {r["vpath"]: r for r in conn.execute(
        "SELECT f.vpath, c.size, c.state FROM copies c JOIN files f ON f.id = c.file_id"
        " WHERE c.node_id = ?", (node_id,))}
    seen: set[str] = set()
    not_indexed, odd = _Tally(), _Tally()
    on_disk = 0

    def classify_tree(directory: str, vdir: str, reason: str) -> None:
        """Everything below a directory the scanner does not enter has one reason."""
        for dirpath, dirnames, filenames in os.walk(directory):
            check(stop)
            rel = os.path.relpath(dirpath, directory)
            base = vdir if rel == "." else vdir + "/" + rel.replace(os.sep, "/")
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for name in filenames:
                not_indexed.add(reason, f"{base}/{name}")

    stack = [(str(root), "")]
    while stack:
        check(stop)
        directory, vdir = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as e:
            odd.add(f"cannot read directory ({e.strerror or e})", vdir or "/")
            continue
        for entry in entries:
            name = entry.name
            vpath = f"{vdir}/{name}"
            try:
                name.encode("utf-8")
            except UnicodeEncodeError:
                not_indexed.add("name is not valid UTF-8", vpath.encode("utf-8", "replace").decode())
                continue
            try:
                if entry.is_symlink():
                    not_indexed.add("symbolic link (not followed)", vpath)
                elif entry.is_dir(follow_symlinks=False):
                    if name == MARKER_DIR:
                        continue
                    if ignorer.hidden_dirs and name.startswith("."):
                        classify_tree(entry.path, vpath, "inside a hidden directory (name starts "
                                                         "with '.')")
                    elif ignorer.ignored(name, vpath, is_dir=True):
                        classify_tree(entry.path, vpath, "ignored by the [scan] ignore list")
                    elif marker_path(entry.path).exists():
                        classify_tree(entry.path, vpath, "inside another node's folder")
                    else:
                        stack.append((entry.path, vpath))
                elif entry.is_file(follow_symlinks=False):
                    on_disk += 1
                    row = indexed.get(vpath)
                    if ignorer.ignored(name, vpath, is_dir=False):
                        not_indexed.add("ignored by the [scan] ignore list", vpath)
                    elif row is None or row["state"] != "present":
                        not_indexed.add("not scanned yet (new or recently changed)"
                                        if row is None else "marked missing by the last scan", vpath)
                    else:
                        seen.add(vpath)
                        if entry.stat(follow_symlinks=False).st_size != row["size"]:
                            odd.add("changed since the last scan", vpath)
                else:
                    not_indexed.add("not a regular file (socket, device, ...)", vpath)
            except OSError as e:
                odd.add(f"cannot read ({e.strerror or e})", vpath)

    missing = [v for v, r in indexed.items() if r["state"] == "present" and v not in seen]
    # A missing copy under a directory that could not be read is not proof of loss.
    return {
        "files_on_disk": on_disk,
        "files_indexed": sum(1 for r in indexed.values() if r["state"] == "present"),
        "not_in_index": not_indexed.out(),
        "not_in_index_total": sum(not_indexed.counts.values()),
        "listed_but_not_on_disk": {"count": len(missing), "examples": sorted(missing)[:EXAMPLES]},
        "other": odd.out(),
    }
