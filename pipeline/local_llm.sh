#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# local_llm.sh — the ONE place that knows which local LLM the pack runs on.
#
# Current model: Qwen 3.8 Flash Next 125B (Strata Swift 1.5, IQ3_XXS) served by
#   unit   strata-swift-iq3-xxs.service
#   API    http://127.0.0.1:8321/v1   (OpenAI-compatible; /v1/models = health)
#   model  swift-1.5-iq3_xxs          (dsh provider "strata-swift", text only)
#   VRAM   ~31.8 GB of 32.6 GB — NOTHING else fits on the GPU beside it
#   RAM    ~40 GB of experts pinned
#   start  ~2 minutes (expert load + GPU cache fill); TimeoutStartSec=300
#   note   its ExecStartPre STOPS comfyui.service, so start the LLM FIRST and
#          bring ComfyUI back afterwards (ComfyUI idles at ~0.6 GB).
#
# The retired ninfer / ninfer-us / qwen-q6k units are masked on 207 but are
# still listed in LLM_LEGACY_UNITS so a stale one can never hold VRAM.
#
# Source this file; it only defines variables and functions.
#   llm_healthy          → 0 if the API answers
#   llm_active           → 0 if the systemd unit is active
#   llm_start [timeout]  → start + wait (default 300 s); 0 on healthy
#   llm_stop             → stop the LLM and every legacy unit
#   llm_wait [timeout]   → wait for health without starting
#   llm_restart          → stop + start (crash/hang recovery)
# Override with env: LLM_UNIT, LLM_PORT, LLM_START_TIMEOUT.
# ─────────────────────────────────────────────────────────────────────────────

LLM_UNIT="${LLM_UNIT:-strata-swift-iq3-xxs.service}"
LLM_PORT="${LLM_PORT:-8321}"
LLM_BASE_URL="http://127.0.0.1:${LLM_PORT}"
LLM_HEALTH_URL="${LLM_BASE_URL}/v1/models"
LLM_PROVIDER="${LLM_PROVIDER:-strata-swift}"
LLM_MODEL="${LLM_MODEL:-swift-1.5-iq3_xxs}"
LLM_EFFORT="${LLM_EFFORT:-xhigh}"
LLM_VRAM_MB=31800
LLM_START_TIMEOUT="${LLM_START_TIMEOUT:-300}"   # 2 min typical, 5 min hard cap
LLM_LEGACY_UNITS=(ninfer.service ninfer-us.service ninfer-qwen36-35b.service
                  qwen3.8-27b-q6k-cuda.service qfn-iq3m-cuda.service llama-qwen35-122b.service)
LLM_LEGACY_DOCKER="qwen38-27b-q6k"
LLM_LEGACY_PORTS=(8080 8081 8083 8085)

_llm_log() { echo "[local-llm $(date '+%H:%M:%S')] $*"; }

llm_healthy() {
    curl -sf --max-time 4 "$LLM_HEALTH_URL" > /dev/null 2>&1
}

llm_active() {
    systemctl is-active --quiet "$LLM_UNIT" 2>/dev/null
}

# Seconds since the unit entered the active state ("" if not active).
llm_uptime() {
    local ts epoch
    ts=$(systemctl show -p ActiveEnterTimestamp --value "$LLM_UNIT" 2>/dev/null)
    [ -n "$ts" ] || { echo ""; return; }
    epoch=$(date -d "$ts" +%s 2>/dev/null) || { echo ""; return; }
    echo $(( $(date +%s) - epoch ))
}

llm_wait() {
    local timeout="${1:-$LLM_START_TIMEOUT}" waited=0
    while [ "$waited" -lt "$timeout" ]; do
        if llm_healthy; then
            _llm_log "$LLM_UNIT healthy after ${waited}s"
            return 0
        fi
        # A unit that died during startup will never become healthy — stop waiting.
        if [ "$waited" -gt 15 ] && ! llm_active && \
           [ "$(systemctl is-active "$LLM_UNIT" 2>/dev/null)" != "activating" ]; then
            _llm_log "$LLM_UNIT is $(systemctl is-active "$LLM_UNIT" 2>/dev/null) — gave up after ${waited}s"
            return 1
        fi
        sleep 2
        waited=$((waited + 2))
    done
    _llm_log "WARNING: $LLM_UNIT not healthy after ${timeout}s"
    return 1
}

# Stop every legacy local LLM (never the current one) — frees VRAM held by a
# unit left over from an older layout.
llm_stop_legacy() {
    local u
    for u in "${LLM_LEGACY_UNITS[@]}"; do
        systemctl stop "$u" 2>/dev/null || true
    done
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${LLM_LEGACY_DOCKER}"; then
        docker stop "$LLM_LEGACY_DOCKER" > /dev/null 2>&1 || true
    fi
}

llm_start() {
    local timeout="${1:-$LLM_START_TIMEOUT}"
    if llm_healthy; then
        return 0
    fi
    llm_stop_legacy
    if ! llm_active; then
        _llm_log "starting $LLM_UNIT (takes ~2 min: 40 GB of experts + GPU cache)…"
        systemctl reset-failed "$LLM_UNIT" 2>/dev/null || true
        systemctl start "$LLM_UNIT" 2>/dev/null || true
    else
        _llm_log "$LLM_UNIT active but not answering yet — waiting"
    fi
    llm_wait "$timeout"
}

llm_stop() {
    _llm_log "stopping $LLM_UNIT (freeing ${LLM_VRAM_MB} MB VRAM)"
    systemctl stop "$LLM_UNIT" 2>/dev/null || true
    llm_stop_legacy
    sleep 3
}

llm_restart() {
    local timeout="${1:-$LLM_START_TIMEOUT}"
    _llm_log "restarting $LLM_UNIT"
    systemctl stop "$LLM_UNIT" 2>/dev/null || true
    sleep 3
    llm_start "$timeout"
}
