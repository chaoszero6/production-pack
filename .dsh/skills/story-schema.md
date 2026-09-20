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
      "voice_style": "warm, youthful, enthusiastic — quieter when scared",
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

## Usage Notes
- `identity_anchors` in characters are used by Screenplay Reviewer to ensure every prompt
  includes full visual identity
- `color_palette` in locations ensures consistent environment generation
- `emotional_beat` per scene guides Music Composer and Director
- `dialogue.emotion` guides Audio Producer's voice synthesis
- Scene IDs are referenced by Director when creating the shot list
