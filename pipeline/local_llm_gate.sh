#!/bin/bash
# Shared helper: park / restore the local LLM around exclusive-GPU work.
#
# The local LLM (Strata Swift, Qwen 3.8 Flash Next — see pipeline/local_llm.sh)
# holds ~31.8 GB of the 32.6 GB card, so any ComfyUI render or Orpheus
# synthesis must park it first and bring it back afterwards (≈2 min).
# While parked, output/.llm_parked tells llm_monitor.py NOT to restart it.
#
# Source it; it only defines functions.
set -uo pipefail

LLM_GATE_DIR="${LLM_GATE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/output}"
LLM_GATE_STATE="${LLM_GATE_DIR}/.llm_gate.last_unit"
LLM_GATE_PARKED="${LLM_GATE_DIR}/.llm_parked"

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/local_llm.sh"

llm_gate_any_healthy() {
    llm_healthy && return 0
    local p
    for p in "${LLM_LEGACY_PORTS[@]}"; do
        if curl -sf --max-time 3 "http://127.0.0.1:${p}/health" > /dev/null 2>&1; then
            return 0
        fi
    done
    return 1
}

llm_gate_active_unit() {
    if llm_active; then
        echo "$LLM_UNIT"
        return 0
    fi
    local u
    for u in "${LLM_LEGACY_UNITS[@]}"; do
        if systemctl is-active --quiet "$u" 2>/dev/null; then
            echo "$u"
            return 0
        fi
    done
    return 1
}

# Stop the local LLM so ComfyUI / Orpheus get exclusive VRAM. Remembers what
# was running so llm_gate_restore can put the SAME provider back, and marks
# the GPU as deliberately parked for the monitor.
llm_gate_stop() {
    local prev
    prev=$(llm_gate_active_unit || true)
    mkdir -p "$LLM_GATE_DIR"
    printf '%s
' "${prev:-none}" > "$LLM_GATE_STATE"
    touch "$LLM_GATE_PARKED"
    llm_stop
}

# Restart the provider that was active before llm_gate_stop (default: the
# current local LLM) and wait for health (≈2 min for Strata).
llm_gate_restore() {
    local want="${1:-}" rc=0
    if [ -z "$want" ] && [ -f "$LLM_GATE_STATE" ]; then
        want=$(tr -d '[:space:]' < "$LLM_GATE_STATE")
    fi
    [ -n "${want:-}" ] && [ "$want" != "none" ] || want="$LLM_UNIT"
    rm -f "$LLM_GATE_PARKED"
    if [ "$want" = "$LLM_UNIT" ]; then
        llm_start || rc=1
    else
        systemctl start "$want" 2>/dev/null || true
        local waited=0
        while [ "$waited" -lt 180 ]; do
            llm_gate_any_healthy && { echo "[llm-gate] ${want} healthy after ${waited}s"; return 0; }
            sleep 2; waited=$((waited + 2))
        done
        echo "[llm-gate] WARNING: ${want} not healthy after ${waited}s"
        rc=1
    fi
    return $rc
}
