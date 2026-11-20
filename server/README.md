# rei-fs server

Python server for rei-fs. See [../docs/DESIGN.md](../docs/DESIGN.md).

Requires Python 3.11+ and no third-party packages at runtime.

```bash
uv sync                      # or: pip install -e .
uv run reifs init --name desktop --root ~/rei
uv run reifs passwd
uv run reifs scan
uv run reifs status
```

Run the tests with `uv run pytest`.

## Commands (milestone 1)

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
| `reifs wanted` | Proposed jobs (executed from milestone 2 on) |
| `reifs status` | Summary: copies, divergent, missing, non-portable... |

The server home defaults to `~/.config/rei-fs` (Linux) or
`%LOCALAPPDATA%\rei-fs` (Windows). Override it with `--home` or `REIFS_HOME`.
