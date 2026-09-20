---
name: prompt-review
description: Skill for reviewing and optimizing MiniMax H3 video generation prompts
whenToUse: When reviewing or writing prompts for MiniMax H3 video generation
---

# Prompt Review Skill

This skill is used by the Screenplay Reviewer agent to validate and optimize
prompts before they are sent to MiniMax H3 for video generation.

## Review Process

### Step 1: Load the shot specification
Read the shot from the Director's shot list (see shot-list-schema skill).

### Step 2: Check the shot type safety matrix
Consult `knowledge/minimax_h3_rules.md` Section 4 — Shot Type Safety Matrix.
If the requested shot type is unsafe for the content:
- Suggest a safer alternative shot type
- Or suggest splitting into multiple shots

### Step 3: Determine dialogue vs narration audio strategy

**CRITICAL DECISION — this affects the entire prompt structure:**

**If the shot has CHARACTER DIALOGUE (lips must move):**
- Audio Producer has ALREADY generated dialogue audio before this step
- Include the dialogue audio as `<Audio N>` in subject_definitions
- Include `<d>[English] dialogue text</d>` tags with speaker labels `(S1)`, `(S2)` in detailed_description
- H3 will generate lip-synced mouth animation matching the audio
- Include speaker in retention_analysis: `<Audio 1>: reference - voice timbre for Maya`

**If the shot has NARRATION ONLY (lips must NOT move):**
- Do NOT include any `<Audio>` reference for the narration
- Do NOT include any `<d>` dialogue tags
- In detailed_description, describe visible characters doing SILENT actions:
  "lips gently closed", "contemplative expression", "gazes silently at the horizon",
  "walks in quiet thought with mouth relaxed"
- Narration audio will be overlaid in post-production by Post-Production Editor

**If the shot has BOTH dialogue AND narration:**
- Include `<Audio>` ref + `<d>` tags ONLY for the dialogue portions
- Narration overlaid in post — character should NOT lip-sync to narration
- In the prompt, clearly separate: the part where the character speaks vs the part
  where they are silent while narration plays

### Step 4: Build the structured prompt

**For ref2va** (most clips), use the 6-section H3 format:
```
subject_definitions:
<Subject 1> is {character with full identity anchors}.
<Subject 2> is {location/environment description}.
<Audio 1> is {ONLY if dialogue — the pre-generated character voice audio}.

summary:
[reference generation] One paragraph summarizing the target video.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - {what is retained}.
<Audio 1>: reference - {ONLY if dialogue — voice timbre description}.

detailed_description:
{Style description.}
[Shot 1] {Shot description with camera, action.}
{IF DIALOGUE: <Subject 1> (S1) says, <d>[English] Exact dialogue here.</d>}
{IF NARRATION: <Subject 1> gazes silently, lips gently closed, contemplative.}
Constraints: no duplicate characters, no distorted hands, ...

overall_soundscape:
{Ambient/physical sounds. NOT narration — narration is post-only.}

non_diegetic_music:
{Score description — instrumentation, tempo, dynamics.}
```

**For i2va / fl2va**, use timecoded segments:
```
[0s-3s] {action description, IF dialogue include <d> tags, IF narration describe silent action}
[3s-6s] ...
```

### Step 5: Run the pre-generation checklist
Go through every item in `knowledge/minimax_h3_rules.md` Section 5.
**ADDITIONAL CHECK:** Verify dialogue/narration handling is correct:
- [ ] Dialogue clips have `<Audio>` ref + `<d>` tags (lip sync intended)
- [ ] Narration clips have NO `<Audio>` ref, NO `<d>` tags (no lip sync)
- [ ] Characters visible during narration are described doing silent actions

### Step 6: Assign reference roles
For each reference in the prompt:
```
<Subject 1> ({filename}): {role — character identity}
<Subject 2> ({filename}): {role — environment}
<Audio 1> ({filename}): {ONLY for dialogue — voice reference for lip sync}
```

### Step 6: Output the review result

```json
{
  "shot_id": "S01_003",
  "status": "APPROVED | REVISED",
  "original_prompt": "the Director's initial description",
  "final_prompt": "the structured, optimized prompt",
  "negative_prompt": "all negative constraints",
  "reference_assignments": [
    {"image": "maya_34view.png", "role": "character identity for Maya"}
  ],
  "changes_made": [
    "Changed shot type from wide to medium close-up (face detail required)",
    "Added explicit hand position (was unspecified)",
    "Added temporal decomposition blocks"
  ],
  "risk_flags": [
    "Character is gesturing — watch for hand artifacts in QA"
  ],
  "checklist_passed": true,
  "h3_mode": "first_last_frame",
  "estimated_quality": "high"
}
```
