"""Applications that use rei-fs for their files (design section 5.18).

An application is registered by the user (client, CLI or API) and gets a *token*, which it
presents to the ``/api/app/...`` endpoints. These reach nothing but the application's own
directory. Two modes:

* **storage**: a directory ``/apps/<name>`` is granted on a node (a named one of this server, or
  one chosen for it) to fill with whatever it wants. It is an ordinary directory of the virtual
  tree: scanned, backed up and replicated like any other.
* **instance**: the application runs on several nodes and its shared files (assets, settings,
  a database file, ...) are kept the same everywhere. Every node where the same name is
  registered joins one group: the directory ``/apps/<name>`` is made *synchronous* (see
  ``synchronous``), and a node registered while still empty gets the files at once.

The application can use the HTTP endpoints (list, read, write, delete inside its directory)
or, when it runs on the machine of the node, read and write the directory itself (its path is
in ``info``) and ask for a rescan with ``sync``. Registrations are replicated, so every server
knows the groups; only the server where a token was issued can check it.
"""

from __future__ import annotations

import base64
import logging
import os
import random
import re
import secrets
import sqlite3
from pathlib import Path

from . import db, index, nodes, paths, peers, priority
from .config import Config
from .hashing import hash_file

log = logging.getLogger("reifs.apps")

ROOT = "/apps"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_READ = 4 << 20
MAX_LIST = 5000
MODES = ("storage", "instance")


class AppError(Exception):
    pass


def vpath_of_app(name: str) -> str:
    return f"{ROOT}/{name}"


# --- registrations -----------------------------------------------------------------------


def _log(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    index.append_change(conn, "app", {
        "name": row["name"], "node_id": row["node_id"], "mode": row["mode"],
        "vpath": row["vpath"], "present": row["present"], "updated": row["updated"],
        "updated_by": row["updated_by"]})


def apply_change(conn: sqlite3.Connection, d: dict) -> None:
    """A registration announced by another server (without its token)."""
    if not conn.execute("SELECT 1 FROM nodes WHERE id = ?", (d["node_id"],)).fetchone():
        return
    row = conn.execute("SELECT updated, updated_by FROM apps WHERE name = ? AND node_id = ?",
                       (d["name"], d["node_id"])).fetchone()
    stamp = (d["updated"], d["updated_by"])
    if row is not None and stamp <= (row[0], row[1]):
        return
    conn.execute(
        "INSERT INTO apps(name, node_id, mode, vpath, present, created, updated, updated_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(name, node_id) DO UPDATE SET"
        " mode = excluded.mode, vpath = excluded.vpath, present = excluded.present,"
        " updated = excluded.updated, updated_by = excluded.updated_by",
        (d["name"], d["node_id"], d["mode"], d["vpath"], int(d["present"]), d["updated"], *stamp))


def get(conn: sqlite3.Connection, name: str, node: str | None = None) -> sqlite3.Row:
    q = ("SELECT a.*, n.name AS node_name FROM apps a JOIN nodes n ON n.id = a.node_id"
         " WHERE a.name = ? AND a.present = 1")
    args: list = [name]
    if node:
        q += " AND (n.name = ? OR n.id = ?)"
        args += [node, node]
    rows = conn.execute(q, args).fetchall()
    if not rows:
        raise AppError(f"no application {name!r}" + (f" on {node}" if node else ""))
    if len(rows) > 1:
        raise AppError(f"{name!r} is registered on several nodes: say which "
                       f"({', '.join(r['node_name'] for r in rows)})")
    return rows[0]


def list_apps(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT a.*, n.name AS node_name, n.online, n.owner_id, s.name AS owner"
            " FROM apps a JOIN nodes n ON n.id = a.node_id JOIN servers s ON s.id = n.owner_id"
            " WHERE a.present = 1 ORDER BY a.name, n.name"):
        out.append({"name": r["name"], "mode": r["mode"], "node": r["node_name"],
                    "node_id": r["node_id"], "dir": r["vpath"], "online": bool(r["online"]),
                    "server": r["owner"], "mine": r["owner_id"] == index.self_id(conn),
                    "has_token": r["token_hash"] is not None})
    return out


def _pick_node(conn: sqlite3.Connection, cfg: Config, node: str | None) -> sqlite3.Row:
    roots = {loc.node_id: loc.root for loc in nodes.discover(conn, cfg).online}
    if node:
        row = nodes.get_node(conn, node)
        if row["owner_id"] != index.self_id(conn):
            raise AppError(f"node {row['name']} belongs to another server: register the "
                           "application on that server")
        if row["id"] not in roots:
            raise AppError(f"node {row['name']} is offline")
        return row
    cands = [r for r in conn.execute("SELECT * FROM nodes WHERE owner_id = ?",
                                     (index.self_id(conn),)) if r["id"] in roots]
    roomy = [r for r in cands if r["free_bytes"] is None or r["free_bytes"] > 256 << 20]
    if not (roomy or cands):
        raise AppError("no node of this server is online")
    return random.choice(roomy or cands)


def new_token() -> str:
    return "reifsapp_" + secrets.token_urlsafe(32)


def register(conn: sqlite3.Connection, cfg: Config, name: str, mode: str,
             node: str | None = None) -> tuple[dict, str]:
    """Register an application (a token is returned: it is not kept anywhere in clear)."""
    if not NAME_RE.match(name or ""):
        raise AppError("the name is letters, digits, '.', '_' and '-' (at most 64)")
    if mode not in MODES:
        raise AppError(f"mode must be one of: {', '.join(MODES)}")
    target = _pick_node(conn, cfg, node)
    existing = conn.execute("SELECT mode, node_id FROM apps WHERE name = ? AND present = 1",
                            (name,)).fetchall()
    if any(r["mode"] != mode for r in existing):
        raise AppError(f"{name!r} is registered as a {existing[0]['mode']} application")
    if mode == "storage" and existing:
        raise AppError(f"{name!r} is taken: a storage directory has one owner")
    if any(r["node_id"] == target["id"] for r in existing):
        raise AppError(f"{name!r} is already registered on {target['name']}")
    root = {loc.node_id: loc.root for loc in nodes.discover(conn, cfg).online}[target["id"]]
    vdir = vpath_of_app(name)
    paths.to_physical(root, vdir).mkdir(parents=True, exist_ok=True)
    token = new_token()
    ts, me = db.now(), index.self_id(conn)
    with db.transaction(conn, "registering an application"):
        conn.execute(
            "INSERT INTO apps(name, node_id, mode, vpath, token_hash, present, created, updated,"
            " updated_by) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?) ON CONFLICT(name, node_id) DO UPDATE"
            " SET mode = excluded.mode, vpath = excluded.vpath, token_hash = excluded.token_hash,"
            " present = 1, updated = excluded.updated, updated_by = excluded.updated_by",
            (name, target["id"], mode, vdir, peers.token_hash(token), ts, ts, me))
        _log(conn, conn.execute("SELECT * FROM apps WHERE name = ? AND node_id = ?",
                                (name, target["id"])).fetchone())
        # How closely the directory is watched: an instance is kept synchronous.
        if mode == "instance":
            priority.set_priority(conn, vdir, priority.SYNCHRONOUS)
        elif priority.Prefs(priority.load(conn)).prefs.get(vdir) is None:
            priority.set_priority(conn, vdir, priority.FREQUENT)
    info = describe(conn, cfg, get(conn, name, target["name"]))
    return info, token


def describe(conn: sqlite3.Connection, cfg: Config, row: sqlite3.Row) -> dict:
    roots = {loc.node_id: loc.root for loc in nodes.discover(conn, cfg).online}
    root = roots.get(row["node_id"])
    group = [{"node": r["node_name"], "online": bool(r["online"])} for r in conn.execute(
        "SELECT n.name AS node_name, n.online FROM apps a JOIN nodes n ON n.id = a.node_id"
        " WHERE a.name = ? AND a.present = 1 ORDER BY n.name", (row["name"],))]
    return {"name": row["name"], "mode": row["mode"], "node": row["node_name"],
            "dir": row["vpath"],
            "path": str(paths.to_physical(root, row["vpath"])) if root else None,
            "instances": group}


def regenerate_token(conn: sqlite3.Connection, name: str, node: str | None) -> str:
    row = get(conn, name, node)
    if row["token_hash"] is None:
        raise AppError("this registration was made on another server: issue its token there")
    token = new_token()
    with db.transaction(conn):
        conn.execute("UPDATE apps SET token_hash = ? WHERE name = ? AND node_id = ?",
                     (peers.token_hash(token), row["name"], row["node_id"]))
    return token


def remove(conn: sqlite3.Connection, name: str, node: str | None) -> None:
    """Unregister: the directory and its files are left as they are."""
    row = get(conn, name, node)
    if index.owner_of(conn, row["node_id"]) != index.self_id(conn):
        raise AppError("that node belongs to another server: unregister it there")
    with db.transaction(conn):
        conn.execute("UPDATE apps SET present = 0, token_hash = NULL, updated = ?, updated_by = ?"
                     " WHERE name = ? AND node_id = ?",
                     (db.now(), index.self_id(conn), row["name"], row["node_id"]))
        _log(conn, conn.execute("SELECT * FROM apps WHERE name = ? AND node_id = ?",
                                (row["name"], row["node_id"])).fetchone())


def authenticate(conn: sqlite3.Connection, header: str | None) -> sqlite3.Row | None:
    """The registration whose token is presented as ``Bearer reifsapp_...``, or None."""
    if not header or not header.startswith("Bearer reifsapp_"):
        return None
    token = header[7:].strip()
    return conn.execute(
        "SELECT a.*, n.name AS node_name FROM apps a JOIN nodes n ON n.id = a.node_id"
        " WHERE a.token_hash = ? AND a.present = 1", (peers.token_hash(token),)).fetchone()


# --- the application's files ---------------------------------------------------------------


class Access:
    """What one registered application may touch: its directory on its node."""

    def __init__(self, conn: sqlite3.Connection, cfg: Config, row: sqlite3.Row):
        self.conn, self.cfg, self.row = conn, cfg, row
        roots = {loc.node_id: loc.root for loc in nodes.discover(conn, cfg).online}
        root = roots.get(row["node_id"])
        if root is None:
            raise AppError(f"node {row['node_name']} is offline")
        self.base = paths.to_physical(root, row["vpath"])
        self.base.mkdir(parents=True, exist_ok=True)

    def _resolve(self, rel: str) -> tuple[str, Path]:
        try:
            clean = paths.normalize(rel or "/")
        except paths.VPathError as e:
            raise AppError(str(e)) from None
        full = paths.to_physical(self.base, clean)
        real_base = os.path.realpath(self.base)
        if os.path.commonpath([real_base, os.path.realpath(full)]) != real_base:
            raise AppError("that path leads out of the application's directory")
        vpath = self.row["vpath"] + ("" if clean == "/" else clean)
        return vpath, full

    def listing(self, rel: str) -> list[dict]:
        _, full = self._resolve(rel)
        if not full.is_dir():
            raise AppError("not a directory")
        out = []
        with os.scandir(full) as it:
            for e in it:
                try:
                    st = e.stat()
                except OSError:
                    continue
                out.append({"name": e.name, "dir": e.is_dir(), "size": 0 if e.is_dir() else st.st_size,
                            "mtime": int(st.st_mtime)})
                if len(out) >= MAX_LIST:
                    break
        return sorted(out, key=lambda x: (not x["dir"], x["name"]))

    def read(self, rel: str, offset: int = 0, length: int = MAX_READ) -> dict:
        _, full = self._resolve(rel)
        if not full.is_file():
            raise AppError("no such file")
        length = max(0, min(int(length), MAX_READ))
        with open(full, "rb") as f:
            f.seek(max(0, int(offset)))
            data = f.read(length)
        size = full.stat().st_size
        return {"size": size, "offset": int(offset), "eof": int(offset) + len(data) >= size,
                "data": base64.b64encode(data).decode("ascii")}

    def write(self, rel: str, data_b64: str, append: bool = False, done: bool = True) -> dict:
        vpath, full = self._resolve(rel)
        if vpath == self.row["vpath"]:
            raise AppError("give a file name")
        try:
            data = base64.b64decode(data_b64 or "", validate=True)
        except ValueError:
            raise AppError("data must be base64") from None
        if full.is_dir():
            raise AppError("that is a directory")
        full.parent.mkdir(parents=True, exist_ok=True)
        if append:
            with open(full, "ab") as f:
                f.write(data)
        else:
            tmp = full.with_name(full.name + ".reifs-part")
            tmp.write_bytes(data)
            os.replace(tmp, full)
        if done:
            self.index(vpath, full)
        return {"path": vpath, "size": full.stat().st_size, "indexed": bool(done)}

    def delete(self, rel: str) -> dict:
        """The application removes a file of its own directory (rei-fs itself never would)."""
        vpath, full = self._resolve(rel)
        if not full.is_file():
            raise AppError("no such file")
        full.unlink()
        fid = index.file_id(self.conn, vpath)
        if fid is not None:
            with db.transaction(self.conn, "app deleted a file"):
                index.forget_copy(self.conn, fid, self.row["node_id"])
        return {"path": vpath, "deleted": True}

    def index(self, vpath: str, full: Path) -> None:
        """Record the file at once, so that it is replicated and synchronized without waiting
        for the next scan."""
        st = full.stat()
        digest = hash_file(full)
        with db.transaction(self.conn, "app wrote a file"):
            fid = index.ensure_file(self.conn, vpath)
            index.record_copy(self.conn, fid, self.row["node_id"], st.st_size, st.st_mtime_ns,
                              digest)

    def pending(self) -> int:
        """Synchronizer items still to do below the application's directory."""
        cond, args = paths.subtree_sql(self.row["vpath"], "i.vpath")
        return self.conn.execute(
            "SELECT COUNT(*) FROM job_items i JOIN jobs j ON j.id = i.job_id WHERE j.hidden = 1"
            f" AND i.state IN ('pending', 'running') AND {cond}", args).fetchone()[0]
