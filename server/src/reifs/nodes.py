"""Node markers, filesystem capability probing and discovery of storage nodes."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import string
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from . import db, index
from .config import Config

MARKER_DIR = ".rei-fs"
MARKER_FILE = "node.json"


class NodeError(Exception):
    pass


@dataclass
class Marker:
    node_id: str
    name: str
    kind: str
    owner_id: str
    created: str


def marker_path(root: Path | str) -> Path:
    return Path(root) / MARKER_DIR / MARKER_FILE


def read_marker(root: Path | str) -> Marker | None:
    path = marker_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Marker(**{k: data[k] for k in Marker.__dataclass_fields__})
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise NodeError(f"unreadable node marker {path}: {e}") from e


def write_marker(root: Path | str, marker: Marker) -> None:
    path = marker_path(root)
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(marker), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def probe_capabilities(root: Path | str) -> tuple[bool, bool]:
    """Return ``(case_sensitive, restricted_names)`` for the filesystem at root."""
    scratch = Path(root) / MARKER_DIR
    scratch.mkdir(exist_ok=True)
    token = uuid.uuid4().hex[:8]

    lower = scratch / f"probe-{token}-case"
    lower.write_bytes(b"")
    try:
        case_sensitive = not (scratch / f"PROBE-{token}-CASE").exists()
    finally:
        lower.unlink()

    restricted = sys.platform == "win32"
    if not restricted:
        odd = scratch / f"probe-{token}-a:b"
        try:
            odd.write_bytes(b"")
        except OSError:
            restricted = True
        else:
            odd.unlink()
    return case_sensitive, restricted


def default_discovery_paths() -> list[Path]:
    if sys.platform == "win32":
        # Skip A: and B: (floppy drives can block on access).
        return [Path(f"{c}:\\") for c in string.ascii_uppercase[2:]]
    paths = [Path("/media"), Path("/run/media"), Path("/mnt")]
    if os.environ.get("TERMUX_VERSION") or Path("/storage/emulated").exists():
        paths.append(Path("/storage"))
    return paths


def find_markers(base: Path, max_depth: int) -> list[Path]:
    """Directories at most ``max_depth`` levels below base holding a marker."""
    found: list[Path] = []
    stack = [(base, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            if marker_path(d).is_file():
                found.append(d)
                continue  # nodes do not nest
        except OSError:
            continue
        if depth >= max_depth:
            continue
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.name.startswith(".") or e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append((Path(e.path), depth + 1))
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def self_id(conn: sqlite3.Connection) -> str:
    sid = db.get_meta(conn, "server_id")
    if not sid:
        raise NodeError("database has no server id")
    return sid


def get_node(conn: sqlite3.Connection, name_or_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM nodes WHERE name = ? OR id = ?", (name_or_id, name_or_id)
    ).fetchone()
    if row is None:
        raise NodeError(f"unknown node: {name_or_id}")
    return row


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def register_node(
    conn: sqlite3.Connection, root: Path, name: str, kind: str = "storage",
    node_id: str | None = None,
) -> str:
    """Create (or adopt) a node at root, owned by this server."""
    owner = self_id(conn)
    root = root.resolve()
    if not root.is_dir():
        raise NodeError(f"not a directory: {root}")
    if conn.execute("SELECT 1 FROM nodes WHERE name = ?", (name,)).fetchone():
        raise NodeError(f"a node named {name!r} already exists")
    for row in conn.execute(
        "SELECT name, root_path FROM nodes WHERE owner_id = ? AND root_path IS NOT NULL",
        (owner,),
    ):
        if _overlaps(root, Path(row["root_path"])):
            raise NodeError(f"{root} overlaps node {row['name']!r} at {row['root_path']}")

    existing = read_marker(root)
    if existing is not None:
        if existing.owner_id != owner:
            raise NodeError(
                f"{root} is node {existing.name!r} owned by server {existing.owner_id}; "
                "transfer its ownership instead"
            )
        if conn.execute("SELECT 1 FROM nodes WHERE id = ?", (existing.node_id,)).fetchone():
            raise NodeError(f"{root} is already registered as node {existing.name!r}")
        node_id = existing.node_id

    node_id = node_id or str(uuid.uuid4())
    case_sensitive, restricted = probe_capabilities(root)
    ts = db.now()
    marker = Marker(node_id=node_id, name=name, kind=kind, owner_id=owner, created=ts)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO nodes(id, name, kind, owner_id, root_path, case_sensitive,"
            " restricted_names, online, last_seen, owner_since)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (node_id, name, kind, owner, str(root), int(case_sensitive), int(restricted), ts, ts),
        )
        _update_space(conn, conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone(),
                      root)
        index.log_node(conn, node_id)
        write_marker(root, marker)
    return node_id


@dataclass
class Located:
    node_id: str
    name: str
    root: Path


@dataclass
class Discovery:
    online: list[Located]
    offline: list[str]          # node names
    foreign: list[tuple[str, str, Path]]  # (name, owner_id, path)
    moved: list[tuple[str, Path]]         # (name, new root)


def discover(conn: sqlite3.Connection, cfg: Config) -> Discovery:
    """Find which owned nodes are reachable now, and update the nodes table."""
    owner = self_id(conn)
    owned = {
        r["id"]: r
        for r in conn.execute("SELECT * FROM nodes WHERE owner_id = ?", (owner,))
    }
    located: dict[str, Path] = {}
    foreign: list[tuple[Marker, Path]] = []

    def consider(root: Path) -> None:
        try:
            m = read_marker(root)
        except NodeError:
            return
        if m is None:
            return
        if m.owner_id != owner:
            foreign.append((m, root))
        elif m.node_id in owned and m.node_id not in located:
            located[m.node_id] = root.resolve()

    for row in owned.values():
        if row["root_path"]:
            consider(Path(row["root_path"]))

    # Always look at the mount points too: a moved drive, or another server's
    # drive plugged in here (shown as foreign, can be adopted).
    bases = [Path(p) for p in cfg.discovery.paths] or default_discovery_paths()
    for base in bases:
        if base.is_dir():
            for root in find_markers(base, cfg.discovery.max_depth):
                consider(root)

    ts = db.now()
    result = Discovery(online=[], offline=[], foreign=[], moved=[])
    with db.transaction(conn):
        for node_id, row in owned.items():
            root = located.get(node_id)
            if root is None:
                if row["online"]:
                    conn.execute("UPDATE nodes SET online = 0 WHERE id = ?", (node_id,))
                    index.log_node(conn, node_id)
                result.offline.append(row["name"])
                continue
            announce = not row["online"]
            if row["root_path"] != str(root):
                result.moved.append((row["name"], root))
                try:
                    cs, rn = probe_capabilities(root)
                except OSError:
                    cs, rn = row["case_sensitive"], row["restricted_names"]
                conn.execute(
                    "UPDATE nodes SET root_path = ?, case_sensitive = ?,"
                    " restricted_names = ? WHERE id = ?",
                    (str(root), cs, rn, node_id),
                )
                announce = announce or (cs, rn) != (row["case_sensitive"], row["restricted_names"])
            conn.execute(
                "UPDATE nodes SET online = 1, last_seen = ? WHERE id = ?", (ts, node_id)
            )
            announce = _update_space(conn, row, root) or announce
            if announce:
                index.log_node(conn, node_id)
            result.online.append(Located(node_id, row["name"], root))
        for m, path in foreign:
            conn.execute(
                "INSERT INTO foreign_sightings(node_id, name, owner_id, path, seen)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(node_id) DO UPDATE SET"
                " name = excluded.name, owner_id = excluded.owner_id,"
                " path = excluded.path, seen = excluded.seen",
                (m.node_id, m.name, m.owner_id, str(path), ts),
            )
    result.foreign = [(m.name, m.owner_id, path) for m, path in foreign]
    return result


SPACE_ANNOUNCE_BYTES = 256 * 1024 * 1024


def _update_space(conn: sqlite3.Connection, row: sqlite3.Row, root: Path) -> bool:
    """Record free space; True if it changed enough to announce to peers
    (more than 1% of the disk or 256 MiB), so the log does not fill up."""
    try:
        usage = shutil.disk_usage(root)
    except OSError:
        return False
    old = row["free_bytes"]
    threshold = max(SPACE_ANNOUNCE_BYTES, usage.total // 100)
    if old is not None and abs(old - usage.free) < threshold and row["total_bytes"] == usage.total:
        return False
    conn.execute("UPDATE nodes SET free_bytes = ?, total_bytes = ? WHERE id = ?",
                 (usage.free, usage.total, row["id"]))
    return True


def adopt_node(conn: sqlite3.Connection, name_or_path: str) -> str:
    """Take ownership of a storage node owned by another server that is now
    attached here (design 3.3). The new ownership is announced with a newer
    ``owner_since``; the former owner stops managing the node when it syncs."""
    me = self_id(conn)
    row = conn.execute("SELECT * FROM foreign_sightings WHERE name = ? OR node_id = ?",
                       (name_or_path, name_or_path)).fetchone()
    root = Path(row["path"] if row else name_or_path).resolve()
    marker = read_marker(root)
    if marker is None:
        raise NodeError(f"no foreign node named {name_or_path!r} and no node marker at {root}")
    if marker.owner_id == me:
        raise NodeError(f"{root} is already one of this server's nodes")
    if marker.kind == "server":
        raise NodeError("that is another server's own storage; it cannot be adopted")
    for r in conn.execute("SELECT name, root_path FROM nodes WHERE owner_id = ? AND"
                          " root_path IS NOT NULL", (me,)):
        if _overlaps(root, Path(r["root_path"])):
            raise NodeError(f"{root} overlaps node {r['name']!r}")
    case_sensitive, restricted = probe_capabilities(root)
    ts = db.now()
    with db.transaction(conn):
        conn.execute("INSERT OR IGNORE INTO servers(id, name) VALUES (?, ?)",
                     (marker.owner_id, marker.owner_id[:8]))
        existing = conn.execute("SELECT name FROM nodes WHERE id = ?", (marker.node_id,)).fetchone()
        if existing:
            name = existing["name"]
            conn.execute(
                "UPDATE nodes SET owner_id = ?, owner_since = ?, root_path = ?, online = 1,"
                " last_seen = ?, case_sensitive = ?, restricted_names = ?, last_scan_id = NULL"
                " WHERE id = ?",
                (me, ts, str(root), ts, int(case_sensitive), int(restricted), marker.node_id))
        else:
            name = marker.name
            n = 2
            while conn.execute("SELECT 1 FROM nodes WHERE name = ?", (name,)).fetchone():
                name = f"{marker.name}-{n}"
                n += 1
            conn.execute(
                "INSERT INTO nodes(id, name, kind, owner_id, owner_since, root_path,"
                " case_sensitive, restricted_names, online, last_seen)"
                " VALUES (?, ?, 'storage', ?, ?, ?, ?, ?, 1, ?)",
                (marker.node_id, name, me, ts, str(root), int(case_sensitive),
                 int(restricted), ts))
        index.log_node(conn, marker.node_id)
        conn.execute("DELETE FROM foreign_sightings WHERE node_id = ?", (marker.node_id,))
        write_marker(root, Marker(node_id=marker.node_id, name=name, kind="storage",
                                  owner_id=me, created=marker.created))
    return name
