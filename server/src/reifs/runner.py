"""Executing job items: resumable, verified transfers (design section 5).

Item life cycle::

    pending -> running -> done
                       -> pending   (transient failure, attempts += 1;
                                     node offline / disk full: no attempt used)
                       -> failed    (attempts exhausted; 'reifs jobs retry')
                       -> skipped   (cannot or need not be done: illegal name, ...)
                       -> conflict  (destination holds different content; decision)

Progress is committed per item, so an interrupted run (Ctrl-C, crash, power
loss, unplugged drive) resumes where it stopped. rsync keeps partial files
in ``<dst>/.rei-fs/partial`` so even a half-copied large file resumes.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import uuid

from . import db, fsops, index, jobs, paths, peers, relay, transfer
from .config import Config
from .control import StopFn, check
from .hashing import hash_file
from .nodes import discover

log = logging.getLogger(__name__)
ProgressFn = Callable[[str], None]
MAX_MESSAGES = 200


@dataclass
class JobReport:
    job_id: int
    description: str
    status: str = ""
    done: int = 0
    skipped: int = 0
    failed: int = 0
    retry: int = 0
    waiting: int = 0
    conflicts: int = 0
    bytes: int = 0
    messages: list[tuple[str, str]] = field(default_factory=list)

    def note(self, vpath: str, msg: str) -> None:
        if len(self.messages) < MAX_MESSAGES:
            self.messages.append((vpath, msg))


@dataclass
class Prepared:
    item: sqlite3.Row
    fid: int
    src_node: str
    src_root: Path | None          # local source
    dst_root: Path
    src_hash: str
    size: int
    src_stat: tuple[int, int] | None
    remote: peers.RemoteSource | peers.SshSource | None = None  # on a paired server
    relayed: tuple[str, str] | None = None   # (server, request id) of a staged file to release


def run_jobs(conn: sqlite3.Connection, cfg: Config, job_ids: list[int] | None = None,
             progress: ProgressFn | None = None, stop: StopFn | None = None) -> list[JobReport]:
    """Run every runnable job (or the given ones). Caller holds the server lock.

    ``stop`` is polled between items and while rsync runs; when it returns
    True the run is interrupted like Ctrl-C (``control.Stopped`` is raised).
    """
    return _Runner(conn, cfg, progress, stop).run(job_ids)


class _Runner:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, progress: ProgressFn | None,
                 stop: StopFn | None = None):
        self.conn = conn
        self.cfg = cfg
        self.stop = stop
        user_progress = progress or (lambda _msg: None)

        def progress_and_check(msg: str) -> None:
            user_progress(msg)
            check(stop)

        self.progress = progress_and_check
        self.roots: dict[str, Path] = {}
        self.nodes: dict[str, sqlite3.Row] = {}
        self.full: set[str] = set()
        self.reserved: dict[str, int] = {}

    # --- driver ---------------------------------------------------------------

    def run(self, job_ids: list[int] | None) -> list[JobReport]:
        conn = self.conn
        with db.transaction(conn):
            # Items left 'running' by a crash are simply pending again.
            conn.execute("UPDATE job_items SET state = 'pending' WHERE state = 'running'")
            conn.execute("UPDATE jobs SET status = 'pending' WHERE status = 'running'")
        self.roots = {loc.node_id: loc.root for loc in discover(conn, self.cfg).online}
        self.nodes = {r["id"]: r for r in conn.execute("SELECT * FROM nodes")}
        # Nodes of paired servers we can pull from: node announced online and
        # its server reachable at the last sync.
        self.remote = {}
        self.remote_why: dict[str, str] = {}   # why a node of another server cannot be pulled from
        all_peers = {p["id"]: p for p in peers.paired_peers(conn)}
        me = index.self_id(conn)
        for node in self.nodes.values():
            if node["owner_id"] == me:
                continue
            peer = all_peers.get(node["owner_id"])
            owner = peer["name"] if peer is not None else node["owner_id"][:8]
            if peer is None:
                self.remote_why[node["id"]] = f"server {owner} is not paired with this server"
            elif not peer["online"]:
                err = f" (last error: {peer['last_error']})" if peer["last_error"] else ""
                self.remote_why[node["id"]] = f"server {owner} is unreachable{err}"
            elif not node["online"]:
                self.remote_why[node["id"]] = f"node {node['name']} is offline on server {owner}"
            else:
                # The transport chosen for that server: its rsync daemon or SSH.
                if peer["transport"] == "ssh":
                    src = peers.ssh_source(peer, node)
                    missing = (f"server {owner} does not offer SSH access to {node['name']} "
                               "(Servers tab: switch the transport, or enable [ssh] there)")
                else:
                    src = peers.remote_source(peer, node["id"])
                    missing = (f"no rsync access to server {owner} (its rsync daemon was not "
                               "set up when pairing: pair the servers again)")
                if src is not None:
                    self.remote[node["id"]] = src
                else:
                    self.remote_why[node["id"]] = missing
        q = "SELECT * FROM jobs WHERE status IN ('pending', 'waiting')"
        args: list = []
        if job_ids:
            q += f" AND id IN ({','.join('?' * len(job_ids))})"
            args = list(job_ids)
        reports = []
        for job in conn.execute(q + " ORDER BY id", args).fetchall():
            reports.append(self.run_job(job))
        return reports

    def run_job(self, job: sqlite3.Row) -> JobReport:
        conn = self.conn
        rep = JobReport(job["id"], job["description"])
        with db.transaction(conn):
            conn.execute("UPDATE jobs SET status = 'running', started = COALESCE(started, ?)"
                         " WHERE id = ?", (db.now(), job["id"]))
        items = conn.execute("SELECT * FROM job_items WHERE job_id = ? AND state = 'pending'"
                             " ORDER BY id", (job["id"],)).fetchall()
        self.total = len(items)
        batches: dict[tuple[str, str], list[Prepared]] = {}
        max_files = max(1, self.cfg.transfers.batch_files)
        max_bytes = max(1, self.cfg.transfers.batch_mb) * 1024 * 1024
        try:
            for item in items:
                check(self.stop)
                if item["op"] == "rename":
                    self._rename(item, rep)
                    continue
                prep = self._prepare(item, rep)
                if prep is None:
                    continue
                key = (prep.src_node, item["dst_node"], str(prep.dst_root),
                       getattr(prep.remote, "subpath", ""))
                batch = batches.setdefault(key, [])
                batch.append(prep)
                if len(batch) >= max_files or sum(p.size for p in batch) >= max_bytes:
                    self._flush(batch, rep)
                    batch.clear()
            for batch in batches.values():
                if batch:
                    self._flush(batch, rep)
                    batch.clear()
        except BaseException:
            with db.transaction(conn):
                # Finished items keep their state; anything in flight is redone.
                conn.execute("UPDATE job_items SET state = 'pending', message = 'interrupted'"
                             " WHERE job_id = ? AND state = 'running'", (job["id"],))
                jobs.refresh_status(conn, job["id"])
            raise
        self._verify_done(job["id"], rep)
        with db.transaction(conn):
            rep.status = jobs.refresh_status(conn, job["id"], after_run=True)
        return rep

    def _verify_done(self, job_id: int, rep: JobReport) -> None:
        """Before calling a job finished, look again at every file it says it copied.

        A transfer is verified by hash when it ends; this checks that the files
        are still there (and of the recorded size) once the whole job is over. A file
        that is not goes back to pending, and the job says so instead of "done".
        """
        conn = self.conn
        lost = 0
        for item in conn.execute("SELECT * FROM job_items WHERE job_id = ? AND state = 'done'"
                                 " AND op IN ('copy', 'overwrite')", (job_id,)).fetchall():
            root = self.roots.get(item["dst_node"])
            if root is None:
                continue  # not attached here: its owner is the one who can look
            fid = index.file_id(conn, item["vpath"])
            row = None
            if item["fetch"] == 1:      # a download: it lives in the fetch folder, not in the index
                root = root / self.cfg.storage.fetch_dir.strip("/")
            elif item["fetch"] == 2:
                continue                # scratch for another server, removed on its own
            elif fid is not None:
                row = conn.execute("SELECT size FROM copies WHERE file_id = ? AND node_id = ?"
                                   " AND state = 'present'", (fid, item["dst_node"])).fetchone()
            try:
                st = os.stat(paths.to_physical(root, item["vpath"]))
                ok = row is None or st.st_size == row["size"]
            except OSError:
                ok = False
            if not ok:
                lost += 1
                with db.transaction(conn):
                    self._set(item, "pending", "missing after the job finished; copying again")
                rep.done -= 1
                rep.note(item["vpath"], "missing after the job finished; will be copied again")
        if lost:
            log.warning("job #%d: %d copied file(s) not found afterwards", job_id, lost)

    # --- item state -------------------------------------------------------------

    def _set(self, item, state: str, message: str | None = None, attempt: bool = False) -> None:
        self.conn.execute(
            "UPDATE job_items SET state = ?, message = ?, updated = ?,"
            " attempts = attempts + ? WHERE id = ?",
            (state, message, db.now(), int(attempt), item["id"]),
        )

    def _skip(self, item, rep: JobReport, msg: str) -> None:
        with db.transaction(self.conn):
            self._set(item, "skipped", msg)
            if item["fetch"] == 2:
                relay.set_state(self.conn, item["serves"], "failed", msg)
        rep.skipped += 1
        rep.note(item["vpath"], f"skipped: {msg}")

    def _wait(self, item, rep: JobReport, msg: str) -> None:
        with db.transaction(self.conn):
            self._set(item, "pending", msg)
        rep.waiting += 1

    def _transient(self, item, rep: JobReport, msg: str) -> None:
        attempts = item["attempts"] + 1
        final = attempts >= self.cfg.transfers.max_attempts
        with db.transaction(self.conn):
            self._set(item, "failed" if final else "pending", msg, attempt=True)
            if final and item["fetch"] == 2:
                relay.set_state(self.conn, item["serves"], "failed", msg)
        if final:
            rep.failed += 1
            rep.note(item["vpath"], f"failed after {attempts} attempts: {msg}")
        else:
            rep.retry += 1
            rep.note(item["vpath"], f"will retry: {msg}")

    def _finish(self, item, src_node: str | None, rep: JobReport, size: int,
                msg: str | None = None, expected_hash: str | None = None) -> None:
        """Mark an item done (caller holds a transaction)."""
        fid = index.file_id(self.conn, item["vpath"])
        if item["mark_source"] and src_node and fid is not None and not item["fetch"]:
            # Only the verified content may be marked: if the source changed
            # meanwhile, its owner refuses.
            index.mark_copy(self.conn, fid, src_node, True, expect_hash=expected_hash)
        self._set(item, "done", msg)
        if item["fetch"] == 2:
            relay.set_state(self.conn, item["serves"], "ready")
        if item["wanted_id"]:
            self.conn.execute("UPDATE wanted SET status = 'done' WHERE id = ?",
                              (item["wanted_id"],))
        rep.done += 1
        rep.bytes += size

    def _name(self, node_id: str) -> str:
        row = self.nodes.get(node_id)
        return row["name"] if row else node_id

    # --- legality ---------------------------------------------------------------

    def _illegal(self, vpath: str, node_id: str, fetch: int = 0) -> str | None:
        node = self.nodes[node_id]
        if fetch == 2:
            return None   # staging on our own disk, for another server
        if fetch:   # nothing is indexed: no collision to fear
            node = {**{k: node[k] for k in node.keys()}, "case_sensitive": 1}
        if node["restricted_names"]:
            bad = paths.portability_issues(vpath) & paths.RESTRICTED_NAME_ISSUES
            if bad:
                return (f"name not allowed on {node['name']} "
                        f"(restricted filesystem: {', '.join(sorted(bad))})")
        if not node["case_sensitive"]:
            clash = self.conn.execute(
                "SELECT f.vpath FROM copies c JOIN files f ON f.id = c.file_id"
                " WHERE c.node_id = ? AND f.vkey = ? AND f.vpath != ?",
                (node_id, paths.key(vpath), vpath)).fetchone()
            if clash:
                return f"case collision with {clash[0]} on {node['name']}"
        return None

    # --- copy / overwrite ---------------------------------------------------------

    def _prepare(self, item, rep: JobReport) -> Prepared | None:
        conn, vpath, dst = self.conn, item["vpath"], item["dst_node"]
        fid = index.file_id(conn, vpath)
        if fid is None:
            self._skip(item, rep, "no longer in the index")
            return None
        dst_root = self.roots.get(dst)
        if dst_root is None:
            self._wait(item, rep, f"node {self._name(dst)} is offline")
            return None
        fetch = item["fetch"]
        if fetch == 1:      # a plain download: <node root>/fetch/<path>, nothing indexed
            dst_root = dst_root / self.cfg.storage.fetch_dir.strip("/")
        elif fetch == 2:    # staged for another server, in our relay scratch area
            dst_root = relay.staging_root(self.cfg) / item["serves"]
        if dst in self.full:
            self._wait(item, rep, f"not enough free space on {self._name(dst)}")
            return None
        reason = self._illegal(vpath, dst, fetch)
        if reason:
            self._skip(item, rep, reason)
            return None
        version = index.current_version(conn, fid)
        if version is None:
            self._skip(item, rep, "no present copy anywhere")
            return None

        # Source: the given node, or any online node with the current version.
        copies = conn.execute(
            "SELECT * FROM copies WHERE file_id = ? AND state = 'present'"
            + ("" if fetch else " AND node_id != ?"), (fid,) if fetch else (fid, dst)).fetchall()
        if fetch == 2 and version.hash != item["allow_hash"]:
            self._skip(item, rep, "the file changed since it was asked for")
            return None
        relayed = None
        if item["src_node"]:
            source = next((c for c in copies if c["node_id"] == item["src_node"]), None)
            if source is None:
                self._skip(item, rep, f"no longer on {self._name(item['src_node'])}")
                return None
            if source["hash"] != version.hash:
                self._skip(item, rep, f"the copy on {self._name(item['src_node'])}"
                                      " is no longer the current version")
                return None
            if source["node_id"] not in self.roots and source["node_id"] not in self.remote:
                relayed = self._relay_source(item, rep, source)
                if relayed is None:
                    return None
        else:
            current = [c for c in copies if c["hash"] == version.hash]
            # Local sources first, then nodes of reachable paired servers.
            online = [c for c in current if c["node_id"] in self.roots] + \
                     [c for c in current if c["node_id"] in self.remote]
            if not online:
                if not current:
                    self._skip(item, rep, "no current copy to copy from")
                    return None
                # Nothing we can read directly: ask a server we are paired with to pass it on.
                source = current[0]
                relayed = self._relay_source(item, rep, source)
                if relayed is None:
                    return None
            else:
                source = online[0]

        src_hash, size = source["hash"], source["size"]
        src_root = self.roots.get(source["node_id"])
        remote = relayed[0] if relayed else (
            None if src_root is not None else self.remote[source["node_id"]])
        if relayed:
            src_root = None
        src_stat = None
        if src_root is not None:
            src_phys = paths.to_physical(src_root, vpath)
            try:
                st = os.stat(src_phys)
            except FileNotFoundError:
                self._transient(item, rep, f"source missing on {self._name(source['node_id'])}"
                                           " (rescan that node)")
                return None
            src_stat = (st.st_size, st.st_mtime_ns)
        if src_root is not None and src_stat != (source["size"], source["mtime_ns"]):
            # Changed since the last scan: index the new content first.
            try:
                src_hash = hash_file(src_phys)
            except OSError as e:
                self._transient(item, rep, f"cannot read source: {e.strerror or e}")
                return None
            size = st.st_size
            with db.transaction(conn):
                index.record_copy(conn, fid, source["node_id"], st.st_size, st.st_mtime_ns,
                                  src_hash)
            version = index.current_version(conn, fid)
            if version is None or version.hash != src_hash:
                self._skip(item, rep, "source changed and is no longer the current version")
                return None

        dst_phys = paths.to_physical(dst_root, vpath)
        if os.path.lexists(dst_phys):
            if os.path.islink(dst_phys) or not os.path.isfile(dst_phys):
                self._skip(item, rep, "destination exists and is not a regular file")
                return None
            try:
                dst_st = os.stat(dst_phys)
                dst_hash = self._dst_hash(fid, dst, dst_phys, dst_st)
            except OSError as e:
                self._transient(item, rep, f"cannot read destination: {e.strerror or e}")
                return None
            if dst_hash == src_hash:
                with db.transaction(conn):
                    if not fetch:
                        index.record_copy(conn, fid, dst, dst_st.st_size, dst_st.st_mtime_ns,
                                          dst_hash)
                    self._finish(item, source["node_id"], rep, 0,
                                 "already fetched" if fetch else "already present",
                                 expected_hash=src_hash)
                return None
            if fetch == 1:
                self._skip(item, rep, "a different file with this name is already in the fetch "
                                      "folder: remove it to fetch this one")
                return None
            if fetch == 2:
                os.unlink(dst_phys)   # our own scratch copy: replace it
            elif item["allow_hash"] != dst_hash:
                with db.transaction(conn):
                    self._set(item, "conflict", "destination holds different content")
                    index.add_decision(conn, "conflict", fid, dst, {
                        "job_id": item["job_id"], "item_id": item["id"], "vpath": vpath,
                        "dst_hash": dst_hash, "src_hash": src_hash,
                        "src_node": source["node_id"],
                    })
                rep.conflicts += 1
                rep.note(vpath, f"conflict on {self._name(dst)}: different file exists")
                return None

        try:
            if fetch:
                dst_root.mkdir(parents=True, exist_ok=True)
            fsops.ensure_dirs(dst_root, paths.parent(vpath),
                              bool(self.nodes[dst]["case_sensitive"]) or bool(fetch))
        except fsops.Refused as e:
            self._skip(item, rep, str(e))
            return None
        except OSError as e:
            self._transient(item, rep, f"cannot create directories: {e.strerror or e}")
            return None

        try:
            free = shutil.disk_usage(dst_root).free
        except OSError:
            free = None
        reserved = self.reserved.get(dst, 0)
        if free is not None and free - reserved - size < self.cfg.transfers.min_free_mb * 2**20:
            self.full.add(dst)
            self._wait(item, rep, f"not enough free space on {self._name(dst)}")
            return None
        self.reserved[dst] = reserved + size
        with db.transaction(conn):
            self._set(item, "running")
        return Prepared(item, fid, source["node_id"], src_root, dst_root, src_hash, size,
                        src_stat, remote, relayed[1] if relayed else None)

    # --- sources behind another server (relay.py) ------------------------------------

    def _server_name(self, server_id: str) -> str:
        row = self.conn.execute("SELECT name FROM servers WHERE id = ?", (server_id,)).fetchone()
        return row[0] if row else server_id[:8]

    def _relay_source(self, item, rep: JobReport, source):
        """A source we cannot read ourselves: have a server we are paired with fetch it for us
        and pass it on. Returns ``(RemoteSource of the staged file, (server, request id))``
        once it is staged; otherwise None and the item waits (or fails) with the reason."""
        conn = self.conn
        node = self.nodes[source["node_id"]]
        owner, me = node["owner_id"], index.self_id(conn)
        direct = self.remote_why.get(node["id"]) or (
            f"node {node['name']} is not attached to this server")
        if owner == me:       # our own drive, not attached
            self._wait(item, rep, direct)
            return None
        via, rid = item["relay_via"], item["relay_id"]
        if via and rid:
            return self._relay_poll(item, rep, via, rid)
        chosen = None
        for p in peers.paired_peers(conn):
            if not p["online"] or p["id"] == owner or p["transport"] == "ssh" \
                    or peers.remote_source(p, "x") is None:
                continue   # a relay is pulled from the intermediate's rsync daemon
            try:
                r = peers.forward(conn, p["id"], "GET",
                                  f"/api/peer/route?to={owner}&hops={relay.MAX_HOPS - 1}"
                                  f"&seen={me}", timeout=10)
            except peers.PeerError:
                continue
            if r.get("reachable"):
                chosen = p
                break
        if chosen is None:
            self._wait(item, rep, f"{direct}; no server paired with this one can reach "
                                  f"server {self._server_name(owner)} either")
            return None
        rid = uuid.uuid4().hex
        try:
            peers.forward(conn, chosen["id"], "POST", "/api/peer/relay-request",
                          {"id": rid, "node_id": node["id"], "vpath": item["vpath"],
                           "hash": source["hash"], "size": source["size"]}, timeout=15)
        except peers.PeerError as e:
            self._wait(item, rep, f"server {chosen['name']} would not relay it: {e}")
            return None
        with db.transaction(conn):
            conn.execute("UPDATE job_items SET relay_via = ?, relay_id = ? WHERE id = ?",
                         (chosen["id"], rid, item["id"]))
        self._wait(item, rep, f"asked server {chosen['name']} to fetch it from server "
                              f"{self._server_name(owner)} and pass it on")
        return None

    def _relay_poll(self, item, rep: JobReport, via: str, rid: str):
        conn = self.conn
        peer = next((p for p in peers.paired_peers(conn) if p["id"] == via), None)
        name = self._server_name(via)
        src = peers.remote_source(peer, "x") if peer is not None else None
        if peer is None or not peer["online"] or src is None:
            self._wait(item, rep, f"server {name}, which is relaying it, is not reachable")
            return None
        try:
            st = peers.forward(conn, via, "GET", f"/api/peer/relay-status?id={rid}", timeout=10)
        except peers.PeerError as e:
            self._wait(item, rep, f"server {name} did not answer about the relayed file: {e}")
            return None
        if st.get("state") == "ready":
            remote = peers.RemoteSource(src.host, src.port, "relay", src.user, src.password, rid)
            return remote, (via, rid)
        if st.get("state") in ("failed", "done"):    # start over next time
            with db.transaction(conn):
                conn.execute("UPDATE job_items SET relay_via = NULL, relay_id = NULL"
                             " WHERE id = ?", (item["id"],))
            self._transient(item, rep, f"server {name} could not pass it on: "
                                       f"{st.get('message') or st.get('state')}")
            return None
        self._wait(item, rep, f"server {name} is fetching it for us ({st.get('state')})")
        return None

    def _dst_hash(self, fid: int, node_id: str, phys: Path, st: os.stat_result) -> str:
        row = self.conn.execute("SELECT size, mtime_ns, hash FROM copies WHERE file_id = ?"
                                " AND node_id = ? AND state = 'present'", (fid, node_id)).fetchone()
        if row and (row["size"], row["mtime_ns"]) == (st.st_size, st.st_mtime_ns):
            return row["hash"]
        return hash_file(phys)

    def _flush(self, batch: list[Prepared], rep: JobReport) -> None:
        first = batch[0]
        base_bytes = rep.bytes
        names = f"{self._name(first.src_node)} -> {self._name(first.item['dst_node'])}"
        self.progress(f"job #{rep.job_id}: {names}: sending {len(batch)} file(s)")

        def on_bytes(n: int) -> None:
            self.progress(f"job #{rep.job_id}: {names}: {rep.done}/{self.total} files,"
                          f" {_human(base_bytes + n)}")

        result = transfer.run_batch(self.cfg, first.src_root, first.dst_root,
                                    [p.item["vpath"].lstrip("/") for p in batch], on_bytes,
                                    remote=first.remote, stop=self.stop)
        error = None
        if result.returncode != 0:
            lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
            error = f"rsync exit {result.returncode}: " + (lines[0] if lines else "no output")
        for p in batch:
            dst = p.item["dst_node"]
            self.reserved[dst] = max(0, self.reserved.get(dst, 0) - p.size)
            dst_phys = paths.to_physical(p.dst_root, p.item["vpath"])
            try:
                st = os.stat(dst_phys)
                got = hash_file(dst_phys)
            except OSError:
                got = None
            if got == p.src_hash:
                with db.transaction(self.conn):
                    if not p.item["fetch"]:
                        index.record_copy(self.conn, p.fid, dst, st.st_size, st.st_mtime_ns, got)
                    self._finish(p.item, p.src_node, rep, p.size,
                                 f"fetched to {dst_phys}" if p.item["fetch"] == 1 else None,
                                 expected_hash=p.src_hash)
                if p.relayed:   # we have it: the server that staged it can drop its copy
                    try:
                        peers.forward(self.conn, p.relayed[0], "POST", "/api/peer/relay-done",
                                      {"id": p.relayed[1]}, timeout=10)
                    except peers.PeerError:
                        pass    # it removes unclaimed files by itself after a few hours
                continue
            if p.remote is not None:
                changed = False  # its owner rescans it; the sync brings the news
            else:
                try:
                    sst = os.stat(paths.to_physical(p.src_root, p.item["vpath"]))
                    changed = (sst.st_size, sst.st_mtime_ns) != p.src_stat
                except OSError:
                    changed = True
            if changed:
                msg = "source changed during the transfer"
            elif got is None:
                msg = error or "file missing after transfer"
            else:
                msg = "verification failed: content differs after transfer"
            self._transient(p.item, rep, msg)

    # --- rename within a node ---------------------------------------------------

    def _rename(self, item, rep: JobReport) -> None:
        conn, node = self.conn, item["dst_node"]
        old, new = item["vpath"], item["new_vpath"]
        root = self.roots.get(node)
        if root is None:
            self._wait(item, rep, f"node {self._name(node)} is offline")
            return
        fid = index.file_id(conn, old)
        copy = conn.execute("SELECT * FROM copies WHERE file_id = ? AND node_id = ?"
                            " AND state = 'present'", (fid, node)).fetchone() if fid else None
        if copy is None:
            self._skip(item, rep, f"no copy at {old} on {self._name(node)}")
            return
        src, dst = paths.to_physical(root, old), paths.to_physical(root, new)
        if os.path.lexists(dst):
            self._skip(item, rep, f"{new} already exists on {self._name(node)}")
            return
        try:
            st = os.stat(src)
        except FileNotFoundError:
            self._transient(item, rep, "file not found (rescan the node)")
            return
        if (st.st_size, st.st_mtime_ns) != (copy["size"], copy["mtime_ns"]):
            self._skip(item, rep, "file changed since the rename was proposed")
            return
        reason = self._illegal(new, node)
        if reason:
            self._skip(item, rep, reason)
            return
        try:
            fsops.ensure_dirs(root, paths.parent(new), bool(self.nodes[node]["case_sensitive"]))
            fsops.rename_file(src, dst)
        except fsops.Refused as e:
            self._skip(item, rep, str(e))
            return
        except OSError as e:
            self._transient(item, rep, f"rename failed: {e.strerror or e}")
            return
        with db.transaction(conn):
            new_fid = index.ensure_file(conn, new)
            index.copy_tags(conn, fid, new_fid)
            index.record_copy(conn, new_fid, node, st.st_size, st.st_mtime_ns, copy["hash"])
            index.forget_copy(conn, fid, node)
            self._finish(item, None, rep, 0, f"renamed to {new}")
        fsops.prune(root, paths.parent(old))


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"
