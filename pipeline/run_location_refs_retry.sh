#!/bin/bash
# Retry wrapper: generate all location references with exclusive GPU.
#
# Same contract as run_location_refs.sh, but with a longer startup grace:
# the launching dsh agent (served by ninfer-us, ~28GB VRAM) needs ~2 LLM
# round-trips after launching this job (process the launch result, then
# issue the blocking job-output wait). 45s grace keeps both inside the
# window before ninfer-us stops. The job's blocking wait needs no LLM,
# and the EXIT trap restores ninfer-us (with health wait) before exit,
# so the agent's next LLM call lands on a healthy service.
#
# The original bash-17 job was killed by its launching session's exit
# before it finished (agent ended its turn without waiting), leaving
# zero location images. This retry runs the same blocking pass to
# completion: stop ninfer-us, generate all 10 location refs, restart it.
set -uo pipefail
PACK=/root/production_pack
RUN_DIR="${1:-$PACK/output/run_20260925_095353}"
NINFER_SVC="ninfer-us.service"
GRACE=45

log() { echo "[loc-retry $(date '+%H:%M:%S')] $*"; }

restart_llm() {
  log "Restarting $NINFER_SVC..."
  systemctl start "$NINFER_SVC"
  for i in $(seq 1 240); do
    if curl -sf http://127.0.0.1:8081/health >/dev/null 2>&1; then
      log "$NINFER_SVC healthy (${i}s)"
      return 0
    fi
    sleep 1
  done
  log "WARNING: $NINFER_SVC not healthy after 240s"
  return 1
}

trap 'restart_llm' EXIT

# Grace: let the launching agent's LLM round-trips land before the stop.
sleep "$GRACE"

log "Stopping $NINFER_SVC to free GPU VRAM..."
systemctl stop "$NINFER_SVC" 2>/dev/null || true
FREE=""
for i in $(seq 1 60); do
  FREE=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null || echo 0)
  [ "${FREE:-0}" -gt 25000 ] && break
  sleep 2
done
log "VRAM free: ${FREE:-?} MiB"

if ! curl -sf http://127.0.0.1:8188/api/system_stats >/dev/null 2>&1; then
  log "ComfyUI down — starting comfyui.service..."
  systemctl start comfyui.service
  for i in $(seq 1 120); do
    curl -sf http://127.0.0.1:8188/api/system_stats >/dev/null 2>&1 && break
    sleep 1
  done
fi

log "Starting generation"
python3 "$PACK/pipeline/generate_location_refs.py" --run-dir "$RUN_DIR"
rc=$?
log "generation exit code: $rc"
exit $rc
