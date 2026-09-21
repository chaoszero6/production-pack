#!/usr/bin/env python3
"""Extract a JSON object from messy agent output — nested, fenced, or prose-wrapped.

WHY THIS EXISTS
run.sh's historical extractor was the regex `\{(?:[^{}]|\{[^{}]*\})*\}`, which only matches
ONE level of nesting. Real production JSON nests 2+ deep, so the regex returned a small INNER
object (here the `visualStyle` sub-object) and the step died with a bogus
    Missing required keys: ['title','logline','theme','characters','locations','acts','scenes']
against a perfectly valid 36 KB model answer. Braces must be counted, not pattern-matched.

SECOND REASON (learned the hard way)
run_dsh_agent() only applied JSON extraction to the `story-creator` preset. Every other preset
used `mv "$raw_output" "$output_file"`, copying the agent's raw stdout verbatim. Agents routinely
frame the object with prose ("I have everything I need. Here's my review and the built prompt.")
or a ```json fence, so the artifact on disk was prose+JSON — a document json.load() rejects.
Downstream consumers that swallow parse errors then take the WRONG branch:

    HAS_DIALOGUE -> except: 'no'   -> dialogue audio skipped on every lip-sync clip
    VERDICT      -> except: 'FAIL' -> a perfect clip burns all MAX_RETRY attempts

So extraction runs for every preset now, not just story-creator.

USAGE
    python3 extract_json.py <raw_output_file> <out.json> [required_key ...]

Exit codes: 0 = wrote <out.json>, 1 = no acceptable object found.

Also worth checking before declaring an agent step failed: agents often write the artifact to a
file with their own tools, so look for a recently modified *.json under $DSH_DIR.
"""
import json
import re
import sys


def balanced_objects(text):
    """Yield every top-level balanced {...} substring, honouring strings and escapes."""
    out, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                out.append(text[start : i + 1])
    return out


def candidates(raw):
    """Every dict we can parse, most-likely first (whole body, fenced blocks, then scans)."""
    seen = []
    try:
        seen.append(json.loads(raw.strip()))
    except Exception:
        pass
    for block in re.findall(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL):
        try:
            seen.append(json.loads(block.strip()))
        except Exception:
            pass
    for obj in balanced_objects(raw):
        try:
            seen.append(json.loads(obj))
        except Exception:
            pass
    return [c for c in seen if isinstance(c, dict)]


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    raw_path, out_path, required = sys.argv[1], sys.argv[2], sys.argv[3:]

    raw = open(raw_path, encoding="utf-8", errors="replace").read()
    best = None
    for cand in candidates(raw):
        # prefer a candidate that satisfies every required key
        if all(k in cand for k in required):
            best = cand
            break
        if best is None or len(cand) > len(best):
            best = cand

    if best is None:
        print("no parseable JSON object found")
        return 1

    missing = [k for k in required if k not in best]
    if missing:
        print(f"Missing required keys: {missing} (largest object had: {list(best)[:8]})")
        return 1

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(best, fh, indent=2)
    print(f"OK: wrote {out_path} ({len(json.dumps(best))} chars, {len(best)} top-level keys)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
