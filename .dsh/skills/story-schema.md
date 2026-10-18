---
name: story-schema
description: Structured JSON schema for story output — used by Story Creator and consumed by Director
whenToUse: When creating or parsing a story document
---

# Story Schema

The Story Creator outputs stories in this structured JSON format.
All downstream agents consume this schema.

```json
{
  "title": "string — movie title",
  "logline": "string — one sentence capturing the essence",
  "theme": "string — the emotional truth explored",
  "style": "pixar-fantasy",
  "target_duration_minutes": 10,

  "want": "string — the concrete thing the protagonist chases from scene 1 (story-craft §1)",
  "need": "string — the unnamed thing she must accept at the climax; never spoken aloud",
  "stakes": "string — one sentence: what is lost if she fails, visible on screen by scene 2",

  "setups_payoffs": [
    {
      "setup": "the silver pin her mother left is used to pick the kitchen lock",
      "payoff": "the pin opens the gate at the summit",
      "scene_setup": "scene_001",
      "scene_payoff": "scene_009"
    }
  ],

  "narration_script": {
    "narrator_voice": "elderly-male, unhurried, dry warmth — a voice class NO character uses",
    "style": "time and place only; never a feeling, never plot the picture shows"
  },

  "characters": [
    {
      "id": "char_001",
      "name": "Maya",
      "role": "protagonist",
      "age": "12",
      "visual_description": {
        "height": "average for age",
        "build": "slender",
        "skin_tone": "warm brown",
        "hair": "long black, usually half-up with silver pin",
        "eyes": "amber, large and expressive",
        "clothing": "pale blue hanfu with translucent outer robe, indigo ribbon",
        "accessories": "silver crescent crown, leather satchel",
        "distinguishing_features": "small star-shaped birthmark on left cheek"
      },
      "personality": "curious, brave, sometimes impulsive, deeply loyal",
      "voice_style": "child, 12 — warm, quick, enthusiastic; quieter when scared",
      "verbal_signature": "counts under breath",
      "arc": "learns that true courage means asking for help",
      "identity_anchors": [
        "long black hair with silver pin",
        "amber eyes",
        "pale blue hanfu",
        "silver crescent crown",
        "star birthmark on left cheek"
      ]
    }
  ],

  "locations": [
    {
      "id": "loc_001",
      "name": "The Whispering Canopy",
      "type": "exterior",
      "description": "An ancient forest where the trees glow faintly with bioluminescent moss. Shafts of golden light pierce the canopy. The floor is carpeted with luminous blue flowers.",
      "mood": "wonder, mystery, slight danger",
      "color_palette": ["deep emerald", "golden light", "luminous blue", "warm amber"],
      "time_of_day": "perpetual twilight",
      "atmospheric_effects": ["floating pollen particles", "gentle mist at ground level"]
    }
  ],

  "acts": [
    {
      "act_number": 1,
      "title": "The Ordinary World",
      "description": "Establish Maya's world, her desire, and the inciting incident",
      "scenes": ["scene_001", "scene_002", "scene_003"]
    }
  ],

  "scenes": [
    {
      "id": "scene_001",
      "title": "Dawn in the Village",
      "act": 1,
      "location_id": "loc_001",
      "characters_present": ["char_001"],
      "time_of_day": "dawn",
      "duration_estimate_seconds": 45,
      "emotional_beat": "wonder and longing",

      "scene_goal": "Maya wants to see the peaks before the village wakes and stops her",
      "obstacle": "the fog, and Nana's lamp already lit in the window below",
      "turn": "from longing to resolve",
      "pov_character": "char_001",
      "audience_feels": "ache",
      "linger": "Maya's hand on the cold rail, the peaks lost in fog, held in silence",
      "music_cue": {
        "mood": "fragile hope",
        "intensity": 0.3,
        "start": "on Maya reaching the overlook",
        "stop": "on her hand touching the rail"
      },
      "readable_props": {
        "village_clock": "hands at ten past five"
      },

      "visual_mood": {
        "lighting": "warm golden dawn light",
        "color_temperature": "warm",
        "atmosphere": "peaceful, misty morning"
      },
      "action_description": "Maya wakes before dawn and climbs to the village overlook. She gazes at the distant Shimmering Peaks, where her mother disappeared years ago.",
      "dialogue": [
        {
          "character_id": "char_001",
          "line": "One day, I'll find what's up there. I promise.",
          "emotion": "determined but wistful",
          "timing": "after she reaches the overlook"
        }
      ],
      "narration": {
        "text": "In a village where the clouds touched the rooftops, there lived a girl who believed the sky owed her an answer.",
        "timing": "opening, before Maya wakes"
      },
      "music_mood": "gentle, hopeful, sparse piano with strings",
      "sound_effects": ["morning birds", "gentle wind", "distant wind chimes"]
    }
  ]
}
```

## Field Notes (craft fields — story-craft skill)

| Field | Where | Meaning |
|-------|-------|---------|
| `want` / `need` / `stakes` | top level | want = concrete goal; need = the unspoken truth; stakes = the visible loss (§1–§2) |
| `setups_payoffs[]` | top level | `{setup, payoff, scene_setup, scene_payoff}` — every Act 3 payoff planted earlier (§5) |
| `narration_script` | top level | `{narrator_voice, style}`; `narrator_voice` names a voice class no character uses |
| `verbal_signature` | character | ≤ 3 words — the tic / vocabulary / rhythm that makes a line attributable (§8.4) |
| `scene_goal` / `obstacle` / `turn` | scene | the scene contract; `turn` is "from X to Y" (§4). A scene with no turn is merged |
| `pov_character` | scene | character id whose scene it is; the camera and the reactions stay with them |
| `audience_feels` | scene | one or two words — the exit emotion at the cut |
| `linger` | scene | the one moment held in silence ≥ 2 s; the Director cuts it as its own shot |
| `music_cue` | scene | `{mood, intensity 0–1, start: "on <beat>", stop: "on <beat>"}`; `mood: "silence"` is a valid cue |
| `readable_props` | scene | `{prop: value}` — the ledger phrase for every readable prop in that scene |

Kept as before: `arc` (character), `emotional_beat`, `music_mood`, `action_description`
(scene). `narration` stays an object `{text, timing}` or `null` — never a list. Scene ids
are `id` (older runs wrote `scene_id`; tools accept both).

## Usage Notes
- `identity_anchors` in characters are used by Screenplay Reviewer to ensure every prompt
  includes full visual identity
- `color_palette` in locations ensures consistent environment generation
- `emotional_beat` per scene guides Music Composer and Director
- `dialogue.emotion` guides Audio Producer's voice synthesis
- Scene IDs are referenced by Director when creating the shot list
- **Director** reads `pov_character`, `audience_feels`, `linger` and `music_cue` per scene:
  the linger becomes a held reaction single, the cue's start/stop land on named shots, and
  every shot copies `pov_character` / `audience_feels` unless it changes POV. Dialogue is
  LOCKED after 1a: every `scenes[].dialogue[].line` is copied verbatim into exactly one shot.
- **Audio Producer** casts from `voice_style` (voice class + age) and
  `narration_script.narrator_voice`; `verbal_signature` guides line delivery.
- **Music Composer** reads `music_cue` (mood, intensity, start/stop beats) before `music_mood`.
- **QA Inspector** compares the POV character's face in a clip against `audience_feels`.
- **`pipeline/story_check.py`** blocks on: a scene whose `turn` is empty, a line > 130 chars
  or > 32 words, > 3 speakers in a scene, a principal sharing the narrator's voice class,
  dialogue share < 40 % of spoken words (warns < 55 %).
- **`pipeline/dialogue_patch.py`** applies the Dialogue Polish output: `line` / `emotion`
  only, addressed by `(scene_id, index, character_id)` where `index` is the 0-based position
  in `scenes[].dialogue`.
