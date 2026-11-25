# rei-fs design

Status: approved. All five milestones are implemented: index, jobs, API and
client, pairing and sync, replication. Also implemented: a configurable copy
goal, rsync over SSH, and CI on Linux and Windows.

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

### 4.2b Unicode names

Names are Unicode strings, stored and compared exactly (no normalisation, so
NFC and NFD spellings are different files). A name that is not valid UTF-8
cannot be stored and is skipped with an error. Case-insensitive comparisons use
Unicode case folding. The 255 limit counts UTF-8 bytes, which is the strictest
of the common file systems. To read names correctly whatever the locale, the
`reifs` command restarts itself in Python's UTF-8 mode when the file system
encoding is not UTF-8, and its output never fails on a character the terminal
cannot show. rsync receives names as bytes (`--files-from` with NUL separators,
UTF-8); the path of a node on a remote server is backslash-escaped for the
remote shell when the client's rsync does not do it itself (before 3.2.4).

### 4.5 Update priority

How often a directory changes differs: documents being edited all day, and
archives that never change. The user sets an **update priority** per directory
(`POST priority {path, priority}`; `reifs priority`; `u` in the Tree) and it
applies to everything below, unless a subdirectory has its own value; the
nearest setting at or above a path wins and the default is 2. It is replicated
like a tag (last writer wins, `inherit` is a tombstone) and follows a directory
when it is moved (5.5).

| value | meaning | automatic scan every |
|---|---|---|
| 0 | synchronous: for programs saving continuously (the API for them comes later) | `scan.synchronous_minutes` (1) |
| 1 | frequent | `scan.frequent_minutes` (10) |
| 2 | infrequent (default) | `scan.interval_minutes` (60) |
| 3 | archival: never changes | `scan.archival_minutes` (0 = only the first time and on request) |

Automatic scans (the timer, a drive coming online, `reifs scan --due`) only visit
the directories whose class is due, per node (`scan_state`); a directory not due
is not even read, and a node with nothing due costs nothing. Files in directories
a scan did not visit are never reported missing. A scan the user asks for, from the
client or `reifs scan`, always covers everything, which is also how changes in an
archive are noticed. A **new-files-only** scan (`reifs scan --new-only`, `w` in the client) just discovers
files the index does not know: nothing indexed is read again, changes are not noticed and
nothing is reported missing or renamed, so it is the cheapest scan. Nested settings work: an archive can hold a frequent folder.
`interval_minutes = 0` still turns all automatic scans off. The same classes will
decide which directories live scanning watches.

### 4.6 Decisions that belong together

A deleted or moved folder with ten thousand files must not become ten thousand
decisions. After each scan, the decisions it raised are gathered under one **group**
decision per folder (`[scan] group_decisions_min`, default 5; 0 turns it off). How the
files are told to belong together is not guessed from names, it comes from what the scan
saw on the disk:

* **missing**: for each vanished file the scan looks for the topmost folder above it that no
  longer exists on that node. Everything under the same vanished folder is one group
  (`/jar` gone, however deep the files). Files whose folder still exists are grouped by that
  folder, so deleting most of one folder's files is also one decision.
* **rename**: the files of one moved folder all show the same pair of prefixes once the
  trailing path they share is removed (`/a/b/x` -> `/n/b/x` and `/a/c/y` -> `/n/c/y` both
  say `/a` -> `/n`), so a moved folder, subfolders included, is one decision.

The members stay in the database but are not listed or counted: the list shows the group, with
a sample of its paths. One choice is applied to all members, a few hundred per transaction
so the database is never held for long; one that cannot take that choice (restoring a file
that had no other copy) leaves the group and stays open on its own. Later findings for the
same folder join the open group, and a group whose members were all settled meanwhile
closes by itself.

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
* Server-to-server: the destination's owner pulls from the source server,
  either from its read-only rsync daemon (unencrypted; for a LAN or a VPN
  such as WireGuard or Tailscale) or over SSH (section 7.3).
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

### 5.5 Moving within the virtual tree

The user can move files and directories to another place in the virtual tree
(`POST vmove {paths, to, name?, on_conflict, dry_run}`). The *vpath* of the
files changes, so every node holding a copy renames it: the plan is made from
the replicated index, nodes of this server get a job of `rename` items, and the
server owning other nodes is asked (`POST jobs {kind: "rename", node, renames}`)
to rename on its own. Nothing is copied or deleted. Tags follow the move, an
offline node renames when it comes back, and a rename that cannot be done (the
target exists on disk, the file changed, a name the node cannot hold) is
reported by the job and left as it was.

When the destination already holds something with the same name the user picks
(`dry_run` tells the client whether to ask):

* **merge** (default). Directories are combined (they are only prefixes). A file
  meeting a file of the same name becomes *one* file with the copies of both.
  Identical copies on one node are redundant: the moved one is marked for
  deletion, never deleted. Different content makes two versions of one file, the
  newest wins and the usual `divergent` decision offers to update the stale
  copies. If one node holds both files with different content they cannot be one
  file there, so the moved file keeps its own name with `(2)` added.
* **preserve**. Nothing is mixed: the moved item takes a free name (`photos (2)`,
  `a (2).txt`).

A file moved onto a directory name (or the reverse), and two moved items with the
same name, always get a free name. Moving a directory into itself is refused.

### 5.6 Directories made by the user

A directory normally exists only as the prefix of file paths, so an empty one
cannot exist. The user can also create directories (`POST mkdir {path}`), which
are recorded in the index (table `vdirs`) and replicated like tags: a
last-writer-wins record with tombstones, so every server shows them. They are
purely virtual: nothing is created on any node, and they stay when empty or when
their files are moved away. `POST rmdir {path}` removes an *empty* directory
(and the empty ones made inside it); a directory holding files is refused, so
nothing is ever deleted through it. Renaming or moving a directory (5.5) carries
the directories made inside it along at once, and an existing empty directory is
a name that merge combines with and preserve avoids.

### 5.7 Never "done" when it is not

* A job is `done` only when **every** item is done (or the user skipped it on
  purpose). Items that could not be done (`failed`, `skipped`, `conflict`) keep it
  `failed`/`waiting`, it stays in the job list, and the client shows "N NOT done"
  with each item and its reason. `retry` re-queues failed *and* skipped items.
* Files a copy left out when the job was created (no present copy anywhere, not on the
  source node, not the current version, ...) are kept as skipped items, not just
  counted in a reply. Files already on the destination are not an issue.
* When a job ends, every file it says it copied is looked at again on the
  destination (it exists, with the recorded size). One that is gone goes back to
  pending and the job says so.
* A job only knows indexed files. `reifs node audit NODE` (`c` in the Nodes tab)
  walks a node's folder without reading file contents and says what is on disk but not
  in the index, with the reason (hidden directory, `[scan] ignore` pattern, symbolic
  link, name not valid UTF-8, another node's folder, not scanned yet, ...), and what
  the index lists that is no longer on disk.

### 5.8 Keeping the client responsive

Lists that grow with use (decisions, deletion marks, proposals) are never sent whole: the
client asks for a page (`limit`) and gets `total` too, and says "showing N of M". The counting for a
directory listing is done by SQLite, and a paired server already known to be unreachable is not
asked again by every refresh. Everything a refresh costs should depend on what is on screen,
not on how long the server has been in use.

While a job runs the change log grows with every file, which used to invalidate the
backup-coverage figures on every status poll and recompute them over every copy
(hundreds of milliseconds, with the GIL held, so the whole API crawled). The status line now
accepts figures a few seconds old, one computation runs at a time, and the counters it
reads have indexes. The client keeps its clock ticking in its own thread, so a slow
status request cannot freeze the display, gives up on a status request after 10 s, and on
quitting interrupts whatever request is in flight instead of waiting for the server.

### 5.9 Millions of files

Measured on a synthetic index of 2.5 million files in four nodes (3.6 million copies). Every
request must cost what is on screen, not the size of the index:

* **Coverage figures** (status line, node totals, backup counts) are one SQL aggregation (about
  2 s) instead of a Python dict per file (64 s and gigabytes). They are cached by the last
  change-log sequence number and, on a database where computing takes more than 0.3 s, refreshed
  in the background: a request is answered with figures a few seconds old, never waits for them.
* **Planning backups** looks at one file at a time (indexed queries when the file comes up),
  never preloads tables: 25 s and 110 MB instead of minutes and gigabytes. At most
  `[replication] max_proposals` (10000) proposals are open and `max_examined` (100000) files are
  looked at per run; the rest is planned as these are decided. It runs at most every ten times
  its own duration.
* **Directory listings** walk the index by jumping (the cost is the entries listed, not the files
  below them), list at most 2000 files of one folder and say how many more there are, and count
  "has a copy on this server's node" up to 10001 files. Slow listings may be a few seconds old
  while background work changes the index; the user's own actions refresh them at once.
* **Search** cuts candidates down in SQL with the longest literal part of the pattern, checks the
  exact match on those, and caches the per-folder counts.
* **Pairing servers that already share a partner** (a triangle): a new pair pulls the other's whole
  log, which for a big index is millions of entries, nearly all already received through the
  common partner. The asker now says what it holds per origin server (`have=origin:seq,...`) and
  the peer sends only the rest, so closing a loop transfers almost nothing. A long pull is
  done in slices of 20 s per peer per round, the peer counts as reachable from its first
  answer ("catching up: N entries" is shown), and the Servers tab shows why a server is unreachable.
* **Short write transactions**: SQLite allows one writer at a time and the others wait up to 30
  seconds. Approving thousands of proposals and applying a peer's changes are done in transactions
  of a few hundred rows (what an item needs is read before the lock is taken), so no writer holds
  the lock long enough to starve the others. The sync thread and the worker survive a round that
  fails (a lock that did not clear, a bad answer from a peer) and try again: before, one failure
  ended the sync thread for good and the servers silently stopped syncing.
* **Temporary files**: big sorts and groupings spill to disk. SQLite's default folder (`/tmp`, often
  a small RAM disk) made "database or disk is full" on servers with plenty of space; they now go
  to `<server home>/tmp` (or `SQLITE_TMPDIR`).

### 5.10 Fetching, and passing files through other servers

**Fetch** (`POST jobs {kind: "fetch", paths|tag}`, `f` in the Tree and Search) downloads files, or
everything below a folder, to this machine: into `<this server's node>/fetch/<path>` (`[storage]
fetch_dir`). It is a plain download. Nothing is indexed (the scanner skips that folder, unless
the node already tracked files of the user's under that name), nothing is marked for deletion,
and an existing file with other content in the way is never replaced (the item is skipped
and says so). The source is any node that can be read, including other servers' nodes
and, below, files passed through other servers.

**Relaying.** A server may only pull from servers it is paired with. If the file lives on a
node of server C that A is not paired with, but both A and C are paired with B (or a longer
chain, up to three hops), B passes it on:

1. A asks its paired servers whether they can reach C (`GET peer/route`, which asks onwards
   with the list of servers already asked, so loops of pairings end).
2. A sends B a request (`POST peer/relay-request {id, node, path, hash}`). B turns it into an
   ordinary job item (`fetch = 2`): B pulls the file from C (or asks further) into its scratch
   area `<home>/relay/<id>/`, verifying the hash, and the request becomes `ready` (or `failed`
   with the reason).
3. A polls (`GET peer/relay-status`), and when it is ready pulls from B's read-only `relay`
   rsync module, verifies the hash again, and tells B (`POST peer/relay-done`), which removes its
   copy. Unclaimed staged files are removed after six hours.

A relay needs the rsync daemon transport between each pair of servers on the way. While it is
in progress the item waits with a message saying where the file is. If there is no route, the
message says so.

### 5.11 Scheduled syncs

A **schedule** (`POST schedules`, `reifs schedule`, the Schedules tab, `S` on a directory in the
Tree) says: every N minutes make the files under a directory of node A also be on node B, to keep
backups up to date. It is run by the server that owns the destination node (a request for another
server's node is forwarded there), which pulls as for any transfer. Each run creates an ordinary
job:

* files the destination lacks (or whose copy is gone) are **copied**;
* files whose destination copy is an *older version* of the source's current one are
  **replaced** (`update_changed`, on by default), only the exact content seen at planning;
* a destination copy that is newer than the source's, or that cannot be judged, is left alone and
  counted in the note ("N left alone");
* nothing is ever deleted at the destination, and deletions at the source are not propagated.

A run is due when its `next_due` time has passed (the first one at once, then every interval after
the last run). The worker looks every 15 seconds, and at once when a node comes online. If a node
cannot be used at that moment (a drive unplugged, a computer asleep, the other server unreachable)
no job is created, the schedule says what it waits for, and it stays due: it runs as soon as both
nodes are back, **once**, however many intervals were missed. A new run is not started while the
previous job is still going.

**Renames.** A schedule names its directory by path. When a directory is renamed or moved in the
virtual tree (5.5) the schedules for it, and for directories inside it, follow it, and the
servers paired with this one are told (`POST peer/dir-moved`), because a schedule lives on the
server owning the backup node, which may not be the one doing the move. A directory renamed
*outside* rei-fs (on a disk) is not noticed: its next run finds nothing under the path and says so
in the schedule's note, and `p` in the Schedules tab (`reifs schedule path ID DIR`) points it at the
new place. A very large directory is synced in pieces of 50,000 files, the next
piece a minute later.

### 5.12 Network figures in the client

The Overview ends with a **Network** section (the Overview scrolls with the arrow keys). It shows how
many files meet their replication goal as a gauge and a percentage, the files by number of good
copies (0, 1, 2, 3, 4+) as bars, and where the copies are: for each node its share of all
copies by file count and, in proportion, by size, as two stacked bars (one colour per node) and per-node
gauges. The figures are the cached coverage figures of §5.9 (`GET network`). The Nodes tab
lists each server (this one first) with the nodes it owns branching beneath it; a scan key on a
server's row covers all its nodes.

### 5.13 A smaller database

Measured on 400,000 files and 600,000 copies (paths of 76 characters): 584 MB before, 280 MB after,
which is about 2.8 GB instead of 5.5 GB at four million files.

* **The change log holds pointers, not copies of the data.** An entry about a copy says only which
  file and node (`ref_file`, `ref_node`); what it states (present or missing, size, mtime, hash,
  path) is read from the copy when the entry is sent, so the wire format is unchanged. Writing an
  entry replaces the older one about the same copy, so the log never holds more than one per
  copy and cannot outgrow the index. That is safe for a peer that is behind: the entry it would
  have fetched is replaced by a newer one that is sent instead. Only a copy's removal carries its
  path (nothing is left to point at). Entries forwarded from another server are stored the same way.
* **Hashes are stored as 32 bytes.** The `hash` column is computed from them (`hash_b`) for reading,
  so the code still sees `blake2b:<hex>`, and aggregations compare the raw bytes.
* **No casefolded copy of the path.** `files.vhash` is a 64-bit number of the casefolded path (indexed);
  two paths that differ only in case have the same number, and candidates are compared for
  real. A name search narrows candidates in SQL with the lower-cased path (ASCII) and keeps every
  path holding a non-ASCII character as a candidate, then checks the exact match, so a case folding
  SQLite's `lower()` does not know (the Kelvin sign is a `k`) still matches.
* **Compaction.** The upgrade rewrites the tables in place, so right after it the file is
  compacted (`VACUUM`, which needs about the database's size in free disk space, and for a short
  while twice that in total). `reifs db stats` shows where the space goes and `reifs db vacuum` compacts
  it at any time (it refuses while another process, such as a running server, has the database
  open, unless `--force`).

The SQL functions the schema uses (`hx`, `hb`, `vh`) are registered by rei-fs on every connection:
the `sqlite3` command-line tool alone cannot read the `hash` column.

### 5.14 Directories are rows

A file's path used to be stored whole, in its row and again in an index. Now a directory is a row of
`dirs(id, parent_id, name, path)` (the root is `path = ''`) and a file is `files(dir_id, name)`, unique
together. The view `vfiles` joins them back into `vpath`, for reads that want the whole path.

* Only directories that hold files (or directories) have rows: `index.ensure_dir` creates them with
  their ancestors when a file is added, and removing the last file of a folder prunes it (and
  then its emptied parents). Folders made on purpose are still `vdirs`.
* "At or below a directory" is `dirs.path = D OR dirs.path >= 'D/' AND dirs.path < 'D0'`, a range on the
  unique `dirs.path` index (`paths.subtree_sql`). Listing a directory is one lookup of its child rows
  plus a count per child, instead of walking the index by jumps.
* Results that used to be in plain path order are in (directory, name) order; the two differ only in
  corner cases (a name containing a character that sorts before `/`).
* The upgrade (version 15) builds `dirs` from the existing paths, swaps the `files` table with
  foreign keys off, checks them afterwards, and then the file is compacted as for 5.13.
  At 400,000 files (7,800 folders): 280 MB to 237 MB, the upgrade with compaction taking 13 s.

### 5.15 Times are numbers

Every time the database keeps (`created`, `updated`, `first_seen`, `hashed_at`, the change log's `ts`,
`last_seen`, `owner_since`, ...) is an integer: microseconds since the Unix epoch, UTC (`db.now()`). A
file's modification time was already `mtime_ns`. That is 8 bytes or fewer instead of 32, and
comparing and ordering them is the same as before.

* **Text stays at the edges.** The API, and so the clients and the peers, still see and send
  `2026-11-23T23:04:08.123456+00:00`: `api._send` converts the fields named in `db.TIME_KEYS` on the
  way out (`db.times_to_iso`) and `sync._apply_chunk` converts them on the way in
  (`db.times_from_iso`). The wire format and the node marker files are unchanged, so a server that
  has not been upgraded keeps working with one that has.
* **Upgrade (version 16).** Each table that had such columns is rebuilt with `INTEGER` columns and the
  old text converted (`db.from_iso`; text that is no time becomes 0), then the file is compacted.
  Stored change-log payloads are not rewritten: they are converted when sent.
* Measured on 400,000 files and 600,000 copies: 237 MB before, 181 MB after (from 584 MB at the
  start of 5.13); the whole upgrade with compaction took 20 s.

### 5.16 Packed directories

A directory of thousands of small, insignificant files (a decompiled application, old source code)
costs an index row, a copy row per node and a hash per file, and a backup transfers them one by one.
It can be packed into one squashfs archive, `<dir>.sqsh`, next to where the directory was.

* **The archive is an ordinary file** of the virtual tree: it has copies, a hash, tags, is
  backed up, scheduled and fetched like any file. Copying or fetching the directory selects its
  archive. The `archives` table (`dir_path`, `file_id`, `files`, `bytes`, a `present` flag that
  makes a removal a tombstone) says which directory it stands for; `archive_members` lists the
  files inside (path and size) so that they can be searched.
* **In the tree** a packed directory is one entry, marked packed; its archive file is not listed
  beside it. It can be entered: the listing is then read from `archive_members` (folders with
  their file counts, files with their size) and is read-only (`in_archive` entries, nothing can be
  tagged, copied or moved from inside; act on the packed directory). A packed directory cannot be moved or renamed
  (unpack it first) and a directory holding or inside one cannot be packed.
* **Search is flat by default.** Names inside archives are found only with `deep` (the "deep
  search" box of the Search tab, `deep=1` of `/api/find`, which adds a `members` list).
* **Pack** (Tree: `z`; `reifs archive pack DIR`) runs on the server owning a node that holds
  every file of the directory, in the worker (the status line shows it; a request for a node of
  another server is forwarded to its owner). `mksquashfs` (zstd, else xz, else gzip) writes
  `<dir>.sqsh.part`; the archive is listed back with `unsquashfs -l` and must contain every file
  found on disk; only then is it renamed, registered, hashed and its copy recorded, and the
  copies of the loose files on this server's nodes are forgotten (the scanner treats packed
  directories like the fetch folder: it does not descend into them). The original files stay on
  disk unless "remove the originals" was chosen: then exactly the files that went into the
  archive are deleted and the folders left empty.
* **The database shrinks again.** Forgetting the loose files frees pages inside the database file,
  which SQLite keeps for reuse and does not return. New databases are made with
  `auto_vacuum = INCREMENTAL`, and after packing (and dropping) `db.reclaim` gives the free pages back
  in short steps of 4,000 pages, so writers wait only for a moment, then checkpoints so that the
  file itself gets smaller; the worker's result line says how much was returned. A database made by
  an earlier version is converted once, when the server starts (`db.make_incremental`: one full rebuild,
  like the compaction after an upgrade; `reifs db vacuum` does it by hand too).
* **Unpack** (`z` on a packed entry; `reifs archive unpack DIR`) extracts into the directory
  without overwriting anything (files that differ stop it before anything is written), then
  retires the archive and marks its file for deletion: like every deletion it is confirmed in the
  Marked tab. **Drop** (`Z`) retires it without extracting.
* **Replication.** Packing, the member lists (in chunks of 1,000) and removal travel in the
  change log as `archive` entries, latest writer wins, so every server shows the same tree and
  can search inside. Loose files of the directory that other servers' nodes still hold are
  forgotten when those servers next scan. The squashfs tools exist on Linux only: a Windows
  node stores and copies the archive like any file but cannot pack or unpack.

## 6. Replication (milestone 5)

**Copy goal.** Every file should have `[replication] copies` good copies
(default 2). Files carrying a tag listed in `[replication] tag_copies`, for
example `{ "important" = 3 }`, have that goal instead when it is higher.
Set the goal with `reifs replication copies N [--tag T]`, from the client
(`g` on the Overview tab), or in `config.toml`. Each server applies its own
configuration, so set the same goal on every server.

**Satisfied files.** A file is *satisfied* when it has as many good copies
as its goal. A good copy is a copy of the current version that is not
marked for deletion, on any node, online or offline.

**Who proposes.** For a file below its goal, **the server owning its good
copy with the lowest node id** proposes the missing copies, one proposal per
missing copy, each to a different node. That server can serve as the
source, and the choice is the same everywhere, so paired servers never
propose duplicates. Files whose only current copies are marked for deletion
are being deleted and are left alone.

**Destination.** The proposing server looks at its own nodes and those of
its paired servers. It skips a node if any of these apply:

* the node is not a backup target;
* it already holds some version of the file (stale versions are handled by
  `divergent` decisions), or has a pending proposal for it;
* it cannot hold the name (restricted names, case clash);
* it lacks the space: its announced free space, minus what pending
  proposals will use, must stay above `transfers.min_free_mb`;
* its server was unreachable at the last sync.

The remaining nodes are ranked online first, then by priority, then by free
space. Nodes announce their free space in their `node` log entries when it
changes by more than 1 % of the disk or 256 MiB.

**Proposals.** A proposal is a wanted job with reason `replicate`:

* It becomes `done` when the file is satisfied, or when its destination
  holds a good copy.
* A **rejected** proposal is not made again until the file's content
  changes.
* Approving a proposal whose destination is on another server creates the
  job on that server, which pulls the file.

**When planning runs.** The worker plans after each scan, every 10 minutes,
and within a minute of any change to the log (including facts from peers).
`reifs replication plan` and the client (`p` on the Overview tab) plan on
request.

**Report.** The last plan is stored. It records:

* the number of files below their goal, and how many this server is
  responsible for;
* how many proposals were made, were already pending, or were declined
  earlier;
* why the rest have no possible destination, with examples.

It is shown by `reifs replication` and on the client's Overview tab.

**Approval.**

* By default, proposals wait in the Wanted list.
* With automatic approval (`[server] auto_replicate`, toggled with
  `reifs replication auto on|off` or `a` on the Overview tab), replication
  proposals are approved as soon as they are made.
* Restores, renames and overwrites come from the user's own decisions and
  always wait for approval.

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
* **rsync over SSH** (encrypted, for servers not on the same LAN or VPN):
  * A server offers it with `[ssh] enabled` (plus `host`, `port`, `user`).
    Its nodes then announce the path they are served under. Peers learn
    the offer and the server's public key during sync
    (`GET /api/peer/info`).
  * Each server has its own Ed25519 key in `<home>/ssh/`. The pulling
    server pins host keys on first use in `<home>/ssh/known_hosts`.
  * On the offering machine, `reifs ssh authorize --apply` (or `k` in the
    client's Servers tab) adds the paired servers' keys to
    `~/.ssh/authorized_keys`. If `rrsync` is installed (it comes with
    rsync), each key is restricted to
    `restrict,command="rrsync -ro /"`: read-only rsync and nothing else,
    like the daemon.
  * The transport is chosen per paired server on the pulling side:
    `reifs peer transport NAME ssh|daemon` or `t` in the Servers tab.
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
| `GET find?q=&tag=&node=&issues=1&under=1` | Search (max 1000 results). `q` is text the name contains (any case), or a glob such as `*.jpg` that must match the whole name; with a `/` it is matched against the whole path. Folders are matched the same way by their own name: `dirs` lists them with the number of files below that pass the other filters |
| `GET tags`, `POST tags {action, tag, paths, recursive}` | Tags |
| `GET decisions`, `POST decisions/<id> {option}` | Decision stack |
| `GET wanted`, `POST wanted {action, ids or all}` | Proposed jobs |
| `GET jobs?all=1`, `GET jobs/<id>`, `POST jobs {kind, paths, tag, to, from}` | Jobs |
| `GET priority?path=`, `POST priority {path, priority}` | Update priority of a directory (4.5) |
| `GET nodes/<name>/audit` | What is on a node's disk but not in the index, and the reverse (5.7) |
| `POST nodes/<name>/root {path, move?}` | Change where a node's root folder is on its server (the server's own node too: `[storage] root` follows; editing that by hand after moving the folder is also found) |
| `POST vmove {paths, to, name?, on_conflict: merge\|preserve, dry_run?}` | Move within the virtual tree (5.5) |
| `POST mkdir {path}`, `POST rmdir {path}` | Create / remove an empty virtual directory (5.6) |
| `POST jobs/run`, `POST jobs/<id>/cancel`, `POST jobs/<id>/retry` | Job control |
| `GET marked?node=`, `POST unmark {paths, node, all}` | Deletion marks |
| `GET replication`, `POST replication {copies, tag_copies, auto_replicate}`, `POST replication/plan` | Copy goal |
| `POST peers/<id>/transport {transport}`, `POST peers/authorize {apply}` | rsync over SSH (client only) |
| `GET peer/info` | Server to server: SSH offer and public key |
| `POST nodes {path, name?}` | Register a storage node (drive or folder on the server's machine) |
| `POST nodes/<name> {name?, priority?, replication_target?}` | Rename a node (also in its ID file) or change settings |
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
* Server calls never run on the interface thread. A background executor
  (one for reads, one for writes, plus the status poller, each with its own
  connection sharing one session) runs them and hands results back to the
  interface thread; loads triggered by the selection are coalesced so only
  the latest runs. The status bar shows requests that are still waiting.
  The Overview shows the server's activity and the database lock
  (`GET status` has a `database` section: free, held by whom for how long,
  locked by another program, waiting writers, requests being served).
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

[replication]
copies = 2                    # good copies every file should have
tag_copies = {}               # e.g. { "important" = 3 }

[ssh]
enabled = false               # let paired servers pull over SSH
host = ""                     # "" = our address as peers know it
port = 22
user = ""                     # "" = the user running rei-fs
command = "ssh"               # ssh client used to pull from peers
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
