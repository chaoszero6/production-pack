---
name: editing-grammar
description: Rules for planning, rendering and cutting shots so every clip-to-clip cut reads as a deliberate edit, not a jump
whenToUse: When writing a shot list, writing an H3 prompt, reviewing adjacent clips, or assembling the final cut
---

# Editing Grammar Skill

Used by the Director, Storyboard Artist, Screenplay Reviewer, QA Inspector and
Post-Production Editor. Every rule here exists because *The Clockwork Moth* (2026-10) was
reviewed publicly as "signs of an inexperienced filmmaker — some clips don't cut, you get a jump
cut or continuity problem at the cut, each cut has a small dissolve in it". Those were
pipeline defects, not render defects:

| Complaint | Root cause found | Rule that prevents it |
|-----------|------------------|-----------------------|
| Jump cuts between clips | 9 consecutive pairs were the SAME character, SAME shot size, SAME location (e.g. S01_05→S01_06, both "Nana close-up, three-quarter, static") | §1 Thirty-degree / two-sizes rule |
| Shots "don't cut" | Dialogue clips were sized to the voice stem (0.4 s lead, 0.6 s tail) — no handles, speech starts almost on the cut, no reaction beats | §2 Handles, §3 Cut points |
| Small dissolve on every cut | RIFE frame interpolation was run on the ASSEMBLED film, so every cut was blended with its neighbour | §5 Interpolation never crosses a cut |
| Clock shows a different time after the cut | No ledger for readable props; each prompt described the clock freely | §4 Readable-prop ledger |

Measurable checks for all of this live in `pipeline/edit_check.py` (run on the ordered clip
list before assembly; writes `final/edit_report.json`).

---

## 1. Which shots are allowed to sit next to each other

A cut is legal only if the audience can tell *why* the picture changed. Between any two
consecutive clips in the same scene, at least ONE of these must be true:

- **Different subject** (A is Tobias, B is Nana; A is a face, B is an insert of the clock).
- **Different shot size by at least two steps** on the ladder
  `extreme-wide → wide → medium-wide → medium → medium-close-up → close-up → extreme-close-up`.
  Close-up → medium-close-up is ONE step: that is a jump cut.
- **Camera angle differs by ≥ 30°** around the subject (the thirty-degree rule), e.g.
  three-quarter-left → profile, or eye-level → low angle.
- **Deliberate time skip** marked in the shot list as `edit.relation_to_prev: "time-jump"`
  with a transition that *announces* it (fade, insert, or an establishing shot in between).

NEVER plan two consecutive clips with the same character set, same shot size and same angle.
If the story needs the same person to keep talking, do one of:
1. Make it ONE longer clip (H3 allows up to 15 s).
2. Cut away to the listener's reaction (a reaction single is the cheapest cut-saver in film).
3. Cut to an insert (hands, prop, the clock) and come back on a different size.
4. Change size by two steps (close-up → medium-wide) AND keep the eyeline.

**Rhythm:** no more than two consecutive clips of the same shot size with different
subjects either (close-up / close-up / close-up reads as a slideshow). Alternate
single → reaction → two-shot → insert. A dialogue scene needs at minimum: one establishing
two-shot, one single per speaker at a clearly different size/angle from the two-shot, at
least one reaction single, and one insert or cutaway.

## 2. Handles — render more than you will use

Every clip is rendered with **handles**: ≥ 1.0 s (24 frames) of in-character, in-situation
action BEFORE the first beat and AFTER the last beat. The shot list records
`edit.handle_head_s` and `edit.handle_tail_s`; `duration_seconds` INCLUDES them.

- Dialogue clip: `duration = handle_head + 0.4 + stem_length + 0.6 + handle_tail`, rounded up
  to the next valid H3 length (frames = 5 + 17k). The voice stem is placed at
  `handle_head + 0.4` — never at 0.4. The character must be visibly *listening / about to
  speak* during the head handle and *landing the line* during the tail handle.
- Silent clip: head handle = subject already in the pose the previous clip left them in;
  tail handle = subject holding the final pose / continuing the motion.
- The handles are what the editor trims to find the cut. A clip without handles can only
  be butt-joined at frame 0 and its last frame — that is why the Moth cuts felt wrong.

Prompts must describe the handles explicitly, e.g. `[0s-1s] Tobias, mouth closed, looks up at
Nana, listening. [1s-5.4s] (S1) says <d>…</d>. [5.4s-6.5s] He holds her gaze, lips closed.`

## 3. Cut points — what the boundary frames must contain

Every shot carries `edit.cut_in` and `edit.cut_out`: one sentence each describing the exact
visual state at the boundary (pose, gaze direction, prop state, motion phase). The
Screenplay Reviewer copies them verbatim into the first and last temporal blocks of the prompt.
Rules for choosing them:

- **Cut on action.** Prefer leaving shot A in the middle of a movement (hand rising, head
  turning, door swinging) and entering shot B with the *same movement finishing*. The brain
  hides the cut inside the motion. Both prompts must describe the same movement phase.
- **Cut on the look.** For dialogue: cut from the speaker to the listener on the beat the
  speaker's eyes move, or 2–6 frames before the next line starts (the audio of B may lead
  the picture — a J-cut; the editor does this with the handles).
- **Never cut static-to-static on the same subject.** If both boundary frames are a still
  pose of the same character, it is a jump cut regardless of angle.
- **Screen direction** is preserved: a character moving left→right keeps moving left→right
  in the next clip. A look to screen-left is answered by a look to screen-right.
- **Eyelines & 180° rule**: speaker and listener face opposite screen sides, camera stays on
  one side of the axis for the whole scene (see continuity-check skill).
- **Exit/entrance**: cut BEFORE the character clears frame in A, pick them up already
  moving in B. Do not show the empty room at the end of A (H3 melts it).

## 4. Readable-prop ledger (time, text, counts)

Any prop whose *state can be read* by the audience — a clock face, a calendar, a sundial,
the number of cups on a table, a candle's height, a window's weather — gets an entry in
`readable_props` in the bible/shot list with its value **per scene**, e.g.
`{"grandfather_clock": {"scene_001": "hands at ten past seven", "scene_002": "hands at half past seven"}}`.

- The Director sets the value; the Screenplay Reviewer writes the SAME phrase into every
  prompt where the prop is visible; the keyframe prompt pins it too.
- If a story beat is "she looks at the clock", the clock's reading is now plot information:
  the next clip that shows it must show the same reading unless the story advanced time, and
  then it must advance the *right amount*.
- If the pipeline cannot reliably render a readable value (H3 draws clock hands at random),
  the Director must frame it so it is NOT readable (angle, blur, distance) or make the time
  cue audible (a chime) instead of visual. A wrong readable value is worse than no value.
- QA continuity check (category 9) compares the readable prop state against the ledger and
  against the previous clip, and FAILS the clip on a mismatch.

## 5. Transitions and interpolation

- **The default transition is a straight cut.** No dissolve, no fade, no xfade unless the
  Director wrote `transition_to_next.type` ≠ `cut` for a story reason (time passing, dream).
  Dissolves do not "mask" a bad cut — they advertise it.
- **Frame interpolation (RIFE / minterpolate) runs per clip only, never across a cut.**
  Interpolating the assembled film blends the last frame of A with the first frame of B and
  produces a 2–4-frame dissolve at every cut. Upscale and interpolate each `clip.mp4` to its
  own `clip_4k60.mp4` (`pipeline/upscale_clip.py`) and concatenate those; if the whole film
  must be processed in one pass, `pipeline/upscale_film.py` MUST be given the cut list
  (`--concat-list` or `--cuts`) so no interpolation window spans a cut.
- Trimming happens on the per-clip masters, frame-accurately, BEFORE concat; the clip's own
  dialogue stem is trimmed with it so lips stay in sync (OpenCineAgent rule).
- Music and ambience cross-fade over cuts; picture never does.

## 6. Adjacent-pair review ("does it cut?")

Before assembly, every boundary between consecutive clips is reviewed as a pair, not as two
clips. Run `python3 pipeline/edit_check.py --run-dir <run_dir>` and then look at every
boundary it flags. Each boundary is scored:

```
cut_score 0.0-1.0 (threshold 0.80)
  jump_cut        : same subject + similar framing + similar frame → FAIL (score ≤ 0.4)
  dead_boundary   : both tail of A and head of B are static holds of the same subject → ≤ 0.6
  light_mismatch  : mean luma / colour temperature differs > 12 % between the boundary frames
                    of the same scene → ≤ 0.6 (grade in post if small, re-render if large)
  axis_break      : speaker/listener eyeline on the same screen side → ≤ 0.5
  readable_prop   : ledger value differs from the previous clip → ≤ 0.5
  rhythm          : third consecutive clip of the same shot size → warn (≤ 0.75)
```

Fix order for a failed boundary (cheapest first):
1. **Trim** into the handles to find a frame where A is mid-motion and B picks it up.
2. **Insert** an existing approved cutaway (reaction single, prop insert) between A and B.
3. **Re-render only B** with a new size/angle (two steps away) keeping the same cut_in.
4. **Re-stage** both (Director) — last resort.

Record every boundary decision in `final/edit_decisions.json`
(`{"boundary": "S01_05|S01_06", "action": "trim", "a_out_s": 5.1, "b_in_s": 0.7, "note": "..."}`).
