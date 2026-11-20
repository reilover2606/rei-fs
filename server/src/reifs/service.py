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

import logging
import queue
import threading
import time
from dataclasses import dataclass, field

from . import db, runner, scanner
from .config import Config
from .control import Stopped
from .locking import LockError, ServerLock
from .nodes import discover

log = logging.getLogger("reifs.service")

DISCOVERY_SECONDS = 30
JOBS_SECONDS = 60


@dataclass
class Activity:
    state: str = "idle"          # idle | scanning | running jobs
    detail: str = ""
    since: str = field(default_factory=db.now)
    last_error: str = ""
    last_result: str = ""


class Worker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tasks: queue.Queue = queue.Queue()
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

    def request_scan(self, nodes: list[str] | None = None, verify: bool = False) -> None:
        self._enqueue(("scan", tuple(sorted(nodes or [])), verify))

    def request_jobs(self) -> None:
        self._enqueue(("jobs",))

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

    def _progress(self, detail: str) -> None:
        with self._lock:
            self._activity.detail = detail

    def _main(self) -> None:
        conn = db.open_existing(self.cfg.db_path)
        last_scan = 0.0 if self.cfg.scan.interval_minutes > 0 else float("inf")
        last_discovery = 0.0
        last_jobs = 0.0
        online: set[str] | None = None
        try:
            while not self.shutdown.is_set():
                now = time.monotonic()
                if self.cfg.scan.interval_minutes > 0 and \
                        now - last_scan >= self.cfg.scan.interval_minutes * 60:
                    last_scan = now
                    self.request_scan()
                if now - last_discovery >= DISCOVERY_SECONDS:
                    last_discovery = now
                    online = self._watch_drives(conn, online)
                if now - last_jobs >= JOBS_SECONDS:
                    last_jobs = now
                    if conn.execute("SELECT 1 FROM jobs WHERE status IN ('pending', 'waiting')"
                                    " LIMIT 1").fetchone():
                        self.request_jobs()
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

    def _watch_drives(self, conn, previous: set[str] | None) -> set[str]:
        try:
            current = {loc.node_id for loc in discover(conn, self.cfg).online}
        except Exception as e:  # noqa: BLE001 - keep the worker alive
            log.warning("discovery failed: %s", e)
            return previous or set()
        if previous is not None:
            for node_id in sorted(current - previous):
                log.info("node %s came online; scanning it", node_id)
                self.request_scan([node_id])
                self.request_jobs()
        return current

    def _run(self, conn, task: tuple) -> None:
        kind = task[0]
        self._set(state="scanning" if kind == "scan" else "running jobs", detail="",
                  since=db.now())
        stop = self.stop_requested.is_set
        try:
            with ServerLock(self.cfg.home):
                if kind == "scan":
                    _, results = scanner.scan_all(
                        conn, self.cfg, list(task[1]) or None, verify=task[2],
                        progress=lambda s, cur: self._progress(f"{s.seen} files, {s.hashed} hashed"),
                        stop=stop)
                    summary = "; ".join(
                        f"{r.node_name}: +{r.stats.added} ~{r.stats.changed} "
                        f"-{r.stats.missing}" for r in results)
                    self._set(last_result=f"scan done ({summary or 'no online node'})",
                              last_error="")
                    if any(r.stats.added or r.stats.changed for r in results):
                        self.request_jobs()
                else:
                    reports = runner.run_jobs(conn, self.cfg, None, self._progress, stop)
                    done = sum(r.done for r in reports)
                    self._set(last_result=f"jobs run: {done} item(s) done", last_error="")
        except Stopped:
            self._set(last_result=f"{kind} stopped on request")
        except LockError as e:
            self._set(last_error=str(e))
        except Exception as e:  # noqa: BLE001 - report, keep serving
            log.exception("%s failed", kind)
            self._set(last_error=f"{kind} failed: {e}")
        finally:
            self._set(state="idle", detail="", since=db.now())
