---
name: qa-inspection
description: Skill for QA inspection of generated video clips and images
whenToUse: When reviewing a generated clip or image for quality issues
---

# QA Inspection Skill

Used by the QA Inspector agent to systematically review each generated clip.
This is a Pixar-quality production — be strict. Every clip must look like it belongs
in a professional animated movie.

## Inspection Protocol

### Step 1: Extract frames
Extract frames from the video at multiple rates:
- **2 FPS** — general quality review (all categories)
- **10 FPS** — motion analysis and temporal consistency
- **30 FPS** — lip sync analysis (dialogue clips only)

**Always use an absolute path under the run directory** — never a relative `output/qa/...`
path (CWD is `$DSH_DIR`, so relative paths land in the harness sandbox and the pipeline
gate never finds them):

```bash
# {run_dir} = parent of clips/ (e.g. .../output/run_YYYYMMDD_HHMMSS)
mkdir -p {run_dir}/qa/{shot_id}
ffmpeg -i {clip_path} -vf fps=2 {run_dir}/qa/{shot_id}/frame_%04d.png
ffmpeg -i {clip_path} -vf fps=10 {run_dir}/qa/{shot_id}/motion_%04d.png
# For dialogue clips only:
ffmpeg -i {clip_path} -vf fps=30 {run_dir}/qa/{shot_id}/lipsync_%04d.png
```

**Write the structured report to BOTH of these absolute paths** (the normaliser
searches them, newest mtime wins):

1. `{run_dir}/qa/{shot_id}/qa_report.json`  ← preferred
2. `{clip_dir}/qa_report.json`              ← also searched

Do **not** write `qa_report.json` under `$DSH_DIR/output/qa/...`.

### Step 2: Single-clip quality analysis
For each extracted frame, analyze:

```
Score each category 0.0-1.0 (1.0 = perfect):

1. HANDS & LIMBS: Count fingers on each visible hand. Check for merging, extra limbs,
   impossible positions, POV intrusion. Zoom crop hands at 2x for verification.
2. FACE & EXPRESSION: Check symmetry, consistency with character reference,
   distortion, morphing artifacts. Expression must match the emotional beat.
3. CHARACTER IDENTITY: Compare against reference sheet. Check hair, clothing,
   accessories, distinguishing features. Every identity anchor must be present.
4. DUPLICATION: No cloned characters, doubled props, ghost objects, double-exposure.
   Every prop should have a clean single-instance lifecycle.
5. MOTION: Smooth, physically plausible, no jitter or teleporting. Use 10fps frames
   to compute inter-frame differences. Static camera = background MAD should be < 2.0.
6. LIP SYNC: Dialogue clips = mouth must move in sync with the audio stem timeline.
   Narration clips = mouth must stay CLOSED (no lip movement ever).
   Extract 30fps face crops and measure mouth onset/offset vs audio envelope.
7. TEXTURE & VISUAL: No swimming textures, resolution drops, banding, visual noise.
   Check pattern stability across frames (clothing patterns, wall textures).
8. COMPOSITION: Framing matches Director's shot spec. Camera angle, lens, character
   positioning all correct. Background consistent with location reference.
```

### Step 3: Cross-clip continuity analysis (CRITICAL)
This step ensures the movie looks coherent as a whole, not just per-clip.

**When to check:** Always check against adjacent clips in the SAME SCENE.
If the prompt includes a "CROSS-CLIP CONTINUITY CONTEXT" section, use it.

**How to check:**
1. If the previous clip in the same scene has a `clip.mp4`, extract its LAST FRAME:
   ```bash
   ffmpeg -sseof -0.1 -i {prev_clip_dir}/clip.mp4 -frames:v 1 {run_dir}/qa/{shot_id}/prev_last_frame.png
   ```
2. Extract the FIRST FRAME of the current clip:
   ```bash
   ffmpeg -i {clip_path} -frames:v 1 {run_dir}/qa/{shot_id}/curr_first_frame.png
   ```
3. Compare them side by side for:

```
9. CONTINUITY (score 0.0-1.0, threshold 0.85):

   a. PROP CONTINUITY: Objects visible in the previous clip must still be present
      in the current clip if the scene hasn't changed. A bowl on the table doesn't
      vanish. A teacup set down stays on the table. A spoon in hand stays in hand.
      Check: count visible props in both frames and verify consistency.

   b. CHARACTER DIRECTION: If the shot list says a character faces "3/4 from right",
      they MUST face that direction. Check against both the shot spec AND the previous
      clip. Characters don't randomly flip direction between cuts.

   c. 180-DEGREE RULE: If character A is on screen-left and B is on screen-right,
      this spatial relationship must be maintained across all cuts in the scene.
      The camera must not cross the axis of action.

   d. SPATIAL LAYOUT: The room layout must be consistent. Window on the right stays
      on the right. The grandfather clock stays in the same wall position. Furniture
      doesn't move between cuts. Compare against the location reference image.

   e. LIGHTING: Same time of day = same lighting temperature and direction across
      all clips in the scene. Morning warmth can't become cold blue mid-scene.
      Shadows should fall in a consistent direction.

   f. CHARACTER STATE: If a character is sitting at the table, they must still be
      sitting unless the action explicitly describes them standing up. Props being
      held/used must persist (eating with a spoon → spoon visible in next shot).

   g. COSTUME & APPEARANCE: Same clothes, hairstyle, accessories across every clip
      in a scene. Goggles on forehead stay on forehead. Apron stays on. Prosthetic
      arm stays on the correct side. Hair stays in the same style.

   h. MATCH CUT/TRANSITION: If the previous clip's transition_to_next specifies
      "match-cut" or "dissolve", the visual connection must be seamless — matching
      composition, color temperature, movement direction, and subject position.

   i. EMOTION ARC: The character's expression should follow the story's emotional
      progression. Don't smile during a tense moment. Don't look calm after alarming
      news. Check the emotional_beat in the shot list.

   j. READABLE PROPS (The Clockwork Moth: "she looks at a clock … then the time is
      different in the next cut"): any clock face, calendar, count of objects, candle
      height or window weather must match the `readable_props` ledger for the scene AND
      the previous clip. A contradiction scores continuity ≤ 0.5 — it is a FAIL, not a nit.

   k. DOES IT CUT (editing-grammar skill): judge the boundary as an edit.
      Same characters + same shot size (≤ 1 ladder step) + same angle as the previous
      clip, with no deliberate `edit.relation_to_prev: time-jump` → JUMP CUT → FAIL.
      Both sides static holds of the same subject → dead boundary. The first ≥ 1 s and
      last ≥ 1 s of the clip must be in-character handles (`edit.cut_in` / `edit.cut_out`
      visible); a dialogue stem starting < 0.5 s from frame 0 = no handle → FAIL.
      Any hard cut INSIDE the clip → FAIL (re-render).
      Measured part:
      ```bash
      python3 pipeline/edit_check.py --clips {prev_clip_dir}/clip.mp4 {clip_path} \
        --shots {run_dir}/shot_list.json --out {run_dir}/qa/{shot_id}/edit_pair.json
      ```
      Put its `cut_score`, `flags` and your verdict under `"edit_pair"` in the report.
      jump_cut / internal_cut / readable_props → `re_render_recommended: true`;
      dead_boundary / small light_mismatch → `recommended_action: "ASSEMBLY_FIX_ONLY"`
      with the trim to apply.
```

### Step 4: Voice quality check (dialogue clips only)
```
10. VOICE QUALITY (dialogue clips only, score 0.0-1.0, threshold 0.85):

   Extract the dialogue audio stem ({clip_dir}/dialogue.wav) and analyze it.
   This must sound like a REAL ACTOR performing in a Pixar movie.

   Check for these ROBOTIC speech indicators (any = major issue):
   a. MONOTONE PITCH: Voice stays at the same pitch without natural rises/falls.
      Real speech has melody — questions rise, statements fall, emphasis varies.
   b. FLAT PROSODY: Even rhythm without natural pauses, hesitations, or emphasis.
      Real speech speeds up when excited, slows for emphasis, pauses for effect.
   c. METALLIC/SYNTHETIC TIMBRE: Voice sounds processed, buzzy, or artificial.
      Should sound like a warm human voice, not a computer.
   d. UNNATURAL BREATH: Missing breath sounds between phrases, or mechanical breathing.
      Real speakers breathe — especially during emotional delivery.
   e. EMOTION MISMATCH: Voice emotion doesn't match the scene's emotional beat.
      A warm loving line can't sound cold. An urgent line can't sound relaxed.
      Compare against the emotional_beat field in the shot list.
   f. PACING: Dialogue pacing must match the character and moment.
      An elderly grandmother speaks differently from an excited child.
   g. CAST SEPARATION (voice-casting skill — The Clockwork Moth: "narrator, child, and
      old woman all had the same voice"): compare this line's f0_median and rate with
      the other roles in `audio/voices/lineup/separation.json`. Nearest other role
      < 4 semitones away in the same voice class, a child/elderly role in an adult-class
      voice, or the narrator in a class a character uses → score ≤ 0.6,
      `corrections: ["RECAST <role>"]`. Within-line prosody passing does NOT clear this.

   Use ffmpeg to extract audio analysis:
   ```bash
   # Extract pitch contour (should show variation, not flat line)
   ffmpeg -i {clip_dir}/dialogue.wav -af "aresample=16000" -f f32le - | \
     python3 -c "import struct,sys; data=sys.stdin.buffer.read(); samples=[struct.unpack('f',data[i:i+4])[0] for i in range(0,len(data),4)]; print(f'samples={len(samples)} max={max(abs(s) for s in samples):.3f}')"
   ```

   For narration-only clips: score is N/A (skip this category, don't count in weighted score).
```

### Step 5: Cinematic quality check
```
11. FILM QUALITY (score 0.0-1.0, threshold 0.80):

   This clip must look like a frame from a GREAT animated movie, not just an
   artifact-free one. Check:
   - Color palette: Appealing, consistent with mood (warm for morning, cool for night)
   - Depth of field: Proper for the shot type (shallow for close-up, deep for wide)
   - Lighting: Cinematic, motivated light sources, proper shadows and highlights
   - Character posing: Natural, expressive, not stiff or awkward
   - Composition: Visual weight balance, rule of thirds, leading lines
   - Environment detail: Rich, lived-in, textured (not flat or sparse)
   - Atmosphere: Dust motes, steam, light rays — the small details that make it feel alive
```

### Step 6: Score calculation
Weighted scoring:
```
hands_limbs:           weight 0.12 (critical — most common artifact)
face_expression:       weight 0.12 (critical — viewer focus)
character_identity:    weight 0.12 (high — consistency is key)
duplication:           weight 0.04 (medium — less common but jarring)
motion_quality:        weight 0.10 (high — temporal artifacts)
lip_sync:              weight 0.10 (high — dialogue sync is essential)
texture_visual:        weight 0.04 (medium — distracting but tolerable)
composition:           weight 0.04 (medium — affects flow)
continuity:            weight 0.12 (critical — makes or breaks the movie)
voice_quality:         weight 0.10 (high — robotic voice ruins immersion)
film_quality:          weight 0.10 (high — overall cinematic polish)

overall_score = sum(score * weight for each category)

Note: For narration-only clips (no dialogue), redistribute voice_quality weight
proportionally across other categories.
```

### Step 7: Verdict
```
ALL of categories 1-10 must score >= 0.85 AND
category 11 must score >= 0.80 AND
overall_score >= 0.85 → PASS

Any category 1-10 below 0.85 → FAIL
Category 11 below 0.80 → FAIL
Any critical issue → FAIL regardless of scores
Voice quality below 0.70 = critical (robotic voice is unacceptable)
```

No CONDITIONAL PASS — either it's good enough for a great movie or it's not.

### Step 7: Failure response
On FAIL, generate:
1. Specific prompt corrections addressing each critical/major issue
2. Reference to the relevant rule from `knowledge/minimax_h3_rules.md`
3. For continuity failures: specify exactly what must match the previous clip
   (e.g., "bowl must be visible on the table, matching S04_001 last frame")
4. Suggestion for shot type change if the issue is inherent to the shot
5. Updated retry count

On max retries (5), escalate:
```json
{
  "escalation": "DIRECTOR_REVIEW",
  "shot_id": "S01_003",
  "reason": "Failed QA 5 times. Issues persist despite corrections.",
  "recommendation": "Consider redesigning this shot."
}
```

### Step 9: Report format
The qa_report.json MUST include all 11 category scores:
```json
{
  "shot_id": "S04_002",
  "verdict": "PASS or FAIL",
  "scores": {
    "hands": {"score": 0.95, "pass": true, "detail": "..."},
    "face": {"score": 0.90, "pass": true, "detail": "..."},
    "character_identity": {"score": 0.88, "pass": true, "detail": "..."},
    "duplication": {"score": 1.0, "pass": true, "detail": "..."},
    "motion": {"score": 0.92, "pass": true, "detail": "..."},
    "lip_sync": {"score": 0.90, "pass": true, "detail": "..."},
    "texture_visual": {"score": 0.93, "pass": true, "detail": "..."},
    "composition": {"score": 0.91, "pass": true, "detail": "..."},
    "continuity": {"score": 0.87, "pass": true, "detail": "..."},
    "voice_quality": {"score": 0.88, "pass": true, "detail": "Orpheus 3B, natural prosody, emotion matches scene beat"},
    "film_quality": {"score": 0.85, "pass": true, "detail": "..."}
  },
  "edit_pair": {"previous_shot": "S04_001", "cut_score": 0.86, "flags": [],
                "readable_props_ok": true, "note": "cut on the head turn, handles 1.1s/1.0s"},
  "issues": [],
  "corrections": []
}
```
