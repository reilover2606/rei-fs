"""Changing where a node's root folder is."""

import pytest

from reifs import nodes, queries

from conftest import write


def test_point_a_node_at_its_moved_folder(env):
    _, ssd = env.add_node("ssd")
    write(ssd / "docs" / "a.txt", "alpha", mtime=1_000_000)
    env.scan()
    new = env.tmp / "elsewhere" / "ssd2"
    new.parent.mkdir()
    ssd.rename(new)                                   # the user moved the folder by hand
    with pytest.raises(nodes.NodeError):              # nothing there (yet)
        nodes.move_root(env.conn, env.cfg, "ssd", str(env.tmp / "nowhere"))
    nodes.move_root(env.conn, env.cfg, "ssd", str(new))
    row = nodes.get_node(env.conn, "ssd")
    assert row["root_path"] == str(new) and row["online"] == 1
    res = env.scan()["ssd"].stats
    assert (res.added, res.missing, res.changed) == (0, 0, 0)       # same files, same tree
    assert queries.lookup(env.conn, "/docs/a.txt").copies


def test_move_the_folder_for_the_user(env):
    _, ssd = env.add_node("ssd")
    write(ssd / "docs" / "a.txt", "alpha", mtime=1_000_000)
    env.scan()
    target = env.tmp / "moved-here"
    nodes.move_root(env.conn, env.cfg, "ssd", str(target), move_folder=True)
    assert not ssd.exists() and (target / "docs" / "a.txt").read_text() == "alpha"
    assert nodes.get_node(env.conn, "ssd")["root_path"] == str(target)
    assert env.scan()["ssd"].stats.missing == 0
    # Refusals: an existing target, inside the node, the server's own node, a relative path.
    (env.tmp / "taken").mkdir()
    for bad in (str(env.tmp / "taken"), str(target / "inside"), "relative/path"):
        with pytest.raises(nodes.NodeError):
            nodes.move_root(env.conn, env.cfg, "ssd", bad, move_folder=True)
    # Another node's folder is not this node's.
    _, other = env.add_node("other")
    with pytest.raises(nodes.NodeError):
        nodes.move_root(env.conn, env.cfg, "ssd", str(other))


def test_the_servers_own_node_can_move_too(env):
    write(env.root / "docs" / "a.txt", "alpha", mtime=1_000_000)
    env.scan()
    old = env.root
    target = env.tmp / "new-server-root"
    nodes.move_root(env.conn, env.cfg, "srv", str(target), move_folder=True)
    assert not old.exists() and (target / "docs" / "a.txt").read_text() == "alpha"
    assert env.cfg.storage.root == str(target)
    assert nodes.get_node(env.conn, "srv")["online"] == 1
    assert env.scan()["srv"].stats.missing == 0


def test_editing_the_config_after_moving_the_folder_by_hand_finds_it_again(env):
    write(env.root / "docs" / "a.txt", "alpha", mtime=1_000_000)
    env.scan()
    target = env.tmp / "moved-by-hand"
    env.root.rename(target)
    nodes.discover(env.conn, env.cfg)
    assert nodes.get_node(env.conn, "srv")["online"] == 0       # nobody knows where it went
    env.cfg.storage.root = str(target)                          # the user edits the config
    d = nodes.discover(env.conn, env.cfg)
    assert [n for n, _ in d.moved] == ["srv"] and d.offline == []
    assert nodes.get_node(env.conn, "srv")["root_path"] == str(target)
