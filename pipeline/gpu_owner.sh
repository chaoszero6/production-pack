#!/bin/bash
# gpu_owner.sh — hand the RTX 5090 to exactly ONE of three owners.
#
#   gpu_owner llm      Strata LLM (Qwen 3.8 Flash Next)   ← agents reason
#   gpu_owner comfyui  ComfyUI (Qwen Image / MiniMax H3)   ← renders, upscale
#   gpu_owner tts      the four TTS engines (tts-cluster)  ← dialogue / narration / line-up
#   gpu_owner none     everything down
#   gpu_owner status   who owns it now
#
# The hard guarantee lives in systemd (pipeline/systemd/*: Conflicts= between
# comfyui.service, strata-swift-iq3-xxs.service and tts-cluster.service), so
# `systemctl start <owner>` already stops the other two. This helper adds what
# systemd cannot: waiting for the new owner's API (Strata needs ~2 min), and
# the output/.llm_parked marker that tells llm_monitor.py the LLM is down on
# purpose (otherwise it would restart it — and systemd would then stop the
# render that parked it).
#
# Source it (functions) or run it (CLI). Exit 0 = owner healthy.
set -uo pipefail

_GPU_OWNER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_GPU_PACK_DIR="$(cd "$_GPU_OWNER_DIR/.." && pwd)"
GPU_OUT_DIR="${GPU_OUT_DIR:-$_GPU_PACK_DIR/output}"
GPU_LLM_PARKED="$GPU_OUT_DIR/.llm_parked"
COMFYUI_HEALTH="${COMFYUI_HEALTH:-http://127.0.0.1:8188/api/system_stats}"
COMFYUI_START_TIMEOUT="${COMFYUI_START_TIMEOUT:-180}"

# shellcheck source=/dev/null
source "$_GPU_OWNER_DIR/local_llm.sh"

_gpu_log() { echo "[gpu-owner $(date '+%H:%M:%S')] $*"; }

_gpu_park_llm()   { mkdir -p "$GPU_OUT_DIR"; printf '%s %s\n' "$(date +%s)" "${1:-gpu_owner}" > "$GPU_LLM_PARKED"; }
_gpu_unpark_llm() { rm -f "$GPU_LLM_PARKED"; }

comfyui_healthy() { curl -sf --max-time 5 "$COMFYUI_HEALTH" > /dev/null 2>&1; }

gpu_owner_status() {
    local llm comfy tts
    llm=$(llm_healthy && echo healthy || echo "$(systemctl is-active "$LLM_UNIT" 2>/dev/null)")
    comfy=$(comfyui_healthy && echo healthy || echo "$(systemctl is-active comfyui.service 2>/dev/null)")
    tts=$(systemctl is-active tts-cluster.service 2>/dev/null)
    echo "llm=$llm comfyui=$comfy tts-cluster=$tts parked=$([ -f "$GPU_LLM_PARKED" ] && echo yes || echo no) free=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | tr -dc '0-9')MiB"
}

gpu_owner_llm() {
    _gpu_unpark_llm
    # Conflicts= stop ComfyUI and the TTS cluster when the LLM starts; stop them
    # first anyway so a half-dead unit never blocks the start job.
    systemctl stop tts-cluster.service 2>/dev/null || true
    systemctl stop comfyui.service 2>/dev/null || true
    llm_start "${1:-$LLM_START_TIMEOUT}"
}

# The operator's second LLM (OrcaRouter, strata-orca-iq3-xxs, :8322) is not in the systemd
# Conflicts= set of the TTS cluster and ComfyUI: on 2026-10-17 it held 10 GB while the TTS
# cluster started and Chatterbox died with CUDA out-of-memory. Park it like the main LLM.
_gpu_stop_other_llms() {
    if systemctl is-active --quiet strata-orca-iq3-xxs.service 2>/dev/null; then
        _gpu_log "parking strata-orca-iq3-xxs.service (OrcaRouter) — the GPU has one owner"
        systemctl stop strata-orca-iq3-xxs.service 2>/dev/null || true
    fi
}

gpu_owner_comfyui() {
    _gpu_park_llm comfyui
    if comfyui_healthy && ! llm_active; then
        return 0
    fi
    systemctl stop tts-cluster.service 2>/dev/null || true
    llm_stop
    _gpu_stop_other_llms
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
    _gpu_log "WARNING: ComfyUI not healthy after ${COMFYUI_START_TIMEOUT}s"
    return 1
}

gpu_owner_tts() {
    _gpu_park_llm tts
    systemctl stop comfyui.service 2>/dev/null || true
    llm_stop
    _gpu_stop_other_llms
    bash "$_GPU_OWNER_DIR/tts_cluster.sh" start "${1:-240}"
}

gpu_owner_none() {
    _gpu_park_llm none
    systemctl stop tts-cluster.service comfyui.service 2>/dev/null || true
    llm_stop
    _gpu_stop_other_llms
}

gpu_owner() {
    case "${1:-status}" in
        llm)     gpu_owner_llm "${2:-}" ;;
        comfyui) gpu_owner_comfyui ;;
        tts)     gpu_owner_tts "${2:-}" ;;
        none)    gpu_owner_none ;;
        status)  gpu_owner_status ;;
        *) echo "usage: gpu_owner {llm|comfyui|tts|none|status}" >&2; return 2 ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    gpu_owner "$@"
fi
