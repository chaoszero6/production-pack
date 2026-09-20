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

  ┌─ PHASE A: REASONING [load 122B] ──────────────────────────┐
  │  Screenplay Reviewer (builds H3-format prompt)             │
  │       ↓                                                    │
  │  Has Dialogue? ──YES──→ Audio Producer generates voice     │
  │       │                  (Chatterbox/CosyVoice/Orpheus)    │
  │       │                  → becomes <Audio N> ref + <d> tags│
  │       NO → skip (narration added in post, no lip sync)     │
  ├────────────────────────────────────────────────────────────┤
  │ PHASE B: GENERATION [unload 122B → load 27B + ComfyUI]     │
  │  Image Generator → scene compositing / frames              │
  │       ↓                                                    │
  │  Video Generator → clip (MiniMax H3)                       │
  │       • Dialogue: <Audio> ref + <d> tags = lip sync        │
  │       • Narration: NO <Audio>, NO <d> = no lip movement    │
  ├────────────────────────────────────────────────────────────┤
  │ PHASE C: QA [27B stays loaded — it's multimodal!]           │
  │  QA Inspector → analyzes frames via Qwen 3.8 27B vision   │
  │       → PASS → next clip                                   │
  │       → FAIL → back to Phase A (reload 122B for rewrite)   │
  └────────────────────────────────────────────────────────────┘

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
- Applies director's shot specifications and camera directions

### 11. QA Inspector
**Preset:** `.dsh/.agent-presets/qa-inspector/`
- Reviews each generated clip using Qwen2.5-VL vision model
- Checks for: hand anomalies, face distortion, character duplication,
  motion artifacts, texture instability, identity drift, audio quality
- Issues PASS/FAIL verdict with detailed feedback
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

## Service Dependencies

| Service          | Used By                        | Port  | VRAM     | Notes                              |
|-----------------|--------------------------------|-------|----------|------------------------------------|
| Qwen 3.5 122B   | Story, Director, Reviewer, etc | 11434 | ~75 GB Q4| Reasoning agents (load on demand)   |
| Qwen 3.8 27B    | All execution + QA agents      | 11435 | ~16 GB Q4| Multimodal — also handles QA vision |
| ComfyUI         | Image Gen, Video Gen           | 8188  | varies   | Workflow execution engine           |
| Qwen Image 2.1  | Image Generator                | -     | ~8-10 GB | Via ComfyUI node                    |
| MiniMax H3      | Video Generator                | -     | ~8-12 GB | Via ComfyUI node                    |
| Chatterbox      | Audio Producer                 | 9882  | ~4-6 GB  | Primary dialogue (best naturalness) |
| CosyVoice 3     | Audio Producer                 | 9880  | ~4-6 GB  | Multilingual / cross-lingual voice  |
| Orpheus 3B      | Audio Producer                 | 9883  | ~8-12 GB | Emotional dialogue (load on demand) |
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
