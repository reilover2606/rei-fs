import base64

import pytest

from reifs import apps, db, priority, queries, runner, sync, synchronous

from conftest import write


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def test_a_storage_app_gets_a_directory_and_a_token(env):
    env.add_node("ssd")
    info, token = apps.register(env.conn, env.cfg, "photos", "storage", node="ssd")
    assert info["dir"] == "/apps/photos" and info["node"] == "ssd"
    assert (env.tmp / "ssd" / "apps" / "photos").is_dir()
    assert token.startswith("reifsapp_")
    assert apps.authenticate(env.conn, f"Bearer {token}")["name"] == "photos"
    assert apps.authenticate(env.conn, "Bearer reifsapp_wrong") is None
    assert apps.authenticate(env.conn, "Bearer something-else") is None
    # the token is not kept in clear
    assert token not in str(env.conn.execute("SELECT * FROM apps").fetchone()[:])
    assert priority.Prefs(priority.load(env.conn)).effective("/apps/photos") == priority.FREQUENT


def test_a_node_is_chosen_when_none_is_named(env):
    env.add_node("ssd")
    info, _ = apps.register(env.conn, env.cfg, "notes", "storage")
    assert info["node"] in ("srv", "ssd")


def test_names_and_modes_are_checked(env):
    env.add_node("ssd")
    with pytest.raises(apps.AppError, match="name"):
        apps.register(env.conn, env.cfg, "../evil", "storage", "ssd")
    with pytest.raises(apps.AppError, match="mode"):
        apps.register(env.conn, env.cfg, "x", "cluster", "ssd")
    apps.register(env.conn, env.cfg, "x", "storage", "ssd")
    with pytest.raises(apps.AppError, match="taken"):
        apps.register(env.conn, env.cfg, "x", "storage", "srv")
    with pytest.raises(apps.AppError, match="registered as"):
        apps.register(env.conn, env.cfg, "x", "instance", "srv")


def access(env, name):
    return apps.Access(env.conn, env.cfg, apps.get(env.conn, name))


def test_the_app_reads_and_writes_inside_its_directory_only(env):
    env.add_node("ssd")
    apps.register(env.conn, env.cfg, "docs", "storage", "ssd")
    acc = access(env, "docs")
    out = acc.write("notes/a.txt", b64("hello"))
    assert out["path"] == "/apps/docs/notes/a.txt" and out["indexed"]
    assert (env.tmp / "ssd" / "apps" / "docs" / "notes" / "a.txt").read_text() == "hello"
    # recorded at once: it is in the virtual tree (and so replicated)
    assert queries.lookup(env.conn, "/apps/docs/notes/a.txt").size == 5
    assert base64.b64decode(acc.read("notes/a.txt")["data"]) == b"hello"
    assert [e["name"] for e in acc.listing("notes")] == ["a.txt"]
    assert [(e["name"], e["dir"]) for e in acc.listing("/")] == [("notes", True)]
    for bad in ("../x", "notes/../../x"):
        with pytest.raises(apps.AppError):
            acc.write(bad, b64("x"))
    with pytest.raises(apps.AppError, match="base64"):
        acc.write("b.txt", "not base64!!")


def test_a_file_can_be_written_in_chunks(env):
    env.add_node("ssd")
    apps.register(env.conn, env.cfg, "big", "storage", "ssd")
    acc = access(env, "big")
    acc.write("f.bin", b64("one "), append=True, done=False)
    assert queries.lookup(env.conn, "/apps/big/f.bin") is None      # not indexed until done
    acc.write("f.bin", b64("two"), append=True, done=True)
    assert queries.lookup(env.conn, "/apps/big/f.bin").size == 7


def test_the_app_can_delete_its_own_file(env):
    env.add_node("ssd")
    apps.register(env.conn, env.cfg, "tmp", "storage", "ssd")
    acc = access(env, "tmp")
    acc.write("x.txt", b64("x"))
    acc.delete("x.txt")
    assert not (env.tmp / "ssd" / "apps" / "tmp" / "x.txt").exists()
    assert queries.lookup(env.conn, "/apps/tmp/x.txt") is None


def test_symlinks_cannot_lead_out(env, tmp_path):
    env.add_node("ssd")
    apps.register(env.conn, env.cfg, "jail", "storage", "ssd")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    try:
        (env.tmp / "ssd" / "apps" / "jail" / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("no symlinks here")
    with pytest.raises(apps.AppError, match="leads out"):
        access(env, "jail").read("link/secret.txt")


def test_instances_of_one_app_are_kept_the_same_on_every_node(env):
    env.add_node("ssd")
    apps.register(env.conn, env.cfg, "shared", "instance", "srv", path="/work/shared")
    acc1 = access(env, "shared") if False else apps.Access(env.conn, env.cfg,
                                                           apps.get(env.conn, "shared", "srv"))
    acc1.write("settings.json", b64("{\"v\": 1}"))
    # the second instance is registered later, on an empty node
    apps.register(env.conn, env.cfg, "shared", "instance", "ssd")      # same path: it is the same app
    assert priority.Prefs(priority.load(env.conn)).effective("/work/shared") == priority.SYNCHRONOUS
    assert synchronous.run(env.conn, env.cfg, full=True) == 1
    runner.run_jobs(env.conn, env.cfg)
    assert (env.tmp / "ssd" / "work" / "shared" / "settings.json").read_text() == "{\"v\": 1}"
    # a change on the other instance travels back
    acc2 = apps.Access(env.conn, env.cfg, apps.get(env.conn, "shared", "ssd"))
    import os
    acc2.write("settings.json", b64("{\"v\": 2}"))
    os.utime(env.tmp / "ssd" / "work" / "shared" / "settings.json", (4_000_000_000, 4_000_000_000))
    env.scan()
    synchronous.run(env.conn, env.cfg, full=True)
    runner.run_jobs(env.conn, env.cfg)
    assert (env.root / "work" / "shared" / "settings.json").read_text() == "{\"v\": 2}"
    with pytest.raises(apps.AppError, match="several nodes"):
        apps.get(env.conn, "shared")


def test_registrations_are_announced_to_other_servers_without_the_token(env, tmp_path):
    env.add_node("ssd")
    apps.register(env.conn, env.cfg, "shared", "instance", "ssd", path="/work/shared")
    sent = [c for c in sync.export_changes(env.conn, 0)["changes"] if c["kind"] == "app"]
    assert sent and "token" not in str(sent) and sent[0]["data"]["mode"] == "instance"
    other = db.create(tmp_path / "o.db")
    other.execute("INSERT INTO servers(id, name, is_self) VALUES ('me', 'me', 1)")
    other.execute("INSERT INTO meta(key, value) VALUES ('server_id', 'me')")
    other.execute("INSERT INTO servers(id, name) VALUES (?, 'origin')", (env.server_id,))
    entries = [c for c in sync.export_changes(env.conn, 0)["changes"] if c["kind"] in ("node", "app")]
    sync.apply_changes(other, [dict(c, ts=db.to_iso(db.now())) for c in entries])
    [row] = other.execute("SELECT * FROM apps").fetchall()
    assert row["name"] == "shared" and row["token_hash"] is None and row["present"] == 1
    apps.remove(env.conn, "shared", "ssd")
    removal = [c for c in sync.export_changes(env.conn, 0)["changes"]
               if c["kind"] == "app" and not c["data"]["present"]]
    sync.apply_changes(other, [dict(c, ts=db.to_iso(db.now())) for c in removal])
    assert other.execute("SELECT present FROM apps").fetchone()[0] == 0


def test_an_instance_uses_its_own_directory_anywhere_in_the_node(env):
    _, ssd = env.add_node("ssd")
    write(env.root / "home" / "me" / ".config" / "myapp" / "prefs.ini", "a=1", mtime=1_000_000)
    with pytest.raises(apps.AppError, match="give its path"):
        apps.register(env.conn, env.cfg, "myapp", "instance", "srv")
    info, _ = apps.register(env.conn, env.cfg, "myapp", "instance", "srv", path="/home/me/.config/myapp")
    assert info["dir"] == "/home/me/.config/myapp"
    assert (env.root / "home" / "me" / ".config" / "myapp" / "prefs.ini").exists()      # its own files
    assert not (env.root / "apps").exists()                                           # not under apps/
    info2, _ = apps.register(env.conn, env.cfg, "myapp", "instance", "ssd")           # same path is taken
    assert info2["dir"] == "/home/me/.config/myapp"
    assert (ssd / "home" / "me" / ".config" / "myapp").is_dir()
    assert priority.Prefs(priority.load(env.conn)).effective("/home/me/.config/myapp") == priority.SYNCHRONOUS


def test_the_instances_of_an_app_must_share_one_path_and_apps_may_not_overlap(env):
    env.add_node("ssd")
    env.add_node("usb")
    apps.register(env.conn, env.cfg, "a", "instance", "srv", path="/data/a")
    with pytest.raises(apps.AppError, match="same path"):
        apps.register(env.conn, env.cfg, "a", "instance", "ssd", path="/elsewhere/a")
    with pytest.raises(apps.AppError, match="overlaps"):
        apps.register(env.conn, env.cfg, "b", "instance", "usb", path="/data/a/sub")
    with pytest.raises(apps.AppError, match="overlaps"):
        apps.register(env.conn, env.cfg, "c", "instance", "usb", path="/data")
    with pytest.raises(apps.AppError, match="whole node"):
        apps.register(env.conn, env.cfg, "d", "instance", "usb", path="/")


def test_an_applications_directory_is_marked_in_the_tree_even_while_empty(env):
    env.add_node("ssd")
    apps.register(env.conn, env.cfg, "notes", "storage", "ssd")
    apps.register(env.conn, env.cfg, "sync", "instance", "srv", path="/deep/in/the/node/sync")
    top = {e.name: e for e in queries.list_dir(env.conn, "/")}
    assert top["apps"].app is None                                     # the folder of storage apps: no marker
    assert [(e.name, e.app) for e in queries.list_dir(env.conn, "/apps")] == [("notes", ("notes", "storage"))]
    assert [(e.name, e.app) for e in queries.list_dir(env.conn, "/deep/in/the/node")] == [("sync", ("sync", "instance"))]
