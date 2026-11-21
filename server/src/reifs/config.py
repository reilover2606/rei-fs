"""Server home directory and ``config.toml``."""

from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "config.toml"
DB_NAME = "reifs.db"
DEFAULT_PORT = 7420

DEFAULT_IGNORE = [
    "Thumbs.db",
    "desktop.ini",
    ".DS_Store",
    "~$*",
    "*.tmp",
    "$RECYCLE.BIN",
    "System Volume Information",
    "lost+found",
]


SECTIONS = ("server", "storage", "scan", "discovery", "rsync", "transfers", "replication",
            "ssh")
PATH_STYLES = ("auto", "native", "cygwin", "msys")


class ConfigError(Exception):
    pass


def default_home() -> Path:
    env = os.environ.get("REIFS_HOME")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "rei-fs"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "rei-fs"


@dataclass
class ServerSection:
    name: str = ""
    listen: str = f"0.0.0.0:{DEFAULT_PORT}"
    password_hash: str = ""
    auto_replicate: bool = False
    # How other servers reach this one (HOST:PORT). Empty: the address they
    # see our requests coming from, with the listen port.
    advertise: str = ""


@dataclass
class StorageSection:
    root: str = ""


@dataclass
class ScanSection:
    # Minutes between automatic scans of a directory, by its update priority.
    # interval_minutes is for priority 2 (the default) and 0 turns every
    # automatic scan off; archival (3) is only scanned the first time and on request.
    interval_minutes: int = 60
    synchronous_minutes: int = 1
    frequent_minutes: int = 10
    archival_minutes: int = 0
    ignore_hidden_dirs: bool = True
    ignore: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE))


    def minutes(self, priority: int) -> int:
        return (self.synchronous_minutes, self.frequent_minutes, self.interval_minutes,
                self.archival_minutes)[priority]


@dataclass
class DiscoverySection:
    paths: list[str] = field(default_factory=list)
    max_depth: int = 3


@dataclass
class RsyncSection:
    path: str = "rsync"
    # How local paths are passed to rsync: "native" (/home/x, also fine for
    # Termux), "cygwin" (/cygdrive/c/x: cwRsync, Cygwin), "msys" (/c/x: MSYS2).
    # "auto" = native on Linux, cygwin on Windows.
    path_style: str = "auto"
    extra_args: list[str] = field(default_factory=list)
    # rsync daemon serving our nodes (read-only) to paired servers.
    daemon_port: int = 7421


@dataclass
class ReplicationSection:
    # Good copies every file should have (on different nodes).
    copies: int = 2
    # More copies for files with these tags, e.g. {"important": 3}.
    tag_copies: dict[str, int] = field(default_factory=dict)


@dataclass
class SshSection:
    # Let paired servers pull from this machine over SSH instead of the rsync
    # daemon (for servers that are not on the same LAN or VPN). Needs an SSH
    # server here; 'reifs ssh authorize' installs the peers' keys.
    enabled: bool = False
    host: str = ""      # host peers connect to ("" = our address as they know it)
    port: int = 22
    user: str = ""      # account on this machine ("" = the user running rei-fs)
    # ssh client used when this server pulls from peers over SSH.
    command: str = "ssh"


@dataclass
class TransfersSection:
    batch_files: int = 500
    batch_mb: int = 1024
    min_free_mb: int = 512
    max_attempts: int = 5


@dataclass
class Config:
    home: Path
    server: ServerSection = field(default_factory=ServerSection)
    storage: StorageSection = field(default_factory=StorageSection)
    scan: ScanSection = field(default_factory=ScanSection)
    discovery: DiscoverySection = field(default_factory=DiscoverySection)
    rsync: RsyncSection = field(default_factory=RsyncSection)
    transfers: TransfersSection = field(default_factory=TransfersSection)
    replication: ReplicationSection = field(default_factory=ReplicationSection)
    ssh: SshSection = field(default_factory=SshSection)

    @property
    def config_path(self) -> Path:
        return self.home / CONFIG_NAME

    @property
    def db_path(self) -> Path:
        return self.home / DB_NAME

    @classmethod
    def load(cls, home: Path) -> "Config":
        path = home / CONFIG_NAME
        if not path.exists():
            raise ConfigError(f"no rei-fs server at {home} (run 'reifs init' first)")
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from e
        cfg = cls(home=home)
        for section_name in SECTIONS:
            section = getattr(cfg, section_name)
            for k, v in data.get(section_name, {}).items():
                if not hasattr(section, k):
                    raise ConfigError(f"{path}: unknown key [{section_name}].{k}")
                expected = type(getattr(section, k))
                if expected is int and isinstance(v, bool) or not isinstance(v, expected):
                    raise ConfigError(
                        f"{path}: [{section_name}].{k} must be {expected.__name__}"
                    )
                if expected is list and not all(isinstance(i, str) for i in v):
                    raise ConfigError(f"{path}: [{section_name}].{k} must be a list of strings")
                if expected is dict and not all(
                        isinstance(a, str) and isinstance(b, int) and not isinstance(b, bool)
                        for a, b in v.items()):
                    raise ConfigError(f"{path}: [{section_name}].{k} must map names to numbers")
                setattr(section, k, v)
        if cfg.replication.copies < 1 or any(n < 1 for n in cfg.replication.tag_copies.values()):
            raise ConfigError(f"{path}: copy goals must be at least 1")
        if cfg.rsync.path_style not in PATH_STYLES:
            raise ConfigError(f"{path}: [rsync].path_style must be one of {', '.join(PATH_STYLES)}")
        return cfg

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".toml.tmp")
        tmp.write_text(self.render(), encoding="utf-8")
        os.replace(tmp, self.config_path)

    def render(self) -> str:
        def s(v: str) -> str:
            # JSON string escaping is valid TOML basic-string escaping.
            return json.dumps(v, ensure_ascii=False)

        def lst(vs: list[str]) -> str:
            if not vs:
                return "[]"
            return "[\n" + "".join(f"    {s(v)},\n" for v in vs) + "]"

        def b(v: bool) -> str:
            return "true" if v else "false"

        def tbl(d: dict[str, int]) -> str:
            return "{ " + ", ".join(f"{s(k)} = {v}" for k, v in sorted(d.items())) + " }" \
                if d else "{}"

        return f"""\
# rei-fs server configuration. See docs/DESIGN.md.

[server]
name = {s(self.server.name)}
listen = {s(self.server.listen)}
# scrypt hash of the admin password; set it with `reifs passwd`.
password_hash = {s(self.server.password_hash)}
# Approve replication ("wanted") jobs automatically.
auto_replicate = {b(self.server.auto_replicate)}
# HOST:PORT other servers use to reach this one ("" = detect).
advertise = {s(self.server.advertise)}

[storage]
# This server's own node root.
root = {s(self.storage.root)}

[scan]
# Minutes between automatic scans of a directory, by update priority
# (set per directory in the client; subdirectories inherit it):
#   0 synchronous  1 frequent  2 infrequent (the default)  3 archival
# interval_minutes is priority 2; 0 there turns ALL automatic scans off. 0 for
# the others means "only the first time and when asked". A scan you start
# yourself always covers everything.
interval_minutes = {self.scan.interval_minutes}
synchronous_minutes = {self.scan.synchronous_minutes}
frequent_minutes = {self.scan.frequent_minutes}
archival_minutes = {self.scan.archival_minutes}
# Skip directories whose name starts with '.'.
ignore_hidden_dirs = {b(self.scan.ignore_hidden_dirs)}
# Glob patterns. Without '/', a pattern matches a file or directory name;
# with '/', it matches a virtual path (e.g. "/scratch/*").
ignore = {lst(self.scan.ignore)}

[discovery]
# Where to look for storage nodes (mounted drives). Empty = platform defaults.
paths = {lst(self.discovery.paths)}
max_depth = {self.discovery.max_depth}

[rsync]
path = {s(self.rsync.path)}
# auto | native | cygwin (/cygdrive/c/..., cwRsync) | msys (/c/..., MSYS2)
path_style = {s(self.rsync.path_style)}
extra_args = {lst(self.rsync.extra_args)}
# Port of the rsync daemon that serves our nodes, read-only, to paired servers.
daemon_port = {self.rsync.daemon_port}

[transfers]
# Files are sent in batches of at most this many files / megabytes.
batch_files = {self.transfers.batch_files}
batch_mb = {self.transfers.batch_mb}
# Never fill a destination below this much free space.
min_free_mb = {self.transfers.min_free_mb}
# Transient failures before an item is marked failed.
max_attempts = {self.transfers.max_attempts}

[replication]
# Good copies every file should have, on different nodes.
copies = {self.replication.copies}
# More copies for files with these tags (the highest goal applies),
# e.g. {{ "important" = 3, "photos" = 3 }}.
tag_copies = {tbl(self.replication.tag_copies)}

[ssh]
# Let paired servers pull from this machine over SSH instead of the rsync
# daemon (encrypted; for servers not on the same LAN/VPN). Install their keys
# with 'reifs ssh authorize'.
enabled = {b(self.ssh.enabled)}
host = {s(self.ssh.host)}
port = {self.ssh.port}
user = {s(self.ssh.user)}
# ssh client used when pulling from peers over SSH.
command = {s(self.ssh.command)}
"""
