#!/bin/bash
# llm_ready_notify.sh — "the LLM announces that it is back".
#
# Runs as ExecStartPost of strata-swift-iq3-xxs.service (see
# pipeline/systemd/dropins/strata-swift-iq3-xxs.service.d__20-announce-ready.conf).
# When ComfyUI or the TTS set takes the GPU, the Strata unit is stopped and any
# dsh agent turn that was mid-request dies with "TRANSPORT: Connection error";
# nothing used to tell the pipeline the model was serving again. This script:
#   1. waits (≤ 270 s — inside the unit's TimeoutStartSec=300) for /v1/models,
#   2. writes output/.llm_ready  {"ts", "unit", "took_s", "run"} — the file the
#      pipeline's retry loop (run.sh run_dsh_agent) and studio.py watch for,
#   3. clears output/.llm_parked (the park ended — the unit IS up),
#   4. posts "LLM back — resuming" to Discord via pipeline/notify.sh.
# It never fails the unit: every exit is 0.
set -uo pipefail

PACK_DIR="${PACK_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
OUT="$PACK_DIR/output"
UNIT="${LLM_UNIT:-strata-swift-iq3-xxs.service}"
HEALTH="${LLM_HEALTH_URL:-http://127.0.0.1:8321/v1/models}"
WAIT_MAX="${LLM_READY_WAIT:-270}"
READY="$OUT/.llm_ready"
LOG="$OUT/llm_ready.log"

mkdir -p "$OUT" 2>/dev/null || true
log() { echo "[llm-ready $(date '+%H:%M:%S')] $*" | tee -a "$LOG" 2>/dev/null; }

rm -f "$READY"
t0=$(date +%s)
waited=0
while [ "$waited" -lt "$WAIT_MAX" ]; do
    if curl -sf --max-time 4 "$HEALTH" > /dev/null 2>&1; then
        break
    fi
    sleep 3
    waited=$(( $(date +%s) - t0 ))
done
if ! curl -sf --max-time 4 "$HEALTH" > /dev/null 2>&1; then
    log "$UNIT started but $HEALTH not answering after ${waited}s — llm_monitor will handle it"
    exit 0
fi

run=""
if [ -L "$OUT/run_latest" ]; then
    run="$(readlink -f "$OUT/run_latest" 2>/dev/null || true)"
fi
pipeline_alive="no"
if [ -f "$OUT/run_latest.pid" ] && kill -0 "$(cat "$OUT/run_latest.pid" 2>/dev/null)" 2>/dev/null; then
    pipeline_alive="yes"
fi

printf '{"ts": %s, "iso": "%s", "unit": "%s", "took_s": %s, "run": "%s", "pipeline_alive": "%s"}\n' \
    "$(date +%s)" "$(date '+%Y-%m-%dT%H:%M:%S')" "$UNIT" "$waited" "$run" "$pipeline_alive" > "$READY"
rm -f "$OUT/.llm_parked"
log "$UNIT serving after ${waited}s -> $READY (pipeline alive: $pipeline_alive)"

msg="LLM back: $UNIT answering on $HEALTH after ${waited}s."
if [ "$pipeline_alive" = "yes" ]; then
    msg="$msg Pipeline is running — the interrupted agent turn resumes now."
elif [ -n "$run" ]; then
    msg="$msg No pipeline process — resume with: ./run.sh --resume $run"
fi
if [ -x "$PACK_DIR/pipeline/notify.sh" ] || [ -f "$PACK_DIR/pipeline/notify.sh" ]; then
    bash "$PACK_DIR/pipeline/notify.sh" "$msg" "LLM Ready" > /dev/null 2>&1 || true
fi
exit 0
