#!/bin/bash
# Shared helper: park / restore whichever local LLM is actually running.
#
# Why this exists: the wrappers used to hard-code ninfer-us (:8081), but the
# routed provider for this pack is ninfer (:8080). Stopping the wrong unit
# leaves ~28 GB held and the render OOMs; restarting the wrong unit on the way
# out silently kills the provider the pipeline's next agent call needs
# (ninfer and ninfer-us are systemd Conflicts=), producing
# "dsh: TRANSPORT: Connection error" three retries deep.
#
# Source it; it only defines functions.
set -uo pipefail

LLM_GATE_DIR="${LLM_GATE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/output}"
LLM_GATE_STATE="${LLM_GATE_DIR}/.llm_gate.last_unit"

LLM_GATE_UNITS=(ninfer.service ninfer-us.service ninfer-qwen36-35b.service qwen3.8-27b-q6k-cuda.service)
LLM_GATE_DOCKER="qwen38-27b-q6k"
LLM_GATE_PORTS=(8080 8081 8085)

llm_gate_any_healthy() {
    local p
    for p in "${LLM_GATE_PORTS[@]}"; do
        if curl -sf --max-time 3 "http://127.0.0.1:${p}/health" > /dev/null 2>&1; then
            return 0
        fi
    done
    return 1
}

llm_gate_active_unit() {
    local u
    for u in "${LLM_GATE_UNITS[@]}"; do
        if systemctl is-active --quiet "$u" 2>/dev/null; then
            echo "$u"
            return 0
        fi
    done
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${LLM_GATE_DOCKER}"; then
        echo "docker:${LLM_GATE_DOCKER}"
        return 0
    fi
    return 1
}

# Stop every local LLM so ComfyUI gets exclusive VRAM. Remembers what was
# running so llm_gate_restore can put the SAME provider back.
llm_gate_stop() {
    local prev
    prev=$(llm_gate_active_unit || true)
    mkdir -p "$LLM_GATE_DIR"
    printf '%s\n' "${prev:-none}" > "$LLM_GATE_STATE"
    local u
    for u in "${LLM_GATE_UNITS[@]}"; do
        systemctl stop "$u" 2>/dev/null || true
    done
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${LLM_GATE_DOCKER}"; then
        docker stop "$LLM_GATE_DOCKER" > /dev/null 2>&1 || true
    fi
    sleep 3
}

# Restart the provider that was active before llm_gate_stop (default: ninfer,
# which is what pipeline/set_agent_model.py routes to) and wait for health.
llm_gate_restore() {
    local want="${1:-}" waited=0 u
    if [ -z "$want" ] && [ -f "$LLM_GATE_STATE" ]; then
        want=$(tr -d '[:space:]' < "$LLM_GATE_STATE")
    fi
    [ -n "${want:-}" ] && [ "$want" != "none" ] || want="ninfer.service"

    case "$want" in
        docker:*)
            docker start "$LLM_GATE_DOCKER" > /dev/null 2>&1 || true
            ;;
        *)
            systemctl start "$want" 2>/dev/null || true
            ;;
    esac

    while [ "$waited" -lt 180 ]; do
        if llm_gate_any_healthy; then
            echo "[llm-gate] ${want} healthy after ${waited}s"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo "[llm-gate] WARNING: ${want} not healthy after ${waited}s"
    return 1
}
