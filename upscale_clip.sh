#!/usr/bin/env bash
# upscale_clip.sh — re-run ONLY the 4K GPU master step for a single clip.
#
# WHY THIS EXISTS
# run.sh builds the 4K master (clips/<SID>/clip_4k.mp4, RTX VSR; RIFE only when
# PP_TARGET_FPS asks for more than the source rate) after the clip loop, so a
# clip that already has a PASS verdict but is missing its master can be
# finished without re-entering prompt/image/video/QA.
#
# Usage: upscale_clip.sh <run_dir> <SHOT_ID> [--force]
# Exit codes: 0 = master written, 2 = no source clip, 3 = ComfyUI unreachable,
#             4 = GPU upscale failed (no CPU fallback, no low-res copy — fix and re-run)
set -uo pipefail

PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMFYUI_URL="http://127.0.0.1:8188"
# The GPU is owned by exactly one of {ComfyUI | TTS cluster} (systemd Conflicts=);
# gpu_owner.sh switches owner and waits for health. There is no local LLM.
# shellcheck source=/dev/null
source "$PACK_DIR/pipeline/gpu_owner.sh"

RUN_DIR="${1:?usage: upscale_clip.sh <run_dir> <SHOT_ID> [--force]}"
SHOT_ID="${2:?usage: upscale_clip.sh <run_dir> <SHOT_ID> [--force]}"
FORCE="${3:-}"
CLIP_DIR="$RUN_DIR/clips/$SHOT_ID"

log() { echo "[$(date +%H:%M:%S)] UPSCALE-ONLY $SHOT_ID: $*" | tee -a "$RUN_DIR/pipeline.log"; }

[ -f "$CLIP_DIR/clip.mp4" ] || { log "ERROR no clip.mp4 in $CLIP_DIR"; exit 2; }

log "Handing the GPU to ComfyUI for RTX VSR..."
gpu_owner comfyui || { log "ERROR: ComfyUI did not report healthy"; exit 3; }
curl -sf "$COMFYUI_URL/system_stats" >/dev/null || { log "ERROR ComfyUI unreachable"; exit 3; }

UPS_ARGS=(--shot-dir "$CLIP_DIR" --comfyui-url "$COMFYUI_URL")
[ "$FORCE" = "--force" ] && UPS_ARGS+=(--force)

if python3 "$PACK_DIR/pipeline/upscale_clip.py" "${UPS_ARGS[@]}" 2>> "$RUN_DIR/pipeline.log"; then
    log "OK clip_4k.mp4 $(du -h "$CLIP_DIR/clip_4k.mp4" | cut -f1) (GPU)"
    exit 0
fi

log "ERROR GPU upscale failed — no fallback (see pipeline.log; re-run after fixing ComfyUI)"
exit 4
