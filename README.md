# rei-fs

A decentralized file and backup manager. Every node (servers, external drives,
Android devices running the server in Termux) shares one virtual directory
tree, and each node physically stores only the files it holds. Servers track
where every file lives, propose and run rsync transfers that resume after a
failure, and work towards every file existing in its current version on at
least two nodes. Servers never delete files.

* `server/`: Python server (index, scanning, transfers, sync).
* `client/`: C++ terminal client using FTXUI, built with meson/ninja.
  See its [README](client/README.md).
* [`docs/DESIGN.md`](docs/DESIGN.md): architecture and behaviour.

Status: all five planned milestones are done:

1. index and scanning;
2. resumable rsync jobs;
3. HTTPS API and terminal client;
4. pairing, metadata sync and transfers between servers;
5. replication to two copies, with optional automatic approval. See the server's
[README](server/README.md) to try it.

## Unicode

File, folder, node and tag names can use any script (CJK, Korean, Thai, Arabic,
Hebrew, emoji, characters outside the basic plane, ...). Names are kept exactly
as they are on disk, and every part of the system is tested with them: scanning,
search, the API, the client's display and input, and transfers (rsync, the
rsync daemon and SSH).

* **Exact names.** Two spellings of the same visible name (for example `é` as
  one character or as `e` plus a combining accent) are two different files.
  Some file systems (macOS) rewrite names to the second form; copy between such
  a drive and others and you will see the file under both spellings.
* **Names that are not valid UTF-8** (possible on Linux file systems that store
  raw bytes) cannot be represented and are skipped, with a message in the scan
  result.
* **The locale does not matter.** On a machine whose locale is not UTF-8
  (`LANG=C`, a Latin-1 locale) the `reifs` command restarts itself in Python's
  UTF-8 mode, so names are not read as garbage. A terminal that cannot show a
  character gets an escape (`\u65e5`) instead of an error.
* **Terminal and fonts.** The client needs a UTF-8 terminal and a font that has the
  characters; wide characters (CJK, emoji) take two cells and are aligned
  accordingly.
* **Length.** A name may be up to 255 bytes in UTF-8 (about 85 CJK characters);
  longer names are flagged as non-portable.
* **SSH transfers** assume a POSIX shell on the machine serving the files, and
  work whatever the folder's name contains (spaces, quotes, `$`, CJK), also with
  rsync older than 3.2.4 and with `rrsync`-restricted keys.

## Platforms

CI (`.github/workflows/ci.yml`) runs on every push, on Linux and Windows.

**Server.** The full test suite runs on both systems. That includes
transfers between servers through the rsync daemon; on Windows it uses
cwRsync, installed with `choco install rsync`. rsync over SSH is tested on
Linux against a real `sshd`, with read-only keys through `rrsync`. On
Windows, the tests that create names Windows forbids, or symlinks, are
skipped.

**Client.** It is built and unit-tested on both systems: on Windows with
MSYS2 UCRT64, on Linux with the distribution's compiler. On both, it then
connects to a freshly started `reifs serve`, checks certificate pinning,
logs in, and reads data.

**Not covered by CI:**

* serving files over SSH *from* a Windows machine;
* running the server under Termux on Android.

License: MIT.
