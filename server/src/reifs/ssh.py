"""rsync over SSH between servers.

The default transport between servers is each server's read-only rsync
daemon. That protocol is unencrypted and meant for a LAN or VPN. A server
can instead offer SSH (``[ssh] enabled``): paired servers then pull with
``rsync -e ssh user@host:<node root>/`` over an encrypted connection.

* Each server has its own Ed25519 key (``<home>/ssh/id_ed25519``),
  created with the ``cryptography`` package. Public keys are exchanged
  with paired servers during sync.
* The server offering SSH must accept its peers' keys: ``reifs ssh
  authorize`` writes them to ``~/.ssh/authorized_keys``. If ``rrsync`` (it
  comes with rsync) is installed, each key is restricted with
  ``restrict,command="rrsync -ro /"``, which allows read-only rsync and
  nothing else, matching the read-only daemon.
* The pulling server pins host keys on first use in
  ``<home>/ssh/known_hosts`` (``StrictHostKeyChecking=accept-new``).
* Which transport to use is chosen per paired server: ``reifs peer
  transport NAME ssh|daemon``, or ``t`` in the client's Servers tab.
"""

from __future__ import annotations

import getpass
import os
import shutil
import sqlite3
from pathlib import Path

from .config import Config

KEY_NAME = "id_ed25519"


def ssh_dir(home: Path) -> Path:
    return Path(home) / "ssh"


def key_path(home: Path) -> Path:
    return ssh_dir(home) / KEY_NAME


def known_hosts(home: Path) -> Path:
    return ssh_dir(home) / "known_hosts"


def ensure_key(home: Path, comment: str) -> str:
    """Create the server's key pair if needed; return the public key line."""
    priv, pub = key_path(home), key_path(home).with_suffix(".pub")
    if priv.exists() and pub.exists():
        return pub.read_text().strip()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(priv.parent, 0o700)
    except OSError:
        pass
    key = Ed25519PrivateKey.generate()
    data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                             serialization.NoEncryption())
    fd = os.open(priv, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    line = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                         serialization.PublicFormat.OpenSSH).decode()
    line = f"{line} rei-fs:{comment}"
    pub.write_text(line + "\n")
    return line


def local_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""


def offer(cfg: Config) -> dict:
    """What we tell paired servers about SSH access to this machine."""
    return {"enabled": cfg.ssh.enabled, "host": cfg.ssh.host, "port": cfg.ssh.port,
            "user": cfg.ssh.user or local_user(),
            "public_key": ensure_key(cfg.home, cfg.server.name)}


def authorized_line(public_key: str, peer_name: str, rrsync: str | None) -> str:
    key = " ".join(public_key.split()[:2])  # type and base64, without the comment
    comment = f"rei-fs peer {peer_name}".replace(" ", "-")
    if rrsync:
        return f'restrict,command="{rrsync} -ro /" {key} {comment}'
    return f"restrict {key} {comment}"


def authorize_lines(conn: sqlite3.Connection) -> tuple[list[str], str | None, list[str]]:
    """authorized_keys lines for every paired server whose key we know.

    Returns (lines, rrsync path or None, names of peers without a known key).
    """
    rrsync = shutil.which("rrsync")
    lines, missing = [], []
    for p in conn.execute("SELECT name, ssh_pubkey FROM servers WHERE paired = 1 ORDER BY name"):
        if p["ssh_pubkey"]:
            lines.append(authorized_line(p["ssh_pubkey"], p["name"], rrsync))
        else:
            missing.append(p["name"])
    return lines, rrsync, missing


def install(lines: list[str], path: Path | None = None) -> tuple[Path, int]:
    """Append the lines missing from authorized_keys. Returns (file, lines added)."""
    path = path or Path.home() / ".ssh" / "authorized_keys"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    existing = path.read_text().splitlines() if path.exists() else []
    keys_present = {" ".join(_key_of(line)) for line in existing}
    new = [line for line in lines if " ".join(_key_of(line)) not in keys_present]
    if new:
        needs_newline = path.exists() and path.stat().st_size > 0 and \
            not path.read_bytes().endswith(b"\n")
        with open(path, "a", encoding="utf-8") as f:
            f.write(("\n" if needs_newline else "") + "".join(line + "\n" for line in new))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path, len(new)


def _key_of(line: str) -> list[str]:
    parts = line.split()
    for i, part in enumerate(parts):
        if part.startswith(("ssh-", "ecdsa-", "sk-")) and i + 1 < len(parts):
            return parts[i:i + 2]
    return []
