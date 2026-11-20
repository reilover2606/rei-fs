# rei-fs

A decentralized file and backup manager. Every node (servers, external drives,
Android devices running the server in Termux) shares one virtual directory
tree, and each node physically stores only the files it holds. Servers track
where every file lives, propose and run rsync transfers that resume after a
failure, and work towards every file existing in its current version on at
least two nodes. Servers never delete files.

* `server/`: Python server (index, scanning, transfers, sync).
* `client/`: C++ terminal client using FTXUI, built with meson/ninja
  (milestone 3).
* [`docs/DESIGN.md`](docs/DESIGN.md): architecture and behaviour.

Status: milestone 1 (index and scanning) is done. See the server's
[README](server/README.md) to try it.

License: MIT.
