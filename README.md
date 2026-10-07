# Production Pack

An AI movie production pipeline that takes a story file and returns a finished,
subtitled, 4K60 animated short film — dialogue lip-synced, narration mixed,
score and effects laid in, assembled end to end.

Shipped reference production: **The Clockwork Moth**, 10:27, 106 shots,
102/102 clips cleared QA before the final cut.

---

## Quick start

```bash
# 1. clone
git clone <repo-url> production-pack && cd production-pack

# 2. bootstrap a fresh machine (weights, nodes, deps)
./setup.sh --check     # see what's missing
./setup.sh             # download everything required

# 3. configure your API key (never committed — read at runtime)
export OPENROUTER_API_KEY="..."
# or
export OPENCODE_GO_API_KEY="..."

# 4. run
./run.sh story.md
```

`setup.sh` is idempotent — re-running it skips files already present and only
fetches what is missing. It reports exit code `2` when required model weights
are still absent.

| Flag | Effect |
|------|--------|
| `--check` | report present/missing, change nothing |
| `--dry-run` | list what would be downloaded |
| `--models` | weights only, skip custom nodes |
| `--nodes` | custom nodes only |
| `--optional` | also clone nodes used only by `workflows/*.json` |
| `--help` | full usage |

Environment overrides: `COMFY_DIR`, `MODEL_DIR`, `HF_TOKEN`, `PP_PROFILE`.

---

## How a run works

```
 story.md
    │
    ▼
 Story Creator ─► Director ─► Character Designer + Location Designer
                                  │
                                  ▼
                          Storyboard Artist
                                  │
      ┌─────────── PER-CLIP LOOP (106 iterations) ───────────┐
      │                                                       │
      │  Screenplay Reviewer  ── rewrites prompt for the      │
      │                         model, injects negatives      │
      │           │                                           │
      │           ▼                                           │
      │  Audio Producer  ── dialogue synthesised FIRST so     │
      │                     lip sync is a generation input    │
      │           │                                           │
      │           ▼                                           │
      │  Image Generator  ── first/last frames from           │
      │                      character + location refs        │
      │           │                                           │
      │           ▼                                           │
      │  Video Generator  ── one clip, MiniMax H3             │
      │           │                                           │
      │           ▼                                           │
      │  QA Inspector  ── samples frames at 2 FPS, scores     │
      │                    hands / face / identity / motion   │
      │           │                                           │
      │     PASS ──┤── FAIL ──► back to Screenplay Reviewer   │
      │            │              (with specific fixes)      │
      └────────────┼──────────────────────────────────────────┘
                   ▼
      Per-clip 4K60 masters ─► Edit check (every boundary scored)
                   │
                   ▼
      Narration ─► Music ─► Subtitles ─► Assembly (straight cuts, trimmed in the handles)
                   │
                   ▼
        final/movie_4k60.mp4 + movie.srt + movie.ass
```

Before the loop starts, the Director's shot list is validated **as an edit**
(`pipeline/edit_check.py --shot-list`): no two consecutive shots of the same
character at the same size and angle, ≥ 1 s handles on every clip, cut points
written, a reaction and an insert per dialogue scene. The Audio Producer casts
voices by voice class and must pass a measured line-up before any clip renders.

### Model routing

Every agent call rewrites the model route in `~/.dsh/settings.yaml` immediately
before it runs, so routing is per-call and can change mid-run without a restart.
The single source of truth is `pipeline/set_agent_model.py` — both `run.sh` and
`pipeline/generate_clip.py` call it, so a subprocess can never inherit the
previous call's route.

| Tier | Default (cloud) | Override |
|------|-----------------|----------|
| Reasoning | `deepseek-v4-pro` | `DSH_REASONING_PROVIDER` / `DSH_REASONING_MODEL` |
| Execution | `deepseek-v4.1-flash` | `DSH_EXECUTION_PROVIDER` / `DSH_EXECUTION_MODEL` |
| Vision (QA) | `google/gemini-3-flash-preview` | `DSH_VISION_PROVIDER` / `DSH_VISION_MODEL` |

Cloud routing is the default (`CLOUD_ROUTING=1`), so **no local LLM is required
to run the pack** — the GPU stays free for ComfyUI. Set `CLOUD_ROUTING=0` to
serve agents from a local `ninfer` unit instead.

> Capability labels are not evidence. `deepseek-v4-pro` advertises image input
> and then rejects it with `404 No endpoints found that support image input`.
> Only `qa-inspector` needs vision, and it is routed to a model that actually
> accepts images. Probe before trusting any declared `input:` list.

---

## Tech stack

| Component | Tool | Purpose |
|-----------|------|---------|
| Agent framework | dsh presets (`.dsh/.agent-presets/`) | 14 specialised agents |
| Image generation | Qwen Image 2.1 via ComfyUI | Character/location refs, frames, compositing |
| Video generation | MiniMax H3 via ComfyUI | `ref2va` / `i2va` / `fl2va` clips (3–15 s) |
| Dialogue TTS | Chatterbox, CosyVoice 3 | Zero-shot voice cloning, primary dialogue |
| Emotional TTS | Orpheus 3B | Crying, whispering, shouting — loaded on demand |
| Narration TTS | Kokoro | Narrator voice, laid in post (no lip sync) |
| QA vision | gemini-3-flash-preview | Per-frame scoring of hands, face, identity |
| Spatial upscale | NVIDIA RTX Video Super Resolution | 4K ULTRA per chunk |
| Temporal upscale | RIFE (rife47.pth) | 24 → 120 fps → decimate to 60 |
| Workflow engine | ComfyUI | All image/video/upscale graphs |
| Notifications | `pipeline/notify.sh` | Discord updates |

### Dialogue vs narration

| Type | Generated | Lip sync | How |
|------|-----------|----------|-----|
| Dialogue | **before** video | yes | TTS stem → H3 `<Audio>` reference + `<d>` tags → baked into the clip |
| Narration | **after** all clips | no | Overlaid in post; sidechain-ducks dialogue, music and SFX underneath |

### MiniMax H3 modes

| Mode | When to use |
|------|-------------|
| `ref2va` | Default — identity locking via reference images |
| `composited i2va` | New scene openings — composite characters into the location, then animate |
| `fl2va` | Continuity — previous clip's last frame becomes this clip's first frame |
| `t2va` | Avoid — inconsistent output; abstract shots without characters only |

---

## The QA gate

The gate reads exactly one field: `qa_verdict['verdict']`. Anything that makes
that field wrong costs a 7–25 minute re-render, so it is **normalised, never
trusted raw**. `normalise_qa_verdict.py` runs after every QA pass and is the
only writer of the field the gate reads.

**Assembly-fixable FAILs are downgraded to PASS** — when the defect lives in the
audio stem or the edit rather than the pixels, the fix is appended to
`assembly_fixes.json` and consumed as a blocking checklist during assembly,
instead of triggering a pointless re-render.

**Retries are diffed against the take they replace.** A retry that scores worse
than the original is archived and the earlier take restored — a later attempt
measurably regressed (hands 0.87 → 0.74) and was discarded.

Every clip that passes still gets its 4K60 master; the upscale step lives inside
the QA loop, so a clip exiting via an assembly-fix PASS would otherwise never get
one. `./upscale_clip.sh <run_dir> <SHOT_ID>` runs that step alone.

---

## Editorial rules — why the cuts work

Per-clip QA is not enough: the first public cut of *The Clockwork Moth* passed
every clip and was still reviewed as "signs of an inexperienced filmmaker" —
jump cuts, a dissolve on every cut, a clock that changed time across an edit,
and a narrator, child and grandmother who all sounded like one adult voice.
Each of those is now a rule with a measurable check
(`.dsh/skills/editing-grammar.md`, `.dsh/skills/voice-casting.md`,
`knowledge/minimax_h3_rules.md` §7):

| Rule | Check |
|------|-------|
| Consecutive shots differ by subject, ≥ 2 shot sizes or ≥ 30° (no jump cuts); never three of the same size in a row | `edit_check.py --shot-list` blocks the Director until fixed |
| Every clip renders ≥ 1 s of in-character **handles** at head and tail, with `edit.cut_in` / `edit.cut_out`; the dialogue stem starts inside the head handle | `validate_audio_timing.py --fix` sizes the clip and places the stem |
| Every boundary is cut inside the handles, on action or on a look; straight cuts only, no dissolves | `edit_check.py --run-dir` scores each boundary (threshold 0.80) before assembly; decisions logged to `final/edit_decisions.json` |
| Frame interpolation never crosses a cut | RIFE runs per clip; `upscale_film.py` refuses to run without `--concat-list` |
| Readable props (clock faces, counts) carry a per-scene ledger value | QA continuity fails on a contradiction |
| Voices are cast by class (child / adult / elderly / creature), narrator in a class no character uses, ≥ 4 semitones apart | Measured line-up in `audio/voices/lineup/separation.json`; `CAST_FAIL` blocks Phase 2 |

Several of these came from [OpenCineAgent](https://github.com/ProgramaGrueso/OpenCineAgent)'s
production notes (one speaker per clip, trim per clip at assembly, identity block
verbatim in every prompt, keep every take).

---

## Usage

```bash
./run.sh story.md                     # new production
./run.sh --resume                     # resume latest, skip completed steps
./run.sh --resume <run_dir>           # resume a specific run
./run.sh --resume --from-clip S02_003 # resume from a clip onward
./run.sh --resume --only-clip S01_005 # regenerate one clip
```

Optional flags: `--dry-run`, `--doctor`.

---

## Project structure

```
production-pack/
├── setup.sh                      # fresh-machine bootstrap (weights, nodes, deps)
├── run.sh                        # main pipeline
├── normalise_qa_verdict.py       # the QA gate's only writer
├── validate_audio_timing.py      # dialogue fits inside the handles, narration never overlaps
├── upscale_clip.sh               # wrapper: re-run one clip's 4K60 master
├── AGENTS.md                     # full architecture reference
├── config/
│   ├── pipeline.yml              # service endpoints, generation settings
│   └── models.yml                # model routing
├── pipeline/
│   ├── set_agent_model.py        # per-call routing (single source of truth)
│   ├── phase_config.yaml         # VRAM-phased service lifecycle
│   ├── services.sh               # phase transitions, health checks
│   ├── orchestrator.py           # production loop
│   ├── comfyui_client.py         # ComfyUI REST + WebSocket
│   ├── queue_manager.py          # clip state machine
│   ├── edit_check.py             # does every cut CUT? shot-list + boundary scoring
│   ├── upscale_clip.py           # per-clip 4K60 master (engine)
│   ├── upscale_film.py           # full-film 4K60 (bounded disk, cut-aware)
│   ├── vram_watchdog.py          # VRAM guard / LLM contention
│   ├── notify.sh                 # Discord notifications
│   └── *_server.py               # Chatterbox, Kokoro TTS
├── workflows/
│   ├── h3_ref2va_turbo.json      # primary clip graph
│   └── h3_fl2va_turbo.json       # continuity clip graph
├── .dsh/
│   ├── .agent-presets/           # 14 agent presets
│   └── skills/                   # schemas, prompt review, QA, assembly,
│                                 # editing-grammar, voice-casting
├── knowledge/
│   └── minimax_h3_rules.md       # known-artifact prevention
└── output/
    └── <run>/
        ├── shots/<ID>/
        │   ├── clip.mp4
        │   ├── clip_4k60.mp4
        │   ├── dialogue.wav
        │   ├── sfx.wav
        │   ├── qa_report.json
        │   └── reviewed_prompt.json
        ├── narration/            # Kokoro narration stems
        ├── frames/               # first/last frames + keyframes
        └── final/
            ├── movie.mp4
            ├── movie_4k60.mp4
            ├── movie.srt
            └── movie.ass
```

---

## Hardware

Tested on **RTX 5090 32 GB**, Debian 13, CUDA 13.1.

| Phase | GPU load |
|-------|----------|
| Agent reasoning (cloud routing) | none — GPU stays free |
| Image + video generation | ~22–32 GB (ComfyUI + H3 + Qwen Image) |
| Audio generation | ~8 GB (Orpheus only; other TTS engines are CPU) |
| 4K60 upscale | ~8–14 GB |

Phases are mutually exclusive. `pipeline/services.sh` reads
`pipeline/phase_config.yaml` and performs atomic transitions —
`stop-all → start-phase-services → wait-for-health` — so no two phases ever
contend for VRAM.

The upscaler holds a **12 GB free-VRAM guard**: if another process has claimed
the card it waits rather than OOMing ComfyUI mid-film.

---

## Services

| Service | Port | Unit |
|---------|------|------|
| ComfyUI | 8188 | `comfyui.service` |
| Chatterbox TTS | 9882 | `chatterbox-tts.service` |
| Kokoro TTS | 9881 | `kokoro-tts.service` |
| CosyVoice 3 | 9880 | `cosyvoice.service` |
| Orpheus 3B | 9883 | `orpheus-tts.service` |
| ninfer (local, optional) | 8080 | `ninfer.service` |
| ninfer-us (local, optional) | 8081 | `ninfer-us.service` |

`ninfer` and `ninfer-us` are mutually exclusive (`Conflicts=`) — only one can
hold the card at a time. Both are only used under `CLOUD_ROUTING=0`.

---

## Configuration reference

- `config/pipeline.yml` — endpoints, generation defaults, per-engine TTS settings
- `config/models.yml` — provider/model routing table
- `pipeline/phase_config.yaml` — per-phase service sets and VRAM budgets
- `knowledge/minimax_h3_rules.md` — known artifacts and the prompt rules that avoid them

No credentials live in the repository. Keys are read from the environment at
runtime (`OPENROUTER_API_KEY`, `OPENCODE_GO_API_KEY`) — see `key_env` entries in
`config/models.yml`.

---

## License

Copyright (c) 2026 **Vinoth Kannah MP**. Released under the
[MIT License](LICENSE) — use it, modify it, ship films with it, commercially or
not. The one condition is that the copyright notice and licence text stay with
any copy or substantial portion of the code.

If you publish work built on this pack, a credit line is appreciated:

> Built with Production Pack by Vinoth Kannah MP — https://github.com/chaoszero6/production-pack

The models and weights the pipeline drives (MiniMax H3, Qwen Image 2.1,
Orpheus 3B, Chatterbox, CosyVoice 3, Kokoro, RIFE, RTX VSR, …) are **not**
covered by this licence; check each one's own terms before commercial use.
