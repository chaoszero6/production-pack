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

PACK_DIR="/root/production_pack"
DSH_DIR="/root/desktop/deepseek-harness"
COMFYUI_DIR="/opt/comfyui"
OUTPUT_DIR="$PACK_DIR/output"
LLM_PORT=8085
LLM_URL="http://127.0.0.1:$LLM_PORT"
COMFYUI_URL="http://127.0.0.1:8188"

# Cloud LLM routing (2026-09-21): EVERY production-pack agent runs on the
# OpenCode Go cloud — deepseek-v4-pro for the five reasoning agents, and
# deepseek-v4.1-flash for the other nine. No local LLM is involved, so the RTX
# 5090 is left entirely to ComfyUI. dsh loads .env from the invocation cwd
# ($DSH_DIR) or $DSH_HOME, neither of which carries OPENCODE_GO_API_KEY, so
# mirror the Hermes env into $DSH_HOME and export it for the dsh process.
DSH_SETTINGS="/root/.dsh/settings.yaml"
if [ -f /root/.hermes/.env ]; then
    cp /root/.hermes/.env "$DSH_DIR/.env"
    OPENCODE_GO_API_KEY=$(sed -n 's/^OPENCODE_GO_API_KEY=//p' /root/.hermes/.env | head -1) || true
    export OPENCODE_GO_API_KEY
    # `log` is not defined yet at this point in the file — use a raw echo
    [ -n "${OPENCODE_GO_API_KEY:-}" ] || echo "[$(date '+%H:%M:%S')] WARNING: OPENCODE_GO_API_KEY not found in /root/.hermes/.env — cloud agents will fail auth"
fi

SVC_122B="llama-qwen35-122b.service"
SVC_27B="qwen3.8-27b-q6k-cuda.service"
SVC_COMFYUI="comfyui.service"
SVC_HERMES="hermes-stack.service"

WAIT_122B=60
WAIT_27B=30

DISCORD_CHANNEL="discord:#film-maker"
NOTIFY="$PACK_DIR/pipeline/notify.sh"

# ── Helpers ─────────────────────────────────────────────────
log()  { echo "[$(date '+%H:%M:%S')] $*"; }
die()  { log "ERROR: $*"; notify "ERROR: $*"; exit 1; }
step() { echo; echo "════════════════════════════════════════"; log "STEP: $*"; echo "════════════════════════════════════════"; }
notify() { bash "$NOTIFY" "$1" "${2:-}" 2>/dev/null & }

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

# ── Local LLM management (VRAM only) ────────────────────────
# 2026-09-21: EVERY production-pack agent runs on the OpenCode Go cloud, so no
# local LLM is needed for agent work. The 27B on :8085 (which also backs the
# interactive Hermes session) therefore stays stopped for the whole run so
# ComfyUI owns the full RTX 5090 for image/video generation. The swap_* names
# are kept as shims so the existing call sites keep working.
ensure_llms_stopped() {
    systemctl stop "$SVC_122B" 2>/dev/null || true
    if systemctl is-active --quiet "$SVC_27B"; then
        log "Stopping $SVC_27B (frees ~27GB VRAM for ComfyUI; Hermes falls back to cloud)"
        systemctl stop "$SVC_27B" 2>/dev/null || true
    fi
    # the docker container sometimes outlives the systemd unit
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^qwen38'; then
        docker stop qwen38-27b-q6k > /dev/null 2>&1 || true
    fi
    sleep 2
}

swap_to_122b()        { ensure_llms_stopped; }   # shim: cloud routing, nothing to swap
swap_to_122b_legacy() { ensure_llms_stopped; }   # shim: cloud routing, nothing to swap
swap_to_27b()         { ensure_llms_stopped; }   # shim: cloud routing, nothing to swap
stop_all_llms()       { ensure_llms_stopped; }

ensure_comfyui() {
    if ! curl -sf "$COMFYUI_URL/api/system_stats" > /dev/null 2>&1; then
        log "Starting ComfyUI..."
        systemctl start "$SVC_COMFYUI"
        wait_for_health "$COMFYUI_URL/api/system_stats" 120 "ComfyUI"
    fi
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

# ── Per-agent cloud model routing ───────────────────────────
# dsh's headless bundle resolves the model for every agent from the
# `agent-default-model` section of settings.yaml at launch — there is no
# per-preset override (the `engine:` plugin the presets reference,
# @yuki-takuya-kun/dsh-engine-switch, is not installed and 404s on npm, so
# those lines are inert). So we rewrite that one section before each call:
#   deepseek-v4-pro     -> reasoning agents + QA (vision capable)
#   deepseek-v4.1-flash -> execution agents (image gen, video gen, audio, etc.)
MODEL_PRO_PRESETS="story-creator director screenplay-reviewer character-designer location-designer qa-inspector"

set_agent_model() {
    local preset="$1" provider="opencode-go-deepseek" model="deepseek-v4.1-flash" effort="low"
    case " $MODEL_PRO_PRESETS " in
        *" $preset "*)
            provider="opencode-go-deepseek-pro"; model="deepseek-v4-pro"; effort="high" ;;
    esac

    python3 - "$DSH_SETTINGS" "$provider" "$model" "$effort" << 'PYEOF'
import sys, re, os, tempfile
path, provider, model, effort = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
text = open(path).read()
m = re.search(r'(?m)^agent-default-model:[ \t]*$', text)
if not m:
    sys.exit('agent-default-model section not found in ' + path)
head, tail = text[:m.start()], text[m.end():]
# drop the section body: everything until the next line that starts at column 0
# (blank lines and indented/comment lines belong to this section)
lines = tail.splitlines(keepends=True)
rest = ''
for i, ln in enumerate(lines):
    if ln.strip() and ln[:1] not in (' ', '\t', '#'):
        rest = ''.join(lines[i:])
        break
block = (
    'agent-default-model:\n'
    '  # Managed by production_pack/run.sh (per-agent cloud routing) — do not hand-edit.\n'
    f'  provider: {provider}\n'
    f'  model: {model}\n'
    f'  reasoningEffort: {effort}\n'
)
# atomic write so a killed run can never leave a truncated settings.yaml
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.settings-', suffix='.tmp')
with os.fdopen(fd, 'w') as fh:
    fh.write(head + block + rest)
os.replace(tmp, path)
PYEOF
    log "Model route: $preset -> $provider / $model (effort=$effort)"
}

run_dsh_agent() {
    local preset="$1" prompt_file="$2" output_file="$3"
    log "Running agent: $preset"
    local prompt_text
    prompt_text=$(cat "$prompt_file")

    cd "$DSH_DIR"
    # dsh headless: takes task as positional arg, prints final answer to stdout
    # For story-creator: extract valid JSON (strip any preamble/summary)
    local raw_output="$RUN_DIR/${preset}_raw.txt"

    # all agents are cloud-hosted: point agent-default-model at the right model
    # (pro for the reasoning agents, flash for everything else) before launch
    set_agent_model "$preset"

    pnpm dsh --profile headless \
        --patch "$PACK_DIR/.dsh/.agent-presets/$preset/agent.cordis.yml" \
        "$prompt_text" \
        > "$raw_output" \
        2>> "$RUN_DIR/pipeline.log" || {
        log "WARNING: Agent $preset exited non-zero. Check $RUN_DIR/pipeline.log"
        # Save raw output even on failure for resume
    }

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

req = ['title', 'logline', 'theme', 'characters', 'locations', 'acts', 'scenes']

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
if missing:
    print(f"Missing required keys: {missing}", file=sys.stderr)
    print(f"Agent's top-level keys were: {sorted(chosen.keys())}",
          file=sys.stderr)
    if len(candidates) > 1:
        print(f"({len(candidates)} JSON candidates scanned)", file=sys.stderr)
    sys.exit(1)

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
        # Every other preset emits JSON too, and agents habitually wrap the object in
        # a prose preamble ("Here's my review and the built prompt.") or a ```json
        # fence. Copying raw stdout verbatim handed downstream consumers a mixed
        # document that json.load() rejects, so their except-branches silently took
        # the wrong path:
        #   * HAS_DIALOGUE -> 'no'   => no dialogue audio on lip-sync clips
        #   * VERDICT      -> 'FAIL' => a valid clip burns all MAX_RETRY attempts
        # Prefer an artifact the agent wrote itself with its tools; otherwise extract
        # the JSON object properly (brace-counted, nesting-safe).
        if [ -s "$output_file" ] && \
           python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$output_file" 2>/dev/null; then
            log "  (kept agent-written $output_file)"
        elif ! python3 "$PACK_DIR/extract_json.py" "$raw_output" "$output_file" \
                 >>"$RUN_DIR/pipeline.log" 2>&1; then
            log "WARNING: $preset emitted no parseable JSON — keeping raw stdout"
            cp "$raw_output" "$output_file"
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
        RUN_DIR=$(ls -td "$OUTPUT_DIR"/run_* 2>/dev/null | head -1)
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
    if [ "$RESUME" = "true" ] && [ -f "$output_file" ] && [ -s "$output_file" ]; then
        log "SKIP (resume): $step_name — output exists: $output_file"
        return 0
    fi
    return 1
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

# 1a. Story Creator
if ! skip_if_done "$RUN_DIR/story.json" "Story Creator"; then
    step "1a. Story Creator — structuring story"
    write_prompt "$RUN_DIR/prompt_story.txt" \
        "Read the story below and structure it into the production JSON format (see story-schema skill). Include: characters with full visual descriptions and identity anchors, locations with color palettes, scenes with dialogue and narration separated, emotional beats, visual mood. Output valid JSON.

--- STORY ---
$(cat "$RUN_DIR/input_story.md")"
    run_dsh_agent "story-creator" "$RUN_DIR/prompt_story.txt" "$RUN_DIR/story.json" \
        || die "story-creator failed — no story.json (check $RUN_DIR/pipeline.log)"
    [ -s "$RUN_DIR/story.json" ] || die "story.json is empty — aborting"
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
- Duration: 3-15 seconds per clip
- Mode: ref2va (default), composited_i2v (new scenes with characters), fl2va (continuity only)
- Mark which shots have DIALOGUE (needs lip sync) vs NARRATION (no lip sync)
- Specify camera, characters, hand positions, identity anchors
- Output valid JSON to be processed shot-by-shot."
    run_dsh_agent "director" "$RUN_DIR/prompt_director.txt" "$RUN_DIR/shot_list.json" \
        || die "director failed — no shot_list.json (check $RUN_DIR/pipeline.log)"
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
if ! skip_if_done "$RUN_DIR/character_manifest.json" "Character Designer"; then
    step "1c. Character Designer — generating reference sheets"
    ensure_comfyui
    write_prompt "$RUN_DIR/prompt_characters.txt" \
        "Read $RUN_DIR/story.json. For each character, generate multi-angle reference sheets using Qwen Image 2.1 via ComfyUI API at $COMFYUI_URL. Model: qwen_image_2.1_bf16.safetensors. Create front, 3/4, side, back views at 2048x2048. Save to $RUN_DIR/characters/{name}/. Output a manifest JSON listing all generated files."
    run_dsh_agent "character-designer" "$RUN_DIR/prompt_characters.txt" "$RUN_DIR/character_manifest.json" \
        || die "character-designer failed — no character_manifest.json (check $RUN_DIR/pipeline.log)"
    notify "Character reference sheets generated" "Character Designer"
fi

# 1d. Location Designer
if ! skip_if_done "$RUN_DIR/location_manifest.json" "Location Designer"; then
    step "1d. Location Designer — generating environments"
    write_prompt "$RUN_DIR/prompt_locations.txt" \
        "Read $RUN_DIR/story.json. For each location, generate reference images using Qwen Image 2.1 via ComfyUI at $COMFYUI_URL. Create establishing and medium shots with mood-appropriate lighting. Save to $RUN_DIR/locations/{name}/. Output a manifest JSON."
    run_dsh_agent "location-designer" "$RUN_DIR/prompt_locations.txt" "$RUN_DIR/location_manifest.json" \
        || die "location-designer failed — no location_manifest.json (check $RUN_DIR/pipeline.log)"
    notify "Location references generated" "Location Designer"
fi

# 1e. Voice Design
if ! skip_if_done "$RUN_DIR/audio/voices/voice_config.json" "Voice Design"; then
    step "1e. Audio Producer — voice design"
    swap_to_27b
    write_prompt "$RUN_DIR/prompt_voices.txt" \
        "Read $RUN_DIR/story.json. For each character, design a voice profile and generate a 5-second reference clip using Chatterbox (port 9882) or CosyVoice (port 50000). Select narrator voice from Kokoro (port 9881). Save voice refs to $RUN_DIR/audio/voices/. Output voice config JSON."
    run_dsh_agent "audio-producer" "$RUN_DIR/prompt_voices.txt" "$RUN_DIR/audio/voices/voice_config.json"
    notify "Voice profiles created" "Audio Producer"
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

for SHOT_ID in $SHOT_IDS; do
    CURRENT=$((CURRENT + 1))
    CLIP_DIR="$RUN_DIR/clips/$SHOT_ID"
    mkdir -p "$CLIP_DIR"

    # Check --from-clip / --only-clip filters
    if ! should_process_clip "$SHOT_ID"; then
        log "SKIP (filter): $SHOT_ID"
        continue
    fi

    # Resume: skip clips that already passed QA and are upscaled
    if [ "$RESUME" = "true" ] && [ -z "$ONLY_CLIP" ] && [ -f "$CLIP_DIR/qa_verdict.json" ] && [ -f "$CLIP_DIR/clip_4k60.mp4" ]; then
        PREV_VERDICT=$(python3 -c "import json; print(json.load(open('$CLIP_DIR/qa_verdict.json')).get('verdict',''))" 2>/dev/null)
        if [ "$PREV_VERDICT" = "PASS" ]; then
            log "SKIP (resume): $SHOT_ID — already passed QA + upscaled"
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
    MAX_RETRY=3
    PASSED=false

    while [ "$PASSED" = "false" ] && [ $RETRY -lt $MAX_RETRY ]; do
        [ $RETRY -gt 0 ] && log "RETRY $RETRY/$MAX_RETRY for $SHOT_ID"

        # ── 2a. Screenplay Reviewer (122B) ──
        log "[$SHOT_ID] Prompt review..."
        CORRECTIONS=""
        [ -f "$CLIP_DIR/qa_verdict.json" ] && CORRECTIONS="Previous QA feedback: $(cat "$CLIP_DIR/qa_verdict.json")"

        write_prompt "$CLIP_DIR/prompt_review.txt" \
            "Review shot $SHOT_ID from $RUN_DIR/shot_list.json. Build the MiniMax H3 ref2va prompt in 6-section format (subject_definitions, summary, retention_analysis, detailed_description, overall_soundscape, non_diegetic_music). Apply rules from knowledge/minimax_h3_rules.md. Character refs: $RUN_DIR/characters/. Location refs: $RUN_DIR/locations/. DIALOGUE lines get <d> tags + <Audio> ref. NARRATION clips: NO <d> tags, describe characters with closed lips. $CORRECTIONS"

        run_dsh_agent "screenplay-reviewer" "$CLIP_DIR/prompt_review.txt" "$CLIP_DIR/reviewed_prompt.json"

        # ── 2b. Dialogue audio (if needed) ──
        HAS_DIALOGUE=$(python3 -c "
import json
try:
    with open('$CLIP_DIR/reviewed_prompt.json') as f:
        d = json.load(f)
    p = d.get('final_prompt', '')
    print('yes' if '<d>' in p or 'Audio' in d.get('h3_mode','') else 'no')
except:
    print('no')
" 2>/dev/null)

        if [ "$HAS_DIALOGUE" = "yes" ]; then
            log "[$SHOT_ID] Generating dialogue audio (before video for lip sync)..."
            swap_to_27b
            write_prompt "$CLIP_DIR/prompt_dialogue.txt" \
                "Generate dialogue audio for $SHOT_ID. Read $CLIP_DIR/reviewed_prompt.json for lines. Voice refs: $RUN_DIR/audio/voices/. Use Chatterbox (9882) for default, CosyVoice (50000) for multilingual. Save combined audio to $CLIP_DIR/dialogue.wav"
            run_dsh_agent "audio-producer" "$CLIP_DIR/prompt_dialogue.txt" "$CLIP_DIR/dialogue_meta.json"
        fi

        # ── 2c. Free VRAM for generation ──
        log "[$SHOT_ID] Freeing VRAM..."
        stop_all_llms
        sleep 3
        ensure_comfyui

        # ── 2d. Image Generation (ComfyUI — no LLM needed) ──
        log "[$SHOT_ID] Generating frames..."
        swap_to_27b
        write_prompt "$CLIP_DIR/prompt_image.txt" \
            "Generate frames for $SHOT_ID. Read $CLIP_DIR/reviewed_prompt.json for mode. If composited_i2v: load location ref, use Qwen Image 2.1 edit to composite characters. If reference: skip frame gen. If fl2va: extract last frame from previous clip. ComfyUI: $COMFYUI_URL. Save to $CLIP_DIR/"
        run_dsh_agent "image-generator" "$CLIP_DIR/prompt_image.txt" "$CLIP_DIR/frame_meta.json"

        # ── 2e. Video Generation (ComfyUI — GPU heavy) ──
        log "[$SHOT_ID] Generating video clip..."
        stop_all_llms
        sleep 2
        ensure_comfyui
        swap_to_27b
        write_prompt "$CLIP_DIR/prompt_video.txt" \
            "Generate video clip for $SHOT_ID via MiniMax H3 in ComfyUI ($COMFYUI_URL). Read $CLIP_DIR/reviewed_prompt.json for the H3 prompt and mode (ref2va/i2va/fl2va). If dialogue exists, upload $CLIP_DIR/dialogue.wav as audio reference. First frame: $CLIP_DIR/first_frame.png. Save clip to $CLIP_DIR/clip.mp4"
        run_dsh_agent "video-generator" "$CLIP_DIR/prompt_video.txt" "$CLIP_DIR/generation_log.json"

        # ── 2f. QA Review (27B is multimodal) ──
        log "[$SHOT_ID] QA inspection..."
        write_prompt "$CLIP_DIR/prompt_qa.txt" \
            "Review $CLIP_DIR/clip.mp4. Extract frames with ffmpeg. Check: hands (finger count, merging), face (distortion, symmetry), character identity (vs refs in $RUN_DIR/characters/), duplication, motion, lip sync (dialogue=lips move, narration=lips closed). Score 0-1 per category, threshold 0.85. Output JSON with verdict PASS/FAIL and corrections if FAIL."
        run_dsh_agent "qa-inspector" "$CLIP_DIR/prompt_qa.txt" "$CLIP_DIR/qa_verdict.json"

        # qa-inspector writes its real report to qa_report.json with its own tools and prints a
        # markdown review to stdout, so qa_verdict.json lands here as PROSE. The verdict read
        # below catches the resulting json.load() failure and silently yields 'FAIL' — which is
        # accidentally right when the clip really failed, but a false FAIL on a passing clip
        # burns all MAX_RETRY attempts in H3 re-renders and ends with the clip ESCALATED.
        QA_NORM=$(python3 "$PACK_DIR/normalise_qa_verdict.py" "$CLIP_DIR" "$RETRY" 2>&1 || true)
        if [ -n "$QA_NORM" ]; then
            log "  $QA_NORM"
            echo "$QA_NORM" >> "$RUN_DIR/pipeline.log"
        fi

        VERDICT=$(python3 -c "
import json
try:
    with open('$CLIP_DIR/qa_verdict.json') as f:
        print(json.load(f).get('verdict','FAIL'))
except:
    print('FAIL')
" 2>/dev/null)

        if [ "$VERDICT" = "PASS" ]; then
            PASSED=true
            log "[$SHOT_ID] QA PASSED"
            notify "Clip $CURRENT/$TOTAL ($SHOT_ID): QA PASSED" "QA Result"
        else
            RETRY=$((RETRY + 1))
            log "[$SHOT_ID] QA FAILED (attempt $RETRY/$MAX_RETRY)"
            notify "Clip $CURRENT/$TOTAL ($SHOT_ID): QA FAILED (retry $RETRY/$MAX_RETRY)" "QA Result"
        fi
    done

    if [ "$PASSED" = "false" ]; then
        log "[$SHOT_ID] ESCALATED after $MAX_RETRY retries"
        notify "Clip $SHOT_ID ESCALATED — failed $MAX_RETRY attempts" "ESCALATION"
    fi

    # ── 2g. Upscale to 4K 60fps ──
    if [ -f "$CLIP_DIR/clip.mp4" ]; then
        log "[$SHOT_ID] Upscaling to 4K 60fps (per-clip)..."
        stop_all_llms
        ensure_comfyui
        # Use NVIDIA RTX Video upscale + RIFE interpolation via ComfyUI
        swap_to_27b
        write_prompt "$CLIP_DIR/prompt_upscale.txt" \
            "Upscale $CLIP_DIR/clip.mp4 to 4K (3840x2160) and 60fps. Use ComfyUI ($COMFYUI_URL) with: 1) NVIDIA RTX Video Super Resolution node for spatial upscale to 4K. 2) RIFE frame interpolation (rife47 model, 2x multiplier) for 60fps. Process per-clip (not full movie) to fit in 32GB VRAM. Save to $CLIP_DIR/clip_4k60.mp4"
        run_dsh_agent "video-generator" "$CLIP_DIR/prompt_upscale.txt" "$CLIP_DIR/upscale_log.json"

        # Fallback if upscale fails
        if [ ! -f "$CLIP_DIR/clip_4k60.mp4" ]; then
            log "[$SHOT_ID] Upscale not produced, using original"
            cp "$CLIP_DIR/clip.mp4" "$CLIP_DIR/clip_4k60.mp4"
        fi
    fi

    log "[$SHOT_ID] DONE ($CURRENT/$TOTAL)"

    # --only-clip: exit after processing the single clip
    if [ -n "$ONLY_CLIP" ]; then
        notify "Single clip $ONLY_CLIP processing complete" "Clip Done"
        log "=== SINGLE CLIP MODE COMPLETE ==="
        exit 0
    fi
done

# ═══════════════════════════════════════════════════════════
# PHASE 3: POST-PRODUCTION [Qwen 3.8 27B]
# ═══════════════════════════════════════════════════════════

step "PHASE 3: POST-PRODUCTION"
notify "All clips done. Phase 3: Post-production starting" "Post-Production"
swap_to_27b

# 3a. Narration
if ! skip_if_done "$RUN_DIR/audio/narration/narration_meta.json" "Narration"; then
    step "3a. Narration (Kokoro — no lip sync)"
    write_prompt "$RUN_DIR/prompt_narration.txt" \
        "Generate ALL narration audio. Read $RUN_DIR/story.json for narrator lines per scene. Use Kokoro (port 9881). Time to align with clips. This is post-production only — NO lip sync. Save to $RUN_DIR/audio/narration/"
    run_dsh_agent "audio-producer" "$RUN_DIR/prompt_narration.txt" "$RUN_DIR/audio/narration/narration_meta.json"
    notify "Narration audio generated" "Audio Producer"
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

# 3d. Assembly (always re-run on resume — it's the final step)
step "3d. Final Assembly"
write_prompt "$RUN_DIR/prompt_assembly.txt" \
    "Assemble final movie with ffmpeg:
- Video: concatenate $RUN_DIR/clips/*/clip_4k60.mp4 in shot order
- Audio: dialogue is BAKED in clips (lip-synced, do NOT replace)
- Overlay: narration from $RUN_DIR/audio/narration/ (no lip sync)
- Layer: music from $RUN_DIR/music/
- Subtitles: burn ASS from $RUN_DIR/final/*.ass
- Output: $RUN_DIR/final/movie_4k60.mp4 (H.264, AAC 256k)
- Also: $RUN_DIR/final/movie_4k60_master.mov (ProRes, PCM)"
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
