# rei-fs server

Python server for rei-fs. See [../docs/DESIGN.md](../docs/DESIGN.md).

Requires Python 3.11+ and the `cryptography` package. Transfers need
rsync 3.1+ (on Windows: cwRsync or MSYS2, set `[rsync] path` and `path_style`).

```bash
uv sync                      # or: pip install -e .
uv run reifs init --name desktop --root ~/rei
uv run reifs passwd
uv run reifs scan
uv run reifs serve           # HTTPS API for the client + background work
```

`reifs serve` listens on `[server] listen` (default `0.0.0.0:7420`). It
creates a self-signed certificate on first start and prints its
fingerprint (also shown by `reifs fingerprint`). It also scans every
`scan.interval_minutes`, scans drives when they are plugged in, and runs
jobs in the background. When servers are paired, `serve` also syncs metadata with them and runs a
read-only rsync daemon on `[rsync] daemon_port` (default 7421) from which
paired servers pull files. Open both ports between your servers (LAN or
VPN). For servers not on the same network, use rsync over SSH instead:

1. On the server that serves the files, set `[ssh] enabled = true`.
2. Once the servers have synced, run `reifs ssh authorize --apply` there.
3. On the server that pulls, run `reifs peer transport NAME ssh`.

Only that server's SSH port then needs to be reachable. Certificates are made with the `cryptography`
package. On Termux, install it with `pkg install python-cryptography` (or
have the `openssl` command available).

Run the tests with `uv run pytest`.

## Commands

| Command | Purpose |
|---|---|
| `reifs init --name N --root DIR` | Create the server home and this server's node |
| `reifs passwd` | Set the admin password (used by the client) |
| `reifs node add PATH --name N` | Register a storage node (drive/folder) owned by this server |
| `reifs node list` / `node set` | List nodes; set priority, replication target, name |
| `reifs discover` | Find which owned nodes are attached right now |
| `reifs scan [NODE...] [--verify]` | Scan online nodes for changes |
| `reifs ls [VDIR]` | List a virtual directory |
| `reifs where VPATH` | Show which nodes hold a file, and which copies are current |
| `reifs find [GLOB] [--tag T] [--node N] [--issues] [--under]` | Search |
| `reifs tag T VPATH... [-r]` / `untag` / `tags` | Tagging |
| `reifs decisions` / `reifs decide ID OPTION` | The decision stack |
| `reifs copy VPATH... [--tag T] --to N [--from N] [--run]` | Copy files (minimal directories created) |
| `reifs move VPATH... [--tag T] --from N --to N [--run]` | Copy, verify, then mark the source copies for deletion |
| `reifs jobs [list\|show\|run\|cancel\|retry] [ID...]` | Manage and run jobs (resumable) |
| `reifs wanted [list\|approve\|reject] [ID...\|--all]` | Proposed jobs from decisions |
| `reifs marked [--node N]` / `reifs unmark VPATH... \| --all` | Copies marked for deletion |
| `reifs status` | Summary: copies, divergent, missing, non-portable... |
| `reifs replication [plan\|copies N [--tag T]\|auto on\|auto off]` | Copy-goal report; propose missing copies; set the goal; automatic approval |
| `reifs peer transport NAME daemon\|ssh` | Pull from that server with its rsync daemon or over SSH |
| `reifs ssh key` / `reifs ssh authorize [--apply]` | This server's SSH key; authorize paired servers (read-only via rrsync) |
| `reifs serve [--listen HOST:PORT]` | Run the API for the client, the background worker and sync |
| `reifs peer pair HOST[:PORT]` | Ask another server to pair (shows its fingerprint first) |
| `reifs peer [list\|accept ID\|reject ID\|remove NAME\|sync]` | Pairing requests and paired servers |
| `reifs node adopt NAME\|PATH` | Take ownership of another server's drive plugged in here |
| `reifs fingerprint` | Show the TLS certificate fingerprint |

The server home defaults to `~/.config/rei-fs` (Linux) or
`%LOCALAPPDATA%\rei-fs` (Windows). Override it with `--home` or `REIFS_HOME`.
