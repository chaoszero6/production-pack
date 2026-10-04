#!/bin/bash
# Watch the detached production run and report once it exits.
# Bounded: polls until the launcher pid dies, then prints the tail of the
# console log plus whatever artifacts exist. One notification, then exits.
PIDFILE="/root/production_pack/output/run_latest.pid"
LOG="/root/production_pack/output/run_latest_console.log"

P=$(cat "$PIDFILE" 2>/dev/null)
if [ -z "$P" ]; then echo "no pidfile"; exit 1; fi

while kill -0 "$P" 2>/dev/null; do
    sleep 60
done

RUN_DIR=$(ls -d /root/production_pack/output/run_2026* 2>/dev/null | sort | tail -1)
echo "=== pipeline pid $P exited at $(date '+%H:%M:%S') ==="
echo "run dir: $RUN_DIR"
echo
echo "=== last 30 console lines ==="
tail -30 "$LOG"
echo
echo "=== artifacts produced ==="
ls -la --time-style=+%H:%M:%S "$RUN_DIR"/*.json "$RUN_DIR"/final/* 2>/dev/null | head -30
