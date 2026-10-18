---
name: final-assembly
description: How the final movie is assembled — a deterministic pipeline (timeline → picture → one audio mix → subtitles) driven by the editor's decisions file
whenToUse: When writing edit decisions, reading assembly output, or reasoning about where any sound lands in the finished film
---

# Final Assembly Skill

Since 2026-10-18 assembly is deterministic. The Post-Production Editor writes ONE file,
`final/edit_decisions.json`; `pipeline/assemble.py` does everything else. No agent runs
ffmpeg, trims a clip, strips or muxes a track, or mixes audio.

## The timeline model

**Clip files contribute PICTURE ONLY.** Every audio element — dialogue stems, narration
blocks, music cues, ambience beds, per-clip SFX — is placed exactly once on ONE film
timeline and mixed once. That removes the three defects of per-clip audio: audio jumps at
every cut (clips arrived in different audio states), double voice (stems muxed twice,
narration rendered per clip, H3's native voices under the narration), and the impossibility
of J/L cuts. The native H3 track is stripped by default (`KEEP_NATIVE_AMBIENCE=1` keeps it as
a bed only when no speech is detected in it). `mux_stem.py` still gives `clip.mp4` its stem
for previews and QA — assembly never reads that track.

Film time is built from frame counts, never float durations:

```
fps           probed from the masters clips/<SID>/clip_4k.mp4 (one fps for all; PP_TARGET_FPS)
src_in_f      = round(cut_in_s  * fps)
n_frames      = round(cut_out_s * fps) − src_in_f
film_start_s  = (sum of n_frames of every earlier segment) / fps
```

## Phase 3 in run.sh

| Step | Who | Writes |
|------|-----|--------|
| 3a narration plan | audio-producer agent (`narration_check.py --plan` validates) | `audio/narration/tts_jobs.json`, `narration_meta.json` — one block per passage, carried by the first clip of its span |
| 3b edit check | `edit_check.py --run-dir` | `final/edit_report.json`, `final/edit_boundaries.jpg` |
| 3c edit decisions | post-production-editor agent (`assemble.py --validate-decisions`) | `final/edit_decisions.json` v2 — ONLY this |
| 3d timeline | `assemble.py --timeline-only` | `final/timeline.json` (film clock for the composer) |
| 3e music cues | music-composer agent (`music_render.py --validate`) | `music/music_cues.json` → `music_cues.resolved.json` |
| 3f render | `tts_render.py --keep-gpu` → `music_render.py` (ACE-Step 1.5) → `sfx_render.py` (MMAudio, `SFX_LAYER=1`) — one GPU excursion | `audio/narration/*.wav`, `music/cues/<id>.wav`, `music/beds/<id>.wav`, `clips/<SID>/sfx.wav` |
| 3g assemble | `assemble.py` then `subtitles.py --embed` then `edit_check.py --concat-list` | `final/movie_4k.mp4`, `final/audio_placements.json`, `final/assembly_log.json`, `final/concat.txt`, `final/movie.srt`, `final/movie.ass` |

## What assemble.py reads

- `shot_list.json` — play order, `edit.handle_head_s/handle_tail_s`, `transition_to_next`.
- `clips/<SID>/qa_verdict.json` (PASS or `MANUAL_PASS`), `clip_4k.mp4`, `dialogue.wav` +
  `dialogue_meta.json` (`anchor_s`), `sfx.wav`.
- `final/edit_decisions.json` — the editor's per-boundary decisions (below).
- `audio/narration/narration_meta.json` + the rendered block WAVs.
- `music/music_cues.resolved.json` + `music/cues/*.wav`, `music/beds/*.wav`.
- `assembly_fixes.json` — the QA downgrade ledger (the editor turned it into decisions).

## The editor's decisions file — `final/edit_decisions.json` (v2)

```json
{"version": 2,
 "decisions": [{"boundary": "S01_005|S01_006", "action": "trim",
                "a_out_s": 5.10, "b_in_s": 1.65,
                "audio_lead_s": 0.25, "audio_lag_s": 0.0,
                "cut_type": "cut", "insert_clip": null,
                "note": "A leaves as the hand rises; B enters mid-turn; B's line leads by 0.25 s"}],
 "removed_clips": [], "inserted": []}
```

- Default trim when no decision exists for a boundary: `src_in = handle_head_s / 2`,
  `src_out = master_duration − handle_tail_s / 2`, clamped on dialogue clips so the stem lies
  inside the picture with 0.2 s on each side.
- `a_out_s` / `b_in_s` override that per boundary (source seconds inside each master).
- Straight cuts only (`cut_type: "cut"`); anything else only when the Director scripted it.
- Fix order for a boundary under 0.80: trim deeper → `insert` an approved reaction/insert
  clip → `re-render` B at a new size/angle → re-stage. Never a dissolve.

### Where J-cuts and L-cuts live

They are numbers on the boundary, not ffmpeg work:

- **J-cut** — `audio_lead_s` (0.15–0.30 s) on a boundary INTO a dialogue clip. B's stem is
  always placed at `B.film_start + (anchor_s − b_in_s)`, so `b_in_s = anchor_s + audio_lead_s`
  (checked to one frame when both are given; lead ≤ 0.5 s).
- **L-cut** — `audio_lag_s` (0.20–0.40 s) on a boundary OUT of a dialogue clip. A's stem is not
  truncated at the cut; it runs past it, capped at 0.6 s with a 40 ms fade.
- **Voices never overlap:** stem A must end 0.10 s before stem B starts; assembly rejects the
  decision otherwise.

## The mix (one pass, film timeline)

float32 stereo at 48 kHz, five buffers — dialogue, narration, music, bed, sfx — and a voice
mask built from the stems' own energy (RMS per 10 ms > −45 dBFS), never from clip audio.

| Element | Placement | Level |
|---------|-----------|-------|
| Dialogue stem | once, at `film_start + anchor_s − src_in` (+ lead / lag) | 0 dB reference |
| Narration block | once per block, at `film_start(starts_on) + 0.4 s`; never under a line | −1 dB |
| Music cue | per `music_cues.resolved.json`, normalised to −18 LUFS, fades per `transition_in/out`, equal-power crossfade between consecutive cues | −6 dB, ducked to −12 dB under dialogue AND narration |
| Bed (ambience) | continuous across cuts, equal-power crossfade with ≥ 180 ms overlap around every boundary | −18 dB, ducked to −24 dB |
| SFX (`clips/<SID>/sfx.wav`) | per clip, crossing cuts with the same 180 ms overlap | −12 dB, ducked to −20 dB |

Ducking is side-chain at 100 Hz control rate, 60 ms look-ahead attack, 400 ms release.
Sum → true-peak guard → `final/mix_raw.wav` → two-pass loudnorm (I = −16 LUFS, TP = −1.5,
LRA = 11) → `final/mix.wav` → muxed onto `final/picture.mp4` → `final/movie_4k.mp4`.
Nothing falls back silently: every "could not" is an error (exit 1/2/3) or a warning in
`final/assembly_log.json`.

## Subtitles

`pipeline/subtitles.py --embed` writes `final/movie.srt` and `final/movie.ass` from
`final/audio_placements.json` — the actual film-time placement of every stem and narration
block — so cues are timed to the audio in the mix, not to shot-list estimates. The
subtitle-generator agent is deprecated and not called.

## Interpolation never crosses a cut

Upscaling and any interpolation happen per clip (`pipeline/upscale_clip.py` →
`clips/<SID>/clip_4k.mp4`; RIFE only when `PP_TARGET_FPS` asks for more than the source
rate). `assemble.py` trims and concatenates those masters; it never interpolates. If the
whole film must ever be processed in one pass, `pipeline/upscale_film.py` MUST be given the
cut list (`--concat-list final/concat.txt`) — it refuses to run without one, because a
blended frame at every cut is exactly the dissolve *The Clockwork Moth* was reviewed for.

## Final checks

- `edit_check.py --concat-list final/concat.txt` re-scores the trimmed segments; no boundary
  below 0.80 ships without an entry in `edit_decisions.json`.
- `final/assembly_log.json` lists every placement, warning and duration check.
