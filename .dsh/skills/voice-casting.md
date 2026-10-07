---
name: voice-casting
description: Rules for casting narrator and character voices so every speaker is instantly distinguishable
whenToUse: When designing voice profiles, choosing TTS engines/voices, or QA-ing dialogue and narration audio
---

# Voice Casting Skill

Used by the Audio Producer (casting + generation) and the QA Inspector (voice checks).

## Why this skill exists

*The Clockwork Moth* (2026-10) was reviewed publicly as: "narrator, child, and old woman all
had the same voice, as if the same human were reading to you and just adding a bit of
affectation for the character. Until the man comes in."

Cast sheet that shipped: narrator = Orpheus `tara`, Tobias (boy, 11) = Orpheus `jess`,
Nana (elderly woman) = Orpheus `leah`, Tick (moth) = Orpheus `zoe`, Mayor = Orpheus `dan`.
Four of five voices came from ONE engine's adult-female set, median pitch within ~3
semitones of each other (Nana ≈ 220 Hz, Tobias ≈ 190 Hz, narrator similar). The only voice
that landed was the one from a different voice class (`dan`, adult male, ≈ 160 Hz).
Per-line prosody metrics were "not monotone" and voice_quality passed QA — the defect was
**between** voices, which nothing measured.

## Rule 1 — Cast by voice class first, engine second

Every speaking role (narrator included) gets a **voice class**: `child`, `teen`,
`adult-female`, `adult-male`, `elderly-female`, `elderly-male`, `creature/synthetic`.
No two principal roles may share a class unless the story has two adults of the same sex,
and then they must differ by Rule 2.

Engine/voice menu by class (verify availability on the server before casting):

| Class | Primary | How | Fallback |
|-------|---------|-----|----------|
| child / teen | **Chatterbox** zero-shot clone from a child voice reference (`audio/voices/refs/child_*.wav`) | reference clip + paralinguistic tags | CosyVoice 3 with instruction "a cheerful eleven-year-old boy" ; Orpheus `zoe`/`mia` with pitch +3 st, tempo 1.08 via `rubberband`/`asetrate` post-shift |
| elderly | **CosyVoice 3** instruction "an elderly woman in her seventies, warm, slightly breathy, slower" or Chatterbox clone from an elderly reference | instruction | Orpheus `leah` with tempo 0.92, pitch −2 st |
| adult-female | Orpheus `tara` / `jess` / `leah` / `mia` / `zoe` | pick by Rule 2 | Chatterbox clone |
| adult-male | Orpheus `leo` / `dan` / `zac` | pick by Rule 2 | Chatterbox clone |
| creature / synthetic | CosyVoice 3 instruction or Chatterbox clone + light post FX (formant shift, tiny chorus) | | Kokoro |
| **narrator** | A class **not used by any character**; if all classes are taken, narrator = Chatterbox clone from a dedicated narrator reference that is ≥ 5 semitones from every character's median pitch | | Kokoro only as last resort — measured "flat, identical prosody across blocks" on the Moth |

The narrator is a *different person* from everyone on screen. Never assign the narrator
an Orpheus voice from the same sex group as a principal character.

## Rule 2 — Minimum separation, measured, not assumed

Before any dialogue is generated, build a **voice line-up**: synthesize the SAME two test
sentences (one statement, one question, ~12 words) in every cast voice, save to
`audio/voices/lineup/<role>.wav`, and run the prosody metrics (`studio.py voice` style:
`f0_median_hz`, `f0_std_semitones`, `rms_dynamic_db`, speaking rate).

Pairwise requirements for ANY two roles that share a scene, and for narrator vs everyone:
- `|Δ f0_median|` ≥ **4 semitones**, OR a different voice class with a clearly different
  timbre (child vs elderly) AND ≥ 2 semitones.
- Speaking-rate difference ≥ 10 % OR a different engine.
- Not the same engine+voice with only tags/punctuation changed. Affectation is not casting.

Write the matrix to `audio/voices/lineup/separation.json` and the verdict `CAST_OK` /
`CAST_FAIL`. `CAST_FAIL` blocks Phase 2 — recast, do not proceed. Keep the line-up: the
QA Inspector listens to it when scoring `voice_quality`, and the user can audition it.

## Rule 3 — Age and register must be audible

- A child must sound like a child: higher median pitch (≥ 250 Hz for a pre-teen boy
  in TTS terms), faster rate, lighter breath. An adult voice "doing a kid" fails Rule 1.
- An elderly character has slower rate (≤ 2.3 words/s), lower dynamic range, audible
  breath, slight rasp or breathiness. Tempo/pitch post-shift is allowed within ±3 st / ±10 %
  if the engine has no elderly voice.
- Register: narrator is calm storytelling (1.9–2.3 w/s, long phrases); characters speak at
  conversational rate (2.4–3.0 w/s) with interjections. Same text must NOT be rendered at the
  same pace for all roles.

## Rule 4 — Keep the cast fixed, keep the references fixed

The voice reference / instruction string / engine+voice for each role is written once to
`audio/voices/voice_config.json` and reused for every line in the film. Never regenerate a
reference per line (identity drifts). Store `voice_class`, `engine`, `voice|reference|instruction`,
`post_fx` (pitch/tempo shift if any), `lineup_f0_median_hz`, `lineup_rate_wps`.

```json
{
  "narrator": {"voice_class": "adult-male", "engine": "chatterbox",
               "reference": "refs/narrator_warm_baritone.wav", "lineup_f0_median_hz": 118},
  "tobias":   {"voice_class": "child", "engine": "chatterbox",
               "reference": "refs/child_boy_11.wav", "lineup_f0_median_hz": 268},
  "nana":     {"voice_class": "elderly-female", "engine": "cosyvoice3",
               "instruction": "an elderly woman in her seventies, warm, slightly breathy, slower",
               "lineup_f0_median_hz": 196},
  "mayor":    {"voice_class": "adult-male", "engine": "orpheus", "voice": "dan",
               "lineup_f0_median_hz": 160}
}
```

(narrator adult-male and mayor adult-male are allowed here only because the measured
separation is 5.3 semitones AND the engines differ — record that in `separation.json`.)

## Rule 5 — QA listens across roles, not just within a line

`voice_quality` (category 10) now has two parts:
1. Within the line: prosody, emotion match, no robotic timbre (as before).
2. **Cast separation**: compare the line's `f0_median` and rate with the other roles'
   line-up values. If the nearest other role is < 4 semitones away and same class → score
   ≤ 0.6 and `corrections: ["RECAST <role>"]`. This is assembly-fixable only if the line can
   be regenerated without re-rendering video (narration, post-only lines); lip-synced
   dialogue needs a re-render with the new stem.
