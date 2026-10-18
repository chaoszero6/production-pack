#!/bin/bash
# Run Qwen Image 2.1 reference generation on ComfyUI (exclusive GPU).
#
# No local LLM exists any more (2026-10-18): ComfyUI is the GPU owner during
# Phase 1, so this script only makes sure ComfyUI is up and no TTS engine holds
# VRAM, then renders. A failure is reported as a non-zero exit — no fallback.
set -uo pipefail
PACK="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${1:-$PACK/output/run_20260925_095353}"

# shellcheck source=/dev/null
source "$PACK/pipeline/gpu_owner.sh"

log() { echo "[loc-run $(date '+%H:%M:%S')] $*"; }

log "Handing the GPU to ComfyUI..."
gpu_owner comfyui || { log "ERROR: ComfyUI not healthy"; exit 3; }
FREE=""
for _i in $(seq 1 60); do
    FREE=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | tr -dc '0-9')
    [ "${FREE:-0}" -gt 25000 ] && break
    sleep 2
done
log "VRAM free: ${FREE:-?} MiB â€” starting generation"

python3 "$PACK/pipeline/generate_location_refs.py" --run-dir "$RUN_DIR"
rc=$?
log "generation exit code: $rc"
exit $rc
