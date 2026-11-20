# rei-fs design

Status: approved. Milestones 1 (index), 2 (jobs), 3 (API and client) and 4
(pairing and sync) are implemented.

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

A job (copy, move, or a batch of approved wanted jobs) is expanded at
creation time into items `(op, vpath, source node, destination node)` stored
in SQLite. Item ops are `copy`, `overwrite` (replace a stale copy) and
`rename` (within one node). Selectors are a vpath, a virtual directory
(recursive) or a tag, and can be combined. Without a source node, any online
node holding the current version is used.

Item states: `pending`, `running`, `done`, `failed`, `skipped`, `conflict`,
`cancelled`. Progress is committed per item, so a run interrupted by Ctrl-C,
a crash or an unplugged drive resumes from the pending items. Items left
`running` by a crash become `pending` again.

* An offline node or a full disk leaves an item pending without using an
  attempt. The job shows as `waiting`.
* Transient failures (rsync error, failed verification, source changed
  during the transfer) use one attempt. After `transfers.max_attempts`
  attempts the item is `failed` until `reifs jobs retry`.
* A source that changed since the last scan is re-hashed and indexed before
  it is sent.
* A destination already holding the same content is recorded without
  copying.

Scans and job runs take a per-server lock, so they never overlap.

### 5.2 Transfers

* rsync copies batches of items listed with `--files-from` (at most
  `transfers.batch_files` files or `transfers.batch_mb` MB per batch). It
  uses `--times` (mtime is the version marker), `--ignore-times` and
  `--no-whole-file`. Partial files go to `<dst>/.rei-fs/partial` and temp
  files to `<dst>/.rei-fs/tmp`, so a half-copied large file resumes and
  nothing is ever left in the user's directories. Permissions and ownership
  are not preserved.
* Only the directories required for the destination path are created.
* After transfer the destination is re-hashed. The item is `done` only if
  the hash matches.
* Before each file, free space on the destination is checked, keeping
  `transfers.min_free_mb` free.
* Renames never replace an existing file. They use a hard link plus unlink
  where supported, otherwise a rename after checking the target is absent.
* **Destination exists with different content**: the item becomes a
  `conflict` decision (overwrite / skip). Nothing is ever overwritten without
  the user's confirmation, except via an explicit "update stale copies"
  decision.
* Server-to-server: the destination's owner pulls from the source server's
  read-only rsync daemon (section 7.3). Credentials are generated at
  pairing. The rsync protocol is not encrypted, so this is intended for a
  LAN or a VPN (e.g. WireGuard, Tailscale). rsync over SSH may be added
  later as an option.
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

### 7.1 The replicated log

* Each server keeps a SQLite database (`reifs.db`) in its home directory.
* The owner of a node is the only writer of facts about that node. Every
  fact is appended to the `changes` log as one of four kinds:
  * `copy`: add/update, missing or removed;
  * `node`: its settings, online state and owner;
  * `tag`;
  * `mark`: deletion mark.

  Each entry carries its **origin server** and a **per-origin sequence
  number**.
* A server pulls each peer's log from a cursor. Entries learned from one
  peer are appended to the local log, so they are passed on to servers that
  never talk to the origin directly (A–B–C: C learns about A through B). An
  entry is applied only once, tracked by `origin_progress`.
* Authority on apply:
  * `copy` and `mark` entries count only if their origin owns the node;
  * a `node` entry counts only from the node's owner;
  * an ownership change wins if its `owner_since` is newer.
* Tags belong to the vpath and follow renames. Each edit carries a
  timestamp and server id, and the newest wins per (file, tag). Tags for a
  vpath not yet known are kept until the file arrives.
* Node names are unique. Another server's node with a clashing name is
  shown as `name@server`.
* Each server raises `divergent` decisions only for stale copies on nodes
  it owns, since it is the one able to update them. `missing` decisions are
  raised by the node's owner. Decisions, wanted jobs and jobs are local to
  each server; the client sees them merged (see 7.4).
* **Propagation timing.**
  * The sync thread pulls every 30 s, or every 5 s while a peer is
    unreachable.
  * It also pulls immediately when poked. A server pokes its peers whenever
    it has new local facts, including facts written by the CLI in another
    process, and after it applied news from another peer.
  * A change typically crosses two hops in about a second.
* **Outbox.** Effects on another server's node are queued and delivered by
  the sync thread, with retries while the peer is unreachable:
  * marking a moved source for deletion;
  * removing a mark;
  * proposing a rename.

  A refusal is final: for example, the owner refuses to mark a copy whose
  content changed since it was copied.

### 7.2 Pairing

* A's administrator enters B's address. A shows B's certificate
  fingerprint to compare with `reifs fingerprint` on B; sending the request
  is acceptance on A's side.
* A's request carries A's id, name, address, fingerprint and a random
  nonce. B lists it as an incoming request.
* B's administrator accepts it (client Servers tab, or `reifs peer
  accept`). B then connects back to A, pinning the fingerprint A sent, and
  completes the handshake with the nonce.
* The two servers exchange API tokens (each stores only a hash of the token
  it issued) and credentials for each other's rsync daemon.
* Peers authenticate with `Authorization: Peer <id>:<token>` and may call
  every admin endpoint, as paired servers obey each other. They cannot call
  login or pairing management.
* Unpairing (from either side) revokes the tokens on both sides when
  reachable.

### 7.3 Transfers between servers

* **The destination's owner runs every transfer into its nodes.**
  * A job targeting another server's node is forwarded to that server, by
    the API or by the CLI.
  * It pulls the files from the source's rsync daemon, verifies the hash and
    records the copy itself.
  * For a move, it asks the source's owner, through the outbox, to mark the
    verified copy for deletion.
* Each server runs `rsync --daemon` while it has at least one peer:
  * one module per online node it owns;
  * a user per paired server;
  * **read-only**, with every delete option refused, so no server can write
    or delete through it;
  * its configuration is rewritten when nodes or pairings change.
* A source is usable if it is attached locally, or if its node is online on
  a paired server that answered the last sync. A transfer therefore needs
  the destination's server to be paired with a server that holds the file.
  Facts relayed through a third server are visible, but files are not
  relayed.

### 7.4 Several servers in the API and client

* A request with `server=<id>` (a query parameter for GET, a body field for
  POST) is forwarded to that paired server.
* `GET decisions|wanted|jobs?everywhere=1` merges the lists of every paired
  server. Each item is labelled with its server, and unreachable servers
  are listed.
* Node settings, scans, deletion marks and new jobs are routed to the
  owning server automatically.
* A storage node owned by another server that is plugged in here appears
  as *foreign* and can be **adopted**. The marker is rewritten and the new
  owner announces itself with a newer `owner_since`; the former owner stops
  managing the node when it syncs.

## 8. Client and API (milestone 3)

### 8.1 Server side

`reifs serve` runs:

* **The HTTPS JSON API** (stdlib `http.server`, one thread per connection,
  with the TLS handshake in that thread). The certificate is self-signed
  (ECDSA P-256), created on first start in `<home>/tls/`.
* **A background worker**: a single thread that performs every scan and
  job run, one at a time, under the server lock. It scans all nodes every
  `scan.interval_minutes`, polls for attached drives every 30 s (scanning a
  storage node when it comes online), and runs pending/waiting jobs every
  minute and right after jobs are created, approved or retried. A stop
  request interrupts the current scan or run between files (progress is
  kept, as with Ctrl-C).

Authentication: `POST /api/login {password}` checks the scrypt hash in the
config and returns a random bearer token (12 h sliding expiry, in memory;
a restarted server forgets tokens and the client logs in again). Failed
logins are delayed by one second.

Endpoints (all JSON, under `/api`):

| Method and path | Purpose |
|---|---|
| `POST login`, `POST logout` | Session |
| `GET status` | Index summary, activity of the worker |
| `GET nodes`, `POST nodes/<name>` | Nodes; set priority / backup target |
| `POST scan {nodes?, verify?}`, `POST stop` | Queue a scan; stop current work |
| `GET tree?path=`, `GET file?path=` | Browse the virtual tree; file details |
| `GET find?q=&tag=&node=&issues=1&under=1` | Search (max 1000 results) |
| `GET tags`, `POST tags {action, tag, paths, recursive}` | Tags |
| `GET decisions`, `POST decisions/<id> {option}` | Decision stack |
| `GET wanted`, `POST wanted {action, ids or all}` | Proposed jobs |
| `GET jobs?all=1`, `GET jobs/<id>`, `POST jobs {kind, paths, tag, to, from}` | Jobs |
| `POST jobs/run`, `POST jobs/<id>/cancel`, `POST jobs/<id>/retry` | Job control |
| `GET marked?node=`, `POST unmark {paths, node, all}` | Deletion marks |
| `POST nodes/<name>/adopt` | Adopt another server's drive attached here |
| `GET peers`, `POST peers/probe`, `POST peers/pair` | Pairing (client only) |
| `POST peers/requests/<id>/accept\|reject`, `POST peers/<id>/unpair`, `POST peers/sync` | Pairing (client only) |
| `GET peer/hello`, `POST peer/pair-request\|pair-confirm\|pair-reject` | Server to server, public |
| `GET peer/changes?since=`, `POST peer/mark\|wanted\|poke\|unpair` | Server to server, peer token |

### 8.2 Client

* C++20, FTXUI 6, cpp-httplib (OpenSSL) and nlohmann_json, built with
  meson/ninja. Dependencies come from the system or from the wraps in
  `client/subprojects/`.
* Trust on first use: on the first connection the client shows the SHA-256
  fingerprint of the server certificate for the user to compare with
  `reifs fingerprint`, then pins it per `host:port`. A changed fingerprint
  is refused with a warning unless the user explicitly trusts the new one.
  The CA chain and host name are not used.
* Tabs: Overview, Tree, Search, Decisions, Wanted, Jobs, Nodes, Marked,
  Servers. A background thread polls `GET status` every 2 s for the status
  bar and the live Jobs view. Decisions, Wanted and Jobs show every paired
  server's items.

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
advertise = ""                # HOST:PORT peers use to reach us ("" = detect)

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
daemon_port = 7421            # read-only rsync daemon for paired servers
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
