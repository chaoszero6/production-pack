# Production Pack - AI Movie Production Pipeline

> Pixar-style fantasy movie production using dsh agent orchestration

## Architecture Overview

This production pack orchestrates 16 specialized agent presets to produce a complete animated movie
with consistent characters, continuity, dialogues, narration, BGM, SFX and subtitles.
Each agent is a dsh preset in `.dsh/.agent-presets/` with skills in `.dsh/skills/`.
Every agent runs in the cloud (three tiers — see "Model Routing"); the GPU on 207 is used only
by ComfyUI and the TTS cluster. Agents reason and write files; the pipeline renders.

## Pipeline Flow

```
═══════════════════════════════════════════════════════════════
 PHASE 1 — PRE-PRODUCTION  [cloud]
═══════════════════════════════════════════════════════════════

  0   Cloud preflight (set_agent_model.py --probe-all; a failed probe halts)
  1a  Story Creator      [creative: anthropic/claude-opus-5-5]  REWRITES the draft
  1a-edit   Story Editor [creative]  one call: critique scores + revised story
  1a-dialogue Dialogue Polish [creative: claude-opus-5-5] lines only → LOCKED
  1b  Director           [creative]  shot list; locked dialogue copied verbatim
  1b-validate / 1b-edit / 1b-lint   fix rounds ≤ 2 each, PATCH mode
  1b-dialogue  story_check.py --story --shots (every locked line present, share ≥ 55 %)
  1c/1d Character Designer + Location Designer [execution] → sheets rendered on ComfyUI
  1e  Audio Producer [execution] casts → tts_render.py + voice_lineup.py (TTS owns GPU)
  (Storyboard Artist is available; run.sh does not call it)

═══════════════════════════════════════════════════════════════
 PHASE 2 — PER-CLIP LOOP
═══════════════════════════════════════════════════════════════

  ┌─ A: REASONING [execution tier, cloud] ─────────────────────┐
  │  Screenplay Reviewer (builds H3-format prompt)             │
  │       ↓                                                    │
  │  Has Dialogue? ──YES──→ dialogue_stem.py renders the stem  │
  │       │                  (TTS owns GPU) → <Audio 1> guide  │
  │       NO → skip (narration is placed in post, no lip sync) │
  ├────────────────────────────────────────────────────────────┤
  │ B: GENERATION [ComfyUI on the local RTX 5090]              │
  │  generate_frames.py → keyframes (Qwen Image 2.1 / Ideogram)│
  │       ↓                                                    │
  │  generate_clip.py → clip (MiniMax H3)                      │
  │       • Dialogue: stem as lip-sync guide + <d> tags        │
  │       • Narration: NO <Audio>, NO <d> = no lip movement    │
  │       • the clip's own audio track never reaches the film  │
  ├────────────────────────────────────────────────────────────┤
  │ C: QA [vision tier: anthropic/claude-haiku-5-5]        │
  │  QA Inspector → analyzes frames → normalise_qa_verdict.py  │
  │       → PASS → next clip                                   │
  │       → FAIL → back to A (reviewer rewrites; MAX_RETRY=3)  │
  └────────────────────────────────────────────────────────────┘
  2g  4K masters: upscale_clip.py → clips/<SID>/clip_4k.mp4 (RTX VSR, PP_TARGET_FPS)

═══════════════════════════════════════════════════════════════
 PHASE 3 — POST-PRODUCTION  (deterministic; agents only PLAN)
═══════════════════════════════════════════════════════════════

  3a  Audio Producer → narration plan (tts_jobs.json + narration_meta.json,
      one block per passage) · narration_check.py --plan
  3b  edit_check.py --run-dir → final/edit_report.json
  3c  Post-Production Editor → final/edit_decisions.json v2 ONLY
      (trims, inserts, J/L cuts as audio_lead_s / audio_lag_s) · assemble.py --validate-decisions
  3d  assemble.py --timeline-only → final/timeline.json
  3e  Music Composer → music/music_cues.json · music_render.py --validate
  3f  ONE GPU excursion: tts_render.py (narration) → music_render.py (ACE-Step 1.5)
      → sfx_render.py (MMAudio per clip, SFX_LAYER=1)
  3g  assemble.py: picture from the masters trimmed per decisions + ONE audio mix on the
      film timeline → final/movie_4k.mp4 · subtitles.py --embed → movie.srt / movie.ass
      · edit_check.py --concat-list
```

### Model Routing (three cloud tiers, no local LLM)

Decision of 2026-10-18: **there is no local LLM.** Every agent runs in the cloud. The route is
resolved from the `agent-default-model` section of `/root/.dsh/settings.yaml` at dsh launch, and
**`pipeline/set_agent_model.py`** (the ONLY routing table) rewrites it before every agent call, so
a subprocess can never inherit the previous call's route. `config/models.yml` documents the same
table; the `engine:` plugin block in presets never worked and has been removed everywhere.

| Tier | Presets | Provider / model | Effort |
|------|---------|------------------|--------|
| `creative` | story-creator, story-editor, director | `anthropic` / `claude-opus-5-5` | high |
| `vision` | qa-inspector (the ONLY agent that reads images) | `anthropic` / `claude-haiku-5-5` | high |
| `execution` | everything else | `opencode-go-deepseek` / `deepseek-v4.1-flash` | high |

- **Env overrides** (the only way to change a route): `DSH_CREATIVE_PROVIDER/MODEL/EFFORT`,
  `DSH_VISION_PROVIDER/MODEL/EFFORT`, `DSH_EXECUTION_PROVIDER/MODEL/EFFORT`; a provider override
  must name the model. `DSH_FORCE_TIER=execution` routes ONE call onto the execution tier — run.sh
  uses it for the Director's mechanical per-scene gate so a lint fix never spends Opus.
  `DSH_SETTINGS` / `DSH_ENV_FILE` relocate the settings and key files.
- **Keys** live in `/root/.hermes/.env` (`ANTHROPIC_API_KEY`, `OPENCODE_GO_API_KEY`) or the
  environment. **Providers are registered for dsh** in `/root/.dsh/profiles/headless/cordis.patch.yml`
  under `llm-pi-ai → providers`: `anthropic` is a built-in pi-ai provider (api anthropic-messages);
  the OpenCode Go entries are custom openai-completions providers with base URL
  `https://opencode.ai/zen/go/v1`. A model must be in `KNOWN_MODELS` AND in cordis.patch.yml.
- **No fallbacks, no retries.** `set_agent_model.py` exits 2 on a routing error (unknown tier /
  provider / model, bad env) and 3 on a provider error (missing key, failed `/models` probe);
  run.sh turns both into `halt_run`. Step 0 runs `--probe-all` before anything else. Tools:
  `--list`, `--dry-run-all`, `<preset> --show`.
- **Cloud spend ledger:** `pipeline/cloud_ledger.py` brackets every agent launch and writes
  `$RUN_DIR/cloud_usage.json`. Caps `CLOUD_MAX_CALLS_CREATIVE` (16), `CLOUD_MAX_USD_EST_CREATIVE`
  (9), `CLOUD_MAX_CALLS_VISION` (600), `CLOUD_MAX_USD_EST_VISION` (4), `CLOUD_MAX_CALLS_EXECUTION` (1500); reaching a cap halts the run; Discord note at 50 % / 80 %.
- Opus 5.5: thinking is always on; effort is the only depth control. Check the run log for
  `ROUTE preset=<p> provider=<prov> model=<m> effort=<e> tier=<t>`.

### Halt on error — `HALT_FOR_REVIEW.json`

Any machinery error — an agent call that fails or returns nothing, a QA inspector that returns no
verdict, a key/probe failure, a render script crash, a spend cap — calls `halt_run` in run.sh:
it writes `$RUN_DIR/HALT_FOR_REVIEW.json` (`{kind: "error", step, reason, preset, stderr_tail,
resume}`), posts to Discord and exits 3. Nothing reroutes, nothing retries silently.
`./launch_resume.sh <run_dir>` clears an error halt and re-runs the halted step.

Quality loops are not errors and still run: QA FAIL → reviewer rewrites → re-render (`MAX_RETRY=3`,
`best_take.py`, then the MANUAL_PASS / RETRY_AGAIN escalation halt); Director edit/lint fix rounds
(≤ 2 each, PATCH mode via `pipeline/shot_list_patch.py`); audio-producer recast rounds (≤ 2); one fix
round each for story-editor, dialogue-polish, the narration plan, the edit decisions and the music cues.

### The GPU has TWO owners: ComfyUI | TTS cluster

With no local LLM, the RTX 5090 alternates between **ComfyUI** (the resting owner — Qwen Image,
MiniMax H3, RTX VSR, ACE-Step, MMAudio) and the **TTS cluster** (`tts-cluster.service`: Orpheus
backend+API, Chatterbox, CosyVoice, Kokoro). The exclusion is enforced in **systemd**
(`pipeline/systemd/install_units.sh` installs the units and `Conflicts=` drop-ins), so starting one
owner stops the other.

- `pipeline/gpu_owner.sh comfyui|tts|none|status` switches owner and **waits for the new owner's
  API** (ComfyUI ≤ 180 s; TTS ≤ 240 s). The `llm` verb is gone; retired LLM units (Strata,
  OrcaRouter, ninfer, …) are stopped before every hand-over so a stale one can never hold VRAM.
- **An agent turn never calls ComfyUI or a TTS port.** Agents write job files (`frame_spec.json`,
  `tts_jobs.json`, `reviewed_prompt.json`, `narration_meta.json`, `edit_decisions.json`,
  `music_cues.json`) and the pipeline renders them: `generate_frames.py`, `tts_render.py` +
  `voice_lineup.py`, `dialogue_stem.py`, `generate_clip.py`, `upscale_clip.py`, `music_render.py`,
  `sfx_render.py`, `assemble.py`. `--keep-gpu` chains render steps without a hand-over.
- `tts-idle-monitor.service` runs only `pipeline/idle_monitor.py` (stops a TTS engine or ComfyUI that
  is active-but-dead during its phase). There is no LLM monitor and no LLM restart.

Vision: only `qa-inspector` reads images (frames at 2 FPS, scored per frame). Capability labels
lie — probe before trusting a declared `input:` list; `deepseek-v4-pro` hard-rejects images
(HTTP 404), so it must never be given a vision job.

## Agents

### 1. Story Creator
**Preset:** `.dsh/.agent-presets/story-creator/`  (creative tier, Opus, no tools)
- **Rewrites the user's draft** into a complete story — scenes, dialogue, emotional arcs
- story-schema and story-craft skills are inlined by run.sh; outputs structured JSON
- Specializes in Pixar-style storytelling (heart, humor, stakes)

### 2. Story Editor
**Preset:** `.dsh/.agent-presets/story-editor/`  (creative tier, Opus, no tools)
- Step 1a-edit, ONE call: scored critique against story-craft (emotional_connect, scene_turns,
  dialogue_subtext, stakes, setups/payoffs, theme, show-don't-tell, dialogue_share) **plus** the
  fully revised story with every rewrite applied
- Writes `story_review.json`; the revised story becomes `story.json` (previous kept as `story_v1.json`)
- One fix round if `story_check.py` still fails

### 3. Dialogue Polish
**Preset:** `.dsh/.agent-presets/dialogue-polish/`  (creative tier, Claude Opus 5.5, no tools)
- Step 1a-dialogue: rewrites ONLY the spoken lines (subtext, no plot explanation, verbal
  signatures, ≤ 130 characters / 30 words, one speaker per beat)
- `pipeline/dialogue_patch.py` applies the patch and writes `story_dialogue_diff.txt`;
  **dialogue is LOCKED afterwards** — the Director copies it verbatim

### 4. Director
**Preset:** `.dsh/.agent-presets/director/`  (creative tier, Opus)
- Breaks story into numbered shots with camera angles; story + skills inlined
- Locked dialogue copied verbatim; per shot `pov_character`, `audience_feels`,
  `audio.music_cue {action, mood, intensity}`; the scene's `linger` becomes a held reaction shot
- Keyframe requirements, handles, cut points, readable props, audio requirements
- Fix rounds (1b-validate / 1b-edit / 1b-lint, ≤ 2 each) are PATCH mode via
  `pipeline/shot_list_patch.py`; `story_check.py --story --shots` then verifies every locked line
  is present and dialogue share ≥ 55 % (missing lines halt; `MIN_DIALOGUE_SHARE_BLOCK=1` halts on
  share). The per-scene gate runs this preset on the execution tier (`DSH_FORCE_TIER=execution`)

### 5. Character Designer
**Preset:** `.dsh/.agent-presets/character-designer/`
- Plans multi-angle character reference sheets (front/side/back/3-quarter); rendered via
  Qwen Image 2.1 on ComfyUI by the pipeline
- Outputs reference images for identity-locking in video generation

### 6. Location Designer
**Preset:** `.dsh/.agent-presets/location-designer/`
- Plans environment/location reference images, day/night/weather variants
- Wide establishing shots and detail textures in the Pixar aesthetic

### 7. Storyboard Artist
**Preset:** `.dsh/.agent-presets/storyboard-artist/`
- Visual storyboard panels from the shot list (composition, action, camera moves, timing)
- Available as a preset; not called by run.sh

### 8. Screenplay Reviewer
**Preset:** `.dsh/.agent-presets/screenplay-reviewer/`
- Reviews all prompts before they reach MiniMax H3; applies the known-artifact knowledge base
- Structures prompts with temporal decomposition, handles and boundary states; ≤ 900 words
- On a QA FAIL rewrites from the shot list; `non_diegetic_music` is always "none — the score is
  added in post"

### 9. Image Generator
**Preset:** `.dsh/.agent-presets/image-generator/`
- Writes `frame_spec.json` (first/last frames, compositing, references); `generate_frames.py`
  renders it on ComfyUI (Qwen Image 2.1, or Ideogram 4 + Qwen identity pass with `FRAME_ENGINE=ideogram4`)

### 10. Audio Producer
**Preset:** `.dsh/.agent-presets/audio-producer/`
- Casts by voice class (voice-casting skill) → `audio/voices/voice_config.json`; measured line-up,
  `CAST_FAIL` blocks Phase 2; ≤ 2 recast rounds
- 4 TTS engines: Chatterbox (primary dialogue), CosyVoice 3 (multilingual / instruction emotion),
  Orpheus 3B (intense emotion), Kokoro (narration only)
- Per-clip dialogue stems are rendered by `pipeline/dialogue_stem.py` BEFORE video (lip-sync guide)
- Step 3a: the **narration plan** — `audio/narration/narration_meta.json` (canonical: one block per
  passage, `span_clips`, `starts_on = span_clips[0]`, never per shot) + `tts_jobs.json`;
  `narration_check.py --plan` validates; `tts_render.py` renders; `assemble.py` places each block once

### 11. Music Composer
**Preset:** `.dsh/.agent-presets/music-composer/`
- PLAN ONLY: writes `music/music_cues.json` (music-cues skill) — cues anchored to shot IDs on
  `final/timeline.json`, motifs, intensity, transitions, ambience beds, deliberate silence
- `music_render.py --validate` → `music_cues.resolved.json` (one fix round); `music_render.py`
  renders every cue and bed with **ACE-Step 1.5** on ComfyUI → `music/cues/<id>.wav`, `music/beds/`
- SFX are not the composer's: `sfx_render.py` renders **MMAudio** foley per clip from the shot's
  `audio.sfx` → `clips/<SID>/sfx.wav`

### 12. Video Generator
**Preset:** `.dsh/.agent-presets/video-generator/`
- MiniMax H3 via ComfyUI — ref2va / i2va / fl2va; mode comes from the shot list
- Dialogue: the approved stem is the lip-sync guide (`MiniMaxH3AddGuide` / `<Audio 1>` + `<d>`);
  narration: no `<Audio>`, no `<d>`, no lip movement
- ⚠ The clip's own audio track (H3's native take — measured r≈0.07 against the stem on S01_004 —
  or the stem muxed back for previews) is **never used in the film**: `assemble.py` places
  `clips/<SID>/dialogue.wav` at `anchor_s` on the film timeline

### 13. QA Inspector
**Preset:** `.dsh/.agent-presets/qa-inspector/`  (vision tier)
- Reviews each generated clip frame by frame: hands, face, duplication, motion, texture,
  identity drift, continuity, edit pair, audience_feels on the POV face
- Issues PASS/FAIL with `re_render_recommended` / `recommended_action` — normalised by
  `normalise_qa_verdict.py`, see "QA Gate & Retry Policy"
- No verdict at all is a machinery error → halt

### 14. Pipeline Orchestrator
**Preset:** `.dsh/.agent-presets/pipeline-orchestrator/`
- Service lifecycle reference (ComfyUI, TTS cluster, VRAM); the running pipeline uses
  `gpu_owner.sh` and systemd directly

### 15. Subtitle Generator — DEPRECATED (2026-10-18)
**Preset:** `.dsh/.agent-presets/subtitle-generator/`
- Not called by run.sh. Subtitles are generated by `pipeline/subtitles.py --embed` from
  `final/audio_placements.json` (actual placement times) after assembly → `movie.srt` / `movie.ass`

### 16. Post-Production Editor
**Preset:** `.dsh/.agent-presets/post-production-editor/`
- **Decides only; `assemble.py` executes.** Reads `assembly_fixes.json` (blocking checklist) and
  `final/edit_report.json`, writes ONLY `final/edit_decisions.json` v2:
  `{"version":2,"decisions":[{boundary,action,a_out_s,b_in_s,audio_lead_s,audio_lag_s,cut_type,insert_clip,note}],"removed_clips":[],"inserted":[]}`
- J-cut = `audio_lead_s` 0.15–0.30 s into a dialogue clip (`b_in_s = anchor_s + audio_lead_s`);
  L-cut = `audio_lag_s` 0.20–0.40 s; voices never overlap; straight cuts only
- Fix order: trim deeper → insert reaction/insert clip → request re-render; never ffmpeg, never
  touches clip or audio files; `assemble.py --validate-decisions` (one fix round)

## QA Gate & Retry Policy

The per-clip gate is `run.sh` reading **`$CLIP_DIR/qa_verdict.json['verdict']`** — PASS advances to the
next clip, FAIL loops back through prompt review into a fresh H3 render (~7–25 min each). Anything that
makes that one field wrong costs real GPU hours, so it is normalised, never trusted raw.

### The verdict is normalised, never trusted raw
qa-inspector writes its rich report to `clips/<SHOT_ID>/qa_report.json` **or** `qa/<SHOT_ID>/qa_report.json`
— it uses both, so read both and take the newest — and it sometimes writes `qa_verdict.json` as prose.
`normalise_qa_verdict.py` runs after every QA pass and is the ONLY writer of the field the gate reads.
It resolves the verdict from the richest source available, applies the **calibrated gate** first
(structural categories ≥ 0.70 and overall ≥ 0.75 → PASS, soft categories → assembly notes; see
"H3 Envelope & QA Calibration"), then the assembly-fixable downgrade below.

### Assembly-fixable FAIL → PASS (do NOT re-render)
A FAIL whose defect lives in the audio stem or the edit — not in the pixels — is downgraded to `PASS`
with `assembly_fix_required: true`, and its fixes are appended to `<run_dir>/assembly_fixes.json`.
Phase 3 consumes that file as a BLOCKING checklist (the editor turns every entry into a decision).
The `audio_fix_target` is the stem itself — `clips/<sid>/dialogue.wav` + `dialogue_meta.json`
(`stem_offset_s`); assembly never uses clip audio, so nothing is ever re-muxed.

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

### Every approved clip needs its 4K master
Phase 2g builds `clips/<SID>/clip_4k.mp4` for every approved clip with `pipeline/upscale_clip.py`
(RTX VSR on ComfyUI). The delivery frame rate is `PP_TARGET_FPS` (default 24 = the source rate,
**no RIFE**); 48/96 = RIFE xN, 60 = RIFE x5 then every 2nd frame. There is no CPU fallback and no
low-res copy — a failed master halts the run. `./upscale_clip.sh <run_dir> <SHOT_ID>` re-runs one
master (it must export `DSH_PERMISSION_MODE=danger-full-access` exactly as run.sh does — dsh confines
writes to the harness workspace and headless has no approval channel). `assemble.py` refuses a clip
without a PASS verdict or without its master.

## Editorial Rules — *The Clockwork Moth* postmortem (2026-10-07)

The first public cut was reviewed as the work of "an inexperienced filmmaker": same voice for
narrator, child and grandmother; a clock that changed time across a cut; a small dissolve on every
cut; shots that "don't cut" (jump cuts). All 102 clips had passed per-clip QA — the defects were
**between** clips and **between** voices, which nothing measured. Fixes live in
`.dsh/skills/editing-grammar.md`, `.dsh/skills/voice-casting.md`, `knowledge/minimax_h3_rules.md §7`
and two tools; the rules that must never regress:

| Complaint | Root cause | Rule / tool |
|-----------|-----------|-------------|
| Dissolve on every cut | `upscale_film.py` ran RIFE over the ASSEMBLED film, blending shot A's last frame into shot B's first | Interpolate per clip only (and only when `PP_TARGET_FPS` asks for more than the source rate). `upscale_film.py` **refuses to run without a cut list** (`--concat-list final/concat.txt`); it holds each shot's last frame and decimates with a global parity so there is no drift |
| Jump cuts | Shot list had 9 consecutive pairs with same character + same size + same angle; dialogue clips were sized to the stem (0.4 s / 0.6 s) so there were no handles to cut into | Director: consecutive shots differ by subject, ≥ 2 shot sizes or ≥ 30°; every clip carries ≥ 1 s handles with `edit.cut_in` / `edit.cut_out`; reaction + insert per dialogue scene. `pipeline/edit_check.py` scores every boundary (threshold 0.80) before assembly and inside QA (`edit_pair`) |
| Clock time changes across the cut | No ledger for readable props | `readable_props` per scene in the story/shot list, phrase copied verbatim into every prompt, QA continuity category 9j fails on contradiction |
| Same voice for everyone | Narrator, boy, grandmother and moth were all Orpheus adult-female voices (`tara/jess/leah/zoe`) within ~3 semitones | Cast by **voice class**; narrator in a class no character uses; measured line-up with ≥ 4 semitone separation (`audio/voices/lineup/separation.json`, `CAST_FAIL` blocks Phase 2); child/elderly via Chatterbox clone or CosyVoice 3 instruction, never adult + affectation |
| Audio jumps at cuts / double voice-over / no J-L cuts | Clips arrived in different audio states (native H3 take, stem muxed twice, narration rendered per clip) and were concatenated with their audio | Clip audio never reaches the film; `assemble.py` places every stem once on the timeline (`film_start + anchor_s − src_in`), narration once per block, beds and SFX crossfade across cuts, J/L cuts via `audio_lead_s` / `audio_lag_s` in `edit_decisions.json`, voices never overlap |

Pipeline hooks: `run.sh` 1b validates the shot list for jump cuts and handles
(`edit_check.py --shot-list`), 1e builds the voice line-up, 3b runs `edit_check.py --run-dir` and
hands `final/edit_report.json` to the Post-Production Editor, who records every boundary decision in
`final/edit_decisions.json` (v2) and cuts straight (no dissolve unless the Director scripted it);
3g re-scores the trimmed segments with `edit_check.py --concat-list`.

## H3 Envelope & QA Calibration — *Cinderquay* postmortem (2026-10-16)

The first run after the Strata switch rendered S01_001 ten times and S01_002 six times and
passed nothing. The clips were not the problem class the gate assumed: the shot list, the
prompts and the QA rubric were all asking for things MiniMax H3 cannot do. What H3 verifiably
is (node source, model card, official prompt guides) and what the run proved are written up in
`knowledge/minimax_h3_rules.md §0`; the rules that must never regress:

| Symptom | Root cause | Rule / tool |
|---|---|---|
| S01_002's mufflered crowd and lantern reader appeared in S01_001, which banned both | the next clip's first frame was fed to ref2va as an "end anchor"; every ref2va image is a CONTENT source | continuity frames are never references — `generate_video_direct.py` refuses them; they enter only as i2va/fl2va frames |
| extreme-wide establishing shot became a crowd-level medium in 10/10 takes | ref2va re-frames toward its subjects | 0-character plate shots run as i2va from the plate (`shot_list_lint.py --fix`); composition-critical shots render a first frame |
| planned `composited_i2v`, rendered a good first frame, ran as ref2va without it | the reviewer's `h3_mode` default won | mode comes from the shot list (`run.sh` passes `SHOT_H3_MODE`); a rendered first frame forces i2va/fl2va |
| prompt grew 1 301 → 3 114 words, full of hex colours, frame numbers and px sizes | QA corrections appended, never applied | ≤ 900 words, `detailed_description` 350–500, no numbers H3 cannot read, retries rewrite from the shot list (reviewer preset + prompt-review skill) |
| "exactly ten figures", four montage beats in one shot, three characters with one copy-pasted action, a whale with a hand_position | shot list outside the envelope | `pipeline/shot_list_lint.py` (step 1b-lint, Director fix rounds): one beat per shot, ≤ 3 named characters, own action per character, extras uncounted |
| gate demanded all 11 categories ≥ 0.85 incl. "figure ≤ 12 % of frame height", "luma ± 10 %" | inspector invented pixel gates; gate trusted them | **the inspector scores, `normalise_qa_verdict.py` decides**: structural categories (hands_limbs, face_expression, character_identity, duplication, motion_quality, composition, continuity) ≥ 0.70 each and overall ≥ 0.75 → PASS; soft categories go to `assembly_fixes.json` |
| a retry can score lower than the take it replaced | last take always kept | failed takes archived under `clips/<id>/takes/`; `pipeline/best_take.py` restores the best at escalation; `MAX_RETRY=3` |
| an escalated clip was carried forward unreviewed | the loop moved on after the escalation post | the run **halts** (exit 3) and writes `HALT_FOR_REVIEW.json`; the operator decides with `clips/<SID>/MANUAL_PASS` or `RETRY_AGAIN`, then `./launch_resume.sh`. `./stop.sh` stops a run and the monitors cleanly |

Keyframe engines: `FRAME_ENGINE=qwen` (default, Qwen Image 2.1 + sheets) or `FRAME_ENGINE=ideogram4`
(`pipeline/ideogram_frames.py`: Ideogram 4 bbox layout pass, then a Qwen identity pass — Ideogram 4 is
text-only, so it places figures exactly but cannot lock identity; native ComfyUI graph with the
nvfp4 cond + unconditional UNETs on `/mnt/ChaosWares1/comfyui/models`; see rules §0.3).

## Service Dependencies

> **Model routing is per-call and cloud-only** (`run.sh` → `pipeline/set_agent_model.py` rewrites the
> `agent-default-model` section of `/root/.dsh/settings.yaml` before each agent call — see "Model
> Routing"). Check the run log for `ROUTE preset=… provider=… model=… tier=…`.
>
> ⚠ Only one of {ComfyUI, TTS cluster} can hold the GPU. `pipeline/gpu_owner.sh status` shows who
> does. Retired LLM units (Strata, OrcaRouter, ninfer, ninfer-us, qfn-iq3m-cuda) are stopped before
> every hand-over and never started by the pack.

| Service          | Used By                        | Port  | VRAM     | Notes                              |
|-----------------|--------------------------------|-------|----------|------------------------------------|
| Anthropic API (`claude-opus-5-5`) | Story Creator, Story Editor, Director | — | — | creative tier; `ANTHROPIC_API_KEY`; call/USD caps in `cloud_usage.json` |
| OpenCode Go (`deepseek-v4.1-flash`) | all other agents | — | — | execution + vision tiers; `OPENCODE_GO_API_KEY`; base `https://opencode.ai/zen/go/v1` |
| ComfyUI         | frames, clips, masters, music, SFX | 8188 | varies | Workflow execution engine; resting GPU owner |
| Qwen Image 2.1  | generate_frames.py, sheets     | -     | ~8-10 GB | Via ComfyUI node                    |
| MiniMax H3      | generate_clip.py               | -     | ~8-12 GB | Via ComfyUI node                    |
| RTX VSR (+ RIFE) | upscale_clip.py               | -     | ~8-14 GB | Via ComfyUI; `clip_4k.mp4` at `PP_TARGET_FPS` |
| ACE-Step 1.5    | music_render.py                | -     | via ComfyUI | score cues + ambience beds (`ace_step_1.5_turbo_aio`) |
| MMAudio         | sfx_render.py                  | -     | via ComfyUI | per-clip foley from the picture (`SFX_LAYER=1`) |
| tts-cluster     | tts_render.py, dialogue_stem.py | —    | ~8 GB    | starts all 4 TTS engines; exclusive GPU owner |
| Chatterbox      | Audio Producer (cast)          | 9882  | ~4-6 GB  | Primary dialogue (best naturalness) |
| CosyVoice 3     | Audio Producer (cast)          | 9880  | ~4-6 GB  | Multilingual / cross-lingual voice  |
| Orpheus 3B      | Audio Producer (cast)          | 9883  | ~3.5 GB  | Emotional dialogue                  |
| Kokoro          | Audio Producer (cast)          | 9881  | ~2-3 GB  | Narration only (lightweight)        |

## Output Structure

```
output/<run>/
├── story.json, story_v1.json, story_review.json, story_dialogue_diff.txt
├── shot_list.json, story_check.json, story_check_shots.json
├── cloud_usage.json          # cloud spend ledger (cloud_ledger.py)
├── HALT_FOR_REVIEW.json      # present only while halted (error or escalation)
├── assembly_fixes.json       # QA downgrades the editor must address
├── characters/, locations/   # reference sheets / environments
├── frames/                   # first/last frames per clip
├── clips/<SID>/
│   ├── clip.mp4              # picture (stem muxed in for preview/QA only)
│   ├── clip_4k.mp4           # 4K master at PP_TARGET_FPS
│   ├── dialogue.wav + dialogue_meta.json   # approved stem + anchor_s
│   ├── sfx.wav               # MMAudio foley
│   ├── qa_report.json, qa_verdict.json, reviewed_prompt.json, takes/
├── audio/
│   ├── voices/               # cast, line-up, separation.json
│   └── narration/            # narration_meta.json, tts_jobs.json, <block>.wav
├── music/
│   ├── music_cues.json, music_cues.resolved.json
│   ├── cues/<id>.wav (+ .json sidecar)
│   └── beds/<id>.wav
└── final/
    ├── edit_report.json, edit_boundaries.jpg
    ├── edit_decisions.json   # the editor's only output (v2)
    ├── timeline.json         # film clock (assemble.py --timeline-only)
    ├── segments/, concat.txt, picture.mp4
    ├── mix_raw.wav, mix.wav
    ├── movie_4k.mp4          # the film
    ├── audio_placements.json # every voice's film-time placement (subtitles.py)
    ├── assembly_log.json
    └── movie.srt, movie.ass
```
