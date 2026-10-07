---
name: continuity-check
description: Skill for checking visual continuity between consecutive clips
whenToUse: When verifying that consecutive clips maintain visual consistency
---

# Continuity Check Skill

Used by the QA Inspector and Post-Production Editor to verify visual continuity
between consecutive clips. This is critical for a seamless viewing experience.

## What to Check

### 1. Frame Boundary Matching
- Extract last frame of clip N
- Extract first frame of clip N+1
- Compare for:
  - Character position consistency (did they teleport?)
  - Lighting consistency (sudden brightness shift?)
  - Background consistency (did the environment change?)
  - Color temperature match

### 2. Character Consistency Across Clips
For each character appearing in consecutive clips:
- Hair style and color matches
- Clothing matches (no costume changes unless scripted)
- Accessories present/absent as expected
- Skin tone consistency
- Size/scale relative to environment

### 3. Location Consistency
- Same architectural features visible
- Same props in the scene
- Same time of day / lighting direction
- Weather consistency

### 4. Motion Continuity
- Character was walking LEFT in clip N → still moving LEFT in clip N+1
- Character was sitting → is still sitting (unless action described otherwise)
- Object was in hand → still in hand

### 5. Readable-prop / time continuity (The Clockwork Moth: "she looks at a clock,
indicating time is important, then the time is different in the next cut")
- Every readable state — clock hands, calendar, number of cups/candles, candle height,
  weather in a window — is compared against the `readable_props` ledger for the scene AND
  against the previous clip's frame.
- A readable value that changed without a scripted time skip is a continuity FAIL (score
  ≤ 0.5), never a nit: the audience was told to read it.
- If the shot list has no ledger value for a visible readable prop, flag
  `"readable_prop_unpinned"` so the Director pins it or frames it unreadable.

### 6. Does the pair CUT? (editing-grammar skill)
Continuity is necessary but not sufficient — two perfectly continuous clips of the same
person at the same size are a jump cut. Check the boundary as an edit:
- different subject, OR ≥ 2 shot-size steps, OR ≥ 30° angle change (unless a deliberate
  `relation_to_prev: time-jump`)
- one side of the cut is in motion or on a look; not static hold → static hold
- `edit.cut_out` (N) and `edit.cut_in` (N+1) describe the same motion phase
- the dialogue stem of N+1 does not start within 0.5 s of frame 0 (no handle)
Measured part: `python3 pipeline/edit_check.py --clips <N.mp4> <N+1.mp4> --shots shot_list.json`.

## Continuity Fix Strategies

### Minor Issues (fix in post):
- Color grade clip N+1 to match clip N
- Crop/pan slightly to match character position
- Trim into the handles to land the cut on action / on a look
- Insert an approved reaction or insert clip between the two
- NEVER add a dissolve to mask a discontinuity — it advertises the cut and reads as a
  mistake (every Moth cut had a blended frame and viewers noticed)

### Major Issues (must regenerate):
- Character identity completely different
- Location doesn't match at all
- Physics violation (character suddenly on wrong side)
- Jump cut between same subject / same size / same angle with no cutaway available
- Readable prop (clock, count) contradicts the ledger
- Internal cut inside a clip

### Prevention (best approach):
- Use "first_from_previous" keyframe strategy — the previous clip's last frame
  becomes the next clip's first frame
- Use reference images from the same character/location sheets
- Keep the same identity anchors in every prompt

## Scoring
```
boundary_match: 0.0-1.0  (frame similarity at boundaries — NOTE: too similar with the
                          same subject and size is a JUMP CUT, see edit_pair)
character_match: 0.0-1.0  (identity consistency)
location_match: 0.0-1.0   (environment consistency)
motion_match: 0.0-1.0     (action continuity)
readable_props: 0.0-1.0   (ledger value matches; 0.5 or lower on any contradiction)
edit_pair: 0.0-1.0        (cut_score from edit_check.py / the §6 checklist)

continuity_score = weighted average
threshold: 0.80 for PASS; readable_props < 0.6 or edit_pair < 0.8 fails on its own
```
