"""Background worker of ``reifs serve``.

One worker thread performs every heavy operation (scans and job runs) one
at a time, under the server lock, so they never overlap with each other or
with the CLI. It also:

* scans all online nodes every ``scan.interval_minutes``;
* polls for attached drives every 30 s and scans a storage node when it
  comes online;
* runs pending/waiting jobs every minute, and right away when asked.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, field

from . import (archives, db, opaque, index, peers, priority, relay, replication, runner, scanner,
               schedules, sync, synchronous)
from .config import Config
from .control import Stopped
from .locking import LockError, ServerLock
from .nodes import discover
from .rsyncd import RsyncDaemon

log = logging.getLogger("reifs.service")

DISCOVERY_SECONDS = 30
SCAN_CHECK_SECONDS = 30   # how often to look for directories due for a scan
JOBS_SECONDS = 60
RELAY_POLL_SECONDS = 5
SCHEDULE_CHECK_SECONDS = 15   # how often scheduled syncs are looked at (a late node is caught up)
HOUSEKEEPING_SECONDS = 600
SYNC_SECONDS = 30
FOLLOWUP_SECONDS = 15   # directories packed everywhere: copy the archive, remove the originals
SYNC_CHECK_SECONDS = 3   # synchronous files: look at the change log this often
PLAN_SECONDS = 600     # replication planning, at least this often
PLAN_CHECK_SECONDS = 60  # ...and when the log changed (local or peer facts)
RETRY_SECONDS = 5      # while a paired server is unreachable
TICK_SECONDS = 2       # how often the pairing state is checked for changes


@dataclass
class Activity:
    state: str = "idle"          # idle | scanning | running jobs
    detail: str = ""
    since: str = field(default_factory=db.now)
    last_error: str = ""
    last_result: str = ""


class Worker:
    def __init__(self, cfg: Config, syncer: "Syncer | None" = None):
        self.cfg = cfg
        self.syncer = syncer
        self.tasks: queue.Queue = queue.Queue()
        self._plan_seconds = 0.0   # how long the last planning took
        self._schedule_now = False  # a node came back: look at the schedules at once
        self.stop_requested = threading.Event()
        self.shutdown = threading.Event()
        self._lock = threading.Lock()
        self._activity = Activity()
        self._pending: set[tuple] = set()
        self.thread = threading.Thread(target=self._main, name="reifs-worker", daemon=True)

    # --- public API (thread safe) ----------------------------------------------

    def start(self) -> None:
        self.thread.start()

    def close(self, timeout: float = 30) -> None:
        self.shutdown.set()
        self.stop_requested.set()
        self.tasks.put(None)
        self.thread.join(timeout)

    def activity(self) -> dict:
        with self._lock:
            a = self._activity
            return {"state": a.state, "detail": a.detail, "since": a.since,
                    "last_error": a.last_error, "last_result": a.last_result,
                    "queued": self.tasks.qsize()}

    def request_scan(self, nodes: list[str] | None = None, verify: bool = False,
                     full: bool = True, new_only: bool = False) -> None:
        """Scan (full: everything, as asked by the user; otherwise only what is due).
        ``new_only``: just discover files not indexed yet."""
        self._enqueue(("scan", tuple(sorted(nodes or [])), verify, full, new_only))

    def schedule_soon(self) -> None:
        """Look at the schedules at the next turn of the loop (one was added or changed)."""
        self._schedule_now = True

    def request_jobs(self) -> None:
        self._enqueue(("jobs",))

    def request_plan(self) -> None:
        self._enqueue(("plan",))

    def request_housekeeping(self) -> None:
        """Forget what the index holds below opaque directories, and old member lists."""
        self._enqueue(("housekeeping",))

    def request_sync(self, full: bool = False) -> None:
        """Bring the copies of synchronous files up to date (a full pass looks at all of them)."""
        self._enqueue(("sync", full))

    def request_stop(self) -> None:
        """Interrupt the current operation and drop queued ones."""
        with self._lock:
            self._pending.clear()
        while True:
            try:
                self.tasks.get_nowait()
            except queue.Empty:
                break
        self.stop_requested.set()

    # --- internals ----------------------------------------------------------------

    def _enqueue(self, task: tuple) -> None:
        with self._lock:
            if task in self._pending:
                return  # already queued
            self._pending.add(task)
        self.tasks.put(task)

    def _set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self._activity, k, v)

    def _scan_progress(self, s, current: str) -> None:
        text = f"{s.node}: {s.seen:,} files seen, {s.hashed:,} hashed"
        if current:
            text += f", {current}"
        self._progress(text)

    def _progress(self, detail: str) -> None:
        with self._lock:
            self._activity.detail = detail

    def _main(self) -> None:
        conn = db.open_existing(self.cfg.db_path)
        # -inf: everything runs once right away. (Not 0.0: monotonic time
        # counts from boot, so on a machine booted less than an hour ago the
        # first hourly scan would otherwise wait.)
        never = float("-inf")
        last_scan = never
        last_discovery = never
        last_jobs = never
        last_relay = never
        last_schedules = never
        last_housekeeping = never
        last_plan = never
        last_plan_check = never
        last_sync_check = never
        last_followup = never
        last_full_sync = never
        synced_at_seq = None
        planned_at_seq = None
        online: set[str] | None = None
        try:
            while not self.shutdown.is_set():
                try:
                    now = time.monotonic()
                    # Which directories are due depends on their update priority;
                    # looking is cheap (nothing is read when nothing is due).
                    live = (self.cfg.scan.synchronous_seconds > 0
                            and synchronous.any_synchronous(conn))
                    # Synchronous directories are looked at every few seconds, the rest every 30.
                    if self.cfg.scan.interval_minutes > 0 and now - last_scan >= (
                            self.cfg.scan.synchronous_seconds if live else SCAN_CHECK_SECONDS):
                        last_scan = now
                        if self._scan_due(conn, online):
                            self.request_scan(full=False)
                    if now - last_discovery >= DISCOVERY_SECONDS:
                        last_discovery = now
                        online = self._watch_drives(conn, online)
                    # Files being passed through other servers: ask again every few seconds
                    # instead of waiting for the next minute.
                    # Scheduled syncs that are due (or that were waiting for a node that is back).
                    if now - last_schedules >= SCHEDULE_CHECK_SECONDS or self._schedule_now:
                        last_schedules, self._schedule_now = now, False
                        if schedules.run_due(conn):
                            self.request_jobs()
                    if now - last_relay >= RELAY_POLL_SECONDS:
                        last_relay = now
                        if conn.execute("SELECT 1 FROM job_items WHERE state = 'pending' AND"
                                        " (relay_via IS NOT NULL OR fetch = 2) LIMIT 1").fetchone():
                            self.request_jobs()
                    if now - last_housekeeping >= HOUSEKEEPING_SECONDS:
                        last_housekeeping = now
                        relay.cleanup(conn, self.cfg)   # staged files nobody collected
                    if live and now - last_sync_check >= SYNC_CHECK_SECONDS:
                        last_sync_check = now
                        seq = conn.execute("SELECT MAX(seq) FROM changes").fetchone()[0]
                        if now - last_full_sync >= synchronous.FULL_PASS_SECONDS:
                            last_full_sync = now
                            synced_at_seq = seq
                            self.request_sync(full=True)
                        elif seq != synced_at_seq:
                            synced_at_seq = seq
                            self.request_sync()
                    if now - last_followup >= FOLLOWUP_SECONDS:
                        last_followup = now
                        if conn.execute("SELECT 1 FROM archive_todo LIMIT 1").fetchone():
                            if archives.followup(conn, self.cfg):
                                self.request_jobs()
                        if opaque.indexed_below(conn):     # a directory was made opaque
                            self.request_housekeeping()
                        if conn.execute("SELECT 1 FROM archives a JOIN archive_members m"
                                        " ON m.archive_id = a.id WHERE a.present = 0 LIMIT 1"
                                        ).fetchone():
                            archives.purge_members(conn)   # a removal came from another server
                    if now - last_jobs >= JOBS_SECONDS:
                        last_jobs = now
                        if conn.execute("SELECT 1 FROM jobs WHERE status IN ('pending', 'waiting')"
                                        " LIMIT 1").fetchone():
                            self.request_jobs()
                    if now - last_plan_check >= PLAN_CHECK_SECONDS:
                        last_plan_check = now
                        seq = conn.execute("SELECT MAX(seq) FROM changes").fetchone()[0]
                        # Planning walks the whole index (seconds on a big one): not more often
                        # than ten times its own duration, however busy the change log is.
                        spaced = now - last_plan >= max(PLAN_CHECK_SECONDS, 10 * self._plan_seconds)
                        if spaced and (seq != planned_at_seq or now - last_plan >= PLAN_SECONDS):
                            planned_at_seq, last_plan = seq, now
                            self.request_plan()
                except Exception as e:  # noqa: BLE001
                    # A failed look at what is due must not end the worker for good.
                    log.warning("worker scheduling failed, trying again: %s", e)
                    time.sleep(2)
                try:
                    task = self.tasks.get(timeout=1)
                except queue.Empty:
                    continue
                if task is None:
                    break
                with self._lock:
                    self._pending.discard(task)
                self.stop_requested.clear()
                self._run(conn, task)
        finally:
            conn.close()

    def _scan_due(self, conn, online: set[str] | None) -> bool:
        """Does any online node have a priority class whose scan is due?"""
        try:
            prefs = priority.Prefs(priority.load(conn))
            owned = index.owned_node_ids(conn)
            # Before the first discovery nothing is known: let the scan find out.
            ids = owned if online is None else owned & online
            return any(priority.due_classes(conn, self.cfg, nid, prefs) for nid in ids)
        except Exception as e:  # noqa: BLE001 - keep the worker alive
            log.warning("checking for due scans failed: %s", e)
            return False

    def _watch_drives(self, conn, previous: set[str] | None) -> set[str]:
        try:
            current = {loc.node_id for loc in discover(conn, self.cfg).online}
        except Exception as e:  # noqa: BLE001 - keep the worker alive
            log.warning("discovery failed: %s", e)
            return previous or set()
        if previous is not None:
            for node_id in sorted(current - previous):
                log.info("node %s came online; scanning it", node_id)
                self.request_scan([node_id], full=False)
                self.request_jobs()
                self.request_sync(full=True)    # synchronous files it missed while away
                self._schedule_now = True
        if self.syncer is not None and current != previous:
            self.syncer.request_daemon_update()
            self.syncer.request_poke()  # node online/offline announcements
        return current

    def _run(self, conn, task: tuple) -> None:
        kind = task[0]
        if kind == "housekeeping":
            try:
                if opaque.indexed_below(conn):
                    self._set(state="tidying", detail="forgetting what is below opaque directories",
                              since=db.now())
                    opaque.forget_indexed(conn, say=self._progress, stop=self.stop_requested.is_set)
                archives.purge_members(conn)
            except Stopped:
                pass
            except Exception as e:  # noqa: BLE001
                log.warning("housekeeping failed: %s", e)
            finally:
                self._set(state="idle", detail="", since=db.now())
            return
        if kind == "sync":        # frequent and quiet: no activity, no result line
            try:
                if synchronous.run(conn, self.cfg, full=task[1]):
                    self.request_jobs()
            except Exception as e:  # noqa: BLE001
                log.warning("synchronizing failed: %s", e)
            return
        state = {"scan": "scanning", "jobs": "running jobs", "plan": "planning backups"}[kind]
        db.set_label({"scan": "scan", "jobs": "running jobs", "plan": "planning backups"}[kind])
        self._set(state=state, detail="", since=db.now())
        if kind == "plan":
            started = time.monotonic()
            try:
                report, approved = replication.cycle(conn, self.cfg)
                self._plan_seconds = time.monotonic() - started
                msg = (f"backup plan: {report.proposed} proposed, "
                       f"{report.no_destination} without destination")
                if report.truncated:
                    msg += " (the rest of the files is planned as these are decided)"
                if approved is not None:
                    msg += f", {report.auto_approved} approved automatically"
                    if approved.errors:
                        msg += f" ({len(approved.errors)} error(s))"
                self._set(last_result=msg)
                if approved is not None and (approved.local_job is not None):
                    self.request_jobs()
            except Exception as e:  # noqa: BLE001
                log.exception("planning failed")
                self._set(last_error=f"planning failed: {e}")
            finally:
                self._set(state="idle", detail="", since=db.now())
            return
        stop = self.stop_requested.is_set
        try:
            with ServerLock(self.cfg.home):
                if kind == "scan":
                    _, results = scanner.scan_all(
                        conn, self.cfg, list(task[1]) or None, verify=task[2],
                        progress=self._scan_progress, stop=stop, full=task[3],
                        new_only=task[4])
                    summary = "; ".join(
                        f"{r.node_name}: +{r.stats.added} ~{r.stats.changed} "
                        f"-{r.stats.missing}" for r in results)
                    if task[3] or any(r.stats.added or r.stats.changed or r.stats.missing
                                      for r in results):   # quiet automatic scans stay quiet
                        self._set(last_result=f"scan done ({summary or 'no online node'})",
                                  last_error="")
                    if any(r.stats.added or r.stats.changed for r in results):
                        self.request_jobs()
                        self.request_sync()
                    self.request_plan()
                    if self.syncer is not None:
                        self.syncer.request_poke()
                else:
                    reports = runner.run_jobs(conn, self.cfg, None, self._progress, stop)
                    done = sum(r.done for r in reports)
                    other = {k: sum(getattr(r, k) for r in reports)
                             for k in ("skipped", "failed", "conflicts", "retry")}
                    note = "".join(f", {n} {k}" for k, n in other.items() if n)
                    self._set(last_result=f"jobs run: {done} item(s) done{note}",
                              last_error="")
                    if done and self.syncer is not None:
                        self.syncer.request_poke()
        except Stopped:
            self._set(last_result=f"{kind} stopped on request")
        except LockError as e:
            self._set(last_error=str(e))
        except Exception as e:  # noqa: BLE001 - report, keep serving
            log.exception("%s failed", kind)
            self._set(last_error=f"{kind} failed: {e}")
        finally:
            self._set(state="idle", detail="", since=db.now())


class Syncer:
    """Pulls the change log of every paired server, delivers the outbox and
    keeps the rsync daemon configuration current. Runs in its own thread so
    long scans and transfers never delay metadata propagation."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.daemon = RsyncDaemon(cfg)
        self.wake = threading.Event()
        self.shutdown = threading.Event()
        self._flags_lock = threading.Lock()
        self._daemon_dirty = True
        self._poke_peers = False
        self.thread = threading.Thread(target=self._main, name="reifs-sync", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self, timeout: float = 15) -> None:
        self.shutdown.set()
        self.wake.set()
        self.thread.join(timeout)
        self.daemon.stop()

    def request_sync(self) -> None:
        self.wake.set()

    def request_daemon_update(self) -> None:
        with self._flags_lock:
            self._daemon_dirty = True
        self.wake.set()

    def request_poke(self) -> None:
        """Ask peers to pull from us now (after local changes)."""
        with self._flags_lock:
            self._poke_peers = True
        self.wake.set()

    @staticmethod
    def _pairing_signature(conn) -> tuple:
        # Changes when servers are paired/unpaired, even by the CLI in another process.
        return tuple(conn.execute("SELECT id, paired, token_out, rsync_in_password FROM servers"
                                  " ORDER BY id").fetchall())

    def _main(self) -> None:
        db.set_label("syncing with paired servers")
        conn = db.open_existing(self.cfg.db_path)
        state = {"signature": None, "last_own": None, "next_sync": 0.0}
        failures = 0
        try:
            while not self.shutdown.is_set():
                try:
                    self._round(conn, state)
                    failures = 0
                except Exception as e:  # noqa: BLE001
                    # One failed round (the database locked for a while by a long job, a
                    # peer answering nonsense) must not end syncing for good: the thread
                    # used to die here and the servers silently stopped syncing.
                    failures += 1
                    if conn.in_transaction:
                        try:
                            conn.execute("ROLLBACK")
                        except sqlite3.Error:
                            pass
                    if failures in (1, 5) or failures % 20 == 0:
                        log.warning("sync round failed (%d in a row), trying again: %s",
                                    failures, e, exc_info=failures == 1 and not isinstance(
                                        e, sqlite3.OperationalError))
                    state["next_sync"] = 0.0
                    self.wake.wait(min(5.0 * failures, 60.0))
        finally:
            conn.close()

    def _round(self, conn, state: dict) -> None:
        current = self._pairing_signature(conn)
        if current != state["signature"]:
            state["signature"] = current
            self.request_daemon_update()
        # New local facts (also from the CLI in another process): tell peers.
        own = conn.execute("SELECT MAX(seq) FROM changes").fetchone()[0]
        if state["last_own"] is not None and own != state["last_own"]:
            self.request_poke()
        state["last_own"] = own
        with self._flags_lock:
            dirty, poke = self._daemon_dirty, self._poke_peers
            self._daemon_dirty = self._poke_peers = False
        if dirty:
            try:
                self.daemon.update(conn)
            except Exception as e:  # noqa: BLE001
                log.warning("rsync daemon update failed: %s", e)
            state["next_sync"] = 0.0  # pairing or nodes changed: sync now
        else:
            self.daemon.ensure()  # restart it if it died
        now = time.monotonic()
        if poke or self.wake.is_set() or now >= state["next_sync"]:
            self.wake.clear()
            self.sync_all(conn, poke)
            offline = conn.execute("SELECT 1 FROM servers WHERE paired = 1"
                                   " AND online = 0 LIMIT 1").fetchone()
            state["next_sync"] = time.monotonic() + (
                1.0 if CATCHING_UP else RETRY_SECONDS if offline else SYNC_SECONDS)
        self.wake.wait(TICK_SECONDS)

    def sync_all(self, conn, poke: bool = False) -> int:
        total = 0
        for peer in peers.paired_peers(conn):
            if self.shutdown.is_set():
                break
            total += sync_peer(conn, peer["id"], poke)
        if total:
            # News learned from one peer: let the others pull it from us now.
            self.request_poke()
        return total


SYNC_SLICE_SECONDS = 20     # at most this long per peer per round: the others get their turn
CATCHING_UP: set[str] = set()   # peers whose log is not fully pulled yet


def sync_peer(conn, server_id: str, poke: bool = False) -> int:
    """Pull a peer's changes and deliver our outbox to it. Returns entries applied.

    The peer counts as reachable from its first answer, not from the end of the pull
    (a newly paired server may have millions of log entries to send), progress is
    stored after every batch, and a long pull is done in slices so the other peers
    are not kept waiting: it continues in the next round.
    """
    applied = 0
    started = time.monotonic()
    try:
        client = peers.client_for(conn, server_id, timeout=30)
        if poke:
            client.request("POST", "/api/peer/poke", {})
        cursor = peers.peer_row(conn, server_id)["pull_cursor"]
        while True:
            have = ",".join(f"{o}:{n}" for o, n in sync.progress_vector(conn).items())
            data = client.request(
                "GET", f"/api/peer/changes?since={cursor}&limit={sync.BATCH}&have={have}")
            applied += sync.apply_changes(conn, data["changes"])
            cursor = int(data["last_seq"])
            more = bool(data.get("more"))
            with db.transaction(conn):
                conn.execute(
                    "UPDATE servers SET pull_cursor = ?, online = 1, last_error = NULL,"
                    " sync_note = ? WHERE id = ?",
                    (cursor, f"catching up: {applied} entries pulled so far" if more else None,
                     server_id))
            if not more:
                CATCHING_UP.discard(server_id)
                break
            CATCHING_UP.add(server_id)
            if time.monotonic() - started > SYNC_SLICE_SECONDS:
                break   # the rest in the next round
        deliver_outbox(conn, server_id, client)
        try:  # what the peer offers (SSH access, its public key)
            info = client.request("GET", "/api/peer/info")
            ssh_offer = info.get("ssh") or {}
            with db.transaction(conn):
                conn.execute(
                    "UPDATE servers SET ssh_enabled = ?, ssh_host = ?, ssh_port = ?, ssh_user = ?,"
                    " ssh_pubkey = ? WHERE id = ?",
                    (int(bool(ssh_offer.get("enabled"))), ssh_offer.get("host") or None,
                     ssh_offer.get("port"), ssh_offer.get("user"), ssh_offer.get("public_key"),
                     server_id))
        except peers.PeerError as e:
            if e.status != 404:  # older peers do not have it
                raise
        with db.transaction(conn):
            conn.execute("UPDATE servers SET online = 1, last_sync = ?, last_error = NULL"
                         " WHERE id = ?", (db.now(), server_id))
    except peers.PeerError as e:
        CATCHING_UP.discard(server_id)
        with db.transaction(conn):
            conn.execute("UPDATE servers SET online = 0, last_error = ?, sync_note = NULL"
                         " WHERE id = ?", (str(e), server_id))
    return applied


def deliver_outbox(conn, server_id: str, client: "peers.PeerClient") -> None:
    for item in conn.execute("SELECT * FROM outbox WHERE server_id = ? ORDER BY id",
                             (server_id,)).fetchall():
        try:
            client.request("POST", item["path"], json.loads(item["body"]))
        except peers.PeerError as e:
            if e.status and 400 <= e.status < 500 and e.status != 495:
                # Refused for good (e.g. the copy changed): drop it, keep a trace.
                log.warning("peer refused %s: %s", item["path"], e)
                with db.transaction(conn):
                    conn.execute("DELETE FROM outbox WHERE id = ?", (item["id"],))
                continue
            with db.transaction(conn):
                conn.execute("UPDATE outbox SET attempts = attempts + 1, last_error = ?"
                             " WHERE id = ?", (str(e), item["id"]))
            raise
        with db.transaction(conn):
            conn.execute("DELETE FROM outbox WHERE id = ?", (item["id"],))
