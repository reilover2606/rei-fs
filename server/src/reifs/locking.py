"""A per-server lock so scans and job runs never overlap."""

from __future__ import annotations

import os
import sys
from pathlib import Path


class LockError(Exception):
    pass


class ServerLock:
    def __init__(self, home: Path):
        self.path = Path(home) / "reifs.lock"
        self.file = None

    def acquire(self) -> "ServerLock":
        f = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            raise LockError("another scan or job run is in progress on this server") from None
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        self.file = f
        return self

    def release(self) -> None:
        if self.file is None:
            return
        if sys.platform == "win32":
            import msvcrt

            self.file.seek(0)
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        self.file.close()  # closing releases flock on POSIX
        self.file = None

    def __enter__(self) -> "ServerLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
