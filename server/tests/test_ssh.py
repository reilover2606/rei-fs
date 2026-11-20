"""rsync over SSH between servers, against a real sshd started for the test."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reifs import jobs, peers, queries, ssh

from conftest import write
from test_peers import cluster, free_port, pair  # noqa: F401  (fixture)

SSHD = shutil.which("sshd") or ("/usr/sbin/sshd" if os.path.exists("/usr/sbin/sshd") else None)
needs_sshd = pytest.mark.skipif(
    sys.platform == "win32" or SSHD is None or shutil.which("ssh") is None
    or shutil.which("rsync") is None or shutil.which("ssh-keygen") is None,
    reason="needs sshd, ssh, ssh-keygen and rsync (POSIX)")


@pytest.fixture
def sshd(tmp_path):
    if SSHD is None:
        pytest.skip("no sshd")
    d = tmp_path / "sshd"
    d.mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(d / "hostkey")],
                   check=True)
    port = free_port()
    authorized = d / "authorized_keys"
    authorized.write_text("")
    (d / "sshd_config").write_text(f"""
Port {port}
ListenAddress 127.0.0.1
HostKey {d / 'hostkey'}
AuthorizedKeysFile {authorized}
PidFile {d / 'sshd.pid'}
StrictModes no
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin prohibit-password
LogLevel ERROR
""")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        os.makedirs("/run/sshd", exist_ok=True)  # privilege separation dir (root only)
    proc = subprocess.Popen([SSHD, "-D", "-e", "-f", str(d / "sshd_config")],
                            stderr=subprocess.PIPE)
    time.sleep(0.5)
    if proc.poll() is not None:
        pytest.skip(f"sshd did not start: {proc.stderr.read().decode()[:200]}")
    yield port, authorized
    proc.terminate()
    proc.wait(5)


@needs_sshd
def test_pull_over_ssh_with_read_only_key(cluster, sshd):  # noqa: F811
    port, authorized = sshd
    a, b = cluster("alpha"), cluster("beta")
    a.cfg.ssh.enabled = True
    a.cfg.ssh.host = "127.0.0.1"
    a.cfg.ssh.port = port
    write(a.root / "docs" / "secret plan.txt", "over ssh", mtime=1_000_000)
    a.scan()
    pair(a, b)
    from reifs import nodes
    nodes.discover(a.conn, a.cfg)       # announces the path served over SSH
    b.sync(a)                           # learns alpha's SSH offer and key
    a.sync(b)
    row = peers.peer_row(b.conn, a.id)
    assert row["ssh_enabled"] and row["ssh_port"] == port
    assert b.conn.execute("SELECT ssh_root FROM nodes WHERE name = 'alpha'").fetchone()[0]

    # Alpha's administrator authorizes beta's key (read-only through rrsync).
    lines, rrsync, missing = ssh.authorize_lines(a.conn)
    assert len(lines) == 1 and not missing
    if rrsync:
        assert 'command="' in lines[0] and "-ro" in lines[0]
    ssh.install(lines, authorized)
    assert ssh.install(lines, authorized)[1] == 0     # no duplicates

    # Beta pulls over SSH; alpha's rsync daemon is off to prove it is not used.
    a.syncer.daemon.stop()
    a.syncer.daemon.wanted = False
    b.conn.execute("UPDATE servers SET transport = 'ssh' WHERE id = ?", (a.id,))
    rep = jobs.create_transfer_job(b.conn, ["/docs"], None, "beta")
    r = b.run_jobs()[rep.job_id]
    assert (r.status, r.done) == ("done", 1), r.messages
    assert (b.root / "docs" / "secret plan.txt").read_text() == "over ssh"
    assert ssh.known_hosts(b.cfg.home).exists()       # host key pinned on first use
    a.sync(b)
    assert queries.lookup(a.conn, "/docs/secret plan.txt").good_copies == 2

    if rrsync:
        # The key cannot write through rsync, nor run a shell.
        key = ssh.key_path(b.cfg.home)
        base = ["ssh", "-p", str(port), "-i", str(key), "-o", "BatchMode=yes",
                "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=no",
                "-o", f"UserKnownHostsFile={ssh.known_hosts(b.cfg.home)}"]
        shell = subprocess.run(base + ["127.0.0.1", "touch", str(a.root / "pwned")],
                               capture_output=True, text=True)
        assert not (a.root / "pwned").exists()
        write(b.tmp / "evil.txt", "x")
        push = subprocess.run(["rsync", "-e", " ".join(base), str(b.tmp / "evil.txt"),
                               f"127.0.0.1:{a.root}/"], capture_output=True, text=True)
        assert push.returncode != 0 and not (a.root / "evil.txt").exists(), push.stderr
        assert shell.returncode != 0 or not (a.root / "pwned").exists()


def test_transport_requires_offer(cluster):  # noqa: F811
    a, b = cluster("alpha"), cluster("beta")
    pair(a, b)
    b.sync(a)
    from reifs import cli
    assert cli.main(["--home", str(b.cfg.home), "peer", "transport", "alpha", "ssh"]) == 1
    assert cli.main(["--home", str(b.cfg.home), "peer", "transport", "alpha", "daemon"]) == 0


def test_key_and_authorized_lines(tmp_path):
    line = ssh.ensure_key(tmp_path, "srv")
    assert line.startswith("ssh-ed25519 ") and line == ssh.ensure_key(tmp_path, "srv")
    if sys.platform != "win32":
        assert oct(os.stat(ssh.key_path(tmp_path)).st_mode & 0o777) == "0o600"
    restricted = ssh.authorized_line(line, "beta", "/usr/bin/rrsync")
    assert restricted.startswith('restrict,command="/usr/bin/rrsync -ro /" ssh-ed25519 ')
    assert ssh.authorized_line(line, "beta", None).startswith("restrict ssh-ed25519 ")
    path = tmp_path / "authorized_keys"
    path.write_text("ssh-rsa AAAAexisting user@host")  # no trailing newline
    ssh.install([restricted], path)
    assert path.read_text().splitlines()[1] == restricted
