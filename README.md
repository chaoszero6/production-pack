# Production Pack

AI-powered movie production pipeline using [DeepSeek Harness (dsh)](https://github.com/deepseek-ai/deepseek-harness) agents. Takes a story and produces a complete Pixar-style animated short film with lip-synced dialogue, narration, background music, subtitles — upscaled to 4K 60fps.

## Architecture

14 specialized dsh agents orchestrated through a phased pipeline:

```
Story Creator → Director → Character Designer + Location Designer
                              ↓
                 ┌─── Per-Clip Loop ──────────────────────────┐
                 │  Screenplay Reviewer (H3 prompt)           │
                 │       ↓                                    │
                 │  Audio Producer (dialogue for lip sync)    │
                 │       ↓                                    │
                 │  Image Generator (scene compositing)       │
                 │       ↓                                    │
                 │  Video Generator (MiniMax H3)              │
                 │       ↓                                    │
                 │  QA Inspector (multimodal review)          │
                 │       → PASS → upscale 4K 60fps → next    │
                 │       → FAIL → fix prompt → retry          │
                 └────────────────────────────────────────────┘
                              ↓
                 Narration → Music → Subtitles → Final Assembly
```

### Agent Model Assignment

| Role | Model | Agents |
|------|-------|--------|
| Reasoning | Qwen 3.5 122B-A10B | Story Creator, Director, Screenplay Reviewer, Character Designer, Location Designer |
| Execution | Qwen 3.8 27B (multimodal) | Image/Video Generator, Audio Producer, QA Inspector, Pipeline Orchestrator, Subtitle Generator, Post-Production Editor, Music Composer, Storyboard Artist |

Models swap automatically via the [dsh-engine-switch](https://dsh-plugin.org/plugins/yuki-takuya-kun/dsh-engine-switch) plugin. Only one LLM runs at a time.

## Tech Stack

| Component | Tool | Purpose |
|-----------|------|---------|
| Agent Framework | [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) | Agent orchestration, plugin system |
| Image Generation | Qwen Image 2.1 | Character refs, location refs, scene compositing, first frames |
| Video Generation | MiniMax H3 | ref2va / i2va / fl2va clip generation (3-15s) |
| Dialogue TTS | Chatterbox / CosyVoice 3 | Zero-shot voice cloning, lip-synced via H3 `<Audio>` refs |
| Emotional TTS | Orpheus 3B | Intense scenes — crying, whispering, shouting (on-demand) |
| Narration TTS | Kokoro | Narrator voice, overlaid in post (no lip sync) |
| QA Vision | Qwen 3.8 27B | Multimodal frame analysis — hands, faces, identity, lip sync |
| Spatial Upscale | NVIDIA RTX Video Super Resolution | 1080p → 4K per clip |
| Temporal Upscale | RIFE | Frame interpolation to 60fps |
| Workflow Engine | ComfyUI | Runs all image/video/upscale workflows |
| Notifications | Hermes Agent | Discord updates at every pipeline step |

## Usage

### New production
```bash
./run.sh story.md
```

### Resume (skip completed steps)
```bash
./run.sh --resume
```

### Resume from a specific clip
```bash
./run.sh --resume --from-clip S02_003
```

### Regenerate a single clip
```bash
./run.sh --resume --only-clip S01_005
```

### Via Discord (Hermes Focus)
Upload a `story.md` file or paste your story — Hermes triggers the pipeline and sends progress updates to `#film-maker`.

## Dialogue vs Narration

| Type | When Generated | Lip Sync | How |
|------|---------------|----------|-----|
| **Dialogue** | BEFORE video | Yes | Audio → `<Audio>` ref + `<d>` tags in H3 prompt → baked into clip |
| **Narration** | AFTER all clips | No | Overlaid in post-production, characters shown with closed lips |

## MiniMax H3 Video Modes

| Mode | When to Use |
|------|-------------|
| **ref2va** | Default — identity locking via reference images |
| **composited i2va** | New scene openings — composite characters into location via Qwen Image Edit, then animate |
| **fl2va** | Continuity — previous clip's last frame becomes this clip's first frame |
| **t2va** | Avoid — inconsistent output, only for abstract shots without characters |

## Project Structure

```
production-pack/
├── run.sh                          # Main pipeline script
├── AGENTS.md                       # Architecture documentation
├── config/
│   ├── pipeline.yml                # Service endpoints, generation settings
│   └── models.yml                  # LLM model swap configuration
├── .dsh/
│   ├── .agent-presets/             # 14 agent presets (agent.cordis.yml)
│   └── skills/                     # 7 skills (schemas, workflows, QA)
├── knowledge/
│   └── minimax_h3_rules.md         # Artifact prevention knowledge base
├── pipeline/
│   ├── orchestrator.py             # Main production loop
│   ├── service_manager.py          # systemctl start/stop/health
│   ├── comfyui_client.py           # ComfyUI REST API + WebSocket client
│   ├── queue_manager.py            # Clip state machine (pending→gen→QA→pass/fail)
│   ├── monitor.py                  # Health monitoring + progress tracking
│   ├── kokoro_server.py            # Kokoro TTS HTTP server (port 9881)
│   ├── chatterbox_server.py        # Chatterbox TTS HTTP server (port 9882)
│   └── notify.sh                   # Discord notification helper
├── hermes/
│   └── production-pack-status.md   # Hermes skill for Discord control
└── output/                         # Generated assets per run
    └── run_<timestamp>/
        ├── story.json
        ├── shot_list.json
        ├── characters/
        ├── locations/
        ├── clips/<shot_id>/
        │   ├── clip.mp4
        │   ├── clip_4k60.mp4
        │   ├── qa_verdict.json
        │   └── reviewed_prompt.json
        ├── audio/
        ├── music/
        └── final/
            ├── movie_4k60.mp4
            ├── movie.srt
            └── movie.ass
```

## Hardware Requirements

Tested on: **RTX 5090 32GB**, Debian 13, CUDA 13.1

| Phase | VRAM Usage |
|-------|-----------|
| Reasoning (122B) | ~27 GB GPU + system RAM offload |
| Generation (27B + ComfyUI + H3) | ~28-32 GB |
| QA (27B multimodal) | ~20 GB |
| Upscaling (RTX + RIFE) | ~8-12 GB |

## Services

| Service | Port | systemd Unit |
|---------|------|-------------|
| Qwen 3.5 122B | 8085 | `llama-qwen35-122b.service` |
| Qwen 3.8 27B | 8085 | `qwen3.8-27b-q6k-cuda.service` |
| ComfyUI | 8188 | `comfyui.service` |
| CosyVoice | 50000 | `cosyvoice.service` |
| Kokoro TTS | 9881 | `kokoro-tts.service` |
| Chatterbox TTS | 9882 | `chatterbox-tts.service` |

> Both LLMs share port 8085 — only one runs at a time. The pipeline swaps them via `systemctl`.

## License

Copyright (c) 2026 Vinoth Kannah MP. All Rights Reserved.

This software is proprietary. Public for viewing/portfolio only — no permission to use, copy, modify, or distribute. See [LICENSE](LICENSE) for details.
