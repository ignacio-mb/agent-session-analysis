#!/bin/sh
# Claude Code SessionEnd hook: reload the session warehouse (`session-analytics warehouse --load`) in the
# background, so ending a session is never held up by the ~30 s load.
#
#   ~/.claude/settings.json
#   {"hooks": {"SessionEnd": [{"hooks": [{"type": "command",
#                                          "command": "<path to convo-analysis>/scripts/warehouse_hook.sh"}]}]}}
#
# One load at a time. A session that ends while a load runs asks for one more pass after it, so every session's
# last lines make it in. Output: ~/claude-session-exports/_warehouse/hook.log (last 400 lines kept); the CSVs:
# _warehouse/latest, overwritten each time. With Docker or the container down, the load fails, the log says why,
# and nothing else happens.

REPO=$(cd "$(dirname "$0")/.." && pwd)
OUT="$HOME/claude-session-exports/_warehouse"
LOG="$OUT/hook.log"
LOCK="${TMPDIR:-/tmp}/convo-analysis-warehouse.lock"
AGAIN="$LOCK.again"
# The desktop app starts hooks with a short PATH; the load needs docker.
PATH="$HOME/.docker/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
export PATH

if [ "$1" = "--run" ]; then
  while :; do
    while :; do
      rm -f "$AGAIN"
      echo "== $(date '+%Y-%m-%d %H:%M:%S') load"
      PYTHONPATH="$REPO/src" /usr/bin/python3 -m session_analytics warehouse --load --out "$OUT/latest" 2>&1 |
        grep -v '^  [0-9]*/[0-9]* transcripts$'
      [ -e "$AGAIN" ] || break
    done
    rmdir "$LOCK" 2>/dev/null
    # A session that ended between the last look and releasing the lock.
    if [ -e "$AGAIN" ] && mkdir "$LOCK" 2>/dev/null; then continue; fi
    break
  done
  tail -n 400 "$LOG" >"$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
  exit 0
fi

mkdir -p "$OUT"
# A lock left by a load that died mid-way (a reboot) stops counting after 30 minutes.
find "$LOCK" -maxdepth 0 -mmin +30 -exec rmdir {} \; 2>/dev/null
if mkdir "$LOCK" 2>/dev/null; then
  nohup "$0" --run >>"$LOG" 2>&1 </dev/null &
else
  touch "$AGAIN"
fi
exit 0
