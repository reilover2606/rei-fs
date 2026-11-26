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

from . import db, index, transfer
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


def clean_name(name: str) -> str:
    """A node name as the user will type it in commands: no slashes, no
    surrounding spaces, not empty, at most 64 characters."""
    name = (name or "").strip()
    if not name:
        raise NodeError("the node needs a name")
    if len(name) > 64 or "/" in name or "\\" in name or any(ord(c) < 32 for c in name):
        raise NodeError("a node name is at most 64 characters, without slashes or control "
                        "characters")
    return name


def rename_node(conn: sqlite3.Connection, node: str, new_name: str) -> str:
    """Rename a storage node owned by this server (also in its ID file)."""
    row = get_node(conn, node)
    if row["owner_id"] != self_id(conn):
        raise NodeError(f"node {row['name']!r} belongs to another server; rename it there")
    if row["kind"] == "server":
        raise NodeError("a server's own node is named after the server")
    new_name = clean_name(new_name)
    if new_name == row["name"]:
        return new_name
    if conn.execute("SELECT 1 FROM nodes WHERE name = ? AND id != ?",
                    (new_name, row["id"])).fetchone():
        raise NodeError(f"a node named {new_name!r} already exists")
    with db.transaction(conn):
        conn.execute("UPDATE nodes SET name = ? WHERE id = ?", (new_name, row["id"]))
        index.log_node(conn, row["id"])
    if row["root_path"]:  # keep the drive's own ID file in step, if it is attached
        try:
            marker = read_marker(row["root_path"])
            if marker is not None and marker.node_id == row["id"]:
                marker.name = new_name
                write_marker(row["root_path"], marker)
        except (NodeError, OSError):
            pass
    return new_name


TX_ROWS = 500   # rows deleted per transaction: the write lock is never held for long


def _delete_in_pieces(conn: sqlite3.Connection, table: str, key: str, where: str, args: tuple) -> None:
    """DELETE FROM table WHERE <where>, TX_ROWS rows per transaction."""
    while True:
        with db.transaction(conn, "removing a node"):
            n = conn.execute(
                f"DELETE FROM {table} WHERE {key} IN"
                f" (SELECT {key} FROM {table} WHERE {where} LIMIT {TX_ROWS})", args).rowcount
        if n < TX_ROWS:
            return


def forget_copies_of(conn: sqlite3.Connection, node_id: str, say=None, stop=None) -> int:
    """Forget every copy a node has in the index, TX_ROWS per transaction, with the change-log
    pointers to them, the decisions and proposals about the node, and the files no other node
    holds. The node itself, its jobs and its schedules stay."""
    from .control import check

    forgotten = 0
    while True:
        check(stop)
        fids = [r[0] for r in conn.execute(
            "SELECT file_id FROM copies WHERE node_id = ? LIMIT ?", (node_id, TX_ROWS))]
        if not fids:
            break
        marks = ",".join("?" * len(fids))
        with db.transaction(conn, "forgetting the copies of a node"):
            conn.execute(f"DELETE FROM copies WHERE node_id = ? AND file_id IN ({marks})",
                         (node_id, *fids))
            conn.execute(f"DELETE FROM collection_stats WHERE node_id = ? AND file_id IN ({marks})",
                         (node_id, *fids))
            conn.execute(f"DELETE FROM changes WHERE ref_node = ? AND ref_file IN ({marks})",
                         (node_id, *fids))
            for fid in fids:
                index.drop_file_if_orphan(conn, fid)
        forgotten += len(fids)
        if say:
            say(f"forgetting what the index knew of the node: {forgotten:,} copies")
    for table, key, where in (("decisions", "id", "node_id = ?"),
                              ("wanted", "id", "src_node = ? OR dst_node = ?"),
                              ("changes", "seq", "ref_node = ?")):
        check(stop)
        _delete_in_pieces(conn, table, key, where, (node_id,) * where.count("?"))
    return forgotten


def reset_node(conn: sqlite3.Connection, node: str, say=None, stop=None) -> int:
    """Start a node of this server over: the index forgets all it knew of the node, with no
    decisions about what is gone, and announces that to the other servers. The files are not
    touched; a scan then indexes the node from scratch."""
    row = get_node(conn, node)
    me = self_id(conn)
    if row["owner_id"] != me:
        raise NodeError(f"node {row['name']!r} belongs to another server; reset it there")
    with db.transaction(conn, "resetting a node"):
        index.append_change(conn, "node", {"id": row["id"], "name": row["name"],
                                           "owner_id": me, "reset": True})
        conn.execute("UPDATE nodes SET last_scan = NULL, last_scan_id = NULL WHERE id = ?",
                     (row["id"],))
        conn.execute("DELETE FROM scan_state WHERE node_id = ?", (row["id"],))
    n = forget_copies_of(conn, row["id"], say, stop)
    db.reclaim(conn)
    return n


def delete_rows(conn: sqlite3.Connection, node_id: str) -> int:
    """Forget a node in the database: its copies, decisions, proposals, jobs, schedules and
    the change-log pointers to it go, and files that no node holds any more with them.
    Done in pieces of TX_ROWS rows (each its own transaction, unless the caller already holds
    one). Nothing is touched on any disk."""
    forgotten = 0
    while True:
        fids = [r[0] for r in conn.execute(
            "SELECT file_id FROM copies WHERE node_id = ? LIMIT ?", (node_id, TX_ROWS))]
        if not fids:
            break
        marks = ",".join("?" * len(fids))
        with db.transaction(conn, "removing a node"):
            conn.execute(f"DELETE FROM copies WHERE node_id = ? AND file_id IN ({marks})",
                         (node_id, *fids))
            conn.execute(f"DELETE FROM changes WHERE ref_node = ? AND ref_file IN ({marks})",
                         (node_id, *fids))
            for fid in fids:
                index.drop_file_if_orphan(conn, fid)
        forgotten += len(fids)
    _delete_in_pieces(conn, "decisions", "id", "node_id = ?", (node_id,))
    _delete_in_pieces(conn, "wanted", "id", "src_node = ? OR dst_node = ?", (node_id, node_id))
    _delete_in_pieces(conn, "job_items", "id", "dst_node = ?", (node_id,))
    _delete_in_pieces(conn, "changes", "seq", "ref_node = ?", (node_id,))
    with db.transaction(conn, "removing a node"):
        conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))   # the little that is left cascades
    return forgotten


def remove_node(conn: sqlite3.Connection, node: str, force: bool = False) -> dict:
    """Take a node out of the index: its drive and files are left exactly as they are.

    A node of this server is announced as removed, so every server forgets it. A node of
    another server can be removed here (the server is gone, or was reset and is a different
    one now) only while that server is not reachable, unless ``force``; the other servers
    must do the same on their side. When it was the last node of a server that is not
    answering, that server is forgotten too.
    """
    row = get_node(conn, node)
    me = self_id(conn)
    if row["kind"] == "server" and row["owner_id"] == me:
        raise NodeError("this server's own node cannot be removed (its folder is set in the "
                        "configuration)")
    owner = conn.execute("SELECT * FROM servers WHERE id = ?", (row["owner_id"],)).fetchone()
    if row["owner_id"] != me and not force and owner is not None and owner["online"]:
        raise NodeError(f"node {row['name']!r} belongs to server {owner['name']!r}, which is "
                        "online: remove it from there")
    if row["owner_id"] == me:
        with db.transaction(conn, "removing a node"):
            index.append_change(conn, "node", {"id": row["id"], "name": row["name"],
                                               "owner_id": me, "removed": True})
    copies = delete_rows(conn, row["id"])       # in pieces: other writers are not kept waiting
    forgot = None
    if row["owner_id"] != me and not conn.execute(
            "SELECT 1 FROM nodes WHERE owner_id = ?", (row["owner_id"],)).fetchone():
        with db.transaction(conn, "removing a node"):
            forgot = forget_server_rows(conn, row["owner_id"])
    db.reclaim(conn)
    return {"name": row["name"], "copies_forgotten": copies, "server_forgotten": forgot}


def forget_server_rows(conn: sqlite3.Connection, server_id: str) -> str | None:
    """Drop what is left of a server that is gone (no node of it remains)."""
    srv = conn.execute("SELECT * FROM servers WHERE id = ? AND is_self = 0", (server_id,)).fetchone()
    if srv is None:
        return None
    conn.execute("UPDATE servers SET paired = 0, token_out = NULL, token_in_hash = NULL,"
                 " rsync_user = NULL, rsync_password = NULL, rsync_in_user = NULL,"
                 " rsync_in_password = NULL, online = 0 WHERE id = ?", (server_id,))
    conn.execute("DELETE FROM outbox WHERE server_id = ?", (server_id,))
    conn.execute("DELETE FROM pair_requests WHERE server_id = ?", (server_id,))
    conn.execute("DELETE FROM relay_requests WHERE requester = ?", (server_id,))
    conn.execute("DELETE FROM changes WHERE origin_id = ?", (server_id,))
    # origin_progress is kept: what a third server still forwards of its log is not applied again
    try:
        conn.execute("DELETE FROM servers WHERE id = ?", (server_id,))
    except sqlite3.IntegrityError:
        return None                    # still referenced: leave the row (it is not paired)
    return srv["name"]


def move_root(conn: sqlite3.Connection, cfg: Config, node: str, new_path: str,
              move_folder: bool = False) -> Path:
    """Change where a storage node's root is on this machine.

    By default the folder is already at ``new_path`` (the drive was remounted
    elsewhere, the folder was moved by hand): it must carry the node's ID
    file. With ``move_folder`` the folder is moved there (a rename: same
    file system, the target must not exist yet). Files are never copied or
    deleted and the virtual tree does not change.
    """
    row = get_node(conn, node)
    if row["owner_id"] != self_id(conn):
        raise NodeError(f"node {row['name']!r} belongs to another server; do this there")
    target = Path(os.path.expanduser(new_path.strip()))
    if not target.is_absolute():
        raise NodeError(f"{new_path!r} is not an absolute path (it is read on the server)")
    target = Path(os.path.normpath(target))
    old = Path(row["root_path"]) if row["root_path"] else None
    for other in conn.execute("SELECT name, root_path FROM nodes WHERE owner_id = ? AND id != ?"
                              " AND root_path IS NOT NULL", (self_id(conn), row["id"])):
        if _overlaps(target, Path(other["root_path"])):
            raise NodeError(f"{target} overlaps node {other['name']!r} at {other['root_path']}")
    if move_folder:
        if old is None or not old.is_dir() or read_marker(old) is None:
            raise NodeError(f"{row['name']!r} is not attached: there is no folder to move")
        if _overlaps(target, old):
            raise NodeError("the new place cannot be inside the old one (or the same)")
        if target.exists():
            raise NodeError(f"{target} already exists")
        if not target.parent.is_dir():
            raise NodeError(f"{target.parent} does not exist")
        try:
            os.rename(old, target)
        except OSError as e:
            raise NodeError(f"cannot move the folder there ({e.strerror or e}); a move needs the "
                            "same file system: copy the node to a new drive instead") from e
    else:
        if not target.is_dir():
            raise NodeError(f"{target} is not a folder on the server machine (is it mounted?)")
        marker = read_marker(target)
        if marker is None or marker.node_id != row["id"]:
            raise NodeError(f"{target} is not the folder of node {row['name']!r} (its "
                            f"{MARKER_DIR}/{MARKER_FILE} file is "
                            f"{'missing' if marker is None else 'another node' + chr(39) + 's'})")
    with db.transaction(conn):
        conn.execute("UPDATE nodes SET root_path = ? WHERE id = ?", (str(target), row["id"]))
    if row["kind"] == "server":   # the config names the server's folder too
        cfg.storage.root = str(target)
        cfg.save()
    discover(conn, cfg)   # capabilities, free space, online state, ssh path
    return target


def register_node(
    conn: sqlite3.Connection, root: Path, name: str, kind: str = "storage",
    node_id: str | None = None,
) -> str:
    """Create (or adopt) a node at root, owned by this server."""
    name = clean_name(name)
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
    marker = Marker(node_id=node_id, name=name, kind=kind, owner_id=owner, created=db.to_iso(ts))
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
    # The server's own folder as the config names it: moving the folder and
    # editing [storage] root must find the node again.
    if cfg.storage.root:
        consider(Path(cfg.storage.root))

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
            # Path served over SSH (as this machine's rsync names it).
            ssh_root = transfer.rsync_path(root, cfg.rsync.path_style) if cfg.ssh.enabled else None
            if ssh_root != row["ssh_root"]:
                conn.execute("UPDATE nodes SET ssh_root = ? WHERE id = ?", (ssh_root, node_id))
                announce = True
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
