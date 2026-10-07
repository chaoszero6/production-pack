#!/usr/bin/env bash
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# VRAM Supervisor â€” background monitor agent
#
# Watches for VRAM conflicts (two+ VRAM-heavy services overlapping).
# On conflict:
#   1. STOP the offending work (the lower-priority / intruding service)
#   2. Ask the NInfer LLM to diagnose + prescribe a fix
#   3. Apply the fix (re-establish the correct phase via svc_transition)
#   4. Restart the stopped work
#
# Usage:
#   ./vram_supervisor.sh [--loop] [--window 20] [--max-interventions 5]
#
#   --loop                keep watching after an intervention (default: one pass)
#   --window N            seconds per watchdog window (default 20)
#   --max-interventions N stop intervening after N fixes (default 5; 0 = unlimited)
#
# Logs to: output/vram_supervisor.log
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
set -uo pipefail

PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$PACK_DIR/output/vram_supervisor.log"
WATCHDOG_LOG="$PACK_DIR/output/vram_watchdog.log"
# Conflict-checking LLM: a small fast model (default = local DeepSeek-R1 1.5B on :8099).
# Override with CHECKER_URL / CHECKER_MODEL if your 1.5B lives elsewhere.
NINFER_URL="${NINFER_URL:-http://127.0.0.1:8080}"          # full LLM used to RESTART work
CHECKER_URL="${CHECKER_URL:-http://127.0.0.1:8099}"        # small 1.5B used to DETECT/diagnose
CHECKER_MODEL="${CHECKER_MODEL:-/models/llm/unsloth_DeepSeek-R1-Distill-Qwen-1.5B-GGUF/DeepSeek-R1-Distill-Qwen-1.5B-Q8_0.gguf}"
mkdir -p "$PACK_DIR/output"

LOOP=false
WINDOW=20
MAX_INTV=5
while [[ $# -gt 0 ]]; do
    case "$1" in
        --loop) LOOP=true; shift ;;
        --window) WINDOW="${2:-20}"; shift 2 ;;
        --max-interventions) MAX_INTV="${2:-5}"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 2 ;;
    esac
done

log() {
    local ts; ts=$(date '+%H:%M:%S')
    echo "[$ts] [supervisor] $*" | tee -a "$LOG"
}

# Reuse the phase-transition machinery (stop-all â†’ start-needed â†’ verify)
source "$PACK_DIR/pipeline/services.sh" 2>/dev/null || true

# Which service is the "intruder" when two overlap? The LLM is the baseline we
# want to keep (agents depend on it); ComfyUI/TTS are the ones that must yield
# during thinking, and vice-versa. We resolve the target phase from what's up.
resolve_target_phase() {
    # If ComfyUI is up â†’ we're mid-generation â†’ the LLM is the intruder â†’ go video_gen
    if curl -sf --max-time 2 http://127.0.0.1:8188/api/system_stats >/dev/null 2>&1; then
        echo "video_gen"; return
    fi
    # If any TTS backend is up â†’ audio_gen
    if curl -sf --max-time 2 http://127.0.0.1:9883/v1/models >/dev/null 2>&1; then
        echo "audio_gen"; return
    fi
    # Default: thinking (keep the LLM, drop generation engines)
    echo "thinking"
}

llm_diagnose() {
    # Feed the recent watchdog log tail to the small 1.5B checker; get a JSON verdict.
    local tail
    tail=$(tail -n 20 "$WATCHDOG_LOG" 2>/dev/null)
    local prompt
    prompt=$(cat <<EOF
You are a GPU supervisor. A VRAM conflict was detected on an RTX 5090 (32 GB).
Recent samples:
$tail

Decide which service should be STOPPED so the system returns to a safe state,
and which phase (thinking|video_gen|audio_gen) it should transition to.
Reply with EXACTLY one line of JSON, no other text:
{"stop_service":"<name>","target_phase":"<phase>","reason":"<one sentence>"}
EOF
)
    curl -s --max-time 120 "$CHECKER_URL/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d "$(python3 -c 'import json,sys; print(json.dumps({"model":sys.argv[1],"messages":[{"role":"user","content":sys.argv[2]}],"max_tokens":1000}))' "$CHECKER_MODEL" "$prompt")" \
        2>/dev/null
}

intervene() {
    local phase; phase=$(resolve_target_phase)
    log "Conflict detected â†’ resolving to phase '$phase'"

    # 1. Stop the offending work atomically (transition clears conflicting svcs)
    if command -v svc_transition >/dev/null 2>&1; then
        svc_transition "$phase" true >>"$LOG" 2>&1 || log "WARNING: svc_transition '$phase' returned non-zero"
    else
        # Fallback: just park the generation engine so the LLM has VRAM
        systemctl stop comfyui.service 2>/dev/null || true
        log "Fell back to manual ComfyUI stop (services.sh unavailable)"
    fi

    # 2. Ask the small 1.5B checker to diagnose + confirm the fix
    log "Asking 1.5B checker ($CHECKER_URL) to diagnose..."
    local resp
    resp=$(llm_diagnose)
    if [[ -n "$resp" ]]; then
        local verdict
        # Extract the JSON object from anywhere in the content (model may prepend
        # reasoning text or whitespace before the actual JSON line).
        verdict=$(echo "$resp" | python3 -c 'import json,sys,re
raw=sys.stdin.read()
try:
    d=json.loads(raw)
    content=d["choices"][0]["message"].get("content","")
except Exception:
    content=""
m=re.search(r"\{.*\}", content, re.DOTALL)
print(m.group(0).strip() if m else (content.strip() or "(no JSON found in LLM response)"))' 2>/dev/null)
        log "LLM diagnosis: $verdict"
        echo "$verdict" >> "$LOG"
    else
        log "WARNING: LLM diagnosis unavailable; applied default phase transition only."
    fi

    # 3. Verify the LLM is back up (work can resume against it)
    for _w in $(seq 1 60); do
        curl -sf --max-time 3 "$NINFER_URL/health" >/dev/null 2>&1 && { log "NInfer healthy again â€” work may resume."; return 0; }
        sleep 1
    done
    log "ERROR: NInfer did not recover within 60s."
    return 1
}

main() {
    local interventions=0
    log "VRAM supervisor started (loop=$LOOP window=${WINDOW}s max_interventions=$MAX_INTV)"
    while true; do
        log "Running ${WINDOW}s watchdog window..."
        python3 "$PACK_DIR/pipeline/vram_watchdog.py" --seconds "$WINDOW" --log "$WATCHDOG_LOG" >>"$LOG" 2>&1
        local rc=$?
        # rc=1 means a conflict was detected
        if [[ $rc -eq 1 ]]; then
            interventions=$((interventions+1))
            log "Intervention #$interventions triggered."
            intervene
            if [[ $MAX_INTV -gt 0 && $interventions -ge $MAX_INTV ]]; then
                log "Reached max interventions ($MAX_INTV) â€” stopping supervision."
                break
            fi
            [[ "$LOOP" == "true" ]] || break
        else
            log "No conflict (watchdog exit=$rc)."
            [[ "$LOOP" == "true" ]] || break
        fi
        # Brief settle before the next window
        sleep 5
    done
    log "VRAM supervisor finished (${interventions} intervention(s))."
}

main
