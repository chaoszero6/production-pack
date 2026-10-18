#!/usr/bin/env python3
"""story_check.py — does the story carry the craft fields, and does the shot list keep its words?

The first films were reviewed as "do not connect on an emotional level" and "the dialogue is
weak — the lines explain the plot". Nothing in the pipeline measured any of that. This gate
checks what CAN be measured (.dsh/skills/story-craft.md, story-schema.md):

  blocking (exit 1):
    missing_key          a required top-level key is absent (title, logline, theme,
                         characters, locations, acts, scenes)
    empty_turn           a scene carries `turn` but it is empty (a scene with no turn is merged)
    line_too_long        a dialogue line > --max-line-chars or > 32 words (H3: one speaker,
                         ~2.5 words/s, 15 s max)
    too_many_speakers    > 3 speaking characters in one scene
    narrator_class       a principal shares its voice class with the narrator (voice-casting)
    dialogue_share       dialogue words / (dialogue + narration words) < --block-share
    missing_line         (--shots) a story line is in no shot's audio.dialogue (difflib ≥ 0.9,
                         sentence-boundary splits across consecutive shots accepted)
    invented_narration   (--shots) a shot's narration text is not in the story
  warnings (reported, exit stays 0):
    want/need/stakes missing, `turn` field absent, no voice class parsable, share below
    --min-dialogue-share, narration missing from the shots

Usage:
  story_check.py --story story.json [--shots shot_list.json] [--min-dialogue-share 0.55]
                 [--block-share 0.40] [--max-line-chars 130] [--out story_check.json]
Writes {ok, blocking, warnings, dialogue_share, missing_lines, ...}.
Exit 0 = ok, 1 = blocking issues, 2 = bad input.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import tempfile

REQUIRED_KEYS = ("title", "logline", "theme", "characters", "locations", "acts", "scenes")
CRAFT_KEYS = ("want", "need", "stakes")
MAX_LINE_WORDS = 32
MAX_SPEAKERS = 3
MATCH_RATIO = 0.9

VOICE_CLASSES = ("child", "teen", "adult-female", "adult-male", "elderly-female",
                 "elderly-male", "creature")
_CLASS_RE = re.compile(
    r"\b(child|teen|adult[\s_-]?female|adult[\s_-]?male|elderly[\s_-]?female|"
    r"elderly[\s_-]?male|creature|synthetic)\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]*>|\[[^\]]*\]")
_WS_RE = re.compile(r"\s+")


# ----------------------------------------------------------------------------- helpers
def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def atomic_write(path, data):
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".story_check-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def clean_text(text) -> str:
    """Strip paralinguistic tags and collapse whitespace; '' for non-strings."""
    if not isinstance(text, str):
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def norm(text) -> str:
    """Lower-cased, punctuation-free form used for matching lines."""
    return _WS_RE.sub(" ", re.sub(r"[^\w\s']", " ", clean_text(text).lower())).strip()


def word_count(text) -> int:
    return len(clean_text(text).split())


def scene_id(scene: dict) -> str:
    return str(scene.get("id") or scene.get("scene_id") or "?")


def narration_texts(narration) -> list[str]:
    """Story narration is an object {text}, or null; older runs wrote lists or strings."""
    if not narration:
        return []
    if isinstance(narration, str):
        return [narration]
    if isinstance(narration, dict):
        t = narration.get("text")
        return [t] if isinstance(t, str) and t.strip() else []
    if isinstance(narration, list):
        out = []
        for n in narration:
            out.extend(narration_texts(n))
        return out
    return []


def voice_class(text) -> str | None:
    """First voice-class word in a voice_style / narrator_voice string (or dict)."""
    if isinstance(text, dict):
        text = text.get("voice_class") or text.get("class") or text.get("style") or ""
    if not isinstance(text, str):
        return None
    m = _CLASS_RE.search(text)
    if not m:
        return None
    c = re.sub(r"[\s_]+", "-", m.group(1).lower())
    return "creature" if c == "synthetic" else c


def ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


# ----------------------------------------------------------------------------- story checks
def check_story(story: dict, max_chars: int, block_share: float, min_share: float):
    blocking, warnings = [], []

    for k in REQUIRED_KEYS:
        if k not in story:
            blocking.append({"code": "missing_key", "key": k, "detail": f"top-level '{k}' is required"})
    for k in CRAFT_KEYS:
        if not clean_text(story.get(k)):
            warnings.append({"code": "missing_craft_key", "key": k,
                             "detail": f"top-level '{k}' missing or empty (story-craft §1–§2)"})

    scenes = story.get("scenes") or []
    if not isinstance(scenes, list):
        blocking.append({"code": "bad_scenes", "detail": "'scenes' must be a list"})
        scenes = []

    dialogue_words = narration_words = 0
    story_lines = []  # (scene_id, index, character_id, line)
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        sid = scene_id(scene)
        if "turn" in scene:
            if not clean_text(scene.get("turn")):
                blocking.append({"code": "empty_turn", "scene": sid,
                                 "detail": "scene has an empty 'turn' — a scene with no turn is cut or merged"})
        else:
            warnings.append({"code": "no_turn_field", "scene": sid,
                             "detail": "scene has no 'turn' field (story-craft §4)"})
        speakers = []
        for i, d in enumerate(scene.get("dialogue") or []):
            if not isinstance(d, dict):
                continue
            cid = d.get("character_id") or d.get("character") or "?"
            line = d.get("line")
            if not isinstance(line, str):
                continue
            story_lines.append((sid, i, cid, line))
            if cid not in speakers:
                speakers.append(cid)
            wc = word_count(line)
            dialogue_words += wc
            if len(line) > max_chars or wc > MAX_LINE_WORDS:
                blocking.append({"code": "line_too_long", "scene": sid, "index": i, "character_id": cid,
                                 "chars": len(line), "words": wc,
                                 "detail": f"line is {len(line)} chars / {wc} words (max {max_chars} / {MAX_LINE_WORDS}): {line[:80]!r}"})
        if len(speakers) > MAX_SPEAKERS:
            blocking.append({"code": "too_many_speakers", "scene": sid, "speakers": speakers,
                             "detail": f"{len(speakers)} speakers in one scene (max {MAX_SPEAKERS})"})
        for t in narration_texts(scene.get("narration")):
            narration_words += word_count(t)

    # voice classes — no principal shares the narrator's class
    narrator = (story.get("narration_script") or {}).get("narrator_voice") if isinstance(story.get("narration_script"), dict) else None
    narrator_class = voice_class(narrator)
    speaking_ids = {cid for _, _, cid, _ in story_lines}
    if narrator_class is None:
        if narration_words:
            warnings.append({"code": "narrator_class_unknown",
                             "detail": "narration_script.narrator_voice names no voice class"})
    for ch in story.get("characters") or []:
        if not isinstance(ch, dict):
            continue
        cid = ch.get("id") or ch.get("name")
        role = str(ch.get("role") or "").lower()
        principal = cid in speaking_ids or ch.get("name") in speaking_ids or \
            any(w in role for w in ("protagonist", "antagonist", "deuteragonist", "mentor", "lead"))
        vc = voice_class(ch.get("voice_style"))
        if vc is None:
            warnings.append({"code": "voice_class_unknown", "character": cid,
                             "detail": "voice_style names no voice class (child/teen/adult-female/...)"})
            continue
        if principal and narrator_class and vc == narrator_class:
            blocking.append({"code": "narrator_class", "character": cid, "voice_class": vc,
                             "detail": f"principal '{cid}' shares voice class '{vc}' with the narrator (voice-casting rule 1)"})

    share = share_checks(dialogue_words, narration_words, block_share, min_share, "story", blocking, warnings)
    return blocking, warnings, share, story_lines, dialogue_words, narration_words


def share_checks(dw: int, nw: int, block_share: float, min_share: float, where: str, blocking, warnings):
    total = dw + nw
    if total == 0:
        warnings.append({"code": "no_spoken_words", "where": where, "detail": "no dialogue and no narration words"})
        return None
    share = dw / total
    if share < block_share:
        blocking.append({"code": "dialogue_share", "where": where, "share": round(share, 3),
                         "detail": f"dialogue is {share:.0%} of spoken words in the {where} (block below {block_share:.0%}): a narrated slideshow, not a film"})
    elif share < min_share:
        warnings.append({"code": "dialogue_share_low", "where": where, "share": round(share, 3),
                         "detail": f"dialogue is {share:.0%} of spoken words in the {where} (target ≥ {min_share:.0%})"})
    return share


# ----------------------------------------------------------------------------- shot checks
def shot_lines(shots: list) -> list[tuple[str, str, str, str]]:
    """(shot_id, scene_id, character_id, line) for every dialogue entry, in list order."""
    out = []
    for s in shots:
        if not isinstance(s, dict):
            continue
        dl = (s.get("audio") or {}).get("dialogue") or []
        if isinstance(dl, dict):
            dl = [dl]
        for d in dl:
            if isinstance(d, dict) and isinstance(d.get("line"), str):
                out.append((str(s.get("shot_id")), str(s.get("scene_id")), str(d.get("character_id") or "?"), d["line"]))
    return out


def line_in_shots(line: str, sl: list[tuple[str, str, str, str]]):
    """Match a story line against single shot lines, then against joins of consecutive
    shot lines (a line split at a sentence boundary into two or three shots)."""
    target = norm(line)
    if not target:
        return True, None
    best = (0.0, None)
    for sid, _, _, l in sl:
        r = ratio(target, norm(l))
        if r > best[0]:
            best = (r, sid)
        if r >= MATCH_RATIO:
            return True, sid
    for width in (2, 3):
        for i in range(len(sl) - width + 1):
            chunk = sl[i:i + width]
            if len({c[2] for c in chunk}) != 1:  # same speaker only
                continue
            joined = norm(" ".join(c[3] for c in chunk))
            r = ratio(target, joined)
            if r >= MATCH_RATIO:
                return True, "+".join(c[0] for c in chunk)
            if r > best[0]:
                best = (r, "+".join(c[0] for c in chunk))
    return False, best


def check_shots(story_lines, story: dict, shots: list, block_share: float, min_share: float,
                blocking, warnings):
    sl = shot_lines(shots)
    missing = []
    for sid, idx, cid, line in story_lines:
        ok, where = line_in_shots(line, sl)
        if not ok:
            r, near = where
            missing.append({"scene": sid, "index": idx, "character_id": cid, "line": line,
                            "nearest": near, "ratio": round(r, 2)})
    for m in missing:
        blocking.append({"code": "missing_line", **m,
                         "detail": f"story line not in any shot (nearest {m['nearest']} at {m['ratio']}): {m['line'][:80]!r}"})

    # narration in shots must come from the story
    story_narr = []
    for scene in story.get("scenes") or []:
        if isinstance(scene, dict):
            story_narr.extend(narration_texts(scene.get("narration")))
    story_narr_norm = [norm(t) for t in story_narr]
    story_narr_all = " ".join(story_narr_norm)
    shot_dw = sum(word_count(l) for _, _, _, l in sl)
    shot_nw = 0
    for s in shots:
        if not isinstance(s, dict):
            continue
        narr = (s.get("audio") or {}).get("narration")
        for t in narration_texts(narr):
            shot_nw += word_count(t)
            tn = norm(t)
            if not tn:
                continue
            if tn in story_narr_all or any(ratio(tn, sn) >= MATCH_RATIO for sn in story_narr_norm):
                continue
            # a span may carry only part of a scene's narration: accept a long common run
            if any(difflib.SequenceMatcher(None, tn, sn).find_longest_match(0, len(tn), 0, len(sn)).size
                   >= 0.9 * len(tn) for sn in story_narr_norm):
                continue
            blocking.append({"code": "invented_narration", "shot": s.get("shot_id"),
                             "detail": f"narration in shot {s.get('shot_id')} is not in the story: {t[:80]!r}"})
    if story_narr and shot_nw == 0:
        warnings.append({"code": "narration_missing_from_shots",
                         "detail": "the story has narration but no shot carries any"})
    share = share_checks(shot_dw, shot_nw, block_share, min_share, "shot list", blocking, warnings)
    return missing, share, shot_dw, shot_nw


# ----------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--story", required=True)
    ap.add_argument("--shots")
    ap.add_argument("--min-dialogue-share", type=float, default=0.55)
    ap.add_argument("--block-share", type=float, default=0.40)
    ap.add_argument("--max-line-chars", type=int, default=130)
    ap.add_argument("--out")
    a = ap.parse_args(argv)

    try:
        story = load_json(a.story)
        if not isinstance(story, dict):
            raise ValueError("story is not a JSON object")
        shots = None
        if a.shots:
            sl = load_json(a.shots)
            shots = sl.get("shots") if isinstance(sl, dict) else sl
            if not isinstance(shots, list):
                raise ValueError("shot list has no 'shots' list")
    except Exception as e:  # noqa: BLE001
        print(f"[story_check] cannot read input: {e}")
        return 2

    blocking, warnings, share, story_lines, dw, nw = check_story(
        story, a.max_line_chars, a.block_share, a.min_dialogue_share)
    report = {
        "ok": False,
        "story": a.story,
        "scenes": len(story.get("scenes") or []),
        "dialogue_lines": len(story_lines),
        "dialogue_words": dw,
        "narration_words": nw,
        "dialogue_share": None if share is None else round(share, 3),
        "dialogue_share_story": None if share is None else round(share, 3),
        "missing_lines": [],
        "blocking": blocking,
        "warnings": warnings,
    }
    if shots is not None:
        missing, sshare, sdw, snw = check_shots(story_lines, story, shots, a.block_share,
                                                a.min_dialogue_share, blocking, warnings)
        report.update({
            "shots": a.shots, "shot_count": len(shots),
            "missing_lines": missing,
            "shot_dialogue_words": sdw, "shot_narration_words": snw,
            "dialogue_share_shots": None if sshare is None else round(sshare, 3),
        })
        if sshare is not None:
            report["dialogue_share"] = round(sshare, 3)
    report["ok"] = not blocking

    # one-screen report
    print(f"[story_check] {story.get('title', '?')!s}: {report['scenes']} scenes, "
          f"{report['dialogue_lines']} lines, dialogue {dw} w / narration {nw} w"
          + (f" → share {share:.0%}" if share is not None else ""))
    if shots is not None:
        print(f"[story_check] shots: {report['shot_count']}, dialogue {report['shot_dialogue_words']} w / "
              f"narration {report['shot_narration_words']} w"
              + (f" → share {report['dialogue_share_shots']:.0%}" if report["dialogue_share_shots"] is not None else "")
              + f", missing lines {len(report['missing_lines'])}")
    for b in blocking:
        print(f"  BLOCK {b['code']:<20} {b.get('detail', '')}")
    for w in warnings:
        print(f"  warn  {w['code']:<20} {w.get('detail', '')}")
    print(f"[story_check] {'OK' if report['ok'] else 'BLOCKED'}: {len(blocking)} blocking, {len(warnings)} warnings")

    if a.out:
        atomic_write(a.out, report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
