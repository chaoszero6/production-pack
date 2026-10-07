# MiniMax H3 — Prompt Engineering Rules & Known Artifacts

> Comprehensive knowledge base for the Screenplay Reviewer and QA Inspector agents.
> Sources: MiniMax official docs, HuggingFace discussions, community reports, fal.ai guide, ComfyUI forums.

---

## 1. CRITICAL ARTIFACTS — Must Prevent

### 1.1 Character Duplication
**Problem:** H3 generates duplicate copies of the same character when multiple characters are
described vaguely or when a single character is described in relation to a scene element.
**Triggers:**
- "A person walking near a mirror" (generates the person AND a reflection as separate character)
- "Two friends talking" without explicit differentiation
- Wide shots with multiple similar-looking characters
**Prevention:**
- Name every character explicitly: "Maya stands on the LEFT, Kai stands on the RIGHT"
- Add explicit count: "exactly two characters in frame"
- Negative prompt: "no duplicate characters, no extra people, no clone, no mirror reflection unless intended"
- Use reference images with explicit role assignment

### 1.2 Hand & Limb Anomalies
**Problem:** Hands merge into background, body, or objects. Extra fingers. POV-style hand intrusion.
**Triggers:**
- Characters reaching toward camera (hands enter POV perspective)
- Characters holding small objects (fingers merge with object)
- Wide shots where hands are low-detail
- Characters facing camera with arms extended forward
**Prevention:**
- Specify hand positions: "hands at sides", "hands clasped behind back", "right hand on hip"
- For close-ups of hands: use dedicated hand close-up shot
- Avoid frontal shots with extended arms toward camera
- Negative prompt: "no distorted hands, no extra fingers, no merged limbs, no hands reaching toward camera"
- Prefer 3/4 angles where hands are more naturally positioned

### 1.3 Face Distortion in Wide Shots
**Problem:** H3 heavily distorts faces when generating wide/full-body shots. Facial features become
asymmetric, blurry, or morphed.
**Triggers:**
- Full-body shots where face is small in frame
- Group shots with multiple faces
- Fast camera movements in wide compositions
**Prevention:**
- RULE: Use ONLY close-ups, medium close-ups, and half-body shots for shots requiring facial detail
- For wide/establishing shots: use back views or rear 3/4 angles
- If a wide shot must show a face: use subsequent close-up to establish identity
- Never request frontal close-ups of distant characters in a wide composition

### 1.4 POV Hand Intrusion
**Problem:** When a character faces the camera, their hands sometimes extend forward
toward the camera as if it were a first-person view, breaking the third-person perspective.
**Triggers:**
- Character facing camera with any hand/arm activity
- "Character reaches out" + frontal angle
- Gesturing during dialogue in close-up
**Prevention:**
- For frontal shots: keep hands below frame or specify "hands in lap / at sides"
- Use 3/4 or profile angles for gesturing characters
- Add: "third-person camera perspective, character does not interact with camera"

---

## 2. HIGH-PRIORITY ARTIFACTS

### 2.1 Texture Instability / Visual Drift
**Problem:** Clothing textures, hair, and surface details shift or swim during the clip.
Character appearance can drift from the start to end of a clip.
**Triggers:**
- Longer clips (>8 seconds)
- Complex textures (patterned clothing, detailed hair)
- Fast motion
**Prevention:**
- Keep clips to 6-8 seconds for best stability
- List EVERY identity anchor: "long black hair, silver crown, indigo ribbon, pale blue hanfu,
  translucent outer robe" — named details anchor identity
- Use reference images for identity locking
- For characters with complex outfits: simplify or use solid colors

### 2.2 Audio Quality Issues
**Problem:** Native H3 audio is often distorted, noisy, or contains timbre crosstalk
between multiple speakers.
**Triggers:**
- Multi-speaker dialogue in a single clip
- Complex sound environments
- Low step counts (8 steps = choppy audio)
**Prevention:**
- RECOMMENDATION: Generate video WITHOUT relying on native audio
- Overlay all dialogue, narration, and music separately using CosyVoice 3 / Kokoro
- If native audio is needed: limit to single speaker per clip
- Use minimum 50 steps for any clip requiring decent native audio

### 2.3 Large-Scale Action Artifacts
**Problem:** Complex action sequences (fights, chases, crowds) introduce motion artifacts
and unnatural movements.
**Triggers:**
- Multiple moving characters
- Fast camera movements combined with fast character motion
- Physics-heavy scenes (explosions, water, flying)
**Prevention:**
- Break complex actions into simpler 4-6 second sub-clips
- One major motion per clip
- Describe motion physically: "arm rises slowly to the right" not "dramatic gesture"
- Use static or slow camera for fast character motion (and vice versa)

### 2.4 Spatial Geometry Collapse (Room Morphing)
**Problem:** When a character moves away from camera (especially toward a door/exit),
H3 loses spatial coherence — the room geometry warps, furniture shifts, walls deform,
and architectural elements (doors, windows, archways) dissolve or vanish entirely.
The environment "melts" once the character is no longer anchoring the scene.
**Triggers:**
- Character walking away from camera toward a door or exit
- Character exiting frame entirely (room left without an anchor subject)
- Wide shots where the character becomes small relative to the environment
- Transition from medium/close-up to wide as character moves away
- Clips longer than 8 seconds with significant character displacement
**Prevention:**
- CRITICAL: Add explicit environment anchoring to EVERY exit/entrance shot:
  `"The workshop interior maintains fixed geometry throughout — walls, doorframe,
  furniture, and all mounted objects remain stationary and unchanged."`
- Pin specific landmarks: "The grandfather clock stays on the LEFT wall.
  The arched wooden door remains at CENTER. The workbench stays at RIGHT."
- Use [START-END] time blocks to describe the room as STATIC while character moves:
  `[0s-END] STATIC ENVIRONMENT: walls, floor, ceiling, door, furniture, clocks maintain
  exact position and appearance throughout the entire clip.`
- For exit shots: keep clip to 6 seconds max — geometry breaks after ~7s
- Prefer cutting BEFORE the character fully exits — match-cut to next shot
  showing the empty room from a DIFFERENT angle (resets H3's spatial model)
- If the door must close: describe it as a single physical action:
  "Character pulls door shut behind them" rather than showing the room after
- Add negative: "no room deformation, no morphing walls, no vanishing doors,
  no shifting furniture, no melting textures"
**Known Failure Case:** S02_008 — character exits through arched door, room textures
swim, geometry warps, door dissolves entirely by final frames. Root cause: no
environment anchoring in prompt, clip too long (11.5s), no spatial landmarks pinned.

---

## 3. PROMPT FORMAT — MiniMax H3 Official Structure

MiniMax H3 uses TWO distinct prompt formats depending on the mode.

### 3.1 IT2V / I2VA Format (Image-to-Video, First-Last-Frame)
Uses temporal segmentation with `[Xs-Ys]` timecodes:
```
[0s-2s] A golden retriever puppy sleeps curled on sunlit wooden floor, morning light streaming through window.
[2s-5s] Puppy slowly wakes, stretches front paws, yawns with tiny squeak, sits up with curious eyes.
```

Rules:
- Use `[Xs-Ys]` brackets marking event timing
- Cover full duration with contiguous segments (no gaps)
- Describe MOTION not static frames (use action verbs, present tense)
- Include: setting, subjects, action, camera movement, mood
- ~5s: 2-3 segments, ~8s: 3-4 segments, ~10s: 4-5 segments, ~15s: 5-8 segments

### 3.2 Ref2VA Format (Reference-to-Video — 6-Section Structure)
This is the structured format for reference-based generation. It has 6 required sections:

```
subject_definitions:
<Subject 1> is [reusable visible content — person, scene, style, action].
<Subject 2> is [another reusable element].
<Picture N> is [concrete frame anchor].
<Video N> is [whole-video structure source].
<Audio N> is [audio signal — voice, music, effects].

summary:
[task type(s)] One paragraph summarizing the target video and how references are used.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - identity, clothing, features retained.
<Audio 1>: reference - voice timbre used as guide.

detailed_description:
Style description (1-2 sentences). Then shot-by-shot:
[Shot 1] Medium shot of <Subject 1>. Camera pushes in slowly. <Subject 2> (S1) says, <d>[English] Dialogue here.</d>
[Shot 2] At 00:04.000, close-up of <Subject 2> (S1). She continues, <d>[English] More dialogue.</d>

overall_soundscape:
1-4 sentences of ambient/physical sounds in the scene.

non_diegetic_music:
1-3 sentences describing the score (instrumentation, tempo, dynamics).
```

### 3.3 Reference Tag System
| Tag            | Meaning                                           |
|----------------|---------------------------------------------------|
| `<Subject N>`  | Reusable visible content (person, scene, style)   |
| `<Picture N>`  | Reference image as frame anchor / composition     |
| `<Video N>`    | Reference video as edit source / motion guide     |
| `<Audio N>`    | Audio signal (voice, SFX, music)                  |

Reference limits: up to 9 images + 3 video clips + 3 audio files (12 total).
Audio cannot be the sole reference — pair with image or video.

### 3.4 Shot & Camera Notation
- `[Shot 1]` — first shot (no timestamp needed)
- `[Shot 2] At 00:03.500, the camera cuts to...` — later shots with timestamps
- Camera: Push In, Pull Out, Pan, Truck, Tilt, Pedestal, Arc, Tracking, Static, Shake, POV, Roll
- Format: "The camera [motion] with [amplitude] at [speed]"

### 3.5 Dialogue Format
```
(S1) = Speaker 1, (S2) = Speaker 2
<d>[English] exact spoken words here</d>
```

### 3.6 Retention Markers
**Visible:** `fully_preserved` · `partially_preserved` · `attribute_transfer` · `weak_reference`
**Audio:** `fully_copy` · `partially_copy` · `reference` · `weak_reference`

### 3.7 Task Type Markers (for summary section)
Combine with ` + `: `keyframe completion`, `reference generation`, `video editing`,
`video continuation`, `audio reuse`, `audio reference`

### 3.8 Quality Standards
- `detailed_description`: 350-500 words for generation tasks
- Be concrete: composition, subject state, actions, camera movement, sound
- Output ONLY the formatted prompt — no commentary
- Use real camera terms: "shallow depth of field", "rack focus", "dolly push-in"
- Describe motion physically, not as effect names

### 3.9 Identity Anchors (REQUIRED in subject_definitions)
List ALL distinguishing features in the `<Subject>` definition:
```
<Subject 1> is Maya, a 12-year-old girl with half-up long black hair held by a silver pin,
amber eyes, warm brown skin, wearing a pale blue layered hanfu with translucent outer robe,
indigo silk ribbon at waist, silver crescent crown, and a small star-shaped birthmark on left cheek.
```

### 3.10 Negative Constraints
Add to `detailed_description` or as a separate line at the end:
```
Constraints: no duplicate characters, no extra people, no distorted hands, no extra fingers,
no merged limbs, no garbled text, no face distortion, no identity drift.
```

---

## 4. SHOT TYPE SAFETY MATRIX

| Shot Type           | Face Detail | Hands Safe | Multi-Character | Recommended |
|---------------------|-------------|------------|-----------------|-------------|
| Extreme close-up    | Excellent   | N/A        | No              | Yes         |
| Close-up            | Excellent   | Careful    | No              | Yes         |
| Medium close-up     | Good        | Careful    | Max 2           | Yes         |
| Half-body / cowboy  | Good        | Yes        | Max 2           | Yes         |
| Medium shot         | Acceptable  | Yes        | Max 2           | Conditional |
| Full-body           | Poor        | Risky      | Max 1           | Avoid faces |
| Wide / establishing | Very poor   | Very risky | Silhouettes only| Back views  |
| Extreme wide        | None        | None       | Silhouettes     | Landscapes  |

---

## 5. PRE-GENERATION CHECKLIST

Before any prompt is sent to MiniMax H3:

- [ ] **Shot type** is safe for the required content (see matrix above)
- [ ] **All characters** are explicitly named and spatially positioned
- [ ] **Identity anchors** are listed for EVERY visible character
- [ ] **Hand positions** are specified OR hands are hidden/out of frame
- [ ] **Temporal structure** with [START-END] time blocks (if >4s)
- [ ] **Reference images** have explicit role assignments
- [ ] **Negative constraints** are appended
- [ ] **Duration** is 6-8 seconds (justified if longer)
- [ ] **Camera movement** is described physically, not as an effect name
- [ ] **No conflicting depth cues** between reference images and prompt
- [ ] **Single major action** per clip (no complex multi-action sequences)
- [ ] **No frontal hand extension** toward camera
- [ ] **Face detail shots** use close-up or medium close-up only
- [ ] **Wide shots** use back views or rear angles for characters
- [ ] **Exit/entrance shots** have explicit environment anchoring (pinned landmarks, static geometry constraint)
- [ ] **Exit clips** are 6 seconds or shorter (geometry collapses after ~7s)
- [ ] **Audio strategy** decided: native H3 audio or separate overlay

---

## 6. QA INSPECTION POINTS

When reviewing generated clips, check for:

### Frame-by-Frame Analysis (sample at 2 FPS)
1. **Hands** — count fingers, check for merging, verify natural positioning
2. **Face** — symmetry, consistency with reference, no morphing
3. **Identity** — character matches reference sheet throughout entire clip
4. **Duplication** — no extra copies of characters
5. **Texture** — no swimming, shifting, or dissolving textures
6. **Motion** — natural physics, no jitter, no impossible trajectories
7. **Composition** — matches Director's shot specification
8. **Continuity** — first frame matches expected input, last frame usable for next clip

### Temporal Analysis
1. **Consistency** — character appearance stable from first to last frame
2. **Pacing** — action matches temporal prompt structure
3. **Transition readiness** — last frame suitable as next clip's first frame

### Audio Analysis (if native audio used)
1. **Clarity** — no distortion or noise
2. **Speaker identity** — no crosstalk
3. **Sync** — audio matches lip movement (if visible)

---

## 7. EDITORIAL RULES — learned from *The Clockwork Moth* (2026-10) and OpenCineAgent

Public review of the Moth: "narrator, child, and old woman all had the same voice … until
the man comes in"; "she looks at a clock, then the time is different in the next cut";
"each cut had a small dissolve in it, likely frame interpolation on the finished cut";
"some clips don't cut — jump cut or continuity problem at the cut. Not all shots can cut
together." Every clip had passed per-clip QA. The defects were between clips.

### 7.1 Jump cuts come from the shot list, not from H3
Nine consecutive shot pairs in the Moth were the same character, same shot size, same
angle (e.g. two "Nana close-up, three-quarter, static" in a row). H3 rendered both
perfectly; the cut between them is a jump by definition.
- Consecutive shots differ by subject, OR ≥ 2 shot-size steps, OR ≥ 30° of angle.
- Never three consecutive shots of the same size. Alternate single / reaction / two-shot / insert.
- Plan reaction singles and inserts in every dialogue scene — they are the editor's cutaways.
- One shot per clip. H3 is never asked for an internal cut ("[Shot 2] at 00:04 cuts to…"
  is forbidden in this pipeline; a new angle is a new shot ID). Internal cuts were the
  top ref2va failure (~1 in 3 takes) until i2v with the keyframe as literal first frame.

### 7.2 Handles: render more than you will use (OpenCineAgent: "render the next valid
length and trim at assembly; trimming per clip keeps cuts from drifting")
Moth dialogue clips were sized to the voice stem (0.4 s lead, 0.6 s tail) — nothing to
trim into, so every cut was a butt-join on a static pose with speech starting on the cut.
- Every clip: ≥ 1.0 s of in-character action before the first beat and after the last
  (`edit.handle_head_s` / `edit.handle_tail_s`), included in `duration_seconds`.
- Dialogue stem anchored at `handle_head + 0.4 s`; pad the stem with leading silence to
  the full clip length so H3's lip sync lines up.
- Prompt the handles explicitly: `[0s-1s] listening, mouth closed, …` and
  `[6.2s-7.2s] holds the look, begins to turn …`. Copy `edit.cut_in` / `edit.cut_out`
  verbatim from the shot list into the first and last temporal blocks.
- H3 valid lengths are 5 + 17k frames at 24 fps (122…362). Round UP to the next valid
  length; the editor trims.

### 7.3 Cut on action / on the look
- Leave shot A mid-movement; enter shot B with the same movement finishing.
- Dialogue: cut from speaker to listener as the speaker's eyes move, or J-cut (next line
  starts 2–6 frames before the picture cut). Never static pose → static pose of the same
  subject.
- Preserve screen direction and the 180° axis; eyelines cross the cut (speaker looks
  screen-right, listener screen-left).
- Exits: cut before the character clears frame; H3 melts an empty room (§2.4).

### 7.4 Readable props are plot information
If the story makes a character look at a clock, the clock's reading must be the same in
every shot of that scene (and advance the right amount after a time skip). H3 draws clock
hands at random unless the prompt pins them ("hands at ten past seven") — and the keyframe
must pin them too. If a value cannot be rendered reliably, frame it unreadable or make the
cue audible (a chime). Keep a `readable_props` ledger per scene; QA compares every visible
readable state to it and to the previous clip.

### 7.5 Interpolation never crosses a cut
RIFE/minterpolate on the ASSEMBLED film blends the last frame of shot A into the first
frame of shot B — a 2–4 frame dissolve at every edit, exactly what viewers saw. Interpolate
per clip (`upscale_clip.py`) and concatenate the masters; a full-film pass
(`upscale_film.py`) takes `--concat-list final/concat.txt` and holds each shot's last
frame instead of blending across. Straight cuts are the default transition; `xfade` only
when the Director scripted a dissolve/fade.

### 7.6 Voice casting is between voices, not within a line
Four of five Moth roles were Orpheus adult-female voices within ~3 semitones; per-line
prosody metrics passed. Cast by voice class (child / teen / adult-f / adult-m / elderly-f /
elderly-m / creature), narrator in a class no character uses, ≥ 4 semitones median-pitch
separation measured on a shared line-up, child and elderly roles from Chatterbox clones or
CosyVoice 3 instructions — never an adult voice with affectation (`.dsh/skills/voice-casting.md`).

### 7.7 OpenCineAgent production notes worth copying
(docs/PRODUCTION_NOTES.md, github.com/ProgramaGrueso/OpenCineAgent)
- One master portrait per character, approved by eye once; every keyframe derives from it.
  "Identity anchors are the same files in every clip. Regenerating anchors per clip
  degrades identity by clip 6." Identity block repeated VERBATIM in every prompt.
- Name colours precisely ("navy blue wool trench coat", not "champagne").
- One speaker per clip; a second voice is (S2), never the same id. ~130 characters per
  line, ~200 per clip — longer lines are rushed or cut.
- "To make someone talk, have them stop what they are doing and look at the camera /
  listener: if they eat while talking, H3 prioritizes eating."
- No text in renders (models invent letters) — titles and subtitles go in post.
- Music is post only: H3 "re-sings" or restarts music per clip.
- Keep every take; corrections are new takes (`_t2`, `_t3`), approved files never overwritten.
- Frame-exact cutting: `frames = round(duration * fps)`, trim video with `-frames:v` and
  audio with `atrim` to the same seconds; concat demuxer with `-c copy`; no transitions.
- Upscaler writes video only; mux the original audio back. Blend filters (glow, screen)
  run in RGB (`format=gbrp`); in YUV they tint magenta.
- "'Verified' means measured. A bad number is a reason to look at the frames, not to
  discard a take blindly." QC reports separate passed / failed / not verified.
