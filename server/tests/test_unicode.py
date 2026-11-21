"""Full Unicode in file names, folder names, node names, tags and searches:
scanning, the database, the API, rsync transfers (local, daemon, SSH) and the
command line, including on a machine whose locale is not UTF-8."""

import json
import os
import shutil
import ssl
import subprocess
import sys
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from reifs import jobs, queries, runner, tags

from conftest import write
from test_api import server  # noqa: F401  (fixture)
from test_peers import cluster, needs_rsync, pair  # noqa: F401  (fixture)

# Names a real collection contains. Nothing here is illegal on Windows.
NAMES = [
    "日本語/ファイル.txt",
    "中文/文件名.pdf",
    "한국어/파일 이름.docx",
    "ไทย/ภาษาไทย.txt",
    "العربية/ملف.txt",
    "עברית/קובץ.txt",
    "emoji/😀 party 🎉.png",
    "rare/𠮷野家 𩸽.txt",                       # CJK beyond the basic plane (4 bytes in UTF-8)
    "accents/café.txt",                    # é, precomposed (NFC)
    "accents/café.txt",                   # é, e + combining accent (NFD): a different name
    "wide/ＡＢＣ ｱｲｳ.txt",                      # full-width Latin and half-width katakana
    "名前 with spaces/a%b&c=d+e.txt",           # spaces and URL-special characters
    "混合/Mixed 混合 123/終わり.bin",
]
LONG_CJK = "あ" * 80 + ".txt"                   # 80 characters, 243 bytes: just under the limit


def make_tree(root: Path) -> None:
    for name in NAMES + [LONG_CJK]:
        write(root / name, f"content of {name}", mtime=1_000_000)


@pytest.fixture
def tree(env):
    make_tree(env.root)
    env.scan()
    return env


def vpaths(env):
    return {f.vpath for f in queries.find(env.conn)}


def test_every_name_is_indexed_exactly(tree):
    expected = {"/" + n for n in NAMES + [LONG_CJK]}
    assert vpaths(tree) == expected
    assert len({unicodedata.normalize("NFC", p) for p in expected}) == len(expected) - 1
    # The two spellings of "café" are two files: names are kept exactly as they are.
    assert {"/accents/café.txt", "/accents/café.txt"} <= vpaths(tree)
    assert tree.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    # Nothing was rejected: the long CJK name is 243 bytes, within the 255 limit.
    info = queries.lookup(tree.conn, "/" + LONG_CJK)
    assert info is not None and "length" not in info.issues


def test_search_in_cjk_and_other_scripts(tree):
    def find(text, **kw):
        return {f.vpath for f in queries.find(tree.conn, text, **kw)}

    assert find("ファイル") == {"/日本語/ファイル.txt"}
    assert find("文件") == {"/中文/文件名.pdf"}
    assert find("파일") == {"/한국어/파일 이름.docx"}
    assert find("ภาษา") == {"/ไทย/ภาษาไทย.txt"}
    assert find("ملف") == {"/العربية/ملف.txt"}
    assert find("🎉") == {"/emoji/😀 party 🎉.png"}
    assert find("野家") == {"/rare/𠮷野家 𩸽.txt"}
    assert find("混合") == set()                                  # only its folders have that name
    assert find("終わり") == {"/混合/Mixed 混合 123/終わり.bin"}
    assert find("a%b&c") == {"/名前 with spaces/a%b&c=d+e.txt"}
    assert queries.search(tree.conn, "名前 with")[1] == [("/名前 with spaces", 1)]   # the folder
    assert find("*.pdf") == {"/中文/文件名.pdf"}
    assert find("文件*") == {"/中文/文件名.pdf"}
    assert find("日本語/") == {"/日本語/ファイル.txt"}          # a path pattern
    assert find("ＡＢＣ") == {"/wide/ＡＢＣ ｱｲｳ.txt"}
    # Case-insensitive for scripts that have case; harmless for those that do not.
    assert find("ＡＢＣ".lower()) == {"/wide/ＡＢＣ ｱｲｳ.txt"}       # full-width case folds too
    assert queries.search(tree.conn, "MIXED")[1] == [("/混合/Mixed 混合 123", 1)]
    # Folders are found by their names too.
    _, dirs = queries.search(tree.conn, "日本")
    assert dirs == [("/日本語", 1)]
    _, dirs = queries.search(tree.conn, "混合")
    assert dirs == [("/混合", 1), ("/混合/Mixed 混合 123", 1)]


def test_tags_in_any_script(tree):
    ids = tags.resolve_targets(tree.conn, "/日本語", True)
    assert tags.set_tag(tree.conn, ids, "休暇 ✈️ 2025", True, tree.server_id) == 1
    assert tags.all_tags(tree.conn) == [("休暇 ✈️ 2025", 1)]
    assert [f.vpath for f in queries.find(tree.conn, tag="休暇 ✈️ 2025")] == ["/日本語/ファイル.txt"]
    assert queries.lookup(tree.conn, "/日本語/ファイル.txt").tags == ["休暇 ✈️ 2025"]


def test_rescan_is_quiet_and_renames_are_followed(tree):
    r = tree.scan()["srv"]
    assert (r.stats.added, r.stats.hashed, r.stats.missing, r.stats.renamed) == (0, 0, 0, 0)
    (tree.root / "日本語" / "ファイル.txt").rename(tree.root / "日本語" / "名前を変更.txt")
    r = tree.scan()["srv"]
    assert r.stats.renamed == 1 and r.stats.missing == 0
    assert "/日本語/名前を変更.txt" in vpaths(tree)


@needs_rsync
def test_local_rsync_copy_keeps_names_byte_for_byte(tree):
    _, ssd = tree.add_node("外付けSSD")                          # a node with a CJK name
    rep = jobs.create_transfer_job(tree.conn, ["/"], None, "外付けSSD")
    r = {x.job_id: x for x in runner.run_jobs(tree.conn, tree.cfg)}[rep.job_id]
    assert (r.status, r.done, r.failed) == ("done", len(NAMES) + 1, 0), r.messages

    def listing(root):
        out = set()
        for dirpath, _dirs, files in os.walk(root):
            if ".rei-fs" in dirpath:
                continue
            for f in files:
                out.add(os.fsencode(os.path.relpath(os.path.join(dirpath, f), root)))
        return out

    assert listing(ssd) == listing(tree.root)                   # the same bytes, not just similar
    assert (ssd / "日本語" / "ファイル.txt").read_text(encoding="utf-8") == "content of 日本語/ファイル.txt"
    # Both spellings of "café" arrived as two separate files.
    assert (ssd / "accents" / "café.txt").exists() and (ssd / "accents" / "café.txt").exists()
    # And the index agrees with the disk: a scan of the new node finds nothing odd.
    s = tree.scan()["外付けSSD"].stats
    assert (s.added, s.changed, s.missing, s.renamed) == (0, 0, 0, 0)
    assert queries.status(tree.conn).satisfied == len(NAMES) + 1


@needs_rsync
def test_pull_through_the_rsync_daemon_with_cjk_names_and_paths(cluster):  # noqa: F811
    # The servers' own names (and so their roots and homes) are CJK, with a space.
    a, b = cluster("アルファ サーバー"), cluster("ベータ")
    make_tree(a.root)
    a.scan()
    pair(a, b)
    b.sync(a)
    a.sync(b)
    rep = jobs.create_transfer_job(b.conn, ["/"], None, "ベータ")
    r = b.run_jobs()[rep.job_id]
    assert (r.status, r.done, r.failed) == ("done", len(NAMES) + 1, 0), r.messages
    assert (b.root / "한국어" / "파일 이름.docx").read_text(encoding="utf-8") == "content of 한국어/파일 이름.docx"
    assert (b.root / "rare" / "𠮷野家 𩸽.txt").exists()
    assert (b.root / "accents" / "café.txt").exists()
    a.sync(b)
    assert queries.lookup(a.conn, "/emoji/😀 party 🎉.png").good_copies == 2
    # The metadata made the trip too (names in the change log, node names).
    assert {n["name"] for n in b.conn.execute("SELECT name FROM nodes")} == {"アルファ サーバー", "ベータ"}
    assert queries.lookup(b.conn, "/日本語/ファイル.txt").good_copies == 2


# --- the API --------------------------------------------------------------------------


def test_api_round_trip_in_cjk(server):
    env, client, _ = server
    make_tree(env.root)
    env.scan()
    client.token = client.post("/api/login", {"password": "pw"})["token"]
    q = lambda s: urllib.parse.quote(s, safe="/")  # noqa: E731  (what the client sends)

    tree_ = client.get("/api/tree?path=" + q("/日本語"))
    assert [e["name"] for e in tree_["entries"]] == ["ファイル.txt"]
    f = client.get("/api/file?path=" + q("/한국어/파일 이름.docx"))
    assert f["path"] == "/한국어/파일 이름.docx" and f["name"] == "파일 이름.docx"
    found = client.get("/api/find?q=" + q("文件"))
    assert [x["path"] for x in found["files"]] == ["/中文/文件名.pdf"]
    found = client.get("/api/find?q=" + q("混合"))
    assert found["dirs"][0] == {"path": "/混合", "name": "混合", "files": 1}
    assert client.post("/api/tags", {"action": "add", "tag": "休暇", "paths": ["/日本語"],
                                     "recursive": True})["changed"] == 1
    assert client.get("/api/find?tag=" + q("休暇"))["total"] == 1
    # A node with a CJK name and folder.
    drive = env.tmp / "外付け ドライブ"
    drive.mkdir()
    out = client.post("/api/nodes", {"path": str(drive), "name": "外付け"})
    assert out["name"] == "外付け"
    assert "外付け" in {n["name"] for n in client.get("/api/nodes")["nodes"]}
    client.post("/api/nodes/" + q("外付け"), {"name": "バックアップ"})
    assert "バックアップ" in {n["name"] for n in client.get("/api/nodes")["nodes"]}
    assert json.loads((drive / ".rei-fs" / "node.json").read_text(encoding="utf-8"))["name"] == "バックアップ"


# --- the command line, on any locale ------------------------------------------------


def run_cli(env_vars, *args, python_flags=()):
    env = dict(os.environ, **env_vars)
    return subprocess.run([sys.executable, *python_flags, "-m", "reifs", *args], env=env,
                          capture_output=True, text=False, timeout=60)


def test_cli_survives_an_ascii_terminal(tmp_path):
    home, root = tmp_path / "home", tmp_path / "root"
    from reifs.server import init_server
    cfg = init_server(home, "srv", root)
    cfg.discovery.paths = []
    cfg.save()
    make_tree(root)
    env = {"REIFS_HOME": str(home), "PYTHONIOENCODING": "ascii"}   # output that cannot show CJK
    assert run_cli(env, "scan").returncode == 0
    out = run_cli(env, "ls", "/日本語")
    assert out.returncode == 0, out.stderr.decode(errors="replace")   # no UnicodeEncodeError
    found = run_cli(env, "find", "文件")
    assert found.returncode == 0 and b"1 file(s)" in found.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="Python always uses Unicode names on Windows")
def test_server_works_when_the_locale_is_not_utf8(tmp_path):
    """LANG=C with Python's UTF-8 mode off: the file system encoding would be
    ASCII and every CJK name would be read as garbage. The CLI restarts itself
    in UTF-8 mode."""
    home, root = tmp_path / "home", tmp_path / "root"
    from reifs.server import init_server
    cfg = init_server(home, "srv", root)
    cfg.discovery.paths = []
    cfg.save()
    make_tree(root)
    env = {"REIFS_HOME": str(home), "LC_ALL": "C", "LANG": "C", "PYTHONCOERCECLOCALE": "0",
           "PYTHONUTF8": "0", "PYTHONIOENCODING": "utf-8"}
    probe = subprocess.run([sys.executable, "-X", "utf8=0", "-c",
                            "import sys; print(sys.getfilesystemencoding())"],
                           env=dict(os.environ, **env), capture_output=True, text=True)
    assert probe.stdout.strip().lower() in ("ascii", "ansi_x3.4-1968", "us-ascii"), probe.stdout
    scan = run_cli(env, "scan", python_flags=("-X", "utf8=0"))
    assert scan.returncode == 0, scan.stderr.decode(errors="replace")
    ls = run_cli(env, "ls", "/日本語", python_flags=("-X", "utf8=0"))
    assert "ファイル.txt".encode("utf-8") in ls.stdout, (ls.stdout, ls.stderr)
    status = run_cli(env, "status", python_flags=("-X", "utf8=0"))
    assert f"files:            {len(NAMES) + 1}".encode() in status.stdout


def test_ssh_authorized_keys_with_non_ascii_comments(tmp_path):
    from reifs import ssh
    path = tmp_path / "authorized_keys"
    path.write_bytes("ssh-ed25519 AAAAexisting 山田@ノートPC café\n".encode("utf-8") + b"# \xff\xfe not utf-8\n")
    line = ssh.authorized_line("ssh-ed25519 AAAAnewkey comment", "ベータ", None)
    p, added = ssh.install([line], path)
    assert added == 1
    data = path.read_bytes()
    assert "山田@ノートPC café".encode("utf-8") in data and b"# \xff\xfe not utf-8" in data   # untouched
    assert ssh.install([line], path)[1] == 0                                                  # idempotent


from test_ssh import needs_sshd, sshd  # noqa: E402,F401  (fixture)


@needs_sshd
@pytest.mark.parametrize("old_rsync", [False, True], ids=["rsync-escapes", "old-rsync"])
@pytest.mark.parametrize("server_name", ["外付け サーバー", "o'neil [x] $y *? 日本 (copy)"])
def test_pull_over_ssh_from_an_awkward_folder(cluster, sshd, server_name, old_rsync,  # noqa: F811
                                              monkeypatch):
    """The remote shell (and rrsync) see the source path: spaces, quotes, ``$``,
    wildcard characters and non-ASCII characters in it must not be split,
    expanded or mangled. ``old_rsync`` makes the client send paths unprotected,
    like rsync before 3.2.4 (we then escape them ourselves)."""
    if old_rsync:
        monkeypatch.setenv("RSYNC_OLD_ARGS", "1")
    from reifs import nodes, peers, ssh

    port, authorized = sshd
    a, b = cluster(server_name), cluster("ベータ")
    a.cfg.ssh.enabled = True
    a.cfg.ssh.host = "127.0.0.1"
    a.cfg.ssh.port = port
    make_tree(a.root)                      # a.root is ".../<server name>-root"
    a.scan()
    pair(a, b)
    nodes.discover(a.conn, a.cfg)
    b.sync(a)
    a.sync(b)
    ssh.install(ssh.authorize_lines(a.conn)[0], authorized)
    a.syncer.daemon.stop()                 # prove the daemon is not what carries the files
    a.syncer.daemon.wanted = False
    b.conn.execute("UPDATE servers SET transport = 'ssh' WHERE id = ?", (a.id,))
    rep = jobs.create_transfer_job(b.conn, ["/"], None, "ベータ")
    r = b.run_jobs()[rep.job_id]
    assert (r.status, r.done, r.failed) == ("done", len(NAMES) + 1, 0), r.messages
    assert (b.root / "日本語" / "ファイル.txt").read_text(encoding="utf-8") == "content of 日本語/ファイル.txt"
    assert (b.root / "名前 with spaces" / "a%b&c=d+e.txt").exists()
    assert (b.root / "rare" / "𠮷野家 𩸽.txt").exists()
