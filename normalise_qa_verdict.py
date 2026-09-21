#!/usr/bin/env python3
"""Guarantee $CLIP_DIR/qa_verdict.json is VALID JSON with a readable `verdict`.

WHY THIS EXISTS
qa-inspector writes its rich structured report to `qa_report.json` using its own tools, then
prints a markdown review to stdout. run_dsh_agent captures that stdout as `qa_verdict.json`, so
the file run.sh actually reads is prose:

    ### Verdict: FAIL (marginal)

`json.load()` rejects it, and run.sh's verdict read is:

    try:    print(json.load(f).get('verdict','FAIL'))
    except: print('FAIL')

so the exception silently forces **FAIL**. For a clip that genuinely failed that is accidentally
the right answer — but for a clip that PASSED it is a false FAIL, which burns all MAX_RETRY
attempts on expensive MiniMax H3 re-renders and ends with the clip ESCALATED. Same class of bug
as the prose-wrapped artifacts, different filename: the agent chose `qa_report.json`, so the
"keep the agent-written file" branch in run_dsh_agent can't see it.

WHAT IT DOES
Resolves the real verdict, most reliable source first, and rewrites `qa_verdict.json` as valid
JSON (atomically) while preserving the agent's corrections/measurements for the reviewer:

  1. `qa_report.json` -> ['verdict']        (agent-written, structured — authoritative)
  2. any JSON object inside `qa_verdict.json` that carries a 'verdict'
  3. regex over the prose:  verdict  ...  PASS|FAIL
  4. 'FAIL' as a loud last resort

USAGE
    python3 normalise_qa_verdict.py <clip_dir> [retry_count]

Exit codes: 0 = verdict resolved (file now valid JSON), 1 = nothing to work with.
"""
import json
import os
import re
import sys
import tempfile


def load_json(path):
    """Return a dict if `path` holds valid JSON, else None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def json_objects_in(text):
    """Every top-level balanced {...} in `text` that parses to a dict (brace-depth safe)."""
    found, depth, start, in_str, esc = [], 0, None, False, False
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
                try:
                    obj = json.loads(text[start : i + 1])
                    if isinstance(obj, dict):
                        found.append(obj)
                except Exception:
                    pass
    return found


def norm_verdict(value):
    """Normalise to 'PASS'/'FAIL'/None."""
    if value is None:
        return None
    v = str(value).strip().strip("*`\"' ").upper()
    if v.startswith("PASS") or v in ("OK", "APPROVED", "TRUE", "YES"):
        return "PASS"
    if v.startswith("FAIL") or v in ("NG", "REJECTED", "FALSE", "NO"):
        return "FAIL"
    return None


def find_verdict(clip_dir):
    """Resolve (verdict, source, rich_report). Most reliable source first."""
    verdict_path = os.path.join(clip_dir, "qa_verdict.json")
    report_path = os.path.join(clip_dir, "qa_report.json")

    rich = load_json(report_path)
    if rich:
        v = norm_verdict(rich.get("verdict"))
        if v:
            return v, "qa_report.json['verdict']", rich

    existing = load_json(verdict_path)
    if existing:
        v = norm_verdict(existing.get("verdict"))
        if v:
            return v, "qa_verdict.json (already valid JSON)", rich or existing

    try:
        with open(verdict_path, encoding="utf-8", errors="replace") as fh:
            prose = fh.read()
    except Exception:
        prose = ""

    for obj in json_objects_in(prose):
        v = norm_verdict(obj.get("verdict"))
        if v:
            return v, "JSON object embedded in prose", rich or obj

    m = re.search(r"verdict[^A-Za-z]{0,24}(PASS|FAIL)", prose, re.IGNORECASE)
    if m:
        return m.group(1).upper(), "regex over prose verdict line", rich

    return None, "unresolved", rich


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    clip_dir = sys.argv[1]
    retry_arg = sys.argv[2] if len(sys.argv) > 2 else None

    verdict, source, rich = find_verdict(clip_dir)
    if verdict is None:
        print(f"qa-verdict: no verdict found in {clip_dir} — leaving file as-is")
        return 1

    verdict_path = os.path.join(clip_dir, "qa_verdict.json")
    out = {
        "_note": "normalised by normalise_qa_verdict.py — see pipeline.log",
        "shot_id": (rich or {}).get("shot_id") or os.path.basename(clip_dir.rstrip("/")),
        "verdict": verdict,
        "_verdict_source": source,
    }

    # carry the agent's review material so the reviewer's CORRECTIONS block stays useful
    for key in ("summary", "corrections", "scoring", "categories", "clip_metadata", "risk_flags"):
        if isinstance(rich, dict) and key in rich:
            out[key] = rich[key]

    # retry_count: keep whatever the clip already recorded, else take run.sh's counter
    prev = load_json(verdict_path) or {}
    rc = prev.get("retry_count", rich.get("retry_count") if isinstance(rich, dict) else None)
    if rc is None and retry_arg is not None:
        try:
            rc = int(retry_arg)
        except ValueError:
            rc = None
    if rc is not None:
        out["retry_count"] = rc

    fd, tmp = tempfile.mkstemp(dir=clip_dir, prefix=".qa_verdict-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    os.replace(tmp, verdict_path)

    print(f"qa-verdict: resolved {verdict} via {source} -> {verdict_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
