#!/bin/bash
# stop.sh — stop the production pack: the run, its agent turns, in-flight renders and the
# pack's monitors. Nothing is deleted; `./launch_resume.sh <run_dir>` picks up where it left off.
#
#   ./stop.sh                 stop everything (run + monitors), cancel the ComfyUI queue
#   ./stop.sh --keep-monitor  leave tts-idle-monitor.service (LLM crash/hang recovery) running
#   ./stop.sh --gpu llm|none  after stopping, hand the GPU to the LLM / park every owner
#                             (default: leave the current owner as it is)
set -uo pipefail
PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$PACK_DIR/output"
KEEP_MONITOR=false
GPU_AFTER=""
while [ $# -gt 0 ]; do
    case "$1" in
        --keep-monitor) KEEP_MONITOR=true ;;
        --gpu) GPU_AFTER="${2:-}"; shift ;;
        -h|--help) sed -n 2,9p "$0"; exit 0 ;;
        *) echo "unknown option: $1"; exit 2 ;;
    esac
    shift
done
log() { echo "[stop $(date '+%H:%M:%S')] $*"; }

term_then_kill() {   # $1 = label, rest = pids
    local label="$1"; shift
    local pids
    pids=$(printf '%s\n' "$@" | grep -E '^[0-9]+$' | sort -u | tr '\n' ' ')
    [ -z "${pids// /}" ] && return 0
    log "stopping $label: $pids"
    kill -TERM $pids 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        sleep 1
        local alive=""
        for p in $pids; do kill -0 "$p" 2>/dev/null && alive="$alive $p"; done
        [ -z "$alive" ] && return 0
    done
    log "  still alive, SIGKILL:$alive"
    kill -KILL $alive 2>/dev/null || true
}

# ── 1. the run itself (run.sh and the setsid launcher that wraps it) ─────────────────
RUN_PIDS=""
for f in "$OUT/run_latest.pid" "$OUT/run_latest.launch.pid"; do
    [ -f "$f" ] && RUN_PIDS="$RUN_PIDS $(cat "$f" 2>/dev/null)"
done
RUN_PIDS="$RUN_PIDS $(pgrep -f 'bash run[.]sh' || true)"
RUN_DIR="$(readlink -f "$OUT/run_latest" 2>/dev/null || true)"
term_then_kill "run.sh" $RUN_PIDS

# ── 2. agent turns (dsh / node) and the render helpers it may have spawned ──────────
term_then_kill "dsh agent turns" $(pgrep -f 'cli/src/bin[.]ts --profile headless' || true) \
                                 $(pgrep -f 'pnpm dsh --profile headless' || true)
term_then_kill "render helpers" \
    $(pgrep -f 'pipeline/(generate_clip|generate_video_direct|generate_frames|ideogram_frames|tts_render|voice_lineup|generate_dialogue_audio|upscale_film|upscale_clip|generate_character_sheets|generate_location_refs)[.]py' || true) \
    $(pgrep -f "$PACK_DIR/upscale_clip[.]sh" || true)

# ── 3. the pack's own watchers / supervisors (never the system tunnel watchdog) ──────
term_then_kill "pack watchers" \
    $(pgrep -f "$PACK_DIR/(monitor_resume|watch_run|watchdog)[.]sh" || true) \
    $(pgrep -f "$PACK_DIR/pipeline/(vram_supervisor|vram_monitor)[.]sh" || true) \
    $(pgrep -f "$PACK_DIR/pipeline/(idle_monitor|llm_monitor|vram_watchdog)[.]py" || true)
rm -f "$OUT/run_monitor.pid"

if [ "$KEEP_MONITOR" = "false" ]; then
    if systemctl is-active --quiet tts-idle-monitor.service 2>/dev/null; then
        log "stopping tts-idle-monitor.service (LLM crash/hang monitor) — restart with: systemctl start tts-idle-monitor"
        systemctl stop tts-idle-monitor.service 2>/dev/null || true
    fi
fi

# ── 4. cancel whatever ComfyUI is rendering (a 10 s H3 clip runs ~6 min) ────────────
if curl -sf --max-time 3 http://127.0.0.1:8188/api/queue >/dev/null 2>&1; then
    curl -sf --max-time 5 -X POST http://127.0.0.1:8188/api/interrupt >/dev/null 2>&1 || true
    curl -sf --max-time 5 -X POST -H 'Content-Type: application/json' -d '{"clear":true}' \
        http://127.0.0.1:8188/api/queue >/dev/null 2>&1 || true
    log "ComfyUI queue cleared"
fi

# ── 5. phase markers the monitors read ───────────────────────────────────────────────
rm -f "$OUT/.gen_phase_active" "$OUT/.audio_phase_active"

if [ -n "$GPU_AFTER" ] && [ -x "$PACK_DIR/pipeline/gpu_owner.sh" ]; then
    log "GPU owner -> $GPU_AFTER"
    bash "$PACK_DIR/pipeline/gpu_owner.sh" "$GPU_AFTER" || true
fi

STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
[ -n "$RUN_DIR" ] && [ -d "$RUN_DIR" ] && echo "[$STAMP] run stopped by operator (stop.sh)" >> "$RUN_DIR/pipeline.log"
[ -x "$PACK_DIR/pipeline/notify.sh" ] && bash "$PACK_DIR/pipeline/notify.sh" "Production pack stopped by operator (stop.sh)${RUN_DIR:+ — run $(basename "$RUN_DIR")}" "Stopped" >/dev/null 2>&1 &

echo
log "stopped. Still running:"
pgrep -af 'bash run[.]sh|bin[.]ts --profile headless|pipeline/.*[.]py' | grep -v -E 'pgrep|llm_ready_notify' | cut -c1-120 || true
log "GPU: $(bash "$PACK_DIR/pipeline/gpu_owner.sh" status 2>/dev/null | head -1 || echo unknown)"
[ -n "$RUN_DIR" ] && log "resume with: ./launch_resume.sh $RUN_DIR"
exit 0
