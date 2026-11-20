"""Virtual paths and name portability checks.

A virtual path (vpath) is a ``/``-separated path relative to a node root and
always starts with ``/``. The root of the virtual tree is ``/``. File names
are stored exactly as the filesystem reports them (no Unicode normalization)
so a vpath always maps back to the physical file.
"""

from __future__ import annotations

import os
from pathlib import Path

ILLEGAL_CHARS = frozenset('<>:"\\|?*') | frozenset(chr(i) for i in range(32))
RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)
MAX_NAME_BYTES = 255

# Issue codes stored in files.issues (comma separated).
ISSUE_CHARS = "chars"
ISSUE_RESERVED = "reserved"
ISSUE_TRAILING = "trailing"
ISSUE_LENGTH = "length"
ISSUE_CASE = "case"

# Issues that make a file impossible to store on a node with restricted
# (Windows/FAT/exFAT) naming rules.
RESTRICTED_NAME_ISSUES = frozenset({ISSUE_CHARS, ISSUE_RESERVED, ISSUE_TRAILING})


class VPathError(ValueError):
    pass


def normalize(vpath: str) -> str:
    """Normalize user input into a canonical vpath.

    ``documents/1.txt``, ``/documents/1.txt/`` and (on Windows)
    ``\\documents\\1.txt`` all become ``/documents/1.txt``.
    """
    if os.sep == "\\":
        vpath = vpath.replace("\\", "/")
    parts = [p for p in vpath.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise VPathError(f"'..' is not allowed in a virtual path: {vpath!r}")
    return "/" + "/".join(parts)


def split(vpath: str) -> list[str]:
    return [p for p in vpath.split("/") if p]


def join(parent: str, name: str) -> str:
    return (parent.rstrip("/") + "/" + name) if parent != "/" else "/" + name


def parent(vpath: str) -> str:
    head = vpath.rsplit("/", 1)[0]
    return head or "/"


def basename(vpath: str) -> str:
    return vpath.rsplit("/", 1)[-1]


def key(vpath: str) -> str:
    """Case-insensitive comparison key, used to detect case collisions."""
    return vpath.casefold()


def to_physical(root: Path | str, vpath: str) -> Path:
    return Path(root).joinpath(*split(vpath))


def prefix_range(vdir: str) -> tuple[str, str]:
    """Bounds such that ``lo <= vpath < hi`` selects every vpath below vdir.

    Lets SQLite use the unique index on files.vpath instead of LIKE (which
    would also need escaping of ``%`` and ``_``).
    """
    lo = vdir.rstrip("/") + "/"
    hi = lo[:-1] + chr(ord("/") + 1)
    return lo, hi


def name_issues(name: str) -> set[str]:
    issues: set[str] = set()
    if any(c in ILLEGAL_CHARS for c in name):
        issues.add(ISSUE_CHARS)
    stem = name.split(".", 1)[0].rstrip(" ").upper()
    if stem in RESERVED_NAMES:
        issues.add(ISSUE_RESERVED)
    if name.endswith((".", " ")):
        issues.add(ISSUE_TRAILING)
    if len(name.encode("utf-8", "surrogatepass")) > MAX_NAME_BYTES:
        issues.add(ISSUE_LENGTH)
    return issues


def portability_issues(vpath: str) -> set[str]:
    """Issues intrinsic to the vpath itself (case collisions are checked in the DB)."""
    issues: set[str] = set()
    for part in split(vpath):
        issues |= name_issues(part)
    return issues


def encode_issues(issues: set[str]) -> str:
    return ",".join(sorted(issues))


def decode_issues(text: str) -> set[str]:
    return {i for i in text.split(",") if i}
