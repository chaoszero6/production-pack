#!/bin/bash
# tts_cluster.sh — start / stop / wait for the four TTS engines as ONE set.
#
# Used by tts-cluster.service (ExecStart = wait, ExecStop = stop) and by the
# pipeline (gpu_owner.sh tts). The cluster is one of three exclusive GPU owners
# (TTS | ComfyUI | Strata LLM); systemd Conflicts= in pipeline/systemd/ make
# starting any one of them stop the other two.
#
#   tts_cluster.sh start [timeout]   systemctl start tts-cluster (members via Wants=) + wait
#   tts_cluster.sh wait  [timeout]   wait until every engine answers (default 240 s)
#   tts_cluster.sh stop              stop every engine
#   tts_cluster.sh status            one line per engine
set -uo pipefail

TTS_UNITS=(orpheus-backend.service orpheus-tts.service chatterbox-tts.service cosyvoice.service kokoro-tts.service)
# unit → health URL (CosyVoice runs its Gradio webui on :50000; the pack's
# config/pipeline.yml still lists 9880 for a REST wrapper that is not deployed)
declare -A TTS_HEALTH=(
    [orpheus-backend.service]="http://127.0.0.1:9884/v1/models"
    [orpheus-tts.service]="http://127.0.0.1:9883/v1/audio/voices"   # no /v1/models on Orpheus-FastAPI
    [chatterbox-tts.service]="http://127.0.0.1:9882/health"
    [cosyvoice.service]="http://127.0.0.1:50000/"
    [kokoro-tts.service]="http://127.0.0.1:9881/health"
)
# Engines the pipeline cannot do without (Orpheus = dialogue, Kokoro = narration fallback).
TTS_REQUIRED=(orpheus-tts.service chatterbox-tts.service kokoro-tts.service)

_log() { echo "[tts-cluster $(date '+%H:%M:%S')] $*"; }

tts_unit_healthy() { curl -sf --max-time 4 "${TTS_HEALTH[$1]}" > /dev/null 2>&1; }

tts_status() {
    local u
    for u in "${TTS_UNITS[@]}"; do
        printf "  %-26s %-9s %s\n" "$u" "$(systemctl is-active "$u" 2>/dev/null)" \
            "$(tts_unit_healthy "$u" && echo healthy || echo no-answer)"
    done
}

tts_all_required_healthy() {
    local u
    for u in "${TTS_REQUIRED[@]}"; do
        tts_unit_healthy "$u" || return 1
    done
    return 0
}

tts_wait() {
    local timeout="${1:-240}" waited=0
    while [ "$waited" -lt "$timeout" ]; do
        if tts_all_required_healthy; then
            _log "required engines healthy after ${waited}s"
            tts_status
            return 0
        fi
        sleep 3
        waited=$((waited + 3))
    done
    _log "WARNING: not every required engine answered within ${timeout}s"
    tts_status
    return 1
}

tts_start() {
    local timeout="${1:-240}"
    if systemctl list-unit-files tts-cluster.service --no-legend 2>/dev/null | grep -q .; then
        _log "starting tts-cluster.service (Conflicts= stops ComfyUI + the LLM)"
        systemctl reset-failed tts-cluster.service "${TTS_UNITS[@]}" 2>/dev/null || true
        systemctl start tts-cluster.service 2>/dev/null || true
    else
        _log "tts-cluster.service not installed — starting members directly"
        systemctl stop comfyui.service strata-swift-iq3-xxs.service 2>/dev/null || true
        systemctl start orpheus-backend.service 2>/dev/null || true
        sleep 3
        local u
        for u in orpheus-tts.service chatterbox-tts.service cosyvoice.service kokoro-tts.service; do
            systemctl start "$u" 2>/dev/null || true
        done
    fi
    tts_wait "$timeout"
}

tts_stop() {
    _log "stopping all TTS engines"
    systemctl stop orpheus-tts.service orpheus-backend.service chatterbox-tts.service cosyvoice.service kokoro-tts.service 2>/dev/null || true
    sleep 2
}

case "${1:-status}" in
    start)  tts_start "${2:-240}" ;;
    wait)   tts_wait "${2:-240}" ;;
    stop)   tts_stop ;;
    status) tts_status ;;
    *) echo "usage: $0 {start|wait|stop|status} [timeout]"; exit 2 ;;
esac
