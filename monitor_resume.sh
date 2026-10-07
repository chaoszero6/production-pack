#!/usr/bin/env bash
# Keep the production pipeline alive: resume run.sh if it is not running.
# - Never double-launch: one monitor, pidfile guard on run.sh.
# - If an orphaned video-generator agent still has a ComfyUI job in flight,
#   wait for that render to finish before resuming (avoid two H3 jobs).
# - Exits when the final movie exists.
set -uo pipefail

PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PACK_DIR" || { echo "pack dir missing: $PACK_DIR"; exit 1; }

RUN_DIR="${RUN_DIR:-$PACK_DIR/output/run_20260921_231424}"
PIDFILE="$PACK_DIR/output/run_latest.pid"
MONITOR_PIDFILE="$PACK_DIR/output/run_monitor.pid"
LOG="$PACK_DIR/output/run_latest_console.log"
MONITOR_LOG="$PACK_DIR/output/run_monitor.log"
MOVIE="$RUN_DIR/final/movie_4k60.mp4"
COMFY="http://127.0.0.1:8188"
ORPHAN_GRACE_SECS=900   # if orphan agent up + queue busy, wait up to this long per check
IDLE_ORPHAN_KILL_SECS=300 # orphan alive but queue empty this long → kill it

echo $$ > "$MONITOR_PIDFILE"
mlog() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$MONITOR_LOG"; }

pipeline_running() {
    # Prefer live process scan: setsid may fork, so $! / pidfile can go stale.
    if pgrep -f 'bash run\.sh --resume' >/dev/null 2>&1; then
        return 0
    fi
    if [ -f "$PIDFILE" ]; then
        local p
        p=$(cat "$PIDFILE" 2>/dev/null || true)
        if [ -n "${p:-}" ] && kill -0 "$p" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

orphan_agents() {
    # video-generator dsh trees that are NOT children of a live run.sh
    pgrep -f 'agent-presets/video-generator' 2>/dev/null || true
}

comfy_busy() {
    local q
    q=$(curl -sf --max-time 5 "$COMFY/api/queue" 2>/dev/null || echo '{}')
    python3 - "$q" <<'PY'
import json,sys
try:
    d=json.loads(sys.argv[1])
except Exception:
    sys.exit(1)
sys.exit(0 if (d.get("queue_running") or d.get("queue_pending")) else 1)
PY
}

kill_orphans() {
    local ids
    ids=$(orphan_agents)
    if [ -n "$ids" ]; then
        mlog "Killing orphan video-generator agent(s): $(echo $ids | tr '\n' ' ')"
        # kill process group politely, then hard
        for p in $ids; do
            kill -TERM "$p" 2>/dev/null || true
        done
        sleep 3
        for p in $ids; do
            kill -KILL "$p" 2>/dev/null || true
        done
        # also reap leftover generate_video scripts under the run dir
        pkill -TERM -f 'generate_video_S0' 2>/dev/null || true
        sleep 1
        pkill -KILL -f 'generate_video_S0' 2>/dev/null || true
    fi
}

resume_pipeline() {
    mlog "Resuming pipeline: run.sh --resume $RUN_DIR"
    if [ -s "$LOG" ]; then
        cp "$LOG" "output/run_latest_console.log.before-resume-$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
    fi
    # Record the real run.sh pid inside the session (setsid $! can be a short-lived parent).
    setsid nohup bash -c 'echo $$ > "$0"; exec bash run.sh --resume "$1"' "$PIDFILE" "$RUN_DIR" \
        >> "$LOG" 2>&1 < /dev/null &
    sleep 1
    local pid
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
    if pipeline_running; then
        mlog "Resumed (pid=${pid:-unknown})"
    else
        mlog "ERROR: resume did not start run.sh — see $LOG"
        return 1
    fi
}

mlog "Monitor started pid=$$ run_dir=$RUN_DIR"

ORPHAN_BUSY_SINCE=0
ORPHAN_IDLE_SINCE=0

while true; do
    if [ -f "$MOVIE" ]; then
        mlog "Final movie present ($MOVIE) — monitor exiting."
        rm -f "$MONITOR_PIDFILE"
        exit 0
    fi

    if pipeline_running; then
        ORPHAN_BUSY_SINCE=0
        ORPHAN_IDLE_SINCE=0
        sleep 30
        continue
    fi

    # Pipeline is down.
    ORPH=$(orphan_agents | tr '\n' ' ')
    if [ -n "${ORPH// /}" ]; then
        if comfy_busy; then
            now=$(date +%s)
            if [ "$ORPHAN_BUSY_SINCE" -eq 0 ]; then
                ORPHAN_BUSY_SINCE=$now
                mlog "Pipeline down; orphan agent(s) [$ORPH] have a ComfyUI job in flight — waiting."
            fi
            if [ $((now - ORPHAN_BUSY_SINCE)) -gt "$ORPHAN_GRACE_SECS" ]; then
                mlog "Orphan render exceeded ${ORPHAN_GRACE_SECS}s — killing and resuming."
                kill_orphans
                ORPHAN_BUSY_SINCE=0
                resume_pipeline
            else
                sleep 20
                continue
            fi
        else
            # orphan alive but queue empty → idle, reap it
            now=$(date +%s)
            if [ "$ORPHAN_IDLE_SINCE" -eq 0 ]; then
                ORPHAN_IDLE_SINCE=$now
                mlog "Pipeline down; orphan agent(s) [$ORPH] idle (queue empty) — grace ${IDLE_ORPHAN_KILL_SECS}s."
            fi
            if [ $((now - ORPHAN_IDLE_SINCE)) -ge "$IDLE_ORPHAN_KILL_SECS" ]; then
                mlog "Idle orphan past grace — killing."
                kill_orphans
                ORPHAN_IDLE_SINCE=0
                resume_pipeline
            else
                sleep 15
                continue
            fi
        fi
    else
        # No pipeline, no orphan — free to resume.
        # Brief settle so a just-exited run.sh does not get double-started.
        sleep 5
        if pipeline_running; then
            sleep 30
            continue
        fi
        if [ -f "$MOVIE" ]; then
            mlog "Final movie present — monitor exiting."
            rm -f "$MONITOR_PIDFILE"
            exit 0
        fi
        resume_pipeline
        sleep 30
    fi
done
