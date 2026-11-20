"""Creating and opening a server home."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from . import db, nodes
from .config import Config, ConfigError


def init_server(home: Path, name: str, root: Path) -> Config:
    home = home.resolve()
    cfg = Config(home=home)
    if cfg.config_path.exists() or cfg.db_path.exists():
        raise ConfigError(f"a rei-fs server already exists at {home}")
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    existing = nodes.read_marker(root)
    if existing is not None:
        raise ConfigError(
            f"{root} is already node {existing.name!r} (owned by {existing.owner_id})"
        )

    home.mkdir(parents=True, exist_ok=True)
    cfg.server.name = name
    cfg.storage.root = str(root)
    server_id = str(uuid.uuid4())
    conn = db.create(cfg.db_path)
    try:
        with db.transaction(conn):
            db.set_meta(conn, "server_id", server_id)
            conn.execute(
                "INSERT INTO servers(id, name, is_self) VALUES (?, ?, 1)", (server_id, name)
            )
        nodes.register_node(conn, root, name, kind="server", node_id=server_id)
    finally:
        conn.close()
    cfg.save()
    return cfg


def open_server(home: Path) -> tuple[Config, sqlite3.Connection]:
    cfg = Config.load(home)
    return cfg, db.open_existing(cfg.db_path)
