import os
import shutil
import sys

import pytest

from reifs import decisions, nodes, queries, scanner, tags
from reifs.nodes import NodeError

from conftest import write


def copies(env, vpath):
    info = queries.lookup(env.conn, vpath)
    return {c.node_name: c for c in info.copies} if info else {}


def test_add_and_unchanged(env):
    write(env.root / "docs" / "a.txt", "aaa")
    write(env.root / "b.bin", "bb")
    r = env.scan()["srv"]
    assert (r.stats.added, r.stats.hashed) == (2, 2)
    r = env.scan()["srv"]
    assert (r.stats.added, r.stats.hashed, r.stats.changed) == (0, 0, 0)
    assert set(copies(env, "/docs/a.txt")) == {"srv"}


def test_ignore_rules(env):
    write(env.root / ".git" / "config", "x")
    write(env.root / ".hiddenfile", "x")        # hidden files are not ignored
    write(env.root / "Thumbs.db", "x")
    write(env.root / "sub" / "THUMBS.DB", "x")  # case-insensitive name match
    write(env.root / "scratch" / "tmp1", "x")
    env.cfg.scan.ignore.append("/scratch/*")
    write(env.root / "keep.txt", "x")
    env.scan()
    vpaths = {f.vpath for f in queries.find(env.conn)}
    assert vpaths == {"/.hiddenfile", "/keep.txt"}


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")
def test_symlinks_ignored(env):
    target = write(env.tmp / "outside.txt", "x")
    os.symlink(target, env.root / "link.txt")
    os.symlink(env.tmp, env.root / "linkdir")
    env.scan()
    assert queries.find(env.conn) == []


def test_change_detected(env):
    f = write(env.root / "a.txt", "one", mtime=1_000_000)
    env.scan()
    write(f, "two!", mtime=2_000_000)
    r = env.scan()["srv"]
    assert r.stats.changed == 1
    assert copies(env, "/a.txt")["srv"].size == 4


def test_vanished_creates_missing_decision_and_prunes(env):
    node_id, ssd = env.add_node("ssd")
    write(env.root / "deep" / "er" / "a.txt", "same", mtime=1_000_000)
    write(ssd / "deep" / "er" / "a.txt", "same", mtime=1_000_000)
    (env.root / "user-empty").mkdir()                    # never tracked: kept
    (env.root / "deep" / "keepme").mkdir()               # sibling: stops pruning
    env.scan()
    (env.root / "deep" / "er" / "a.txt").unlink()
    r = env.scan()["srv"]
    assert r.stats.missing == 1
    assert not (env.root / "deep" / "er").exists()
    assert (env.root / "deep" / "keepme").exists()
    assert (env.root / "user-empty").exists()
    assert copies(env, "/deep/er/a.txt")["srv"].state == "missing"
    [d] = env.decisions("missing")
    opts = decisions.get_decision(env.conn, d["id"]).options
    assert set(opts) == {"restore", "forget", "delete"}

    # The copy reappears: decision becomes obsolete.
    write(env.root / "deep" / "er" / "a.txt", "same", mtime=1_000_000)
    r = env.scan()["srv"]
    assert r.stats.reappeared == 1
    assert env.decisions("missing") == []


def test_missing_resolutions(env):
    _, ssd = env.add_node("ssd")
    for name in ("r.txt", "f.txt", "d.txt"):
        write(env.root / name, name, mtime=1_000_000)
        write(ssd / name, name, mtime=1_000_000)
    env.scan()
    for name in ("r.txt", "f.txt", "d.txt"):
        (env.root / name).unlink()
    env.scan()
    by_path = {decisions.get_decision(env.conn, d["id"]).vpath: d["id"]
               for d in env.decisions("missing")}

    decisions.resolve(env.conn, by_path["/r.txt"], "restore")
    [w] = env.conn.execute("SELECT * FROM wanted").fetchall()
    assert w["op"] == "copy" and w["dst_node"] == env.server_id

    decisions.resolve(env.conn, by_path["/f.txt"], "forget")
    assert set(copies(env, "/f.txt")) == {"ssd"}

    decisions.resolve(env.conn, by_path["/d.txt"], "delete")
    assert copies(env, "/d.txt")["ssd"].marked_delete

    # The user deletes the marked copy: forgotten silently, file leaves the index.
    (ssd / "d.txt").unlink()
    env.scan()
    assert queries.lookup(env.conn, "/d.txt") is None
    assert env.decisions() == []


def test_last_copy_vanishes(env):
    write(env.root / "only.txt", "x")
    env.scan()
    (env.root / "only.txt").unlink()
    env.scan()
    [d] = env.decisions("missing")
    assert set(decisions.get_decision(env.conn, d["id"]).options) == {"acknowledge"}
    decisions.resolve(env.conn, d["id"], "acknowledge")
    assert queries.lookup(env.conn, "/only.txt") is None


def test_rename_rekeys_and_keeps_tags(env):
    write(env.root / "a" / "old.txt", "content")
    env.scan()
    tags.set_tag(env.conn, tags.resolve_targets(env.conn, "/a/old.txt", False), "keep",
                 True, env.server_id)
    (env.root / "b").mkdir()
    (env.root / "a" / "old.txt").rename(env.root / "b" / "new.txt")
    r = env.scan()["srv"]
    assert r.stats.renamed == 1 and r.stats.missing == 0
    assert queries.lookup(env.conn, "/a/old.txt") is None
    assert queries.lookup(env.conn, "/b/new.txt").tags == ["keep"]
    assert env.decisions() == []          # no other node held it
    assert not (env.root / "a").exists()  # pruned


def test_rename_with_other_copies_raises_decision(env):
    _, ssd = env.add_node("ssd")
    write(env.root / "old.txt", "content", mtime=1_000_000)
    write(ssd / "old.txt", "content", mtime=1_000_000)
    env.scan()
    tags.set_tag(env.conn, tags.resolve_targets(env.conn, "/old.txt", False), "t", True,
                 env.server_id)
    (env.root / "old.txt").rename(env.root / "new.txt")
    env.scan()
    [d] = env.decisions("rename")
    assert set(copies(env, "/old.txt")) == {"ssd"}
    assert queries.lookup(env.conn, "/new.txt").tags == ["t"]
    decisions.resolve(env.conn, d["id"], "apply")
    [w] = env.conn.execute("SELECT * FROM wanted").fetchall()
    assert w["op"] == "rename" and w["dst_node"] != env.server_id


def test_rename_detected_after_interrupted_scan(env, monkeypatch):
    write(env.root / "a.txt", "content")
    env.scan()
    (env.root / "a.txt").rename(env.root / "b.txt")
    write(env.root / "c.txt", "c")

    # Interrupt the scan after the first file was processed.
    real = scanner._process_file
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real(*a, **kw)

    monkeypatch.setattr(scanner, "_process_file", flaky)
    with pytest.raises(KeyboardInterrupt):
        env.scan()
    # Nothing was marked missing by the aborted scan.
    assert copies(env, "/a.txt")["srv"].state == "present"
    monkeypatch.setattr(scanner, "_process_file", real)
    r = env.scan()["srv"]
    assert r.stats.renamed == 1 and r.stats.missing == 0


def test_divergent(env):
    _, ssd = env.add_node("ssd")
    write(env.root / "doc.txt", "v1", mtime=1_000_000)
    write(ssd / "doc.txt", "v1", mtime=1_000_000)
    env.scan()
    write(env.root / "doc.txt", "v2 newer", mtime=2_000_000)
    env.scan()
    [d] = env.decisions("divergent")
    c = copies(env, "/doc.txt")
    assert c["srv"].current and not c["ssd"].current
    assert queries.lookup(env.conn, "/doc.txt").good_copies == 1

    # "keep" dismisses until something changes again.
    decisions.resolve(env.conn, d["id"], "keep")
    env.scan()
    assert env.decisions("divergent") == []
    write(env.root / "doc.txt", "v3 newest", mtime=3_000_000)
    env.scan()
    [d] = env.decisions("divergent")
    decisions.resolve(env.conn, d["id"], "update")
    [w] = env.conn.execute("SELECT w.*, n.name FROM wanted w JOIN nodes n"
                           " ON n.id = w.dst_node").fetchall()
    assert w["op"] == "overwrite" and w["name"] == "ssd"

    # Converged again (user copied it by hand): decision obsolete.
    shutil.copy2(env.root / "doc.txt", ssd / "doc.txt")
    env.scan()
    assert env.decisions("divergent") == []
    assert queries.lookup(env.conn, "/doc.txt").good_copies == 2


def test_offline_node_not_marked_missing(env):
    _, ssd = env.add_node("ssd")
    write(ssd / "x.txt", "x")
    env.scan()
    moved = env.tmp / "unplugged"
    ssd.rename(moved)               # drive unplugged
    ssd.mkdir()                     # empty mount point left behind
    results = env.scan()
    assert "ssd" not in results
    assert copies(env, "/x.txt")["ssd"].state == "present"
    assert env.conn.execute("SELECT online FROM nodes WHERE name = 'ssd'").fetchone()[0] == 0


def test_node_found_at_new_mount_point(env):
    _, ssd = env.add_node("ssd")
    write(ssd / "x.txt", "x")
    env.scan()
    mounts = env.tmp / "media" / "user"
    mounts.mkdir(parents=True)
    ssd.rename(mounts / "SSD")
    env.cfg.discovery.paths = [str(env.tmp / "media")]
    results = env.scan()
    assert results["ssd"].stats.missing == 0
    assert copies(env, "/x.txt")["ssd"].state == "present"


def test_refuses_scan_without_marker(env):
    node_id, ssd = env.add_node("ssd")
    shutil.rmtree(ssd / ".rei-fs")
    with pytest.raises(NodeError):
        scanner.scan_node(env.conn, env.cfg, node_id, ssd)


def test_unreadable_dir_not_marked_missing(env, monkeypatch):
    write(env.root / "locked" / "a.txt", "x")
    env.scan()
    real = os.scandir
    locked = str(env.root / "locked")

    def fake(path="."):
        if str(path) == locked:
            raise PermissionError(13, "Permission denied")
        return real(path)

    monkeypatch.setattr(scanner.os, "scandir", fake)
    r = env.scan()["srv"]
    assert r.errors and r.stats.missing == 0
    assert copies(env, "/locked/a.txt")["srv"].state == "present"


def test_newly_ignored_file_forgotten_silently(env):
    write(env.root / "a.log", "x")
    env.scan()
    env.cfg.scan.ignore.append("*.log")
    r = env.scan()["srv"]
    assert r.stats.forgotten == 1 and env.decisions() == []
    assert (env.root / "a.log").exists()


def test_nested_node_skipped(env):
    nested = env.root / "inner"
    nested.mkdir()
    with pytest.raises(NodeError):
        nodes.register_node(env.conn, nested, "inner")


def test_case_collision_flag(env):
    _, ssd = env.add_node("ssd")
    write(env.root / "Readme.txt", "a")
    write(ssd / "README.txt", "b")
    env.scan()
    assert "case" in queries.lookup(env.conn, "/Readme.txt").issues
    (ssd / "README.txt").unlink()
    env.scan()
    [d] = env.decisions("missing")
    decisions.resolve(env.conn, d["id"], "acknowledge")
    assert "case" not in queries.lookup(env.conn, "/Readme.txt").issues


def test_verify_detects_silent_corruption(env):
    f = write(env.root / "a.bin", "good", mtime=1_000_000)
    env.scan()
    write(f, "bad!", mtime=1_000_000)
    assert env.scan()["srv"].stats.hashed == 0      # unnoticed without --verify
    r = env.scan(verify=True)["srv"]
    assert r.stats.verify_mismatch == 1


def test_status_and_find(env):
    _, ssd = env.add_node("ssd")
    write(env.root / "both.txt", "x", mtime=1_000_000)
    write(ssd / "both.txt", "x", mtime=1_000_000)
    write(env.root / "one.txt", "y")
    env.scan()
    s = queries.status(env.conn)
    assert (s.files, s.satisfied, s.under_replicated) == (2, 1, 1)
    assert [f.vpath for f in queries.find(env.conn, under_replicated=True)] == ["/one.txt"]
    assert [f.vpath for f in queries.find(env.conn, node="ssd")] == ["/both.txt"]
    assert [f.vpath for f in queries.find(env.conn, "ONE*")] == ["/one.txt"]


def test_scan_never_holds_the_write_lock_while_hashing(env, monkeypatch):
    """Hashing a huge file (or reading a stalled disk) takes minutes: another
    writer (the API, the sync thread) must not have to wait for it."""
    import sqlite3

    for i in range(30):
        write(env.root / "d" / f"f{i}.txt", f"content {i}")
    write(env.root / "d" / "huge.bin", "x")
    other = sqlite3.connect(env.cfg.db_path, isolation_level=None, timeout=0.3)
    waits = []
    real = scanner.hash_file

    def probing_hash(path):
        t = __import__("time").time()
        try:                      # a writer elsewhere, while we are "reading the file"
            other.execute("BEGIN IMMEDIATE")
            other.execute("UPDATE meta SET value = value WHERE key = 'server_id'")
            other.execute("COMMIT")
            waits.append(("ok", __import__("time").time() - t))
        except sqlite3.OperationalError:
            waits.append(("LOCKED", __import__("time").time() - t))
        return real(path)

    monkeypatch.setattr(scanner, "hash_file", probing_hash)
    env.scan()
    assert len(waits) == 31
    assert [w for w in waits if w[0] != "ok"] == []
    monkeypatch.setattr(scanner, "hash_file", real)
    other.close()


def test_find_by_part_of_a_name(env):
    for name in ("Report-2025.pdf", "holiday/beach photo.jpg", "notes.txt",
                 "docs/annual report.docx", "docs/sub/other.txt"):
        write(env.root / name, name)
    env.scan()
    names = lambda pattern, **kw: [f.vpath for f in queries.find(env.conn, pattern, **kw)]  # noqa: E731
    # Without wildcards the text only has to be contained, whatever its case.
    assert names("report") == ["/Report-2025.pdf", "/docs/annual report.docx"]
    assert names("PHOTO") == ["/holiday/beach photo.jpg"]
    assert names("txt") == ["/docs/sub/other.txt", "/notes.txt"]
    assert names("nothing like this") == []
    # With a "/" the text is matched against the whole path.
    assert names("docs/sub") == ["/docs/sub/other.txt"]
    # The exact name still works, and so do globs (which match the whole name).
    assert names("notes.txt") == ["/notes.txt"]
    assert names("*.docx") == ["/docs/annual report.docx"]
    assert names("rep*") == ["/Report-2025.pdf"]
    assert names("report", tag="none") == []
    assert [f.vpath for f in queries.find(env.conn)] != []   # no pattern: everything


def test_search_finds_folders_too(env):
    for name in ("holiday/beach/a.jpg", "holiday/beach/b.jpg", "holiday/notes.txt",
                 "work/Holiday-plan.docx", "work/projects/x.txt", "misc.txt"):
        write(env.root / name, name)
    env.scan()
    ids, dirs = queries.search(env.conn, "holiday")
    # The file "Holiday-plan.docx" matches by its name; the folder by its own.
    assert [queries.file_info(env.conn, i).vpath for i in ids] == ["/work/Holiday-plan.docx"]
    assert dirs == [("/holiday", 3)]                       # files below it, counted recursively
    # Nested folders are listed too, each with its own count.
    assert queries.search(env.conn, "beach")[1] == [("/holiday/beach", 2)]
    assert queries.search(env.conn, "o")[1] == [         # "beach" has no "o": not listed
        ("/holiday", 3), ("/work", 2), ("/work/projects", 1)]
    assert queries.search(env.conn, "e")[1] == [("/holiday/beach", 2), ("/work/projects", 1)]
    # A path pattern is matched against the folder's whole path.
    assert queries.search(env.conn, "holiday/b")[1] == [("/holiday/beach", 2)]
    # Globs match the whole folder name.
    assert queries.search(env.conn, "pro*")[1] == [("/work/projects", 1)]
    assert queries.search(env.conn, "proj")[1] == [("/work/projects", 1)]
    assert queries.search(env.conn, "hol*")[1] == [("/holiday", 3)]
    # No pattern: no folders (they would all match). Nothing matching: nothing.
    assert queries.search(env.conn)[1] == []
    assert queries.search(env.conn, "zzz") == ([], [])
    # The other filters decide which files are counted below a folder.
    tags.set_tag(env.conn, tags.resolve_targets(env.conn, "/holiday/beach/a.jpg", False),
                 "keep", True, env.server_id)
    assert queries.search(env.conn, "holiday", tag="keep")[1] == [("/holiday", 1)]
    assert queries.search(env.conn, "beach", tag="keep")[1] == [("/holiday/beach", 1)]
    assert queries.search(env.conn, "work", tag="keep")[1] == []   # nothing tagged below it


def test_big_files_are_hashed_by_several_threads_with_the_same_result(env, monkeypatch):
    from reifs import scanner
    monkeypatch.setattr(scanner, "PARALLEL_MIN", 1000)
    for i in range(12):
        write(env.root / "big" / f"f{i}.bin", "x" * 3000 + str(i), mtime=1_000_000)
    env.cfg.scan.hash_threads = 4
    env.scan()
    parallel = {r[0]: r[1] for r in env.conn.execute("SELECT f.vpath, c.hash FROM copies c JOIN vfiles f ON f.id = c.file_id")}
    assert len(parallel) == 12
    env.conn.execute("DELETE FROM copies")
    env.cfg.scan.hash_threads = 1
    env.scan()
    one = {r[0]: r[1] for r in env.conn.execute("SELECT f.vpath, c.hash FROM copies c JOIN vfiles f ON f.id = c.file_id")}
    assert parallel == one


def test_an_automatic_scan_of_a_few_directories_does_not_read_the_whole_index(env):
    """With millions of files indexed, a scan of one small synchronous directory every few
    seconds read and sifted every copy of the node each time (a core busy for ever)."""
    from reifs import priority, scanner
    for i in range(300):
        write(env.root / "big" / f"d{i % 10}" / f"f{i}.txt", str(i), mtime=1_000_000)
    write(env.root / "live" / "a.txt", "a", mtime=1_000_000)
    env.scan()
    priority.set_priority(env.conn, "/live", priority.SYNCHRONOUS)
    prefs = priority.Prefs(priority.load(env.conn))
    node = env.conn.execute("SELECT id FROM nodes").fetchone()[0]
    sched = priority.Schedule(prefs, {0})
    known = scanner._known_copies(env.conn, node, {0}, prefs, sched)
    assert sorted(known) == ["/live/a.txt"]                      # not the 300 files elsewhere
    # a nearer setting inside it is respected, and a file with a setting of its own is read
    priority.set_priority(env.conn, "/live/sub", priority.ARCHIVAL)
    priority.set_priority(env.conn, "/big/d1/f1.txt", priority.SYNCHRONOUS)
    write(env.root / "live" / "sub" / "b.txt", "b", mtime=1_000_000)
    env.scan()
    prefs = priority.Prefs(priority.load(env.conn))
    known = scanner._known_copies(env.conn, node, {0}, prefs, priority.Schedule(prefs, {0}))
    assert sorted(known) == ["/big/d1/f1.txt", "/live/a.txt"]
    # everything is read when the default priority is among those scanned (or for a full scan)
    # (299: the file with a priority of its own is not among those of the default priority)
    assert len(scanner._known_copies(env.conn, node, {2}, prefs, priority.Schedule(prefs, {2}))) == 299
    assert len(scanner._known_copies(env.conn, node, None, prefs, priority.Schedule(prefs, None))) > 300


def test_the_wait_between_automatic_scans_grows_with_how_long_they_take():
    from reifs.service import scan_check_interval
    assert scan_check_interval(5, 0.05) == 5
    assert scan_check_interval(5, 3) == 12
    assert scan_check_interval(30, 60) == 240


def test_the_narrow_queries_start_from_the_directory_not_from_every_copy(env):
    """Reading a directory's copies (and looking for the fetch folder's) began with all of
    the node's copies and looked each one up: at half a million files that was most of an
    idle server's CPU."""
    from reifs import paths, scanner
    write(env.root / "live" / "a.txt", "a", mtime=1_000_000)
    env.scan()
    node = env.conn.execute("SELECT id FROM nodes").fetchone()[0]
    cond, args = paths.subtree_sql("/live", "d.path")
    plan = " ".join(r[3] for r in env.conn.execute(
        "EXPLAIN QUERY PLAN " + scanner._DIR_COPY_COLUMNS + cond, (node, *args)))
    assert "copies_node" not in plan and "d USING" in plan
    assert [r["vpath"] for r in env.conn.execute(scanner._DIR_COPY_COLUMNS + cond,
                                                 (node, *args))] == ["/live/a.txt"]
