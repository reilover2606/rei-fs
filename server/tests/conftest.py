from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from reifs import db, nodes, scanner
from reifs.config import Config
from reifs.server import init_server


@dataclass
class Env:
    cfg: Config
    conn: object
    root: Path            # server node root
    tmp: Path

    @property
    def server_id(self) -> str:
        return nodes.self_id(self.conn)

    def add_node(self, name: str) -> tuple[str, Path]:
        path = self.tmp / name
        path.mkdir()
        return nodes.register_node(self.conn, path, name), path

    def scan(self, **kw):
        _, results = scanner.scan_all(self.conn, self.cfg, **kw)
        return {r.node_name: r for r in results}

    def decisions(self, kind: str | None = None):
        q = "SELECT * FROM decisions WHERE status = 'open'"
        rows = self.conn.execute(q).fetchall()
        return [r for r in rows if kind is None or r["kind"] == kind]


def write(path: Path, content: str, mtime: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def env(tmp_path: Path) -> Env:
    root = tmp_path / "server"
    cfg = init_server(tmp_path / "home", "srv", root)
    cfg.discovery.paths = [str(tmp_path / "nomounts")]  # don't scan the real /media
    conn = db.open_existing(cfg.db_path)
    yield Env(cfg, conn, root, tmp_path)
    conn.close()
