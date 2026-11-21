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
    with pytest.raises(nodes.NodeError):
        nodes.move_root(env.conn, env.cfg, "srv", str(env.tmp / "x"), move_folder=True)
    # Another node's folder is not this node's.
    _, other = env.add_node("other")
    with pytest.raises(nodes.NodeError):
        nodes.move_root(env.conn, env.cfg, "ssd", str(other))
