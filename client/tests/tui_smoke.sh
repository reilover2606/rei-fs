#!/usr/bin/env bash
# Drive the real terminal UI in tmux against a running server and check that
# it renders, stays idle, and (with a sanitizer build) never touches freed
# memory. Usage: tui_smoke.sh CLIENT_BINARY PORT PASSWORD
#
# It goes through the first-connection fingerprint prompt, visits every tab,
# opens a few dialogs and quits.
set -euo pipefail

BIN=$(realpath "$1")
PORT=$2
PASSWORD=$3
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

tmux new-session -d -s "$SESSION" -x 130 -y 36 \
  "REIFS_CLIENT_HOME='$WORK/home' ASAN_OPTIONS=halt_on_error=1 '$BIN' 127.0.0.1:$PORT 2>'$WORK/stderr'; echo \$? > '$WORK/exit'; sleep 600"

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
sleep 3

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
