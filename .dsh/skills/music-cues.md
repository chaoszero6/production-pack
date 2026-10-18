---
name: music-cues
description: Schema and rules for music/music_cues.json — the Music Composer's cue sheet that music_render.py renders with ACE-Step 1.5 and assemble.py lays into the film
whenToUse: When composing the score plan for a run, validating a cue sheet, or assembling music and ambience beds into the final mix
---

# Music Cues Skill

Used by the Music Composer (writes the cue sheet), `pipeline/music_render.py`
(validates and renders it) and `pipeline/assemble.py` (lays the rendered cues into the film).

## Why a cue sheet

The GPU has exactly one owner at a time (ComfyUI | TTS cluster (agents run in the cloud)). The composer
runs on the LLM, so ComfyUI — and with it ACE-Step — is down during its turn. The composer
therefore **plans**: it writes `$RUN_DIR/music/music_cues.json`. The pipeline then hands the
GPU to ComfyUI and renders every cue with `pipeline/music_render.py`. The composer never
calls port 8188 or any audio port, never writes a WAV and never invents a file layout.

## File: `$RUN_DIR/music/music_cues.json`

```json
{
  "version": 1,
  "film_duration_s": 612.4,
  "global": {
    "style_tags": "orchestral film score, pixar, warm, cinematic",
    "key": "D major",
    "vocals": false,
    "bed_level_db": -18,
    "music_level_db": -6
  },
  "cues": [
    {
      "id": "M01",
      "scene_id": "scene_001",
      "anchor": {"start_clip": "S01_001", "end_clip": "S01_004"},
      "start_s": 0.0,
      "end_s": 48.2,
      "mood": "gentle wonder",
      "tempo_bpm": 72,
      "intensity": 0.35,
      "instrumentation": "soft strings, celesta, harp",
      "genre_tags": ["orchestral", "film score", "ambient"],
      "key": "D major",
      "time_signature": "4",
      "vocals": false,
      "transition_in": {"type": "fade", "s": 1.5},
      "transition_out": {"type": "crossfade", "s": 2.5},
      "loopable": false,
      "tail_s": 3.0,
      "seed": 20260001,
      "sync_points": [{"t_s": 12.0, "note": "swell when the lantern lights"}]
    }
  ],
  "beds": [
    {
      "id": "A01",
      "scene_id": "scene_001",
      "start_s": 0.0,
      "end_s": 48.2,
      "prompt": "night harbour ambience, gulls, soft wind, no music",
      "engine": "acestep",
      "level_db": 0,
      "seed": 20260101
    }
  ],
  "silence": [
    {"scene_id": "scene_004", "reason": "the reveal plays dry"}
  ]
}
```

### Field reference

| Field | Meaning |
|-------|---------|
| `film_duration_s` | copied from `final/timeline.json`; the validator overwrites it |
| `global.style_tags` | prepended to every cue's tag string (house style of the score) |
| `global.key` | default key when a cue has none |
| `global.vocals` | must be `false` — the score is instrumental |
| `global.music_level_db` / `bed_level_db` | gain assembly applies to the music bus / the bed bus (dB relative to the dialogue reference; defaults −6 / −18) |
| `global.ref_lufs` | loudness every cue is normalised to before the gain (default −18 LUFS) |
| `cues[].level_db` / `beds[].level_db` | per-item **trim** in dB on top of the bus gain (default 0; `-3` = a touch quieter). Never write the bus level here — assembly adds the two |
| `cues[].id` | `M` + two digits, unique (`M01`, `M02`, …) |
| `cues[].scene_id` | the scene the cue serves (`story.json scenes[].id`) |
| `cues[].anchor` | `{start_clip, end_clip}` — shot IDs; **anchors win** over `start_s`/`end_s` and are resolved from `timeline.json` (start = first frame of `start_clip`, end = last frame of `end_clip`) |
| `cues[].start_s` / `end_s` | film time in seconds; used only when there is no anchor |
| `cues[].mood` | one short phrase (`gentle wonder`, `rising dread`, `triumphant release`) |
| `cues[].tempo_bpm` | 50–160 |
| `cues[].intensity` | 0–1; follows the scene's emotional beat; drives the dynamics word in the prompt |
| `cues[].instrumentation` | comma-separated instruments; use the character/location motif instruments consistently |
| `cues[].genre_tags` | list of ACE-Step style tags (`orchestral`, `film score`, `ambient`, `celtic`, `lullaby`, …) |
| `cues[].key` | musical key (`D major`, `A minor`); keep one key per scene, change keys on scene changes |
| `cues[].time_signature` | `"4"` (4/4), `"3"` (3/4), `"6"` (6/8) — the renderer maps it to the node's option list |
| `cues[].vocals` | must be `false` |
| `cues[].transition_in` / `transition_out` | `{type, s}` — `type ∈ {fade, crossfade, hard, none}`, `s` seconds |
| `cues[].loopable` | hint for assembly: the cue may be looped if the picture runs long |
| `cues[].tail_s` | extra seconds rendered past `end_s` so the fade-out has material (2–4 s) |
| `cues[].seed` | integer; fixed so a re-render reproduces the take |
| `cues[].sync_points` | `[{t_s, note}]` — film-time hits the cue should land; documentation for the editor, not rendered |
| `beds[]` | ambience beds (no music): `id` `A` + two digits, `scene_id`, `start_s`/`end_s` or `anchor`, `prompt`, `engine` (`acestep` or `mmaudio`), `level_db`, `seed` |
| `silence[]` | scenes that deliberately have no cue — `{scene_id, reason}` |

## Rules (enforced by `music_render.py --validate`, exit 2 on any violation)

1. **Anchors win.** A cue with `anchor` gets its `start_s`/`end_s` from `timeline.json`; an
   unknown shot ID is a violation. Without an anchor `start_s < end_s` must be numbers inside
   the film.
2. **Cues tile each scene.** Between consecutive cues inside a scene the gap is ≤ 0.5 s; the
   first cue starts within 0.5 s of the scene start and the last ends within 0.5 s of the scene
   end. Cues may overlap only by the earlier cue's crossfade seconds.
3. **Every scene has a cue or a `silence` entry.** Silence is a choice, so it is written down.
4. **No vocals.** `global.vocals` and every `cues[].vocals` are `false`; no prompt, mood,
   instrumentation or tag mentions vocals, lyrics, singing, choir, humming or a singer.
   Beds likewise never ask for voices.
5. **Tempo 50–160 bpm**, **intensity 0–1**.
6. **Cue length 10–180 s.** ACE-Step's minimum render is 10 s, so a shorter scene is covered by
   a neighbouring cue's anchor. A longer cue is split by the validator into chained pieces
   (`M03a`, `M03b`, …) with a 2 s crossfade between them and `seed + 1` per piece; you do not
   need to split by hand.
7. `transition_in.type` / `transition_out.type ∈ {fade, crossfade, hard, none}`.
8. Cue IDs and bed IDs are unique; `scene_id` values exist in the timeline.

The validator writes the resolved sheet to `music/music_cues.resolved.json` (anchors turned
into seconds, long cues split, `film_duration_s` filled). The composer gets ONE fix round from
`run.sh` when validation fails; the report is one line per problem.

## How the renderer turns a cue into sound

`music_render.py` builds, per cue: tags = `global.style_tags` + `genre_tags` + `instrumentation`
+ `mood` + `instrumental` + `<tempo> bpm` + an intensity word (`sparse and quiet` …
`climactic and powerful`); lyrics = `[Instrumental]`; duration = `end - start + tail_s +
transition_out.s` (min 10 s, max `--max-cue-s`); the cue's seed; key and time signature mapped
to the node's option lists. Output: `music/cues/<id>.wav` (48 kHz stereo 24-bit) with a
sidecar `music/cues/<id>.json` `{prompt, seed, seconds, lufs, engine, ckpt, ts, cue_hash}`.
Beds go to `music/beds/<id>.wav`. A cue whose hash is unchanged is not re-rendered.

The tag string never contains the word "vocals", not even as "no vocals": in a positive prompt a
negated word still pulls toward what it names. Instrumental is asserted positively.

## How `assemble.py` consumes the cue sheet

- Reads `music/music_cues.resolved.json` (never the raw sheet) and `music/cues/<id>.json`.
- Places each cue at `start_s` on the music bus; applies `transition_in` / `transition_out`
  (fade / crossfade over the stated seconds; `hard` = no ramp; `none` = the cue was rendered to
  end naturally and the tail is kept).
- Gain: `music_level_db` on the music bus plus the cue's `level_db` trim; `bed_level_db` plus
  the bed's trim. The measured `lufs` in the sidecar normalises every cue to `ref_lufs` before
  the gain, so a loud retake does not jump out.
- The resolved file also carries assembly's key names (`film_start_s`, `film_end_s`,
  `crossfade_s`, top-level `music_level_db` / `ref_lufs`) so split pieces (`M03a`, `M03b`)
  and anchors are already plain seconds when assembly reads them.
- **Ducking is assembly's job, not the composer's.** Assembly ducks the music bus under
  dialogue and narration (sidechain, ≈ −6 dB during a line, 300 ms attack / 800 ms release).
  The composer writes the full-level cue; it never pre-lowers a cue because "there is
  dialogue here" — that would double the duck.
- Music and beds may crossfade over cuts; picture never does.
- `silence` scenes get nothing on the music bus; beds may still run there if listed.

## Thinking like a film composer (what makes a good sheet)

- **Motifs.** Give each principal character and the central location a motif in a fixed
  instrumentation and key family; reuse it, vary tempo and intensity. Write the motif
  assignments in `global.notes` so they stay consistent across cues.
- **Intensity follows the beat**, not the action. A chase can be quiet if the audience knows
  the outcome; a still room can be at 0.8 when the secret lands.
- **Silence is a choice.** The biggest reveal and the quietest grief often play dry; list them
  in `silence` with the reason.
- **Transitions on cuts.** End a cue on a scene change (`crossfade` into the next cue or `fade`
  to silence); never let a cue end mid-scene unless the next cue starts there with a `hard`
  cut that the picture motivates.
- **Dialogue scenes** get lower intensity and sparser instrumentation (strings pad, no
  percussion), not a lower level — ducking is assembly's job.
- One mood per cue. A scene that turns in the middle gets two cues joined by a crossfade on the
  turn, with a `sync_point` noting the moment.
