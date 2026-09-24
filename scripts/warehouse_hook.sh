#!/bin/sh
# Claude Code SessionEnd hook: bring the session warehouse up to date in the background, so ending a session is
# never held up by a load. It does what is set up and nothing else:
#   - the shared ClickHouse (~/.config/convo-analysis/.env has a CLICKHOUSE_URL): the session that just ended, and
#     only if it invoked one of CLICKHOUSE_SKILLS (default rde) — added or updated among this machine's rows; every
#     other session, this machine's and everyone else's, stays as it is (--clickhouse=auto --session-queue);
#   - the local Postgres, when its container is running: every session (--load=auto).
# With neither, it exits before reading a transcript.
#
# The plugin installs it (hooks/hooks.json). From a checkout, add it to ~/.claude/settings.json instead — not both:
#   {"hooks": {"SessionEnd": [{"hooks": [{"type": "command",
#                                          "command": "<path to convo-analysis>/scripts/warehouse_hook.sh"}]}]}}
#
# Claude Code passes the hook its input as JSON on stdin; the transcript path in it goes into a queue (one file per
# session), and a single background runner works the queue. A session that ends while a load runs is queued and
# picked up by one more pass. A load that fails leaves its sessions queued for the next one. Output:
# ~/claude-session-exports/_warehouse/hook.log (last 400 lines kept); the CSVs: _warehouse/latest.

REPO=$(cd "$(dirname "$0")/.." && pwd)
OUT="$HOME/claude-session-exports/_warehouse"
LOG="$OUT/hook.log"
QUEUE="$OUT/queue"
LOCK="${TMPDIR:-/tmp}/convo-analysis-warehouse.lock"
AGAIN="$LOCK.again"
# The desktop app starts hooks with a short PATH; the load needs docker.
PATH="$HOME/.docker/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
export PATH
PY=/usr/bin/python3
[ -x "$PY" ] || PY=$(command -v python3)

if [ "$1" = "--run" ]; then
  while :; do
    while :; do
      rm -f "$AGAIN"
      echo "== $(date '+%Y-%m-%d %H:%M:%S') load"
      PYTHONPATH="$REPO/src" "$PY" -m session_analytics warehouse --load=auto --clickhouse=auto \
        --session-queue "$QUEUE" --out "$OUT/latest" 2>&1 |
        grep -v '^  [0-9]*/[0-9]* transcripts$'
      [ -e "$AGAIN" ] || break
    done
    # Trim the log while still holding the lock, and in place: a runner started right after we release it appends
    # to the same file, and a replaced file would take its output with it.
    tail -n 400 "$LOG" >"$LOG.tmp" 2>/dev/null && cat "$LOG.tmp" >"$LOG" && rm -f "$LOG.tmp"
    rmdir "$LOCK" 2>/dev/null
    # A session that ended between the last look and releasing the lock.
    if [ -e "$AGAIN" ] && mkdir "$LOCK" 2>/dev/null; then continue; fi
    break
  done
  exit 0
fi

mkdir -p "$OUT" "$QUEUE"
# Queue the session that ended: a file named after it, holding its transcript's path. (Run by hand, from a
# terminal, there is no hook input to read: nothing is queued, and a running local Postgres is still reloaded.)
if [ ! -t 0 ]; then
  "$PY" -c 'import json, os, sys
try:
    t = json.load(sys.stdin).get("transcript_path") or ""
except ValueError:
    sys.exit(0)
if t.endswith(".jsonl"):
    entry = os.path.join(sys.argv[1], os.path.basename(t)[:-len(".jsonl")])
    with open(entry + ".tmp", "w") as fh:  # the runner ignores this name until it is whole
        fh.write(t + "\n")
    os.replace(entry + ".tmp", entry)' "$QUEUE" 2>>"$LOG"
fi
# A lock left by a load that died mid-way (a reboot) stops counting after 30 minutes.
find "$LOCK" -maxdepth 0 -mmin +30 -exec rmdir {} \; 2>/dev/null
if mkdir "$LOCK" 2>/dev/null; then
  nohup "$0" --run >>"$LOG" 2>&1 </dev/null &
else
  touch "$AGAIN"
  # The runner may have released the lock between our mkdir and the touch, after its last look for AGAIN:
  # take it now, or the queued session would wait for the next SessionEnd.
  if mkdir "$LOCK" 2>/dev/null; then
    nohup "$0" --run >>"$LOG" 2>&1 </dev/null &
  fi
fi
exit 0
