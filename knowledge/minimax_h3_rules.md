# MiniMax H3 — Prompt Engineering Rules & Known Artifacts

> Comprehensive knowledge base for the Screenplay Reviewer and QA Inspector agents.
> Sources: MiniMax official docs, HuggingFace discussions, community reports, fal.ai guide, ComfyUI forums.

---
## 0. THE H3 ENVELOPE — verified 2026-10-16 (read before anything else)

What the local H3 actually is (sources: `comfy_extras/nodes_minimax_h3.py`,
`comfy/text_encoders/minimax.py`, the MiniMaxAI/MiniMax-H3 model card, the official
`VIDEO_PROMPT_WRITING_GUIDE_base_en.md` / `_ref_en.md` shipped with the H3 Prompt Writer node):

- **Model.** H3-Base open weights: a 33B dense single-stream transformer; text and images are read
  by a Qwen3-VL-32B encoder (50 layers). Two variants on 207: `ref2va` (omni-reference, ≤ 9 images)
  and `fl2va` (0/1/2 frames → t2va / i2va / l2va / fl2va). CFG-distilled. Native 32 kHz stereo
  audio (the pipeline discards it).
- **Canvas.** 768 short edge, area cap 1344×768, 24 fps. Frame count = 17k+5; trained range
  124–362 frames (5.2 s – 15.1 s). `generate_video_direct.py` rounds UP; the editor trims.
- **Speed.** Turbo LoRA (larryvrh): 4–8 steps, 8 is best, strength 1.0; its own README names
  "audio and fast, intense motion" as the weak spots. Ours: 8 steps, ddim/beta, ≈ 6 min per 10 s.
- **The prompt is prose read by Qwen3-VL.** The node has no token cap, but the OFFICIAL guide sizes
  `detailed_description` at 350–500 words and a whole Context-IR output lands around 600–900.
  H3 has no concept of pixels, percentages, frame numbers, hex colours, NCC, luma budgets,
  "exactly ten" or "92 px of 768". Those words displace the description it does read.
- **Every ref2va image is a CONTENT source.** H3 copies what it sees in a reference — people,
  props, costumes, weather — into the clip. A reference is never "an anchor to end on".
- **Camera vocabulary (official).** Push In / Pull Out, Pan Left/Right, Truck Left/Right,
  Tilt Up/Down, Pedestal Up/Down, Arc Shot, Tracking Shot, Static Shot, Shake Slightly/Strongly,
  POV, Roll — as a sentence: "The camera pushes in with small amplitude at slow speed toward …".
  Nothing else ("monotone 1.00→1.15 cumulative scale, no pull-back") is understood. A move
  must name its END state with the subject still framed at the shot's size — "slow tilt up"
  alone drifted S01_004 into four seconds of empty sky with the kite gone by 3 s. A MOVING prop
  (kite, bird, thrown object) leaves the frame within 2–3 s whatever the prompt says — take 5 had
  "the kite stays in the top of frame from first frame to last", the end state and the negatives,
  and still lost it at 2 s. Keep such a prop "holding station" under a static camera, or do not
  require it; never make its exit the cut point.

### 0.1 What the Cinderquay run proved (run_20261011_130959 — S01_001 ×10 takes, S01_002 ×6, 0 passes)

| Observed in the render | Root cause | Rule now enforced |
|---|---|---|
| Olive-coat crowd in wool mufflers, a man reading a ledger beside a lantern — in a shot that banned mufflers and had no reader | `next_clip_firstframe.png` (S01_002's first frame, which shows exactly those figures) was fed to ref2va as an "end anchor" | Continuity frames are **never** ref2va references. A previous/next frame enters as the i2va first frame or the fl2va last frame, or not at all. `generate_video_direct.py` refuses them. |
| "extreme-wide, high angle, slow push in" became a crowd-level medium shot by 1.5 s in all 10 takes | ref2va does not hold a plate's framing — it re-frames toward the subjects it was given | Composition-critical shots (establishing, placed groups, 0-character plates) render a FIRST FRAME and run as i2va. H3 only animates. `shot_list_lint.py --fix` switches 0-character "reference" shots to `first_frame_only`. |
| 11 → 14 → 8 figures against "exactly ten"; one face repeated across a clone cluster | crowds are drawn as clone clusters; counts are not controllable | ≤ 3 named characters per clip. Extras are "a few distant figures, backs to camera" — never counted, never costumed one by one. |
| Crowd "materialised at frame 7" against "empty until frame 30" | frame-numbered reveals are not understood; figures pop in | No frame numbers, no timed reveals. One entrance per clip, one subject, medium framing, in seconds (± 1 s). |
| Fused mitten hands and repeated faces on figures ~300 px tall | at 768p faces and hands are unreadable below medium framing | Faces/hands are prompted AND scored only at medium framing or closer. Wide = backs, silhouettes. |
| Prompt grew 1 301 → 3 114 words over ten takes; every take failed | QA corrections were appended, never applied; numeric gates replaced description | Each take is REWRITTEN from the shot list: ≤ 900 words total, `detailed_description` 350–500, the three highest-priority structural corrections applied, the rest dropped. |
| S01_002 was planned `composited_i2v`; a good first frame was rendered; the clip ran as ref2va without it | the reviewer's `h3_mode` default (ref2va) overrode the shot list | Mode comes from the shot list (`run.sh` passes it); a rendered first frame forces i2va/fl2va in `generate_video_direct.py`. |
| A 10 s shot asked for four montage beats (nets, bell, kite, baby) and an entrance at 9.0 s | a montage was written as one shot | One beat per clip (`shot_list_lint.py` blocks `montage_in_one_shot`). |
| S02_002 (Kite humming) rendered with no audio track and no stem; every clip since 23:55 was detected as narration | a double-quoted comment inside the bash python snippet ended the string, the d-tag became a shell redirection and `|| echo no` answered for every clip; the stem script was still the Moth's Orpheus-only one; the i2va graph never passed the stem to H3 | `dialogue_stem.py` renders the shot's lines from `voice_config.json` through the TTS cluster (line-up stem as the Chatterbox reference when the cast reference is missing, Kokoro fallback); i2va anchors the stem with `MiniMaxH3AddGuide` at `anchor_s` so the mouth follows the audio; no double quotes inside bash-embedded python |
| S02_005 (static 5 s ECU insert of an unlit storm-glass jar) rendered the jar brightening over the clip in three identical takes; everything else correct | H3 animates a glowing-prone object on a static frame by lighting it; the composite also referenced the plate with lit windows | an exposure ramp on a correct static prop is an ASSEMBLY grade (inspector rule), the planner writes the ledger state positively and drops contradicting references; accepted with a grade note |
| S01_004 take 6 ran as ref2va (shot-list mode `reference`, so no keyframe was rendered): half-body for 1.2 s, then a long shot for the remaining 65 % | ref2va re-frames toward its subjects; every ref2va take so far drifted wide, every i2va take held its framing | **Every shot is keyframed.** `run.sh` maps `reference` to i2va and always renders the keyframe; `shot_list_lint.py --fix` rewrites `reference` + characters to `composited_i2v`; ref2va remains only as the fallback when no keyframe exists. |
| S01_004 (medium of Aurel on the cliff path, i2va from a correct keyframe) held its framing for 2 s, then hard-cut to a wide of the quay with the bell — three takes in a row | the prompt's `[Shot 1]` opened with a paragraph describing the previous clip's quay-level layout ("the square as the last shot left it: the bell centre, the lamp, the bollards…") before the cliff-path medium; H3 rendered both compositions and cut between them | **One composition per prompt.** `[Shot 1]` opens on this shot's own framing and subject; continuity from the previous clip is STATE (light, weather, prop states, costume), never a layout paragraph; things outside the framing are one background clause at most. |
| The QA gate demanded all 11 categories ≥ 0.85 including "figure ≤ 12 % of frame height", "wall gradient energy ≥ 0.75 of plate", "luma within ± 10 %" | the inspector invented pixel gates and the gate trusted them | The inspector scores; `normalise_qa_verdict.py` decides: structural categories ≥ 0.70, overall ≥ 0.75 → PASS. Soft categories go to the assembly ledger. Invented numeric gates are evidence, not rules. |


### 0.3 Keyframe engines — where the position and size of a figure come from
`FRAME_ENGINE=qwen` (default): Qwen Image 2.1 with the design sheets and the plate as references
(`generate_frames.py`). Identity is good; placement is prompt-only (no boxes).
`FRAME_ENGINE=ideogram4`: `pipeline/ideogram_frames.py`, two passes. Pass 1 renders the layout
with the LOCAL Ideogram 4 through ComfyUI's native graph (the official "Ideogram v4: Text to
Image" template: conditional + unconditional nvfp4 UNETs in a `DualModelGuider` at cfg 7,
`CFGOverride` 3 for the last 30 %, Qwen3-VL-8B encoder `type ideogram4`, `Ideogram4Scheduler`
20 steps / mu 0 / std 1.75, Flux2 VAE) from a structured JSON caption whose `elements[].bbox`
= `[y_min, x_min, y_max, x_max]` in 0–1000 pins every figure's position and height — the one
control the Qwen path lacks. Pass 2 hands that layout image to Qwen Image 2.1 as `<Picture 1>`
with the character sheets and asks for the same composition with the sheet identities, because
Ideogram 4 is text-to-image only (official docs: no reference images, no edit, no character
reference — its figures are generic). The result goes to H3 as the i2va first frame (or fl2va
first+last). Models on 207: `/mnt/ChaosWares1/comfyui/models` (registered in
`extra_model_paths.yaml`): `ideogram4_nvfp4_mixed` + `ideogram4_unconditional_nvfp4_mixed`
(Comfy-Org/Ideogram-4), `qwen3vl_8b_nvfp4`; VAE `flux2-vae`. Licence: Ideogram 4
Non-Commercial. The planner (`run.sh` 2c) writes `placements` / `background` / `style` into
`frame_spec.json` for it; without the models the script exits 4 and `run.sh` falls back to the
Qwen path.

### 0.2 Narration
The official voiceover pattern is `The narrator (S1) says in an off-screen voiceover: <d>…</d>
while <Subject 1>'s lips remain completely closed.` The pipeline does NOT use it: `run.sh`
treats any `<d>` as dialogue (it would trigger a TTS stem and lip sync). Narration clips keep
the closed-lips description and the stem is laid in post. Make the PICTURE illustrate the
narration line (the Director's job: S01_002's narration was a kite and a laughing baby while
the shot showed a whale hunt).

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
- `detailed_description`: 350-500 words for generation tasks (official); whole prompt ≤ 900 words
- NO frame numbers, pixel sizes, percentages, hex colours or "exactly N" counts — H3 does not read them (§0)
- Camera moves ONLY in the official vocabulary (§0): "The camera pushes in with small amplitude at slow speed"
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
- Music is post only: H3 "re-sings" or restarts music per clip. In this pack ALL audio —
  dialogue stems, narration, music, beds, SFX — is mixed on the film timeline by
  `pipeline/assemble.py`; H3's native track is discarded (clips contribute picture only).
- Keep every take; corrections are new takes (`_t2`, `_t3`), approved files never overwritten.
- Frame-exact cutting: `frames = round(duration * fps)`; `assemble.py` trims the masters by
  frame count from the editor's decisions and concatenates with the demuxer; no transitions.
  Stems are never trimmed — they are placed at `film_start + anchor_s − src_in`.
- Upscaler writes video only (`clip_4k.mp4`); the audio is laid in at assembly, never muxed
  back from the clip. Blend filters (glow, screen) run in RGB (`format=gbrp`); in YUV they
  tint magenta.
- "'Verified' means measured. A bad number is a reason to look at the frames, not to
  discard a take blindly." QC reports separate passed / failed / not verified.
