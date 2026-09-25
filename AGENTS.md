# Production Pack - AI Movie Production Pipeline

> Pixar-style fantasy movie production using dsh agent orchestration

## Architecture Overview

This production pack orchestrates 14 specialized agents to produce a complete animated movie
with consistent characters, continuity, dialogues, narration, BGM, and subtitles.
Each agent is a dsh preset in `.dsh/.agent-presets/` with skills in `.dsh/skills/`.

## Pipeline Flow

```
═══════════════════════════════════════════════════════════════
 PHASE A — REASONING  [Qwen 3.5 122B-A10B loaded, ~75 GB]
═══════════════════════════════════════════════════════════════

  Story Creator → Director → Character Designer + Location Designer
                                ↓
                        Storyboard Artist

═══════════════════════════════════════════════════════════════
 PER-CLIP LOOP
═══════════════════════════════════════════════════════════════

  ┌─ CROSS-CLIP CONTINUITY FRAME EXTRACTION ───────────────────┐
  │  Extract reference frames from adjacent clips in scene:    │
  │  • prev_clip_lastframe.png  (start anchor)                 │
  │  • scene_establishing_frame.png  (room layout anchor)      │
  │  • next_clip_firstframe.png  (end anchor, if exists)       │
  ├────────────────────────────────────────────────────────────┤
  │ PHASE A: REASONING [load 122B] ────────────────────────────┤
  │  Screenplay Reviewer (builds H3-format prompt)             │
  │    • Receives continuity frames as <Picture N> refs        │
  │    • Applies spatial anchoring for exit/entrance shots      │
  │       ↓                                                    │
  │  Has Dialogue? ──YES──→ Audio Producer (Orpheus 3B default)│
  │       │                  → generates voice on port 9883    │
  │       │                  → becomes <Audio N> ref + <d> tags│
  │       NO → skip (narration added in post, no lip sync)     │
  ├────────────────────────────────────────────────────────────┤
  │ PHASE B: GENERATION [unload 122B → load 27B + ComfyUI]     │
  │  Image Generator → scene compositing / frames              │
  │    • Uses continuity frames as visual reference             │
  │       ↓                                                    │
  │  Video Generator → clip (MiniMax H3, turbo LoRA)           │
  │       • Dialogue: <Audio> ref + <d> tags = lip sync        │
  │       • Narration: NO <Audio>, NO <d> = no lip movement    │
  ├────────────────────────────────────────────────────────────┤
  │ PHASE C: QA [11-category inspection, max 5 retries]        │
  │  QA Inspector → 11 scoring categories:                     │
  │    1-8: hands, face, identity, duplication, motion,        │
  │         lip sync, texture, composition                     │
  │    9:   cross-clip continuity (frame comparison)           │
  │    10:  voice quality (anti-robotic check)                 │
  │    11:  film quality (cinematic polish)                    │
  │       → PASS → send clip to Discord → next clip            │
  │       → FAIL → send clip to Discord → retry (max 5)        │
  │       → ESCALATED → send to Discord for manual review      │
  └────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════
 BATCH UPSCALE  [ComfyUI only, no LLM]
═══════════════════════════════════════════════════════════════

  All passed clips → 4K 60fps upscale (GPU or CPU fallback)

═══════════════════════════════════════════════════════════════
 POST-PRODUCTION  [Qwen 3.8 27B loaded, lightweight]
═══════════════════════════════════════════════════════════════

  Audio Producer → narration audio (Kokoro, no lip sync)
       ↓
  Music Composer → score, ambient, SFX
       ↓
  Subtitle Generator → dialogue + narration subs (.srt/.ass)
       ↓
  Post-Production Editor
     • Dialogue audio already baked into clips (lip-synced)
     • Overlay narration (no lip sync — post only)
     • Layer music, SFX, ambient
     • Burn subtitles
       ↓
  Final Movie (.mp4 + .srt + .ass)
```

### Model Switching (via dsh-engine-switch plugin)
| Agent | Model | Why |
|-------|-------|-----|
| Story Creator, Director, Screenplay Reviewer, Character Designer, Location Designer | **Qwen 3.5 122B-A10B** | Complex reasoning, creative writing, structured prompt construction |
| All other agents (9 total) | **Qwen 3.8 27B** | Fast execution, tool use, multimodal vision for QA |

Models are swapped automatically per preset via the `dsh-engine-switch` plugin.
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
- Applies knowledge base of known H3 artifacts and bugs (`knowledge/minimax_h3_rules.md`)
- Prevents: character duplication, hand anomalies, face distortion in wide shots,
  POV hand issues, text rendering problems, spatial geometry collapse
- Applies spatial anchoring for exit/entrance shots (pins room geometry, limits clip to 6s)
- Includes continuity frame references as `<Picture N>` in H3 prompts
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
  - **Orpheus 3B** — PRIMARY dialogue engine (best prosody, natural emotion via LLM inference)
    - Paralinguistic tags: `<laugh>`, `<sigh>`, `<gasp>`, `<chuckle>`, `<sniffle>`, `<groan>`
    - 8 voices: tara, leah, jess, leo, dan, mia, zac, zoe
    - ~3.5 GB VRAM (GGUF Q8_0 via llama.cpp), port 9883
  - **Chatterbox** — fallback dialogue (zero-shot cloning, MIT license), port 9882
  - **CosyVoice 3** — multilingual/cross-lingual dialogue, port 9880
  - **Kokoro** — narration only (lightweight, 54 built-in voices), port 9881
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
- Applies director's shot specifications and camera directions

### 11. QA Inspector
**Preset:** `.dsh/.agent-presets/qa-inspector/`
- Reviews each generated clip using Qwen 3.8 27B vision model
- 11 scoring categories (all must score >= 0.85, film quality >= 0.80):
  1. Hands & limbs  2. Face & expression  3. Character identity
  4. Duplication  5. Motion quality  6. Lip sync  7. Texture & visual
  8. Composition  9. Cross-clip continuity  10. Voice quality  11. Film quality
- Cross-clip continuity: extracts last frame of previous clip, compares with first frame
  Checks props, character direction, 180-degree rule, spatial layout, lighting, costume
- Voice quality: detects robotic speech (monotone pitch, flat prosody, metallic timbre)
- No CONDITIONAL PASS — strict binary verdict
- Max 5 retries before escalation to director review
- Sends clip video to Discord on every QA pass, fail, or escalation

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

## Service Dependencies

| Service          | Used By                        | Port  | VRAM     | Notes                              |
|-----------------|--------------------------------|-------|----------|------------------------------------|
| Qwen 3.5 122B   | Story, Director, Reviewer, etc | 11434 | ~75 GB Q4| Reasoning agents (load on demand)   |
| Qwen 3.8 27B    | All execution + QA agents      | 11435 | ~16 GB Q4| Multimodal — also handles QA vision |
| ComfyUI         | Image Gen, Video Gen           | 8188  | varies   | Workflow execution engine           |
| Qwen Image 2.1  | Image Generator                | -     | ~8-10 GB | Via ComfyUI node                    |
| MiniMax H3      | Video Generator                | -     | ~8-12 GB | Via ComfyUI node                    |
| Orpheus 3B      | Audio Producer                 | 9883  | ~3.5 GB  | PRIMARY dialogue (GGUF Q8, llama.cpp) |
| Chatterbox      | Audio Producer                 | 9882  | ~4-6 GB  | Fallback dialogue (zero-shot cloning) |
| CosyVoice 3     | Audio Producer                 | 9880  | ~4-6 GB  | Multilingual / cross-lingual voice  |
| Kokoro          | Audio Producer                 | 9881  | ~2-3 GB  | Narration only (lightweight)        |

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
