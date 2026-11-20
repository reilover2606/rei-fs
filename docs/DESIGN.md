# rei-fs design

Status: draft for sign-off. Milestone 1 is implemented against this document.

## 1. Goals

rei-fs is a decentralized file and backup manager.

* All storage shares one **virtual tree**. A file lives at the same virtual
  path (e.g. `/documents/1.txt`) on every node that holds a copy.
* Nodes only physically contain the directories needed for the files they
  actually hold.
* The backup goal: every file, **in its current version**, is present on at
  least **two** nodes.
* Servers keep metadata (virtual path, copies, size, mtime, hash, tags) and
  move data with `rsync`. Servers never open files for the user and **never
  delete files**. Only the user deletes files.

Expected scale: up to several tens of thousands of files, fewer than a dozen
nodes. SQLite on each server is more than enough.

## 2. Terminology

| Term | Meaning |
|---|---|
| **Node** | A storage location with a root directory holding (part of) the virtual tree. |
| **Server node** | A node running the rei-fs server (Python). Its own storage root is a node. |
| **Storage node** | A non-server node: flash drive, external SSD, second disk. Owned by exactly one server. |
| **Owner** | The server responsible for a node. A server owns itself and its storage nodes. Only the owner scans a node and publishes facts about it. |
| **Virtual path (vpath)** | `/`-separated path relative to a node root, always starting with `/`. |
| **File** | A virtual path that has at least one tracked copy. |
| **Copy** | A physical instance of a file on a specific node, with its size, mtime and hash. |
| **Current version** | The hash of the copy with the newest mtime. |
| **Decision** | Something the user must resolve in the client (the "decision stack"). |
| **Wanted job** | A job the server proposes (e.g. to reach two copies) that the user can approve or reject. |

Android devices run the server in Termux and are therefore server nodes.

## 3. Nodes

### 3.1 Node identity

Every node root contains a marker:

```
<root>/.rei-fs/node.json
{ "node_id": "<uuid>", "name": "black-ssd", "kind": "storage",
  "owner_id": "<server uuid>", "created": "2026-11-20T18:00:00Z" }
```

Nodes are identified by `node_id`, never by mount path or drive letter.
`.rei-fs/` also holds scratch space (rsync partial dir, probe files).

### 3.2 Discovery of storage nodes

On every scan (and periodically) the owner looks for markers:

1. at the node's last known path;
2. under the configured discovery paths (default: `/media/<user>/*`,
   `/run/media/<user>/*`, `/mnt/*` on Linux; every drive letter on Windows),
   searching up to `discovery.max_depth` levels (default 3) below each base
   (`/storage/*` is also searched on Android/Termux).

A node whose marker is not found is **offline**. An offline node is never
scanned, so unplugging a drive never marks its files missing. A root that
exists but has no marker (e.g. an empty mount point) is also treated as
offline.

### 3.3 Foreign nodes

If a server finds a marker whose `owner_id` is another server, it records the
node as *present but foreign* and leaves it alone. The user can **transfer
ownership** from the client, which rewrites the marker and notifies the old
owner via metadata sync.

### 3.4 Filesystem capabilities

When a node is registered (and each time it is mounted), the owner probes its
filesystem by creating test files in `.rei-fs/`:

* `case_sensitive`: does `probe` differ from `PROBE`?
* `restricted_names`: can a name containing `:` be created? (No for
  NTFS/FAT/exFAT and any Windows host.)

These flags decide which files can legally be copied to the node (section 5.4).

## 4. Scanning

A scan walks a node root and reconciles it with the database.

* **Ignored**: symlinks (and Windows junctions); hidden directories (names
  starting with `.`, which includes `.rei-fs/`); names matching the
  configurable ignore list. The default list is `Thumbs.db`, `desktop.ini`,
  `.DS_Store`, `~$*`, `*.tmp`, `$RECYCLE.BIN`, `System Volume Information`
  and `lost+found`. Hidden *files* are not ignored unless the list says so.
  Patterns without `/` match a name. Patterns containing `/` match a vpath.
* **Hashing**: BLAKE2b-256 from the standard library, stored as
  `blake2b:<hex>`. It is fast on both x86 and ARM and needs no compiled
  dependency, which matters for Termux. A file is re-hashed only when its
  size or mtime changed (or on an explicit `--verify`).
* **Unreadable directories** are recorded as scan errors. Copies below them
  are left untouched, never marked missing.
* **Atomicity**: new and changed copies are committed in batches as the walk
  progresses. Vanished copies are processed only after the walk completed
  without being interrupted, so an aborted scan never produces false
  "missing" results.
* **Empty directories** are ignored. The user can create empty directories
  freely and the server neither tracks nor removes them. The server only
  prunes a directory when **a tracked file vanished from it** and the
  directory is now completely empty. It then walks up while parents are
  empty. Pruning uses `rmdir`, which cannot remove anything non-empty.
  Virtual directories are never stored; they exist only as prefixes of file
  vpaths, so a virtual directory disappears when its last file does.
* Triggers: on request (client/CLI), on a timer (`scan.interval_minutes`),
  and when a storage node comes online. Live filesystem watching may come
  later.

### 4.1 Vanished copies (decision: `missing`)

When a tracked copy vanishes from an online node, it is marked `missing` and
a decision is pushed. Its options are:

* **Lost: restore it.** Creates a wanted job to copy the current version back
  from another node.
* **Removed from this node only.** Forgets the copy. Replication policy may
  later propose a copy elsewhere if the file drops below two copies.
* **Deleted from the system.** Forgets the copy and marks every other copy
  *for deletion* (section 5.3). The file is excluded from replication.

If the vanished copy was the last one, only "acknowledge" is offered and the
file is forgotten.

### 4.2 Renames (decision: `rename`)

Within one scan of a node, a vanished copy at `P` paired with a new file at
`Q` that has the same hash and size is treated as a rename. Pairs whose
basenames match are preferred.

* Tags follow the rename.
* If no other node has `P`, the file is simply re-keyed from `P` to `Q`.
* If other nodes still hold `P`, a `rename` decision offers to apply the
  same rename on those nodes. This is never done automatically. Until the
  user decides, `P` and `Q` are separate files and `Q` keeps a copy of `P`'s
  tags.

### 4.3 Diverging versions (decision: `divergent`)

When copies of the same vpath have different hashes, the copy with the
newest mtime defines the current version. Copies with another hash are
**stale** and do not count towards the two-copy goal. A `divergent`
decision is pushed. Its options are:

* **Update stale copies.** Creates a job that overwrites the stale copies
  with the current version.
* **Keep as is.** Dismissed until any of the copies changes again.

The server never overwrites automatically.

### 4.4 Non-portable names (flag: `portability`)

A vpath is flagged non-portable when any component:

* contains `< > : " \ | ? *` or control characters;
* is a reserved Windows device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`,
  `LPT1-9`, with or without extension);
* ends with a dot or a space;
* is longer than 255 bytes.

A vpath is also flagged when it collides case-insensitively with another
vpath. Flags are shown in the client. Flagged files are still fully managed
and can be copied to any node whose capabilities allow them. A copy to an
incompatible node is refused for that file and reported, not silently
skipped.

## 5. Jobs and transfers (milestone 2)

### 5.1 Jobs

A job (copy, move, verify, overwrite-stale) is expanded at creation time
into a list of `(vpath, source node, destination node)` items stored in
SQLite. Selectors are a vpath, a virtual directory (recursive) or a tag. Each
item has a state: `pending`, `running`, `done`, `failed`, `skipped`, or
`conflict`. A failed or interrupted job resumes from its pending items when
both nodes are reachable again. Retries use backoff.

### 5.2 Transfers

* rsync copies one file per item (or batches with `--files-from`), using
  `--partial-dir=.rei-fs/partial` so a large file resumes mid-way, and
  `--times` to preserve mtime. Permissions and ownership are not preserved.
* Only the directories required for the destination path are created.
* After transfer the destination is re-hashed. The item is `done` only if
  the hash matches.
* Before a job starts, free space on the destination is checked.
* **Destination exists with different content**: the item becomes a
  `conflict` decision (overwrite / skip). Nothing is ever overwritten without
  the user's confirmation, except via an explicit "update stale copies"
  decision.
* Server-to-server: each server runs `rsync --daemon` itself on a dedicated
  port, with a module per owned node and credentials generated at pairing.
  Intended for a LAN or a VPN (e.g. WireGuard, Tailscale). rsync over SSH may
  be added later as an option.
* On Windows the rsync binary (cwRsync, MSYS2, ...) is set in the config.
  rei-fs translates paths to the `/cygdrive/c/...` form as needed.

### 5.3 Move = copy + mark for deletion

A move copies and verifies, then marks the source copy **for deletion**. The
client lists marked copies. The user deletes them manually (the next scan
notices the deletion and clears the mark without raising a `missing`
decision) or removes the mark. Copies marked for deletion do not count
towards the two-copy goal.

### 5.4 Legal destinations

Before copying, each item is checked against the destination's
capabilities. Restricted-name nodes reject non-portable names. Case-
insensitive nodes reject vpaths that would collide with an existing file
there.

## 6. Replication (milestone 5)

A file is **satisfied** when at least two *online or offline* nodes hold a
copy of the current version that is not marked for deletion. For every
unsatisfied file the server proposes a **wanted job**, picking the
destination by node priority and then free space, and skipping nodes that are
excluded as replication targets or incompatible.

* Default mode: wanted jobs appear in the client for the user to approve or
  reject one by one or in bulk.
* Optional **auto-replicate** mode (per server): wanted jobs are approved
  automatically.

## 7. Metadata and server-to-server sync (milestone 4)

* Each server keeps a SQLite database (`reifs.db`) in its home directory.
* The owner of a node is the only writer of facts about that node's copies.
  Every change is appended to a `changes` log with a monotonic sequence
  number.
* Paired servers pull each other's logs from their last seen sequence
  number, so propagation works whenever servers can talk and catches up
  after downtime. Changes from a peer are forwarded as well, so a server
  learns about nodes it never talks to directly.
* Tags belong to the vpath and follow renames. Tag edits (add/remove) carry
  a timestamp and server id, and the last writer wins per (file, tag).
* Decisions about a node are owned by the node's owner. Resolving one from
  another server forwards the resolution to the owner.

### 7.1 Pairing

Server A sends a pairing request to B (address entered manually or found via
mDNS on the LAN). The user must accept it in the client on **both** sides.
After that, the servers exchange rsync credentials and API tokens. Paired
servers obey each other's send/receive requests without further prompts.

## 8. Client (milestone 3)

* C++20, TUI with FTXUI, built with meson/ninja. Dependencies come from
  WrapDB (ftxui, nlohmann_json, cpp-httplib with OpenSSL).
* Connects to one server at a time over HTTPS (JSON API). The server has a
  self-signed certificate that the client pins the first time it connects.
* There is a single admin user. The password is stored in the server config
  as an scrypt hash and is the same on all servers.
* Views:
  * virtual tree browser (copies per file, flags);
  * search by name, tag, size and date;
  * file details (locations, versions, tags);
  * decision stack;
  * wanted jobs;
  * jobs with progress, resume and cancel;
  * nodes;
  * pairing requests;
  * copies marked for deletion.

## 9. Server configuration

Server home: `$REIFS_HOME`, otherwise `%LOCALAPPDATA%\rei-fs` on Windows or
`$XDG_CONFIG_HOME/rei-fs` (default `~/.config/rei-fs`) elsewhere. It contains
`config.toml`, `reifs.db` and logs.

```toml
[server]
name = "desktop"
listen = "0.0.0.0:7420"
password_hash = ""            # set with `reifs passwd`
auto_replicate = false

[storage]
root = "/home/me/rei"         # this server's own node root

[scan]
interval_minutes = 60
ignore_hidden_dirs = true
ignore = ["Thumbs.db", "desktop.ini", ".DS_Store", "~$*", "*.tmp",
          "$RECYCLE.BIN", "System Volume Information", "lost+found"]

[discovery]
paths = []                    # empty = platform defaults
max_depth = 3

[rsync]
path = "rsync"
```

## 10. Milestones

1. **Index**: server home, config, database, server node and storage nodes,
   discovery, scanning, hashing, decisions (missing / rename / divergent),
   portability flags, tags, and a CLI (`reifs`) to drive it all.
2. **Jobs**: copy/move by path, directory or tag; resumable rsync transfers;
   marks for deletion; conflicts.
3. **Client**: HTTPS API on the server; FTXUI client.
4. **Pairing and sync**: pairing, change-log replication, remote transfers.
5. **Replication**: two-copy reports, wanted jobs, auto-replicate.
