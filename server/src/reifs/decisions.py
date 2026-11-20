"""The decision stack: listing decisions and applying the user's choice."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import db, index, jobs


class DecisionError(Exception):
    pass


@dataclass
class Decision:
    id: int
    kind: str
    file_id: int | None
    vpath: str | None
    node_id: str | None
    node_name: str | None
    data: dict
    status: str
    created: str
    options: dict[str, str]


def _row(conn: sqlite3.Connection, decision_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT d.*, f.vpath, n.name AS node_name FROM decisions d"
        " LEFT JOIN files f ON f.id = d.file_id LEFT JOIN nodes n ON n.id = d.node_id"
        " WHERE d.id = ?",
        (decision_id,),
    ).fetchone()
    if row is None:
        raise DecisionError(f"no decision #{decision_id}")
    return row


def _other_present(conn, fid: int, node_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT c.node_id, c.hash, n.online FROM copies c JOIN nodes n ON n.id = c.node_id"
        " WHERE c.file_id = ? AND c.node_id != ? AND c.state = 'present'",
        (fid, node_id),
    ).fetchall()


def options_for(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, str]:
    kind = row["kind"]
    if kind == "missing":
        if _other_present(conn, row["file_id"], row["node_id"]):
            return {
                "restore": "Lost: copy the current version back to this node",
                "forget": "Removed from this node only: stop tracking this copy",
                "delete": "Deleted from the system: forget it and mark all other copies for deletion",
            }
        return {"acknowledge": "This was the last copy: forget the file"}
    if kind == "rename":
        return {
            "apply": "Rename the copies on the other nodes too",
            "ignore": "Leave the other copies at the old path",
        }
    if kind == "conflict":
        return {
            "overwrite": "Replace the file on this node with the job's version",
            "skip": "Keep the file on this node; skip this item",
        }
    if kind == "divergent":
        return {
            "update": "Overwrite the stale copies with the current version",
            "keep": "Keep as is until a copy changes again",
        }
    return {}


def _to_decision(conn, row) -> Decision:
    return Decision(
        id=row["id"], kind=row["kind"], file_id=row["file_id"], vpath=row["vpath"],
        node_id=row["node_id"], node_name=row["node_name"], data=json.loads(row["data"]),
        status=row["status"], created=row["created"], options=options_for(conn, row),
    )


def list_decisions(conn: sqlite3.Connection, status: str = "open") -> list[Decision]:
    rows = conn.execute(
        "SELECT d.*, f.vpath, n.name AS node_name FROM decisions d"
        " LEFT JOIN files f ON f.id = d.file_id LEFT JOIN nodes n ON n.id = d.node_id"
        " WHERE d.status = ? ORDER BY d.id",
        (status,),
    ).fetchall()
    return [_to_decision(conn, r) for r in rows]


def get_decision(conn: sqlite3.Connection, decision_id: int) -> Decision:
    return _to_decision(conn, _row(conn, decision_id))


def resolve(conn: sqlite3.Connection, decision_id: int, option: str) -> str:
    """Apply an option. Returns a short human-readable summary."""
    with db.transaction(conn):
        row = _row(conn, decision_id)
        if row["status"] != "open":
            raise DecisionError(f"decision #{decision_id} is {row['status']}")
        opts = options_for(conn, row)
        if option not in opts:
            raise DecisionError(
                f"invalid option {option!r} for #{decision_id}; choose from: {', '.join(opts)}"
            )
        summary = _APPLY[row["kind"]](conn, row, json.loads(row["data"]), option)
        conn.execute(
            "UPDATE decisions SET status = ?, resolution = ?, resolved = ? WHERE id = ?",
            ("dismissed" if option == "keep" else "resolved", option, db.now(), decision_id),
        )
    return summary


def _apply_missing(conn, row, data, option) -> str:
    fid, node_id = row["file_id"], row["node_id"]
    if option == "restore":
        version = index.current_version(conn, fid)
        others = _other_present(conn, fid, node_id)
        source = sorted(
            (o for o in others if version and o["hash"] == version.hash),
            key=lambda o: -o["online"],
        ) or others
        index.forget_copy(conn, fid, node_id)
        index.add_wanted(conn, "copy", fid, source[0]["node_id"], node_id, "restore")
        return "queued a copy back to the node"
    index.forget_copy(conn, fid, node_id)
    if option == "delete":
        others = [r[0] for r in conn.execute(
            "SELECT node_id FROM copies WHERE file_id = ?", (fid,)).fetchall()]
        for other in others:
            index.mark_copy(conn, fid, other, True)
        n = len(others)
        return f"forgot the copy and marked {n} other cop{'y' if n == 1 else 'ies'} for deletion"
    return "forgot the copy"


def _apply_rename(conn, row, data, option) -> str:
    if option == "ignore":
        return "left the other copies in place"
    me, n, remote = index.self_id(conn), 0, 0
    for node_id, owner in conn.execute(
        "SELECT c.node_id, n.owner_id FROM copies c JOIN nodes n ON n.id = c.node_id"
        " WHERE c.file_id = ? AND c.state = 'present'", (row["file_id"],)
    ).fetchall():
        if owner == me:
            index.add_wanted(conn, "rename", row["file_id"], node_id, node_id, "rename",
                             {"from": data["from"], "to": data["to"]})
            n += 1
        else:
            # Renames on another server's node are proposed to that server.
            index.queue_peer_call(conn, owner, "/api/peer/wanted",
                                  {"op": "rename", "node_id": node_id, "from": data["from"],
                                   "to": data["to"], "reason": "rename"})
            remote += 1
    extra = f"; proposed {remote} rename(s) to the servers owning the other nodes" if remote else ""
    return f"queued {n} rename(s){extra}"


def _apply_divergent(conn, row, data, option) -> str:
    if option == "keep":
        return "kept as is"
    fid = row["file_id"]
    version = index.current_version(conn, fid)
    rows = conn.execute(
        "SELECT c.node_id, c.hash, n.online FROM copies c JOIN nodes n ON n.id = c.node_id"
        " WHERE c.file_id = ? AND c.state = 'present'",
        (fid,),
    ).fetchall()
    current = sorted((r for r in rows if r["hash"] == version.hash), key=lambda r: -r["online"])
    stale = [r for r in rows if r["hash"] != version.hash]
    for r in stale:
        index.add_wanted(conn, "overwrite", fid, current[0]["node_id"], r["node_id"],
                         "update stale copy", {"hash": version.hash})
    return f"queued {len(stale)} overwrite(s)"


def _apply_conflict(conn, row, data, option) -> str:
    item = conn.execute("SELECT * FROM job_items WHERE id = ?", (data["item_id"],)).fetchone()
    if item is None or item["state"] != "conflict":
        return "the job item no longer exists or is not in conflict; nothing to do"
    if option == "overwrite":
        # Only the exact content seen at conflict time may be replaced; if the
        # destination changes again, a new conflict is raised.
        conn.execute(
            "UPDATE job_items SET state = 'pending', allow_hash = ?, message = NULL,"
            " updated = ? WHERE id = ?", (data["dst_hash"], db.now(), item["id"]))
        summary = f"item will overwrite the file on the next run of job #{item['job_id']}"
    else:
        conn.execute("UPDATE job_items SET state = 'skipped', message = 'skipped by user',"
                     " updated = ? WHERE id = ?", (db.now(), item["id"]))
        summary = "item skipped"
    jobs.refresh_status(conn, item["job_id"])
    return summary


_APPLY = {"missing": _apply_missing, "rename": _apply_rename, "divergent": _apply_divergent,
          "conflict": _apply_conflict}
