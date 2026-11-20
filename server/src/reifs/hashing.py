"""Content hashing.

BLAKE2b-256 from the standard library: fast on x86 and ARM, no compiled
dependency (important for Termux). Hashes are stored as ``blake2b:<hex>`` so
the algorithm can change later without ambiguity.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

ALGORITHM = "blake2b"


def _new():
    return hashlib.blake2b(digest_size=32)


def hash_file(path: Path | str) -> str:
    with open(path, "rb") as f:
        digest = hashlib.file_digest(f, _new)
    return f"{ALGORITHM}:{digest.hexdigest()}"


def hash_bytes(data: bytes) -> str:
    h = _new()
    h.update(data)
    return f"{ALGORITHM}:{h.hexdigest()}"


def short(h: str, n: int = 12) -> str:
    return h.split(":", 1)[-1][:n]
