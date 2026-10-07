#!/usr/bin/env bash
# upscale_clip.sh — re-run ONLY the 4K60 GPU upscale step for a single clip.
#
# WHY THIS EXISTS
# run.sh performs the 4K60 upscale *inside* the per-clip loop, so a clip that
# already has a PASS verdict but is missing its master (or was upscaled with the
# old CPU path) can be finished without re-entering prompt/image/video/QA.
#
# Usage: upscale_clip.sh <run_dir> <SHOT_ID> [--force]
# Exit codes: 0 = upscaled, 2 = no source clip, 3 = ComfyUI unreachable, 4 = fell back
set -uo pipefail

PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMFYUI_URL="http://127.0.0.1:8188"
SVC_27B="ninfer-us.service"
SVC_NINFER_TEXTONLY="ninfer.service"
SVC_122B="llama-qwen35-122b.service"
SVC_COMFYUI="comfyui.service"

RUN_DIR="${1:?usage: upscale_clip.sh <run_dir> <SHOT_ID> [--force]}"
SHOT_ID="${2:?usage: upscale_clip.sh <run_dir> <SHOT_ID> [--force]}"
FORCE="${3:-}"
CLIP_DIR="$RUN_DIR/clips/$SHOT_ID"

log() { echo "[$(date +%H:%M:%S)] UPSCALE-ONLY $SHOT_ID: $*" | tee -a "$RUN_DIR/pipeline.log"; }

[ -f "$CLIP_DIR/clip.mp4" ] || { log "ERROR no clip.mp4 in $CLIP_DIR"; exit 2; }

# Free VRAM for RTX VSR + RIFE (ComfyUI stays up)
log "Stopping LLM for VRAM..."
systemctl stop "$SVC_122B" 2>/dev/null || true
systemctl stop "$SVC_27B" 2>/dev/null || true
systemctl stop "$SVC_NINFER_TEXTONLY" 2>/dev/null || true
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^qwen38'; then
    docker stop qwen38-27b-q6k > /dev/null 2>&1 || true
fi
sleep 2

if ! curl -sf "$COMFYUI_URL/system_stats" >/dev/null; then
    log "ComfyUI down — starting"
    systemctl start "$SVC_COMFYUI"
    for i in $(seq 1 120); do
        curl -sf "$COMFYUI_URL/system_stats" >/dev/null && break
        sleep 1
    done
fi
curl -sf "$COMFYUI_URL/system_stats" >/dev/null || { log "ERROR ComfyUI unreachable"; exit 3; }

UPS_ARGS=(--shot-dir "$CLIP_DIR" --comfyui-url "$COMFYUI_URL")
[ "$FORCE" = "--force" ] && UPS_ARGS+=(--force)

if python3 "$PACK_DIR/pipeline/upscale_clip.py" "${UPS_ARGS[@]}" 2>> "$RUN_DIR/pipeline.log"; then
    log "OK clip_4k60.mp4 $(du -h "$CLIP_DIR/clip_4k60.mp4" | cut -f1) (GPU)"
    systemctl start "$SVC_27B" 2>/dev/null || true
    exit 0
fi

log "GPU upscale failed — CPU ffmpeg fallback"
if ffmpeg -y -v error \
    -i "$CLIP_DIR/clip.mp4" \
    -vf "minterpolate=fps=60:mi_mode=mci:mc_mode=aobmc:vsbmc=1:me_mode=bidir:me=epzs,format=yuv420p,scale=3840:2160:flags=lanczos" \
    -c:v libx264 -preset medium -crf 17 -c:a aac \
    "$CLIP_DIR/clip_4k60.mp4"; then
    log "OK clip_4k60.mp4 (CPU fallback)"
    systemctl start "$SVC_27B" 2>/dev/null || true
    exit 0
fi

log "FALLBACK_COPY_USED — copying clip.mp4 (LOW-RES master)"
cp "$CLIP_DIR/clip.mp4" "$CLIP_DIR/clip_4k60.mp4"
systemctl start "$SVC_27B" 2>/dev/null || true
exit 4
