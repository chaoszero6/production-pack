#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
# Production Pack — End-to-End Movie Generation Pipeline
# Usage: ./run.sh <story.md>
#
# Takes a story markdown file and produces a complete Pixar-style
# animated movie with dialogue (lip-synced), narration, BGM,
# subtitles, upscaled to 4K 60fps.
#
# Server: RTX 5090 32GB, single LLM at a time (port 8085)
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

# ── dsh FILE SANDBOX ────────────────────────────────────────────────────────
# dsh derives its filesystem sandbox from this env var; see
# packages/bundle/base/cordis.patch.yml in the harness:
#     mode:            process.env.DSH_PERMISSION_MODE ?? 'workspace-write'
#     workspaceRoot:   process.cwd()   -> $DSH_DIR, because run_dsh_agent cds there
#     approval policy: 'never' iff danger-full-access, else 'ask'
# Under the default every agent may write ONLY inside $DSH_DIR, so each write to
# $RUN_DIR is denied -- and because the headless profile has no approval channel,
# the sanctioned escalation to danger-full-access is rejected outright
# ("sandbox escalation ... requires approval, but no approval channel is
# available"), leaving the agent no way to recover. The director then wrote its
# shot list to $DSH_DIR/shot_list.json and returned a PROSE summary ("Done. The
# complete shot list is written to ...") which run_dsh_agent captured as the
# artifact -- that is the real cause of
#     "could not parse shot list: Expecting value: line 1 column 1 (char 0)"
# (Python failing to parse the word "Done."). The character- and location-
# designer hit the same wall and wrote their PNGs to /tmp instead.
# Grant full access up front: the pack must write into $RUN_DIR and headless can
# never prompt for it. Agents here are already trusted to run bash, so this
# widens nothing that was not already reachable; it only stops the silent
# prose-instead-of-JSON skew.
export DSH_PERMISSION_MODE="${DSH_PERMISSION_MODE:-danger-full-access}"

# Resolve from this script's location so the pack runs from any checkout.
PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DSH_DIR="${DSH_DIR:-/root/desktop/deepseek-harness}"
COMFYUI_DIR="/opt/comfyui"
OUTPUT_DIR="$PACK_DIR/output"
COMFYUI_URL="http://127.0.0.1:8188"

# ── The GPU has exactly ONE owner at a time: Strata LLM | ComfyUI | TTS cluster ──
# systemd Conflicts= (pipeline/systemd/) enforce it; these helpers switch owner
# and wait for the new one's API. The local LLM is Qwen 3.8 Flash Next via
# Strata Swift (unit/port/model in pipeline/local_llm.sh — ~2 min to start).
# shellcheck source=/dev/null
source "$PACK_DIR/pipeline/local_llm.sh"
# shellcheck source=/dev/null
source "$PACK_DIR/pipeline/gpu_owner.sh"
LLM_URL="$LLM_BASE_URL"

# LLM routing: all providers are configurable at startup via environment variables.
# Default is OpenRouter for all tiers. Override any tier independently:
#
#   DSH_REASONING_PROVIDER=openrouter         # reasoning agents (director, story-creator, etc.)
#   DSH_REASONING_MODEL="deepseek/deepseek-v4-pro"  # model name on that provider
#   DSH_REASONING_EFFORT=high                 # off|low|medium|high|max|xhigh
#
#   DSH_EXECUTION_PROVIDER=openrouter         # execution agents (image-gen, video-gen, audio-producer)
#   DSH_EXECUTION_MODEL="deepseek/deepseek-v4.1-flash"
#   DSH_EXECUTION_EFFORT=low
#
#   DSH_VISION_PROVIDER=openrouter            # qa-inspector (needs pixel vision)
#   DSH_VISION_MODEL="google/gemini-3-flash-preview"
#   DSH_VISION_EFFORT=high
#
# Examples:
#   DSH_REASONING_PROVIDER=tokenharbor DSH_EXECUTION_PROVIDER=tokenharbor ./run.sh story.md
#   DSH_REASONING_PROVIDER=opencode-go-deepseek-pro ./run.sh --resume
#   export DSH_*=tokenharbor  # use Token Harbor for everything
DSH_SETTINGS="/root/.dsh/settings.yaml"
# Still copy .env for any cloud fallback tools the agents might use
if [ -f /root/.hermes/.env ]; then
    cp /root/.hermes/.env "$DSH_DIR/.env" 2>/dev/null || true
fi

SVC_122B="llama-qwen35-122b.service"
SVC_27B="qwen3.8-27b-q6k-cuda.service"
SVC_COMFYUI="comfyui.service"
SVC_HERMES="hermes-stack.service"

WAIT_122B=60
WAIT_27B=30

DISCORD_CHANNEL="discord:#film-maker"
NOTIFY="$PACK_DIR/pipeline/notify.sh"

# ── Phase-based Service Management ──────────────────────────
# Sourced by pipeline runs. Provides:
#   svc_transition <phase>  — atomic stop-all/start-phase-services
#   svc_is_healthy <svc>    — health check
# All older stop/ensure functions below delegate to these.
source "$PACK_DIR/pipeline/services.sh" 2>/dev/null || true

VRAM_MONITOR="${PACK_DIR}/pipeline/vram_monitor.sh"
# vram_report / vram_check_now are DEFINED here, not called — without sourcing,
# every agent call logged "command not found" at run.sh:716/724.
# The file has an entry-point guard, so sourcing only declares functions/vars.
if [[ -f "$VRAM_MONITOR" ]]; then
    # shellcheck source=/dev/null
    source "$VRAM_MONITOR" 2>/dev/null || true
fi

# ── Helpers ─────────────────────────────────────────────────
log()  { echo "[$(date '+%H:%M:%S')] $*"; }
die()  { log "ERROR: $*"; notify "ERROR: $*"; exit 1; }
step() { echo; echo "════════════════════════════════════════"; log "STEP: $*"; echo "════════════════════════════════════════"; }
notify() { bash "$NOTIFY" "$1" "${2:-}" "${3:-}" 2>/dev/null & }

wait_for_health() {
    local url="$1" timeout="$2" name="$3"
    log "Waiting for $name (max ${timeout}s)..."
    for i in $(seq 1 "$timeout"); do
        if curl -sf "$url" > /dev/null 2>&1; then
            log "$name ready (${i}s)"
            return 0
        fi
        sleep 1
    done
    die "$name failed to start within ${timeout}s"
}

# Non-fatal wait — used by retry loops that must not abort the whole run.
_wait_url() {
    local url="$1" timeout="$2" name="$3" i
    log "Waiting for $name (max ${timeout}s)..."
    for i in $(seq 1 "$timeout"); do
        if curl -sf --max-time 5 "$url" > /dev/null 2>&1; then
            log "$name ready (${i}s)"
            return 0
        fi
        sleep 1
    done
    log "$name NOT ready after ${timeout}s"
    return 1
}

# ── Local LLM lifecycle for agent turns ─────────────────────
# Every dsh agent is served by the local NInfer model, so an agent turn must
# ALWAYS have an LLM up. What it must never do is stop ComfyUI: ComfyUI idles
# at ~0.6 GB (models unload via POST /free), and killing it mid-turn is what
# made `wait_for_health ComfyUI` time out and kill a run. Heavy GPU work
# (ComfyUI renders, TTS synthesis) is serialized outside agent turns by the
# wrapper scripts in pipeline/, not by stopping the LLM the agent is using.
# llm_healthy / llm_start / llm_stop come from pipeline/local_llm.sh.

# Drop ComfyUI's loaded models (Qwen Image 2.1 ~10 GB / H3 ~12-14 GB) without
# touching the server process. Safe to call when ComfyUI is down.
comfyui_release_models() {
    curl -sf --max-time 10 -X POST "$COMFYUI_URL/free" \
        -H 'Content-Type: application/json' \
        -d '{"unload_models":true,"free_memory":true}' > /dev/null 2>&1 || true
}

ensure_agent_llm() {
    if [[ "${CLOUD_ROUTING:-1}" == "1" ]]; then
        return 0
    fi
    if llm_healthy; then
        return 0
    fi
    # Hand the GPU to the LLM: Conflicts= stop ComfyUI / the TTS cluster, the
    # Strata engine loads 40 GB of experts (~2 min), gpu_owner waits for /v1/models.
    log "Local LLM not serving — handing the GPU to $LLM_UNIT (≈2 min)..."
    local attempt
    for attempt in 1 2; do
        if gpu_owner llm; then
            return 0
        fi
        log "$LLM_UNIT not healthy (attempt $attempt/2) — resetting and retrying"
        systemctl reset-failed "$LLM_UNIT" 2>/dev/null || true
        sleep 5
    done
    die "No local LLM could be started for agent turn ($LLM_UNIT :$LLM_PORT unhealthy — journalctl -u $LLM_UNIT)"
}

# ── Phase-aware service management (wrappers for run.sh compatibility) ──
# These preserve the original function signatures used throughout run.sh
# but delegate to the centralized phase-based service manager.
#
# Under CLOUD_ROUTING=1 (default): all local-service functions are no-ops
# because agents use OpenRouter cloud. Only ComfyUI management applies.
#
# Under CLOUD_ROUTING=0: full VRAM-aware lifecycle via svc_transition.

ensure_local_llm_running() {
    # Under CLOUD_ROUTING=0 the agent is served by NInfer, so this must make
    # the LLM healthy WITHOUT disturbing ComfyUI — the old body called
    # svc_transition "thinking", which stops ComfyUI right before steps that
    # need it (image generation, QA frame extraction).
    ensure_agent_llm
}

_legacy_start_llm() {
    gpu_owner llm || die "No local LLM started within timeout"
}

stop_all_llms() {
    if [[ "${CLOUD_ROUTING:-1}" == "1" ]]; then
        return 0
    fi
    # Park the LLM (marks output/.llm_parked so llm_monitor leaves it down).
    _gpu_park_llm run.sh
    llm_stop
}

_legacy_stop_all_llms() {
    _gpu_park_llm run.sh
    llm_stop
    systemctl stop orpheus-tts.service 2>/dev/null || true
    systemctl stop orpheus-backend.service 2>/dev/null || true
}

swap_to_122b()  { ensure_local_llm_running; }
swap_to_27b()   { ensure_local_llm_running; }

stop_all_llms_for_generation() {
    if [[ "${CLOUD_ROUTING:-1}" == "1" ]]; then
        ensure_comfyui
        return 0
    fi
    log "Handing the GPU to ComfyUI (parks the LLM)..."
    ensure_comfyui
}

ensure_comfyui() {
    # ComfyUI and the LLM are exclusive GPU owners: starting ComfyUI stops the
    # LLM (Conflicts=). Never call this inside an agent turn — the agent is
    # served by that LLM. gpu_owner marks the LLM parked and waits for :8188.
    local attempt
    if curl -sf --max-time 5 "$COMFYUI_URL/api/system_stats" > /dev/null 2>&1 && ! llm_active; then
        return 0
    fi
    for attempt in 1 2 3; do
        log "Handing the GPU to ComfyUI (attempt $attempt/3)..."
        if gpu_owner comfyui; then
            return 0
        fi
        systemctl restart "$SVC_COMFYUI" 2>/dev/null || true
        sleep 3
    done
    die "ComfyUI failed to start within $((COMFYUI_START_TIMEOUT * 3))s — check: journalctl -u $SVC_COMFYUI -n 50"
}

stop_hermes() {
    # Stop Hermes stack if running (it uses the same LLM port)
    systemctl stop "$SVC_HERMES" 2>/dev/null || true
    # Also stop the user-level gateway
    XDG_RUNTIME_DIR="/run/user/0" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/0/bus" \
        systemctl --user stop hermes-gateway 2>/dev/null || true
}

start_hermes() {
    # Restart Hermes with qwen3.8-27b after production
    log "Starting Hermes stack..."
    systemctl start "$SVC_HERMES" 2>/dev/null || true
}

# ── Routing: reasoning + execution on the LOCAL Strata LLM, vision on cloud ──
# All text agents run on Qwen 3.8 Flash Next (strata-swift / swift-1.5-iq3_xxs,
# effort xhigh, maxTokens 131072). Only qa-inspector goes to OpenRouter
# (gemini-3-flash-preview) because Strata is text-only. Per-agent table lives
# in pipeline/set_agent_model.py; GPU ownership in pipeline/gpu_owner.sh.
export CLOUD_ROUTING="0"

LOCAL_MODEL_PROVIDER="$LLM_PROVIDER"
LOCAL_MODEL_ID="$LLM_MODEL"
LOCAL_MODEL_EFFORT="$LLM_EFFORT"

set_agent_model() {
    local preset="$1"
    # Always use set_agent_model.py — it dynamically picks the healthy provider
    # (cloud or local) and writes the route atomically. The old CLOUD_ROUTING=0
    # path hardcoded LOCAL_MODEL_PROVIDER which didn't match the running service.
    local route
    route=$(python3 "$PACK_DIR/pipeline/set_agent_model.py" "$preset") || {
        die "set_agent_model failed for preset '$preset'"
    }
    log "Model route: $route"
}

# Last-resort fallback when an agent emitted no parseable JSON: keep its raw
# stdout as the artifact. For .json outputs only accept raw stdout that really
# IS json — a crashed agent's console output ("[ELIFECYCLE] Command failed...")
# is 47 bytes and would otherwise be frozen in place by skip_if_done() on every
# later resume (that is exactly how character_manifest.json, location_manifest.json
# and voice_config.json ended up unparsable).
# Always returns 0: this runs under `set -e`, and the caller's own
# `[ ! -s "$output_file" ]` check is what turns a missing artifact into a failure.
store_raw_artifact() {
    local preset="$1" raw="$2" out="$3"
    case "$out" in
        *.json)
            if python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$raw" >/dev/null 2>&1; then
                cp "$raw" "$out" 2>/dev/null || true
                log "WARNING: $preset raw stdout is valid JSON — kept as $out"
                return 0
            fi
            if [ -s "$out" ] && ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$out" >/dev/null 2>&1; then
                mv -f "$out" "${out%.json}_bad_$(date +%s).json" 2>/dev/null || true
            fi
            log "WARNING: $preset emitted no parseable JSON and raw stdout is not JSON — leaving $out absent so resume retries"
            return 0
            ;;
        *)
            cp "$raw" "$out" 2>/dev/null || true
            return 0
            ;;
    esac
}

_run_dsh_agent_internal() {
    local preset="$1" prompt_file="$2" output_file="$3"
    log "Running agent: $preset"
    local prompt_text
    prompt_text=$(cat "$prompt_file")

    cd "$DSH_DIR"
    # dsh headless: takes task as positional arg, prints final answer to stdout
    # For story-creator: extract valid JSON (strip any preamble/summary)
    local raw_output="$RUN_DIR/${preset}_raw.txt"
    local err_output="$RUN_DIR/${preset}_stderr.log"

    # all agents are cloud-hosted: point agent-default-model at the right model
    # (pro for the reasoning agents, flash for everything else) before launch
    #
    # audio-producer robustness: its voice-casting turn (effort xhigh) has
    # repeatedly looped ("finalizing / preparing to write JSON") and been killed
    # by the empty-artifact watcher without writing voice_config.json. Curb the
    # loop by running ONLY this preset at reduced effort; every other agent keeps
    # xhigh. set_agent_model.py now honors DSH_EXECUTION_EFFORT through its local
    # fallback, so the route and the dsh child inherit this value.
    _AP_SAVED_EFFORT=""
    if [ "$preset" = "audio-producer" ]; then
        _AP_SAVED_EFFORT="${DSH_EXECUTION_EFFORT:-}"
        export DSH_EXECUTION_EFFORT="${_AP_SAVED_EFFORT:-high}"
    fi
    set_agent_model "$preset"

    local agent_exit=0
    pnpm dsh --profile headless \
        --patch "$PACK_DIR/.dsh/.agent-presets/$preset/agent.cordis.yml" \
        "$prompt_text" \
        > "$raw_output" \
        2> "$err_output" || agent_exit=$?

    # Restore/clear the audio-producer effort override now the agent call is done.
    if [ "$preset" = "audio-producer" ]; then
        if [ -n "$_AP_SAVED_EFFORT" ]; then export DSH_EXECUTION_EFFORT="$_AP_SAVED_EFFORT"; else unset DSH_EXECUTION_EFFORT; fi
    fi

    # Append stderr to pipeline.log for post-mortem
    if [[ -s "$err_output" ]]; then
        cat "$err_output" >> "$RUN_DIR/pipeline.log"
    fi

    # ── Runtime cloud-exhaustion catch ──
    # The pre-launch probe in set_agent_model.py checks the key's monthly
    # limit, but OpenRouter can still 402 when account credits can't cover
    # max_tokens ("can only afford N tokens"). dsh surfaces that as
    # `PI_AI_ERROR: 402` on stderr and exits non-zero — without this block
    # the run just retries the same broken cloud route forever.
    if [[ $agent_exit -ne 0 ]] && grep -qE 'PI_AI_ERROR:\s*(402|429|401)' "$err_output" 2>/dev/null; then
        local http_code
        http_code=$(grep -oE 'PI_AI_ERROR:\s*(402|429|401)' "$err_output" | head -1 | grep -oE '(402|429|401)')
        log "Cloud API $http_code for $preset — marking exhausted and falling back to local Qwen"
        # Mark exhausted (writes .cloud_fallback_state + .use_local_llm).
        # Call route_for via the Python CLI WITHOUT --show so it also runs
        # _ensure_local_llm (starts the service, waits for health) and
        # rewrites settings.yaml to the local provider — the bash
        # CLOUD_ROUTING=0 path only rewrites YAML and would leave the LLM down.
        python3 "$PACK_DIR/pipeline/set_agent_model.py" --mark-exhausted "runtime $http_code from $preset"
        export CLOUD_ROUTING="0"
        local fallback_route
        fallback_route=$(python3 "$PACK_DIR/pipeline/set_agent_model.py" "$preset") || {
            log "ERROR: local fallback route failed for $preset"
        }
        log "Model route (fallback): $fallback_route"
        # Retry once on local
        agent_exit=0
        pnpm dsh --profile headless \
            --patch "$PACK_DIR/.dsh/.agent-presets/$preset/agent.cordis.yml" \
            "$prompt_text" \
            > "$raw_output" \
            2> "$err_output" || agent_exit=$?
        if [[ -s "$err_output" ]]; then
            cat "$err_output" >> "$RUN_DIR/pipeline.log"
        fi
    fi

    if [[ $agent_exit -ne 0 ]]; then
        log "WARNING: Agent $preset exited non-zero (exit $agent_exit). Check $RUN_DIR/pipeline.log"
        # Save raw output even on failure for resume
    fi

    # Post-process: for story-creator, extract JSON block
    if [[ "$preset" == "story-creator" ]]; then
        log "Extracting JSON from story-creator output..."
        python3 - "$raw_output" "$output_file" << 'PYEOF'
import sys, json, re
raw = open(sys.argv[1]).read()

# Candidate list, most-trusted source first:
#   1. the whole stdout (agent printed nothing but JSON)
#   2. fenced ```json blocks (agent wrapped it in prose)
#   3. every balanced top-level {...} in the text, found by brace-depth scan
# A naive regex cannot do (3): the story schema nests objects 3+ levels deep,
# so a one-level pattern silently returns an inner sub-object instead.
candidates = []

try:
    candidates.append(json.loads(raw.strip()))
except Exception:
    pass

for block in re.findall(r'```(?:json)?\s*(.*?)```', raw, re.DOTALL):
    try:
        candidates.append(json.loads(block.strip()))
    except Exception:
        pass

def balanced_objects(text):
    objects, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    objects.append(text[start:i + 1])
    return objects

for blob in balanced_objects(raw):
    try:
        candidates.append(json.loads(blob))
    except Exception:
        pass

req = ['title', 'logline', 'characters', 'locations', 'acts', 'scenes']
# 'theme' is nice-to-have: if the agent omits it, derive one from the logline
# so a single omission never kills the run.
optional = ['theme']

# Prefer a candidate that satisfies the schema; otherwise the richest object,
# so the error message below can name exactly what the agent omitted.
best, best_complete = None, None
for obj in candidates:
    if not isinstance(obj, dict):
        continue
    if all(k in obj for k in req):
        if best_complete is None or len(obj) > len(best_complete):
            best_complete = obj
    elif best is None or len(obj) > len(best):
        best = obj

chosen = best_complete if best_complete is not None else best
if chosen is None:
    print("No parseable JSON object found in agent output "
          f"({len(raw)} bytes)", file=sys.stderr)
    sys.exit(1)

missing = [k for k in req if k not in chosen]
# Last-resort: auto-fill any missing key we can derive, so a single omission
# degrades gracefully instead of aborting the whole production.
if missing:
    scenes = chosen.get('scenes', []) or []
    if 'title' not in chosen:
        chosen['title'] = (chosen.get('meta', {}) or {}).get('title') \
            or (scenes[0].get('title') if scenes and isinstance(scenes[0], dict) else 'Untitled')
    if 'logline' not in chosen:
        chosen['logline'] = (chosen.get('meta', {}) or {}).get('logline', '')
    if 'theme' not in chosen:
        chosen['theme'] = chosen.get('logline', '')
    if 'acts' not in chosen:
        # Derive a single-act wrapper from the scene list
        chosen['acts'] = [{'act': 1, 'title': 'Main', 'scene_ids':
                           [s.get('scene_id') for s in scenes if isinstance(s, dict)]}]
    if 'characters' not in chosen:
        chosen['characters'] = []
    if 'locations' not in chosen:
        chosen['locations'] = []
    if 'scenes' not in chosen:
        chosen['scenes'] = []
    refilled = [k for k in missing if k in chosen]
    print(f"Auto-filled missing keys: {refilled}", file=sys.stderr)
    still_missing = [k for k in req if k not in chosen]
    if still_missing:
        print(f"Still missing after refill: {still_missing}", file=sys.stderr)
        sys.exit(1)

# Auto-fill optional keys the agent skipped (e.g. theme) so downstream agents
# always have them.
for k in optional:
    if k not in chosen:
        if k == 'theme':
            chosen['theme'] = chosen.get('logline', 'A story of repair and renewal').strip()
        else:
            chosen[k] = ""

json.dump(chosen, open(sys.argv[2], 'w'), indent=2)
print(f"story.json written ({len(json.dumps(chosen))} chars, "
      f"{len(chosen.get('scenes', []) or [])} scenes)")
PYEOF
        local rc=$?
        if [[ $rc -ne 0 ]]; then
            log "ERROR: story-creator JSON extraction failed (exit $rc)"
            # Don't copy raw output — it's incomplete; let resume retry
            return 1
        fi
    else
        # Agents on local models often write files via tools to paths they choose
        # (e.g., prompts/S01_001_review.json) instead of printing JSON to stdout.
        # Search strategy:
        #   1. Agent wrote $output_file directly (exact path match)
        #   2. Agent wrote a JSON file somewhere in $RUN_DIR recently (within 5 min)
        #   3. Extract JSON from raw stdout
        #   4. Fall back to raw stdout
        # qa-inspector: never take the "kept" branch for a prior FAIL/PASS unless THIS
        # run produced a fresh signal. A crashed agent (exit != 0) or prose-only raw
        # must not re-commit yesterday's / last take's qa_verdict.json — that burns
        # (or falsely advances) H3 retries on a clip the inspector never looked at.
        local keep_existing=true
        if [[ "$preset" == "qa-inspector" ]]; then
            local has_verdict=false
            if [ -s "$output_file" ] && python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); v=str(d.get("verdict","")).upper(); sys.exit(0 if v.startswith(("PASS","FAIL")) else 1)' "$output_file" 2>/dev/null; then
                has_verdict=true
            fi
            local fresh_report=""
            fresh_report=$(find "$RUN_DIR" "$DSH_DIR" -maxdepth 5 -name "qa_report.json" -newer "$prompt_file" -mmin -15 \
                -exec python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if str(d.get("verdict","")).upper().startswith(("PASS","FAIL")) else 1)' {} \; -print \
                2>/dev/null | head -1 || true)
            local raw_has_verdict=false
            if python3 "$PACK_DIR/extract_json.py" "$raw_output" /tmp/qa_raw_extract_$$.json verdict >/dev/null 2>&1; then
                raw_has_verdict=true
                rm -f /tmp/qa_raw_extract_$$.json
            fi
            # Fresh signal from THIS run wins over a prior-take verdict sitting in output_file.
            # raw_has_verdict must force extraction (keep_existing=false): S02_002 shipped a
            # PASS 0.93 in raw stdout while the keep-branch retained yesterday's FAIL, then
            # normalise filtered both the old report and old verdict as older than clip.mp4
            # and the gate re-read the stale FAIL — burning a whole H3 retry.
            if [ "$raw_has_verdict" = "true" ]; then
                keep_existing=false
                log "  (qa-inspector raw stdout has a fresh verdict — extracting, not keeping prior)"
            elif [ "$has_verdict" = "true" ] && [ "$agent_exit" -eq 0 ] && [ -n "$fresh_report" ]; then
                keep_existing=true
            else
                keep_existing=false
                if [ "$agent_exit" -ne 0 ]; then
                    log "  (qa-inspector crashed (exit $agent_exit) with no fresh report — discarding prior verdict)"
                elif [ "$has_verdict" = "true" ]; then
                    log "  (qa-inspector produced no fresh report/raw verdict this run — re-resolving, not keeping prior)"
                else
                    log "  ($output_file has no verdict field — re-resolving for qa-inspector)"
                fi
                # Neutralise stale verdict so normalise cannot re-bind last take's FAIL.
                if [ "$has_verdict" = "true" ]; then
                    mv -f "$output_file" "${output_file%.json}_stale_$(date +%s).json" 2>/dev/null || true
                fi
            fi
        fi

        if [ "$keep_existing" = "true" ] && [ -s "$output_file" ] && \
           python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$output_file" 2>/dev/null; then
            log "  (kept agent-written $output_file)"
        else
            # Search strategy for agent-written files:
            # 1. Recently-written JSON (within 10 min, any depth) — prefer qa_report.json
            # 2. Shot-specific files in prompts/ dir (from previous runs)
            # 3. Extract JSON from raw stdout
            local found_json=""

            # Search 1: recent JSON files; prefer *qa_report.json* when preset is qa-inspector
            # NOTE: the trailing `|| true` is load-bearing. Under `set -euo pipefail`
            # (line 13) a bare assignment takes the substitution's exit status, and
            # grep -v exits 1 when it selects no lines — which is exactly the outcome
            # when an agent legitimately wrote nothing (e.g. ref2va "skip frame gen").
            # Without it the pipeline dies silently right after the agent finishes.
            if [[ "$preset" == "qa-inspector" ]]; then
                found_json=$(find "$RUN_DIR" "$DSH_DIR" -maxdepth 5 -name "qa_report.json" -newer "$prompt_file" -mmin -15 \
                    -exec python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if str(d.get("verdict","")).upper().startswith(("PASS","FAIL")) else 1)' {} \; -print \
                    2>/dev/null | head -1 || true)
            fi
            # screenplay-reviewer: NEVER generic-find across $RUN_DIR. A pre-synthesised
            # dialogue_meta.json (or frame_meta) sitting in another clip dir is newer than
            # prompt_review.txt and gets copied over reviewed_prompt.json — S02_003 and
            # S02_004 both lost their H3 prompt that way. Live bash caches the function
            # body at process start, so this only takes effect after a pipeline restart.
            # For the reviewer, skip Search 1 entirely; extract_json / prose-wrap below
            # (which prefers prompts/<shot>_*.md) is the correct path.
            if [ -z "$found_json" ] && [[ "$preset" != "screenplay-reviewer" ]]; then
                found_json=$(find "$RUN_DIR" "$DSH_DIR" -maxdepth 4 -name "*.json" -newer "$prompt_file" -mmin -10 \
                    -exec python3 -c 'import json,sys; json.load(open(sys.argv[1])); print(sys.argv[1])' {} \; 2>/dev/null \
                    | grep -v "pipeline.log\|run_latest\|settings.yaml\|watchdog" | head -1 || true)
            fi

            # Search 2: shot-specific files in prompts/ directory (agent may have written here)
            if [ -z "$found_json" ]; then
                local shot_id_guess=""
                shot_id_guess=$(basename "$(dirname "$output_file")" 2>/dev/null)
                for candidate in "$RUN_DIR/prompts/${shot_id_guess}_review.json" \
                                 "$RUN_DIR/prompts/${shot_id_guess}"_*.json; do
                    if [ ! -f "$candidate" ]; then
                        continue
                    fi
                    # Reviewer envelopes must carry final_prompt / h3_mode — never accept
                    # a bare json.load (that is how dialogue_meta sneaks in).
                    if [[ "$preset" == "screenplay-reviewer" ]]; then
                        if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if isinstance(d,dict) and ("final_prompt" in d or "h3_mode" in d) else 1)' "$candidate" 2>/dev/null; then
                            found_json="$candidate"
                            break
                        fi
                    elif python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$candidate" 2>/dev/null; then
                        found_json="$candidate"
                        break
                    fi
                done
            fi

            if [ -n "$found_json" ] && [ "$found_json" != "$output_file" ]; then
                log "  (found agent-written JSON at $found_json -> copying to $output_file)"
                cp "$found_json" "$output_file"
            elif python3 "$PACK_DIR/extract_json.py" "$raw_output" "$output_file" \
                     >>"$RUN_DIR/pipeline.log" 2>&1; then
                :
            elif [[ "$preset" == "screenplay-reviewer" ]]; then
                # Reviewer often prints a build summary instead of the JSON envelope.
                # Wrap prose so downstream json.load + HAS_DIALOGUE/<d> checks work.
                # Prefer a prompts/*.txt the summary names as the real H3 prompt.
                local wrap_rc=0
                python3 - "$raw_output" "$output_file" "$(basename "$(dirname "$output_file")")" "$RUN_DIR" << 'PYEOF' \
                    >>"$RUN_DIR/pipeline.log" 2>&1 || wrap_rc=$?
import json, re, sys, os
raw_path, out_path, shot_id, run_dir = sys.argv[1:5]
raw = open(raw_path, encoding="utf-8", errors="replace").read()
final = None
# Prefer an on-disk H3 prompt the reviewer wrote under $RUN_DIR/prompts/
pdir = os.path.join(run_dir, "prompts")
if os.path.isdir(pdir):
    cands = sorted(
        (os.path.join(pdir, n) for n in os.listdir(pdir)
         if shot_id in n and n.endswith((".txt", ".md"))),
        key=os.path.getmtime, reverse=True,
    )
    for p in cands:
        try:
            body = open(p, encoding="utf-8", errors="replace").read().strip()
        except OSError:
            continue
        if len(body) >= 80:
            final = body
            break
if final is None:
    # Strip a leading summary header; keep the rest as the prompt body.
    body = raw.strip()
    if len(body) < 80:
        print(f"screenplay-reviewer prose too short to wrap ({len(body)} bytes)")
        sys.exit(1)
    final = body
mode = "ref2va"
m = re.search(r"\b(ref2va|i2va|fl2va)\b", final, re.I) or re.search(r"\b(ref2va|i2va|fl2va)\b", raw, re.I)
if m:
    mode = m.group(1).lower()
env = {
    "shot_id": shot_id,
    "status": "REVISED",
    "h3_mode": mode,
    "original_description": "",
    "final_prompt": final,
    "reference_assignments": [],
    "changes_made": ["wrapped prose/stdout into JSON envelope (agent omitted fenced JSON)"],
    "risk_flags": [],
    "wrapped_from_prose": True,
}
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(env, fh, indent=2)
print(f"OK: wrapped screenplay prose -> {out_path} ({len(final)} char final_prompt)")
PYEOF
                if [ $wrap_rc -ne 0 ]; then
                    store_raw_artifact "$preset" "$raw_output" "$output_file"
                fi
            elif [[ "$preset" == "qa-inspector" ]]; then
                # Never fall back to prose-as-verdict: that forces json.load FAIL below.
                # Leave output_file absent so normalise reports "no verdict" clearly.
                log "WARNING: qa-inspector emitted no parseable JSON (exit $agent_exit) — no verdict written"
                if [ -s "$output_file" ]; then
                    mv -f "$output_file" "${output_file%.json}_bad_$(date +%s).json" 2>/dev/null || true
                fi
            else
                store_raw_artifact "$preset" "$raw_output" "$output_file"
            fi
        fi
    fi

    # Mid-run failure watcher: an empty artifact means the agent call itself
    # failed (API/transport), which would otherwise silently cascade into
    # downstream steps. Log + push to Discord so the run is never silently wrong.
    if [[ ! -s "$output_file" ]]; then
        log "ERROR: agent $preset produced an empty artifact: $output_file"
        notify "Agent $preset produced EMPTY output during run $(basename "$RUN_DIR") — see pipeline.log" "Pipeline Warning"
        return 1
    fi
    log "Agent $preset done -> $output_file"
    return 0
}

# ── Phase-Aware Agent Runner ─────────────────────────────────
# Predecessor to run_dsh_agent that handles service phase transitions.
# Call this instead of run_dsh_agent for all pipeline agents.
# Under cloud routing (default), it's a thin pass-through.
# Under local mode, it ensures the correct services are running.

# Preset → phase mapping (mirrors run_dsh_agent.sh but inline here)
declare -A _AGENT_PHASE=(
    ["story-creator"]="thinking" ["director"]="thinking" ["screenplay-reviewer"]="thinking"
    ["character-designer"]="video_gen" ["location-designer"]="video_gen"
    ["image-generator"]="video_gen" ["storyboard-artist"]="video_gen"
    ["video-generator"]="video_gen"
    ["audio-producer"]="audio_gen" ["music-composer"]="audio_gen"
    ["qa-inspector"]="thinking"
    ["subtitle-generator"]="monitoring" ["post-production-editor"]="monitoring"
    ["pipeline-orchestrator"]="monitoring"
)

_agent_phase_of() {
    echo "${_AGENT_PHASE[$1]:-thinking}"
}

_run_dsh_agent_with_phase() {
    local preset="$1"
    shift
    # After shift: "$@" = (prompt_file, output_file) — matches _run_dsh_agent_internal's args
    local phase
    phase=$(_agent_phase_of "$preset")

    # Cloud routing: skip all service management
    if [[ "${CLOUD_ROUTING:-1}" == "1" ]]; then
        # Ensure ComfyUI is up if this agent uses it
        case "$phase" in
            video_gen) ensure_comfyui ;;
        esac
        _run_dsh_agent_internal "$preset" "$@"
        return $?
    fi

    # Local routing. The agent ITSELF is served by the local LLM, so every
    # phase must leave an LLM healthy — the old body stopped it first
    # (svc_transition video_gen / stop_all_llms_for_generation), which is why
    # character-designer, location-designer, image-generator and
    # audio-producer all died with "dsh: TRANSPORT: Connection error" and
    # wrote 47-byte [ELIFECYCLE] artifacts. Generation work that truly needs
    # exclusive VRAM runs in pipeline/*.sh with the LLM parked, never inside
    # an agent turn.
    # Every phase: the agent is served by the local LLM, which is the GPU's
    # sole owner while the turn runs (ComfyUI and the TTS cluster are
    # Conflicts= with it). Agents therefore never call ComfyUI or a TTS port:
    # they write job files (frame_spec.json, tts_jobs.json, reviewed_prompt.json)
    # and the pipeline renders them afterwards with the LLM parked
    # (generate_frames.py, tts_render.py, generate_clip.py, …).
    ensure_agent_llm

    # Optional VRAM pre-check
    if [[ -f "$VRAM_MONITOR" ]]; then
        vram_check_now >> "$RUN_DIR/pipeline.log" 2>&1 || true
    fi

    _run_dsh_agent_internal "$preset" "$@"
    local rc=$?

    # Post-agent VRAM report
    if [[ -f "$VRAM_MONITOR" ]]; then
        vram_report >> "$RUN_DIR/pipeline.log" 2>&1 || true
    fi

    return $rc
}

restart_comfyui_clean() {
    systemctl restart comfyui.service 2>/dev/null || true
}

# Wait for the local LLM's "I am back" announcement: output/.llm_ready written
# by pipeline/llm_ready_notify.sh (ExecStartPost of the Strata unit) newer than
# the failure, or the API answering directly. If the unit is not even starting
# (parked by a render that is still running), kick it: llm_monitor.py would do
# the same on its next pass, but the agent turn is waiting right now.
_wait_llm_ready() {
    local timeout="${1:-330}" since waited=0 ready="$OUTPUT_DIR/.llm_ready"
    local health="${LLM_HEALTH_URL:-http://127.0.0.1:8321/v1/models}"
    local unit="${LLM_UNIT:-strata-swift-iq3-xxs.service}"
    since=$(date +%s)
    while [ "$waited" -lt "$timeout" ]; do
        if [ -f "$ready" ] && [ "$(stat -c %Y "$ready" 2>/dev/null || echo 0)" -ge "$since" ]; then
            log "LLM announced it is back ($(cat "$ready" 2>/dev/null | head -c 160)) — resuming"
            return 0
        fi
        if curl -sf --max-time 4 "$health" > /dev/null 2>&1; then
            log "LLM answering again after ${waited}s — resuming"
            return 0
        fi
        if [ "$waited" -ge 20 ] && [ ! -f "$OUTPUT_DIR/.llm_parked" ] && \
           [ "$(systemctl is-active "$unit" 2>/dev/null)" != "active" ] && \
           [ "$(systemctl is-active "$unit" 2>/dev/null)" != "activating" ]; then
            log "LLM unit is $(systemctl is-active "$unit" 2>/dev/null) and not parked — starting it (≈2 min)"
            systemctl reset-failed "$unit" 2>/dev/null || true
            systemctl start "$unit" 2>/dev/null || true
        fi
        sleep 5
        waited=$((waited + 5))
    done
    log "WARNING: LLM did not come back within ${timeout}s — retrying the turn anyway"
    return 1
}

# ── Public Agent Launcher ─────────────────────────────────────
# This is the function called throughout run.sh by every pipeline step.
# It delegates to _run_dsh_agent_with_phase for VRAM-safe transitions,
# which then calls _run_dsh_agent_internal for the actual dsh execution.
 run_dsh_agent() {
    local preset="$1"
    shift
    # Retry up to 3 times on transient dsh transport errors (the local ninfer
    # occasionally drops long sessions with "TRANSPORT: Connection error" or
    # "inference request expired while waiting for admission"). A clean success
    # returns immediately; only transport/admission failures trigger a retry.
    local attempt=1
    while [[ $attempt -le 3 ]]; do
        _run_dsh_agent_with_phase "$preset" "$@"
        local rc=$?
        if [[ $rc -eq 0 ]]; then
            return 0
        fi
        # Detect a transient transport/admission failure from the stderr log
        local errfile="${RUN_DIR}/${preset}_stderr.log"
        if grep -qE 'TRANSPORT: Connection error|expired while waiting for admission|PI_AI_ERROR' "$errfile" 2>/dev/null; then
            # The usual cause: ComfyUI/TTS took the GPU and the Strata unit was
            # stopped mid-turn. Its ExecStartPost (pipeline/llm_ready_notify.sh)
            # writes output/.llm_ready once the API answers again — wait for
            # that announcement (≤ 330 s, ≈2 min typical) and then re-issue the
            # SAME prompt file; agent turns are idempotent (every output is a file).
            log "[$preset] transient transport error (attempt $attempt/3) — waiting for the LLM to announce it is back"
            _wait_llm_ready 330
            attempt=$((attempt+1))
            continue
        fi
        # Non-transient failure — don't retry
        return $rc
    done
    return $rc
}

write_prompt() {
    local file="$1" content="$2"
    echo "$content" > "$file"
}

# ── Parse Arguments ─────────────────────────────────────────
RESUME=false
FROM_CLIP=""
ONLY_CLIP=""
STORY_FILE=""
RUN_DIR=""

show_usage() {
    echo "Usage:"
    echo "  $0 <story.md>                            Start new production"
    echo "  $0 --resume                               Resume latest run (skip completed steps)"
    echo "  $0 --resume <run_dir>                     Resume specific run"
    echo "  $0 --resume --from-clip S02_003           Resume from a specific clip onward"
    echo "  $0 --resume --only-clip S01_005           Regenerate only one clip"
    echo ""
    echo "Options:"
    echo "  --resume [run_dir]    Resume an existing production run"
    echo "  --from-clip <id>     Start processing from this clip (skip earlier clips)"
    echo "  --only-clip <id>     Process only this single clip then exit"
    echo ""
    echo "Output: $OUTPUT_DIR/run_<timestamp>/final/"
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --resume)
            RESUME=true
            # Next arg might be a run dir
            if [ -n "${2:-}" ] && [ -d "$2" ]; then
                RUN_DIR="$2"; shift
            fi
            shift ;;
        --from-clip)
            FROM_CLIP="${2:-}"; shift 2 ;;
        --only-clip)
            ONLY_CLIP="${2:-}"; shift 2 ;;
        -h|--help)
            show_usage ;;
        *)
            if [ -z "$STORY_FILE" ]; then
                STORY_FILE="$1"
            fi
            shift ;;
    esac
done

# Resolve RUN_DIR
if [ "$RESUME" = "true" ]; then
    if [ -z "$RUN_DIR" ]; then
        # Match only timestamped run dirs (run_[0-9]*); the run_latest symlink and
        # run_latest*.pid/console files also match run_* and can win mtime sort —
        # resolving to the symlink makes line 446 recreate a self-referential loop.
        # `|| true` keeps the assignment pipefail-safe when no run dir exists;
        # the [ -z ] check below dies with a clear message in that case.
        RUN_DIR=$(ls -td "$OUTPUT_DIR"/run_[0-9]* 2>/dev/null | head -1 || true)
        [ -z "$RUN_DIR" ] && die "No previous run found to resume"
    fi
    [ -f "$RUN_DIR/input_story.md" ] || die "No story found in $RUN_DIR"
    log "=== RESUMING PRODUCTION ==="
    log "Run: $RUN_DIR"
    [ -n "$FROM_CLIP" ] && log "From clip: $FROM_CLIP"
    [ -n "$ONLY_CLIP" ] && log "Only clip: $ONLY_CLIP"
    notify "Resuming production$([ -n "$FROM_CLIP" ] && echo " from $FROM_CLIP")$([ -n "$ONLY_CLIP" ] && echo " (only $ONLY_CLIP)")\nRun: $RUN_DIR" "Production Pack Resume"
elif [ -n "$STORY_FILE" ]; then
    [ -f "$STORY_FILE" ] || die "File not found: $STORY_FILE"
    TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
    RUN_DIR="$OUTPUT_DIR/run_$TIMESTAMP"
    mkdir -p "$RUN_DIR"/{characters,locations,storyboards,frames,clips,audio/voices,audio/narration,music,final}
    cp "$STORY_FILE" "$RUN_DIR/input_story.md"
else
    show_usage
fi

# Save PID and symlink for status checks
echo $$ > "$OUTPUT_DIR/run_latest.pid"
ln -sfn "$RUN_DIR" "$OUTPUT_DIR/run_latest"

log "=== PRODUCTION PACK ==="
log "Run:    $RUN_DIR"
log "Resume: $RESUME"
[ -n "$FROM_CLIP" ] && log "From:   $FROM_CLIP"
[ -n "$ONLY_CLIP" ] && log "Only:   $ONLY_CLIP"
log "GPU:    $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo 'unknown')"

[ "$RESUME" = "false" ] && notify "Production started\nStory: $(basename "${STORY_FILE:-resumed}")\nRun: $RUN_DIR" "Production Pack"

# Helper: skip a step if its output already exists (for resume)
skip_if_done() {
    local output_file="$1" step_name="$2"
    [ "$RESUME" = "true" ] || return 1
    [ -f "$output_file" ] && [ -s "$output_file" ] || return 1
    # A crashed agent leaves its raw stdout as the artifact (47-byte
    # "[ELIFECYCLE] Command failed with exit code 1."). Skipping on -s alone
    # would carry that forward forever — validate JSON before trusting it.
    case "$output_file" in
        *.json)
            if ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' \
                    "$output_file" > /dev/null 2>&1; then
                log "RESUME: $step_name — $output_file is not valid JSON, regenerating"
                return 1
            fi
            ;;
    esac
    log "SKIP (resume): $step_name — output exists: $output_file"
    return 0
}

# Helper: should we process this clip?
should_process_clip() {
    local shot_id="$1"
    # --only-clip: only process the specified clip
    if [ -n "$ONLY_CLIP" ] && [ "$shot_id" != "$ONLY_CLIP" ]; then
        return 1
    fi
    # --from-clip: skip clips before the specified one
    if [ -n "$FROM_CLIP" ] && [ "$REACHED_FROM_CLIP" = "false" ]; then
        if [ "$shot_id" = "$FROM_CLIP" ]; then
            REACHED_FROM_CLIP=true
        else
            return 1
        fi
    fi
    return 0
}

# ═══════════════════════════════════════════════════════════
# PHASE 1: PRE-PRODUCTION [Qwen 3.5 122B]
# ═══════════════════════════════════════════════════════════

step "PHASE 1: PRE-PRODUCTION"
notify "Phase 1: Pre-production starting (DeepSeek V4 Pro via OpenCode Go)" "Pre-Production"

# ── Agent handoff from the idle-monitor ─────────────────────
# If a TTS service went stuck and the monitor reclaimed VRAM to NInfer, it left
# a handoff note. Surface it so this run's agents continue the debugging.
HANDOFF="$OUTPUT_DIR/agent_handoff.json"
if [ -s "$HANDOFF" ]; then
    log "Agent handoff detected — continuing prior debugging:"
    python3 -c "import json,sys; d=json.load(open('$HANDOFF')); print('  ', d.get('instruction',''))" 2>/dev/null
    # Append the instruction to the story prompt context so the first agent sees it
    HANDOFF_NOTE=$(python3 -c "import json; print(json.load(open('$HANDOFF')).get('instruction',''))" 2>/dev/null)
    if [ -n "$HANDOFF_NOTE" ]; then
        echo "" >> "$RUN_DIR/input_story.md" 2>/dev/null || true
        echo "" >> "$RUN_DIR/input_story.md" 2>/dev/null || true
        echo "[DEBUG HANDOFF FROM IDLE-MONITOR] $HANDOFF_NOTE" >> "$RUN_DIR/input_story.md" 2>/dev/null || true
        log "Handoff instruction appended to input context."
    fi
fi

# 1a. Story Creator
if ! skip_if_done "$RUN_DIR/story.json" "Story Creator"; then
    step "1a. Story Creator — structuring story"
    write_prompt "$RUN_DIR/prompt_story.txt" \
        "Read the story below and structure it into the production JSON format (see story-schema skill).

OUTPUT: a single valid JSON object with EXACTLY these top-level keys (all required):
  title, logline, theme, characters, locations, acts, scenes
- characters: array of {name, description, identity_anchors}
- locations: array of {name, description, color_palette}
- acts: array of act objects
- scenes: array of {scene_id, title, dialogue[], narration[], emotional_beat, visual_mood}
Do NOT omit any of the top-level keys above. Do not wrap the JSON in prose or code fences — output raw JSON only.

--- STORY ---
$(cat "$RUN_DIR/input_story.md")"
    # Retry up to 3 times: local ninfer can drop long requests with
    # "inference request expired while waiting for admission" under load.
    attempt=1
    while [[ $attempt -le 3 ]]; do
        if run_dsh_agent "story-creator" "$RUN_DIR/prompt_story.txt" "$RUN_DIR/story.json"; then
            [ -s "$RUN_DIR/story.json" ] && break
            log "story.json empty after attempt $attempt — retrying"
        else
            log "story-creator attempt $attempt failed — checking for transient admission timeout"
        fi
        if grep -qE 'expired while waiting for admission|PI_AI_ERROR' "$RUN_DIR/story-creator_stderr.log" 2>/dev/null; then
            log "Transient LLM admission timeout on attempt $attempt — backing off and retrying"
            sleep 5
        fi
        attempt=$((attempt+1))
    done
    [ -s "$RUN_DIR/story.json" ] || die "story-creator failed after 3 attempts — no story.json (check $RUN_DIR/pipeline.log)"
fi

# Post story summary to Discord before continuing
if [ -f "$RUN_DIR/story.json" ]; then
    STORY_SUMMARY=$(python3 - "$RUN_DIR" << 'PYEOF'
import json, sys
try:
    with open("RUNDIR/story.json".replace("RUNDIR", sys.argv[1])) as f:
        d = json.load(f)
    title = d.get("title", "Untitled")
    logline = d.get("logline", "")
    chars = d.get("characters", [])
    locs = d.get("locations", [])
    scenes = d.get("scenes", [])
    total_dur = sum(s.get("duration_estimate_seconds", 30) for s in scenes)
    mins = total_dur // 60
    secs = total_dur % 60
    char_names = ", ".join(c.get("name","?") for c in chars[:5])
    loc_names = ", ".join(l.get("name","?") for l in locs[:5])
    print(f"Title: {title}")
    print(f"Logline: {logline}")
    print(f"Characters ({len(chars)}): {char_names}")
    print(f"Locations ({len(locs)}): {loc_names}")
    print(f"Scenes: {len(scenes)}")
    print(f"Est. duration: {mins}m {secs}s")
except Exception as e:
    print(f"(could not parse story: {e})")
PYEOF
)

    notify "$STORY_SUMMARY\n\nProceeding with directing and asset generation..." "Story Analysis"
    log "$STORY_SUMMARY"
fi

# 1b. Director
if ! skip_if_done "$RUN_DIR/shot_list.json" "Director"; then
    step "1b. Director — creating shot list"
    write_prompt "$RUN_DIR/prompt_director.txt" \
        "Read the structured story at $RUN_DIR/story.json. Create a complete shot list following the shot-list-schema skill. For each scene, plan shots:
- Duration: 5-8 seconds per clip (10 s max unless dialogue needs it; H3 minimum 5.2 s)
- Mode: reference (mid-scene coverage), composited_i2v (scene openings with characters), first_frame_only (0-character establishing shots from the plate), first_last_frame (continuous action only)
- H3 ENVELOPE (knowledge/minimax_h3_rules.md section 0, enforced by shot_list_lint.py): ONE beat per shot (a montage = one shot per beat); <= 3 named characters per frame, extras are 'a few distant figures, backs to camera' and never counted; EACH character gets its own action / hand_position / expression (no copy-paste); extreme-wide shots carry no named characters and no faces; faces and hands only at medium or closer; the picture must illustrate the narration line laid over it; reference_images.source only canonical files (characters/<id>/<id>_side.png, locations/<id>/<id>.png)
- Mark which shots have DIALOGUE (needs lip sync) vs NARRATION (no lip sync)
- Specify camera, characters, hand positions, identity anchors
- EDITING (editing-grammar skill, mandatory): consecutive shots in a scene differ by subject, >= 2 shot sizes or >= 30 degrees (no jump cuts); never three shots of the same size in a row; every shot has edit.handle_head_s / edit.handle_tail_s >= 1.0 INCLUDED in duration_seconds, edit.cut_in and edit.cut_out (pose, gaze, prop state, motion phase); every dialogue scene has a reaction single and an insert; readable_props (clock faces, counts) pinned per scene; transition_to_next.type = cut unless a story reason
- Output valid JSON to be processed shot-by-shot."
    run_dsh_agent "director" "$RUN_DIR/prompt_director.txt" "$RUN_DIR/shot_list.json" \
        || die "director failed — no shot_list.json (check $RUN_DIR/pipeline.log)"

    # Validate audio timing — fix dialogue overflow and narration overlap
    step "1b-validate. Audio Timing Validation"
    TIMING_ISSUES=$(python3 "$PACK_DIR/validate_audio_timing.py" "$RUN_DIR/shot_list.json" --fix 2>&1)
    log "$TIMING_ISSUES"
    if echo "$TIMING_ISSUES" | grep -q "timing issue"; then
        notify "$TIMING_ISSUES" "Audio Timing Fix"
    fi

    # Validate the shot list as an EDIT — jump cuts, handles, cut points, coverage
    # (The Clockwork Moth postmortem: 9 same-subject/same-size consecutive pairs shipped)
    step "1b-edit. Shot List Edit Validation"
    EDIT_ROUNDS=0
    while ! EDIT_ISSUES=$(python3 "$PACK_DIR/pipeline/edit_check.py" --shot-list \
            --shots "$RUN_DIR/shot_list.json" --out "$RUN_DIR/shot_list_edit_report.json" 2>&1); do
        log "$EDIT_ISSUES"
        EDIT_ROUNDS=$((EDIT_ROUNDS + 1))
        if [ "$EDIT_ROUNDS" -gt 2 ]; then
            notify "Shot list still has blocking edit issues after $EDIT_ROUNDS fixes — continuing, see shot_list_edit_report.json" "Edit Validation"
            break
        fi
        write_prompt "$RUN_DIR/prompt_director_edit_fix.txt" \
            "Your shot list $RUN_DIR/shot_list.json fails the editing-grammar validation. Read $RUN_DIR/shot_list_edit_report.json and fix EVERY 'jump_cut', 'handles' and 'cut_points' issue (and the rhythm/coverage ones): change shot size by >= 2 steps or angle by >= 30 degrees, insert a reaction single or an insert between same-subject shots, add edit.handle_head_s/handle_tail_s >= 1.0 (included in duration_seconds) and edit.cut_in/cut_out to every shot. Rewrite $RUN_DIR/shot_list.json in place, same schema, valid JSON only."
        run_dsh_agent "director" "$RUN_DIR/prompt_director_edit_fix.txt" "$RUN_DIR/shot_list.json" \
            || die "director edit-fix failed (check $RUN_DIR/pipeline.log)"
    done
    log "$EDIT_ISSUES"

    # Lint the shot list against the H3 envelope — montage-in-one-shot, > 3 named
    # characters, copy-pasted character actions, counted crowds, invented reference
    # files (Cinderquay postmortem 2026-10-16, knowledge/minimax_h3_rules.md §0).
    step "1b-lint. Shot List H3-Envelope Lint"
    LINT_ROUNDS=0
    while ! LINT_ISSUES=$(python3 "$PACK_DIR/pipeline/shot_list_lint.py" --shots "$RUN_DIR/shot_list.json" \
            --run-dir "$RUN_DIR" --out "$RUN_DIR/shot_list_lint_report.json" --fix 2>&1); do
        log "$LINT_ISSUES"
        LINT_ROUNDS=$((LINT_ROUNDS + 1))
        if [ "$LINT_ROUNDS" -gt 2 ]; then
            notify "Shot list still has blocking H3-envelope issues after $LINT_ROUNDS fixes — continuing, see shot_list_lint_report.json" "Shot Lint"
            break
        fi
        write_prompt "$RUN_DIR/prompt_director_lint_fix.txt" \
            "Your shot list $RUN_DIR/shot_list.json breaks the MiniMax H3 envelope (knowledge/minimax_h3_rules.md section 0). Read $RUN_DIR/shot_list_lint_report.json and fix EVERY blocking issue: split a montage into one shot per beat (5-8 s each, new shot ids, renumber the scene); no more than 3 named characters in a frame (extras become 'a few distant figures, backs to camera' in action_description, never counted); write each character's OWN action, hand_position and expression (no identical strings between characters); extreme-wide shots carry no named characters and no faces. Keep every editing-grammar rule (handles, cut_in/cut_out, no jump cuts, readable_props). Rewrite $RUN_DIR/shot_list.json in place, same schema, valid JSON only."
        run_dsh_agent "director" "$RUN_DIR/prompt_director_lint_fix.txt" "$RUN_DIR/shot_list.json" \
            || die "director lint-fix failed (check $RUN_DIR/pipeline.log)"
    done
    log "$LINT_ISSUES"
fi

# Post shot plan summary to Discord
if [ -f "$RUN_DIR/shot_list.json" ]; then
    SHOT_SUMMARY=$(python3 - "$RUN_DIR" << 'PYEOF'
import json, sys
try:
    with open("RUNDIR/shot_list.json".replace("RUNDIR", sys.argv[1])) as f:
        d = json.load(f)
    shots = d.get("shots", [])
    total_dur = sum(s.get("duration_seconds", 6) for s in shots)
    modes = {}
    dialogue_count = 0
    for s in shots:
        m = s.get("keyframe_strategy", {}).get("mode", "reference")
        modes[m] = modes.get(m, 0) + 1
        if s.get("audio", {}).get("dialogue"):
            dialogue_count += 1
    mins = total_dur // 60
    secs = total_dur % 60
    mode_str = ", ".join(f"{v}x {k}" for k, v in sorted(modes.items(), key=lambda x: -x[1]))
    print(f"Total shots: {len(shots)}")
    print(f"Total duration: {mins}m {secs}s")
    print(f"Modes: {mode_str}")
    print(f"Dialogue clips: {dialogue_count} (lip-synced)")
    print(f"Narration-only clips: {len(shots) - dialogue_count}")
except Exception as e:
    print(f"(could not parse shot list: {e})")
PYEOF
)

    notify "$SHOT_SUMMARY\n\nGenerating character sheets and locations next..." "Shot Plan"
    log "$SHOT_SUMMARY"
fi

# 1c. Character Designer
# Reference sheets are GPU work, not agent work: generate_character_sheets.py
# parks the local LLM itself (with a restart trap), renders every sheet in one
# blocking pass, then brings the LLM back. Doing this through an agent stopped
# the very LLM the agent was served by -> "dsh: TRANSPORT: Connection error"
# -> 47-byte [ELIFECYCLE] character_manifest.json that skip_if_done liked.
if ! skip_if_done "$RUN_DIR/character_manifest.json" "Character Designer"; then
    step "1c. Character Designer — generating reference sheets"
    ensure_comfyui
    bash "$PACK_DIR/pipeline/run_character_sheets.sh" "$RUN_DIR" \
        >> "$RUN_DIR/pipeline.log" 2>&1 \
        || die "character sheet generation failed (exit $?) — see $RUN_DIR/pipeline.log"
    [ -s "$RUN_DIR/character_manifest.json" ] \
        || die "character sheet generation wrote no character_manifest.json"
    notify "Character reference sheets generated" "Character Designer"
fi

# 1d. Location Designer
if ! skip_if_done "$RUN_DIR/location_manifest.json" "Location Designer"; then
    step "1d. Location Designer — generating environments"
    bash "$PACK_DIR/pipeline/run_location_refs.sh" "$RUN_DIR" \
        >> "$RUN_DIR/pipeline.log" 2>&1 \
        || die "location reference generation failed (exit $?) — see $RUN_DIR/pipeline.log"
    [ -s "$RUN_DIR/location_manifest.json" ] \
        || die "location reference generation wrote no location_manifest.json"
    notify "Location references generated" "Location Designer"
fi

# 1e. Voice Design
if ! skip_if_done "$RUN_DIR/audio/voices/voice_config.json" "Voice Design"; then
    step "1e. Audio Producer — voice design"
    swap_to_27b
    write_prompt "$RUN_DIR/prompt_voices.txt" \
        "Read $RUN_DIR/story.json and CAST the voices per the voice-casting skill. Every role incl. 'narrator' gets a voice class (child/teen/adult-female/adult-male/elderly-female/elderly-male/creature); the narrator uses a class NO character uses. Orpheus (voices tara/leah/jess/leo/dan/mia/zac/zoe) has ADULT voices only — children and elderly roles use Chatterbox cloning from an age-matched reference WAV (look under $RUN_DIR/audio/voices/refs/ and /root/production_pack/output/voices/refs/; pick the closest) or Orpheus with post_fx (pitch_semitones -2..+3, tempo 0.9..1.1); never an adult voice with affectation. CosyVoice is NOT available (no REST endpoint). DO NOT call any TTS port — the TTS engines are down while you run; the pipeline renders and measures the line-up afterwards. Write ONLY $RUN_DIR/audio/voices/voice_config.json: {\"<role>\": {\"voice_class\": ..., \"engine\": \"orpheus|chatterbox|kokoro\", \"voice\": ... or \"reference_audio\": <abs path>, \"post_fx\": {...} optional, \"notes\": ...}, ...} with one entry per speaking character (ids from story.json) plus 'narrator'."
    # The audio-producer reasoning turn can degenerate into a loop and time out
    # without writing voice_config.json (empty artifact → run abort). Retry the
    # initial cast a few times on an absent/empty artifact so one bad turn does
    # not kill the whole production.
    _CAST_ATTEMPT=1
    while [ "$_CAST_ATTEMPT" -le 3 ]; do
        if run_dsh_agent "audio-producer" "$RUN_DIR/prompt_voices.txt" "$RUN_DIR/audio/voices/voice_config.json"; then
            [ -s "$RUN_DIR/audio/voices/voice_config.json" ] && break
        fi
        log "voice_config.json empty after cast attempt $_CAST_ATTEMPT — retrying"
        _CAST_ATTEMPT=$((_CAST_ATTEMPT + 1))
        rm -f "$RUN_DIR/audio/voices/voice_config.json"
        sleep 5
    done
    if [ ! -s "$RUN_DIR/audio/voices/voice_config.json" ]; then
        die "audio-producer failed to write voice_config.json after $_CAST_ATTEMPT attempts (check $RUN_DIR/pipeline.log)"
    fi
    # pipeline renders the line-up with the GPU handed to the TTS cluster and
    # measures the separation. CAST_FAIL sends the agent back with the report.
    CAST_ROUNDS=0
    while ! python3 "$PACK_DIR/pipeline/voice_lineup.py" --run-dir "$RUN_DIR" >> "$RUN_DIR/pipeline.log" 2>&1; do
        LINEUP_RC=$?
        [ "$LINEUP_RC" -eq 2 ] && die "voice_lineup.py could not read $RUN_DIR/audio/voices/voice_config.json"
        CAST_ROUNDS=$((CAST_ROUNDS + 1))
        if [ "$CAST_ROUNDS" -gt 2 ]; then
            notify "Voice cast still CAST_FAIL after $CAST_ROUNDS recasts — continuing, see audio/voices/lineup/separation.json" "Voice Casting"
            break
        fi
        log "Voice line-up CAST_FAIL — asking the Audio Producer to recast (round $CAST_ROUNDS)"
        write_prompt "$RUN_DIR/prompt_voices_recast.txt" \
            "The measured voice line-up FAILED: read $RUN_DIR/audio/voices/lineup/separation.json ('failures' lists the pairs that are too close or share a voice class with the narrator). Recast those roles per the voice-casting skill — a different voice CLASS (child via chatterbox reference, elderly via chatterbox reference or orpheus with post_fx {\"pitch_semitones\": -2, \"tempo\": 0.92}, narrator in a class no character uses) and >= 4 semitones apart. Rewrite $RUN_DIR/audio/voices/voice_config.json in place (same schema: voice_class, engine, voice|reference_audio, optional post_fx per role incl. 'narrator'). Do NOT call any TTS port — the pipeline renders the line-up."
        run_dsh_agent "audio-producer" "$RUN_DIR/prompt_voices_recast.txt" "$RUN_DIR/audio/voices/voice_config.json"
    done
    notify "Voice cast line-up: $(python3 -c "import json;print(json.load(open('$RUN_DIR/audio/voices/lineup/separation.json')).get('verdict','?'))" 2>/dev/null || echo unknown)" "Audio Producer"
fi

# ═══════════════════════════════════════════════════════════
# PHASE 2: PER-CLIP LOOP
# ═══════════════════════════════════════════════════════════

step "PHASE 2: CLIP GENERATION"
notify "Phase 1 complete. Phase 2: Clip generation starting" "Clip Generation"

SHOT_IDS=$(python3 -c "
import json
with open('$RUN_DIR/shot_list.json') as f:
    data = json.load(f)
for shot in data.get('shots', []):
    print(shot['shot_id'])
" 2>/dev/null) || die "Failed to parse shot list"

TOTAL=$(echo "$SHOT_IDS" | wc -l)
CURRENT=0
REACHED_FROM_CLIP=true
[ -n "$FROM_CLIP" ] && REACHED_FROM_CLIP=false

# ── Manual-review halt ────────────────────────────────────────────────────────
# A clip that fails QA after every retry writes $RUN_DIR/HALT_FOR_REVIEW.json and the
# run EXITS (exit 3) so the operator can look at the takes. The run resumes only after
# one of these markers exists in that clip's directory:
#   clips/<SID>/MANUAL_PASS   accept the clip as it is (best take restored by best_take.py)
#   clips/<SID>/RETRY_AGAIN   re-render it (retry counter reset; edit the shot list /
#                             reviewed_prompt.json first if the fix lives there)
HALT_FILE="$RUN_DIR/HALT_FOR_REVIEW.json"
if [ -f "$HALT_FILE" ]; then
    HALT_SID=$(python3 -c "import json; print(json.load(open('$HALT_FILE')).get('shot_id',''))" 2>/dev/null || echo "")
    if [ -n "$HALT_SID" ] && [ -f "$RUN_DIR/clips/$HALT_SID/MANUAL_PASS" ]; then
        log "[$HALT_SID] MANUAL_PASS found — operator accepted the clip; clearing the halt"
        python3 - "$RUN_DIR/clips/$HALT_SID/qa_verdict.json" <<'PYEOF'
import json, os, sys, time
p = sys.argv[1]
try:
    d = json.load(open(p))
except Exception:
    d = {}
d["verdict"] = "PASS"
d["manual_pass"] = True
d["_manual_pass_note"] = "accepted by the operator after escalation (" + time.strftime("%Y-%m-%d %H:%M:%S") + ")"
tmp = p + ".tmp"
json.dump(d, open(tmp, "w"), indent=2)
os.replace(tmp, p)
PYEOF
        rm -f "$HALT_FILE"
    elif [ -n "$HALT_SID" ] && [ -f "$RUN_DIR/clips/$HALT_SID/RETRY_AGAIN" ]; then
        log "[$HALT_SID] RETRY_AGAIN found — operator asked for a fresh set of takes; clearing the halt"
        python3 - "$RUN_DIR/clips/$HALT_SID/qa_verdict.json" <<'PYEOF'
import json, os, sys
p = sys.argv[1]
try:
    d = json.load(open(p))
except Exception:
    d = {}
d["retry_count"] = 0
d["verdict"] = "FAIL"
d.pop("escalated_best_take", None)
tmp = p + ".tmp"
json.dump(d, open(tmp, "w"), indent=2)
os.replace(tmp, p)
PYEOF
        # every stage must re-run: park the escalated take's artifacts (resume would otherwise
        # see reviewed_prompt.json / clip.mp4 and skip straight to QA of the same clip)
        _ARCH="$RUN_DIR/clips/$HALT_SID/takes/escalated_$(date +%Y%m%d_%H%M%S)"
        mkdir -p "$_ARCH"
        for f in reviewed_prompt.json frame_spec.json frame_meta.json first_frame.png last_frame.png composited_frame.png clip.mp4 generation_log.json comfyui_workflow.json qa_report.json prompt_review.txt prompt_image.txt prompt_qa.txt; do
            [ -e "$RUN_DIR/clips/$HALT_SID/$f" ] && mv -f "$RUN_DIR/clips/$HALT_SID/$f" "$_ARCH/" 2>/dev/null || true
        done
        log "[$HALT_SID] escalated take parked in $_ARCH — review, frames and video will re-run"
        rm -f "$RUN_DIR/clips/$HALT_SID/RETRY_AGAIN" "$HALT_FILE"
    else
        die "run is halted for manual review of ${HALT_SID:-?} (see $HALT_FILE). Decide first: touch $RUN_DIR/clips/${HALT_SID:-<SID>}/MANUAL_PASS to accept it, or touch $RUN_DIR/clips/${HALT_SID:-<SID>}/RETRY_AGAIN to re-render it, then ./launch_resume.sh $RUN_DIR"
    fi
fi

# ── Per-scene gate: lint + Director fix right before a scene's first clip ──────────
# The 1b-lint step only runs in Phase 1, so a resumed run (and a story whose later scenes
# were never fixed) would carry copy-pasted actions, counted crowds and 4-character frames
# into H3 and burn three takes each before escalating. Gate each scene once, just in time:
# skip if any clip of the scene already PASSED (never rewrite a scene mid-production).
# The Director keeps the SAME shot ids and count (the clip loop iterates a fixed list).
scene_gate() {
    local scene="$1"
    local rep="$RUN_DIR/shot_list_lint_${scene}.json"
    local fix="$RUN_DIR/shot_list_${scene}.fix.json"
    if python3 - "$RUN_DIR" "$scene" <<'PYEOF'
import json, os, sys
run_dir, scene = sys.argv[1:3]
for s in json.load(open(os.path.join(run_dir, "shot_list.json")))["shots"]:
    if s.get("scene_id") != scene:
        continue
    v = os.path.join(run_dir, "clips", s["shot_id"], "qa_verdict.json")
    try:
        if json.load(open(v)).get("verdict") == "PASS":
            sys.exit(0)   # scene already in production — do not touch it
    except Exception:
        pass
sys.exit(1)
PYEOF
    then
        log "[gate $scene] a clip of this scene already passed — scene is locked, no rewrite"
        return 0
    fi
    local rounds=0 lint_out=""
    while ! lint_out=$(python3 "$PACK_DIR/pipeline/shot_list_lint.py" --shots "$RUN_DIR/shot_list.json" \
            --run-dir "$RUN_DIR" --scene "$scene" --out "$rep" --fix 2>&1); do
        log "[gate $scene] $lint_out"
        rounds=$((rounds + 1))
        if [ "$rounds" -gt 2 ]; then
            notify "Scene $scene still has blocking H3-envelope issues after $rounds Director fixes — continuing; QA will judge (see $rep)" "Scene Gate"
            return 0
        fi
        local ids
        ids=$(python3 -c "import json; print(', '.join(s['shot_id'] for s in json.load(open('$RUN_DIR/shot_list.json'))['shots'] if s.get('scene_id')=='$scene'))")
        write_prompt "$RUN_DIR/prompt_director_gate_${scene}.txt" \
            "Fix ONLY the shots of $scene ($ids) in $RUN_DIR/shot_list.json. Read $rep (the blocking issues) and $RUN_DIR/story.json for that scene. Keep the SAME shot ids, the same count and order, the same narration/dialogue assignments and the same scene_id. Rules (knowledge/minimax_h3_rules.md section 0): each character in characters_in_frame gets its OWN action, hand_position, expression and facing written for THIS shot (never the same string on two characters; creatures get a body pose, no hands); at most 3 named characters per frame (drop the least important to 'a few distant figures, backs to camera' in action_description); a montage shot keeps its single strongest beat, the other beats are dropped (do NOT add shots); no counted crowds; extreme-wide shots carry no named characters and no faces; faces only at medium or closer; duration 5-8 s (10 s max without dialogue); the picture illustrates the narration line over it; reference_images.source only canonical existing files (locations/<id>/<id>.png, characters/<id>/<id>_side.png). Keep every editing-grammar field (edit.cut_in/cut_out, handles, readable_props). Write ONLY {\"shots\": [...]} for these $(echo "$ids" | tr ',' '\n' | wc -l) shots, same schema, to $fix. Do NOT modify shot_list.json."
        rm -f "$fix"
        run_dsh_agent "director" "$RUN_DIR/prompt_director_gate_${scene}.txt" "$fix" || true
        if [ -s "$fix" ] && python3 "$PACK_DIR/pipeline/splice_scene.py" "$RUN_DIR/shot_list.json" "$scene" "$fix" 2>&1 | tee -a "$RUN_DIR/pipeline.log"; then
            log "[gate $scene] Director fix round $rounds spliced"
        else
            log "[gate $scene] Director fix round $rounds rejected or missing — retrying"
        fi
    done
    log "[gate $scene] $lint_out"
    return 0
}

for SHOT_ID in $SHOT_IDS; do
    CURRENT=$((CURRENT + 1))
    CLIP_DIR="$RUN_DIR/clips/$SHOT_ID"
    mkdir -p "$CLIP_DIR"

    # Check --from-clip / --only-clip filters
    if ! should_process_clip "$SHOT_ID"; then
        log "SKIP (filter): $SHOT_ID"
        continue
    fi

    SHOT_SCENE=$(python3 -c "import json; print(next((s.get('scene_id','') for s in json.load(open('$RUN_DIR/shot_list.json'))['shots'] if s.get('shot_id')=='$SHOT_ID'), ''))" 2>/dev/null || echo "")
    if [ -n "$SHOT_SCENE" ] && [ "$SHOT_SCENE" != "${LAST_GATED_SCENE:-}" ]; then
        LAST_GATED_SCENE="$SHOT_SCENE"
        ensure_local_llm_running
        scene_gate "$SHOT_SCENE"
    fi

    # Resume: skip clips that already passed QA
    if [ "$RESUME" = "true" ] && [ -z "$ONLY_CLIP" ] && [ -f "$CLIP_DIR/qa_verdict.json" ]; then
        PREV_VERDICT=$(python3 -c "import json; print(json.load(open('$CLIP_DIR/qa_verdict.json')).get('verdict',''))" 2>/dev/null)
        if [ "$PREV_VERDICT" = "PASS" ]; then
            log "SKIP (resume): $SHOT_ID — already passed QA"
            continue
        fi
    fi

    step "CLIP $CURRENT/$TOTAL: $SHOT_ID"
    notify "Clip $CURRENT/$TOTAL: $SHOT_ID starting" "Clip Generation"

    # On resume, carry forward retry count
    RETRY=0
    if [ "$RESUME" = "true" ] && [ -f "$CLIP_DIR/qa_verdict.json" ]; then
        RETRY=$(python3 -c "import json; print(json.load(open('$CLIP_DIR/qa_verdict.json')).get('retry_count', 0))" 2>/dev/null || echo 0)
    fi
    MAX_RETRY=3   # each take ≈ 30 min (review 8 + render 6 + QA 12 + two LLM reloads); best take is restored at escalation
    PASSED=false

    # ── Stage-level resume ──────────────────────────────────
    # PASS already earned → nothing left in this loop (upscale is batch now).
    # This shouldn't normally trigger (the resume skip above catches it), but
    # handles edge cases like RETRY>0 with a stale PASS verdict file.
    SKIP_TO_UPSCALE=false
    if [ "$RESUME" = "true" ] && [ -z "$ONLY_CLIP" ] && [ $RETRY -eq 0 ] \
       && [ -f "$CLIP_DIR/qa_verdict.json" ]; then
        PREV_VERDICT=$(python3 -c "import json; print(json.load(open('$CLIP_DIR/qa_verdict.json')).get('verdict',''))" 2>/dev/null || echo FAIL)
        if [ "$PREV_VERDICT" = "PASS" ]; then
            log "[$SHOT_ID] QA already PASS — skipping (upscale deferred to batch)"
            continue
        fi
    fi

    # Per-stage skip flags (RETRY==0 resume only; cleared on corrective retry)
    STAGE_OK=false
    if [ "$RESUME" = "true" ] && [ -z "$ONLY_CLIP" ] && [ $RETRY -eq 0 ]; then
        STAGE_OK=true
    fi

    while [ "$PASSED" = "false" ] && [ $RETRY -lt $MAX_RETRY ]; do
        [ $RETRY -gt 0 ] && log "RETRY $RETRY/$MAX_RETRY for $SHOT_ID"
        # Corrective retries must re-run every stage with QA feedback
        [ $RETRY -gt 0 ] && STAGE_OK=false

        # ── Cross-clip continuity: extract reference frames from scene ──
        CONTINUITY_REFS=""
        CONTINUITY_FRAME_INFO=$(python3 -c "
import json, sys, os
shot_id = '$SHOT_ID'
run_dir = '$RUN_DIR'
try:
    shots = json.load(open(os.path.join(run_dir, 'shot_list.json')))['shots']
    idx = next(i for i, s in enumerate(shots) if s.get('shot_id') == shot_id)
    scene_id = shots[idx]['scene_id']
    result = {}
    # Previous clip in same scene → last frame (start-of-clip anchor)
    if idx > 0 and shots[idx-1]['scene_id'] == scene_id:
        prev_id = shots[idx-1]['shot_id']
        prev_clip = os.path.join(run_dir, 'clips', prev_id, 'clip.mp4')
        if os.path.exists(prev_clip):
            result['prev'] = prev_id
    # First clip in scene → first frame (establishing shot anchor for room layout)
    scene_first = next(s for s in shots if s['scene_id'] == scene_id)
    if scene_first['shot_id'] != shot_id:
        first_id = scene_first['shot_id']
        first_clip = os.path.join(run_dir, 'clips', first_id, 'clip.mp4')
        if os.path.exists(first_clip) and first_id != result.get('prev'):
            result['first'] = first_id
    # Next clip in same scene → first frame (if already generated, for end-of-clip anchor)
    if idx < len(shots) - 1 and shots[idx+1]['scene_id'] == scene_id:
        next_id = shots[idx+1]['shot_id']
        next_clip = os.path.join(run_dir, 'clips', next_id, 'clip.mp4')
        if os.path.exists(next_clip):
            result['next'] = next_id
    print(json.dumps(result))
except Exception:
    print('{}')
" 2>/dev/null || echo "{}")

        PREV_CLIP_ID=$(echo "$CONTINUITY_FRAME_INFO" | python3 -c "import json,sys; print(json.load(sys.stdin).get('prev',''))" 2>/dev/null || echo "")
        FIRST_CLIP_ID=$(echo "$CONTINUITY_FRAME_INFO" | python3 -c "import json,sys; print(json.load(sys.stdin).get('first',''))" 2>/dev/null || echo "")
        NEXT_CLIP_ID=$(echo "$CONTINUITY_FRAME_INFO" | python3 -c "import json,sys; print(json.load(sys.stdin).get('next',''))" 2>/dev/null || echo "")

        # Extract previous clip's last frame (most important — start-of-clip anchor)
        if [ -n "$PREV_CLIP_ID" ]; then
            PREV_LASTFRAME="$CLIP_DIR/prev_clip_lastframe.png"
            if [ ! -f "$PREV_LASTFRAME" ] || [ $RETRY -gt 0 ]; then
                ffmpeg -y -sseof -0.1 -i "$RUN_DIR/clips/$PREV_CLIP_ID/clip.mp4" -frames:v 1 -q:v 2 "$PREV_LASTFRAME" 2>/dev/null && \
                    log "[$SHOT_ID] Extracted last frame from prev clip $PREV_CLIP_ID" || \
                    log "[$SHOT_ID] WARNING: Could not extract last frame from $PREV_CLIP_ID"
            fi
            [ -f "$PREV_LASTFRAME" ] && CONTINUITY_REFS="CONTINUITY (previous clip $PREV_CLIP_ID last frame): $PREV_LASTFRAME — look at it and carry over its STATE only: light direction and colour, weather, readable prop states, costume, where each character was left. NEVER describe the previous clip's framing or layout — this clip opens on ITS OWN framing from the shot list in the first sentence of [Shot 1], and anything the previous shot showed that is outside this framing (the square, the bell, the quay in a medium of a character on the cliff) is at most one background clause after the subject, or omitted. Two compositions in one prompt = H3 cuts between them (S01_004 x3). It is NOT a ref2va reference and must NOT appear in reference_assignments."
        fi

        # Extract scene establishing shot's first frame (room layout anchor)
        if [ -n "$FIRST_CLIP_ID" ]; then
            SCENE_FIRSTFRAME="$CLIP_DIR/scene_establishing_frame.png"
            if [ ! -f "$SCENE_FIRSTFRAME" ] || [ $RETRY -gt 0 ]; then
                ffmpeg -y -i "$RUN_DIR/clips/$FIRST_CLIP_ID/clip.mp4" -frames:v 1 -q:v 2 "$SCENE_FIRSTFRAME" 2>/dev/null && \
                    log "[$SHOT_ID] Extracted establishing frame from scene first clip $FIRST_CLIP_ID" || \
                    log "[$SHOT_ID] WARNING: Could not extract establishing frame from $FIRST_CLIP_ID"
            fi
            [ -f "$SCENE_FIRSTFRAME" ] && CONTINUITY_REFS="$CONTINUITY_REFS SCENE LAYOUT (establishing shot $FIRST_CLIP_ID first frame): $SCENE_FIRSTFRAME — keeps left/right geography consistent (what is screen-left stays screen-left); mention only the parts of it that are inside THIS shot's framing. NOT a ref2va reference."
        fi

        # Extract next clip's first frame if it exists (end-of-clip anchor for retries)
        if [ -n "$NEXT_CLIP_ID" ]; then
            NEXT_FIRSTFRAME="$CLIP_DIR/next_clip_firstframe.png"
            if [ ! -f "$NEXT_FIRSTFRAME" ] || [ $RETRY -gt 0 ]; then
                ffmpeg -y -i "$RUN_DIR/clips/$NEXT_CLIP_ID/clip.mp4" -frames:v 1 -q:v 2 "$NEXT_FIRSTFRAME" 2>/dev/null && \
                    log "[$SHOT_ID] Extracted first frame from next clip $NEXT_CLIP_ID" || \
                    log "[$SHOT_ID] WARNING: Could not extract first frame from $NEXT_CLIP_ID"
            fi
            # NOT passed to the reviewer: feeding the next clip's first frame to ref2va as an
            # "end anchor" copied its whole crowd into S01_001 (rules §0). The file stays on
            # disk for the keyframe planner (fl2va last frame) only.
        fi

        # ── 2a. Screenplay Reviewer — skip if reviewed_prompt.json already done ──
        if [ "$STAGE_OK" = "true" ] && [ -s "$CLIP_DIR/reviewed_prompt.json" ]; then
            log "[$SHOT_ID] SKIP (resume): prompt review — reviewed_prompt.json exists"
        else
            log "[$SHOT_ID] Prompt review..."
            CORRECTIONS=""
            # Compact feedback: the verdict summary + up to 5 corrections. Dumping the whole
            # verdict (16 corrections, pixel gates) is how prompts grew to 3 000 words.
            [ -f "$CLIP_DIR/qa_verdict.json" ] && CORRECTIONS=$(python3 - "$CLIP_DIR/qa_verdict.json" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
out = ["Previous take QA: " + str(d.get("verdict", "?"))]
s = d.get("summary")
if isinstance(s, str) and s.strip():
    out.append("Summary: " + s.strip()[:600])
sc = d.get("scores") or d.get("categories") or d.get("category_scores") or {}
low = []
for k, v in (sc.items() if isinstance(sc, dict) else []):
    x = v.get("score") if isinstance(v, dict) else v
    if isinstance(x, (int, float)) and x < 0.75:
        low.append(f"{k} {x:.2f}")
if low:
    out.append("Low categories: " + ", ".join(low))
corr = d.get("corrections") or d.get("prompt_corrections") or []
lines = []
for c in (corr if isinstance(corr, list) else [corr])[:5]:
    t = c if isinstance(c, str) else (c.get("prompt_fix") or c.get("fix") or c.get("description") or json.dumps(c))
    lines.append("- " + str(t).strip()[:300])
if lines:
    out.append("Top corrections (apply the structural ones, REWRITE the prompt, do not append):")
    out.extend(lines)
print(" ".join(out).replace("\n", " "))
PYEOF
)
            SHOT_H3_MODE=$(python3 - "$RUN_DIR/shot_list.json" "$SHOT_ID" "$CLIP_DIR" <<'PYEOF'
import json, os, sys
m = "reference"
try:
    shots = json.load(open(sys.argv[1]))["shots"]
    s = next(x for x in shots if x.get("shot_id") == sys.argv[2])
    m = (s.get("keyframe_strategy") or {}).get("mode") or (s.get("production_notes") or {}).get("minimax_h3_mode") or "reference"
except Exception:
    pass
# Every shot is keyframed (rules §0.1): ref2va pulled every reference-mode take out to a wide
# (S01_001 x10, S01_004 take 6) while i2va from a composited keyframe held its framing in every
# take. "reference" therefore maps to i2va too; ref2va only remains for text_only shots.
table = {"reference": "i2va", "ref2va": "i2va", "composited_i2v": "i2va", "first_frame_only": "i2va",
         "first_from_previous": "i2va", "i2va": "i2va", "first_last_frame": "fl2va", "fl2va": "fl2va", "text_only": "ref2va"}
mode = table.get(str(m).lower(), "ref2va")
# a rendered first frame from an earlier take forces i2va (never discard it again)
try:
    fm = json.load(open(os.path.join(sys.argv[3], "frame_meta.json")))
    if mode == "ref2va" and isinstance(fm.get("first_frame"), str) and os.path.isfile(fm["first_frame"]):
        mode = "fl2va" if isinstance(fm.get("last_frame"), str) and os.path.isfile(fm["last_frame"]) else "i2va"
except Exception:
    pass
print(mode)
PYEOF
)
            [ -z "$SHOT_H3_MODE" ] && SHOT_H3_MODE=ref2va

            write_prompt "$CLIP_DIR/prompt_review.txt" \
                "Review shot $SHOT_ID from $RUN_DIR/shot_list.json. H3 MODE for this shot: $SHOT_H3_MODE — write exactly that in h3_mode (ref2va = 6-section format: subject_definitions, summary, retention_analysis, detailed_description, overall_soundscape, non_diegetic_music; i2va/fl2va = the timecoded [Xs-Ys] format with the first/last frame as the literal start/end state). HARD LIMITS (knowledge/minimax_h3_rules.md section 0): whole prompt <= 900 words, detailed_description 350-500 words; NO frame numbers, pixel sizes, percentages, hex colours or 'exactly N' counts; <= 3 named subjects, extras uncounted with backs to camera; faces/hands only at medium or closer; ONE composition per clip: [Shot 1] opens on this shot's own framing and subject and never describes another framing (not the previous clip's, not a wider view of the location); camera only in the official vocabulary (push in / pull out / pan / truck / tilt / pedestal / arc / tracking / static, 'with small amplitude at slow speed'); one beat, one continuous shot. reference_assignments: ONLY design sheets under $RUN_DIR/characters/ (one per character in frame) and the location plate under $RUN_DIR/locations/, <= 5 images, never a prev/next clip frame. DIALOGUE lines get <d> tags + <Audio> ref. NARRATION clips: NO <d> tags, describe characters with closed lips. On a retry REWRITE from the shot list and apply only the top 3 structural corrections. $CONTINUITY_REFS $CORRECTIONS"

            run_dsh_agent "screenplay-reviewer" "$CLIP_DIR/prompt_review.txt" "$CLIP_DIR/reviewed_prompt.json"
        fi

        # ── 2b. Dialogue audio (if needed) ──
        # shot_list.json is authoritative: reviewed_prompt.json is often prose
        # (screenplay-reviewer omits the JSON envelope), so json.load fails and a
        # final_prompt-only check silently skips TTS → H3 invents audio (QA blocker).
        HAS_DIALOGUE=$(python3 -c "
import json, sys
shot_id = '$SHOT_ID'
# 1) shot_list has dialogue lines?
try:
    shots = json.load(open('$RUN_DIR/shot_list.json'))['shots']
    shot = next(s for s in shots if s.get('shot_id') == shot_id)
    print('yes' if shot.get('audio', {}).get('dialogue') else 'no')   # shot list is authoritative
    sys.exit(0)
except Exception:
    pass
# 2) shot missing from the list: fall back to the FINAL PROMPT only (never the JSON envelope,
#    whose changes_made notes say "no <d> tags" and used to trigger a doomed Orpheus call)
try:
    d = json.load(open('$CLIP_DIR/reviewed_prompt.json'))
    p = d.get('final_prompt') or ''
    if '<d>' in p or '<Audio' in p:
        print('yes')
        sys.exit(0)
except Exception:
    pass
# 3) companion H3 prompt under prompts/
import os, glob
for path in glob.glob(os.path.join('$RUN_DIR', 'prompts', shot_id + '*')):
    try:
        raw = open(path, encoding='utf-8', errors='replace').read()
    except Exception:
        continue
    if '<d>' in raw or '<Audio' in raw:
        print('yes')
        sys.exit(0)
print('no')
" 2>/dev/null || echo no)

        # Generate dialogue if: prompt expects it AND (wav missing OR prior synth failed).
        # Do NOT clobber a valid existing stem on every retry — Chatterbox/CosyVoice may
        # be down, and a good pre-rendered stem is required for H3 lip sync.
        NEED_DIALOGUE=false
        if [ "$HAS_DIALOGUE" = "yes" ]; then
            if [ ! -s "$CLIP_DIR/dialogue.wav" ]; then
                log "[$SHOT_ID] dialogue.wav MISSING — must generate before video"
                NEED_DIALOGUE=true
            elif [ ! -s "$CLIP_DIR/dialogue_meta.json" ]; then
                log "[$SHOT_ID] dialogue.wav present but dialogue_meta.json missing — re-running audio-producer"
                NEED_DIALOGUE=true
            elif [ $RETRY -gt 0 ]; then
                # Keep existing stem unless meta records a failed synth.
                # exit 0 = meta OK (keep), exit 1 = failed/missing fields (regen)
                if python3 -c "
import json, sys
try:
    d = json.load(open('$CLIP_DIR/dialogue_meta.json'))
except Exception:
    sys.exit(1)
sys.exit(1 if (d.get('failed') or str(d.get('status','')).lower() in ('failed','error')) else 0)
" 2>/dev/null; then
                    log "[$SHOT_ID] Keeping existing dialogue.wav (meta ok) — not clobbering stem on retry"
                else
                    log "[$SHOT_ID] Retry — regenerating dialogue audio with corrected emotion"
                    NEED_DIALOGUE=true
                fi
            fi
        fi

        if [ "$NEED_DIALOGUE" = "true" ]; then
            log "[$SHOT_ID] Generating dialogue audio (before video for lip sync)..."
            # Local mode: use direct Orpheus script (handles VRAM dance internally —
            # stops NInfer, starts Orpheus, generates audio, stops Orpheus).
            # Avoids the VRAM conflict where NInfer-US (28GB) + Orpheus (5GB) > 32GB.
            if [[ "${CLOUD_ROUTING:-1}" == "0" ]]; then
                log "[$SHOT_ID] Direct Orpheus audio generation (local VRAM dance)..."
                # Signal the idle-monitor that TTS services are expected up now,
                # so it reclaims VRAM to NInfer if one goes stuck mid-generation.
                touch "$OUTPUT_DIR/.audio_phase_active"
                python3 "$PACK_DIR/pipeline/generate_dialogue_audio.py" \
                    "$RUN_DIR/shot_list.json" "$SHOT_ID" "$CLIP_DIR" 2>&1 | \
                    while IFS= read -r line; do log "  $line"; done
                rm -f "$OUTPUT_DIR/.audio_phase_active"
                if [ -s "$CLIP_DIR/dialogue.wav" ]; then
                    log "[$SHOT_ID] Dialogue audio ready"
                else
                    log "[$SHOT_ID] WARNING: Direct audio generation failed"
                fi
            else
            swap_to_27b
            # Start Orpheus TTS if not running (primary dialogue engine)
            if ! curl -sf http://127.0.0.1:9883/v1/audio/voices > /dev/null 2>&1; then
                log "[$SHOT_ID] Starting Orpheus TTS..."
                systemctl start orpheus-backend.service 2>/dev/null || true
                sleep 3
                systemctl start orpheus-tts.service 2>/dev/null || true
                for _w in $(seq 1 30); do
                    curl -sf http://127.0.0.1:9883/v1/audio/voices > /dev/null 2>&1 && break
                    sleep 1
                done
                if curl -sf http://127.0.0.1:9883/v1/audio/voices > /dev/null 2>&1; then
                    log "[$SHOT_ID] Orpheus TTS ready on :9883"
                else
                    log "[$SHOT_ID] WARNING: Orpheus TTS not responding — agent will fall back to Chatterbox"
                fi
            fi
            # Extract emotion info from shot list for the prompt
            DIALOGUE_INFO=$(python3 -c "
import json
with open('$RUN_DIR/shot_list.json') as f:
    shots = json.load(f)['shots']
shot = next((s for s in shots if s['shot_id'] == '$SHOT_ID'), None)
if shot:
    for d in shot.get('audio',{}).get('dialogue',[]):
        print(f\"Character: {d.get('character_id','?')}\")
        print(f\"Line: {d.get('line','')}\")
        print(f\"Emotion: {d.get('emotion','neutral')}\")
        print(f\"Start: {d.get('start_time',0)}s  End: {d.get('end_time','?')}s\")
        print()
" 2>/dev/null)
            # Extract emotional beat from shot list
            EMOTIONAL_BEAT=$(python3 -c "
import json
with open('$RUN_DIR/shot_list.json') as f:
    shots = json.load(f)['shots']
shot = next((s for s in shots if s['shot_id'] == '$SHOT_ID'), None)
if shot:
    print(shot.get('emotional_beat', 'neutral'))
" 2>/dev/null || echo "neutral")

            write_prompt "$CLIP_DIR/prompt_dialogue.txt" \
                "Generate dialogue audio for $SHOT_ID. This must sound like a REAL ACTOR performing in a Pixar movie — expressive, natural, alive.

Voice refs: $RUN_DIR/audio/voices/
Shot data from shot_list.json:
$DIALOGUE_INFO

Scene emotional beat: $EMOTIONAL_BEAT

ENGINE PRIORITY — USE ORPHEUS 3B (port 9883) AS DEFAULT:
Orpheus produces the most natural, expressive speech. Use it for ALL dialogue unless unavailable.
API: POST http://localhost:9883/v1/audio/speech
Body: {"model":"orpheus","input":"<text>","voice":"<voice>","response_format":"wav"}
Voices: tara, leah, jess, leo, dan, mia, zac, zoe

1. Orpheus controls emotion through NATURAL PROSODY from text context, NOT emotion-name tags.
   Write the dialogue with expressive punctuation and word choice.
   Add inline paralinguistic sounds where natural:
   - <laugh> — laughter    - <chuckle> — soft chuckle
   - <sigh> — sigh         - <gasp> — gasp of surprise
   - <sniffle> — sniffle   - <groan> — groan
   Example: 'Tobias love <chuckle> you have got a gear in your porridge again!'
   Example: '<gasp> You found the missing piece!'
   Example: '<sigh> I suppose you are right about that...'

2. Don't over-tag — one or two sounds per line max. Let the text carry the emotion.

3. If Orpheus (9883) is DOWN, fall back to Chatterbox (9882) with:
   - exaggeration=0.5 for moderate emotion, 0.7 for strong
   - Paralinguistic tags: [laugh], [sigh], [gasp] in the text

4. Voice reference: use the character's ref WAV from $RUN_DIR/audio/voices/
   Assign each character a consistent Orpheus voice (e.g., elderly woman=leah, child=jess).

QUALITY STANDARD: The output must NOT sound robotic, monotone, or flat.
It should sound like a warm, expressive human performance with natural
breath patterns, rhythm variation, and emotional coloring matching the scene.

Save to $CLIP_DIR/dialogue.wav and $CLIP_DIR/dialogue_meta.json (include engine_used, voice, character_id)."
            run_dsh_agent "audio-producer" "$CLIP_DIR/prompt_dialogue.txt" "$CLIP_DIR/dialogue_meta.json"
            fi
        fi

        # ── 2c. Image Generation — skip if frame_meta exists or mode needs no frames ──
        # The SHOT LIST decides whether a keyframe is rendered (S01_002 was planned
        # composited_i2v, the reviewer wrote h3_mode ref2va, and the rendered first
        # frame was never used). ref2va needs no frame; every other mode does.
        NEED_FRAMES=$(python3 -c "
import json
try:
    shots = json.load(open('$RUN_DIR/shot_list.json'))['shots']
    s = next(x for x in shots if x.get('shot_id') == '$SHOT_ID')
    mode = str((s.get('keyframe_strategy') or {}).get('mode') or 'reference').lower()
    # every shot renders a keyframe (ref2va drifts wide); only text_only skips it
    print('no' if mode in ('t2v', 'text_only', 'text_to_video') else 'yes')
except Exception:
    print('yes')
" 2>/dev/null || echo yes)
        if [ "$STAGE_OK" = "true" ] && { [ -s "$CLIP_DIR/frame_meta.json" ] || [ "$NEED_FRAMES" = "no" ]; }; then
            log "[$SHOT_ID] SKIP (resume): image generation — frames already done or not required"
        else
            log "[$SHOT_ID] Planning frames (agent) ..."
            ensure_local_llm_running
            # The agent only PLANS: ComfyUI cannot run beside its LLM. It writes
            # frame_spec.json; generate_frames.py renders it with the GPU handed
            # to ComfyUI and writes frame_meta.json for the video step.
            write_prompt "$CLIP_DIR/prompt_image.txt" \
                "Plan the keyframes for $SHOT_ID — DO NOT call ComfyUI (it is down while you run; the pipeline renders your spec). Read $CLIP_DIR/reviewed_prompt.json (h3_mode, final_prompt, reference_assignments), $RUN_DIR/shot_list.json (this shot's camera, characters_in_frame with positions/hand_position/facing, location_id, lighting, edit.cut_in/cut_out, readable_props) and the manifests $RUN_DIR/character_manifest.json / $RUN_DIR/location_manifest.json for reference image paths. Write $CLIP_DIR/frame_spec.json: {\"mode\": \"composited_i2v|first_frame_only|first_last_frame|reference\", \"frames\": [{\"role\": \"first_frame\", \"prompt\": <Qwen Image 2.1 prompt: shot size FIRST and LAST, every character exactly once with position/pose/gaze from edit.cut_in, the location's layout, lighting, readable prop values verbatim>, \"references\": [<abs paths: ONE design sheet per character in frame, the location plate, and continuity frames prev_clip_lastframe.png / scene_establishing_frame.png if present in $CLIP_DIR>], \"width\": 1344, \"height\": 768, \"background\": <one sentence: the environment behind the figures, from the location plate>, \"placements\": [{\"character_id\": <id>, \"bbox\": [y_min, x_min, y_max, x_max] in 0-1000 of the frame (where the figure stands and how tall it is for THIS shot size: full-body in a wide = 300-900 tall, medium = 150-1000, close-up = 0-1000), \"desc\": <the figure in one sentence: pose from edit.cut_in, facing, costume colours, hand position>}] one per character in frame, \"style\": {\"aesthetics\": ..., \"lighting\": <from the shot>, \"art_style\": \"Pixar-style 3D animated feature film\", \"medium\": \"3d_render\"}}, {\"role\": \"last_frame\", ...only for first_last_frame, pose from edit.cut_out}], \"video_references\": [{\"tag\": \"<Subject N>\", \"image\": <abs path>, \"role\": ...}]}. For mode reference write frames: [] (no keyframe needed). Never name an object you want absent (Qwen ignores negatives); describe only what IS in frame."
            run_dsh_agent "image-generator" "$CLIP_DIR/prompt_image.txt" "$CLIP_DIR/frame_spec.json"
            touch "$OUTPUT_DIR/.gen_phase_active"
            # FRAME_ENGINE=qwen (default): Qwen Image 2.1 with the design sheets as references.
            # FRAME_ENGINE=ideogram4: Ideogram 4 bbox layout pass + Qwen identity pass
            # (pipeline/ideogram_frames.py; needs the ideogram4 core package + gated weights —
            # exit 4 = not installed, falls back to the Qwen path).
            # A retry MUST re-render the keyframe: the renderers skip when frame_meta.json
            # already points at a frame, and S01_001 take 2 reused take 1's roped keyframe
            # that way (QA had asked for a rope-free one).
            FORCE_FRAMES=""
            [ "$RETRY" -gt 0 ] && FORCE_FRAMES="--force"
            FRAMES_OK=false
            if [ "${FRAME_ENGINE:-qwen}" = "ideogram4" ]; then
                if python3 "$PACK_DIR/pipeline/ideogram_frames.py" --shot-dir "$CLIP_DIR" --run-dir "$RUN_DIR" \
                        --comfyui-url "$COMFYUI_URL" --keep-gpu $FORCE_FRAMES >> "$RUN_DIR/pipeline.log" 2>&1; then
                    FRAMES_OK=true
                    log "[$SHOT_ID] frames rendered (ideogram4 layout + qwen identity)"
                else
                    log "[$SHOT_ID] WARNING: ideogram_frames.py failed — falling back to generate_frames.py"
                fi
            fi
            if [ "$FRAMES_OK" = "false" ]; then
                if python3 "$PACK_DIR/pipeline/generate_frames.py" --shot-dir "$CLIP_DIR" --run-dir "$RUN_DIR" \
                        --comfyui-url "$COMFYUI_URL" --keep-gpu $FORCE_FRAMES >> "$RUN_DIR/pipeline.log" 2>&1; then
                    log "[$SHOT_ID] frames rendered"
                else
                    log "[$SHOT_ID] WARNING: generate_frames.py failed — video step will use references only"
                fi
            fi
            rm -f "$OUTPUT_DIR/.gen_phase_active"
        fi

        # ── 2d. Video Generation — skip if clip.mp4 exists and generation completed ──
        VIDEO_DONE=false
        if [ -f "$CLIP_DIR/clip.mp4" ] && python3 -c "
import json, sys
try:
    d = json.load(open('$CLIP_DIR/generation_log.json'))
    sys.exit(0 if d.get('status') == 'completed' else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
            VIDEO_DONE=true
        fi
        if [ "$STAGE_OK" = "true" ] && [ "$VIDEO_DONE" = "true" ]; then
            log "[$SHOT_ID] SKIP (resume): video generation — clip.mp4 already completed"
        else
            # generate_clip.py: route agent → queue H3 → wait → download 
            # (cloud routing: no local LLM to start/stop around generation)
            log "[$SHOT_ID] Video generation (direct ComfyUI, no LLM during generation)..."
            # Signal the idle-monitor that ComfyUI is expected up for video gen.
            touch "$OUTPUT_DIR/.gen_phase_active"
            GEN_EXIT=0
            python3 "$PACK_DIR/pipeline/generate_clip.py" \
                --shot-dir "$CLIP_DIR" \
                --run-dir "$RUN_DIR" \
                --comfyui-url "$COMFYUI_URL" \
                --shot-id "$SHOT_ID" \
                2>> "$RUN_DIR/pipeline.log" || GEN_EXIT=$?
            rm -f "$OUTPUT_DIR/.gen_phase_active"

            if [ $GEN_EXIT -ne 0 ]; then
                log "[$SHOT_ID] WARNING: generate_clip.py exited $GEN_EXIT"
                notify "Clip $SHOT_ID generation script failed (exit $GEN_EXIT)" "Generation Warning"
            fi
        fi

        # Monitoring: LLM must be up for QA whether we generated or resumed mid-clip
        ensure_local_llm_running

        # BUGFIX: never run QA against a clip that was not actually produced.
        # If the video step failed to write clip.mp4, running the QA inspector
        # only produces a bogus "no clip to review" FAIL — which the gate below
        # would consume as a real clip retry (burnt cycles on an unjudged clip,
        # exactly the S01_001 race). Treat a missing clip as a GENERATION failure:
        # re-attempt the video render (bounded by MAX_RETRY), never a QA verdict.
        if [ ! -f "$CLIP_DIR/clip.mp4" ]; then
            log "[$SHOT_ID] WARNING: clip.mp4 not produced after video step — retrying generation, not QA"
            # Guard against corrupt/stale qa_verdict that could short-circuit
            rm -f "$CLIP_DIR/qa_verdict.json" "$CLIP_DIR/qa_report.json" 2>/dev/null || true
            RETRY=$((RETRY + 1))
            if [ $RETRY -ge $MAX_RETRY ]; then
                log "[$SHOT_ID] MAX_RETRY reached with no clip produced — escalating, not a QA verdict"
                notify "Clip $SHOT_ID generated no clip after $RETRY attempts" "ESCALATION"
                PASSED=false
                RETRY=$MAX_RETRY
                break
            fi
            log "[$SHOT_ID] Re-attempting video generation (attempt $RETRY/$MAX_RETRY)..."
            continue  # back to top of the per-clip while loop → re-run video step
        fi

        # ── 2e. QA Review ──
        # Always run when we reached the loop without a PASS (no prior verdict,
        # or FAIL being retried). SKIP_TO_UPSCALE short-circuits the whole loop.
        # Infra failures (agent crash / no parseable verdict) re-run QA only —
        # they must never consume a clip retry or restart 2a–2d.
        log "[$SHOT_ID] QA inspection..."

        # Build cross-clip continuity context
        CROSS_CLIP_CTX=$(python3 "$PACK_DIR/pipeline/qa_context.py" "$SHOT_ID" "$RUN_DIR" 2>/dev/null || echo "")

        write_prompt "$CLIP_DIR/prompt_qa.txt" \
            "Review $CLIP_DIR/clip.mp4 for a Pixar-quality animated movie. Score what a viewer sees at playback scale; the calibrated gate below decides (knowledge/minimax_h3_rules.md section 0).

CLIP CONTEXT: Shot $SHOT_ID from $RUN_DIR/shot_list.json.
Character references: $RUN_DIR/characters/
Location references: $RUN_DIR/locations/

FRAME EXTRACTION: Extract frames with ffmpeg at 2fps for general review, 10fps for motion, 30fps for lip sync.

QUALITY CHECKS (score 0.0-1.0 each; STRUCTURAL floor 0.70):
1. HANDS & LIMBS: merging, extra limbs, impossible positions — judged ONLY at medium framing or closer (0.85 'not resolvable' otherwise)
2. FACE & EXPRESSION: distortion, morphing, expression matches story beat — judged ONLY at medium framing or closer
3. CHARACTER IDENTITY: Match against reference sheets — hair, clothes, accessories, prosthetics
4. DUPLICATION: a NAMED character twice, or an identical clone pair side by side (a crowd of similar extras is not duplication)
5. MOTION: Smooth, physically plausible, no jitter/teleporting
6. LIP SYNC: Dialogue clips = lips move in sync; Narration clips = lips CLOSED
7. TEXTURE & VISUAL: No swimming textures, resolution drops, banding, noise
8. COMPOSITION: shot size and angle match the SHOT LIST (not the prompt's pixel targets or crowd counts)

CROSS-CLIP CONTINUITY CHECKS (score 0.0-1.0; STRUCTURAL floor 0.70):
9. CONTINUITY: Compare against adjacent clips in the same scene.
   If the previous clip exists, extract its LAST FRAME and compare with this clip's FIRST FRAME.
   Check: prop continuity (objects don't appear/disappear), character facing direction,
   180-degree rule, spatial layout (window/door/furniture positions stable), lighting,
   character state (sitting/standing), costume consistency, match-cut visual connection.

$CROSS_CLIP_CTX

VOICE QUALITY (dialogue clips only, score 0.0-1.0; SOFT category):
10. VOICE QUALITY: Analyze the dialogue.wav stem in this clip directory.
    Check for robotic indicators: monotone pitch, flat prosody, metallic timbre,
    missing breath sounds, emotion mismatch with the scene's emotional beat.
    Must sound like a REAL ACTOR — warm, expressive, naturally paced.
    Voice quality below 0.70 = critical failure (robotic voice is unacceptable).
    For narration-only clips (no dialogue.wav): skip, mark N/A.

CINEMATIC QUALITY (score 0.0-1.0; SOFT category):
11. FILM QUALITY: Does this look like a frame from a professional animated movie?
    Check: appealing color palette, proper depth of field, cinematic lighting,
    natural character posing, clean composition with visual weight balance,
    environment detail and atmosphere. A great movie doesn't just avoid artifacts —
    it looks BEAUTIFUL.

VERDICT RULES (calibrated gate — normalise_qa_verdict.py is the final arbiter):
- STRUCTURAL categories (hands_limbs, face_expression, character_identity, duplication, motion_quality, composition, continuity): each >= 0.70
- overall_score >= 0.75 (weighted as in the qa-inspection skill)
- SOFT categories (texture_visual, film_quality, audio_quality, voice_quality, lip_sync offset, exposure/saturation drift, prop presence, crowd size) never FAIL a clip on their own: write them as assembly/grading notes
- Never invent numeric gates (pixel heights, % luma, NCC, gradient energy, frame indices); measurements are evidence for a score, not rules
- re_render_recommended: true ONLY with a structural category < 0.70 or a critical structural correction
- Output JSON with verdict PASS/FAIL, scores for all 11 categories under the exact keys hands_limbs, face_expression, character_identity, duplication, motion_quality, lip_sync, texture_visual, composition, continuity, voice_quality, film_quality, overall_score, re_render_recommended, and <= 5 one-sentence corrections if FAIL"
        VERDICT=""
        QA_NORM=""
        for QA_TRY in 1 2 3; do
            run_dsh_agent "qa-inspector" "$CLIP_DIR/prompt_qa.txt" "$CLIP_DIR/qa_verdict.json" || true
            QA_NORM=$(python3 "$PACK_DIR/normalise_qa_verdict.py" "$CLIP_DIR" "$RETRY" 2>&1 || true)
            if [ -n "$QA_NORM" ]; then
                log "  $QA_NORM"
                echo "$QA_NORM" >> "$RUN_DIR/pipeline.log"
            fi
            VERDICT=$(python3 -c "
import json
try:
    with open('$CLIP_DIR/qa_verdict.json') as f:
        print(json.load(f).get('verdict',''))
except Exception:
    print('')
" 2>/dev/null)
            [ -n "$VERDICT" ] && break
            log "[$SHOT_ID] QA produced no verdict (infra try $QA_TRY/3) — re-running QA only"
        done

        # Unresolved = inspector never delivered a verdict (crash, prose-only,
        # stale report filtered). Do NOT map that to FAIL — that is how a prior
        # take's report (or a dead agent) burns H3 retries on an unjudged clip.
        if [ -z "$VERDICT" ]; then
            log "[$SHOT_ID] QA infrastructure failure (no verdict after 3 tries) — escalating, not a clip FAIL"
            notify "Clip $SHOT_ID QA infrastructure failure (no verdict after 3 tries)" "ESCALATION"
            PASSED=false
            RETRY=$MAX_RETRY
            break
        fi

        if [ "$VERDICT" = "PASS" ]; then
            PASSED=true
            log "[$SHOT_ID] QA PASSED"
            # Build QA score summary for Discord
            QA_SCORES=$(python3 -c "
import json
try:
    d = json.load(open('$CLIP_DIR/qa_verdict.json'))
    s = d.get('scores', {})
    parts = []
    for k in ('hands','face','character_identity','continuity','voice_quality','film_quality'):
        v = s.get(k, {})
        score = v.get('score', '?')
        parts.append(f'{k}: {score}')
    print(' | '.join(parts))
except: print('')
" 2>/dev/null || echo "")
            notify "Clip $CURRENT/$TOTAL ($SHOT_ID): QA PASSED\n$QA_SCORES" "QA PASS" "$CLIP_DIR/clip.mp4"
        else
            RETRY=$((RETRY + 1))
            log "[$SHOT_ID] QA FAILED (attempt $RETRY/$MAX_RETRY)"
            # Keep every take: a retry can score LOWER than the take it replaces
            # (S01_005: hands 0.87 -> 0.74). best_take.py restores the best at escalation.
            mkdir -p "$CLIP_DIR/takes"
            TAKE_N=$RETRY
            while [ -e "$CLIP_DIR/takes/take_${TAKE_N}.mp4" ]; do TAKE_N=$((TAKE_N + 1)); done   # never overwrite an earlier take
            cp -f "$CLIP_DIR/clip.mp4" "$CLIP_DIR/takes/take_${TAKE_N}.mp4" 2>/dev/null || true
            cp -f "$CLIP_DIR/qa_verdict.json" "$CLIP_DIR/takes/take_${TAKE_N}_verdict.json" 2>/dev/null || true
            [ -f "$CLIP_DIR/first_frame.png" ] && cp -f "$CLIP_DIR/first_frame.png" "$CLIP_DIR/takes/take_${TAKE_N}_first_frame.png" 2>/dev/null || true
            # Build failure summary for Discord
            QA_ISSUES=$(python3 -c "
import json
try:
    d = json.load(open('$CLIP_DIR/qa_verdict.json'))
    issues = d.get('issues', d.get('corrections', []))
    if isinstance(issues, list):
        for i in issues[:3]:
            if isinstance(i, dict):
                print(f\"- {i.get('category','?')}: {i.get('description', i.get('issue','?'))}\")
            else:
                print(f'- {i}')
    s = d.get('scores', {})
    fails = [f\"{k}: {v.get('score','?')}\" for k,v in s.items() if isinstance(v,dict) and not v.get('pass',True)]
    if fails: print('Failed: ' + ', '.join(fails[:5]))
except: print('')
" 2>/dev/null || echo "")
            notify "Clip $CURRENT/$TOTAL ($SHOT_ID): QA FAILED (attempt $RETRY/$MAX_RETRY)\n$QA_ISSUES" "QA FAIL" "$CLIP_DIR/clip.mp4"
        fi
    done

    if [ "$PASSED" = "false" ]; then
        python3 "$PACK_DIR/pipeline/best_take.py" "$CLIP_DIR" 2>&1 | tee -a "$RUN_DIR/pipeline.log" || true
        log "[$SHOT_ID] ESCALATED after $MAX_RETRY retries — halting the run for manual review"
        HALT_FILE="$RUN_DIR/HALT_FOR_REVIEW.json"
        python3 - "$HALT_FILE" "$SHOT_ID" "$CLIP_DIR" "$MAX_RETRY" "$CURRENT" "$TOTAL" <<'PYEOF'
import json, os, sys, time
halt, sid, clip_dir, max_retry, cur, total = sys.argv[1:7]
reason = ""
try:
    v = json.load(open(os.path.join(clip_dir, "qa_verdict.json")))
    reason = v.get("_re_render_reason") or v.get("summary") or ""
except Exception:
    pass
d = {"shot_id": sid, "clip": cur + "/" + total, "halted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
     "reason": f"QA FAIL after {max_retry} takes: " + str(reason)[:600],
     "takes": os.path.join(clip_dir, "takes"), "clip": os.path.join(clip_dir, "clip.mp4"),
     "to_accept": "touch " + os.path.join(clip_dir, "MANUAL_PASS"),
     "to_rerender": "touch " + os.path.join(clip_dir, "RETRY_AGAIN") + "  (edit shot_list.json / reviewed_prompt.json first if needed)",
     "then": "./launch_resume.sh " + os.path.dirname(os.path.dirname(clip_dir))}
tmp = halt + ".tmp"
json.dump(d, open(tmp, "w"), indent=2)
os.replace(tmp, halt)
PYEOF
        notify "Clip $CURRENT/$TOTAL ($SHOT_ID) ESCALATED — failed $MAX_RETRY takes.\nRUN HALTED for manual review.\nTakes: $CLIP_DIR/takes/\nAccept: touch $CLIP_DIR/MANUAL_PASS\nRe-render: touch $CLIP_DIR/RETRY_AGAIN\nThen: ./launch_resume.sh $RUN_DIR" "ESCALATION — RUN HALTED" "$CLIP_DIR/clip.mp4"
        log "[$SHOT_ID] halt record: $HALT_FILE — accept with MANUAL_PASS or re-render with RETRY_AGAIN, then ./launch_resume.sh $RUN_DIR"
        sleep 3   # let the notify post before the process tree goes away
        rm -f "$OUTPUT_DIR/.gen_phase_active" "$OUTPUT_DIR/.audio_phase_active"
        exit 3
    fi

    # ── 2g. Upscale deferred to batch pass after all clips generated ──
    # Per-clip upscale removed: wastes VRAM cycles during generation.
    # Batch upscale runs between Phase 2 and Phase 3.

    log "[$SHOT_ID] DONE ($CURRENT/$TOTAL)"

    # --only-clip: exit after processing the single clip
    if [ -n "$ONLY_CLIP" ]; then
        notify "Single clip $ONLY_CLIP processing complete" "Clip Done"
        log "=== SINGLE CLIP MODE COMPLETE ==="
        exit 0
    fi
done

# ═══════════════════════════════════════════════════════════
# PHASE 2g: BATCH UPSCALE (all passed clips → 4K 60fps)
# ═══════════════════════════════════════════════════════════

step "PHASE 2g: BATCH UPSCALE TO 4K 60fps"
notify "All clips generated. Batch upscaling to 4K 60fps..." "Upscale"
stop_all_llms
ensure_comfyui

UPS_DONE=0
UPS_SKIP=0
UPS_FAIL=0
for SHOT_ID in $SHOT_IDS; do
    CLIP_DIR="$RUN_DIR/clips/$SHOT_ID"
    [ -f "$CLIP_DIR/clip.mp4" ] || continue

    # Only upscale clips that passed QA
    if [ -f "$CLIP_DIR/qa_verdict.json" ]; then
        UPS_VERDICT=$(python3 -c "import json; print(json.load(open('$CLIP_DIR/qa_verdict.json')).get('verdict',''))" 2>/dev/null)
        [ "$UPS_VERDICT" != "PASS" ] && continue
    else
        continue
    fi

    # Skip if already up to date
    if [ -f "$CLIP_DIR/clip_4k60.mp4" ] && [ "$CLIP_DIR/clip_4k60.mp4" -nt "$CLIP_DIR/clip.mp4" ]; then
        UPS_SKIP=$((UPS_SKIP + 1))
        continue
    fi

    log "[$SHOT_ID] Upscaling to 4K 60fps..."
    UPS_FORCE=""
    [ -f "$CLIP_DIR/clip_4k60.mp4" ] && UPS_FORCE="--force"
    if python3 "$PACK_DIR/pipeline/upscale_clip.py" \
        --shot-dir "$CLIP_DIR" \
        --comfyui-url "$COMFYUI_URL" \
        $UPS_FORCE \
        2>> "$RUN_DIR/pipeline.log"; then
        log "[$SHOT_ID] 4K60 ready ($(stat -c%s "$CLIP_DIR/clip_4k60.mp4") bytes)"
        UPS_DONE=$((UPS_DONE + 1))
    else
        log "[$SHOT_ID] WARNING GPU upscale failed — CPU ffmpeg fallback"
        if ffmpeg -y -v error \
            -i "$CLIP_DIR/clip.mp4" \
            -vf "minterpolate=fps=60:mi_mode=mci:mc_mode=aobmc:vsbmc=1:me_mode=bidir:me=epzs,format=yuv420p,scale=3840:2160:flags=lanczos" \
            -c:v libx264 -preset medium -crf 17 -c:a aac \
            "$CLIP_DIR/clip_4k60.mp4"; then
            log "[$SHOT_ID] 4K60 ready (CPU fallback)"
            UPS_DONE=$((UPS_DONE + 1))
        else
            log "[$SHOT_ID] WARNING upscale failed, using original"
            cp "$CLIP_DIR/clip.mp4" "$CLIP_DIR/clip_4k60.mp4"
            UPS_FAIL=$((UPS_FAIL + 1))
        fi
    fi
done
log "Batch upscale complete: $UPS_DONE upscaled, $UPS_SKIP already done, $UPS_FAIL failed"
notify "Batch upscale done: $UPS_DONE new, $UPS_SKIP skipped, $UPS_FAIL failed" "Upscale"

# ═══════════════════════════════════════════════════════════
# PHASE 3: POST-PRODUCTION [Qwen 3.8 27B]
# ═══════════════════════════════════════════════════════════

step "PHASE 3: POST-PRODUCTION"
notify "All clips done. Phase 3: Post-production starting" "Post-Production"
swap_to_27b

# 3a. Narration
if ! skip_if_done "$RUN_DIR/audio/narration/narration_meta.json" "Narration"; then
    step "3a. Narration (Kokoro — no lip sync)"
    mkdir -p "$RUN_DIR/audio/narration"
    write_prompt "$RUN_DIR/prompt_narration.txt" \
        "Prepare ALL narration audio jobs — DO NOT call any TTS port (the engines are down while you run; the pipeline renders). Read $RUN_DIR/story.json (narrator lines per scene), $RUN_DIR/shot_list.json (narration spans: span_clips, which clip each block starts on) and the narrator entry of $RUN_DIR/audio/voices/voice_config.json (engine/voice/reference_audio/post_fx — use EXACTLY that cast, never Kokoro unless the cast says so). Write $RUN_DIR/audio/narration/tts_jobs.json: {\"jobs\": [{\"id\": \"N1\", \"engine\": ..., \"voice\"/\"reference_audio\"/\"post_fx\" from the cast, \"text\": <one narration block, sentence-level punctuation kept, ~2.2 words/s pace>, \"out\": \"$RUN_DIR/audio/narration/N1.wav\"}, ...]} and $RUN_DIR/audio/narration/narration_meta.json: {\"blocks\": [{\"id\": \"N1\", \"scene_id\": ..., \"span_clips\": [...], \"starts_on\": <first clip id>, \"text\": ..., \"file\": \"N1.wav\"}]} (durations are filled in by the renderer). Narration is post-only — NO lip sync, never overlaps dialogue clips."
    run_dsh_agent "audio-producer" "$RUN_DIR/prompt_narration.txt" "$RUN_DIR/audio/narration/narration_meta.json"
    if python3 "$PACK_DIR/pipeline/tts_render.py" --jobs "$RUN_DIR/audio/narration/tts_jobs.json" --run-dir "$RUN_DIR" >> "$RUN_DIR/pipeline.log" 2>&1; then
        log "narration rendered"
    else
        log "WARNING: some narration jobs failed — see $RUN_DIR/audio/narration/tts_jobs.results.json"
    fi
    python3 - "$RUN_DIR/audio/narration" <<'PYEOF'
import json, os, sys
d = sys.argv[1]
try:
    meta = json.load(open(os.path.join(d, "narration_meta.json")))
    res = {r["id"]: r for r in json.load(open(os.path.join(d, "tts_jobs.results.json")))["results"]}
except Exception as e:
    print(f"narration meta merge skipped: {e}"); sys.exit(0)
for b in meta.get("blocks", []):
    r = res.get(b.get("id"))
    if r:
        b["duration_s"] = r.get("duration_s"); b["status"] = r.get("status"); b["file"] = os.path.basename(r.get("out") or b.get("file", ""))
json.dump(meta, open(os.path.join(d, "narration_meta.json"), "w"), indent=2)
PYEOF
    notify "Narration audio rendered ($(python3 -c "import json;r=json.load(open('$RUN_DIR/audio/narration/tts_jobs.results.json'));print(r['ok'],'/',r['jobs'])" 2>/dev/null || echo '?'))" "Audio Producer"
fi

# 3b. Music
if ! skip_if_done "$RUN_DIR/music/music_meta.json" "Music"; then
    step "3b. Music & SFX"
    write_prompt "$RUN_DIR/prompt_music.txt" \
        "Create background score and SFX. Read $RUN_DIR/story.json for mood per scene. Generate separate stems. Save to $RUN_DIR/music/"
    run_dsh_agent "music-composer" "$RUN_DIR/prompt_music.txt" "$RUN_DIR/music/music_meta.json"
    notify "Music and SFX generated" "Music Composer"
fi

# 3c. Subtitles
if ! skip_if_done "$RUN_DIR/final/sub_meta.json" "Subtitles"; then
    step "3c. Subtitles"
    write_prompt "$RUN_DIR/prompt_subs.txt" \
        "Create timed subtitles (SRT + ASS). Read $RUN_DIR/shot_list.json and audio metadata. Dialogue: [CHARACTER]: text. Narration: (Narrator) italic. Save to $RUN_DIR/final/"
    run_dsh_agent "subtitle-generator" "$RUN_DIR/prompt_subs.txt" "$RUN_DIR/final/sub_meta.json"
    notify "Subtitles generated" "Subtitle Generator"
fi

# 3d-pre. Boundary check — does every clip-to-clip cut CUT? (editing-grammar skill)
step "3d-pre. Edit Check (clip boundaries)"
mkdir -p "$RUN_DIR/final"
if python3 "$PACK_DIR/pipeline/edit_check.py" --run-dir "$RUN_DIR" \
        --out "$RUN_DIR/final/edit_report.json" >> "$RUN_DIR/pipeline.log" 2>&1; then
    log "edit check: every boundary >= 0.80"
else
    EDIT_FAILS=$(python3 -c "import json; r=json.load(open('$RUN_DIR/final/edit_report.json')); print(r.get('failed',0), 'of', r.get('boundaries',0))" 2>/dev/null || echo "?")
    log "edit check: $EDIT_FAILS boundaries below 0.80 — editor must trim/insert (see final/edit_report.json + edit_boundaries.jpg)"
    notify "Edit check: $EDIT_FAILS boundaries below 0.80 — see final/edit_boundaries.jpg" "Edit Check" "$RUN_DIR/final/edit_boundaries.jpg"
fi

# 3d. Assembly (always re-run on resume — it's the final step)
step "3d. Final Assembly"
write_prompt "$RUN_DIR/prompt_assembly.txt" \
    "Assemble the final movie with ffmpeg as the EDITOR (final-assembly + editing-grammar skills):
- FIRST apply every entry of $RUN_DIR/assembly_fixes.json (blocking checklist)
- Read $RUN_DIR/final/edit_report.json and look at $RUN_DIR/final/edit_boundaries.jpg: for every boundary choose the cut inside the handles (a_out_s / b_in_s) so it lands on action or on a look; boundaries below 0.80 get a trim, an approved reaction/insert clip between them, or a re-render request — never a dissolve. Record each decision in $RUN_DIR/final/edit_decisions.json
- Video: trim each $RUN_DIR/clips/<SID>/clip_4k60.mp4 frame-accurately (-frames:v) to the chosen in/out, then concat with the demuxer and -c:v copy in shot order. STRAIGHT CUTS ONLY — no xfade unless transition_to_next.type says dissolve/fade. Write $RUN_DIR/final/concat.txt (it is the cut list for any later full-film pass). Never run RIFE/minterpolate on the assembled film
- Dialogue: strip the native H3 track; mux the APPROVED stem clips/<SID>/dialogue.wav at the QA-recorded offset, trimmed with the same in/out as the picture
- Overlay: narration from $RUN_DIR/audio/narration/ (no lip sync, sidechain-ducked under any line)
- Layer: music + SFX from $RUN_DIR/music/ (music and ambience may cross-fade over cuts; picture never does)
- Subtitles: burn ASS from $RUN_DIR/final/*.ass; loudnorm two-pass
- Output: $RUN_DIR/final/movie_4k60.mp4 (H.264, AAC 256k)
- Also: $RUN_DIR/final/movie_4k60_master.mov (ProRes, PCM)
- Re-run python3 $PACK_DIR/pipeline/edit_check.py --concat-list $RUN_DIR/final/concat.txt and include its result in $RUN_DIR/final/assembly_log.json"
run_dsh_agent "post-production-editor" "$RUN_DIR/prompt_assembly.txt" "$RUN_DIR/final/assembly_log.json"

# ═══════════════════════════════════════════════════════════
# DONE
# ═══════════════════════════════════════════════════════════

step "PRODUCTION COMPLETE"
log "Output: $RUN_DIR/final/"
ls -lh "$RUN_DIR/final/" 2>/dev/null
log ""
log "Movie:     $RUN_DIR/final/movie_4k60.mp4"
log "Subtitles: $RUN_DIR/final/*.srt"
log "Log:       $RUN_DIR/pipeline.log"

FINAL_SIZE=$(du -sh "$RUN_DIR/final/movie_4k60.mp4" 2>/dev/null | cut -f1 || echo "N/A")
notify "PRODUCTION COMPLETE\nMovie: $FINAL_SIZE\nClips: $TOTAL\nPath: $RUN_DIR/final/movie_4k60.mp4" "Production Complete"
