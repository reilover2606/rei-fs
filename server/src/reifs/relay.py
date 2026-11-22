"""Passing a file through an intermediate server (design section 5.10).

Server A may want a file that only lives on a node of server C, with A and C not
paired, while both are paired with B. B can pull from C and A can pull from B, so
B is asked to *stage* the file: it fetches it from C into its own scratch area
(``<home>/relay/<id>/``, served read-only to its peers by the rsync daemon),
verifies the hash, and A pulls it from there and tells B when it is done. Staged
files are scratch, not user data: they are removed when A is done or after a few
hours. B may itself pass the request on to another server (up to ``MAX_HOPS``).

B stages with an ordinary job item (``fetch = 2``), so everything a transfer does
(rsync, hashing, free-space checks, even relaying further) is reused.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from pathlib import Path

from . import db, index, peers
from .config import Config

log = logging.getLogger("reifs.relay")

MAX_HOPS = 3
STAGING_TTL_SECONDS = 6 * 3600


def staging_root(cfg: Config) -> Path:
    return Path(cfg.home) / "relay"


def route(conn: sqlite3.Connection, to_server: str, hops: int = MAX_HOPS,
          seen: tuple[str, ...] = ()) -> dict:
    """Can this server reach ``to_server``, directly or through the servers it is paired
    with? ``seen`` are the servers already asked (a loop of pairings must not ask forever)."""
    me = index.self_id(conn)
    if to_server == me:
        return {"reachable": True, "hops": 0}
    online = [p for p in peers.paired_peers(conn) if p["online"]]
    if any(p["id"] == to_server for p in online):
        return {"reachable": True, "hops": 1}
    if hops <= 1:
        return {"reachable": False}
    best = None
    for p in online:
        if p["id"] in seen or p["id"] == me:
            continue
        try:
            out = peers.forward(conn, p["id"], "GET",
                                f"/api/peer/route?to={to_server}&hops={hops - 1}"
                                f"&seen={','.join((*seen, me))}", timeout=10)
        except peers.PeerError:
            continue
        if out.get("reachable") and (best is None or out["hops"] + 1 < best):
            best = out["hops"] + 1
    return {"reachable": True, "hops": best} if best else {"reachable": False}


def request(conn: sqlite3.Connection, requester: str, body: dict) -> dict:
    """Another server asks us to stage a file it cannot reach itself."""
    from . import jobs

    rid = str(body.get("id", ""))
    node_id, vpath, hash_ = str(body.get("node_id", "")), str(body.get("vpath", "")), \
        str(body.get("hash", ""))
    if not (rid.isalnum() and node_id and vpath.startswith("/") and hash_):
        raise ValueError("a relay request needs id, node_id, vpath and hash")
    row = conn.execute("SELECT * FROM relay_requests WHERE id = ?", (rid,)).fetchone()
    if row is not None:                       # asked again: the same answer
        return {"state": row["state"], "message": row["message"] or ""}
    if conn.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone() is None:
        raise ValueError(f"unknown node: {node_id}")
    ts = db.now()
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO relay_requests(id, requester, node_id, vpath, hash, size, created,"
            " updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, requester, node_id, vpath, hash_, int(body.get("size") or 0), ts, ts))
        jobs.create_relay_job(conn, rid, node_id, vpath, hash_, int(body.get("size") or 0),
                              requester)
    return {"state": "queued", "message": ""}


def status(conn: sqlite3.Connection, rid: str) -> dict:
    row = conn.execute("SELECT state, message FROM relay_requests WHERE id = ?", (rid,)).fetchone()
    if row is None:
        return {"state": "failed", "message": "unknown request (it may have expired)"}
    return {"state": row["state"], "message": row["message"] or ""}


def set_state(conn: sqlite3.Connection, rid: str | None, state: str, message: str = "") -> None:
    if rid:
        conn.execute("UPDATE relay_requests SET state = ?, message = ?, updated = ? WHERE id = ?",
                     (state, message, db.now(), rid))


def done(conn: sqlite3.Connection, cfg: Config, rid: str) -> None:
    """The requester has the file: remove the staged copy."""
    shutil.rmtree(staging_root(cfg) / rid, ignore_errors=True)
    with db.transaction(conn):
        set_state(conn, rid, "done")


def cleanup(conn: sqlite3.Connection, cfg: Config) -> int:
    """Remove staged files nobody collected, and old finished requests."""
    cutoff = time.time() - STAGING_TTL_SECONDS
    removed = 0
    root = staging_root(cfg)
    known = {r[0] for r in conn.execute("SELECT id FROM relay_requests")}
    if root.is_dir():
        for d in root.iterdir():
            try:
                if d.is_dir() and (d.name not in known or d.stat().st_mtime < cutoff):
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
    with db.transaction(conn):
        for r in conn.execute("SELECT id, updated FROM relay_requests WHERE state != 'queued'"
                              ).fetchall():
            if _age_seconds(r["updated"]) > STAGING_TTL_SECONDS:
                conn.execute("DELETE FROM relay_requests WHERE id = ?", (r["id"],))
    return removed


def _age_seconds(iso: str) -> float:
    from datetime import datetime, timezone

    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return 0.0
