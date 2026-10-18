#!/usr/bin/env python3
"""dialogue_patch.py story.json patch.json [--max-chars 130] [--out story.json]
                     [--diff story_dialogue_diff.txt] [--backup]

Applies the Dialogue Polish agent's answer to the story. The patch is
    {"lines": [{"scene_id": "scene_003", "index": 2, "character_id": "char_002",
                "line": "Your coat's still wet.", "emotion": "quiet"}], "notes": [...]}
(a bare list of entries is accepted too). `index` is the 0-based position inside
`scenes[].dialogue`. Only `line` and `emotion` are ever written.

Each entry must address an existing line AND keep its speaker. Entries are DROPPED with a
warning when they: name a scene or index that does not exist, change the speaker, exceed
--max-chars or 32 words, or carry an empty/non-string line. The rest are applied; a
before→after diff is written to --diff (default: story_dialogue_diff.txt beside the story);
--backup keeps the input as <story>_v2_before.json. The write is atomic (tmp + os.replace).

Exit 0 = applied (possibly partially — warnings printed), 1 = nothing could be applied,
2 = bad input.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile

MAX_WORDS = 32
_TAG_RE = re.compile(r"<[^>]*>|\[[^\]]*\]")


def word_count(text: str) -> int:
    return len(_TAG_RE.sub(" ", text).split())


def atomic_write_json(path: str, data) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".dialogue_patch-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def scene_index(story: dict) -> dict:
    out = {}
    for sc in story.get("scenes") or []:
        if isinstance(sc, dict):
            for key in ("id", "scene_id"):
                if sc.get(key):
                    out.setdefault(str(sc[key]), sc)
    return out


def entries_of(patch) -> list:
    if isinstance(patch, dict):
        if "lines" not in patch:
            return None
        patch = patch["lines"]
    return patch if isinstance(patch, list) else None


def apply_patch(story: dict, entries: list, max_chars: int):
    """Returns (applied, dropped) lists; mutates `story`."""
    scenes = scene_index(story)
    applied, dropped = [], []
    for n, e in enumerate(entries):
        tag = f"entry {n}"
        if not isinstance(e, dict):
            dropped.append((tag, "not an object")); continue
        sid, idx, cid = e.get("scene_id"), e.get("index"), e.get("character_id")
        tag = f"{sid}[{idx}] {cid}"
        sc = scenes.get(str(sid))
        if sc is None:
            dropped.append((tag, "scene does not exist")); continue
        dl = sc.get("dialogue") or []
        if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < len(dl) or not isinstance(dl[idx], dict):
            dropped.append((tag, f"index out of range (scene has {len(dl)} lines)")); continue
        have = dl[idx].get("character_id") or dl[idx].get("character")
        if have != cid:
            dropped.append((tag, f"speaker changed (story has {have!r})")); continue
        line = e.get("line")
        if not isinstance(line, str) or not line.strip():
            dropped.append((tag, "line is empty or not a string")); continue
        line = line.strip()
        wc = word_count(line)
        if len(line) > max_chars or wc > MAX_WORDS:
            dropped.append((tag, f"line too long: {len(line)} chars / {wc} words (max {max_chars} / {MAX_WORDS})")); continue
        before = dict(dl[idx])
        dl[idx]["line"] = line
        emo = e.get("emotion")
        if isinstance(emo, str) and emo.strip():
            dl[idx]["emotion"] = emo.strip()
        if dl[idx] == before:
            dropped.append((tag, "no change")); continue
        applied.append((tag, before.get("line", ""), line, before.get("emotion"), dl[idx].get("emotion")))
    return applied, dropped


def write_diff(path: str, applied, dropped) -> None:
    lines = [f"# dialogue_patch: {len(applied)} applied, {len(dropped)} dropped", ""]
    for tag, old, new, oe, ne in applied:
        lines += [f"== {tag}", f"- {old}", f"+ {new}"]
        if oe != ne:
            lines.append(f"  emotion: {oe!r} -> {ne!r}")
        lines.append("")
    if dropped:
        lines.append("# dropped")
        lines += [f"  {tag}: {why}" for tag, why in dropped]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("story")
    ap.add_argument("patch")
    ap.add_argument("--max-chars", type=int, default=130)
    ap.add_argument("--out", help="default: overwrite the story in place")
    ap.add_argument("--diff", help="default: story_dialogue_diff.txt beside the story")
    ap.add_argument("--backup", action="store_true", help="keep the input as <story>_v2_before.json")
    a = ap.parse_args(argv)

    try:
        with open(a.story, encoding="utf-8") as fh:
            story = json.load(fh)
        with open(a.patch, encoding="utf-8") as fh:
            patch = json.load(fh)
        if not isinstance(story, dict) or not isinstance(story.get("scenes"), list):
            raise ValueError("story has no 'scenes' list")
        entries = entries_of(patch)
        if entries is None:
            raise ValueError("patch has no 'lines' list")
    except Exception as e:  # noqa: BLE001
        print(f"[dialogue_patch] cannot read input: {e}")
        return 2

    out = a.out or a.story
    diff = a.diff or os.path.join(os.path.dirname(os.path.abspath(a.story)), "story_dialogue_diff.txt")

    applied, dropped = apply_patch(story, entries, a.max_chars)
    for tag, why in dropped:
        print(f"[dialogue_patch] WARN dropped {tag}: {why}")
    for note in (patch.get("notes") or []) if isinstance(patch, dict) else []:
        print(f"[dialogue_patch] note: {note}")

    if not entries:
        print("[dialogue_patch] empty patch — story unchanged")
        write_diff(diff, applied, dropped)
        return 0
    if not applied:
        print(f"[dialogue_patch] REJECTED: none of {len(entries)} entries could be applied")
        write_diff(diff, applied, dropped)
        return 1

    if a.backup:
        root, ext = os.path.splitext(a.story)
        shutil.copyfile(a.story, f"{root}_v2_before{ext or '.json'}")
    atomic_write_json(out, story)
    write_diff(diff, applied, dropped)
    print(f"[dialogue_patch] applied {len(applied)} of {len(entries)} entries → {out} (diff {diff})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
