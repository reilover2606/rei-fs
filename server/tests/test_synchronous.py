import time

from reifs import jobs, priority, runner, synchronous, tolerance

from conftest import write


def setup(env, root="/live"):
    _, ssd = env.add_node("ssd")
    write(env.root / "live" / "a.txt", "v1", mtime=1_000_000)
    write(ssd / "live" / "a.txt", "v1", mtime=1_000_000)
    env.scan()
    priority.set_priority(env.conn, root, priority.SYNCHRONOUS)
    return ssd


def run_jobs(env):
    runner.run_jobs(env.conn, env.cfg)


def test_a_change_is_copied_to_the_other_node_without_a_decision(env):
    ssd = setup(env)
    synchronous.run(env.conn, env.cfg, full=True)
    write(env.root / "live" / "a.txt", "v2 newer", mtime=2_000_000)
    env.scan()
    assert env.decisions("divergent") == []                    # no question is asked
    assert synchronous.run(env.conn, env.cfg) == 1
    run_jobs(env)
    assert (ssd / "live" / "a.txt").read_text() == "v2 newer"
    # the job is not listed
    assert env.conn.execute("SELECT COUNT(*) FROM jobs WHERE hidden = 0").fetchone()[0] == 0
    assert env.conn.execute("SELECT COUNT(*) FROM jobs WHERE hidden = 1").fetchone()[0] == 1


def test_the_newest_version_wins_whichever_node_has_it(env):
    ssd = setup(env)
    write(ssd / "live" / "a.txt", "from ssd, newest", mtime=3_000_000)
    write(env.root / "live" / "a.txt", "from srv", mtime=2_000_000)
    env.scan()
    synchronous.run(env.conn, env.cfg, full=True)
    run_jobs(env)
    assert (env.root / "live" / "a.txt").read_text() == "from ssd, newest"


def test_new_files_go_to_the_nodes_holding_the_directory_only(env):
    ssd = setup(env)
    _, usb = env.add_node("usb")                               # holds nothing of /live
    write(env.root / "live" / "new.txt", "n", mtime=1_500_000)
    env.scan()
    synchronous.run(env.conn, env.cfg, full=True)
    run_jobs(env)
    assert (ssd / "live" / "new.txt").read_text() == "n"
    assert not (usb / "live").exists()


def test_nothing_is_deleted_or_brought_back(env):
    ssd = setup(env)
    (ssd / "live" / "a.txt").unlink()                          # deleted on one node
    env.scan()
    synchronous.run(env.conn, env.cfg, full=True)
    run_jobs(env)
    assert not (ssd / "live" / "a.txt").exists()               # not restored
    assert (env.root / "live" / "a.txt").exists()              # and not deleted elsewhere


def test_tolerant_and_unmarked_paths_are_left_alone(env):
    ssd = setup(env)
    tolerance.set_tolerance(env.conn, "/live", True)
    write(env.root / "live" / "a.txt", "v2", mtime=2_000_000)
    env.scan()
    assert synchronous.run(env.conn, env.cfg, full=True) == 0
    priority.set_priority(env.conn, "/live", None)
    assert synchronous.run(env.conn, env.cfg, full=True) == 0


def test_a_single_file_can_be_synchronous(env):
    _, ssd = env.add_node("ssd")
    write(env.root / "x" / "one.txt", "1", mtime=1_000_000)
    write(env.root / "x" / "two.txt", "1", mtime=1_000_000)
    write(ssd / "x" / "one.txt", "1", mtime=1_000_000)
    write(ssd / "x" / "two.txt", "1", mtime=1_000_000)
    env.scan()
    priority.set_priority(env.conn, "/x/one.txt", priority.SYNCHRONOUS)
    write(env.root / "x" / "one.txt", "2", mtime=2_000_000)
    write(env.root / "x" / "two.txt", "2", mtime=2_000_000)
    env.scan()
    synchronous.run(env.conn, env.cfg, full=True)
    run_jobs(env)
    assert (ssd / "x" / "one.txt").read_text() == "2"
    assert (ssd / "x" / "two.txt").read_text() == "1"          # only the marked file


def test_synchronous_directories_are_scanned_every_few_seconds(env):
    setup(env)
    from reifs.config import Config
    assert env.cfg.scan.seconds(priority.SYNCHRONOUS) == 5
    assert env.cfg.scan.seconds(priority.INFREQUENT) == 60 * 60
    prefs = priority.Prefs(priority.load(env.conn))
    node = env.conn.execute("SELECT id FROM nodes LIMIT 1").fetchone()[0]
    priority.record_scan(env.conn, node, {0}, time.time() - 6)
    assert 0 in priority.due_classes(env.conn, env.cfg, node, prefs)
    priority.record_scan(env.conn, node, {0}, time.time())
    assert 0 not in priority.due_classes(env.conn, env.cfg, node, prefs)
