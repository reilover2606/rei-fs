"""``reifs`` command-line interface (administration and testing).

The FTXUI client (milestone 3) talks to the server over its API; this CLI
works directly on the local server home.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import (__version__, apps, archives, auth, tolerance, db, decisions, index, jobs, nodes, peers, queries,
               priority, replication, runner, scanner, tags)
from .config import ConfigError, default_home
from .hashing import short
from .locking import LockError, ServerLock
from .paths import VPathError, to_physical
from .server import init_server, open_server


class CliError(Exception):
    pass


# --- formatting ---------------------------------------------------------------


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def fmt_mtime(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def table(rows: list[list[str]], header: list[str]) -> str:
    rows = [header] + rows
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def copies_badge(info: queries.FileInfo) -> str:
    good = info.good_copies
    flags = ""
    if any(c.state == "present" and not c.current for c in info.copies):
        flags += " stale"
    if any(c.state == "missing" for c in info.copies):
        flags += " missing"
    if any(c.marked_delete for c in info.copies):
        flags += " del"
    if info.issues:
        flags += " !" + ",".join(sorted(info.issues))
    return f"{good}/{info.goal}{flags}"


# --- commands -----------------------------------------------------------------


def cmd_init(args) -> None:
    cfg = init_server(args.home, args.name, Path(args.root))
    print(f"Created server {args.name!r} at {cfg.home}")
    print(f"  node root: {cfg.storage.root}")
    print("Set the admin password with 'reifs passwd', then run 'reifs scan'.")


def cmd_passwd(args) -> None:
    cfg, conn = open_server(args.home)
    conn.close()
    pw = getpass.getpass("New admin password: ")
    if not pw:
        raise CliError("empty password")
    if getpass.getpass("Repeat: ") != pw:
        raise CliError("passwords do not match")
    cfg.server.password_hash = auth.hash_password(pw)
    cfg.save()
    print("Password updated.")


def cmd_node_add(args) -> None:
    _, conn = open_server(args.home)
    node_id = nodes.register_node(conn, Path(args.path), args.name)
    row = nodes.get_node(conn, node_id)
    print(f"Registered storage node {args.name!r} ({node_id})")
    print(f"  case sensitive: {bool(row['case_sensitive'])}, "
          f"restricted names: {bool(row['restricted_names'])}")


def cmd_node_list(args) -> None:
    _, conn = open_server(args.home)
    me = nodes.self_id(conn)
    rows = []
    for r in conn.execute(
        "SELECT n.*, s.name AS owner, (SELECT COUNT(*) FROM copies c WHERE c.node_id = n.id)"
        " AS ncopies, (SELECT COALESCE(SUM(size), 0) FROM copies c WHERE c.node_id = n.id)"
        " AS nbytes FROM nodes n JOIN servers s ON s.id = n.owner_id ORDER BY n.name"
    ):
        fs = ("cs" if r["case_sensitive"] else "ci") + (",win" if r["restricted_names"] else "")
        rows.append([
            r["name"], r["kind"], "online" if r["online"] else "offline",
            "self" if r["owner_id"] == me else r["owner"], str(r["ncopies"]),
            human_size(r["nbytes"]), fs, str(r["priority"]),
            "yes" if r["replication_target"] else "no", r["root_path"] or "",
        ])
    print(table(rows, ["NAME", "KIND", "STATE", "OWNER", "FILES", "SIZE", "FS", "PRIO",
                       "TARGET", "ROOT"]))


def cmd_node_set(args) -> None:
    _, conn = open_server(args.home)
    node = nodes.get_node(conn, args.node)
    if (args.priority is None and args.target is None and not args.rename
            and args.mirrored is None and args.backup_first is None):
        raise CliError("nothing to change")
    if node["owner_id"] != nodes.self_id(conn):
        raise CliError(f"node {node['name']!r} belongs to another server; change it there "
                       "(or from the client)")
    if args.rename:
        nodes.rename_node(conn, node["id"], args.rename)
    updates = {}
    if args.priority is not None:
        updates["priority"] = args.priority
    if args.target is not None:
        updates["replication_target"] = int(args.target == "yes")
    if args.mirrored is not None:
        updates["mirrored"] = int(args.mirrored == "yes")
    if args.backup_first is not None:
        updates["backup_first"] = int(args.backup_first == "yes")
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        with db.transaction(conn):
            conn.execute(f"UPDATE nodes SET {sets} WHERE id = ?", (*updates.values(), node["id"]))
            index.log_node(conn, node["id"])
    print(f"Updated node {args.rename or node['name']!r}.")


def cmd_discover(args) -> None:
    cfg, conn = open_server(args.home)
    d = nodes.discover(conn, cfg)
    for loc in d.online:
        print(f"online   {loc.name:20} {loc.root}")
    for name in d.offline:
        print(f"offline  {name}")
    for name, root in d.moved:
        print(f"moved    {name:20} now at {root}")
    for name, owner, path in d.foreign:
        print(f"foreign  {name:20} {path} (owned by server {owner})")


def cmd_tolerate(args) -> None:
    _, conn = open_server(args.home)
    value = {"yes": True, "no": False, "inherit": None}[args.value]
    try:
        if args.value is None or not args.vpath:
            raise CliError("usage: reifs tolerate PATH yes|no|inherit")
        res = tolerance.set_tolerance(conn, args.vpath, value)
    except tolerance.ToleranceError as e:
        raise CliError(str(e)) from None
    state = "several versions are fine" if res["tolerant"] else "conflicting versions are decided"
    print(f"{res['path']}: {state}" + ("" if not res["inherited"] else f" (from {res['set_on']})"))


def cmd_app(args) -> None:
    cfg, conn = open_server(args.home)
    act = args.action
    try:
        if act == "list":
            rows = apps.list_apps(conn)
            if not rows:
                print("No applications registered.")
            print(table([[a["name"], a["mode"], a["node"], a["dir"], a["server"],
                          "online" if a["online"] else "offline"] for a in rows],
                        ["NAME", "MODE", "NODE", "DIRECTORY", "SERVER", "STATE"]) if rows else "",
                  end="")
            return
        if not args.name:
            raise CliError(f"usage: reifs app {act} NAME")
        if act == "add":
            info, token = apps.register(conn, cfg, args.name, args.mode, args.node)
            print(f"Registered {info['name']!r} ({info['mode']}) on node {info['node']}.")
            print(f"  directory: {info['dir']}" + (f"   on disk: {info['path']}" if info["path"] else ""))
            if len(info["instances"]) > 1:
                print("  instances: " + ", ".join(i["node"] for i in info["instances"]))
            print(f"  token: {token}")
            print("Keep the token: it is not shown again (reifs app token NAME makes a new one).")
        elif act == "remove":
            apps.remove(conn, args.name, args.node)
            print(f"Unregistered {args.name!r}; its directory and files were left as they are.")
        else:
            print(apps.regenerate_token(conn, args.name, args.node))
    except (apps.AppError, nodes.NodeError, priority.PriorityError) as e:
        raise CliError(str(e)) from None


def cmd_archive(args) -> None:
    cfg, conn = open_server(args.home)
    act = args.action
    try:
        if act == "list":
            rows = conn.execute("SELECT * FROM archives WHERE present = 1 ORDER BY dir_path").fetchall()
            if not rows:
                print("No packed directories.")
            for r in rows:
                print(f"{r['dir_path']}  ({r['files']:,} files, {r['bytes'] / 1e6:.1f} MB unpacked, "
                      f"archive {archives.archive_vpath(r['dir_path'])})")
            return
        if not args.vpath:
            raise CliError(f"usage: reifs archive {act} DIRECTORY")

        def progress(msg: str) -> None:
            print(msg, file=sys.stderr, flush=True)

        with ServerLock(cfg.home):
            if act == "pack":
                res = archives.pack(conn, cfg, args.vpath, args.node, args.remove_originals,
                                    progress=progress, everywhere=args.everywhere)
                print(f"Packed {res['files']:,} files of {res['dir']} on {res['node']} into "
                      f"{res['archive']} ({res['archive_bytes'] / 1e6:.1f} MB).")
                if args.everywhere:
                    print(f"The archive will be copied to {res['targets']} other node(s) and the "
                          "originals removed there once it has arrived (the running server does "
                          "this; see the Jobs tab).")
                if res["originals_kept"]:
                    print("The original files were kept: remove them yourself once you are happy "
                          "with the archive (or pack with --remove-originals).")
            elif act == "unpack":
                res = archives.unpack(conn, cfg, args.vpath, args.node, progress=progress)
                print(f"Unpacked {res['files']:,} files into {res['dir']} on {res['node']}. "
                      "The archive is marked for deletion (Marked tab, or 'reifs marked').")
            else:
                res = archives.drop(conn, args.vpath)
                print(f"{res['dir']} is no longer a packed directory; {res['archive']} is "
                      "marked for deletion. Its contents are lost once it is deleted.")
    except archives.ArchiveError as e:
        raise CliError(str(e)) from None


def cmd_scan(args) -> None:
    cfg, conn = open_server(args.home)

    def progress(stats: scanner.ScanStats, current: str) -> None:
        if sys.stderr.isatty() and current:
            line = f"  {stats.seen} files, {stats.hashed} hashed: {current}"
            print("\r" + line[:110].ljust(110), end="", file=sys.stderr, flush=True)

    with ServerLock(cfg.home):
        disc, results = scanner.scan_all(conn, cfg, args.nodes or None, verify=args.verify,
                                         progress=progress, full=not args.due,
                                         new_only=args.new_only)
    if sys.stderr.isatty():
        print("\r" + " " * 110 + "\r", end="", file=sys.stderr)
    for name in disc.offline:
        if not args.nodes or name in args.nodes:
            print(f"{name}: offline, not scanned")
    for res in results:
        s = res.stats
        parts = [f"{s.seen} files", f"{s.hashed} hashed", f"+{s.added}", f"~{s.changed}"]
        for label, value in (("renamed", s.renamed), ("missing", s.missing),
                             ("reappeared", s.reappeared), ("forgotten", s.forgotten),
                             ("deleted as marked", s.deleted_marked),
                             ("VERIFY MISMATCH", s.verify_mismatch),
                             ("dirs pruned", s.pruned_dirs),
                             ("dirs not due (skipped)", s.skipped_dirs),
                             ("errors", len(res.errors))):
            if value:
                parts.append(f"{value} {label}")
        print(f"{res.node_name}: " + ", ".join(parts))
        for vpath, msg in res.errors[: args.max_errors]:
            print(f"    error: {vpath}: {msg}")
        if len(res.errors) > args.max_errors:
            print(f"    ... {len(res.errors) - args.max_errors} more")
    open_count = len(decisions.list_decisions(conn))
    if open_count:
        print(f"{open_count} decision(s) need attention: 'reifs decisions'")


def cmd_ls(args) -> None:
    cfg, conn = open_server(args.home)
    entries = queries.list_dir(conn, args.vdir, replication.policy(cfg))
    if entries is None:
        info = queries.lookup(conn, args.vdir)
        if info is None:
            raise CliError(f"not in the virtual tree: {args.vdir}")
        entries = [queries.DirEntry(info.vpath.rsplit("/", 1)[-1], False, 1, info)]
    rows = []
    for e in entries:
        if e.is_dir:
            rows.append([e.name + "/", "", f"{e.files} files", "", ""])
        else:
            f = e.file
            cur = next((c for c in f.copies if c.current), None)
            rows.append([
                e.name, human_size(f.size), copies_badge(f),
                fmt_mtime(cur.mtime_ns) if cur else "",
                " ".join(f.tags),
            ])
    print(table(rows, ["NAME", "SIZE", "COPIES", "MODIFIED", "TAGS"]))


def cmd_where(args) -> None:
    cfg, conn = open_server(args.home)
    info = queries.lookup(conn, args.vpath, replication.policy(cfg))
    if info is None:
        raise CliError(f"not a file in the virtual tree: {args.vpath}")
    print(info.vpath)
    print(f"  good copies: {info.good_copies}/{info.goal}")
    if info.tags:
        print(f"  tags: {', '.join(info.tags)}")
    if info.issues:
        print(f"  portability issues: {', '.join(sorted(info.issues))}")
    rows = []
    for c in info.copies:
        state = c.state
        if c.state == "present":
            state = "current" if c.current else "stale"
        if c.marked_delete:
            state += ", marked for deletion"
        rows.append([c.node_name, "online" if c.online else "offline", state,
                     human_size(c.size), fmt_mtime(c.mtime_ns), short(c.hash)])
    print(table(rows, ["NODE", "NODE STATE", "COPY", "SIZE", "MODIFIED", "HASH"]))


def cmd_find(args) -> None:
    cfg, conn = open_server(args.home)
    pol = replication.policy(cfg)
    ids, dirs = queries.search(conn, args.pattern, tag=args.tag, node=args.node,
                               issues_only=args.issues, under_replicated=args.under, policy=pol)
    results = [queries.file_info(conn, fid, pol) for fid in ids]
    if dirs:
        print(table([[d + "/", f"{n} file(s)"] for d, n in dirs], ["FOLDER", "CONTAINS"]))
        print()
    rows = [[f.vpath, human_size(f.size), copies_badge(f),
             ",".join(c.node_name for c in f.copies if c.state == "present"),
             " ".join(f.tags)] for f in results]
    print(table(rows, ["PATH", "SIZE", "COPIES", "NODES", "TAGS"]))
    print(f"{len(results)} file(s)")


def cmd_priority(args) -> None:
    from . import priority

    cfg, conn = open_server(args.home)
    try:
        if args.value is not None:
            info = priority.set_priority(conn, args.vdir, priority.parse(args.value))
        else:
            info = priority.get(conn, args.vdir)
    except (priority.PriorityError, VPathError) as e:
        raise SystemExit(f"error: {e}") from None
    where = "set here" if not info["inherited"] else \
        f"inherited from {info['set_on']}" if info["set_on"] else "the default"
    print(f"{info['path']}: {info['priority']} {info['name']} ({where})")


def cmd_tag(args) -> None:
    _, conn = open_server(args.home)
    me = nodes.self_id(conn)
    total = 0
    for vpath in args.vpaths:
        ids = tags.resolve_targets(conn, vpath, args.recursive)
        total += tags.set_tag(conn, ids, args.tag, args.action == "add", me)
    verb = "Tagged" if args.action == "add" else "Untagged"
    print(f"{verb} {total} file(s) with {args.tag!r}.")


def cmd_tags(args) -> None:
    _, conn = open_server(args.home)
    rows = [[t, str(n)] for t, n in tags.all_tags(conn)]
    print(table(rows, ["TAG", "FILES"]))


def cmd_decisions(args) -> None:
    _, conn = open_server(args.home)
    items = decisions.list_decisions(conn, args.status)
    if args.json:
        print(json.dumps([d.__dict__ for d in items], indent=2))
        return
    if not items:
        print(f"No {args.status} decisions.")
        return
    for d in items:
        where = f" on {d.node_name}" if d.node_name else ""
        print(f"#{d.id} [{d.kind}] {d.vpath or d.data.get('vpath') or d.data.get('dir', '?')}{where}")
        if d.kind == "group":
            n, size = decisions.group_info(conn, d.id)
            print(f"    {n} {d.data.get('member_kind')} decision(s) in one: "
                  + ", ".join(decisions.group_members(conn, d.id, 3)) + (" ..." if n > 3 else ""))
        elif d.kind == "rename":
            print(f"    renamed to {d.data['to']}; {len(d.data['nodes'])} other node(s) keep the old path")
        elif d.kind == "divergent":
            print(f"    {len(d.data['stale_nodes'])} stale cop(ies)"
                  + (" (several versions share the newest mtime!)" if d.data.get("tie") else ""))
        for opt, desc in d.options.items():
            print(f"    {opt:12} {desc}")


def cmd_decide(args) -> None:
    _, conn = open_server(args.home)
    print(decisions.resolve(conn, args.id, args.option))


def cmd_wanted(args) -> None:
    _, conn = open_server(args.home)
    if args.action in ("approve", "reject"):
        if not args.ids and not args.all:
            raise CliError(f"give wanted job ids to {args.action}, or --all")
        ids = None if args.all else args.ids
        if args.action == "reject":
            print(f"Rejected {jobs.reject_wanted(conn, ids)} proposal(s).")
            return
        res = replication.approve(conn, ids)
        print(res.summary())
        if res.local_job is not None:
            print(f"Run it with 'reifs jobs run {res.local_job}' (or let 'reifs serve' run it).")
        return
    rows = []
    for r in conn.execute(
        "SELECT w.*, f.vpath, s.name AS src, d.name AS dst FROM wanted w"
        " LEFT JOIN vfiles f ON f.id = w.file_id LEFT JOIN nodes s ON s.id = w.src_node"
        " LEFT JOIN nodes d ON d.id = w.dst_node WHERE w.status = 'open' ORDER BY w.id"
    ):
        extra = json.loads(r["data"])
        target = f"{extra['from']} -> {extra['to']}" if r["op"] == "rename" else r["vpath"] or ""
        rows.append([str(r["id"]), r["op"], target, r["src"] or "", r["dst"] or "",
                     r["reason"] + (f" ({r['message']})" if r["message"] else "")])
    if not rows:
        print("No proposed jobs.")
        return
    print(table(rows, ["ID", "OP", "FILE", "FROM", "TO", "REASON"]))
    print("Approve with 'reifs wanted approve ID... | --all', or reject.")


def _print_create_report(rep: jobs.CreateReport) -> None:
    for reason, vpaths in rep.excluded.items():
        shown = ", ".join(vpaths[:5]) + (f" (+{len(vpaths) - 5} more)" if len(vpaths) > 5 else "")
        print(f"  excluded ({reason}): {shown}")
    if rep.job_id is None:
        print("Nothing to do; no job created.")
    else:
        print(f"Created job #{rep.job_id} with {rep.items} item(s). "
              f"Run it with 'reifs jobs run {rep.job_id}'.")


def _run_jobs(cfg, conn, job_ids) -> list[runner.JobReport]:
    tty = sys.stderr.isatty()

    def progress(msg: str) -> None:
        if tty:
            print("\r" + msg[:110].ljust(110), end="", file=sys.stderr, flush=True)

    with ServerLock(cfg.home):
        try:
            reports = runner.run_jobs(conn, cfg, job_ids or None, progress)
        finally:
            if tty:
                print("\r" + " " * 110 + "\r", end="", file=sys.stderr)
    if not reports:
        print("No runnable jobs.")
    for r in reports:
        parts = [f"{r.done} done ({human_size(r.bytes)})"]
        for label, value in (("skipped", r.skipped), ("conflicts", r.conflicts),
                             ("waiting", r.waiting), ("will retry", r.retry),
                             ("failed", r.failed)):
            if value:
                parts.append(f"{value} {label}")
        print(f"job #{r.job_id} [{r.status}] {r.description}: " + ", ".join(parts))
        for vpath, msg in r.messages[:20]:
            print(f"    {vpath}: {msg}")
        if len(r.messages) > 20:
            print(f"    ... {len(r.messages) - 20} more (see 'reifs jobs show {r.job_id}')")
    return reports


def cmd_transfer(args) -> None:
    cfg, conn = open_server(args.home)
    try:
        rep = jobs.create_transfer_job(conn, args.vpaths, args.tag, args.to, args.src,
                                       move=args.move)
    except jobs.RemoteDestination as e:
        # Transfers into a node are run by its owner: create the job there.
        body = {"kind": "move" if args.move else "copy", "paths": args.vpaths, "to": args.to}
        if args.tag:
            body["tag"] = args.tag
        if args.src:
            body["from"] = args.src
        out = peers.forward(conn, e.owner_id, "POST", "/api/jobs", body)
        owner = conn.execute("SELECT name FROM servers WHERE id = ?", (e.owner_id,)).fetchone()[0]
        if out.get("job_id") is None:
            print(f"Nothing to do on {owner}; no job created.")
        else:
            print(f"Created job #{out['job_id']} with {out['items']} item(s) on server {owner!r}; "
                  "it runs there.")
        return
    _print_create_report(rep)
    if rep.job_id is not None and args.run:
        _run_jobs(cfg, conn, [rep.job_id])


def cmd_jobs(args) -> None:
    cfg, conn = open_server(args.home)
    if args.action == "run":
        _run_jobs(cfg, conn, args.ids)
        return
    if args.action in ("show", "cancel", "retry"):
        if len(args.ids) != 1:
            raise CliError(f"'jobs {args.action}' takes exactly one job id")
        job_id = args.ids[0]
        if args.action == "cancel":
            print(f"Cancelled job #{job_id} ({jobs.cancel_job(conn, job_id)} item(s) not done).")
        elif args.action == "retry":
            print(f"Re-queued {jobs.retry_job(conn, job_id)} failed item(s) of job #{job_id}.")
        else:
            _show_job(conn, job_id, args.all)
        return
    q = "SELECT * FROM jobs WHERE hidden = 0"
    if not args.all:
        q += " AND status NOT IN ('done', 'cancelled')"
    rows = []
    for j in conn.execute(q + " ORDER BY id"):
        c = jobs.item_counts(conn, j["id"])
        total = sum(c.values())
        summary = " ".join(f"{k}:{v}" for k, v in sorted(c.items()) if k != "done")
        rows.append([str(j["id"]), j["status"], f"{c.get('done', 0)}/{total}", summary,
                     j["description"]])
    if not rows:
        print("No active jobs." if not args.all else "No jobs.")
        return
    print(table(rows, ["ID", "STATUS", "DONE", "OTHER", "DESCRIPTION"]))


def _show_job(conn, job_id: int, show_all: bool) -> None:
    j = jobs.get_job(conn, job_id)
    print(f"job #{j['id']} [{j['status']}] {j['description']}")
    print(f"  created {db.to_iso(j['created'])}"
          + (f", started {db.to_iso(j['started'])}" if j["started"] else "")
          + (f", finished {db.to_iso(j['finished'])}" if j["finished"] else ""))
    c = jobs.item_counts(conn, job_id)
    print("  items: " + ", ".join(f"{v} {k}" for k, v in sorted(c.items())))
    q = ("SELECT i.*, s.name AS src, d.name AS dst FROM job_items i"
         " LEFT JOIN nodes s ON s.id = i.src_node JOIN nodes d ON d.id = i.dst_node"
         " WHERE i.job_id = ?")
    if not show_all:
        q += " AND i.state NOT IN ('done')"
    rows = [[str(i["id"]), i["op"], i["state"],
             i["vpath"] + (f" -> {i['new_vpath']}" if i["new_vpath"] else ""),
             i["src"] or "(any)", i["dst"], str(i["attempts"]), i["message"] or ""]
            for i in conn.execute(q + " ORDER BY i.id", (job_id,))]
    if rows:
        print(table(rows, ["ITEM", "OP", "STATE", "FILE", "FROM", "TO", "TRIES", "MESSAGE"]))


def cmd_marked(args) -> None:
    _, conn = open_server(args.home)
    rows = jobs.marked_copies(conn, args.node)
    if not rows:
        print("No copies are marked for deletion.")
        return
    out = [[r["node_name"], r["vpath"], human_size(r["size"]),
            str(to_physical(r["root_path"], r["vpath"])) if r["root_path"] else ""]
           for r in rows]
    print(table(out, ["NODE", "FILE", "SIZE", "PHYSICAL PATH"]))
    print(f"{len(rows)} cop(ies), {human_size(sum(r['size'] for r in rows))}. Delete them "
          "yourself; the next scan clears the marks. Or keep them with 'reifs unmark'.")


def cmd_unmark(args) -> None:
    _, conn = open_server(args.home)
    if not args.vpaths and not args.all:
        raise CliError("give paths to unmark, or --all")
    n = jobs.unmark(conn, [] if args.all else args.vpaths, args.node)
    print(f"Removed the deletion mark from {n} cop(ies).")


def _make_ctrl_c_work() -> None:
    """Make sure Ctrl+C can reach us (POSIX).

    * A terminal left in raw mode by a crashed or killed full-screen program
      has signals disabled (``stty -isig``): Ctrl+C is then just an input
      character and nothing stops the server. Re-enable them (only when we
      are the foreground job, otherwise changing the terminal would stop us).
    * SIGINT/SIGTERM/SIGHUP blocked in the inherited signal mask are unblocked.
    """
    if os.name != "posix":
        return
    import signal

    try:
        signal.pthread_sigmask(signal.SIG_UNBLOCK,
                               {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
    except (ValueError, OSError):
        pass
    try:
        import termios
    except ImportError:
        return
    try:
        if not sys.stdin.isatty() or os.getpgrp() != os.tcgetpgrp(sys.stdin.fileno()):
            return
        attrs = termios.tcgetattr(sys.stdin.fileno())
        if not attrs[3] & termios.ISIG:
            attrs[3] |= termios.ISIG
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, attrs)
            print("note: this terminal had Ctrl+C disabled (a program that did not exit "
                  "cleanly leaves it like that; 'stty sane' also fixes it). Enabled it.",
                  file=sys.stderr, flush=True)
    except (OSError, ValueError, termios.error):
        pass


class _QuitKey:
    """Press q to stop the server (works without Enter, in a terminal).

    While the server runs in a terminal's foreground, the keyboard is put in
    cbreak mode (single keys, Ctrl+C still works) and restored on exit. With
    no terminal (a service, a pipe, a background job) nothing is read.
    """

    def __init__(self, on_quit):
        self.on_quit = on_quit
        self.saved = None
        self.active = False

    def start(self) -> bool:
        if not sys.stdin or not sys.stdin.isatty():
            return False
        if os.name == "posix":
            try:
                import termios
                import tty

                fd = sys.stdin.fileno()
                if os.getpgrp() != os.tcgetpgrp(fd):
                    return False       # background job: reading would stop it
                self.saved = termios.tcgetattr(fd)
                tty.setcbreak(fd)
            except (ImportError, OSError, ValueError):
                self.saved = None
                return False
        self.active = True
        threading.Thread(target=self._watch, name="reifs-keys", daemon=True).start()
        return True

    def _watch(self) -> None:
        import select

        while self.active:
            try:
                if os.name == "posix":
                    if not select.select([sys.stdin], [], [], 0.5)[0]:
                        continue
                    key = os.read(sys.stdin.fileno(), 1).decode("utf-8", "ignore")
                else:
                    import msvcrt

                    key = msvcrt.getwch()
            except (OSError, ValueError):
                return
            if not key:
                return                 # input closed
            if key in ("q", "Q"):
                self.on_quit()
                return

    def restore(self) -> None:
        self.active = False
        if self.saved is not None:
            try:
                import termios

                termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, self.saved)
            except (ImportError, OSError, ValueError, termios.error):
                pass
            self.saved = None


def cmd_serve(args) -> None:
    import logging

    from . import api, tls
    from .service import Syncer, Worker

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg, conn = open_server(args.home)
    db.make_incremental(conn, cfg.db_path)
    conn.close()
    # Scans and transfers run Python code for a long time. With the default 5 ms
    # switch interval a request thread waits for its turn at every step of its
    # work, which made the API crawl while a job ran. A short interval gives it back.
    sys.setswitchinterval(0.0005)
    if not cfg.server.password_hash:
        print("warning: no admin password set; clients cannot log in ('reifs passwd')",
              file=sys.stderr)
    syncer = Syncer(cfg)
    worker = Worker(cfg, syncer)
    server = api.make_server(cfg, worker, args.listen, syncer)
    cert, _ = tls.paths(cfg.home)
    host, port = server.server_address[:2]
    print(f"rei-fs server {cfg.server.name!r} listening on https://{host}:{port}")
    print(f"certificate fingerprint (SHA-256): {tls.fingerprint(cert)}")
    print("Compare it with what the client shows the first time it connects.", flush=True)
    import faulthandler
    import signal
    import threading

    def on_signal(signum, frame):
        # Our own handlers: a SIGINT inherited as "ignored" (a background job
        # of a script, some launchers) would otherwise make Ctrl+C do nothing.
        raise KeyboardInterrupt

    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, on_signal)
            except (ValueError, OSError):
                pass
    if hasattr(signal, "SIGUSR1"):
        try:  # kill -USR1 PID: show where every thread is (for diagnosing hangs)
            faulthandler.register(signal.SIGUSR1, all_threads=True)
        except (ValueError, OSError, RuntimeError):
            pass
    _make_ctrl_c_work()
    quit_reason = []

    def quit_from_keyboard() -> None:
        quit_reason.append("q")
        server.shutdown()              # makes serve_forever() return

    keys = _QuitKey(quit_from_keyboard)
    has_keys = keys.start()
    print("Press q (or Ctrl+C) to stop." if has_keys else "Press Ctrl+C to stop.", flush=True)
    worker.start()
    syncer.start()

    def force_exit(code: int) -> None:
        from . import transfer

        keys.restore()                 # do not leave the terminal in cbreak mode
        transfer.kill_all()
        syncer.daemon.stop()
        sys.stderr.flush()
        os._exit(code)

    try:
        server.serve_forever()
        if quit_reason:
            print("\nshutting down... (Ctrl+C to force)", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        print("\nshutting down... (Ctrl+C again to force)", file=sys.stderr, flush=True)
    keys.restore()
    # Shutdown must always end: show what is stuck after a few seconds, and
    # exit regardless after a few more (also covers threads that would block
    # the interpreter's own exit).
    limit = float(os.environ.get("REIFS_SHUTDOWN_SECONDS") or 20)
    faulthandler.dump_traceback_later(max(1.0, limit * 0.4), exit=False)

    def watchdog() -> None:
        print(f"shutdown did not finish in {limit:g} s (thread stacks above); forcing exit",
              file=sys.stderr, flush=True)
        force_exit(1)

    timer = threading.Timer(limit, watchdog)
    timer.daemon = True
    timer.start()
    try:
        server.server_close()
        server.api.pool.shutdown(wait=False, cancel_futures=True)
        worker.close(timeout=10)   # stops the current scan or transfer
        syncer.close(timeout=5)
    except KeyboardInterrupt:
        # A second Ctrl+C (or SIGTERM): do not wait for anything.
        print("forcing exit", file=sys.stderr, flush=True)
        force_exit(130)
    faulthandler.cancel_dump_traceback_later()
    if worker.thread.is_alive() or syncer.thread.is_alive():
        print("some work did not stop in time; ending it", file=sys.stderr, flush=True)
        force_exit(1)


def cmd_fingerprint(args) -> None:
    from . import tls

    cfg, conn = open_server(args.home)
    conn.close()
    cert, _ = tls.ensure_certificate(cfg.home, cfg.server.name)
    print(tls.fingerprint(cert))


def cmd_peer(args) -> None:
    cfg, conn = open_server(args.home)
    if args.action == "pair":
        if not args.target:
            raise CliError("give the other server's HOST[:PORT]")
        info, fp = peers.hello(args.target)
        print(f"Server {info.get('name')!r} ({info.get('id')}) at {args.target}")
        print(f"certificate fingerprint: {fp}")
        print("Compare it with 'reifs fingerprint' on that server.")
        if not args.yes and input("Send a pairing request? [y/N] ").strip().lower() != "y":
            print("Cancelled.")
            return
        peers.request_pairing(conn, cfg, args.target, fp)
        print("Request sent. Accept it on the other server ('reifs peer list' there, or the "
              "client's Servers tab). This server must be running 'reifs serve' for that.")
        return
    if args.action in ("accept", "reject"):
        if not args.target or not args.target.isdigit():
            raise CliError(f"give the request number to {args.action} (see 'reifs peer list')")
        if args.action == "accept":
            name = peers.accept_pairing(conn, cfg, int(args.target))
            print(f"Paired with {name!r}.")
        else:
            peers.reject_pairing(conn, int(args.target))
            print("Rejected.")
        return
    if args.action == "remove":
        row = peers.find_peer(conn, args.target or "")
        peers.unpair(conn, row["id"])
        print(f"Unpaired from {row['name']!r}.")
        return
    if args.action == "transport":
        row = peers.find_peer(conn, args.target or "")
        if args.value not in ("daemon", "ssh"):
            raise CliError("use 'reifs peer transport NAME daemon|ssh'")
        if args.value == "ssh" and not row["ssh_enabled"]:
            raise CliError(f"{row['name']} does not offer SSH: set [ssh] enabled = true there "
                           "(and sync)")
        with db.transaction(conn):
            conn.execute("UPDATE servers SET transport = ? WHERE id = ?", (args.value, row["id"]))
        print(f"Files from {row['name']} are now pulled over "
              f"{'SSH' if args.value == 'ssh' else 'its rsync daemon'}.")
        return
    if args.action == "sync":
        from .service import sync_peer

        for p in peers.paired_peers(conn):
            if args.target and args.target not in (p["name"], p["id"]):
                continue
            n = sync_peer(conn, p["id"])
            row = peers.peer_row(conn, p["id"])
            state = "ok" if row["online"] else f"failed: {row['last_error']}"
            print(f"{p['name']}: {n} change(s) applied ({state})")
        return
    rows = [[p["name"], p["address"], "online" if p["online"] else "offline",
             p["transport"] + ("" if p["transport"] == "ssh" or not p["ssh_enabled"]
                               else " (ssh offered)"),
             db.to_iso(p["last_sync"] or 0)[:19], p["last_error"] or ""] for p in peers.paired_peers(conn)]
    print(table(rows, ["SERVER", "ADDRESS", "STATE", "TRANSPORT", "LAST SYNC", "LAST ERROR"])
          if rows else "Not paired with any server.")
    reqs = conn.execute("SELECT * FROM pair_requests WHERE status = 'pending' ORDER BY id"
                        ).fetchall()
    if reqs:
        print("\nPending pairing requests:")
        print(table([[str(r["id"]), "incoming" if r["direction"] == "in" else "outgoing",
                      r["name"], r["address"], r["fingerprint"][:23] + "..."] for r in reqs],
                    ["ID", "DIRECTION", "SERVER", "ADDRESS", "FINGERPRINT"]))
        print("Accept an incoming request with 'reifs peer accept ID'.")


def cmd_ssh(args) -> None:
    from . import ssh

    cfg, conn = open_server(args.home)
    if args.action == "key":
        print(ssh.ensure_key(cfg.home, cfg.server.name))
        return
    lines, rrsync, missing = ssh.authorize_lines(conn)
    if not cfg.ssh.enabled:
        print("note: [ssh] enabled is false in config.toml, so peers will not pull over SSH "
              "from this server until you enable it.")
    if missing:
        print(f"no SSH key known yet for: {', '.join(missing)} (they appear after a sync)")
    if not lines:
        print("Nothing to authorize.")
        return
    if not rrsync:
        print("warning: 'rrsync' was not found, so these keys are NOT restricted to read-only "
              "rsync (install it, it comes with rsync, then run this again).")
    if args.apply:
        path, added = ssh.install(lines, Path(args.file) if args.file else None)
        print(f"Added {added} key(s) to {path}.")
    else:
        print("Add these lines to ~/.ssh/authorized_keys of the account in [ssh] user "
              "(or run 'reifs ssh authorize --apply'):")
        for line in lines:
            print(line)


def cmd_db(args) -> None:
    cfg, conn = open_server(args.home)
    if args.action == "stats":
        sizes = db.table_sizes(conn)
        total = sum(s for _, s in sizes) if sizes else 0
        print(f"{cfg.db_path}: {cfg.db_path.stat().st_size / 2**20:.0f} MiB, "
              f"{db.free_space_ratio(conn) * 100:.1f}% of it free pages")
        if sizes is None:
            print("(this SQLite has no dbstat: no breakdown)")
            return
        for name, size in sizes[:15]:
            print(f"  {name:34}{size / 2**20:9.0f} MiB  {size * 100 / max(total, 1):5.1f}%")
        return
    holders = db.holders_of(cfg.db_path)
    if holders and not args.force:
        listing = "\n".join(f"  pid {pid}: {cmd}" for pid, cmd in holders)
        raise CliError("other processes have the database open (a running server would be "
                       f"unusable while it is compacted):\n{listing}\nStop them, or use --force.")
    before, after = db.vacuum(conn, cfg.db_path)
    print(f"database: {before / 2**20:.0f} MiB -> {after / 2**20:.0f} MiB")


def cmd_schedule(args) -> None:
    from . import schedules

    cfg, conn = open_server(args.home)
    act = args.action
    try:
        if act == "add":
            if not (args.target and args.src and args.dst and args.every):
                raise CliError("usage: reifs schedule add PATH --from NODE --to NODE --every 6h")
            sid = schedules.create(conn, args.target, args.src, args.dst, args.every,
                                   args.name, not args.no_update)
            print(f"Schedule #{sid} added; its first run is due now (the server runs it).")
        elif act in ("remove", "enable", "disable", "run", "every", "path"):
            if not args.target or not args.target.isdigit():
                raise CliError(f"usage: reifs schedule {act} ID" + (" INTERVAL" if act == "every" else ""))
            sid = int(args.target)
            if act == "remove":
                schedules.remove(conn, sid)
                print(f"Schedule #{sid} removed.")
            elif act in ("enable", "disable"):
                schedules.update(conn, sid, enabled=act == "enable")
                print(f"Schedule #{sid} {act}d.")
            elif act == "path":
                if not args.interval:
                    raise CliError("usage: reifs schedule path ID NEW-DIRECTORY")
                schedules.update(conn, sid, path=args.interval)
                print(f"Schedule #{sid} now syncs {args.interval}.")
            elif act == "every":
                schedules.update(conn, sid, interval=args.interval or args.every)
                print(f"Schedule #{sid} now runs every {args.interval or args.every}.")
            else:
                res = schedules.run(conn, sid, force=True)
                print(res.note or ("nothing to do" if not res.ran else "done"))
                if res.job_id is not None:
                    print(f"Run it with 'reifs jobs run {res.job_id}' (or let the server do it).")
        else:
            rows = schedules.listing(conn)
            if not rows:
                print("No schedules. Add one with 'reifs schedule add PATH --from A --to B --every 6h'.")
                return
            out = []
            for r in rows:
                due = "now" if r["next_due"] <= time.time() else \
                    f"in {int((r['next_due'] - time.time()) // 60)} min"
                out.append([str(r["id"]), r["name"], r["path"], f"{r['from']} -> {r['to']}",
                            r["every"], "on" if r["enabled"] else "off", due,
                            r["waiting"] or r["note"]])
            print(table(out, ["ID", "NAME", "DIRECTORY", "NODES", "EVERY", "", "NEXT", "LAST RESULT"]))
    except (schedules.ScheduleError, jobs.RemoteDestination) as e:
        raise CliError(str(e) if not isinstance(e, jobs.RemoteDestination) else
                       "the destination node belongs to another server: run this there") from None


def cmd_node_audit(args) -> None:
    from . import audit

    cfg, conn = open_server(args.home)
    row = nodes.get_node(conn, args.node)
    root = next((loc.root for loc in nodes.discover(conn, cfg).online
                 if loc.node_id == row["id"]), None)
    if root is None:
        raise CliError(f"node {row['name']!r} is not attached here")
    out = audit.audit_node(conn, cfg, row["id"], root)
    print(f"{row['name']}: {out['files_on_disk']} files on disk, {out['files_indexed']} indexed")
    for item in out["not_in_index"]:
        print(f"  {item['count']:>7} not in the index: {item['reason']}")
        for ex in item["examples"][:args.examples]:
            print(f"            {ex}")
    for item in out["other"]:
        print(f"  {item['count']:>7} {item['reason']}")
        for ex in item["examples"][:args.examples]:
            print(f"            {ex}")
    gone = out["listed_but_not_on_disk"]
    if gone["count"]:
        print(f"  {gone['count']:>7} listed in the index but not on disk")
        for ex in gone["examples"][:args.examples]:
            print(f"            {ex}")
    if not (out["not_in_index_total"] or gone["count"] or out["other"]):
        print("  everything on disk is in the index, and the other way round.")


def cmd_node_move(args) -> None:
    cfg, conn = open_server(args.home)
    with ServerLock(cfg.home):
        root = nodes.move_root(conn, cfg, args.node, args.path, move_folder=args.move)
    print(f"node {args.node!r} now has its root at {root}")


def cmd_node_adopt(args) -> None:
    _, conn = open_server(args.home)
    name = nodes.adopt_node(conn, args.node)
    print(f"This server now owns node {name!r}. Run 'reifs scan {name}'.")


def cmd_replication(args) -> None:
    cfg, conn = open_server(args.home)
    if args.action == "auto":
        if args.value not in ("on", "off"):
            raise CliError("use 'reifs replication auto on' or 'auto off'")
        cfg.server.auto_replicate = args.value == "on"
        cfg.save()
        print(f"Automatic approval is {args.value} (a running 'reifs serve' picks it up when "
              "restarted; the client changes it live).")
        return
    if args.action == "copies":
        if not args.value or not args.value.isdigit() or int(args.value) < 1:
            raise CliError("use 'reifs replication copies N' (N >= 1), optionally --tag T")
        n = int(args.value)
        if args.tag:
            cfg.replication.tag_copies[args.tag] = n
            what = f"files tagged {args.tag!r}"
        else:
            cfg.replication.copies = n
            what = "every file"
        cfg.save()
        print(f"Goal: {n} good copies of {what}.")
        return
    if args.action == "plan":
        report, approved = replication.cycle(conn, cfg)
    else:
        raw = replication.last_plan(conn)
        fields = replication.PlanReport.__dataclass_fields__
        report = replication.PlanReport(**{k: v for k, v in raw.items() if k in fields}) \
            if raw else None
        approved = None
    pol = replication.policy(cfg)
    s = queries.status(conn, pol)
    extra = ", ".join(f"{t}: {n}" for t, n in pol.tag_copies)
    print(f"goal: {pol.copies} good copies per file" + (f" ({extra})" if extra else "")
          + f"; automatic approval {'on' if cfg.server.auto_replicate else 'off'}")
    print(f"  satisfied:        {s.satisfied}")
    print(f"  under-replicated: {s.under_replicated}")
    if report is None:
        print("No plan yet: run 'reifs replication plan'.")
        return
    print(f"last plan ({db.to_iso(report.finished)[:19]}):")
    print(f"  files below their goal:         {report.under_replicated}"
          f" ({report.responsible} planned by this server; others by the server holding them)")
    print(f"  copies proposed now:            {report.proposed}")
    print(f"  files already fully proposed:   {report.already_proposed}")
    print(f"  declined earlier:               {report.declined}")
    print(f"  files lacking a destination:    {report.no_destination}")
    for reason, n in sorted(report.reasons.items(), key=lambda x: -x[1]):
        print(f"      {n:6}  {reason}")
    for vpath, reason in report.examples[:args.examples]:
        print(f"      e.g. {vpath}: {reason}")
    if approved is not None:
        print(f"approved automatically: {approved.summary()}")
    elif report.proposed or report.already_proposed:
        print("See and approve the proposals with 'reifs wanted' / 'reifs wanted approve --all'.")


def cmd_status(args) -> None:
    cfg, conn = open_server(args.home)
    s = queries.status(conn, replication.policy(cfg))
    online = conn.execute("SELECT COUNT(*) FROM nodes WHERE online = 1").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    print(f"server {cfg.server.name!r}  ({cfg.home})")
    print(f"  nodes:            {online}/{total} online")
    paired = conn.execute("SELECT COUNT(*), COALESCE(SUM(online), 0) FROM servers"
                          " WHERE paired = 1").fetchone()
    if paired[0]:
        print(f"  paired servers:   {paired[1]}/{paired[0]} reachable")
    print(f"  files:            {s.files} ({human_size(s.bytes)})")
    print(f"  enough copies:    {s.satisfied} (goal {cfg.replication.copies})")
    print(f"  under-replicated: {s.under_replicated} ({s.single_copy} with a single good copy)")
    print(f"  divergent:        {s.divergent}")
    print(f"  missing copies:   {s.missing_copies}")
    print(f"  marked to delete: {s.marked_delete}")
    print(f"  non-portable:     {s.non_portable}")
    print(f"  open decisions:   {s.open_decisions}")
    print(f"  wanted jobs:      {s.open_wanted}")
    if not cfg.server.password_hash:
        print("  warning: no admin password set ('reifs passwd')")


# --- parser -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reifs", description="rei-fs server administration")
    p.add_argument("--version", action="version", version=f"rei-fs {__version__}")
    p.add_argument("--home", type=Path, default=None,
                   help="server home directory (default: $REIFS_HOME or platform default)")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    s = sub.add_parser("init", help="create a server home and its own node")
    s.add_argument("--name", required=True, help="server (and server node) name")
    s.add_argument("--root", required=True, help="root directory of this server's node")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("passwd", help="set the admin password")
    s.set_defaults(func=cmd_passwd)

    node = sub.add_parser("node", help="manage nodes").add_subparsers(
        dest="node_cmd", required=True, metavar="ACTION")
    s = node.add_parser("add", help="register a storage node (drive, folder) owned by this server")
    s.add_argument("path")
    s.add_argument("--name", required=True)
    s.set_defaults(func=cmd_node_add)
    s = node.add_parser("list", help="list nodes")
    s.set_defaults(func=cmd_node_list)
    s = node.add_parser("set", help="change node settings")
    s.add_argument("node")
    s.add_argument("--priority", type=int, help="higher = preferred replication target")
    s.add_argument("--target", choices=["yes", "no"], help="may receive replication copies")
    s.add_argument("--mirrored", choices=["yes", "no"],
                   help="the drive is mirrored (RAID 1, ...): its copies count as two")
    s.add_argument("--backup-first", dest="backup_first", choices=["yes", "no"],
                   help="the node exists for backups: planning fills it before other nodes")
    s.add_argument("--rename", metavar="NAME")
    s.set_defaults(func=cmd_node_set)

    s = node.add_parser("audit", help="list what is on a node's disk but not in the index, and why")
    s.add_argument("node")
    s.add_argument("--examples", type=int, default=10, help="paths to show per reason")
    s.set_defaults(func=cmd_node_audit)

    s = node.add_parser("move", help="change where a node's root folder is on this machine")
    s.add_argument("node")
    s.add_argument("path", help="the new absolute path")
    s.add_argument("--move", action="store_true",
                   help="move the folder there (default: it is already there)")
    s.set_defaults(func=cmd_node_move)

    s = node.add_parser("adopt", help="take ownership of another server's drive attached here")
    s.add_argument("node", help="name (see 'reifs discover') or path of the drive's node root")
    s.set_defaults(func=cmd_node_adopt)

    s = sub.add_parser("peer", help="other servers: list, pair, accept, reject, remove, sync")
    s.add_argument("action", nargs="?", default="list",
                   choices=["list", "pair", "accept", "reject", "remove", "sync", "transport"])
    s.add_argument("target", nargs="?", help="address (pair), request id, or server name")
    s.add_argument("value", nargs="?", help="daemon|ssh (with transport)")
    s.add_argument("-y", "--yes", action="store_true", help="pair without asking")
    s.set_defaults(func=cmd_peer)

    s = sub.add_parser("ssh", help="rsync over SSH: show this server's key, authorize peers")
    s.add_argument("action", choices=["key", "authorize"])
    s.add_argument("--apply", action="store_true", help="append to authorized_keys")
    s.add_argument("--file", help="authorized_keys file (default ~/.ssh/authorized_keys)")
    s.set_defaults(func=cmd_ssh)

    s = sub.add_parser("discover", help="find which owned nodes are attached")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("tolerate", help="allow several versions of a file or directory (no decisions)")
    s.add_argument("vpath", help="a file or directory of the virtual tree")
    s.add_argument("value", choices=["yes", "no", "inherit"])
    s.set_defaults(func=cmd_tolerate)

    s = sub.add_parser("app", help="applications that use rei-fs for their files")
    s.add_argument("action", choices=["add", "list", "remove", "token"])
    s.add_argument("name", nargs="?", help="the application's name")
    s.add_argument("--mode", choices=["storage", "instance"], default="storage",
                   help="storage: a directory to fill; instance: its files are kept the same on "
                        "every node it is registered on")
    s.add_argument("--node", help="the node (default for a new one: any online node of this server)")
    s.set_defaults(func=cmd_app)

    s = sub.add_parser("archive", help="pack a directory into one squashfs archive, or unpack one")
    s.add_argument("action", choices=["pack", "unpack", "drop", "list"])
    s.add_argument("vpath", nargs="?", help="the directory in the virtual tree, e.g. /src/old-app")
    s.add_argument("--node", help="node to pack on / unpack to (default: one holding everything)")
    s.add_argument("--remove-originals", action="store_true",
                   help="pack: delete the files that went into the archive, after checking it")
    s.add_argument("--everywhere", action="store_true",
                   help="pack: also copy the archive to every node that holds the directory and "
                        "remove the originals there (each file is checked against the archive)")
    s.set_defaults(func=cmd_archive)

    s = sub.add_parser("scan", help="scan online owned nodes for changes")
    s.add_argument("nodes", nargs="*", help="node names (default: all online)")
    s.add_argument("--verify", action="store_true", help="re-hash every file")
    s.add_argument("--max-errors", type=int, default=20)
    s.add_argument("--new-only", action="store_true",
                   help="only discover files not indexed yet (no re-hashing, no missing/renamed)")
    s.add_argument("--due", action="store_true",
                   help="only directories due for an automatic scan (by update priority)")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("db", help="the database: its size (stats) and compacting it (vacuum)")
    s.add_argument("action", choices=["stats", "vacuum"])
    s.add_argument("--force", action="store_true",
                   help="vacuum even though other processes have the database open")
    s.set_defaults(func=cmd_db)

    s = sub.add_parser("schedule", help="keep a directory synced from one node to another "
                                        "at regular intervals")
    s.add_argument("action", nargs="?", default="list",
                   choices=["list", "add", "remove", "enable", "disable", "run", "every", "path"])
    s.add_argument("target", nargs="?", help="add: the directory; others: the schedule's ID")
    s.add_argument("interval", nargs="?", help="every: the new interval")
    s.add_argument("--from", dest="src", help="source node")
    s.add_argument("--to", dest="dst", help="destination node (of this server)")
    s.add_argument("--every", help="interval, e.g. 30m, 6h, 1d, 1w")
    s.add_argument("--name")
    s.add_argument("--no-update", action="store_true",
                   help="only copy missing files; do not replace older copies with newer ones")
    s.set_defaults(func=cmd_schedule)

    s = sub.add_parser("priority", help="show or set the update priority of a directory")
    s.add_argument("vdir", nargs="?", default="/")
    s.add_argument("value", nargs="?",
                   help="0|1|2|3 or synchronous|frequent|infrequent|archival, or 'inherit'")
    s.set_defaults(func=cmd_priority)

    s = sub.add_parser("ls", help="list a virtual directory")
    s.add_argument("vdir", nargs="?", default="/")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("where", help="show where a file's copies are")
    s.add_argument("vpath")
    s.set_defaults(func=cmd_where)

    s = sub.add_parser("find", help="search files")
    s.add_argument("pattern", nargs="?",
                   help="text in the name, e.g. report (or a glob such as '*.jpg' that must match "
                        "the whole name; with '/' it is matched against the path)")
    s.add_argument("--tag")
    s.add_argument("--node")
    s.add_argument("--issues", action="store_true", help="only non-portable names")
    s.add_argument("--under", action="store_true", help="only files with fewer than 2 good copies")
    s.set_defaults(func=cmd_find)

    for name, action, help_ in (("tag", "add", "tag files"),
                                ("untag", "rm", "remove a tag from files")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("tag")
        s.add_argument("vpaths", nargs="+")
        s.add_argument("-r", "--recursive", action="store_true",
                       help="apply to every file below a directory")
        s.set_defaults(func=cmd_tag, action=action)

    s = sub.add_parser("tags", help="list tags")
    s.set_defaults(func=cmd_tags)

    s = sub.add_parser("decisions", help="list the decision stack")
    s.add_argument("--status", default="open",
                   choices=["open", "resolved", "dismissed", "obsolete"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_decisions)

    s = sub.add_parser("decide", help="resolve a decision")
    s.add_argument("id", type=int)
    s.add_argument("option")
    s.set_defaults(func=cmd_decide)

    s = sub.add_parser("wanted", help="proposed jobs: list, approve, reject")
    s.add_argument("action", nargs="?", default="list", choices=["list", "approve", "reject"])
    s.add_argument("ids", nargs="*", type=int)
    s.add_argument("--all", action="store_true", help="approve/reject every open proposal")
    s.set_defaults(func=cmd_wanted)

    for name, move, help_ in (
        ("copy", False, "copy files to a node"),
        ("move", True, "copy files to a node, then mark the source copies for deletion"),
    ):
        s = sub.add_parser(name, help=help_)
        s.add_argument("vpaths", nargs="*", help="files or directories (recursive)")
        s.add_argument("--tag", help="select every file with this tag")
        s.add_argument("--to", required=True, help="destination node")
        s.add_argument("--from", dest="src", required=move,
                       help="source node" + ("" if move else " (default: any current copy)"))
        s.add_argument("--run", action="store_true", help="run the job right away")
        s.set_defaults(func=cmd_transfer, move=move)

    s = sub.add_parser("jobs", help="jobs: list, show, run, cancel, retry")
    s.add_argument("action", nargs="?", default="list",
                   choices=["list", "show", "run", "cancel", "retry"])
    s.add_argument("ids", nargs="*", type=int)
    s.add_argument("--all", action="store_true",
                   help="list: include finished jobs; show: include done items")
    s.set_defaults(func=cmd_jobs)

    s = sub.add_parser("marked", help="list copies marked for deletion")
    s.add_argument("--node")
    s.set_defaults(func=cmd_marked)

    s = sub.add_parser("unmark", help="remove deletion marks")
    s.add_argument("vpaths", nargs="*")
    s.add_argument("--node", help="only on this node")
    s.add_argument("--all", action="store_true")
    s.set_defaults(func=cmd_unmark)

    s = sub.add_parser("serve", help="run the server: HTTPS API for the client + background work")
    s.add_argument("--listen", help="HOST:PORT (default: [server] listen in config.toml)")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("fingerprint", help="show the TLS certificate fingerprint")
    s.set_defaults(func=cmd_fingerprint)

    s = sub.add_parser("replication",
                       help="copy goal: report, plan, copies N [--tag T], auto on|off")
    s.add_argument("action", nargs="?", default="report",
                   choices=["report", "plan", "auto", "copies"])
    s.add_argument("value", nargs="?", help="on|off (auto) or a number (copies)")
    s.add_argument("--tag", help="with copies: goal for files carrying this tag")
    s.add_argument("--examples", type=int, default=10)
    s.set_defaults(func=cmd_replication)

    s = sub.add_parser("status", help="summary of the index")
    s.set_defaults(func=cmd_status)
    return p


def _use_utf8() -> None:
    """File names are Unicode: make sure Python reads and writes them as UTF-8.

    On a machine whose locale is not UTF-8 (LANG=C, a Latin-1 locale) Python's
    file system encoding would be ASCII or Latin-1, and every CJK name would
    be read as garbage or rejected. Python's UTF-8 mode fixes that but can only
    be chosen at startup, so the command restarts itself once with it. (Not
    needed on Windows, where names are always UTF-16 for Python, nor when
    called from a program or test with explicit arguments.)
    """
    if os.environ.get("REIFS_NO_REEXEC") or sys.flags.utf8_mode:
        return
    if sys.getfilesystemencoding().lower().replace("-", "").replace("_", "") == "utf8":
        return
    os.environ["PYTHONUTF8"] = "1"
    try:
        os.execv(sys.executable, [sys.executable, "-X", "utf8", "-m", "reifs", *sys.argv[1:]])
    except OSError:
        pass   # cannot restart: carry on with what we have


def _robust_streams() -> None:
    """Printing a name the terminal cannot show must not crash the command:
    unsupported characters are written as escapes (\u65e5) instead. A redirected
    output on Windows is UTF-8, not the console's legacy code page."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if os.name == "nt" and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            else:
                stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    if argv is None:        # started as a command, not called by a program
        _use_utf8()
    _robust_streams()
    args = build_parser().parse_args(argv)
    if args.home is None:
        args.home = default_home()
    try:
        args.func(args)
    except (CliError, ConfigError, nodes.NodeError, decisions.DecisionError,
            tags.TagError, jobs.JobError, peers.PeerError, LockError, ValueError,
            OSError, db.DatabaseLocked) as e:
        print(f"reifs: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
