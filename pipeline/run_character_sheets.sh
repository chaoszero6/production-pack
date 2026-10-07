#!/bin/bash
# Run Qwen Image 2.1 character reference sheet generation with exclusive GPU.
#
# ComfyUI needs ~14 GB (qwen_image_2.1_bf16) + T5-XXL at 2048x2048, and the
# local LLM holds ~28 GB on the RTX 5090, so the LLM must be parked for the
# duration. pipeline/local_llm_gate.sh stops whichever provider is ACTIVE and
# restores that same one on exit â€” the old code hard-coded ninfer-us, which
# either left ninfer holding 28 GB (render OOM) or restarted the wrong unit and
# killed the provider set_agent_model.py routes to (ninfer/ninfer-us are
# systemd Conflicts=), so the pipeline's next agent turn died with
# "dsh: TRANSPORT: Connection error".
set -uo pipefail
PACK="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${1:-$PACK/output/run_20260925_095353}"

source "$PACK/pipeline/local_llm_gate.sh"

log() { echo "[char-run $(date '+%H:%M:%S')] $*"; }

restore_llm() {
    log "Restoring local LLM..."
    llm_gate_restore
}
trap restore_llm EXIT

if ! curl -sf --max-time 5 http://127.0.0.1:8188/api/system_stats > /dev/null 2>&1; then
    log "ComfyUI down â€” starting comfyui.service..."
    systemctl start comfyui.service 2>/dev/null || true
    for _i in $(seq 1 120); do
        curl -sf --max-time 5 http://127.0.0.1:8188/api/system_stats > /dev/null 2>&1 && break
        sleep 1
    done
fi

log "Parking local LLM to free GPU VRAM..."
llm_gate_stop
FREE=""
for _i in $(seq 1 60); do
    FREE=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | tr -dc '0-9')
    [ "${FREE:-0}" -gt 25000 ] && break
    sleep 2
done
log "VRAM free: ${FREE:-?} MiB â€” starting generation"

python3 "$PACK/pipeline/generate_character_sheets.py" --run-dir "$RUN_DIR"
rc=$?
log "generation exit code: $rc"
exit $rc
