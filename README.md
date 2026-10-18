# Production Pack

An AI movie production pipeline that takes a story file and returns a finished,
subtitled, 4K animated short film — dialogue lip-synced, narration, score and
effects mixed once on the film timeline, assembled end to end.

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

# 3. configure the two API keys (never committed — read at runtime from the
#    environment or /root/.hermes/.env)
export ANTHROPIC_API_KEY="..."     # creative tier (story, story editor, director)
export OPENCODE_GO_API_KEY="..."   # execution + vision tiers

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

## Stopping, halting and resuming

- `./switch-off.sh` (on the server) parks every service, records the NIC MAC into
  `config/server.env`, enables Wake-on-LAN on the NIC and powers the box off through the UPS
  monitor (`upsmon -c fsd`). `./wake.sh` (from the workstation) sends the magic packet and
  waits for ssh; pass a MAC explicitly if `config/server.env` does not exist yet.

- `./stop.sh` stops the run, its agent turns, in-flight renders and the pack's watchers, and
  clears the ComfyUI queue (`--keep-monitor` leaves the idle monitor up,
  `--gpu comfyui|none` picks the GPU owner afterwards). Nothing is deleted.

- **Error halts.** There are no fallbacks and no retries: any machinery error — an agent call
  that fails or returns nothing, a QA inspector that returns no verdict, a missing key or a
  failed provider probe, a render script crash, a cloud spend cap — writes
  `output/<run>/HALT_FOR_REVIEW.json` (`{kind: "error", step, reason, preset, stderr_tail,
  resume}`), posts to Discord and exits 3. Fix the cause, then `./launch_resume.sh output/<run>`
  clears the halt and re-runs the halted step.

- **Escalation halts.** A clip that still fails QA after `MAX_RETRY` (3) takes halts the run the
  same way after posting the escalation: the file names the shot and the archived takes under
  `clips/<SID>/takes/`. Review by hand, then `touch clips/<SID>/MANUAL_PASS` to accept the best
  take or `touch clips/<SID>/RETRY_AGAIN` to render a fresh set (edit the shot list or
  `reviewed_prompt.json` first if the fix lives there), and `./launch_resume.sh output/<run>`.

Quality loops are not errors and still run: QA FAIL → reviewer rewrites → re-render; Director
edit/lint fix rounds (≤ 2 each, patch mode); audio-producer recast rounds (≤ 2); one fix round
each for the story editor, dialogue polish, narration plan, edit decisions and music cues.

## How a run works

```
 story.md
    │
    ▼
 Story Creator (Opus, REWRITES the draft)
    ▼
 Story Editor (Opus, one call: critique + revised story)
    ▼
 Dialogue Polish (Opus, lines only) ─► dialogue LOCKED
    ▼
 Director (Opus) ─► validate / edit-check / lint / locked-dialogue check (patch rounds)
    ▼
 Character Designer + Location Designer ─► Audio Producer casts voices (measured line-up)
    │
    ┌─────────── PER-CLIP LOOP ─────────────────────────────┐
    │                                                       │
    │  Screenplay Reviewer  ── rewrites prompt for the      │
    │                         model, injects negatives      │
    │           │                                           │
    │           ▼                                           │
    │  dialogue_stem.py ── stem rendered FIRST; it is the   │
    │                      lip-sync guide for H3            │
    │           │                                           │
    │           ▼                                           │
    │  generate_frames.py ── first/last frames from         │
    │                        character + location refs      │
    │           │                                           │
    │           ▼                                           │
    │  generate_clip.py ── one clip, MiniMax H3 (ComfyUI)   │
    │           │                                           │
    │           ▼                                           │
    │  QA Inspector  ── samples frames at 2 FPS, scores     │
    │                    hands / face / identity / motion   │
    │           │                                           │
    │     PASS ──┤── FAIL ──► back to Screenplay Reviewer   │
    │            │              (MAX_RETRY=3, best take)    │
    └────────────┼──────────────────────────────────────────┘
                 ▼
    4K masters (clips/<SID>/clip_4k.mp4 at PP_TARGET_FPS, RTX VSR)
                 ▼
    3a narration plan ─► 3b edit check ─► 3c edit decisions (editor writes ONLY
    final/edit_decisions.json) ─► 3d timeline ─► 3e music cues (composer plans)
                 ▼
    3f one GPU excursion: narration (Kokoro) → music (ACE-Step 1.5) → SFX (MMAudio)
                 ▼
    3g assemble.py: picture trimmed per decisions + ONE audio mix on the film
       timeline ─► subtitles.py ─► final/movie_4k.mp4 + movie.srt + movie.ass
```

Before the loop starts, the Director's shot list is validated **as an edit**
(`pipeline/edit_check.py --shot-list`): no two consecutive shots of the same
character at the same size and angle, ≥ 1 s handles on every clip, cut points
written, a reaction and an insert per dialogue scene. `pipeline/story_check.py`
confirms every locked dialogue line survived into the shot list and dialogue
carries ≥ 55 % of the spoken words. The Audio Producer casts voices by voice
class and must pass a measured line-up before any clip renders.

### Model routing

Every agent runs in the cloud; there is no local LLM. Every agent call rewrites
the model route in `~/.dsh/settings.yaml` immediately before it runs, so routing
is per-call. The single source of truth is `pipeline/set_agent_model.py`
(`config/models.yml` documents it) — `run.sh` and `pipeline/generate_clip.py`
both call it, so a subprocess can never inherit the previous call's route.

| Tier | Presets | Default | Override |
|------|---------|---------|----------|
| Creative | story-creator, story-editor, director | `anthropic` / `claude-opus-5-5`, effort high | `DSH_CREATIVE_PROVIDER` / `DSH_CREATIVE_MODEL` / `DSH_CREATIVE_EFFORT` |
| Vision (QA) | qa-inspector | `anthropic` / `claude-haiku-5-5` | `DSH_VISION_PROVIDER` / `DSH_VISION_MODEL` / `DSH_VISION_EFFORT` |
| Execution | everything else | `opencode-go-deepseek` / `deepseek-v4.1-flash` | `DSH_EXECUTION_PROVIDER` / `DSH_EXECUTION_MODEL` / `DSH_EXECUTION_EFFORT` |

A provider override must name the model. `DSH_FORCE_TIER=execution` routes one
call onto the execution tier (the Director's mechanical per-scene gate uses it).
A routing error (exit 2) or a provider error (exit 3 — missing key, failed
`/models` probe) halts the run; step 0 probes every provider before anything
else. Keys come from the environment or `/root/.hermes/.env`; providers are
registered for dsh in `/root/.dsh/profiles/headless/cordis.patch.yml`
(`anthropic` is built in; the OpenCode Go entries are openai-completions
providers on `https://opencode.ai/zen/go/v1`). `pipeline/cloud_ledger.py` keeps
`output/<run>/cloud_usage.json` and halts at the caps
`CLOUD_MAX_CALLS_CREATIVE` (16), `CLOUD_MAX_USD_EST_CREATIVE` (9),
`CLOUD_MAX_CALLS_VISION` (600), `CLOUD_MAX_USD_EST_VISION` (4), `CLOUD_MAX_CALLS_EXECUTION` (1500), with a Discord note at 50 % and 80 %.

The GPU has two owners — **ComfyUI** (resting owner) and the four-engine **TTS
cluster** — enforced by systemd `Conflicts=` installed from `pipeline/systemd/`.
`pipeline/gpu_owner.sh comfyui|tts|none|status` switches owner and waits for its
API. Agents never call ComfyUI or a TTS port during a turn: they write job files,
and the pipeline renders them (`generate_frames.py`, `dialogue_stem.py`,
`generate_clip.py`, `tts_render.py`, `music_render.py`, `sfx_render.py`,
`assemble.py`).

> Capability labels are not evidence. `deepseek-v4-pro` advertises image input
> and then rejects it with `404 No endpoints found that support image input`.
> Only `qa-inspector` needs vision, and it is routed to a model that actually
> accepts images. Probe before trusting any declared `input:` list.

---

## Tech stack

| Component | Tool | Purpose |
|-----------|------|---------|
| Agent framework | dsh presets (`.dsh/.agent-presets/`) | 16 specialised agent presets, all cloud-routed |
| Creative writing | Claude Opus 5.5 (Anthropic) | Story, story editor, director |
| Execution | DeepSeek v4.1 Flash via OpenCode Go | Prompts, plans, decisions |
| QA vision | Claude Haiku 5.5 (Anthropic) | Per-frame scoring; ~$0.005 per pass |
| Image generation | Qwen Image 2.1 via ComfyUI | Character/location refs, frames, compositing |
| Video generation | MiniMax H3 via ComfyUI | `ref2va` / `i2va` / `fl2va` clips (3–15 s) |
| Dialogue TTS | Chatterbox, CosyVoice 3 | Zero-shot voice cloning, primary dialogue |
| Emotional TTS | Orpheus 3B | Crying, whispering, shouting |
| Narration TTS | Kokoro | Narrator voice, placed by assembly (no lip sync) |
| QA vision | Claude Haiku 5.5 (Anthropic, ~$0.005 per pass) | Per-frame scoring of hands, face, identity |
| Music | ACE-Step 1.5 via ComfyUI (`music_render.py`) | Score cues and ambience beds from the composer's cue sheet |
| SFX | MMAudio via ComfyUI (`sfx_render.py`) | Per-clip foley synced to the picture |
| Spatial upscale | NVIDIA RTX Video Super Resolution | 4K ULTRA per chunk → `clip_4k.mp4` |
| Temporal upscale (optional) | RIFE (rife47.pth) | Only when `PP_TARGET_FPS` > source rate (default 24 = no RIFE) |
| Assembly | `pipeline/assemble.py` | Picture from the masters + one audio mix on the film timeline |
| Workflow engine | ComfyUI | All image/video/audio/upscale graphs |
| Notifications | `pipeline/notify.sh` | Discord updates |

### Dialogue vs narration

| Type | Generated | Lip sync | How |
|------|-----------|----------|-----|
| Dialogue | **before** video | yes | TTS stem → H3 lip-sync guide (`<Audio 1>` + `<d>` tags). The clip's own audio is never used; `assemble.py` places `clips/<SID>/dialogue.wav` once at `anchor_s` on the film timeline |
| Narration | **after** all clips | no | Planned as one block per passage (`narration_meta.json`), rendered by Kokoro, placed once by `assemble.py` at the first clip of its span + 0.4 s; music, beds and SFX duck underneath; never over a line |

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
`assembly_fixes.json` and consumed as a blocking checklist by the editor,
instead of triggering a pointless re-render.

**Retries are diffed against the take they replace.** A retry that scores worse
than the original is archived and the earlier take restored — a later attempt
measurably regressed (hands 0.87 → 0.74) and was discarded.

Every approved clip gets its 4K master (`clips/<SID>/clip_4k.mp4`, built by
`pipeline/upscale_clip.py` with RTX VSR at `PP_TARGET_FPS`, default 24 = the
source rate with no interpolation; 48/96 = RIFE xN, 60 = RIFE x5 then every
2nd frame). There is no CPU fallback and no low-res copy — a failed master halts
the run. `./upscale_clip.sh <run_dir> <SHOT_ID>` re-runs one master.

---

## Editorial rules — why the cuts work

Per-clip QA is not enough: the first public cut of *The Clockwork Moth* passed
every clip and was still reviewed as "signs of an inexperienced filmmaker" —
jump cuts, a dissolve on every cut, a clock that changed time across an edit,
and a narrator, child and grandmother who all sounded like one adult voice.
Each of those is now a rule with a measurable check
(`.dsh/skills/editing-grammar.md`, `.dsh/skills/voice-casting.md`,
`.dsh/skills/final-assembly.md`, `knowledge/minimax_h3_rules.md` §7):

| Rule | Check |
|------|-------|
| Consecutive shots differ by subject, ≥ 2 shot sizes or ≥ 30° (no jump cuts); never three of the same size in a row | `edit_check.py --shot-list` blocks the Director until fixed |
| Every clip renders ≥ 1 s of in-character **handles** at head and tail, with `edit.cut_in` / `edit.cut_out`; the dialogue stem starts inside the head handle | `validate_audio_timing.py --fix` sizes the clip and places the stem |
| Every boundary is cut inside the handles, on action or on a look; straight cuts only, no dissolves | `edit_check.py --run-dir` scores each boundary (threshold 0.80); the editor's decisions in `final/edit_decisions.json` (v2) are validated and executed by `assemble.py` |
| Frame interpolation never crosses a cut | Upscale (and any RIFE) runs per clip; `upscale_film.py` refuses to run without `--concat-list` |
| Readable props (clock faces, counts) carry a per-scene ledger value | QA continuity fails on a contradiction |
| Voices are cast by class (child / adult / elderly / creature), narrator in a class no character uses, ≥ 4 semitones apart | Measured line-up in `audio/voices/lineup/separation.json`; `CAST_FAIL` blocks Phase 2 |
| Clip audio never reaches the film: no audio jumps at cuts, no double voice-over, J/L cuts possible | `assemble.py` places every stem once on the timeline, narration once per block, beds and SFX crossfade across cuts; J-cut = `audio_lead_s`, L-cut = `audio_lag_s`; voices never overlap |

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

Optional flags: `--dry-run`, `--doctor`. Useful env: `PP_TARGET_FPS` (24),
`SFX_LAYER` (1), `KEEP_NATIVE_AMBIENCE` (0), `MIN_DIALOGUE_SHARE_BLOCK` (0),
`FRAME_ENGINE` (qwen | ideogram4), the `DSH_*` routing overrides and the
`CLOUD_MAX_*` caps.

---

## Project structure

```
production-pack/
├── setup.sh                      # fresh-machine bootstrap (weights, nodes, deps)
├── run.sh                        # main pipeline (halt_run on any error)
├── launch_resume.sh              # detached resume of a halted run
├── stop.sh                       # stop a run and its watchers cleanly
├── normalise_qa_verdict.py       # the QA gate's only writer
├── validate_audio_timing.py      # dialogue fits inside the handles, narration never overlaps
├── upscale_clip.sh               # wrapper: re-run one clip's 4K master
├── AGENTS.md                     # full architecture reference
├── config/
│   ├── pipeline.yml              # service endpoints, generation settings
│   └── models.yml                # the three cloud tiers (documentation of set_agent_model.py)
├── pipeline/
│   ├── set_agent_model.py        # per-call routing (single source of truth)
│   ├── cloud_ledger.py           # cloud_usage.json, spend caps
│   ├── gpu_owner.sh              # ComfyUI | TTS cluster hand-over
│   ├── story_check.py            # story + shot-list dialogue checks (locked lines, share)
│   ├── dialogue_patch.py         # applies the dialogue-polish patch
│   ├── shot_list_patch.py        # Director fix rounds in patch mode
│   ├── edit_check.py             # does every cut CUT? shot-list + boundary scoring
│   ├── dialogue_stem.py          # per-clip dialogue stem (lip-sync guide)
│   ├── generate_frames.py / generate_clip.py   # keyframes, H3 clips
│   ├── upscale_clip.py           # per-clip 4K master (RTX VSR, optional RIFE)
│   ├── upscale_film.py           # full-film pass (cut-aware; needs --concat-list)
│   ├── narration_check.py        # one block per passage, never per shot
│   ├── tts_render.py             # renders tts_jobs.json on the TTS cluster
│   ├── music_render.py           # validates the cue sheet, renders ACE-Step 1.5
│   ├── sfx_render.py             # MMAudio foley per clip
│   ├── speech_detect.py          # is there speech in a clip's native track?
│   ├── assemble.py               # timeline, picture, ONE audio mix → movie_4k.mp4
│   ├── subtitles.py              # SRT/ASS from final/audio_placements.json
│   ├── mux_stem.py               # preview/QA only — assembly never reads this track
│   ├── systemd/                  # units + Conflicts= drop-ins (install_units.sh)
│   └── notify.sh                 # Discord notifications
├── workflows/
│   ├── h3_ref2va_turbo.json      # primary clip graph
│   └── h3_fl2va_turbo.json       # continuity clip graph
├── .dsh/
│   ├── .agent-presets/           # 16 agent presets
│   └── skills/                   # schemas, story-craft, prompt review, QA, final-assembly,
│                                 # editing-grammar, voice-casting, music-cues
├── knowledge/
│   └── minimax_h3_rules.md       # known-artifact prevention
└── output/
    └── <run>/
        ├── story.json, shot_list.json, cloud_usage.json, HALT_FOR_REVIEW.json
        ├── clips/<ID>/
        │   ├── clip.mp4
        │   ├── clip_4k.mp4
        │   ├── dialogue.wav + dialogue_meta.json
        │   ├── sfx.wav
        │   ├── qa_report.json
        │   └── reviewed_prompt.json
        ├── audio/narration/      # narration_meta.json, tts_jobs.json, block WAVs
        ├── music/                # music_cues.json, cues/, beds/
        ├── frames/               # first/last frames + keyframes
        └── final/
            ├── edit_decisions.json
            ├── timeline.json
            ├── audio_placements.json
            ├── assembly_log.json
            ├── movie_4k.mp4
            ├── movie.srt
            └── movie.ass
```

---

## Hardware

Tested on **RTX 5090 32 GB**, Debian 13, CUDA 13.1.

| Phase | GPU load |
|-------|----------|
| Agent reasoning (cloud) | none — GPU stays free |
| Image + video generation | ~22–32 GB (ComfyUI + H3 + Qwen Image) |
| Audio generation | ~8 GB (TTS cluster) |
| 4K upscale, music, SFX | ~8–14 GB (ComfyUI) |

The GPU alternates between ComfyUI and the TTS cluster (`pipeline/gpu_owner.sh`,
systemd `Conflicts=`); no two owners ever contend for VRAM.

The upscaler holds a **12 GB free-VRAM guard**: if another process has claimed
the card it waits rather than OOMing ComfyUI mid-film.

---

## Services

| Service | Port | Unit |
|---------|------|------|
| ComfyUI | 8188 | `comfyui.service` |
| TTS cluster (all four engines) | — | `tts-cluster.service` |
| Chatterbox TTS | 9882 | `chatterbox-tts.service` |
| Kokoro TTS | 9881 | `kokoro-tts.service` |
| CosyVoice 3 | 9880 | `cosyvoice.service` |
| Orpheus 3B | 9883 | `orpheus-tts.service` |
| Idle monitor | — | `tts-idle-monitor.service` (`idle_monitor.py` only) |

`comfyui.service` and `tts-cluster.service` are mutually exclusive
(`Conflicts=`) — only one can hold the card at a time. No LLM unit is part of
the pipeline.

---

## Configuration reference

- `config/pipeline.yml` — endpoints, generation defaults, per-engine TTS settings
- `config/models.yml` — the three cloud tiers and two providers (documentation; the code is `pipeline/set_agent_model.py`)
- `knowledge/minimax_h3_rules.md` — known artifacts and the prompt rules that avoid them

No credentials live in the repository. Keys are read at runtime from the
environment or `/root/.hermes/.env` (`ANTHROPIC_API_KEY`, `OPENCODE_GO_API_KEY`).

---

## License

Copyright (c) 2026 **Vinoth Kannah MP**. Released under the
[MIT License](LICENSE) — use it, modify it, ship films with it, commercially or
not. The one condition is that the copyright notice and licence text stay with
any copy or substantial portion of the code.

If you publish work built on this pack, a credit line is appreciated:

> Built with Production Pack by Vinoth Kannah MP — https://github.com/chaoszero6/production-pack

The models and weights the pipeline drives (MiniMax H3, Qwen Image 2.1,
Orpheus 3B, Chatterbox, CosyVoice 3, Kokoro, ACE-Step 1.5, MMAudio, RIFE,
RTX VSR, …) are **not** covered by this licence; check each one's own terms
before commercial use.
