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


@dataclass
class StorageSection:
    root: str = ""


@dataclass
class ScanSection:
    interval_minutes: int = 60
    ignore_hidden_dirs: bool = True
    ignore: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE))


@dataclass
class DiscoverySection:
    paths: list[str] = field(default_factory=list)
    max_depth: int = 3


@dataclass
class RsyncSection:
    path: str = "rsync"


@dataclass
class Config:
    home: Path
    server: ServerSection = field(default_factory=ServerSection)
    storage: StorageSection = field(default_factory=StorageSection)
    scan: ScanSection = field(default_factory=ScanSection)
    discovery: DiscoverySection = field(default_factory=DiscoverySection)
    rsync: RsyncSection = field(default_factory=RsyncSection)

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
        for section_name in ("server", "storage", "scan", "discovery", "rsync"):
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
                setattr(section, k, v)
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

        return f"""\
# rei-fs server configuration. See docs/DESIGN.md.

[server]
name = {s(self.server.name)}
listen = {s(self.server.listen)}
# scrypt hash of the admin password; set it with `reifs passwd`.
password_hash = {s(self.server.password_hash)}
# Approve replication ("wanted") jobs automatically.
auto_replicate = {b(self.server.auto_replicate)}

[storage]
# This server's own node root.
root = {s(self.storage.root)}

[scan]
interval_minutes = {self.scan.interval_minutes}
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
"""
