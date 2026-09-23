---
name: qa-inspection
description: Skill for QA inspection of generated video clips and images
whenToUse: When reviewing a generated clip or image for quality issues
---

# QA Inspection Skill

Used by the QA Inspector agent to systematically review each generated clip.

## Inspection Protocol

### Step 1: Extract frames
Extract frames from the video at 2 FPS for analysis.
**Always use an absolute path under the run directory** — never a relative `output/qa/...`
path (CWD is `$DSH_DIR`, so relative paths land in the harness sandbox and the pipeline
gate never finds them):

```bash
# {run_dir} = parent of clips/ (e.g. .../output/run_YYYYMMDD_HHMMSS)
mkdir -p {run_dir}/qa/{shot_id}
ffmpeg -i {clip_path} -vf fps=2 {run_dir}/qa/{shot_id}/frame_%04d.png
```

**Write the structured report to BOTH of these absolute paths** (the normaliser
searches them, newest mtime wins):

1. `{run_dir}/qa/{shot_id}/qa_report.json`  ← preferred
2. `{clip_dir}/qa_report.json`              ← also searched

Do **not** write `qa_report.json` under `$DSH_DIR/output/qa/...`.

### Step 2: Prepare analysis prompts
For each extracted frame, send to Qwen2.5-VL with this analysis prompt:

```
Analyze this frame from an AI-generated animated video for quality issues.
The frame should match this description: {original_prompt}
Character reference: {character_reference_description}

Score each category 0.0-1.0 (1.0 = perfect):

1. HANDS & LIMBS: Count fingers on each visible hand. Check for merging, extra limbs,
   impossible positions, POV intrusion.
2. FACE & EXPRESSION: Check symmetry, consistency with character reference,
   distortion, morphing artifacts.
3. CHARACTER IDENTITY: Compare against reference sheet. Check hair, clothing,
   accessories, distinguishing features.
4. TEXTURE QUALITY: Check for swimming textures, resolution drops, banding,
   visual noise.
5. COMPOSITION: Does the framing match the Director's specification?
   Check character positioning, background consistency.

For each issue found, provide:
- Category
- Severity (critical / major / minor)
- Location in frame (e.g., "upper-right quadrant")
- Description
- Suggested prompt fix
```

### Step 3: Temporal consistency check
Compare consecutive frames for:
- Character identity drift (features changing between frames)
- Texture stability (swimming or shifting textures)
- Motion naturalness (jitter, teleporting, impossible physics)
- Background consistency (elements appearing/disappearing)

### Step 4: Score calculation
Weighted scoring:
```
hands_limbs:           weight 0.20 (critical — most common artifact)
face_expression:       weight 0.20 (critical — viewer focus)
character_identity:    weight 0.20 (high — consistency is key)
motion_quality:        weight 0.15 (high — temporal artifacts)
texture_visual:        weight 0.10 (medium — distracting but tolerable)
composition_continuity: weight 0.10 (medium — affects flow)
audio_quality:         weight 0.05 (low — we overlay audio separately)

overall_score = sum(score * weight for each category)
```

### Step 5: Verdict
```
overall_score >= 0.85 → PASS
overall_score >= 0.70 → CONDITIONAL PASS (note issues for post-production)
overall_score < 0.70  → FAIL (must regenerate)
```

### Step 6: Failure response
On FAIL, generate:
1. Specific prompt corrections addressing each critical/major issue
2. Reference to the relevant rule from `knowledge/minimax_h3_rules.md`
3. Suggestion for shot type change if the issue is inherent to the shot
4. Updated retry count

On max retries (3), escalate:
```json
{
  "escalation": "DIRECTOR_REVIEW",
  "shot_id": "S01_003",
  "reason": "Failed QA 3 times. Issues persist with hand artifacts in this composition.",
  "recommendation": "Consider redesigning this shot to avoid frontal hand positioning."
}
```
