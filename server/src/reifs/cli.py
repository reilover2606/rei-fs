"""``reifs`` command-line interface (administration and testing).

The FTXUI client (milestone 3) talks to the server over its API; this CLI
works directly on the local server home.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import (__version__, auth, db, decisions, index, jobs, nodes, peers, queries,
               replication, runner, scanner, tags)
from .config import ConfigError, default_home
from .hashing import short
from .locking import LockError, ServerLock
from .paths import to_physical
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
    updates = {}
    if args.priority is not None:
        updates["priority"] = args.priority
    if args.target is not None:
        updates["replication_target"] = int(args.target == "yes")
    if args.rename:
        updates["name"] = args.rename
    if not updates:
        raise CliError("nothing to change")
    sets = ", ".join(f"{k} = ?" for k in updates)
    if node["owner_id"] != nodes.self_id(conn):
        raise CliError(f"node {node['name']!r} belongs to another server; change it there "
                       "(or from the client)")
    with db.transaction(conn):
        conn.execute(f"UPDATE nodes SET {sets} WHERE id = ?", (*updates.values(), node["id"]))
        index.log_node(conn, node["id"])
    print(f"Updated node {node['name']!r}.")


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


def cmd_scan(args) -> None:
    cfg, conn = open_server(args.home)

    def progress(stats: scanner.ScanStats, current: str) -> None:
        if sys.stderr.isatty() and current:
            line = f"  {stats.seen} files, {stats.hashed} hashed: {current}"
            print("\r" + line[:110].ljust(110), end="", file=sys.stderr, flush=True)

    with ServerLock(cfg.home):
        disc, results = scanner.scan_all(conn, cfg, args.nodes or None, verify=args.verify,
                                         progress=progress)
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
                             ("dirs pruned", s.pruned_dirs), ("errors", len(res.errors))):
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
    results = queries.find(conn, args.pattern, tag=args.tag, node=args.node,
                           issues_only=args.issues, under_replicated=args.under,
                           policy=replication.policy(cfg))
    rows = [[f.vpath, human_size(f.size), copies_badge(f),
             ",".join(c.node_name for c in f.copies if c.state == "present"),
             " ".join(f.tags)] for f in results]
    print(table(rows, ["PATH", "SIZE", "COPIES", "NODES", "TAGS"]))
    print(f"{len(results)} file(s)")


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
        print(f"#{d.id} [{d.kind}] {d.vpath or d.data.get('vpath', '?')}{where}")
        if d.kind == "rename":
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
        " LEFT JOIN files f ON f.id = w.file_id LEFT JOIN nodes s ON s.id = w.src_node"
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
    q = "SELECT * FROM jobs"
    if not args.all:
        q += " WHERE status NOT IN ('done', 'cancelled')"
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
    print(f"  created {j['created']}" + (f", started {j['started']}" if j["started"] else "")
          + (f", finished {j['finished']}" if j["finished"] else ""))
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


def cmd_serve(args) -> None:
    import logging

    from . import api, tls
    from .service import Syncer, Worker

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg, conn = open_server(args.home)
    conn.close()
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
    import signal

    def on_term(signum, frame):  # shut down cleanly (stops the rsync daemon too)
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, on_term)
    except (ValueError, OSError):
        pass
    worker.start()
    syncer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down (finishing the current file)...", file=sys.stderr)
    finally:
        server.server_close()
        worker.close()
        syncer.close()


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
             (p["last_sync"] or "")[:19], p["last_error"] or ""] for p in peers.paired_peers(conn)]
    print(table(rows, ["SERVER", "ADDRESS", "STATE", "LAST SYNC", "LAST ERROR"])
          if rows else "Not paired with any server.")
    reqs = conn.execute("SELECT * FROM pair_requests WHERE status = 'pending' ORDER BY id"
                        ).fetchall()
    if reqs:
        print("\nPending pairing requests:")
        print(table([[str(r["id"]), "incoming" if r["direction"] == "in" else "outgoing",
                      r["name"], r["address"], r["fingerprint"][:23] + "..."] for r in reqs],
                    ["ID", "DIRECTION", "SERVER", "ADDRESS", "FINGERPRINT"]))
        print("Accept an incoming request with 'reifs peer accept ID'.")


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
    print(f"last plan ({report.finished[:19]}):")
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
    s.add_argument("--rename", metavar="NAME")
    s.set_defaults(func=cmd_node_set)

    s = node.add_parser("adopt", help="take ownership of another server's drive attached here")
    s.add_argument("node", help="name (see 'reifs discover') or path of the drive's node root")
    s.set_defaults(func=cmd_node_adopt)

    s = sub.add_parser("peer", help="other servers: list, pair, accept, reject, remove, sync")
    s.add_argument("action", nargs="?", default="list",
                   choices=["list", "pair", "accept", "reject", "remove", "sync"])
    s.add_argument("target", nargs="?", help="address (pair), request id, or server name")
    s.add_argument("-y", "--yes", action="store_true", help="pair without asking")
    s.set_defaults(func=cmd_peer)

    s = sub.add_parser("discover", help="find which owned nodes are attached")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("scan", help="scan online owned nodes for changes")
    s.add_argument("nodes", nargs="*", help="node names (default: all online)")
    s.add_argument("--verify", action="store_true", help="re-hash every file")
    s.add_argument("--max-errors", type=int, default=20)
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("ls", help="list a virtual directory")
    s.add_argument("vdir", nargs="?", default="/")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("where", help="show where a file's copies are")
    s.add_argument("vpath")
    s.set_defaults(func=cmd_where)

    s = sub.add_parser("find", help="search files")
    s.add_argument("pattern", nargs="?", help="glob on the name (or on the path if it has '/')")
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.home is None:
        args.home = default_home()
    try:
        args.func(args)
    except (CliError, ConfigError, nodes.NodeError, decisions.DecisionError,
            tags.TagError, jobs.JobError, peers.PeerError, LockError, ValueError,
            OSError) as e:
        print(f"reifs: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
