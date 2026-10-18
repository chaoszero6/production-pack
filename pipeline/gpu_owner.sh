#!/bin/bash
# gpu_owner.sh — hand the RTX 5090 to exactly ONE of two owners.
#
#   gpu_owner comfyui  ComfyUI (Qwen Image / MiniMax H3 / RTX VSR / ACE-Step)  ← renders
#   gpu_owner tts      the TTS engines (tts-cluster)                            ← dialogue / narration / line-up
#   gpu_owner none     everything down
#   gpu_owner status   who owns it now
#
# There is NO local LLM any more (decision 2026-10-18): every agent runs in the
# cloud, so the GPU alternates only between ComfyUI and the TTS cluster. The
# retired LLM units (Strata, OrcaRouter, ninfer, …) are stopped before every
# hand-over so a stale one can never hold VRAM.
#
# The hard guarantee lives in systemd (pipeline/systemd/*: Conflicts= between
# comfyui.service and tts-cluster.service); this helper adds what systemd
# cannot: waiting for the new owner's API.
#
# Source it (functions) or run it (CLI). Exit 0 = owner healthy.
set -uo pipefail

_GPU_OWNER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_GPU_PACK_DIR="$(cd "$_GPU_OWNER_DIR/.." && pwd)"
GPU_OUT_DIR="${GPU_OUT_DIR:-$_GPU_PACK_DIR/output}"
COMFYUI_HEALTH="${COMFYUI_HEALTH:-http://127.0.0.1:8188/api/system_stats}"
COMFYUI_START_TIMEOUT="${COMFYUI_START_TIMEOUT:-180}"

# Retired LLM units — never started by the pack, always stopped before a hand-over.
GPU_RETIRED_LLM_UNITS=(strata-swift-iq3-xxs.service strata-orca-iq3-xxs.service
                       ninfer.service ninfer-us.service ninfer-qwen36-35b.service
                       qwen3.8-27b-q6k-cuda.service qfn-iq3m-cuda.service)

_gpu_log() { echo "[gpu-owner $(date '+%H:%M:%S')] $*"; }

comfyui_healthy() { curl -sf --max-time 5 "$COMFYUI_HEALTH" > /dev/null 2>&1; }

_gpu_stop_llms() {
    local u
    for u in "${GPU_RETIRED_LLM_UNITS[@]}"; do
        if systemctl is-active --quiet "$u" 2>/dev/null || systemctl is-activating --quiet "$u" 2>/dev/null; then
            _gpu_log "stopping retired LLM unit $u (no local LLM in the pipeline)"
            systemctl stop "$u" 2>/dev/null || true
        fi
    done
    # OrcaRouter may run outside systemd (held 10 GB while the TTS cluster started).
    pkill -f 'strata.*orca' 2>/dev/null || true
}

gpu_owner_status() {
    local comfy tts
    comfy=$(comfyui_healthy && echo healthy || echo "$(systemctl is-active comfyui.service 2>/dev/null)")
    tts=$(systemctl is-active tts-cluster.service 2>/dev/null)
    echo "comfyui=$comfy tts-cluster=$tts free=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | tr -dc '0-9')MiB"
}

gpu_owner_comfyui() {
    _gpu_stop_llms
    if comfyui_healthy && ! systemctl is-active --quiet tts-cluster.service 2>/dev/null; then
        return 0
    fi
    systemctl stop tts-cluster.service 2>/dev/null || true
    _gpu_log "starting comfyui.service"
    systemctl reset-failed comfyui.service 2>/dev/null || true
    systemctl start comfyui.service 2>/dev/null || true
    local waited=0
    while [ "$waited" -lt "$COMFYUI_START_TIMEOUT" ]; do
        if comfyui_healthy; then
            _gpu_log "ComfyUI healthy after ${waited}s"
            return 0
        fi
        sleep 2; waited=$((waited + 2))
    done
    _gpu_log "ERROR: ComfyUI not healthy after ${COMFYUI_START_TIMEOUT}s"
    return 1
}

gpu_owner_tts() {
    _gpu_stop_llms
    systemctl stop comfyui.service 2>/dev/null || true
    bash "$_GPU_OWNER_DIR/tts_cluster.sh" start "${1:-240}"
}

gpu_owner_none() {
    _gpu_stop_llms
    systemctl stop tts-cluster.service comfyui.service 2>/dev/null || true
}

gpu_owner() {
    case "${1:-status}" in
        comfyui) gpu_owner_comfyui ;;
        tts)     gpu_owner_tts "${2:-}" ;;
        none)    gpu_owner_none ;;
        status)  gpu_owner_status ;;
        llm)     _gpu_log "ERROR: there is no local LLM owner any more (agents run in the cloud)"; return 2 ;;
        *) echo "usage: gpu_owner {comfyui|tts|none|status}" >&2; return 2 ;;
    esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    gpu_owner "$@"
fi
