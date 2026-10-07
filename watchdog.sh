#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
# Watchdog — smart monitor for the production pipeline.
#
# Behavior:
#   1. If run.sh is running → sleep
#   2. If pipeline down but an orphan agent has a ComfyUI job → wait
#      (max 15 min) so the render isn't wasted
#   3. If pipeline down and nothing in flight → kill leftovers, resume
#   4. Exits when final/movie_4k60.mp4 exists
#
# Usage:
#   ./watchdog.sh <story.md>         (new production)
#   ./watchdog.sh --resume           (resume latest)
#   ./watchdog.sh --resume <dir>     (resume specific)
# ═══════════════════════════════════════════════════════════════════

set -uo pipefail

PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DSH_DIR="${DSH_DIR:-/root/desktop/deepseek-harness}"
NOTIFY="$PACK_DIR/pipeline/notify.sh"
COMFYUI_URL="http://127.0.0.1:8188"
CHECK_INTERVAL=30          # seconds between monitor checks
ORPHAN_RENDER_MAX=900      # 15 min max wait for orphan ComfyUI job
MAX_CONSECUTIVE_CRASHES=5
CRASH_COUNT=0

notify() { bash "$NOTIFY" "$1" "${2:-}" 2>/dev/null & }
log()    { echo "[watchdog $(date '+%H:%M:%S')] $*"; }

# ── Detect the run directory ──────────────────────────────────
find_run_dir() {
    # Match only timestamped run dirs (run_YYYYMMDD_HHMMSS)
    ls -td "$PACK_DIR/output/run_"[0-9]* 2>/dev/null | head -1 || true
}

# ── Check if final movie exists ───────────────────────────────
is_movie_done() {
    local run_dir
    run_dir=$(find_run_dir)
    [ -n "$run_dir" ] && [ -f "$run_dir/final/movie_4k60.mp4" ]
}

# ── Check if run.sh is alive ─────────────────────────────────
is_pipeline_running() {
    pgrep -f "run\.sh.*--resume\|run\.sh.*/run_" > /dev/null 2>&1
}

# ── Check if ComfyUI has an active job ────────────────────────
comfyui_has_job() {
    local queue
    queue=$(curl -sf "$COMFYUI_URL/api/queue" 2>/dev/null) || return 1
    local running pending
    running=$(echo "$queue" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("queue_running",[])))' 2>/dev/null || echo 0)
    pending=$(echo "$queue" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("queue_pending",[])))' 2>/dev/null || echo 0)
    [ "$running" -gt 0 ] || [ "$pending" -gt 0 ]
}

# ── Check for orphan dsh agents ───────────────────────────────
has_orphan_agents() {
    pgrep -f "dsh --profile headless" > /dev/null 2>&1
}

# ── Kill leftover processes ───────────────────────────────────
kill_leftovers() {
    log "Cleaning up leftover processes..."
    pkill -f "dsh --profile headless" 2>/dev/null || true
    sleep 2
    # Double-check: force kill if still alive
    pkill -9 -f "dsh --profile headless" 2>/dev/null || true
}

# ── Wait for orphan ComfyUI render to finish ──────────────────
wait_for_orphan_render() {
    log "Orphan ComfyUI render detected — waiting up to ${ORPHAN_RENDER_MAX}s..."
    notify "Pipeline down but ComfyUI is rendering — waiting for it to finish" "Watchdog"
    local waited=0
    while [ $waited -lt $ORPHAN_RENDER_MAX ]; do
        if ! comfyui_has_job; then
            log "Orphan render completed after ${waited}s"
            # Copy any new outputs to the clip dir
            return 0
        fi
        sleep 10
        waited=$((waited + 10))
        if [ $((waited % 60)) -eq 0 ]; then
            log "  Still rendering... (${waited}s / ${ORPHAN_RENDER_MAX}s)"
        fi
    done
    log "Orphan render timed out after ${ORPHAN_RENDER_MAX}s — killing"
    notify "Orphan render timed out after ${ORPHAN_RENDER_MAX}s" "Watchdog"
    return 1
}

# ── AI diagnosis on crash (uses local or cloud model) ─────────
ai_diagnose() {
    local run_dir
    run_dir=$(find_run_dir)
    [ -z "$run_dir" ] && return

    log "AI agent diagnosing crash..."

    local ctx_file="$run_dir/watchdog_crash_context.txt"
    {
        echo "=== PIPELINE CRASH CONTEXT ==="
        echo "=== LAST 50 LINES OF pipeline.log ==="
        tail -50 "$run_dir/pipeline.log" 2>/dev/null
        echo "=== SERVICES ==="
        echo "comfyui: $(systemctl is-active comfyui.service 2>/dev/null)"
        echo "ninfer-us: $(systemctl is-active ninfer-us.service 2>/dev/null)"
        echo "ninfer: $(systemctl is-active ninfer.service 2>/dev/null)"
        echo "=== GPU ==="
        nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader 2>/dev/null
        echo "=== DISK ==="
        df -h /root --output=avail 2>/dev/null | tail -1
    } > "$ctx_file" 2>/dev/null

    local prompt_file="$run_dir/watchdog_prompt.txt"
    cat > "$prompt_file" << 'DIAG'
You are the pipeline watchdog. The production pipeline crashed. Your job:
1. READ the crash context below
2. DIAGNOSE the root cause
3. FIX the problem by running shell commands
4. Output JSON: {"diagnosis": "...", "fix_applied": "...", "can_resume": true/false, "wait_seconds": 30}
DIAG
    cat "$ctx_file" >> "$prompt_file"

    # Route to the right model
    cd "$DSH_DIR"
    python3 "$PACK_DIR/pipeline/set_agent_model.py" "pipeline-orchestrator" 2>/dev/null || true

    export DSH_PERMISSION_MODE="${DSH_PERMISSION_MODE:-danger-full-access}"
    local diag_raw="$run_dir/watchdog_diagnosis_raw.txt"
    timeout 120 pnpm dsh --profile headless \
        --patch "$PACK_DIR/.dsh/.agent-presets/pipeline-orchestrator/agent.cordis.yml" \
        "$(cat "$prompt_file")" \
        > "$diag_raw" 2>> "$run_dir/pipeline.log" || true

    # Extract diagnosis
    local diag_json="$run_dir/watchdog_diagnosis.json"
    python3 "$PACK_DIR/extract_json.py" "$diag_raw" "$diag_json" diagnosis 2>/dev/null || true

    if [ -s "$diag_json" ]; then
        local diagnosis fix
        diagnosis=$(python3 -c "import json; print(json.load(open('$diag_json')).get('diagnosis','unknown'))" 2>/dev/null || echo "unknown")
        fix=$(python3 -c "import json; print(json.load(open('$diag_json')).get('fix_applied','unknown'))" 2>/dev/null || echo "unknown")
        log "Diagnosis: $diagnosis"
        log "Fix: $fix"
        notify "Crash diagnosis: $diagnosis\nFix: $fix\nAttempt $CRASH_COUNT/$MAX_CONSECUTIVE_CRASHES" "Watchdog AI"
    else
        log "AI diagnosis produced no JSON"
        notify "Pipeline crashed — AI diagnosis inconclusive. Retrying." "Watchdog"
    fi
}

# ══════════════════════════════════════════════════════════════
# MAIN MONITOR LOOP
# ══════════════════════════════════════════════════════════════

log "=== Watchdog started ==="
log "Args: $*"

# First run: start the pipeline
FIRST_RUN=true
ORIGINAL_ARGS=("$@")

while true; do
    # ── Check 4: Movie done? Exit. ──
    if is_movie_done; then
        log "Final movie exists! Production complete."
        notify "Production COMPLETE! Movie ready." "Watchdog"
        exit 0
    fi

    # ── Check 1: Pipeline running? Sleep. ──
    if is_pipeline_running; then
        sleep "$CHECK_INTERVAL"
        continue
    fi

    # Pipeline is NOT running.
    # Is this the first iteration? Start it.
    if [ "$FIRST_RUN" = "true" ]; then
        FIRST_RUN=false
        log "Starting pipeline..."
        "$PACK_DIR/run.sh" "${ORIGINAL_ARGS[@]}" &
        PIPELINE_PID=$!
        log "Pipeline started (PID=$PIPELINE_PID)"
        sleep "$CHECK_INTERVAL"
        continue
    fi

    # Pipeline died. Increment crash count.
    CRASH_COUNT=$((CRASH_COUNT + 1))
    log "Pipeline is down (crash $CRASH_COUNT/$MAX_CONSECUTIVE_CRASHES)"

    # ── Check max crashes ──
    if [ "$CRASH_COUNT" -ge "$MAX_CONSECUTIVE_CRASHES" ]; then
        notify "Pipeline crashed $MAX_CONSECUTIVE_CRASHES times — giving up.\nManual intervention needed." "Watchdog STOPPED"
        log "Max crashes reached. Exiting."
        exit 1
    fi

    # ── Check 2: Orphan ComfyUI render in flight? Wait for it. ──
    if comfyui_has_job; then
        wait_for_orphan_render
        # After render finishes (or times out), the output may be in ComfyUI's dir.
        # generate_clip.py's find_new_outputs will pick it up on resume.
    fi

    # ── Check 3: Kill leftover agents, diagnose, resume. ──
    if has_orphan_agents; then
        kill_leftovers
    fi

    # AI diagnosis
    ai_diagnose

    # Find the run dir for resume
    RUN_DIR=$(find_run_dir)
    if [ -z "$RUN_DIR" ]; then
        log "No run directory found — cannot resume"
        notify "Watchdog: no run dir found" "Watchdog ERROR"
        exit 1
    fi

    log "Resuming pipeline from $RUN_DIR..."
    sleep 10  # brief cooldown

    "$PACK_DIR/run.sh" --resume "$RUN_DIR" &
    PIPELINE_PID=$!
    log "Pipeline resumed (PID=$PIPELINE_PID)"

    # Reset crash count on successful resume (checked next iteration)
    sleep "$CHECK_INTERVAL"

    # If pipeline survived CHECK_INTERVAL, reset crash counter
    if is_pipeline_running; then
        if [ "$CRASH_COUNT" -gt 0 ]; then
            log "Pipeline stable after resume — resetting crash counter"
            CRASH_COUNT=0
        fi
    fi
done
