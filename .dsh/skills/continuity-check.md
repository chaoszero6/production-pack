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

## Continuity Fix Strategies

### Minor Issues (fix in post):
- Color grade clip N+1 to match clip N
- Crop/pan slightly to match character position
- Add a transition (dissolve) to mask the discontinuity

### Major Issues (must regenerate):
- Character identity completely different
- Location doesn't match at all
- Physics violation (character suddenly on wrong side)

### Prevention (best approach):
- Use "first_from_previous" keyframe strategy — the previous clip's last frame
  becomes the next clip's first frame
- Use reference images from the same character/location sheets
- Keep the same identity anchors in every prompt

## Scoring
```
boundary_match: 0.0-1.0  (frame similarity at boundaries)
character_match: 0.0-1.0  (identity consistency)
location_match: 0.0-1.0   (environment consistency)
motion_match: 0.0-1.0     (action continuity)

continuity_score = weighted average
threshold: 0.80 for PASS
```
