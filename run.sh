#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
# Production Pack — End-to-End Movie Generation Pipeline
# Usage: ./run.sh <story.md>
#
# Takes a story markdown file and produces a complete Pixar-style
# animated movie with dialogue (lip-synced), narration, BGM,
# subtitles, upscaled to 4K 60fps.
#
# Server: RTX 5090 32GB — ComfyUI | TTS cluster own the GPU; every agent is in the cloud.
# Any error halts the run (HALT_FOR_REVIEW.json + Discord); ./launch_resume.sh resumes it.
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

# ── The GPU has exactly TWO owners: ComfyUI | TTS cluster (no local LLM since 2026-10-18) ──
# systemd Conflicts= (pipeline/systemd/) enforce it; gpu_owner.sh switches owner
# and waits for the new one's API. Every agent runs in the cloud.
# shellcheck source=/dev/null
source "$PACK_DIR/pipeline/gpu_owner.sh"

# LLM routing (pipeline/set_agent_model.py — the single source of truth):
#   creative   story-creator, story-editor, director   → anthropic / claude-opus-5-5 (effort high)
#   vision     qa-inspector                            → opencode-go-deepseek / gemini-3.8-flash
#   execution  everything else                         → opencode-go-deepseek / deepseek-v4.1-flash
#                                                        (dialogue-polish: gemini-3.8-flash)
# Override per tier with DSH_CREATIVE_* / DSH_VISION_* / DSH_EXECUTION_* (PROVIDER/MODEL/EFFORT).
# NO FALLBACKS: a routing or provider error halts the run (halt_run), never reroutes.
DSH_SETTINGS="/root/.dsh/settings.yaml"
# API keys live in /root/.hermes/.env: dsh reads its copy, the pipeline's own
# probes and ledger read the exported variables.
if [ -f /root/.hermes/.env ]; then
    cp /root/.hermes/.env "$DSH_DIR/.env" 2>/dev/null || true
    set -a
    # shellcheck source=/dev/null
    . /root/.hermes/.env 2>/dev/null || true
    set +a
fi

# Delivery frame rate of the per-clip 4K masters: 24 = source rate, no RIFE
# (default); 48/96 = RIFE xN; 60 = RIFE x5 then every 2nd frame.
export PP_TARGET_FPS="${PP_TARGET_FPS:-24}"
# Cloud spend caps (pipeline/cloud_ledger.py) — reaching one halts the run.
export CLOUD_MAX_CALLS_ANTHROPIC="${CLOUD_MAX_CALLS_ANTHROPIC:-12}"
export CLOUD_MAX_USD_EST_ANTHROPIC="${CLOUD_MAX_USD_EST_ANTHROPIC:-9}"
export CLOUD_MAX_CALLS_OPENCODE="${CLOUD_MAX_CALLS_OPENCODE:-1500}"

DISCORD_CHANNEL="discord:#film-maker"
NOTIFY="$PACK_DIR/pipeline/notify.sh"

# ── Helpers ─────────────────────────────────────────────────
log()  { echo "[$(date '+%H:%M:%S')] $*"; }
CURRENT_STEP="start"
step() { CURRENT_STEP="$*"; echo; echo "════════════════════════════════════════"; log "STEP: $*"; echo "════════════════════════════════════════"; }
notify() { bash "$NOTIFY" "$1" "${2:-}" "${3:-}" 2>/dev/null & }

# ── Halt on error (decision 2026-10-18: no fallbacks, no retries, no reroutes) ──
# Every failure of the machinery — an agent call, a render script, a missing
# key, a spend cap — writes $RUN_DIR/HALT_FOR_REVIEW.json, posts to Discord and
# exits 3. ./launch_resume.sh <run_dir> re-runs the halted step (skip_if_done
# markers skip everything that already succeeded). Quality verdicts — QA FAIL,
# lint findings, CAST_FAIL — are NOT errors: their bounded loops stay.
halt_run() {
    local step="$1" reason="$2" artifact="${3:-}"
    log "HALT: [$step] $reason"
    if [ -n "${RUN_DIR:-}" ] && [ -d "$RUN_DIR" ]; then
        python3 - "$RUN_DIR/HALT_FOR_REVIEW.json" "$step" "$reason" "$RUN_DIR" "${HALT_PRESET:-}" "${HALT_STDERR:-}" <<'PYEOF'
import json, os, sys, time
halt, step, reason, run_dir, preset, errfile = sys.argv[1:7]
tail = ""
if errfile and os.path.isfile(errfile):
    try:
        with open(errfile, encoding="utf-8", errors="replace") as fh:
            tail = "".join(fh.readlines()[-40:])[-4000:]
    except OSError:
        pass
d = {"kind": "error", "step": step, "reason": reason, "preset": preset or None,
     "stderr_tail": tail, "halted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
     "resume": "./launch_resume.sh " + run_dir,
     "note": "no automatic retry or fallback by design — fix the cause, then resume"}
tmp = halt + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(d, fh, indent=2)
os.replace(tmp, halt)
PYEOF
        echo "[$(date '+%H:%M:%S')] HALT [$step] $reason" >> "$RUN_DIR/pipeline.log" 2>/dev/null || true
    fi
    notify "RUN HALTED at: $step\n$reason\nResume: ./launch_resume.sh ${RUN_DIR:-<run_dir>}" "RUN HALTED" "$artifact"
    sleep 3   # let the Discord post leave before the process tree goes away
    rm -f "$OUTPUT_DIR/.gen_phase_active" "$OUTPUT_DIR/.audio_phase_active" 2>/dev/null || true
    exit 3
}
die()  { halt_run "${CURRENT_STEP:-run}" "$*"; }

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

# ── GPU helpers ─────────────────────────────────────────────
# Drop ComfyUI's loaded models (Qwen Image 2.1 ~10 GB / H3 ~12-14 GB) without
# touching the server process. Safe to call when ComfyUI is down.
comfyui_release_models() {
    curl -sf --max-time 10 -X POST "$COMFYUI_URL/free" \
        -H 'Content-Type: application/json' \
        -d '{"unload_models":true,"free_memory":true}' > /dev/null 2>&1 || true
}

ensure_comfyui() {
    # ComfyUI is the resting GPU owner. gpu_owner stops the TTS cluster (and any
    # stale LLM unit) and waits for :8188. No retries: a dead ComfyUI halts.
    if curl -sf --max-time 5 "$COMFYUI_URL/api/system_stats" > /dev/null 2>&1 \
       && ! systemctl is-active --quiet tts-cluster.service 2>/dev/null; then
        return 0
    fi
    log "Handing the GPU to ComfyUI..."
    gpu_owner comfyui || halt_run "${CURRENT_STEP:-gpu}" "ComfyUI did not become healthy within ${COMFYUI_START_TIMEOUT}s — journalctl -u comfyui.service -n 50"
}

# ── Routing: every agent in the cloud, resolved per call, never rerouted ──
ROUTE_PROVIDER=""; ROUTE_MODEL=""; ROUTE_EFFORT=""; ROUTE_TIER=""
set_agent_model() {
    local preset="$1" out rc=0 line
    out=$(python3 "$PACK_DIR/pipeline/set_agent_model.py" "$preset" 2>&1) || rc=$?
    if [ "$rc" -ne 0 ]; then
        halt_run "${CURRENT_STEP:-route}" "set_agent_model.py exit $rc for preset '$preset': $(echo "$out" | tail -2 | tr '\n' ' ')"
    fi
    line=$(echo "$out" | grep -m1 '^ROUTE ' || true)
    ROUTE_PROVIDER=$(echo "$line" | sed -n 's/.*provider=\([^ ]*\).*/\1/p')
    ROUTE_MODEL=$(echo "$line" | sed -n 's/.*model=\([^ ]*\).*/\1/p')
    ROUTE_EFFORT=$(echo "$line" | sed -n 's/.*effort=\([^ ]*\).*/\1/p')
    ROUTE_TIER=$(echo "$line" | sed -n 's/.*tier=\([^ ]*\).*/\1/p')
    [ -n "$ROUTE_PROVIDER" ] || halt_run "${CURRENT_STEP:-route}" "set_agent_model.py printed no ROUTE line for '$preset': $(echo "$out" | tail -2 | tr '\n' ' ')"
    log "Model route: $preset -> $ROUTE_PROVIDER/$ROUTE_MODEL (effort=$ROUTE_EFFORT, tier=$ROUTE_TIER)"
}

# Discord note once at 50 % and 80 % of a provider's spend cap.
_ledger_progress() {
    local provider="$1" pct marker
    pct=$(python3 - "$RUN_DIR/cloud_usage.json" "$provider" <<'PYEOF' 2>/dev/null || echo 0
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    cap = d.get("caps", {}).get(sys.argv[2], {})
    used = d.get("totals", {}).get("by_provider", {}).get(sys.argv[2], {})
    r = 0.0
    for k in ("calls", "usd_est"):
        if cap.get(k):
            r = max(r, float(used.get(k, 0)) / float(cap[k]))
    print(int(r * 100))
except Exception:
    print(0)
PYEOF
)
    for t in 80 50; do
        marker="$RUN_DIR/.ledger_notified_${provider}_${t}"
        if [ "${pct:-0}" -ge "$t" ] && [ ! -f "$marker" ]; then
            touch "$marker"
            notify "Cloud spend on $provider is at ${pct}% of its cap ($(python3 "$PACK_DIR/pipeline/cloud_ledger.py" summary "$RUN_DIR" 2>/dev/null | grep "$provider" | head -1))" "Cloud Spend"
            break
        fi
    done
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

    cd "$DSH_DIR"
    # dsh headless: takes task as positional arg, prints final answer to stdout
    # For story-creator: extract valid JSON (strip any preamble/summary)
    local raw_output="$RUN_DIR/${preset}_raw.txt"
    local err_output="$RUN_DIR/${preset}_stderr.log"

    set_agent_model "$preset"
    # Spend ledger: cap check before, record after (pipeline/cloud_ledger.py).
    if ! python3 "$PACK_DIR/pipeline/cloud_ledger.py" check "$RUN_DIR" --provider "$ROUTE_PROVIDER" >> "$RUN_DIR/pipeline.log" 2>&1; then
        halt_run "${CURRENT_STEP:-agent}" "cloud spend cap reached for $ROUTE_PROVIDER before $preset — see $RUN_DIR/cloud_usage.json (raise CLOUD_MAX_* to continue)"
    fi
    local agent_exit=0 t0 t1
    t0=$(date +%s)
    # The task is piped on stdin (`-`): inlined skills + story can exceed the
    # 128 KB single-argument limit of the kernel.
    pnpm dsh --profile headless \
        --patch "$PACK_DIR/.dsh/.agent-presets/$preset/agent.cordis.yml" \
        - < "$prompt_file" \
        > "$raw_output" \
        2> "$err_output" || agent_exit=$?
    t1=$(date +%s)

    # Append stderr to pipeline.log for post-mortem
    if [[ -s "$err_output" ]]; then
        cat "$err_output" >> "$RUN_DIR/pipeline.log"
    fi
    python3 "$PACK_DIR/pipeline/cloud_ledger.py" record "$RUN_DIR" --preset "$preset" --provider "$ROUTE_PROVIDER" \
        --model "$ROUTE_MODEL" --effort "$ROUTE_EFFORT" --step "${CURRENT_STEP:-}" --prompt-file "$prompt_file" \
        --raw-file "$raw_output" --stderr-file "$err_output" --exit "$agent_exit" --seconds "$((t1 - t0))" \
        >> "$RUN_DIR/pipeline.log" 2>&1 || true
    _ledger_progress "$ROUTE_PROVIDER"

    # Any failure of the call halts — no retry, no second provider (decision 2026-10-18).
    if [[ $agent_exit -ne 0 ]] || grep -qE 'PI_AI_ERROR|TRANSPORT: Connection error|expired while waiting for admission' "$err_output" 2>/dev/null; then
        HALT_PRESET="$preset" HALT_STDERR="$err_output" \
            halt_run "${CURRENT_STEP:-agent}" "agent $preset failed (exit $agent_exit) on $ROUTE_PROVIDER/$ROUTE_MODEL: $(grep -m1 -oE 'PI_AI_ERROR.{0,160}|TRANSPORT.{0,120}' "$err_output" 2>/dev/null || tail -c 300 "$err_output" | tr '\n' ' ')"
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
            HALT_PRESET="$preset" HALT_STDERR="$err_output" \
                halt_run "${CURRENT_STEP:-agent}" "story-creator returned no parseable story JSON (extraction exit $rc) — see $raw_output"
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
            # Tool-less presets answer on stdout only: never scan the run dir for them
            # (the spend ledger cloud_usage.json is always newer than their prompt).
            case "$preset" in
                screenplay-reviewer|story-editor|dialogue-polish|director|music-composer) ;;
                *)
            if [ -z "$found_json" ]; then
                found_json=$(find "$RUN_DIR" "$DSH_DIR" -maxdepth 4 -name "*.json" -newer "$prompt_file" -mmin -10 \
                    -exec python3 -c 'import json,sys; json.load(open(sys.argv[1])); print(sys.argv[1])' {} \; 2>/dev/null \
                    | grep -v "pipeline.log\|run_latest\|settings.yaml\|watchdog\|cloud_usage\|story_review\|story_check\|HALT_FOR_REVIEW\|narration_check\|timeline.json\|_report.json\|assembly_log\|render_log\|\.ledger_" | head -1 || true)
            fi
                ;;
            esac

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

    # An empty artifact means the call itself failed or the agent answered in prose.
    if [[ ! -s "$output_file" ]]; then
        HALT_PRESET="$preset" HALT_STDERR="$err_output" \
            halt_run "${CURRENT_STEP:-agent}" "agent $preset produced no usable artifact: $output_file (raw stdout kept at $raw_output)"
    fi
    log "Agent $preset done -> $output_file"
    return 0
}

# ── Public Agent Launcher ─────────────────────────────────────
# One attempt per call. Any failure halts the run inside _run_dsh_agent_internal
# (halt_run); ./launch_resume.sh re-runs the step. No transport retries, no phase
# juggling: the GPU is not involved in an agent turn (every agent is in the cloud).
run_dsh_agent() {
    _run_dsh_agent_internal "$@"
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
    if [ -f "$RUN_DIR/HALT_FOR_REVIEW.json" ] && \
       python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("kind")=="error" else 1)' "$RUN_DIR/HALT_FOR_REVIEW.json" 2>/dev/null; then
        log "previous run halted on an error: $(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("step",""), "-", str(d.get("reason",""))[:200])' "$RUN_DIR/HALT_FOR_REVIEW.json" 2>/dev/null) — clearing the halt, the step re-runs"
        rm -f "$RUN_DIR/HALT_FOR_REVIEW.json"
    fi
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
notify "Phase 1: Pre-production starting (story + director on Claude Opus 5.5, the rest on OpenCode Go)" "Pre-Production"

# ── Cloud preflight: every provider in use must answer /models with its key ──
# No reroute: a missing key or a failed probe halts before any GPU work starts.
step "0. Cloud preflight"
PROBE_OUT=$(python3 "$PACK_DIR/pipeline/set_agent_model.py" --probe-all 2>&1) || \
    halt_run "0. Cloud preflight" "provider probe failed: $(echo "$PROBE_OUT" | grep -E 'FAIL|ERROR' | head -3 | tr '\n' ' ')"
log "$PROBE_OUT"
python3 "$PACK_DIR/pipeline/set_agent_model.py" --dry-run-all >> "$RUN_DIR/pipeline.log" 2>&1 || true
log "4K master frame rate: ${PP_TARGET_FPS} fps; cloud caps: anthropic ${CLOUD_MAX_CALLS_ANTHROPIC} calls / \$${CLOUD_MAX_USD_EST_ANTHROPIC}, opencode ${CLOUD_MAX_CALLS_OPENCODE} calls"

# 1a. Story Creator — Claude Opus 5.5 REWRITES the draft into a screenplay-grade
# story. The agent has no tools: the schema and the craft skill are inlined. One
# attempt; any failure halts (run_dsh_agent). The draft is the premise, not the script.
if ! skip_if_done "$RUN_DIR/story.json" "Story Creator"; then
    step "1a. Story Creator — writing the story"
    write_prompt "$RUN_DIR/prompt_story.txt" \
        "Rewrite the draft below into a complete, screenplay-grade story for a Pixar-style animated short, following the story-schema and story-craft skills reproduced here. Keep the draft's premise, named characters and ending unless a craft rule forces a change. Walk the self-check in story-craft section 12 before answering. Output ONLY the JSON document — raw, no code fences, no prose — starting with {.

--- STORY-SCHEMA SKILL ---
$(cat "$PACK_DIR/.dsh/skills/story-schema.md")

--- STORY-CRAFT SKILL ---
$(cat "$PACK_DIR/.dsh/skills/story-craft.md")

--- THE DRAFT ---
$(cat "$RUN_DIR/input_story.md")"
    run_dsh_agent "story-creator" "$RUN_DIR/prompt_story.txt" "$RUN_DIR/story.json"
    if python3 "$PACK_DIR/pipeline/story_check.py" --story "$RUN_DIR/story.json" --out "$RUN_DIR/story_check.json" >> "$RUN_DIR/pipeline.log" 2>&1; then
        log "story_check: ok after 1a"
    else
        log "story_check: issues after 1a (the story editor pass fixes them): $(python3 -c "import json; d=json.load(open('$RUN_DIR/story_check.json')); print('; '.join(d.get('blocking',[]))[:300])" 2>/dev/null)"
    fi
fi

# 1a-edit. Story Editor — ONE Opus call: critique (scores 1-10, concrete rewrites)
# followed by the revised story. The envelope is split into story_review.json and
# story.json. If story_check still blocks, the editor gets exactly one fix round;
# a story that still fails after that halts the run (the operator decides).
_split_story_envelope() {   # $1 envelope  $2 review_out  $3 story_out
    python3 - "$1" "$2" "$3" <<'PYEOF'
import json, os, sys
env_path, review_out, story_out = sys.argv[1:4]
d = json.load(open(env_path, encoding="utf-8"))
if not isinstance(d, dict) or "story" not in d or "review" not in d:
    print("envelope lacks 'review'/'story' keys: " + ", ".join(list(d)[:8] if isinstance(d, dict) else []), file=sys.stderr)
    sys.exit(1)
story = d["story"]
req = ["title", "logline", "characters", "locations", "acts", "scenes"]
missing = [k for k in req if k not in story]
if missing:
    print("revised story misses required keys: " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
for path, obj in ((review_out, d["review"]), (story_out, story)):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
sc = d["review"].get("scores", {}) if isinstance(d["review"], dict) else {}
print("review scores: " + ", ".join(f"{k}={v}" for k, v in sc.items()) + f"; overall={d['review'].get('overall','?')}")
PYEOF
}

if ! skip_if_done "$RUN_DIR/story_review.json" "Story Editor"; then
    step "1a-edit. Story Editor — critique and revision"
    [ -f "$RUN_DIR/story_v1.json" ] || cp "$RUN_DIR/story.json" "$RUN_DIR/story_v1.json"
    EDIT_NOTES=""
    if [ -s "$RUN_DIR/story_check.json" ]; then
        EDIT_NOTES="The pipeline's automatic story_check reported: $(python3 -c "import json; d=json.load(open('$RUN_DIR/story_check.json')); print('; '.join(d.get('blocking',[])+d.get('warnings',[]))[:1500])" 2>/dev/null). Fix every blocking item in the revision."
    fi
    write_prompt "$RUN_DIR/prompt_story_edit.txt" \
        "Act as the story editor for the story below: score it, list concrete rewrites, then deliver the FULL revised story. Output ONLY the JSON envelope {\"review\": {\"scores\": {...}, \"overall\": n, \"notes\": [...], \"rewrites\": [...]}, \"story\": {...}} — raw, starting with {. The revised story follows the story-schema exactly and keeps every character, the ending and all readable_props values. $EDIT_NOTES

--- STORY-CRAFT SKILL (the rubric) ---
$(cat "$PACK_DIR/.dsh/skills/story-craft.md")

--- STORY-SCHEMA SKILL ---
$(cat "$PACK_DIR/.dsh/skills/story-schema.md")

--- STORY v1 ---
$(cat "$RUN_DIR/story_v1.json")"
    run_dsh_agent "story-editor" "$RUN_DIR/prompt_story_edit.txt" "$RUN_DIR/story_edit_envelope.json"
    SPLIT_MSG=$(_split_story_envelope "$RUN_DIR/story_edit_envelope.json" "$RUN_DIR/story_review.json" "$RUN_DIR/story.json" 2>&1) \
        || halt_run "$CURRENT_STEP" "story-editor envelope unusable: $SPLIT_MSG (see $RUN_DIR/story_edit_envelope.json)"
    log "$SPLIT_MSG"
    if ! python3 "$PACK_DIR/pipeline/story_check.py" --story "$RUN_DIR/story.json" --out "$RUN_DIR/story_check.json" >> "$RUN_DIR/pipeline.log" 2>&1; then
        FIX_NOTES=$(python3 -c "import json; d=json.load(open('$RUN_DIR/story_check.json')); print('; '.join(d.get('blocking',[]))[:1500])" 2>/dev/null)
        log "story_check still blocks after the editor pass — one fix round: $FIX_NOTES"
        write_prompt "$RUN_DIR/prompt_story_edit_fix.txt" \
            "Your revised story still fails the pipeline's story_check: $FIX_NOTES. Fix EVERY item (keep everything else) and return the same JSON envelope {\"review\": {...}, \"story\": {...}} — raw, starting with {.

--- STORY-SCHEMA SKILL ---
$(cat "$PACK_DIR/.dsh/skills/story-schema.md")

--- STORY-CRAFT SKILL ---
$(cat "$PACK_DIR/.dsh/skills/story-craft.md")

--- YOUR REVISED STORY ---
$(cat "$RUN_DIR/story.json")"
        rm -f "$RUN_DIR/story_edit_envelope.json"
        run_dsh_agent "story-editor" "$RUN_DIR/prompt_story_edit_fix.txt" "$RUN_DIR/story_edit_envelope.json"
        SPLIT_MSG=$(_split_story_envelope "$RUN_DIR/story_edit_envelope.json" "$RUN_DIR/story_review.json" "$RUN_DIR/story.json" 2>&1) \
            || halt_run "$CURRENT_STEP" "story-editor fix envelope unusable: $SPLIT_MSG"
        log "$SPLIT_MSG"
        python3 "$PACK_DIR/pipeline/story_check.py" --story "$RUN_DIR/story.json" --out "$RUN_DIR/story_check.json" >> "$RUN_DIR/pipeline.log" 2>&1 \
            || halt_run "$CURRENT_STEP" "story still fails story_check after the editor's fix round: $(python3 -c "import json; d=json.load(open('$RUN_DIR/story_check.json')); print('; '.join(d.get('blocking',[]))[:400])" 2>/dev/null) — edit $RUN_DIR/story.json by hand (or the draft) and resume"
    fi
    notify "Story editor pass done — $SPLIT_MSG" "Story Editor"
fi

# 1a-dialogue. Dialogue Polish — Gemini (OpenCode Go) rewrites ONLY the dialogue
# lines for subtext, within the H3 limits. dialogue_patch.py validates and applies;
# after this step the dialogue is LOCKED (the Director copies lines verbatim).
if ! skip_if_done "$RUN_DIR/story_dialogue_patch.json" "Dialogue Polish"; then
    step "1a-dialogue. Dialogue Polish (Gemini)"
    DIALOGUE_INPUT=$(python3 - "$RUN_DIR/story.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
chars = []
for c in d.get("characters", []):
    chars.append({k: c.get(k) for k in ("id", "name", "personality", "verbal_signature", "voice_style") if c.get(k)})
scenes = []
for sc in d.get("scenes", []):
    lines = []
    for i, ln in enumerate(sc.get("dialogue") or []):
        lines.append({"index": i, "character_id": ln.get("character_id"), "line": ln.get("line"), "emotion": ln.get("emotion")})
    if not lines:
        continue
    scenes.append({"id": sc.get("id") or sc.get("scene_id"), "scene_goal": sc.get("scene_goal"), "turn": sc.get("turn"),
                   "pov_character": sc.get("pov_character"), "emotional_beat": sc.get("emotional_beat"),
                   "action_description": sc.get("action_description"), "readable_props": sc.get("readable_props"),
                   "dialogue": lines})
out = {"title": d.get("title"), "logline": d.get("logline"), "theme": d.get("theme"), "want": d.get("want"),
       "need": d.get("need"), "characters": chars, "scenes": scenes}
print(json.dumps(out, ensure_ascii=False, indent=1))
PYEOF
)
    write_prompt "$RUN_DIR/prompt_dialogue.txt" \
        "Polish the dialogue of this story for subtext and character voice. Rewrite ONLY lines that improve; keep speaker, order and count; <= 130 characters and <= 30 words per line; one speaker per beat; never explain the plot or name a feeling; keep every readable-prop value verbatim. Output ONLY {\"lines\": [{\"scene_id\", \"index\", \"character_id\", \"line\", \"emotion\"}], \"notes\": [...]} with the CHANGED lines (index is 0-based) — raw JSON starting with {.

$DIALOGUE_INPUT"
    run_dsh_agent "dialogue-polish" "$RUN_DIR/prompt_dialogue.txt" "$RUN_DIR/story_dialogue_patch.json"
    PATCH_RC=0
    python3 "$PACK_DIR/pipeline/dialogue_patch.py" "$RUN_DIR/story.json" "$RUN_DIR/story_dialogue_patch.json" \
        --backup --diff "$RUN_DIR/story_dialogue_diff.txt" >> "$RUN_DIR/pipeline.log" 2>&1 || PATCH_RC=$?
    case "$PATCH_RC" in
        0) log "dialogue polish applied — diff in $RUN_DIR/story_dialogue_diff.txt" ;;
        1) log "dialogue polish: no line could be applied (all entries rejected) — keeping the editor's lines" ;;
        *) halt_run "$CURRENT_STEP" "dialogue_patch.py exit $PATCH_RC — the dialogue-polish output is not a valid patch (see $RUN_DIR/story_dialogue_patch.json)" ;;
    esac
    python3 "$PACK_DIR/pipeline/story_check.py" --story "$RUN_DIR/story.json" --out "$RUN_DIR/story_check.json" >> "$RUN_DIR/pipeline.log" 2>&1 \
        || halt_run "$CURRENT_STEP" "story fails story_check after the dialogue polish: $(python3 -c "import json; d=json.load(open('$RUN_DIR/story_check.json')); print('; '.join(d.get('blocking',[]))[:400])" 2>/dev/null)"
    notify "Dialogue polished (locked from here)\n$(head -c 900 "$RUN_DIR/story_dialogue_diff.txt" 2>/dev/null)" "Dialogue Polish"
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
    DIRECTOR_CRAFT=$(sed -n '/^## Director excerpt/,/^## End director excerpt/p' "$PACK_DIR/.dsh/skills/story-craft.md")
    write_prompt "$RUN_DIR/prompt_director.txt" \
        "Create the complete shot list for the story below, following the shot-list-schema and editing-grammar skills reproduced here. Output ONLY the shot list JSON — raw, starting with {. Plan shots per scene:
- Duration: 5-8 seconds per clip (10 s max unless dialogue needs it; H3 minimum 5.2 s)
- Mode: reference (mid-scene coverage), composited_i2v (scene openings with characters), first_frame_only (0-character establishing shots from the plate), first_last_frame (continuous action only)
- H3 ENVELOPE (knowledge/minimax_h3_rules.md section 0, enforced by shot_list_lint.py): ONE beat per shot (a montage = one shot per beat); <= 3 named characters per frame, extras are 'a few distant figures, backs to camera' and never counted; EACH character gets its own action / hand_position / expression (no copy-paste); extreme-wide shots carry no named characters and no faces; faces and hands only at medium or closer; the picture must illustrate the narration line laid over it; reference_images.source only canonical files (characters/<id>/<id>_side.png, locations/<id>/<id>.png)
- DIALOGUE IS LOCKED: every scenes[].dialogue[].line is copied VERBATIM into exactly one shot's audio.dialogue (split only at a sentence boundary into consecutive shots, both halves kept); never omit, merge or paraphrase a line; narration only from scenes[].narration.text, never invented
- EMOTION IS THE EDIT: per shot pov_character and audience_feels (from the scene unless the shot changes POV); the scene's linger beat is its own held reaction single (5-6 s, no dialogue, edit.relation_to_prev reaction); per shot audio.music_cue {action start|continue|swell|stop|silence, mood, intensity} derived from the scene's music_cue, start/stop landing on named shots
- Mark which shots have DIALOGUE (needs lip sync) vs NARRATION (no lip sync)
- Specify camera, characters, hand positions, identity anchors
- EDITING (editing-grammar skill, mandatory): consecutive shots in a scene differ by subject, >= 2 shot sizes or >= 30 degrees (no jump cuts); never three shots of the same size in a row; every shot has edit.handle_head_s / edit.handle_tail_s >= 1.0 INCLUDED in duration_seconds, edit.cut_in and edit.cut_out (pose, gaze, prop state, motion phase); every dialogue scene has a reaction single and an insert; readable_props (clock faces, counts) pinned per scene; transition_to_next.type = cut unless a story reason

--- SHOT-LIST-SCHEMA SKILL ---
$(cat "$PACK_DIR/.dsh/skills/shot-list-schema.md")

--- EDITING-GRAMMAR SKILL ---
$(cat "$PACK_DIR/.dsh/skills/editing-grammar.md")

--- STORY-CRAFT (director excerpt) ---
$DIRECTOR_CRAFT

--- STORY ---
$(cat "$RUN_DIR/story.json")"
    run_dsh_agent "director" "$RUN_DIR/prompt_director.txt" "$RUN_DIR/shot_list.json" \
        || die "director failed — no shot_list.json (check $RUN_DIR/pipeline.log)"

    # Validate audio timing — fix dialogue overflow and narration overlap
    step "1b-validate. Audio Timing Validation"
    TIMING_ISSUES=$(python3 "$PACK_DIR/validate_audio_timing.py" "$RUN_DIR/shot_list.json" --fix 2>&1)
    log "$TIMING_ISSUES"
    if echo "$TIMING_ISSUES" | grep -q "timing issue"; then
        notify "$TIMING_ISSUES" "Audio Timing Fix"
    fi

    # Director fix rounds run in PATCH mode: the Director returns only the shots
    # it changes (replace / insert_after / delete) and pipeline/shot_list_patch.py
    # merges them — ~10x fewer Opus output tokens than rewriting 100 shots, and
    # dialogue_guard rejects any patch that drops a locked line.
    PATCH_SCHEMA='{"patch_version": 1, "replace": [<full shot objects, same shot_id>], "insert_after": [{"after": "<shot_id>", "shot": {<full shot object, new shot_id like S01_003a>}}], "delete": ["<shot_id>"], "notes": "one line per fix"}'
    director_patch_round() {   # $1 kind  $2 report.json  $3 instructions  -> 0 applied, 1 rejected
        local kind="$1" report="$2" instructions="$3" n patch affected
        n=$(ls "$RUN_DIR"/shot_list_patch_"$kind"_*.json 2>/dev/null | wc -l)
        n=$((n + 1))
        patch="$RUN_DIR/shot_list_patch_${kind}_${n}.json"
        affected=$(python3 - "$RUN_DIR/shot_list.json" "$report" "$RUN_DIR/story.json" <<'PYEOF'
import json, re, sys
shots = json.load(open(sys.argv[1], encoding="utf-8")).get("shots", [])
ids = [s.get("shot_id") for s in shots]
rep_text = open(sys.argv[2], encoding="utf-8", errors="replace").read()
hit = [i for i in ids if i and re.search(r"\b" + re.escape(i) + r"\b", rep_text)]
want = set()
for i in hit:
    k = ids.index(i)
    for j in (k - 1, k, k + 1):
        if 0 <= j < len(ids):
            want.add(ids[j])
if not hit:          # report names no shot — give the first 12 shots as context
    want = set(ids[:12])
sel = [s for s in shots if s.get("shot_id") in want]
scenes = {s.get("scene_id") for s in sel}
try:
    story = json.load(open(sys.argv[3], encoding="utf-8"))
    story_scenes = [sc for sc in story.get("scenes", []) if (sc.get("id") or sc.get("scene_id")) in scenes]
except Exception:
    story_scenes = []
print(json.dumps({"shots": sel, "story_scenes": story_scenes}, ensure_ascii=False, indent=1))
PYEOF
)
        write_prompt "$RUN_DIR/prompt_director_${kind}_fix_${n}.txt" \
            "$instructions
Reply with a PATCH document only — raw JSON starting with { — in exactly this schema: $PATCH_SCHEMA
Rules: a replaced or inserted shot is a COMPLETE shot object (same schema as the shot list); keep every locked dialogue line verbatim (a patch that drops a line is rejected); keep edit.cut_in/cut_out, handles, readable_props, pov_character, audience_feels, audio.music_cue on every shot you touch; do not renumber (the pipeline renumbers).

--- THE REPORT ---
$(cat "$report")

--- THE AFFECTED SHOTS (with their neighbours) AND THEIR STORY SCENES ---
$affected

--- EDITING-GRAMMAR SKILL ---
$(cat "$PACK_DIR/.dsh/skills/editing-grammar.md")"
        run_dsh_agent "director" "$RUN_DIR/prompt_director_${kind}_fix_${n}.txt" "$patch"
        local prc=0
        python3 "$PACK_DIR/pipeline/shot_list_patch.py" "$RUN_DIR/shot_list.json" "$patch" --renumber --run-dir "$RUN_DIR" \
            --report "${patch%.json}_report.json" >> "$RUN_DIR/pipeline.log" 2>&1 || prc=$?
        case "$prc" in
            0) log "director $kind fix $n applied ($(python3 -c "import json; d=json.load(open('${patch%.json}_report.json')); print('replaced', d.get('replaced'), 'inserted', d.get('inserted'), 'deleted', d.get('deleted'))" 2>/dev/null))"
               python3 "$PACK_DIR/validate_audio_timing.py" "$RUN_DIR/shot_list.json" --fix >> "$RUN_DIR/pipeline.log" 2>&1 || true
               return 0 ;;
            1) halt_run "$CURRENT_STEP" "director $kind fix $n REJECTED by shot_list_patch.py: $(python3 -c "import json; print(json.load(open('${patch%.json}_report.json')).get('error',''))" 2>/dev/null | head -c 300) — the Director dropped a locked line or wrote an invalid shot (see $patch)" ;;
            *) halt_run "$CURRENT_STEP" "shot_list_patch.py exit $prc on $patch (not a valid patch document)" ;;
        esac
    }

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
        director_patch_round "edit" "$RUN_DIR/shot_list_edit_report.json" \
            "Your shot list fails the editing-grammar validation. Fix EVERY 'jump_cut', 'handles' and 'cut_points' issue in the report (and the rhythm/coverage ones): change shot size by >= 2 steps or angle by >= 30 degrees, insert a reaction single or an insert between same-subject shots, add edit.handle_head_s/handle_tail_s >= 1.0 (included in duration_seconds) and edit.cut_in/cut_out to every shot you touch."
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
        director_patch_round "lint" "$RUN_DIR/shot_list_lint_report.json" \
            "Your shot list breaks the MiniMax H3 envelope (knowledge/minimax_h3_rules.md section 0). Fix EVERY blocking issue in the report: split a montage into one shot per beat (5-8 s each, insert_after with new shot ids like S01_003a); no more than 3 named characters in a frame (extras become 'a few distant figures, backs to camera' in action_description, never counted); write each character's OWN action, hand_position and expression (no identical strings between characters); extreme-wide shots carry no named characters and no faces. Keep every editing-grammar rule (handles, cut_in/cut_out, no jump cuts, readable_props)."
    done
    log "$LINT_ISSUES"

    # Locked dialogue + dialogue share: every story line must be in the shot list,
    # narration must not crowd out dialogue. One dedicated patch round, then a loud
    # note (MIN_DIALOGUE_SHARE_BLOCK=1 halts instead); a still-missing LINE halts.
    step "1b-dialogue. Locked dialogue & dialogue share"
    if ! python3 "$PACK_DIR/pipeline/story_check.py" --story "$RUN_DIR/story.json" --shots "$RUN_DIR/shot_list.json" \
            --out "$RUN_DIR/story_check_shots.json" >> "$RUN_DIR/pipeline.log" 2>&1; then
        SC_MSG=$(python3 -c "import json; d=json.load(open('$RUN_DIR/story_check_shots.json')); print('missing lines:', len(d.get('missing_lines',[])), '| share:', d.get('dialogue_share'), '|', '; '.join(d.get('blocking',[]))[:600])" 2>/dev/null)
        log "story_check (shots): $SC_MSG"
        director_patch_round "dialogue" "$RUN_DIR/story_check_shots.json" \
            "The shot list does not carry the locked dialogue correctly. For every entry of missing_lines in the report, put that exact line (verbatim) into the right shot's audio.dialogue (replace the shot, or insert_after a new one, keeping one speaker per clip); if narration was invented or crowds out dialogue (dialogue share below the floor), convert invented narration back to the story's lines. Keep every other shot untouched."
        if ! python3 "$PACK_DIR/pipeline/story_check.py" --story "$RUN_DIR/story.json" --shots "$RUN_DIR/shot_list.json" \
                --out "$RUN_DIR/story_check_shots.json" >> "$RUN_DIR/pipeline.log" 2>&1; then
            SC_MSG=$(python3 -c "import json; d=json.load(open('$RUN_DIR/story_check_shots.json')); print('missing lines:', len(d.get('missing_lines',[])), '| share:', d.get('dialogue_share'), '|', '; '.join(d.get('blocking',[]))[:600])" 2>/dev/null)
            if python3 -c "import json,sys; sys.exit(0 if json.load(open('$RUN_DIR/story_check_shots.json')).get('missing_lines') else 1)" 2>/dev/null; then
                halt_run "$CURRENT_STEP" "locked dialogue lines are still missing from the shot list after the Director's fix round — $SC_MSG (see $RUN_DIR/story_check_shots.json)"
            elif [ "${MIN_DIALOGUE_SHARE_BLOCK:-0}" = "1" ]; then
                halt_run "$CURRENT_STEP" "dialogue share below the floor after the Director's fix round — $SC_MSG"
            else
                notify "Dialogue share still below the floor after the Director's fix round — continuing (set MIN_DIALOGUE_SHARE_BLOCK=1 to halt instead)\n$SC_MSG" "Dialogue Share"
            fi
        fi
    fi
    log "story_check (shots): $(python3 -c "import json; d=json.load(open('$RUN_DIR/story_check_shots.json')); print('ok' if d.get('ok') else 'warnings', '| dialogue share', d.get('dialogue_share'))" 2>/dev/null)"
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
    try:
        sc = json.load(open("RUNDIR/story_check_shots.json".replace("RUNDIR", sys.argv[1])))
        print(f"Dialogue share: {float(sc.get('dialogue_share') or 0) * 100:.0f}% of spoken words; missing story lines: {len(sc.get('missing_lines', []))}")
    except Exception:
        pass
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
    write_prompt "$RUN_DIR/prompt_voices.txt" \
        "Read $RUN_DIR/story.json and CAST the voices per the voice-casting skill. Every role incl. 'narrator' gets a voice class (child/teen/adult-female/adult-male/elderly-female/elderly-male/creature); the narrator uses a class NO character uses. Orpheus (voices tara/leah/jess/leo/dan/mia/zac/zoe) has ADULT voices only — children and elderly roles use Chatterbox cloning from an age-matched reference WAV (look under $RUN_DIR/audio/voices/refs/ and /root/production_pack/output/voices/refs/; pick the closest) or Orpheus with post_fx (pitch_semitones -2..+3, tempo 0.9..1.1); never an adult voice with affectation. CosyVoice is NOT available (no REST endpoint). DO NOT call any TTS port — the TTS engines are down while you run; the pipeline renders and measures the line-up afterwards. Write ONLY $RUN_DIR/audio/voices/voice_config.json: {\"<role>\": {\"voice_class\": ..., \"engine\": \"orpheus|chatterbox|kokoro\", \"voice\": ... or \"reference_audio\": <abs path>, \"post_fx\": {...} optional, \"notes\": ...}, ...} with one entry per speaking character (ids from story.json) plus 'narrator'."
    # One attempt: a failed turn halts (run_dsh_agent); a CAST_FAIL verdict is a
    # quality loop and gets up to two recasts below.
    run_dsh_agent "audio-producer" "$RUN_DIR/prompt_voices.txt" "$RUN_DIR/audio/voices/voice_config.json"
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
        local ids gate_ctx
        ids=$(python3 -c "import json; print(', '.join(s['shot_id'] for s in json.load(open('$RUN_DIR/shot_list.json'))['shots'] if s.get('scene_id')=='$scene'))")
        gate_ctx=$(python3 - "$RUN_DIR/shot_list.json" "$RUN_DIR/story.json" "$scene" <<'PYEOF'
import json, sys
shots = [s for s in json.load(open(sys.argv[1], encoding="utf-8")).get("shots", []) if s.get("scene_id") == sys.argv[3]]
try:
    story = json.load(open(sys.argv[2], encoding="utf-8"))
    scene = next((sc for sc in story.get("scenes", []) if (sc.get("id") or sc.get("scene_id")) == sys.argv[3]), None)
except Exception:
    scene = None
print(json.dumps({"shots": shots, "story_scene": scene}, ensure_ascii=False, indent=1))
PYEOF
)
        write_prompt "$RUN_DIR/prompt_director_gate_${scene}.txt" \
            "Fix ONLY the shots of $scene ($ids). The blocking issues and the shots are inlined below. Keep the SAME shot ids, the same count and order, the same narration/dialogue assignments (every dialogue line verbatim) and the same scene_id. Rules (knowledge/minimax_h3_rules.md section 0): each character in characters_in_frame gets its OWN action, hand_position, expression and facing written for THIS shot (never the same string on two characters; creatures get a body pose, no hands); at most 3 named characters per frame (drop the least important to 'a few distant figures, backs to camera' in action_description); a montage shot keeps its single strongest beat, the other beats are dropped (do NOT add shots); no counted crowds; extreme-wide shots carry no named characters and no faces; faces only at medium or closer; duration 5-8 s (10 s max without dialogue); the picture illustrates the narration line over it; reference_images.source only canonical existing files (locations/<id>/<id>.png, characters/<id>/<id>_side.png). Keep every editing-grammar field (edit.cut_in/cut_out, handles, readable_props, pov_character, audience_feels, audio.music_cue). Reply with ONLY {\"shots\": [...]} for these $(echo "$ids" | tr ',' '\n' | wc -l) shots, same schema — raw JSON starting with {.

--- LINT REPORT ---
$(cat "$rep")

--- THE SCENE'S SHOTS AND STORY SCENE ---
$gate_ctx"
        rm -f "$fix"
        # Mechanical lint fix: run the director preset on the execution tier (OpenCode Go).
        DSH_FORCE_TIER=execution run_dsh_agent "director" "$RUN_DIR/prompt_director_gate_${scene}.txt" "$fix"
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
#    whose changes_made notes mention the d-tags and used to trigger a doomed Orpheus call).
#    NO double quotes in this python: it lives inside a bash double-quoted string.
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
            log "[$SHOT_ID] Rendering the dialogue stem from voice_config.json (TTS cluster takes the GPU)..."
            touch "$OUTPUT_DIR/.audio_phase_active"
            # --keep-gpu: the TTS cluster stays up; the keyframe step hands the GPU to ComfyUI next.
            STEM_RC=0
            python3 "$PACK_DIR/pipeline/dialogue_stem.py" \
                "$RUN_DIR/shot_list.json" "$SHOT_ID" "$CLIP_DIR" --run-dir "$RUN_DIR" --keep-gpu \
                > "$CLIP_DIR/dialogue_stem.log" 2>&1 || STEM_RC=$?
            while IFS= read -r line; do log "  $line"; done < "$CLIP_DIR/dialogue_stem.log"
            rm -f "$OUTPUT_DIR/.audio_phase_active"
            if [ -s "$CLIP_DIR/dialogue.wav" ]; then
                log "[$SHOT_ID] Dialogue stem ready: $(python3 -c "import json;d=json.load(open('$CLIP_DIR/dialogue_meta.json'));print(d.get('engine_used'),d.get('voice'),d.get('duration_s'),'s at',d.get('anchor_s'),'s')" 2>/dev/null)"
            elif python3 -c "import json,sys; sys.exit(0 if json.load(open('$CLIP_DIR/dialogue_meta.json')).get('status')=='no_speech' else 1)" 2>/dev/null; then
                log "[$SHOT_ID] dialogue line is a silence beat — no stem"
            else
                halt_run "$CURRENT_STEP" "dialogue stem for $SHOT_ID was not rendered (dialogue_stem.py exit $STEM_RC) — a dialogue clip never renders without its approved stem (see pipeline.log, TTS cluster: systemctl status tts-cluster)"
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
            # The agent only PLANS (agents never touch ComfyUI): it writes
            # frame_spec.json; generate_frames.py renders it on ComfyUI and
            # writes frame_meta.json for the video step.
            write_prompt "$CLIP_DIR/prompt_image.txt" \
                "Plan the keyframes for $SHOT_ID — DO NOT call ComfyUI (it is down while you run; the pipeline renders your spec). Read $CLIP_DIR/reviewed_prompt.json (h3_mode, final_prompt, reference_assignments), $RUN_DIR/shot_list.json (this shot's camera, characters_in_frame with positions/hand_position/facing, location_id, lighting, edit.cut_in/cut_out, readable_props) and the manifests $RUN_DIR/character_manifest.json / $RUN_DIR/location_manifest.json for reference image paths. Write $CLIP_DIR/frame_spec.json: {\"mode\": \"composited_i2v|first_frame_only|first_last_frame|reference\", \"frames\": [{\"role\": \"first_frame\", \"prompt\": <Qwen Image 2.1 prompt: shot size FIRST and LAST, every character exactly once with position/pose/gaze from edit.cut_in, the location's layout, lighting, readable prop values verbatim>, \"references\": [<abs paths: ONE design sheet per character in frame, the location plate, and continuity frames prev_clip_lastframe.png / scene_establishing_frame.png if present in $CLIP_DIR>], \"width\": 1344, \"height\": 768, \"background\": <one sentence: the environment behind the figures, from the location plate>, \"placements\": [{\"character_id\": <id>, \"bbox\": [y_min, x_min, y_max, x_max] in 0-1000 of the frame (where the figure stands and how tall it is for THIS shot size: full-body in a wide = 300-900 tall, medium = 150-1000, close-up = 0-1000), \"desc\": <the figure in one sentence: pose from edit.cut_in, facing, costume colours, hand position>}] one per character in frame, \"style\": {\"aesthetics\": ..., \"lighting\": <from the shot>, \"art_style\": \"Pixar-style 3D animated feature film\", \"medium\": \"3d_render\"}}, {\"role\": \"last_frame\", ...only for first_last_frame, pose from edit.cut_out}], \"video_references\": [{\"tag\": \"<Subject N>\", \"image\": <abs path>, \"role\": ...}]}. For mode reference write frames: [] (no keyframe needed). Never name an object you want absent (Qwen ignores negatives); describe only what IS in frame. A readable prop's ledger STATE is written positively (an 'unlit' jar = 'dark matte translucent cobalt glass lit only by the flat grey sky, no light inside, the sill under it in shadow'), and any reference image that shows the prop in the OTHER state (the location plate's glowing windows for an unlit-jar insert) is left OUT of references — Qwen copies what it sees."
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
            FRAME_RC=0
            if [ "${FRAME_ENGINE:-qwen}" = "ideogram4" ]; then
                python3 "$PACK_DIR/pipeline/ideogram_frames.py" --shot-dir "$CLIP_DIR" --run-dir "$RUN_DIR" \
                    --comfyui-url "$COMFYUI_URL" --keep-gpu $FORCE_FRAMES >> "$RUN_DIR/pipeline.log" 2>&1 || FRAME_RC=$?
                [ "$FRAME_RC" -eq 0 ] && log "[$SHOT_ID] frames rendered (ideogram4 layout + qwen identity)"
            else
                python3 "$PACK_DIR/pipeline/generate_frames.py" --shot-dir "$CLIP_DIR" --run-dir "$RUN_DIR" \
                    --comfyui-url "$COMFYUI_URL" --keep-gpu $FORCE_FRAMES >> "$RUN_DIR/pipeline.log" 2>&1 || FRAME_RC=$?
                [ "$FRAME_RC" -eq 0 ] && log "[$SHOT_ID] frames rendered"
            fi
            rm -f "$OUTPUT_DIR/.gen_phase_active"
            if [ "$FRAME_RC" -ne 0 ]; then
                halt_run "$CURRENT_STEP" "keyframe render for $SHOT_ID failed (${FRAME_ENGINE:-qwen} engine, exit $FRAME_RC) — no fallback to references-only; see pipeline.log"
            fi
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
            # The i2va graph writes video only: give the clip its stem as the audio track at
            # anchor_s so previews, QA and assembly carry the same approved audio.
            python3 "$PACK_DIR/pipeline/mux_stem.py" "$CLIP_DIR" 2>&1 | while IFS= read -r line; do log "  $line"; done

            if [ $GEN_EXIT -ne 0 ] || [ ! -f "$CLIP_DIR/clip.mp4" ]; then
                halt_run "$CURRENT_STEP" "video render for $SHOT_ID failed (generate_clip.py exit $GEN_EXIT, clip.mp4 $([ -f "$CLIP_DIR/clip.mp4" ] && echo present || echo missing)) — see $CLIP_DIR/generation_log.json and pipeline.log"
            fi
        fi

        # A resumed clip whose video step was skipped must still have its clip.
        if [ ! -f "$CLIP_DIR/clip.mp4" ]; then
            halt_run "$CURRENT_STEP" "$SHOT_ID has no clip.mp4 to judge (video step skipped on resume but the file is gone) — delete $CLIP_DIR/generation_log.json and resume to re-render"
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
        run_dsh_agent "qa-inspector" "$CLIP_DIR/prompt_qa.txt" "$CLIP_DIR/qa_verdict.json"
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
        # No verdict = the inspector did not judge the clip (crash, prose, stale report).
        # That is an error of the machinery, never a clip FAIL: halt, do not burn a take.
        if [ -z "$VERDICT" ]; then
            halt_run "$CURRENT_STEP" "qa-inspector returned no PASS/FAIL verdict for $SHOT_ID — see $CLIP_DIR/qa_verdict.json / qa_report.json and $RUN_DIR/qa-inspector_raw.txt"
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
            # Record the count AFTER the increment: a resume reads it back as RETRY, and 0 would
            # mean "never failed" — every stage skipped and the same clip re-judged (S01_004 x2).
            python3 - "$CLIP_DIR/qa_verdict.json" "$RETRY" <<'PYEOF'
import json, os, sys
p, n = sys.argv[1], int(sys.argv[2])
try:
    d = json.load(open(p))
    d["retry_count"] = n
    tmp = p + ".tmp"
    json.dump(d, open(tmp, "w"), indent=2)
    os.replace(tmp, p)
except Exception:
    pass
PYEOF
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

step "PHASE 2g: BATCH 4K MASTERS (${PP_TARGET_FPS} fps)"
notify "All clips generated. Building the 4K masters at ${PP_TARGET_FPS} fps (RTX VSR$([ "${PP_TARGET_FPS}" != "24" ] && echo ' + RIFE'))..." "Upscale"
ensure_comfyui

UPS_DONE=0
UPS_SKIP=0
for SHOT_ID in $SHOT_IDS; do
    CLIP_DIR="$RUN_DIR/clips/$SHOT_ID"
    [ -f "$CLIP_DIR/clip.mp4" ] || continue

    # Only clips that passed QA (or were accepted by the operator)
    if [ -f "$CLIP_DIR/qa_verdict.json" ]; then
        UPS_VERDICT=$(python3 -c "import json; print(json.load(open('$CLIP_DIR/qa_verdict.json')).get('verdict',''))" 2>/dev/null)
        [ "$UPS_VERDICT" != "PASS" ] && continue
    else
        continue
    fi

    # Skip if already up to date
    if [ -f "$CLIP_DIR/clip_4k.mp4" ] && [ "$CLIP_DIR/clip_4k.mp4" -nt "$CLIP_DIR/clip.mp4" ]; then
        UPS_SKIP=$((UPS_SKIP + 1))
        continue
    fi

    log "[$SHOT_ID] 4K master at ${PP_TARGET_FPS} fps..."
    UPS_FORCE=""
    [ -f "$CLIP_DIR/clip_4k.mp4" ] && UPS_FORCE="--force"
    UPS_RC=0
    python3 "$PACK_DIR/pipeline/upscale_clip.py" \
        --shot-dir "$CLIP_DIR" \
        --comfyui-url "$COMFYUI_URL" \
        --target-fps "$PP_TARGET_FPS" \
        $UPS_FORCE \
        2>> "$RUN_DIR/pipeline.log" || UPS_RC=$?
    if [ "$UPS_RC" -ne 0 ] || [ ! -s "$CLIP_DIR/clip_4k.mp4" ]; then
        halt_run "$CURRENT_STEP" "4K master for $SHOT_ID failed (upscale_clip.py exit $UPS_RC) — no CPU fallback, no low-res copy; fix ComfyUI/RTX VSR and resume"
    fi
    log "[$SHOT_ID] 4K master ready ($(stat -c%s "$CLIP_DIR/clip_4k.mp4") bytes)"
    UPS_DONE=$((UPS_DONE + 1))
done
log "Batch 4K masters: $UPS_DONE built, $UPS_SKIP already done"
notify "4K masters done: $UPS_DONE new, $UPS_SKIP skipped" "Upscale"

# ═══════════════════════════════════════════════════════════
# PHASE 3: POST-PRODUCTION — deterministic
#   3a narration plan (agent) → 3b edit check → 3c edit decisions (agent)
#   → 3d timeline → 3e music cues (agent) → 3f ONE GPU excursion (TTS, then
#   ComfyUI: music + SFX) → 3g assemble.py (picture + one mix) + subtitles.
# Clip files contribute PICTURE only; every audio element is placed once on
# the film timeline by pipeline/assemble.py (J/L cuts = audio_lead_s/audio_lag_s).
# ═══════════════════════════════════════════════════════════

step "PHASE 3: POST-PRODUCTION"
notify "All clips done. Phase 3: Post-production starting" "Post-Production"
mkdir -p "$RUN_DIR/final" "$RUN_DIR/music" "$RUN_DIR/audio/narration"
NARR_DIR="$RUN_DIR/audio/narration"

# 3a. Narration plan — the Audio Producer writes the jobs; the pipeline renders later.
if ! skip_if_done "$NARR_DIR/tts_jobs.json" "Narration plan"; then
    step "3a. Narration plan"
    write_prompt "$RUN_DIR/prompt_narration.txt" \
        "Prepare ALL narration audio jobs — DO NOT call any TTS port (the engines are down while you run; the pipeline renders). Read $RUN_DIR/story.json (scenes[].narration.text), $RUN_DIR/shot_list.json (narration spans: which clips each block covers; a block is carried by the FIRST clip of its span only) and the narrator entry of $RUN_DIR/audio/voices/voice_config.json (engine/voice/reference_audio/post_fx — use EXACTLY that cast). Write $NARR_DIR/tts_jobs.json: {\"jobs\": [{\"id\": \"N1\", \"engine\": ..., \"voice\"/\"reference_audio\"/\"post_fx\" from the cast, \"text\": <one narration passage, sentence-level punctuation kept, ~2.2 words/s pace>, \"out\": \"$NARR_DIR/N1.wav\"}, ...]} and $NARR_DIR/narration_meta.json: {\"blocks\": [{\"id\": \"N1\", \"scene_id\": ..., \"span_clips\": [<clip ids in order>], \"starts_on\": <span_clips[0]>, \"text\": ..., \"file\": \"N1.wav\"}]}. EXACTLY ONE block per narration passage — never repeat a passage on several clips; no span clip may carry dialogue; ids N1, N2, … unique and identical in both files. Narration is post-only: NO lip sync, never overlaps a dialogue clip."
    run_dsh_agent "audio-producer" "$RUN_DIR/prompt_narration.txt" "$NARR_DIR/narration_meta.json"
    [ -s "$NARR_DIR/tts_jobs.json" ] || halt_run "$CURRENT_STEP" "audio-producer wrote narration_meta.json but no $NARR_DIR/tts_jobs.json"
    if ! python3 "$PACK_DIR/pipeline/narration_check.py" --run-dir "$RUN_DIR" --plan >> "$RUN_DIR/pipeline.log" 2>&1; then
        NC_MSG=$(python3 -c "import json; d=json.load(open('$NARR_DIR/narration_check.json')); print('; '.join(d.get('errors', d.get('blocking', [])))[:1200])" 2>/dev/null || tail -20 "$RUN_DIR/pipeline.log" | tr '\n' ' ')
        log "narration plan rejected — one fix round: $NC_MSG"
        write_prompt "$RUN_DIR/prompt_narration_fix.txt" \
            "Your narration plan was rejected by the pipeline's narration_check: $NC_MSG. Rewrite BOTH $NARR_DIR/tts_jobs.json and $NARR_DIR/narration_meta.json so they pass: exactly one block per passage, starts_on = first clip of span_clips, no clip in two spans, no span clip with dialogue, ids 1:1 between the two files, same cast as $RUN_DIR/audio/voices/voice_config.json. Do NOT call any TTS port."
        run_dsh_agent "audio-producer" "$RUN_DIR/prompt_narration_fix.txt" "$NARR_DIR/narration_meta.json"
        python3 "$PACK_DIR/pipeline/narration_check.py" --run-dir "$RUN_DIR" --plan >> "$RUN_DIR/pipeline.log" 2>&1 \
            || halt_run "$CURRENT_STEP" "narration plan still fails narration_check after the fix round — see $NARR_DIR/narration_check.json"
    fi
    notify "Narration plan: $(python3 -c "import json; print(len(json.load(open('$NARR_DIR/tts_jobs.json'))['jobs']), 'blocks')" 2>/dev/null) ready" "Audio Producer"
fi

# 3b. Edit check — scores every clip-to-clip boundary (picture); the editor reads it.
step "3b. Edit Check (clip boundaries)"
if python3 "$PACK_DIR/pipeline/edit_check.py" --run-dir "$RUN_DIR" \
        --out "$RUN_DIR/final/edit_report.json" >> "$RUN_DIR/pipeline.log" 2>&1; then
    log "edit check: every boundary >= 0.80"
else
    EDIT_FAILS=$(python3 -c "import json; r=json.load(open('$RUN_DIR/final/edit_report.json')); print(r.get('failed',0), 'of', r.get('boundaries',0))" 2>/dev/null || echo "?")
    log "edit check: $EDIT_FAILS boundaries below 0.80 — the editor decides trims/inserts (final/edit_report.json + edit_boundaries.jpg)"
    notify "Edit check: $EDIT_FAILS boundaries below 0.80 — see final/edit_boundaries.jpg" "Edit Check" "$RUN_DIR/final/edit_boundaries.jpg"
fi

# 3c. Edit decisions — the Post-Production Editor DECIDES (edit_decisions.json v2);
# assemble.py executes. The editor never runs ffmpeg and never touches clip audio.
if ! skip_if_done "$RUN_DIR/final/edit_decisions.json" "Edit decisions"; then
    step "3c. Edit decisions (Post-Production Editor)"
    write_prompt "$RUN_DIR/prompt_edit_decisions.txt" \
        "You are the EDITOR. Decide the cut at every clip boundary and write ONLY $RUN_DIR/final/edit_decisions.json (version 2). Do NOT run ffmpeg, do NOT touch any clip or audio file — pipeline/assemble.py executes your decisions.
Read: $RUN_DIR/final/edit_report.json (score per boundary, flags, suggested fixes) and look at $RUN_DIR/final/edit_boundaries.jpg; $RUN_DIR/shot_list.json (edit.handle_head_s / handle_tail_s / cut_in / cut_out, audio.dialogue, transition_to_next); $RUN_DIR/clips/<SID>/dialogue_meta.json (anchor_s, duration_s of the stem); $RUN_DIR/assembly_fixes.json if it exists (blocking notes from QA: stem offsets, trims).
Rules (editing-grammar skill): choose a_out_s / b_in_s INSIDE the handles so the cut lands on action or on a look; boundaries below 0.80 get a deeper trim, an approved reaction/insert clip between them ('inserted'), or a request_rerender — never a dissolve. Dialogue clips: never cut inside the stem (anchor_s .. anchor_s+duration_s) unless you set a J/L cut. J-cut = audio_lead_s (0.15-0.30 s): B's line starts BEFORE the picture cut (then b_in_s = anchor_s + audio_lead_s). L-cut = audio_lag_s (0.20-0.40 s): A's line continues over B's first frames (a_out_s = anchor_s + duration_s - audio_lag_s). Voices never overlap. Use J/L cuts on dialogue boundaries where the previous speaker's tail is a hold.
Schema: {\"version\": 2, \"decisions\": [{\"boundary\": \"S01_003|S01_004\", \"action\": \"trim|insert|hold|request_rerender\", \"a_out_s\": <seconds into clip A>, \"b_in_s\": <seconds into clip B>, \"audio_lead_s\": 0.0, \"audio_lag_s\": 0.0, \"cut_type\": \"on-action|on-look|hold\", \"insert_clip\": null, \"note\": \"...\"}], \"removed_clips\": [], \"inserted\": [{\"after\": \"S01_003\", \"shot_id\": \"S01_007\", \"src_in_s\": 0.5, \"src_out_s\": 2.5}]}. One decision per boundary in shot order. Output the JSON only."
    run_dsh_agent "post-production-editor" "$RUN_DIR/prompt_edit_decisions.txt" "$RUN_DIR/final/edit_decisions.json"
    if ! VALID_OUT=$(python3 "$PACK_DIR/pipeline/assemble.py" --run-dir "$RUN_DIR" --validate-decisions 2>&1); then
        log "edit decisions rejected — one fix round: $(echo "$VALID_OUT" | tail -5 | tr '\n' ' ')"
        echo "$VALID_OUT" >> "$RUN_DIR/pipeline.log"
        write_prompt "$RUN_DIR/prompt_edit_decisions_fix.txt" \
            "Your $RUN_DIR/final/edit_decisions.json was rejected by pipeline/assemble.py --validate-decisions: $(echo "$VALID_OUT" | tail -12). Fix every named boundary (keep the version-2 schema, cuts inside the handles, voices never overlapping, b_in_s = anchor_s + audio_lead_s on a J-cut) and rewrite the file. Do NOT run ffmpeg."
        run_dsh_agent "post-production-editor" "$RUN_DIR/prompt_edit_decisions_fix.txt" "$RUN_DIR/final/edit_decisions.json"
        python3 "$PACK_DIR/pipeline/assemble.py" --run-dir "$RUN_DIR" --validate-decisions >> "$RUN_DIR/pipeline.log" 2>&1 \
            || halt_run "$CURRENT_STEP" "edit_decisions.json still invalid after the editor's fix round — see pipeline.log; fix $RUN_DIR/final/edit_decisions.json by hand and resume"
    fi
    log "edit decisions validated"
fi

# 3d. Timeline — always rebuilt (cheap); the composer reads it for cue lengths.
step "3d. Timeline"
python3 "$PACK_DIR/pipeline/assemble.py" --run-dir "$RUN_DIR" --timeline-only >> "$RUN_DIR/pipeline.log" 2>&1 \
    || halt_run "$CURRENT_STEP" "assemble.py --timeline-only failed — see pipeline.log (a clip without a PASS verdict or a missing 4K master?)"
python3 "$PACK_DIR/pipeline/assemble.py" --run-dir "$RUN_DIR" --dry-run >> "$RUN_DIR/pipeline.log" 2>&1 || true
log "timeline: $(python3 -c "import json; t=json.load(open('$RUN_DIR/final/timeline.json')); print(len(t.get('segments',[])), 'segments,', round(t.get('film_duration_s',0),1), 's at', t.get('fps'), 'fps')" 2>/dev/null)"

# 3e. Music cues — the Music Composer PLANS (music_cues.json); music_render.py renders.
if ! skip_if_done "$RUN_DIR/music/music_cues.json" "Music cues"; then
    step "3e. Music cues (Music Composer)"
    write_prompt "$RUN_DIR/prompt_music.txt" \
        "Plan the score as a film composer and write ONLY $RUN_DIR/music/music_cues.json following the music-cues skill — DO NOT call ComfyUI or any audio port (the pipeline renders with ACE-Step afterwards). Read $RUN_DIR/story.json (scenes[].music_cue {mood,intensity,start,stop}, emotional_beat, audience_feels), $RUN_DIR/shot_list.json (shots[].audio.music_cue action start|continue|swell|stop|silence, audio.sfx, dialogue clips) and $RUN_DIR/final/timeline.json (film time of every clip and scene — cue anchors are clip ids). Cues tile every scene (or an explicit silence entry), 10-180 s each, instrumental only, tempo 50-160, intensity follows the emotional beat, transitions on cuts; motifs per character; ambience beds per scene. Output the JSON only."
    run_dsh_agent "music-composer" "$RUN_DIR/prompt_music.txt" "$RUN_DIR/music/music_cues.json"
    if ! MV_OUT=$(python3 "$PACK_DIR/pipeline/music_render.py" --run-dir "$RUN_DIR" --validate 2>&1); then
        log "music cues rejected — one fix round: $(echo "$MV_OUT" | tail -6 | tr '\n' ' ')"
        echo "$MV_OUT" >> "$RUN_DIR/pipeline.log"
        write_prompt "$RUN_DIR/prompt_music_fix.txt" \
            "Your $RUN_DIR/music/music_cues.json was rejected by the pipeline's validator: $(echo "$MV_OUT" | tail -15). Fix every problem (same schema, music-cues skill) and rewrite the file. Do NOT call any audio port."
        run_dsh_agent "music-composer" "$RUN_DIR/prompt_music_fix.txt" "$RUN_DIR/music/music_cues.json"
        python3 "$PACK_DIR/pipeline/music_render.py" --run-dir "$RUN_DIR" --validate >> "$RUN_DIR/pipeline.log" 2>&1 \
            || halt_run "$CURRENT_STEP" "music_cues.json still invalid after the composer's fix round — see pipeline.log"
    fi
    log "music cues validated -> $RUN_DIR/music/music_cues.resolved.json"
fi
[ -s "$RUN_DIR/music/music_cues.resolved.json" ] || \
    python3 "$PACK_DIR/pipeline/music_render.py" --run-dir "$RUN_DIR" --validate >> "$RUN_DIR/pipeline.log" 2>&1 \
    || halt_run "3e. Music cues" "music_cues.json does not validate (resumed run) — see pipeline.log"

# 3f. Render audio — ONE GPU excursion: TTS cluster (narration), then ComfyUI (music, SFX).
step "3f. Render narration, music and SFX"
NARR_OK=$(python3 -c "import json; r=json.load(open('$NARR_DIR/tts_jobs.results.json')); print('yes' if r.get('ok')==r.get('jobs') and r.get('jobs') else 'no')" 2>/dev/null || echo no)
if [ "$NARR_OK" = "yes" ] && [ "$NARR_DIR/tts_jobs.results.json" -nt "$NARR_DIR/tts_jobs.json" ]; then
    log "narration already rendered ($NARR_DIR/tts_jobs.results.json)"
else
    touch "$OUTPUT_DIR/.audio_phase_active"
    TTS_RC=0
    python3 "$PACK_DIR/pipeline/tts_render.py" --jobs "$NARR_DIR/tts_jobs.json" --run-dir "$RUN_DIR" --keep-gpu >> "$RUN_DIR/pipeline.log" 2>&1 || TTS_RC=$?
    rm -f "$OUTPUT_DIR/.audio_phase_active"
    [ "$TTS_RC" -eq 0 ] || halt_run "$CURRENT_STEP" "narration render failed (tts_render.py exit $TTS_RC) — see $NARR_DIR/tts_jobs.results.json and pipeline.log"
    python3 - "$NARR_DIR" <<'PYEOF'
import json, os, sys
d = sys.argv[1]
meta = json.load(open(os.path.join(d, "narration_meta.json")))
res = {r["id"]: r for r in json.load(open(os.path.join(d, "tts_jobs.results.json")))["results"]}
for b in meta.get("blocks", []):
    r = res.get(b.get("id"))
    if r:
        b["duration_s"] = r.get("duration_s"); b["status"] = r.get("status"); b["file"] = os.path.basename(r.get("out") or b.get("file", ""))
tmp = os.path.join(d, "narration_meta.json.tmp")
json.dump(meta, open(tmp, "w"), indent=2)
os.replace(tmp, os.path.join(d, "narration_meta.json"))
PYEOF
    python3 "$PACK_DIR/pipeline/narration_check.py" --run-dir "$RUN_DIR" --rendered >> "$RUN_DIR/pipeline.log" 2>&1 \
        || halt_run "$CURRENT_STEP" "rendered narration fails narration_check (missing wav / duration) — see $NARR_DIR/narration_check.json"
    log "narration rendered: $(python3 -c "import json;r=json.load(open('$NARR_DIR/tts_jobs.results.json'));print(r['ok'],'/',r['jobs'])" 2>/dev/null)"
fi

touch "$OUTPUT_DIR/.gen_phase_active"
MUSIC_RC=0
python3 "$PACK_DIR/pipeline/music_render.py" --run-dir "$RUN_DIR" --keep-gpu >> "$RUN_DIR/pipeline.log" 2>&1 || MUSIC_RC=$?
[ "$MUSIC_RC" -eq 0 ] || { rm -f "$OUTPUT_DIR/.gen_phase_active"; halt_run "$CURRENT_STEP" "music render failed (music_render.py exit $MUSIC_RC) — see $RUN_DIR/music/render_log.json and pipeline.log"; }
log "music rendered: $(python3 -c "import json; r=json.load(open('$RUN_DIR/music/render_log.json')); print(r.get('ok'), 'cues/beds ok,', len(r.get('failed',[])), 'failed')" 2>/dev/null)"
SFX_FLAG=""
if [ "${SFX_LAYER:-1}" = "1" ]; then
    SFX_RC=0
    python3 "$PACK_DIR/pipeline/sfx_render.py" --run-dir "$RUN_DIR" --no-gpu-switch --keep-gpu >> "$RUN_DIR/pipeline.log" 2>&1 || SFX_RC=$?
    [ "$SFX_RC" -eq 0 ] || { rm -f "$OUTPUT_DIR/.gen_phase_active"; halt_run "$CURRENT_STEP" "SFX render failed (sfx_render.py exit $SFX_RC) — see $RUN_DIR/clips/sfx_render_log.json (set SFX_LAYER=0 to skip the layer)"; }
    log "sfx rendered"
else
    SFX_FLAG="--no-sfx"
fi
notify "Narration, music$([ "${SFX_LAYER:-1}" = "1" ] && echo ' and SFX') rendered — assembling" "Post-Production"

# 3g. Assemble — picture from the 4K masters, ONE audio mix on the timeline, subtitles.
step "3g. Assemble"
AMB_FLAG=""
[ "${KEEP_NATIVE_AMBIENCE:-0}" = "1" ] && AMB_FLAG="--keep-native-ambience"
ASM_RC=0
python3 "$PACK_DIR/pipeline/assemble.py" --run-dir "$RUN_DIR" --encoder "${ASSEMBLE_ENCODER:-libx264}" $SFX_FLAG $AMB_FLAG \
    >> "$RUN_DIR/pipeline.log" 2>&1 || ASM_RC=$?
rm -f "$OUTPUT_DIR/.gen_phase_active"
[ "$ASM_RC" -eq 0 ] || halt_run "$CURRENT_STEP" "assemble.py exit $ASM_RC — see $RUN_DIR/final/assembly_log.json and pipeline.log"
python3 "$PACK_DIR/pipeline/subtitles.py" --run-dir "$RUN_DIR" --embed >> "$RUN_DIR/pipeline.log" 2>&1 \
    || halt_run "$CURRENT_STEP" "subtitles.py failed — see pipeline.log"
python3 "$PACK_DIR/pipeline/edit_check.py" --concat-list "$RUN_DIR/final/concat.txt" --shots "$RUN_DIR/shot_list.json" \
    --out "$RUN_DIR/final/edit_report_final.json" >> "$RUN_DIR/pipeline.log" 2>&1 \
    || log "final edit check: boundaries below 0.80 remain — see final/edit_report_final.json"

# ═══════════════════════════════════════════════════════════
# DONE
# ═══════════════════════════════════════════════════════════

step "PRODUCTION COMPLETE"
log "Output: $RUN_DIR/final/"
ls -lh "$RUN_DIR/final/" 2>/dev/null
log ""
log "Movie:     $RUN_DIR/final/movie_4k.mp4"
log "Subtitles: $RUN_DIR/final/movie.srt / movie.ass"
log "Log:       $RUN_DIR/pipeline.log"
python3 "$PACK_DIR/pipeline/cloud_ledger.py" summary "$RUN_DIR" 2>/dev/null | while IFS= read -r line; do log "$line"; done

FINAL_SIZE=$(du -sh "$RUN_DIR/final/movie_4k.mp4" 2>/dev/null | cut -f1 || echo "N/A")
notify "PRODUCTION COMPLETE\nMovie: $FINAL_SIZE\nClips: $TOTAL\nPath: $RUN_DIR/final/movie_4k.mp4\n$(python3 "$PACK_DIR/pipeline/cloud_ledger.py" summary "$RUN_DIR" 2>/dev/null | head -3)" "Production Complete"
