"""Stopping transfers and the server: a hung rsync must never block shutdown."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reifs import control, db, jobs, scanner, transfer
from reifs.config import Config
from reifs.server import init_server

from conftest import write

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")

MARK = "61.37"      # a sleep duration nothing else uses, to find stray processes


def strays() -> list[str]:
    out = subprocess.run(["pgrep", "-af", f"sleep {MARK}"], capture_output=True, text=True)
    return [ln for ln in out.stdout.splitlines() if "pgrep" not in ln]


def fake_rsync(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-rsync"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return script


@pytest.fixture(autouse=True)
def no_strays():
    yield
    subprocess.run(["pkill", "-f", f"sleep {MARK}"])


def cfg_with(tmp_path: Path, script: Path) -> Config:
    cfg = Config(home=tmp_path / "home")
    cfg.home.mkdir()
    cfg.rsync.path = str(script)
    return cfg


def run(cfg, tmp_path, stop=None):
    dst = tmp_path / "dst"
    dst.mkdir(exist_ok=True)
    return transfer.run_batch(cfg, tmp_path / "src", dst, ["a.txt"], stop=stop)


def test_stop_while_rsync_is_silent(tmp_path):
    """No output at all from rsync: the stop request is still noticed."""
    cfg = cfg_with(tmp_path, fake_rsync(tmp_path, f"sleep {MARK}\n"))
    t0 = time.time()
    stop_at = t0 + 1.0
    with pytest.raises(control.Stopped):
        run(cfg, tmp_path, stop=lambda: time.time() > stop_at)
    assert time.time() - t0 < 5
    assert strays() == []


def test_rsync_dies_but_a_child_keeps_the_pipe(tmp_path):
    """The main rsync process is gone, a leftover child holds its output."""
    script = fake_rsync(tmp_path, f'printf "\\r    1,000  1%%  1.00MB/s  0:00:09 "\nsleep {MARK} &\nexit 20\n')
    cfg = cfg_with(tmp_path, script)
    t0 = time.time()
    result = run(cfg, tmp_path)
    assert result.returncode == 20
    assert time.time() - t0 < 6
    assert strays() == []              # the whole process group was ended


def test_normal_run_still_reports_progress(tmp_path):
    # rsync rewrites one line with "\\r": a line counts once the next update starts.
    script = fake_rsync(tmp_path, 'printf "\\r    1,024  2%%  1.00MB/s  0:00:09 "\nprintf "\\r    2,048  3%%  1.00MB/s  0:00:09 "\nsleep 0.3\nexit 0\n')
    cfg = cfg_with(tmp_path, script)
    seen = []
    dst = tmp_path / "dst"
    dst.mkdir()
    result = transfer.run_batch(cfg, tmp_path / "src", dst, ["a.txt"], progress=seen.append)
    assert result.returncode == 0 and seen == [1024]


def test_kill_all_ends_running_rsync(tmp_path):
    cfg = cfg_with(tmp_path, fake_rsync(tmp_path, f"sleep {MARK}\n"))
    import threading

    box = {}

    def go():
        try:
            run(cfg, tmp_path)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=go)
    t.start()
    for _ in range(50):
        if strays():
            break
        time.sleep(0.1)
    assert strays()
    transfer.kill_all()
    t.join(10)
    assert not t.is_alive() and strays() == []


def test_sigint_stops_the_server_during_a_silent_transfer(tmp_path):
    """The real thing: `reifs serve` gets Ctrl+C while rsync is hung."""
    root = tmp_path / "root"
    cfg = init_server(tmp_path / "home", "srv", root)
    cfg.discovery.paths = [str(tmp_path / "none")]
    cfg.server.listen = "127.0.0.1:0"
    cfg.rsync.path = str(fake_rsync(tmp_path, f'echo $$ > "{tmp_path}/started"\nsleep {MARK}\n'))
    cfg.rsync.daemon_port = 0
    cfg.save()
    conn = db.open_existing(cfg.db_path)
    from reifs import nodes
    (tmp_path / "ssd").mkdir()
    nodes.register_node(conn, tmp_path / "ssd", "ssd")
    write(root / "a.txt", "hello")
    scanner.scan_all(conn, cfg)
    assert jobs.create_transfer_job(conn, ["/a.txt"], None, "ssd").job_id
    conn.close()

    env = dict(os.environ, REIFS_HOME=str(tmp_path / "home"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "reifs", "serve", "--listen", "127.0.0.1:0"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    try:
        for _ in range(100):
            if (tmp_path / "started").exists():
                break
            time.sleep(0.2)
        assert (tmp_path / "started").exists(), "the transfer never started"
        t0 = time.time()
        proc.send_signal(signal.SIGINT)          # only the server, like `kill -INT`
        proc.wait(timeout=30)
        took = time.time() - t0
        assert took < 12, f"shutdown took {took:.0f} s"
        assert strays() == []
    finally:
        if proc.poll() is None:
            proc.kill()


def test_second_ctrl_c_forces_exit(tmp_path):
    """Even if something refuses to stop, a second Ctrl+C ends the server."""
    root = tmp_path / "root"
    cfg = init_server(tmp_path / "home", "srv", root)
    cfg.discovery.paths = [str(tmp_path / "none")]
    cfg.save()
    env = dict(os.environ, REIFS_HOME=str(tmp_path / "home"))
    code = (
        "import sys, threading, time\n"
        "from reifs import service\n"
        "orig = service.Worker.close\n"
        "def stuck(self, timeout=30):\n"
        "    time.sleep(120)\n"             # shutdown that never finishes
        "service.Worker.close = stuck\n"
        "from reifs.cli import main\n"
        "sys.exit(main(['serve', '--listen', '127.0.0.1:0']))\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    try:
        time.sleep(4)
        proc.send_signal(signal.SIGINT)
        time.sleep(1.5)
        assert proc.poll() is None            # stuck in its shutdown
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)
        assert proc.returncode == 130
    finally:
        if proc.poll() is None:
            proc.kill()
