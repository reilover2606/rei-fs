"""Running rsync for a batch of files between two local node roots.

One rsync process copies a batch listed with ``--files-from`` (NUL
separated). Important options:

* ``--times``: the modification time is the version marker, keep it;
* ``--ignore-times``: never trust size+mtime; we decided what to send;
* ``--no-whole-file``: use the delta algorithm even locally, so a partial
  file left by an interrupted transfer is reused instead of re-sent;
* ``--partial-dir`` / ``--temp-dir`` inside ``<dst>/.rei-fs/`` (same
  filesystem as the destination): an interrupted transfer leaves nothing in
  the user's directories, and the final rename into place is atomic;
* no ``--perms``/``--owner``: FAT, exFAT and Android cannot keep them.

Correctness never depends on rsync's exit code: every file is verified by
hash afterwards.
"""

from __future__ import annotations

import functools
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config
from .control import StopFn, check

SCRATCH = ".rei-fs"
_PROGRESS = re.compile(rb"^\s*([\d,.']+)\s+\d+%")


def resolve_style(style: str) -> str:
    if style == "auto":
        return "cygwin" if sys.platform == "win32" else "native"
    return style


def rsync_path(p: Path | str, style: str) -> str:
    """Convert a local path to the form the configured rsync build expects.

    Windows rsync builds would read ``C:\\x`` as host ``C``, so drive paths
    become ``/cygdrive/c/x`` (cwRsync, Cygwin) or ``/c/x`` (MSYS2).
    """
    s = str(p)
    style = resolve_style(style)
    if style == "native":
        return s
    drive, rest = os.path.splitdrive(s)
    if len(drive) == 2 and drive[1] == ":":
        rest = rest.replace("\\", "/")
        prefix = "/cygdrive/" if style == "cygwin" else "/"
        return f"{prefix}{drive[0].lower()}{rest}"
    if drive.startswith(("\\\\", "//")):  # UNC: \\server\share
        return (drive + rest).replace("\\", "/")
    return s.replace("\\", "/")


@dataclass
class BatchResult:
    returncode: int
    stderr: str


ProgressFn = Callable[[int], None]  # bytes transferred so far in this batch


def build_command(cfg: Config, src_root: Path | None, dst_root: Path, list_file: Path,
                  remote=None) -> list[str]:
    """rsync command line; ``remote`` (peers.RemoteSource) pulls from a peer's daemon."""
    style = cfg.rsync.path_style
    scratch = Path(dst_root) / SCRATCH
    source = remote.url(manual_escape=needs_manual_escape(cfg)) if remote \
        else rsync_path(src_root, style).rstrip("/\\") + "/"
    if remote is None:
        daemon_opts = []
    elif getattr(remote, "password", None) is None:  # over SSH
        daemon_opts = ["-e", ssh_command(cfg, remote), "--timeout=120"]
    else:  # rsync daemon
        daemon_opts = ["--contimeout=20", "--timeout=120"]
    return [
        cfg.rsync.path,
        "--times",
        "--ignore-times",
        "--omit-dir-times",
        "--no-whole-file",
        f"--partial-dir={rsync_path(scratch / 'partial', style)}",
        f"--temp-dir={rsync_path(scratch / 'tmp', style)}",
        "--from0",
        f"--files-from={rsync_path(list_file, style)}",
        "--info=progress2",
        *daemon_opts,
        *cfg.rsync.extra_args,
        source,
        rsync_path(dst_root, style).rstrip("/\\") + "/",
    ]


@functools.lru_cache(maxsize=8)
def rsync_version(rsync: str) -> tuple[int, int, int]:
    """(major, minor, patch) of the rsync program; a recent one if it cannot be told."""
    try:
        out = subprocess.run([rsync, "--version"], capture_output=True, text=True, timeout=10,
                             errors="replace").stdout
        m = re.search(r"version\s+(\d+)\.(\d+)(?:\.(\d+))?", out)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    except (OSError, subprocess.SubprocessError):
        pass
    return (3, 9, 0)


def needs_manual_escape(cfg: Config) -> bool:
    """Does this rsync send a remote (SSH) path unprotected?

    rsync 3.2.4 started to backslash-escape remote arguments itself; before
    that, or with RSYNC_OLD_ARGS set, it does not and we must (see
    peers.escape_remote_path).
    """
    if os.environ.get("RSYNC_OLD_ARGS"):
        return True
    return rsync_version(cfg.rsync.path) < (3, 2, 4)


def ssh_command(cfg: Config, remote) -> str:
    """The -e value for rsync over SSH (rsync splits it, honouring quotes)."""
    from . import ssh  # local import: ssh imports config only, but keep transfer light

    style = cfg.rsync.path_style
    key = rsync_path(ssh.key_path(cfg.home), style)
    known = rsync_path(ssh.known_hosts(cfg.home), style)
    return (f'{cfg.ssh.command} -p {int(remote.port)} -i "{key}" -o BatchMode=yes'
            f' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new'
            f' -o UserKnownHostsFile="{known}" -o ConnectTimeout=20')


# rsync processes running now, so that a forced shutdown can kill them.
_ACTIVE: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()
POLL_SECONDS = 0.5


def run_batch(cfg: Config, src_root: Path | None, dst_root: Path, rel_paths: list[str],
              progress: ProgressFn | None = None, remote=None,
              stop: StopFn | None = None) -> BatchResult:
    """Copy ``rel_paths`` (``/``-separated, relative to the roots).

    With ``remote`` (a peers.RemoteSource or SshSource) the files are pulled
    from a paired server instead of the local ``src_root``.

    ``stop`` is polled twice a second whether or not rsync prints anything.
    When it returns True, rsync (all of its processes) is terminated, keeping
    its partial file, and ``control.Stopped`` is raised. KeyboardInterrupt
    does the same.
    """
    scratch = Path(dst_root) / SCRATCH
    (scratch / "partial").mkdir(parents=True, exist_ok=True)
    (scratch / "tmp").mkdir(parents=True, exist_ok=True)
    tmp_dir = cfg.home / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, list_name = tempfile.mkstemp(prefix="files-", suffix=".lst", dir=tmp_dir)
    list_file = Path(list_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b"\0".join(p.encode("utf-8") for p in rel_paths) + b"\0")
        cmd = build_command(cfg, src_root, dst_root, list_file, remote)
        env = None
        if remote is not None and getattr(remote, "password", None) is not None:
            env = dict(os.environ, RSYNC_PASSWORD=remote.password)       # rsync daemon

        with tempfile.TemporaryFile(dir=tmp_dir) as err:
            try:
                # Its own process group (POSIX): rsync is several processes, and
                # we must be able to end all of them, whatever the terminal does.
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err,
                                        stdin=subprocess.DEVNULL, env=env,
                                        start_new_session=(os.name == "posix"))
            except OSError as e:
                return BatchResult(127, f"cannot run rsync ({cfg.rsync.path}): {e}")
            with _ACTIVE_LOCK:
                _ACTIVE.add(proc)
            try:
                _pump_progress(proc, progress, stop)
                proc.wait()
            except BaseException:
                _stop(proc)
                raise
            finally:
                _kill_group(proc)  # stragglers still holding the pipe
                with _ACTIVE_LOCK:
                    _ACTIVE.discard(proc)
            err.seek(0)
            stderr = err.read().decode("utf-8", "replace").strip()
        return BatchResult(proc.returncode, stderr)
    finally:
        try:
            list_file.unlink()
        except OSError:
            pass


def _pump_progress(proc: subprocess.Popen, progress: ProgressFn | None,
                   stop: StopFn | None = None) -> None:
    """Read rsync's progress until it ends, checking ``stop`` all the time.

    The reading is done in a thread: a blocking read would not notice a stop
    request while rsync is silent (stalled network, a long checksum), nor an
    rsync that died while a child process still holds the pipe open.
    """
    assert proc.stdout is not None
    chunks: queue.Queue = queue.Queue()

    def reader() -> None:
        try:
            while True:
                data = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") \
                    else proc.stdout.read(4096)
                if not data:
                    break
                chunks.put(data)
        except (OSError, ValueError):  # pipe closed under us
            pass
        finally:
            chunks.put(None)

    threading.Thread(target=reader, name="reifs-rsync-output", daemon=True).start()
    buf = b""
    while True:
        try:
            chunk = chunks.get(timeout=POLL_SECONDS)
        except queue.Empty:
            check(stop)
            if proc.poll() is not None:
                break  # rsync is gone, even if a leftover child keeps the pipe open
            continue
        if chunk is None:
            break
        buf += chunk
        *lines, buf = re.split(rb"[\r\n]", buf)
        if progress:
            for line in reversed(lines):
                m = _PROGRESS.match(line)
                if m:
                    progress(int(re.sub(rb"[^\d]", b"", m.group(1)) or b"0"))
                    break
        check(stop)


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    if os.name != "posix":
        return
    try:
        os.killpg(proc.pid, sig)  # the group was created by start_new_session
    except (ProcessLookupError, PermissionError):
        pass


def _kill_group(proc: subprocess.Popen) -> None:
    _signal_group(proc, signal.SIGKILL if os.name == "posix" else 0)


def _stop(proc: subprocess.Popen) -> None:
    """End rsync, politely first (it keeps its partial file on SIGTERM)."""
    if os.name == "posix":
        _signal_group(proc, signal.SIGTERM)
    elif proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            _kill_group(proc)
        else:
            proc.kill()
        proc.wait()
    _kill_group(proc)


def kill_all() -> None:
    """Forced shutdown: kill every rsync still running."""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
    for proc in procs:
        _kill_group(proc)
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
