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

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config

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
    source = remote.url() if remote else rsync_path(src_root, style).rstrip("/\\") + "/"
    daemon_opts = ["--contimeout=20", "--timeout=300"] if remote else []
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


def run_batch(cfg: Config, src_root: Path | None, dst_root: Path, rel_paths: list[str],
              progress: ProgressFn | None = None, remote=None) -> BatchResult:
    """Copy ``rel_paths`` (``/``-separated, relative to the roots).

    With ``remote`` (a peers.RemoteSource) the files are pulled from a paired
    server's rsync daemon instead of the local ``src_root``.

    KeyboardInterrupt terminates rsync (keeping its partial file) and is
    re-raised.
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
        if remote is not None:
            env = dict(os.environ, RSYNC_PASSWORD=remote.password)
        with tempfile.TemporaryFile(dir=tmp_dir) as err:
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err,
                                        stdin=subprocess.DEVNULL, env=env)
            except OSError as e:
                return BatchResult(127, f"cannot run rsync ({cfg.rsync.path}): {e}")
            try:
                _pump_progress(proc, progress)
                proc.wait()
            except BaseException:
                _stop(proc)
                raise
            err.seek(0)
            stderr = err.read().decode("utf-8", "replace").strip()
        return BatchResult(proc.returncode, stderr)
    finally:
        try:
            list_file.unlink()
        except OSError:
            pass


def _pump_progress(proc: subprocess.Popen, progress: ProgressFn | None) -> None:
    assert proc.stdout is not None
    buf = b""
    while True:
        chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
        if not chunk:
            break
        buf += chunk
        *lines, buf = re.split(rb"[\r\n]", buf)
        if progress:
            for line in reversed(lines):
                m = _PROGRESS.match(line)
                if m:
                    progress(int(re.sub(rb"[^\d]", b"", m.group(1)) or b"0"))
                    break


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()  # rsync keeps the partial file on SIGTERM
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
