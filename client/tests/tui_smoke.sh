#!/usr/bin/env bash
# Drive the real terminal UI in tmux against a running server and check that
# it renders, stays idle, and (with a sanitizer build) never touches freed
# memory. Usage: tui_smoke.sh CLIENT_BINARY PORT PASSWORD [QUERY EXPECTED]
#
# With QUERY and EXPECTED (for example 日本語 日本語/ファイル.txt) it also types
# QUERY into the Search tab and checks that EXPECTED is listed: CJK input,
# the search and the display of wide characters.
#
# It goes through the first-connection fingerprint prompt, visits every tab,
# opens a few dialogs and quits.
set -euo pipefail

BIN=$(realpath "$1")
PORT=$2
PASSWORD=$3
QUERY=${4:-}
EXPECTED=${5:-}
SESSION=reifs-smoke-$$
WORK=$(mktemp -d)
trap 'tmux kill-session -t "$SESSION" 2>/dev/null || true' EXIT

fail() {
  echo "FAIL: $*"
  echo "--- screen ---"
  tmux capture-pane -t "$SESSION" -p 2>/dev/null || true
  echo "--- stderr ---"
  cat "$WORK/stderr" 2>/dev/null || true
  exit 1
}

# -u and a UTF-8 locale: tmux and the client must treat input and output as UTF-8.
tmux -u new-session -d -s "$SESSION" -x 130 -y 36 \
  "LC_ALL=C.UTF-8 LANG=C.UTF-8 REIFS_CLIENT_HOME='$WORK/home' ASAN_OPTIONS=halt_on_error=1 '$BIN' 127.0.0.1:$PORT 2>'$WORK/stderr'; echo \$? > '$WORK/exit'; sleep 600"

send() { tmux send-keys -t "$SESSION" "$@"; sleep 1; }
screen() { tmux capture-pane -t "$SESSION" -p; }

sleep 2
send "$PASSWORD" Enter
sleep 2
screen | grep -q "First connection" || fail "no fingerprint prompt"
send Enter                      # trust and continue

# The Overview must show the server's status within a few seconds.
for _ in $(seq 1 15); do
  screen | grep -q "files  " && break
  sleep 1
done
screen | grep -q "files  " || fail "the Overview never showed the status"

# Every tab, a few dialogs.
for key in 2 3 4 5 6 7 8 9 1; do send "$key"; done
send 2; send Enter; send Left
send 9; send k; send Escape
send '?'; send Escape
send 1; send g; send Escape
send 2; send x; send Escape      # pick up an item and put it down again
send 7; send Space; send Space; send n; send Escape   # choose/unchoose a node; the add-drive dialog
send 1
sleep 3

if [ -n "$QUERY" ]; then
  send 3; send /; send "$QUERY"; send Enter
  for _ in $(seq 1 15); do                       # the first scan may still be running
    screen | grep -qF -- "$EXPECTED" && break
    send Enter
  done
  screen | grep -qF -- "$EXPECTED" || fail "searching '$QUERY' did not list '$EXPECTED'"
  send Escape; send 1
fi

# Idle: the client must not spin.
pid=$(pgrep -f "^$BIN 127.0.0.1:$PORT" | head -1) || fail "the client is not running"
before=$(awk '{print $14 + $15}' "/proc/$pid/stat")
sleep 4
after=$(awk '{print $14 + $15}' "/proc/$pid/stat")
ticks=$((after - before))
hz=$(getconf CLK_TCK)
[ "$ticks" -le $((hz * 2)) ] || fail "the client used $ticks CPU ticks in 4 s while idle"

screen | grep -q "files  " || fail "the Overview is gone"
grep -qE "AddressSanitizer|runtime error" "$WORK/stderr" && fail "sanitizer report"

send q; send Down; send Enter   # quit: "No" -> "Yes"
sleep 2
[ -f "$WORK/exit" ] || fail "the client did not quit"
[ "$(cat "$WORK/exit")" = "0" ] || fail "the client exited with $(cat "$WORK/exit")"
grep -qE "AddressSanitizer|runtime error" "$WORK/stderr" && fail "sanitizer report"
echo "TUI smoke test passed"
