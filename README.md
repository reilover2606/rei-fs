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
