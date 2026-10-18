---
name: story-craft
description: Craft rules for a story that connects emotionally — want/need, visible stakes, scene turns, subtext dialogue, show-don't-tell, beat sheet and music cues — inside the MiniMax H3 line limits
whenToUse: When writing, editing or polishing a story or its dialogue (Story Creator, Story Editor, Dialogue Polish); the Director reads §8–§11
---

# Story Craft

Why this skill exists: the first films were reviewed as "do not connect on an emotional
level" and "the dialogue is weak — the lines explain the plot". Every clip had passed QA.
The story was the defect, and nothing in the pipeline wrote or judged for emotion. These
rules are that gate. `pipeline/story_check.py` enforces the measurable ones.

## 1. Want vs need — the climax forces the choice
The protagonist WANTS something concrete she can name in scene 1: a thing, a place, a win.
She NEEDS something she cannot name: to be forgiven, to let go, to ask for help. The want
drives the plot; the need decides the ending. The climax is the moment she must choose
between them, and the choice COSTS her the want. If she can have both, there is no story.
Write `want` and `need` as one line each at the top level. Nobody ever says the need aloud.

## 2. Stakes — visible by scene 2, escalating once per act
Name what is lost if she fails, and put it ON SCREEN by scene 2: the empty chair, the last
lantern, the boat that leaves at dawn. Escalate once per act — Act 1 the thing, Act 2 the
person, Act 3 the self. A stake the audience cannot picture is not a stake. `stakes` is
one sentence naming the loss.

## 3. Structure — the Pixar spine on three acts
*Once upon a time* (opening image: the ordinary world with the want on show) → *every day*
(the routine and the flaw) → *until one day* (inciting incident; end of Act 1) → *because of
that* (first pursuit, the fun part) → *because of that* (MIDPOINT REVERSAL: she gets what
she wanted and it is wrong, or loses it outright) → *until finally* (LOW POINT: all is lost,
the need is undeniable — then the climax choice) → *ever since* (the new routine).
For ten minutes: 8–12 scenes; Act 1 ≈ 25 %, Act 2 ≈ 50 % with the reversal at its centre,
Act 3 ≈ 25 %. The last scene answers the first: same place or object, opposite meaning.

## 4. Scene contract — a scene with no turn is cut or merged
Every scene carries four fields:
- `scene_goal` — what the POV character tries to get in THIS scene, concrete, now.
- `obstacle` — who or what is in the way. A person beats a wall; a loved one beats a stranger.
- `turn` — the value that flips by the end, written "from X to Y": from hope to dread,
  from alone to together, from trusted to caught lying.
- `audience_feels` — the exit emotion at the cut (§9).
Test: cover the scene. If the next scene still makes sense without it, it has no turn —
merge it into its neighbour or cut it.

## 5. Setups and payoffs
List `setups_payoffs`: every object, line or skill that matters in Act 3 is planted in
Act 1 without comment, and nothing planted goes unused. Minimum three. The best payoff reuses
the setup with its meaning reversed — the key that locked her in is the key that opens the gate.

## 6. Theme is argued, never stated
The theme is a question that two characters answer differently through what they DO. The
antagonist or mentor believes the opposite of the need and is partly right. No line, and no
narration, may say the moral.
BAD: "I learned that real courage is asking for help."
GOOD: she stops at the ledge, looks back, and holds her hand out first.

## 7. Show, don't tell
Emotion travels through action, objects and silence, never labels. Grief is the second cup
still set out. Fear is hands that will not stop working. Love is handing over the warmer coat.
Narration exists for time and place only ("Three winters later, in the harbour town of
Cinderquay."). Narration never says what a character feels and never tells plot the picture
is already showing.
BAD narration: "Maya felt a deep sadness as she remembered her mother."
GOOD narration: "The second winter without her." — then the picture: Maya setting two cups.

## Director excerpt

## 8. Dialogue rules
1. **Subtext.** People ask for what they want through something else; every line hides the
   real request.
   BAD: "I'm scared you'll leave like Mother did."
   GOOD: "Take the lantern. It's dark past the bridge."
2. **No plot-explaining lines.** If a line's only job is to tell the audience what happened
   or will happen, cut it; a prop or the picture carries it.
   BAD: "We have to reach the peak before the eclipse tonight or the gate closes forever."
   GOOD: "Eclipse is at moonrise." — "Then walk faster."
3. **Nobody announces a feeling.** Never "I'm so angry", "I'm happy", "I miss her".
   BAD: "I'm furious with you."
   GOOD: "Eat your soup." — the bowl set down too hard.
4. **Verbal signature.** Each character gets ≤ 3 words in `verbal_signature` — a tic, a
   vocabulary, a rhythm (Nana says "child"; the moth counts things; the mayor says "with
   respect"). With the names removed, every line must still be attributable.
5. **Conflict in every exchange.** Two people who agree have nothing to say. Give them
   opposite goals, or at least opposite tempos: one wants, one withholds.
6. **Cut the first and last line** of every scene's dialogue draft — they are usually the
   greeting and the summary. Enter late, leave early.
7. **One speaker per beat; alternating short lines.** H3 renders one speaker per clip, so a
   conversation is a rally: A / B / A. No speeches. A long thought is two lines with the
   other character's reaction between them.
8. **Hard limits** (H3; `story_check.py` blocks): ≤ 130 characters per line, ≤ ~30 words
   (2.5 words/s fills a 12 s clip), ≤ 3 speakers per scene, two or three people in a room with
   fixed positions so shot/reverse-shot can cover them.
9. A line that carries a `readable_props` value ("ten past seven") quotes the ledger
   phrase verbatim.

## 9. Emotional beat sheet — per scene
- `emotional_beat` — the beat in one phrase ("the apology she can't say").
- `audience_feels` — one or two words, the feeling at the cut (dread, relief, ache). QA
  compares the POV character's face against it.
- `pov_character` — whose scene it is. The camera stays with them; the reactions are theirs.
- `linger` — the ONE moment held in silence ≥ 2 s (a hand resting on the door, the cup
  untouched). Every scene has one; the Director cuts it as its own shot; no dialogue over it.

## 10. Dialogue share
Count spoken words. Dialogue ≥ 55 % of (dialogue + narration); narration ≤ 45 %. Below 40 %
the story is BLOCKED: it is a narrated slideshow, not a film. Turn narration into a scene
that shows it, or cut it.

## 11. Music cues — per scene
`music_cue: {"mood", "intensity" 0–1, "start": "on <beat>", "stop": "on <beat>"}`, e.g.
`{"mood": "fragile hope", "intensity": 0.3, "start": "on Maya lifting the lantern",
"stop": "on the door closing"}`. Start and stop name a visible action, never a time. Silence
is a cue — `{"mood": "silence", "intensity": 0, ...}` for the linger and the low point.
Never score a whole scene: music that never stops is wallpaper.

## End director excerpt

## 12. Writer self-check — every box before output
- [ ] want and need differ; the climax forces the choice; the want is paid for
- [ ] the stake is on screen by scene 2 and escalates once per act
- [ ] midpoint reversal; low point before the climax; last scene answers the first
- [ ] every scene: `scene_goal`, `obstacle`, `turn` ("from X to Y"), `pov_character`,
      `audience_feels`, `linger`
- [ ] ≥ 3 `setups_payoffs`, each planted before it is used, none unused
- [ ] no line states the theme or names the speaker's feeling; no plot-explaining line
- [ ] narration is time/place only; dialogue ≥ 55 % of spoken words
- [ ] every line ≤ 130 characters and ≤ 30 words; speakers alternate; ≤ 3 speakers per scene
- [ ] every character has `verbal_signature` and a voice class; the narrator's class is
      used by no character
- [ ] every scene has `music_cue` with start/stop on named beats; at least one silence cue
- [ ] `readable_props` set per scene and quoted verbatim wherever a line reads one
