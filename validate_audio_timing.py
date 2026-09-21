#!/usr/bin/env python3
"""Validate that dialogue fits within clip durations and narration doesn't overlap dialogue.

Run after Director creates the shot list to catch timing problems BEFORE generation.

USAGE
    python3 validate_audio_timing.py <shot_list.json> [--fix]

    --fix: auto-fix issues by extending clip durations and splitting narration spans

Exit codes: 0 = all valid, 1 = issues found (printed to stdout)
"""
import json
import sys
import math

WORDS_PER_SEC = 2.5  # conversational speech pace
BUFFER_SEC = 1.0     # safety buffer


def estimate_duration(text):
    """Estimate speech duration in seconds from text."""
    words = len(text.split())
    return words / WORDS_PER_SEC


def validate(shot_list_path, fix=False):
    with open(shot_list_path) as f:
        data = json.load(f)

    shots = data.get("shots", [])
    issues = []
    fixes_applied = 0

    # Build a map of which clips are in narration spans
    narration_spans = {}  # clip_id -> narration text
    for shot in shots:
        audio = shot.get("audio", {})
        narr = audio.get("narration")
        if narr and isinstance(narr, dict):
            span = narr.get("span_clips", [shot["shot_id"]])
            for clip_id in span:
                narration_spans[clip_id] = narr.get("text", "")
        elif narr and isinstance(narr, str) and narr.strip():
            # Old format: narration as plain string (single clip)
            narration_spans[shot["shot_id"]] = narr

    for shot in shots:
        sid = shot["shot_id"]
        dur = shot.get("duration_seconds", 6)
        audio = shot.get("audio", {})

        # --- Check 1: Dialogue fits in clip ---
        for dlg in audio.get("dialogue", []):
            line = dlg.get("line", "")
            words = len(line.split())
            est = estimate_duration(line)
            max_dur = dur - BUFFER_SEC

            if est > max_dur:
                msg = (f"{sid}: Dialogue too long — {words} words (~{est:.1f}s) "
                       f"exceeds clip duration {dur}s (max {max_dur:.1f}s)")
                issues.append(msg)

                if fix:
                    new_dur = math.ceil(est + BUFFER_SEC + 1)
                    new_dur = min(new_dur, 15)  # H3 max
                    shot["duration_seconds"] = new_dur
                    dlg["word_count"] = words
                    dlg["estimated_duration"] = round(est, 1)
                    fixes_applied += 1
                    issues[-1] += f" → FIXED: clip extended to {new_dur}s"

        # --- Check 2: Narration doesn't overlap dialogue ---
        has_dialogue = bool(audio.get("dialogue"))
        has_narration = sid in narration_spans

        if has_dialogue and has_narration:
            msg = (f"{sid}: Narration overlaps dialogue — "
                   f"narration span includes this clip but it has dialogue. "
                   f"Narration: \"{narration_spans[sid][:60]}...\"")
            issues.append(msg)

            if fix:
                # Remove this clip from narration span
                narr = audio.get("narration")
                if isinstance(narr, dict) and "span_clips" in narr:
                    narr["span_clips"] = [c for c in narr["span_clips"] if c != sid]
                    if not narr["span_clips"]:
                        audio["narration"] = None
                elif isinstance(narr, str):
                    audio["narration"] = None
                del narration_spans[sid]
                fixes_applied += 1
                issues[-1] += " → FIXED: removed from narration span"

    # --- Check 3: Narration duration fits span ---
    seen_narrations = set()
    for shot in shots:
        audio = shot.get("audio", {})
        narr = audio.get("narration")
        if not narr:
            continue
        if isinstance(narr, dict):
            text = narr.get("text", "")
            span = narr.get("span_clips", [shot["shot_id"]])
        elif isinstance(narr, str):
            text = narr
            span = [shot["shot_id"]]
        else:
            continue

        narr_key = text[:50]
        if narr_key in seen_narrations:
            continue
        seen_narrations.add(narr_key)

        est = estimate_duration(text)
        span_dur = sum(
            s.get("duration_seconds", 6) for s in shots if s["shot_id"] in span
        )
        if est > span_dur:
            msg = (f"{shot['shot_id']}: Narration too long for span — "
                   f"{len(text.split())} words (~{est:.1f}s) "
                   f"exceeds span duration {span_dur}s ({len(span)} clips)")
            issues.append(msg)

    if fix and fixes_applied > 0:
        with open(shot_list_path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Applied {fixes_applied} fixes to {shot_list_path}")

    return issues


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    path = sys.argv[1]
    fix = "--fix" in sys.argv

    issues = validate(path, fix=fix)

    if issues:
        print(f"Found {len(issues)} timing issue(s):")
        for i, msg in enumerate(issues, 1):
            print(f"  {i}. {msg}")
        return 0 if fix else 1
    else:
        print("All dialogue/narration timing is valid.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
