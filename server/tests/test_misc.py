import pytest

from reifs import auth, cli, tags
from reifs.config import Config, ConfigError
from reifs.server import init_server

from conftest import write


def test_password_roundtrip():
    h = auth.hash_password("s3cret")
    assert auth.verify_password("s3cret", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("s3cret", "garbage")


def test_config_roundtrip(tmp_path):
    cfg = Config(home=tmp_path)
    cfg.server.name = 'quote " and \\ backslash'
    cfg.storage.root = "C:\\Users\\me\\rei"
    cfg.scan.ignore.append("/scratch/*")
    cfg.save()
    loaded = Config.load(tmp_path)
    assert loaded.server.name == cfg.server.name
    assert loaded.storage.root == cfg.storage.root
    assert loaded.scan.ignore == cfg.scan.ignore


def test_config_rejects_bad_types(tmp_path):
    (tmp_path / "config.toml").write_text("[scan]\ninterval_minutes = true\n")
    with pytest.raises(ConfigError):
        Config.load(tmp_path)
    (tmp_path / "config.toml").write_text("[scan]\nbogus = 1\n")
    with pytest.raises(ConfigError):
        Config.load(tmp_path)


def test_init_twice_fails(tmp_path):
    init_server(tmp_path / "h", "a", tmp_path / "r")
    with pytest.raises(ConfigError):
        init_server(tmp_path / "h", "a", tmp_path / "r")
    with pytest.raises(ConfigError):  # root already belongs to a node
        init_server(tmp_path / "h2", "b", tmp_path / "r")


def test_tags(env):
    write(env.root / "d" / "a.txt", "a")
    write(env.root / "d" / "e" / "b.txt", "b")
    env.scan()
    with pytest.raises(tags.TagError):
        tags.resolve_targets(env.conn, "/d", recursive=False)
    ids = tags.resolve_targets(env.conn, "/d", recursive=True)
    assert tags.set_tag(env.conn, ids, "x", True, env.server_id) == 2
    assert tags.set_tag(env.conn, ids, "x", True, env.server_id) == 0  # idempotent
    assert tags.all_tags(env.conn) == [("x", 2)]
    assert tags.set_tag(env.conn, ids[:1], "x", False, env.server_id) == 1
    assert tags.set_tag(env.conn, ids[:1], "x", False, env.server_id) == 0
    assert tags.all_tags(env.conn) == [("x", 1)]
    with pytest.raises(tags.TagError):
        tags.validate("a,b")


def test_cli_end_to_end(tmp_path, capsys):
    home, root, ssd = tmp_path / "home", tmp_path / "root", tmp_path / "ssd"
    ssd.mkdir()
    write(root / "docs" / "1.txt", "hello")
    base = ["--home", str(home)]
    assert cli.main(base + ["init", "--name", "desk", "--root", str(root)]) == 0
    cfg = Config.load(home)
    cfg.discovery.paths = [str(tmp_path / "none")]
    cfg.save()
    assert cli.main(base + ["node", "add", str(ssd), "--name", "ssd"]) == 0
    assert cli.main(base + ["scan"]) == 0
    assert cli.main(base + ["tag", "work", "/docs", "-r"]) == 0
    assert cli.main(base + ["find", "--tag", "work"]) == 0
    assert "/docs/1.txt" in capsys.readouterr().out
    assert cli.main(base + ["where", "/docs/1.txt"]) == 0
    assert cli.main(base + ["ls", "/docs"]) == 0
    assert cli.main(base + ["status"]) == 0
    assert cli.main(base + ["node", "list"]) == 0
    assert cli.main(base + ["where", "/nope"]) == 1
    assert cli.main(base + ["decide", "99", "x"]) == 1
