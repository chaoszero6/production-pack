#!/usr/bin/env python3
"""shot_list_lint.py — does every shot fit inside what MiniMax H3 can render?

The Cinderquay run (2026-10-16) failed 16 takes in a row before a single clip passed, and
the defects were planned into the shot list before any prompt was written: a four-beat
montage in one 10 s extreme-wide shot, three named characters all carrying the same
copy-pasted action string, "exactly ten" costumed crowd members, a whale given a
hand_position, reference files that did not exist. knowledge/minimax_h3_rules.md §0
records what H3 does with each of those. This lint catches them before the Director's
list reaches the per-clip loop.

  blocking (exit 1 — the Director rewrites):
    montage_in_one_shot   action_description is a montage / ≥ 3 ';'-separated beats
    too_many_named        > 3 characters_in_frame
    copied_action         ≥ 2 characters in one shot share the identical action string
  advisory (reported, auto-fixed where mechanical with --fix):
    counted_crowd         "exactly N", "crowd of N", "N figures" — H3 cannot count
    wide_with_faces       extreme-wide shot carrying named characters / expressions
    duration              > 10 s without dialogue (fixed to 10), < 5.2 s (H3 rounds up to 124 frames)
    plate_mode            0-character shot in mode "reference" → first_frame_only from the plate
                          (ref2va re-frames toward subjects; i2va holds the plate's framing)
    missing_reference     reference_images.source not on disk (resolved to a sibling file or
                          dropped) — only checked once the manifests exist

Usage:
  shot_list_lint.py --shots <shot_list.json> --run-dir <run_dir> [--out report.json] [--fix]
Exit 0 = no blocking issues, 1 = blocking issues remain, 2 = bad input.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile

MAX_NAMED = 3
MAX_SILENT_S = 10.0
MIN_TRAINED_S = 124 / 24.0

COUNTED_RE = re.compile(
    r"\b(exactly|precisely)\s+(\w+)\b|\bcrowd of\s+\w+|\b(\d+|ten|eleven|twelve|fifteen|twenty)\s+"
    r"(figures|townsfolk|villagers|people|fishermen|children|soldiers|guards)\b",
    re.IGNORECASE,
)
MONTAGE_RE = re.compile(r"\bmontage\b", re.IGNORECASE)


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def atomic_write(path, data):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".lint-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def resolve_source(src, run_dir):
    """Return an existing absolute path for a reference source, or None."""
    if not isinstance(src, str) or not src.strip():
        return None
    p = src.strip()
    cands = []
    if os.path.isabs(p):
        cands.append(p)
    else:
        rel = p
        # "output/run_x/characters/a.png" or "output/characters/a.png" → under run_dir
        m = re.match(r"^output/(run_\d{8}_\d{6}/)?(.*)$", rel)
        if m:
            rel = m.group(2)
        cands.append(os.path.join(run_dir, rel))
        cands.append(os.path.join(run_dir, p))
    for c in cands:
        if os.path.isfile(c):
            return os.path.abspath(c)
    # sibling fallbacks: same directory, canonical names
    base = cands[0]
    d = os.path.dirname(base)
    if os.path.isdir(d):
        stem = os.path.basename(d)
        for name in (f"{stem}.png", f"{stem}_side.png", f"{stem}_three_quarter.png",
                     f"{stem}_front.png"):
            c = os.path.join(d, name)
            if os.path.isfile(c):
                return os.path.abspath(c)
        pngs = sorted(f for f in os.listdir(d) if f.lower().endswith(".png"))
        if pngs:
            return os.path.abspath(os.path.join(d, pngs[0]))
    return None


def lint(shots, run_dir, fix):
    issues = []
    manifests_exist = all(os.path.isfile(os.path.join(run_dir, m))
                          for m in ("character_manifest.json", "location_manifest.json"))

    def add(shot_id, kind, blocking, msg, fixed=False):
        issues.append({"shot_id": shot_id, "type": kind, "blocking": blocking,
                       "message": msg, "auto_fixed": fixed})

    for s in shots:
        sid = s.get("shot_id", "?")
        chars = s.get("characters_in_frame") or []
        action = str(s.get("action_description") or "")
        cam = s.get("camera") or {}
        shot_type = str(cam.get("shot_type") or "").lower().replace(" ", "-")
        dur = float(s.get("duration_seconds") or 0)
        audio = s.get("audio") or {}
        has_dialogue = bool(audio.get("dialogue"))
        ks = s.get("keyframe_strategy") or {}
        mode = str(ks.get("mode") or "reference").lower()

        # ── blocking ──
        beats = [b for b in re.split(r";\s*", action) if b.strip()]
        if MONTAGE_RE.search(action) or len(beats) >= 3:
            add(sid, "montage_in_one_shot", True,
                f"{len(beats)} beats in one {dur:g} s shot ('{action[:80]}…') — one beat per clip; "
                "split into one shot per beat, 5–8 s each")
        if len(chars) > MAX_NAMED:
            add(sid, "too_many_named", True,
                f"{len(chars)} named characters in frame (max {MAX_NAMED}); extras become "
                "'a few distant figures, backs to camera' in action_description")
        if len(chars) > 1:
            acts = [str(c.get("action") or "").strip() for c in chars]
            if len(set(acts)) < len(acts):
                add(sid, "copied_action", True,
                    "two or more characters carry the identical action string — write each "
                    "character's own action, hand_position and expression")

        # ── advisory ──
        for field, text in (("action_description", action),
                            ("cut_in", str((s.get("edit") or {}).get("cut_in") or "")),
                            ("cut_out", str((s.get("edit") or {}).get("cut_out") or ""))):
            m = COUNTED_RE.search(text)
            if m:
                add(sid, "counted_crowd", False,
                    f"{field} counts a crowd ('{m.group(0)}') — H3 cannot count; say 'a few' / 'several'")
                break
        exit_re = re.compile(r"\b(climbs|flies|sails|drifts|exits?|leaves|goes)\s+(out|off)\b|out of (the )?frame|out of shot", re.IGNORECASE)
        co = str((s.get("edit") or {}).get("cut_out") or "")
        if exit_re.search(co):
            add(sid, "exit_as_cut_point", False,
                f"cut_out scripts something leaving the frame ('{co[:70]}') — H3 cannot time an exit; "
                "cut on the character's look or hands instead, keep moving props holding station")
        if shot_type == "extreme-wide" and chars:
            add(sid, "wide_with_faces", False,
                f"extreme-wide shot with {len(chars)} named character(s) — at 768p faces and hands "
                "are unreadable; make them silhouettes with backs to camera, or step in to wide/medium")
        if dur > MAX_SILENT_S and not has_dialogue:
            if fix:
                s["duration_seconds"] = MAX_SILENT_S
                add(sid, "duration", False, f"{dur:g} s without dialogue → 10 s", fixed=True)
            else:
                add(sid, "duration", False, f"{dur:g} s without dialogue (max {MAX_SILENT_S:g} s)")
        elif 0 < dur < 4.5:
            add(sid, "duration", False,
                f"{dur:g} s is under H3's trained minimum (124 frames = 5.17 s); the render is "
                "rounded up and the editor trims")
        refs = ks.get("reference_images") or []
        has_plate = any("location" in str(r.get("role") or "").lower() or "/locations/" in str(r.get("source") or "")
                        for r in refs if isinstance(r, dict))
        if not chars and mode in ("reference", "ref2va") and has_plate:
            if fix:
                ks["mode"] = "first_frame_only"
                ks["first_frame_source"] = "location plate as literal first frame (i2va holds the framing; ref2va re-frames)"
                s["keyframe_strategy"] = ks
                add(sid, "plate_mode", False, "0 characters in mode reference → first_frame_only from the plate", fixed=True)
            else:
                add(sid, "plate_mode", False,
                    "0-character shot in mode reference — ref2va drifts away from the plate's framing "
                    "(S01_001 ×10); use first_frame_only with the plate as first frame")
        if manifests_exist:
            kept = []
            for r in refs:
                if not isinstance(r, dict):
                    continue
                src = r.get("source")
                res = resolve_source(src, run_dir)
                if res is None:
                    add(sid, "missing_reference", False, f"reference not on disk and no sibling found: {src} — dropped" if fix else f"reference not on disk: {src}", fixed=fix)
                    if not fix:
                        kept.append(r)
                    continue
                if fix and os.path.abspath(str(src)) != res and not str(src).endswith(os.path.basename(res)):
                    add(sid, "missing_reference", False, f"{src} → {res}", fixed=True)
                    r = dict(r, source=res, declared_source=src)
                kept.append(r)
            if fix:
                ks["reference_images"] = kept
                s["keyframe_strategy"] = ks
    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--fix", action="store_true", help="apply mechanical fixes in place")
    ap.add_argument("--scene", default=None, help="lint only the shots of this scene_id")
    a = ap.parse_args()
    try:
        data = load(a.shots)
    except Exception as e:  # noqa: BLE001
        print(f"shot_list_lint: cannot read {a.shots}: {e}", file=sys.stderr)
        return 2
    shots = data.get("shots") if isinstance(data, dict) else data
    if not isinstance(shots, list):
        print("shot_list_lint: no 'shots' list", file=sys.stderr)
        return 2
    subset = [s for s in shots if not a.scene or s.get("scene_id") == a.scene]
    issues = lint(subset, a.run_dir, a.fix)
    if a.fix and any(i["auto_fixed"] for i in issues):
        atomic_write(a.shots, data)
    blocking = [i for i in issues if i["blocking"]]
    report = {"shots": len(subset), "scene": a.scene, "issues": issues, "blocking": len(blocking),
              "advisory": len(issues) - len(blocking),
              "auto_fixed": sum(1 for i in issues if i["auto_fixed"])}
    if a.out:
        atomic_write(a.out, report)
    by_type = {}
    for i in issues:
        by_type[i["type"]] = by_type.get(i["type"], 0) + 1
    summary = ", ".join(f"{k} {v}" for k, v in sorted(by_type.items())) or "clean"
    print(f"[shot_list_lint] {len(subset)} shots{' of ' + a.scene if a.scene else ''}: {len(blocking)} blocking, "
          f"{len(issues) - len(blocking)} advisory ({summary})"
          + (f" -> {a.out}" if a.out else ""))
    for i in blocking[:12]:
        print(f"  BLOCK {i['shot_id']} {i['type']}: {i['message']}")
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
