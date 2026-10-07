#!/bin/bash
# Durable launcher for the Clockwork Moth production run.
# Detaches the pipeline from the Hermes gateway (setsid) so a gateway restart
# cannot kill a multi-hour render.
set -uo pipefail
PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PACK_DIR" || { echo "pack dir missing: $PACK_DIR"; exit 1; }
STORY="${STORY_FILE:-/root/desktop/films/the-clockwork-moth/story-v2.md}"
LOG="$PACK_DIR/output/run_latest_console.log"
PIDFILE="$PACK_DIR/output/run_latest.pid"

[ -f "$STORY" ] || { echo "story file missing: $STORY"; exit 1; }

# already running?
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "pipeline already running (pid $(cat "$PIDFILE"))"
    exit 0
fi

: > "$LOG"
setsid nohup bash run.sh "$STORY" >> "$LOG" 2>&1 < /dev/null &
PID=$!
echo "$PID" > "$PIDFILE"
echo "launched pid=$PID"
echo "log=$LOG"
