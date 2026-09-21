---
name: shot-list-schema
description: Structured JSON schema for Director's shot list — consumed by all production agents
whenToUse: When creating or parsing a shot list from the Director
---

# Shot List Schema

The Director outputs shot lists in this format. Every downstream agent
(Image Generator, Video Generator, Screenplay Reviewer, Audio Producer, QA Inspector)
consumes this schema.

```json
{
  "movie_title": "string",
  "scene_id": "scene_001",
  "scene_title": "Dawn in the Village",
  "total_shots": 5,

  "shots": [
    {
      "shot_id": "S01_001",
      "scene_id": "scene_001",
      "sequence_order": 1,

      "camera": {
        "shot_type": "wide | medium | close-up | extreme-close-up | pov | over-shoulder",
        "angle": "eye-level | low-angle | high-angle | birds-eye | dutch",
        "movement": "static | pan-left | pan-right | tilt-up | tilt-down | dolly-in | dolly-out | crane-up | tracking | handheld",
        "lens": "wide (24mm) | normal (50mm) | telephoto (85mm+)",
        "focal_description": "shallow depth of field, background softly blurred"
      },

      "duration_seconds": 6,

      "characters_in_frame": [
        {
          "character_id": "char_001",
          "position": "center | left | right | foreground | background",
          "framing": "full-body | half-body | bust | face-only",
          "action": "walking slowly toward camera",
          "expression": "wonder",
          "hand_position": "right hand holding satchel strap, left hand at side",
          "facing": "3/4 toward camera from left"
        }
      ],

      "location_id": "loc_001",
      "time_of_day": "dawn",
      "lighting": "warm golden light from right, soft fill from left",

      "action_description": "Maya walks through the glowing forest, looking up at the luminous canopy with wide eyes",

      "keyframe_strategy": {
        "mode": "first_frame_only | first_last_frame | first_from_previous | text_only | reference",
        "first_frame_source": "generate | previous_clip_last_frame",
        "generate_last_frame": false,
        "reference_images": [
          {"source": "output/characters/maya/maya_34view.png", "role": "character identity"},
          {"source": "output/locations/whispering_canopy/canopy_medium.png", "role": "location mood"}
        ]
      },

      "audio": {
        "dialogue": [
          {
            "character_id": "char_001",
            "line": "It's even more beautiful than I imagined...",
            "word_count": 7,
            "estimated_duration": 2.8,
            "emotion": "awestruck whisper",
            "start_time": 2.0,
            "end_time": 4.8
          }
        ],
        "narration": {
          "text": "Long narration text that may span multiple clips...",
          "span_clips": ["S01_001", "S01_002"],
          "word_count": 45,
          "estimated_duration": 18.0
        },
        "use_native_h3_audio": false,
        "music_mood": "gentle wonder, soft strings",
        "sfx": ["footsteps on soft ground", "magical ambient hum"]
      },

      "transition_to_next": {
        "type": "cut | dissolve | match-cut | whip | fade",
        "description": "cut on Maya's upward gaze"
      },

      "emotional_beat": "wonder — audience should feel the magic of the forest through Maya's eyes",

      "production_notes": {
        "minimax_h3_mode": "first_last_frame",
        "estimated_generation_time": 120,
        "risk_flags": [],
        "priority": "high"
      }
    }
  ]
}
```

## Field Notes

### keyframe_strategy.mode — Choose based on clip needs
- `reference` — **DEFAULT.** ref2va. Use character + location reference images for identity locking.
  Best for mid-scene clips where characters are already established. No frame generation needed.
- `composited_i2v` — i2va with scene compositing. **PREFERRED for new scene openings.**
  Image Generator takes the location ref and uses Qwen Image 2.1 EDIT to composite characters
  into the location at specified positions. The composited image becomes the first frame for i2va.
  Best for: new scenes, specific character placement, establishing shots with characters.
- `first_last_frame` — fl2va. ONLY when this clip needs visual continuity from the previous clip.
  Captures last frame of previous clip as first frame here. Optionally generates a last frame.
- `first_frame_only` — i2va. Generate a first frame via text-to-image (no compositing).
  Good for isolated shots where compositing isn't needed.
- `text_only` — t2va. AVOID. Only for abstract shots with no characters (landscapes, magic effects).

### characters_in_frame.hand_position
ALWAYS specify this. If hands aren't relevant, use "hands not visible" or "hands in pockets".
This prevents MiniMax H3 hand artifacts.

### characters_in_frame.facing
Specify the angle the character faces relative to camera. Avoid "directly facing camera"
for shots where hands are active — use 3/4 angles instead.

### audio.dialogue — Duration Validation
ALWAYS include `word_count` and `estimated_duration` (words / 2.5).
Dialogue `estimated_duration` MUST be <= clip `duration_seconds` minus 1s buffer.
If it doesn't fit, either shorten the line or increase clip duration.

### audio.narration — Spanning Multiple Clips
Narration is overlaid in post — it CAN span multiple clips via `span_clips`.
Only set `narration` on the FIRST clip of the span; later clips get `"narration": null`.
`estimated_duration` = `word_count` / 2.5 (narration pace ~150 wpm = 2.5 words/sec).

### audio.narration — No Overlap with Dialogue
If ANY clip in the narration's `span_clips` has dialogue, the narration span MUST
stop BEFORE that clip. Narration and dialogue must NEVER play simultaneously.
Pattern: narration over visual-only clips → pause → dialogue clip → pause → narration resumes.

### audio.use_native_h3_audio
Almost always `false`. Native H3 audio quality is unreliable.
Only set `true` for ambient-only clips with no dialogue.
