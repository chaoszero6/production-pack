# Production Pack - AI Movie Production Pipeline

> Pixar-style fantasy movie production using dsh agent orchestration

## Architecture Overview

This production pack orchestrates 14 specialized agents to produce a complete animated movie
with consistent characters, continuity, dialogues, narration, BGM, and subtitles.
Each agent is a dsh preset in `.dsh/.agent-presets/` with skills in `.dsh/skills/`.

## Pipeline Flow

```
═══════════════════════════════════════════════════════════════
 PHASE A — REASONING  [cloud: deepseek-v4-pro, effort=high]
═══════════════════════════════════════════════════════════════

  Story Creator → Director → Character Designer + Location Designer
                                ↓
                        Storyboard Artist

═══════════════════════════════════════════════════════════════
 PER-CLIP LOOP
═══════════════════════════════════════════════════════════════

  ┌─ PHASE A: REASONING [deepseek-v4-pro] ────────────────────┐
  │  Screenplay Reviewer (builds H3-format prompt)             │
  │       ↓                                                    │
  │  Has Dialogue? ──YES──→ Audio Producer generates voice     │
  │       │                  (Chatterbox/CosyVoice/Orpheus)    │
  │       │                  → becomes <Audio N> ref + <d> tags│
  │       NO → skip (narration added in post, no lip sync)     │
  ├────────────────────────────────────────────────────────────┤
  │ PHASE B: GENERATION [ComfyUI on the local RTX 5090]        │
  │  Image Generator → scene compositing / frames              │
  │       ↓                                                    │
  │  Video Generator → clip (MiniMax H3)                       │
  │       • Dialogue: <Audio> ref + <d> tags = lip sync        │
  │       • Narration: NO <Audio>, NO <d> = no lip movement    │
  ├────────────────────────────────────────────────────────────┤
  │ PHASE C: QA [deepseek-v4-flash-vision-exp — the one        │
  │  │           routed model that actually accepts images]     │
  │  QA Inspector → analyzes frames                             │
  │       → PASS → next clip                                   │
  │       → FAIL → back to Phase A (rewrite the prompt)        │
  └────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════
 POST-PRODUCTION  [cloud, no local model needed]
═══════════════════════════════════════════════════════════════

  Audio Producer → narration audio (Kokoro, no lip sync)
       ↓
  Music Composer → score, ambient, SFX
       ↓
  Subtitle Generator → dialogue + narration subs (.srt/.ass)
       ↓
  Post-Production Editor
     • APPLY $RUN_DIR/assembly_fixes.json FIRST (blocking checklist)
     • Dialogue: strip the rejected native H3 track, mux the APPROVED
       stem (clips/<SID>/dialogue.wav) at the recorded offset
     • Overlay narration (no lip sync — post only)
     • Layer music, SFX, ambient
     • Burn subtitles
       ↓
  Final Movie (.mp4 + .srt + .ass)
```

### Model Switching (per-agent routing, NOT a plugin)

There is **no `dsh-engine-switch` plugin** (it 404s on npm) and the `engine:` field in a preset is
a no-op. The model is resolved from the **`agent-default-model` section of `/root/.dsh/settings.yaml`
at dsh launch**, so the route is rewritten before every agent call. All agents run on the
**OpenRouter cloud** — no local LLM server is involved.

The per-agent table lives in **`pipeline/set_agent_model.py`** (single source of truth; `run.sh`'s
`set_agent_model` and `pipeline/generate_clip.py` both call it, so a subprocess can never inherit
the previous agent call's route). `CLOUD_ROUTING=1` in `run.sh` is the active mode; set it to `0`
to fall back to the retired local layout.

| Agent | Model (OpenRouter) | Effort |
|-------|--------------------|--------|
| Story Creator, Director, Screenplay Reviewer, Character Designer, Location Designer | `qwen3.8-27b` (ninfer:8080) | xhigh |
| QA Inspector — the ONLY vision agent | `google/gemini-3-flash-preview` | high |
| All other agents | `qwen3.8-27b` (ninfer:8080) | xhigh |

**Only `qa-inspector` needs vision** (it extracts frames at 2 FPS and scores hands/face/identity
per frame). Capability labels lie — probe before trusting a declared `input:` list.
`deepseek/deepseek-v4-pro` **hard-rejects images** (HTTP 404 "No endpoints found that support image
input"), so it must never be given a vision job. Verified 2026-09-23: `gemini-3-flash-preview` and
`qwen/qwen3-vl-32b-instruct` both read images correctly; a real 1280×704 frame costs ~1100 prompt
tokens on gemini and ~910 on qwen-vl. Budget tier = `qwen/qwen3-vl-32b-instruct`.

Routing is re-resolved on every agent call, so a route change takes effect without restarting the run.
The pipeline phases naturally — reasoning and generation never run simultaneously.

## Agents

### 1. Story Creator
**Preset:** `.dsh/.agent-presets/story-creator/`
- Writes complete story with scenes, dialogue, emotional arcs
- Outputs structured JSON with scenes, characters, locations, mood
- Specializes in Pixar-style storytelling (heart, humor, stakes)

### 2. Director
**Preset:** `.dsh/.agent-presets/director/`
- Breaks story into numbered shots with camera angles
- Decides which characters and locations need visual generation
- Specifies keyframe requirements (first-frame from previous clip output)
- Defines audio requirements per character and narrator
- Controls pacing, transitions, and emotional timing

### 3. Character Designer
**Preset:** `.dsh/.agent-presets/character-designer/`
- Generates multi-angle character reference sheets via Qwen Image 2.1
- Creates front/side/back/3-quarter views for each character
- Maintains consistent style across all characters
- Outputs reference images for identity-locking in video generation

### 4. Location Designer
**Preset:** `.dsh/.agent-presets/location-designer/`
- Generates environment/location reference images
- Creates day/night/weather variants as needed
- Ensures locations match story mood and Pixar aesthetic
- Provides wide establishing shots and detail textures

### 5. Storyboard Artist
**Preset:** `.dsh/.agent-presets/storyboard-artist/`
- Creates visual storyboard panels from director's shot list
- Quick sketch-style frames showing composition and action
- Validates visual flow before committing to expensive generation
- Annotates with camera moves, timing, and transitions

### 6. Screenplay Reviewer
**Preset:** `.dsh/.agent-presets/screenplay-reviewer/`
- Reviews all prompts before they reach MiniMax H3
- Applies knowledge base of known H3 artifacts and bugs
- Prevents: character duplication, hand anomalies, face distortion in wide shots,
  POV hand issues, text rendering problems, audio crosstalk
- Structures prompts with temporal decomposition [START-END] format
- Adds negative constraints to prevent common failures

### 7. Image Generator
**Preset:** `.dsh/.agent-presets/image-generator/`
- Manages Qwen Image 2.1 workflows in ComfyUI
- Generates first-frame and last-frame images for each clip
- Uses character/location references for consistency (up to 10 refs)
- Handles text-to-image and reference-to-image generation

### 8. Audio Producer
**Preset:** `.dsh/.agent-presets/audio-producer/`
- 4 TTS engines, chosen per task:
  - **Chatterbox** — primary dialogue (best naturalness, zero-shot cloning, MIT)
  - **CosyVoice 3** — multilingual/cross-lingual dialogue, instruction-based emotion
  - **Orpheus 3B** — intense emotional scenes (crying, shouting, whispering — load on demand)
  - **Kokoro** — narration only (lightweight, 54 built-in voices)
- Dialogue generated BEFORE video (for H3 lip sync via `<Audio>` ref)
- Narration generated AFTER all clips (overlaid in post, NO lip sync)

### 9. Music Composer
**Preset:** `.dsh/.agent-presets/music-composer/`
- Generates background score matching scene mood
- Creates ambient soundscapes and sound effects
- Handles musical transitions between scenes
- Produces final audio mix layers

### 10. Video Generator
**Preset:** `.dsh/.agent-presets/video-generator/`
- Manages MiniMax H3 workflows in ComfyUI
- Supports T2V, I2V, and First-Last-Frame-to-Video modes
- Handles 2K output, up to 15-second clips with stereo audio
- ⚠ `ref2va` ALWAYS emits its own native H3 audio branch on stream 1, **even when
  `use_native_h3_audio=false`** in `shot_list.json`. That track is H3's own generated
  take, not the approved TTS stem — it may match the stem closely or be a completely
  different take (measured r≈0.07 on S01_004). Never treat it as the dialogue master;
  see "QA Gate & Retry Policy".
- Applies director's shot specifications and camera directions

### 11. QA Inspector
**Preset:** `.dsh/.agent-presets/qa-inspector/`
- Reviews each generated clip using Qwen2.5-VL vision model
- Checks for: hand anomalies, face distortion, character duplication,
  motion artifacts, texture instability, identity drift, audio quality
- Issues PASS/FAIL verdict with detailed feedback
- Verdict carries `re_render_recommended` (bool) and `recommended_action`
  (`"ASSEMBLY_FIX_ONLY"` when the render itself is fine) — these now gate retries,
  see "QA Gate & Retry Policy"
- On FAIL: provides specific prompt corrections for regeneration
- Tracks retry count and escalates after max retries

### 12. Pipeline Orchestrator
**Preset:** `.dsh/.agent-presets/pipeline-orchestrator/`
- Manages service lifecycle (ComfyUI, Qwen model loading/unloading)
- Coordinates: stop Qwen → generate image/video → restart Qwen → QA
- Monitors ComfyUI health and restarts if needed
- Manages VRAM allocation between models
- Queues and sequences clip generation

### 13. Subtitle Generator
**Preset:** `.dsh/.agent-presets/subtitle-generator/`
- Creates timed subtitles for all dialogue and narration
- Outputs SRT and ASS formats with character attribution
- Handles timing sync with audio tracks
- Supports styled subtitles (italic narration, character-colored dialogue)

### 14. Post-Production Editor
**Preset:** `.dsh/.agent-presets/post-production-editor/`
- Assembles all approved clips into scene sequences
- Layers audio: dialogue, narration, music, SFX
- Handles transitions between clips
- Produces final movie file with proper encoding

## QA Gate & Retry Policy

The per-clip gate is `run.sh` reading **`$CLIP_DIR/qa_verdict.json['verdict']`** — PASS advances to the
next clip, FAIL loops back through prompt review into a fresh H3 render (~7–25 min each). Anything that
makes that one field wrong costs real GPU hours, so it is normalised, never trusted raw.

### The verdict is normalised, never trusted raw
qa-inspector writes its rich report to `clips/<SHOT_ID>/qa_report.json` **or** `qa/<SHOT_ID>/qa_report.json`
— it uses both, so read both and take the newest — and it sometimes writes `qa_verdict.json` as prose.
`normalise_qa_verdict.py` runs after every QA pass and is the ONLY writer of the field the gate reads.
It resolves the verdict from the richest source available, then applies the downgrade below.

### Assembly-fixable FAIL → PASS (do NOT re-render)
A FAIL whose defect lives in the audio stem or the edit — not in the pixels — is downgraded to `PASS`
with `assembly_fix_required: true`, and its fixes are appended to `<run_dir>/assembly_fixes.json`.
Phase 3 assembly consumes that file as a BLOCKING checklist.

Downgrade applies only when ALL of these hold:
- the inspector signalled "no re-render" — in ANY of the three forms it uses, and
- no `critical`-priority correction targets a structural category
  (face / hands_limbs / character_identity / duplication / motion / composition), and
- no structural category scores below 0.70.

The three forms of that signal, checked in order:
1. `re_render_recommended: false` (structured — the most common)
2. `recommended_action: "ASSEMBLY_FIX_ONLY"`
3. **prose only, inside a correction** — "POST-PRODUCTION, NOT A RE-RENDER", "fixable in final
   assembly without re-rendering". S01_003 shipped this way: no structured field at all, and
   defaulting to re-render on that basis cost a full H3 render per retry.

If the inspector is silent in all three, the verdict is **inferred from the measurements** rather
than defaulted to re-render: downgrade only when every structural category clears 0.85, the weighted
overall clears 0.85, and only asset-level categories failed. Anything else keeps the re-render —
that is the conservative default.

Assembly-fixable categories: `audio_quality`, `lip_sync`, `shot_list_data`.
**An explicit `re_render_recommended: true` always wins** — soft wording like "ASSEMBLY_FIX_ONLY"
must never override it.

### Compare takes before accepting a retry
ComfyUI keeps every attempt at `/opt/comfyui/ComfyUI/output/<SHOT_ID>/<SHOT_ID>_h3_0000N_.mp4`.
A retry can come out **worse** than the take it replaced — on S01_005 take 1 scored hands 0.87 / lip 0.88 /
overall 0.901 and was assembly-fixable, while take 2 regressed to hands 0.74 / lip 0.78 / overall 0.843.
Diff the new take's category scores against the previous one before letting it replace the accepted clip;
if the retry regressed, restore the earlier take and archive the new report under `_take_archive/`.

### A pass-through clip still needs its 4K60 master
The upscale step (2g) lives *inside* the QA loop, so a clip that exits via the assembly-fix PASS never
gets one — and resume re-renders any clip lacking `clip_4k60.mp4` (the skip test needs PASS **and** the
master). Use `./upscale_clip.sh <run_dir> <SHOT_ID>` to run just that step.

⚠ `upscale_clip.sh` must export `DSH_PERMISSION_MODE=danger-full-access` exactly as `run.sh:36` does.
dsh confines writes to the harness workspace and headless has no approval channel, so without that export
the agent builds a correct 4K master and then cannot copy it into the run dir — it reports the blocker and
the script's fallback silently ships a LOW-RES master. The script now also salvages the master from
`$DSH_DIR/.work/<SHOT_ID>/` if that happens.

## Editorial Rules — *The Clockwork Moth* postmortem (2026-10-07)

The first public cut was reviewed as the work of "an inexperienced filmmaker": same voice for
narrator, child and grandmother; a clock that changed time across a cut; a small dissolve on every
cut; shots that "don't cut" (jump cuts). All 102 clips had passed per-clip QA — the defects were
**between** clips and **between** voices, which nothing measured. Fixes live in
`.dsh/skills/editing-grammar.md`, `.dsh/skills/voice-casting.md`, `knowledge/minimax_h3_rules.md §7`
and two tools; the rules that must never regress:

| Complaint | Root cause | Rule / tool |
|-----------|-----------|-------------|
| Dissolve on every cut | `upscale_film.py` ran RIFE over the ASSEMBLED film, blending shot A's last frame into shot B's first | Interpolate per clip only. `upscale_film.py` now **refuses to run without a cut list** (`--concat-list final/concat.txt`); it holds each shot's last frame and decimates with a global parity so there is no drift |
| Jump cuts | Shot list had 9 consecutive pairs with same character + same size + same angle; dialogue clips were sized to the stem (0.4 s / 0.6 s) so there were no handles to cut into | Director: consecutive shots differ by subject, ≥ 2 shot sizes or ≥ 30°; every clip carries ≥ 1 s handles with `edit.cut_in` / `edit.cut_out`; reaction + insert per dialogue scene. `pipeline/edit_check.py` scores every boundary (threshold 0.80) before assembly and inside QA (`edit_pair`) |
| Clock time changes across the cut | No ledger for readable props | `readable_props` per scene in the story/shot list, phrase copied verbatim into every prompt, QA continuity category 9j fails on contradiction |
| Same voice for everyone | Narrator, boy, grandmother and moth were all Orpheus adult-female voices (`tara/jess/leah/zoe`) within ~3 semitones | Cast by **voice class**; narrator in a class no character uses; measured line-up with ≥ 4 semitone separation (`audio/voices/lineup/separation.json`, `CAST_FAIL` blocks Phase 2); child/elderly via Chatterbox clone or CosyVoice 3 instruction, never adult + affectation |

Pipeline hooks: `run.sh` 1b validates the shot list for jump cuts and handles
(`edit_check.py --shot-list`), 1e builds the voice line-up, 3d runs `edit_check.py --run-dir` and
hands `final/edit_report.json` to the Post-Production Editor, who records every boundary decision in
`final/edit_decisions.json` and cuts straight (no `xfade` unless the Director scripted it).

## Service Dependencies

> **Model routing is per-run and currently ALL CLOUD via OpenRouter** (`run.sh`'s `set_agent_model`
> → `pipeline/set_agent_model.py` rewrites the `agent-default-model` section of
> `/root/.dsh/settings.yaml` before each agent call — see "Model Switching"). A cloud-routed run
> needs **no local LLM services at all** — only ComfyUI and the TTS servers. The two Qwen rows below
> are the **retired local layout**, kept only for `CLOUD_ROUTING=0` rollback. Check the run log for
> `Model route: <agent> -> openrouter/<model>` to see which layout is in effect; `openrouter/` means
> cloud and the Qwen rows are unused.
>
> ⚠ Under cloud routing the pack deliberately **never starts the local LLM**, so a Qwen server left
> running from a previous local-mode run will sit on ~27 GB of VRAM and starve H3. Stop it by hand:
> `systemctl stop qwen3.8-27b-q6k-cuda.service` (+ `docker stop qwen38-27b-q6k` if it lingers).

| Service          | Used By                        | Port  | VRAM     | Notes                              |
|-----------------|--------------------------------|-------|----------|------------------------------------|
| ~~Qwen 3.5 122B~~ | ~~Story, Director, Reviewer~~ | 11434 | ~75 GB Q4| RETIRED 2026-09-21 — cloud instead  |
| ~~Qwen 3.8 27B~~  | ~~All execution + QA agents~~ | 11435 | ~16 GB Q4| RETIRED 2026-09-21 — cloud instead  |
| ninfer          | dsh local route (`ninfer`)     | 8080  | ~28 GB   | Swift Qwen3.8 27B, int8 KV 262k     |
| ninfer-us       | dsh local route (`ninfer-us`)  | 8081  | ~31 GB   | Huihui-Abliterated, int8 KV 262k    |
| ComfyUI         | Image Gen, Video Gen           | 8188  | varies   | Workflow execution engine           |
| Qwen Image 2.1  | Image Generator                | -     | ~8-10 GB | Via ComfyUI node                    |
| MiniMax H3      | Video Generator                | -     | ~8-12 GB | Via ComfyUI node                    |
| Chatterbox      | Audio Producer                 | 9882  | ~4-6 GB  | Primary dialogue (best naturalness) |
| CosyVoice 3     | Audio Producer                 | 9880  | ~4-6 GB  | Multilingual / cross-lingual voice  |
| Orpheus 3B      | Audio Producer                 | 9883  | ~8-12 GB | Emotional dialogue (load on demand) |
| Kokoro          | Audio Producer                 | 9881  | ~2-3 GB  | Narration only (lightweight)        |

⚠ `ninfer` and `ninfer-us` have mutual systemd `Conflicts=` — only one can hold the RTX 5090 at a time.
Both are registered as dsh providers; switch with `systemctl start ninfer` / `ninfer-us`.

## Output Structure

```
output/
├── characters/    # Character reference sheets
├── locations/     # Location reference images
├── storyboards/   # Storyboard panels
├── frames/        # First/last frame images per clip
├── clips/         # Generated video clips
├── audio/         # Voice and narration tracks
├── music/         # Background score and SFX
└── final/         # Assembled movie
```
