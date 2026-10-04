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

PACK_DIR="/root/production_pack"
DSH_DIR="/root/desktop/deepseek-harness"
COMFYUI_DIR="/opt/comfyui"
OUTPUT_DIR="$PACK_DIR/output"
LLM_PORT=8085
LLM_URL="http://127.0.0.1:$LLM_PORT"
COMFYUI_URL="http://127.0.0.1:8188"

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

swap_to_122b() {
    log "Swapping LLM -> Qwen 3.5 122B..."
    systemctl stop "$SVC_27B" 2>/dev/null || true
    sleep 3
    systemctl start "$SVC_122B"
    wait_for_health "$LLM_URL/health" "$WAIT_122B" "Qwen 3.5 122B"
}

swap_to_27b() {
    log "Swapping LLM -> Qwen 3.8 27B..."
    systemctl stop "$SVC_122B" 2>/dev/null || true
    sleep 3
    systemctl start "$SVC_27B"
    wait_for_health "$LLM_URL/health" "$WAIT_27B" "Qwen 3.8 27B"
}

stop_all_llms() {
    systemctl stop "$SVC_122B" 2>/dev/null || true
    systemctl stop "$SVC_27B" 2>/dev/null || true
    sleep 2
}

ensure_comfyui() {
    if ! curl -sf "$COMFYUI_URL/api/system_stats" > /dev/null 2>&1; then
        log "Starting ComfyUI..."
        systemctl start "$SVC_COMFYUI"
        wait_for_health "$COMFYUI_URL/api/system_stats" 120 "ComfyUI"
    fi
}

run_dsh_agent() {
    local preset="$1" prompt_file="$2" output_file="$3"
    log "Running agent: $preset"
    local prompt_text
    prompt_text=$(cat "$prompt_file")

    cd "$DSH_DIR"
    # dsh headless: takes task as positional arg, prints final answer to stdout
    pnpm dsh --profile headless \
        --patch "$PACK_DIR/.dsh/.agent-presets/$preset/agent.cordis.yml" \
        "$prompt_text" \
        > "$output_file" \
        2>> "$RUN_DIR/pipeline.log" || {
        log "WARNING: Agent $preset exited non-zero. Check $RUN_DIR/pipeline.log"
    }
    log "Agent $preset done -> $output_file"
}

write_prompt() {
    local file="$1" content="$2"
    echo "$content" > "$file"
}

# ── Validate Input ──────────────────────────────────────────
STORY_FILE="${1:-}"
if [ -z "$STORY_FILE" ]; then
    echo "Usage: $0 <story.md>"
    echo ""
    echo "  Provide a story in markdown format."
    echo "  The pipeline will produce a complete 4K 60fps movie."
    echo ""
    echo "Output: $OUTPUT_DIR/run_<timestamp>/final/"
    exit 1
fi

[ -f "$STORY_FILE" ] || die "File not found: $STORY_FILE"

TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
RUN_DIR="$OUTPUT_DIR/run_$TIMESTAMP"
mkdir -p "$RUN_DIR"/{characters,locations,storyboards,frames,clips,audio/voices,audio/narration,music,final}
cp "$STORY_FILE" "$RUN_DIR/input_story.md"

log "=== PRODUCTION PACK ==="
log "Run:   $RUN_DIR"
log "Story: $STORY_FILE"
log "GPU:   $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo 'unknown')"

notify "Production started\nStory: $(basename "$STORY_FILE")\nRun: $RUN_DIR" "Production Pack"

# ═══════════════════════════════════════════════════════════
# PHASE 1: PRE-PRODUCTION [Qwen 3.5 122B]
# ═══════════════════════════════════════════════════════════

step "PHASE 1: PRE-PRODUCTION"
notify "Phase 1: Pre-production starting (Qwen 3.5 122B)" "Pre-Production"
swap_to_122b

# 1a. Story Creator
step "1a. Story Creator — structuring story"
write_prompt "$RUN_DIR/prompt_story.txt" \
    "Read the story below and structure it into the production JSON format (see story-schema skill). Include: characters with full visual descriptions and identity anchors, locations with color palettes, scenes with dialogue and narration separated, emotional beats, visual mood. Output valid JSON.

--- STORY ---
$(cat "$RUN_DIR/input_story.md")"

run_dsh_agent "story-creator" "$RUN_DIR/prompt_story.txt" "$RUN_DIR/story.json"

# 1b. Director
step "1b. Director — creating shot list"
write_prompt "$RUN_DIR/prompt_director.txt" \
    "Read the structured story at $RUN_DIR/story.json. Create a complete shot list following the shot-list-schema skill. For each scene, plan shots:
- Duration: 3-15 seconds per clip
- Mode: ref2va (default), composited_i2v (new scenes with characters), fl2va (continuity only)
- Mark which shots have DIALOGUE (needs lip sync) vs NARRATION (no lip sync)
- Specify camera, characters, hand positions, identity anchors
- Output valid JSON to be processed shot-by-shot."

run_dsh_agent "director" "$RUN_DIR/prompt_director.txt" "$RUN_DIR/shot_list.json"

# 1c. Character Designer
step "1c. Character Designer — generating reference sheets"
ensure_comfyui
stop_all_llms
swap_to_122b

write_prompt "$RUN_DIR/prompt_characters.txt" \
    "Read $RUN_DIR/story.json. For each character, generate multi-angle reference sheets using Qwen Image 2.1 via ComfyUI API at $COMFYUI_URL. Model: qwen_image_2.1_bf16.safetensors. Create front, 3/4, side, back views at 2048x2048. Save to $RUN_DIR/characters/{name}/. Output a manifest JSON listing all generated files."

run_dsh_agent "character-designer" "$RUN_DIR/prompt_characters.txt" "$RUN_DIR/character_manifest.json"

# 1d. Location Designer
step "1d. Location Designer — generating environments"
write_prompt "$RUN_DIR/prompt_locations.txt" \
    "Read $RUN_DIR/story.json. For each location, generate reference images using Qwen Image 2.1 via ComfyUI at $COMFYUI_URL. Create establishing and medium shots with mood-appropriate lighting. Save to $RUN_DIR/locations/{name}/. Output a manifest JSON."

run_dsh_agent "location-designer" "$RUN_DIR/prompt_locations.txt" "$RUN_DIR/location_manifest.json"

# 1e. Voice Design (pre-production)
step "1e. Audio Producer — voice design"
swap_to_27b
write_prompt "$RUN_DIR/prompt_voices.txt" \
    "Read $RUN_DIR/story.json. For each character, design a voice profile and generate a 5-second reference clip using Chatterbox (port 9882) or CosyVoice (port 50000). Select narrator voice from Kokoro (port 9881). Save voice refs to $RUN_DIR/audio/voices/. Output voice config JSON."

run_dsh_agent "audio-producer" "$RUN_DIR/prompt_voices.txt" "$RUN_DIR/audio/voices/voice_config.json"

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

for SHOT_ID in $SHOT_IDS; do
    CURRENT=$((CURRENT + 1))
    CLIP_DIR="$RUN_DIR/clips/$SHOT_ID"
    mkdir -p "$CLIP_DIR"

    step "CLIP $CURRENT/$TOTAL: $SHOT_ID"
    notify "Clip $CURRENT/$TOTAL: $SHOT_ID starting" "Clip Generation"

    RETRY=0
    MAX_RETRY=3
    PASSED=false

    while [ "$PASSED" = "false" ] && [ $RETRY -lt $MAX_RETRY ]; do
        [ $RETRY -gt 0 ] && log "RETRY $RETRY/$MAX_RETRY for $SHOT_ID"

        # ── 2a. Screenplay Reviewer (122B) ──
        log "[$SHOT_ID] Prompt review..."
        swap_to_122b
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
done

# ═══════════════════════════════════════════════════════════
# PHASE 3: POST-PRODUCTION [Qwen 3.8 27B]
# ═══════════════════════════════════════════════════════════

step "PHASE 3: POST-PRODUCTION"
notify "All clips done. Phase 3: Post-production starting" "Post-Production"
swap_to_27b

# 3a. Narration
step "3a. Narration (Kokoro — no lip sync)"
write_prompt "$RUN_DIR/prompt_narration.txt" \
    "Generate ALL narration audio. Read $RUN_DIR/story.json for narrator lines per scene. Use Kokoro (port 9881). Time to align with clips. This is post-production only — NO lip sync. Save to $RUN_DIR/audio/narration/"
run_dsh_agent "audio-producer" "$RUN_DIR/prompt_narration.txt" "$RUN_DIR/audio/narration/narration_meta.json"

# 3b. Music
step "3b. Music & SFX"
write_prompt "$RUN_DIR/prompt_music.txt" \
    "Create background score and SFX. Read $RUN_DIR/story.json for mood per scene. Generate separate stems. Save to $RUN_DIR/music/"
run_dsh_agent "music-composer" "$RUN_DIR/prompt_music.txt" "$RUN_DIR/music/music_meta.json"

# 3c. Subtitles
step "3c. Subtitles"
write_prompt "$RUN_DIR/prompt_subs.txt" \
    "Create timed subtitles (SRT + ASS). Read $RUN_DIR/shot_list.json and audio metadata. Dialogue: [CHARACTER]: text. Narration: (Narrator) italic. Save to $RUN_DIR/final/"
run_dsh_agent "subtitle-generator" "$RUN_DIR/prompt_subs.txt" "$RUN_DIR/final/sub_meta.json"

# 3d. Assembly
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
