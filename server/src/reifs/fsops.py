"""The few filesystem modifications a server may make on a node.

* create the directories needed to hold a file (``ensure_dirs``);
* remove directories left empty by vanished files (``prune``), with
  ``rmdir``, which cannot remove anything non-empty;
* rename a file within a node, only when the user approved it.

Files are never deleted.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import paths


class Refused(Exception):
    """The operation would not be safe or legal on this node."""


def ensure_dirs(root: Path, vdir: str, case_sensitive: bool) -> None:
    """Create the directories of vdir below root, one component at a time.

    On a case-insensitive node an existing directory that differs only in
    case would silently receive the file under the wrong virtual path, so
    that is refused.
    """
    current = Path(root)
    for part in paths.split(vdir):
        target = current / part
        if not case_sensitive:
            try:
                names = os.listdir(current)
            except FileNotFoundError:
                names = []
            if part not in names:
                clash = next((n for n in names if n.casefold() == part.casefold()), None)
                if clash is not None:
                    raise Refused(
                        f"directory {part!r} would collide with existing {clash!r} "
                        "on a case-insensitive node"
                    )
        try:
            os.mkdir(target)
        except FileExistsError:
            if not target.is_dir() or target.is_symlink():
                raise Refused(f"{target} exists and is not a directory")
        current = target


def prune(root: Path, vdir: str) -> int:
    """rmdir vdir and its parents while they are empty. Returns dirs removed."""
    removed = 0
    while vdir != "/":
        try:
            os.rmdir(paths.to_physical(root, vdir))
            removed += 1
        except FileNotFoundError:
            pass
        except OSError:
            break  # not empty: stop climbing
        vdir = paths.parent(vdir)
    return removed


def rename_file(src: Path, dst: Path) -> None:
    """Rename without ever replacing an existing file (os.rename would on POSIX)."""
    if os.path.lexists(dst):
        raise Refused(f"{dst} already exists")
    if hasattr(os, "link") and os.name != "nt":
        try:
            os.link(src, dst)  # fails atomically if dst exists
        except FileExistsError:
            raise Refused(f"{dst} already exists") from None
        except OSError:
            pass  # filesystem without hard links (FAT, exFAT): fall back
        else:
            os.unlink(src)  # the same data stays reachable at dst
            return
    os.rename(src, dst)
