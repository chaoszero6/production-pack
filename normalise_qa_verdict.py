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

Note the inspector does not always write next to the clip: sometimes the report lands in
`$RUN_DIR/qa/<SHOT_ID>/qa_report.json` instead of `$CLIP_DIR/qa_report.json`. Both locations are
searched, newest mtime wins.

ASSEMBLY-FIXABLE FAILURES (second, larger waste)
The inspector frequently returns verdict FAIL while stating in the same report that the shot needs
NO re-render:

    "re_render_recommended": false
    "recommended_action": "ASSEMBLY_FIX_ONLY"

Typical cause is a delivery/asset defect shared by *every* clip of a mode — e.g. ref2va clips
carrying the native H3 AAC track even though shot_list.json sets `use_native_h3_audio=false`, or a
dialogue stem placed at the wrong offset (content correct, placement wrong). run.sh cannot see any
of that: it only reads `verdict`, so it loops back and burns a full H3 re-render (~20-85 min,
~660s GPU) and the retry reproduces the identical, perfectly fixable failure by construction.

So: when the inspector itself says no re-render is warranted AND no *structural* defect blocks it,
this script downgrades the verdict to PASS and records the required post fixes in
`$RUN_DIR/assembly_fixes.json` for Phase 3a/3d to apply. The FAIL is never hidden — it stays in
`qa_verdict_original`, `assembly_fix_required: true` and `assembly_fixes.json`.

Downgrade guard rails (ALL must hold):
  * rich report present, verdict FAIL, `re_render_recommended` is false (or ASSEMBLY_FIX_ONLY)
  * no correction of priority critical/blocker in a STRUCTURAL category
  * every non-assembly category scores >= STRUCTURAL_FLOOR (0.70)

WHAT IT DOES
Resolves the real verdict, most reliable source first, and rewrites `qa_verdict.json` as valid
JSON (atomically) while preserving the agent's corrections/measurements for the reviewer:

  1. `qa_report.json` (clip dir or run qa dir, newest) -> ['verdict']  (structured — authoritative)
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
from datetime import datetime

# Categories whose failure is fixed in post-production (assembly), not by re-rendering.
ASSEMBLY_CATEGORIES = {
    "audio_quality",
    "lip_sync",
    "shot_list_data",
    "delivery",
    "encoding",
    "bitrate",
    "subtitles",
}

# A structural category scoring below this is a real artifact -> always re-render.
STRUCTURAL_FLOOR = 0.70

BLOCKING_PRIORITIES = {"critical", "blocker", "fatal"}

# Per-category gate from run.sh's QA prompt.
PASS_THRESHOLD = 0.85

# The inspector sometimes states "no re-render needed" ONLY in prose inside a correction, with no
# structured field anywhere. S01_003 shipped exactly that way ("POST-PRODUCTION, NOT A RE-RENDER",
# +0.80s stem offset, all structural categories >= 0.88) and defaulting to re-render there burned a
# full H3 render per retry. Recognise the wording.
ASSEMBLY_PROSE_RE = re.compile(
    r"post-?production|not a re-?render|without re-?rendering|no re-?render|"
    r"fixable in (?:final )?assembly|in final assembly|assembly fix",
    re.IGNORECASE,
)


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


def run_dir_for(clip_dir):
    """$RUN_DIR for a $RUN_DIR/clips/<SHOT_ID> path."""
    cd = os.path.abspath(clip_dir.rstrip("/"))
    parent = os.path.dirname(cd)
    if os.path.basename(parent) == "clips":
        return os.path.dirname(parent)
    return parent


def candidate_reports(clip_dir):
    """qa_report.json locations, newest mtime first (inspector is inconsistent about where it writes).

    A report older than clip.mp4 is from a PRIOR take and must not be bound to this
    clip — a crashed/absent inspector would otherwise re-commit yesterday's FAIL.
    """
    shot_id = os.path.basename(os.path.abspath(clip_dir.rstrip("/")))
    paths = [
        os.path.join(clip_dir, "qa_report.json"),
        os.path.join(run_dir_for(clip_dir), "qa", shot_id, "qa_report.json"),
    ]
    # Skill historically said relative output/qa/{shot_id}/ — under dsh CWD=$DSH_DIR
    # that lands in the harness sandbox, not the run dir. Search there too.
    dsh_dir = os.environ.get("DSH_DIR") or "/root/desktop/deepseek-harness"
    paths.append(os.path.join(dsh_dir, "output", "qa", shot_id, "qa_report.json"))
    paths.append(os.path.join(dsh_dir, "qa", shot_id, "qa_report.json"))

    def mtime(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return -1.0

    clip_mp4 = os.path.join(clip_dir, "clip.mp4")
    clip_m = mtime(clip_mp4)
    fresh = []
    for p in paths:
        if not os.path.isfile(p):
            continue
        # Allow 2s clock skew; drop clearly-older prior-take reports.
        if clip_m > 0 and mtime(p) + 2.0 < clip_m:
            continue
        fresh.append(p)
    return sorted(fresh, key=mtime, reverse=True)


def find_verdict(clip_dir):
    """Resolve (verdict, source, rich_report). Most reliable source first."""
    verdict_path = os.path.join(clip_dir, "qa_verdict.json")
    clip_mp4 = os.path.join(clip_dir, "clip.mp4")

    def _mtime(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return -1.0

    clip_m = _mtime(clip_mp4)

    rich = None
    rich_path = None
    for path in candidate_reports(clip_dir):
        data = load_json(path)
        if data and norm_verdict(data.get("verdict")):
            rich, rich_path = data, path
            break

    if rich:
        v = norm_verdict(rich.get("verdict"))
        src = "qa_report.json['verdict']" + (f" ({rich_path})" if rich_path else "")
        return v, src, rich

    # An existing qa_verdict.json older than the current clip is a prior take —
    # ignore it (agent crash must not re-bind last take's FAIL/PASS).
    existing = load_json(verdict_path)
    if existing and not (clip_m > 0 and _mtime(verdict_path) + 2.0 < clip_m):
        v = norm_verdict(existing.get("verdict"))
        if v:
            return v, "qa_verdict.json (already valid JSON)", rich or existing

    try:
        with open(verdict_path, encoding="utf-8", errors="replace") as fh:
            prose = fh.read()
    except Exception:
        prose = ""
    if clip_m > 0 and _mtime(verdict_path) + 2.0 < clip_m:
        prose = ""

    for obj in json_objects_in(prose):
        v = norm_verdict(obj.get("verdict"))
        if v:
            return v, "JSON object embedded in prose", rich or obj

    m = re.search(r"verdict[^A-Za-z]{0,24}(PASS|FAIL)", prose, re.IGNORECASE)
    if m:
        return m.group(1).upper(), "regex over prose verdict line", rich

    return None, "unresolved", rich


def _overall(rich):
    """Overall score, tolerant of the several shapes qa-inspector reports use."""
    rich = rich or {}
    sc = rich.get("score_calculation")
    for cand in (
        (sc or {}).get("weighted_overall") if isinstance(sc, dict) else None,
        (sc or {}).get("overall") if isinstance(sc, dict) else None,
        rich.get("overall_score"),
        rich.get("weighted_overall"),
        rich.get("overall"),
    ):
        if isinstance(cand, (int, float)):
            return round(float(cand), 4)
    return None


def category_scores(rich):
    """{category: float} from the inspector's category_scores / categories block."""
    raw = (rich or {}).get("category_scores") or (rich or {}).get("categories") or {}
    out = {}
    if isinstance(raw, dict):
        for key, val in raw.items():
            if isinstance(val, dict) and isinstance(val.get("score"), (int, float)):
                out[key] = float(val["score"])
            elif isinstance(val, (int, float)):
                out[key] = float(val)
    return out


def corrections_of(rich):
    c = (rich or {}).get("corrections")
    if c is None:
        return []
    return [x for x in (c if isinstance(c, list) else [c]) if isinstance(x, dict)]


def _explicit_assembly_signal(rich, corr):
    """(signal, why) where signal is True (assembly-only), False (re-render), or None (silent).

    The inspector says "no re-render needed" three different ways, and sometimes not at all:
      (a) `re_render_recommended: false`
      (b) `recommended_action: "ASSEMBLY_FIX_ONLY"`
      (c) prose only, inside a correction ("POST-PRODUCTION, NOT A RE-RENDER", "fixable in final
          assembly without re-rendering") — no structured field anywhere. S01_003 shipped as (c).
    """
    rrr = (rich or {}).get("re_render_recommended")
    action = str((rich or {}).get("recommended_action", "")).upper()
    if rrr is True:
        return False, "inspector recommended a re-render (re_render_recommended=true)"
    if rrr is False:
        return True, "re_render_recommended=false"
    if "ASSEMBLY_FIX_ONLY" in action:
        return True, f"recommended_action={action}"
    for c in corr:
        cat = str(c.get("category", "")).strip().lower()
        if cat and cat not in ASSEMBLY_CATEGORIES:
            continue
        text = " ".join(str(c.get(k, "")) for k in ("prompt_fix", "fix", "rule"))
        if ASSEMBLY_PROSE_RE.search(text):
            return True, f"prose directive in the {cat or 'unnamed'} correction"
    return None, "inspector did not state re_render_recommended=false / ASSEMBLY_FIX_ONLY"


def _structural_evidence(rich):
    """(clean: bool, why: str) — the render is structurally sound and only asset-level categories failed.

    Used only when the inspector stayed silent, so the call is made from measurements rather than
    from a missing field. Conservative: any sub-threshold structural category or a sub-threshold
    weighted overall keeps the re-render.
    """
    scores = category_scores(rich)
    if not scores:
        return False, "no category scores to judge"
    sub = {c: s for c, s in scores.items()
           if c.lower() not in ASSEMBLY_CATEGORIES and s < PASS_THRESHOLD}
    if sub:
        return False, "structural categories under threshold: " + ", ".join(
            f"{c} {s:.2f}" for c, s in sorted(sub.items()))
    overall = _overall(rich)
    if overall is not None and overall < PASS_THRESHOLD:
        return False, f"weighted overall {overall:.3f} < {PASS_THRESHOLD}"
    return True, ("every structural category clears the gate; only asset-level categories failed"
                  + (f" (overall {overall:.3f})" if overall is not None else ""))


def assembly_downgrade_check(rich):
    """Return (allowed, blocks, fixes, reason). Mirrors the guard rails in the docstring."""
    blocks, fixes = [], []
    corr = corrections_of(rich)

    signal, why = _explicit_assembly_signal(rich, corr)
    if signal is False:
        blocks.append(why)
    elif signal is None:
        clean, ev = _structural_evidence(rich)
        if clean:
            why = f"inferred from the measurements — {ev}"
        else:
            blocks.append(f"{why}; the evidence does not support an assembly-only fix ({ev})")

    fixes = [
        {
            k: c[k]
            for k in ("category", "priority", "rule", "prompt_fix", "fix")
            if k in c
        }
        for c in corr
    ]

    for c in corr:
        cat = str(c.get("category", "")).strip().lower()
        prio = str(c.get("priority", "")).strip().lower()
        if prio in BLOCKING_PRIORITIES and cat not in ASSEMBLY_CATEGORIES:
            blocks.append(f"critical structural correction: {cat or '?'}")

    for cat, score in category_scores(rich).items():
        if cat.lower() in ASSEMBLY_CATEGORIES:
            continue
        if score < STRUCTURAL_FLOOR:
            blocks.append(f"structural category {cat} {score:.2f} < floor {STRUCTURAL_FLOOR:.2f}")

    return (not blocks), blocks, fixes, why


def write_ledger(run_dir, shot_id, entry):
    """Upsert this clip's assembly work into $RUN_DIR/assembly_fixes.json (atomic)."""
    path = os.path.join(run_dir, "assembly_fixes.json")
    ledger = load_json(path) or {}
    ledger.setdefault("_note", (
        "Clips whose QA verdict was FAIL but the QA inspector itself reported "
        "re_render_recommended=false. run.sh treats these as PASS so the pipeline moves on; "
        "Phase 3 assembly MUST apply every fix listed here before muxing the final movie."
    ))
    ledger["updated_at"] = datetime.now().isoformat(timespec="seconds")
    clips = ledger.setdefault("clips", {})
    if entry is None:
        clips.pop(shot_id, None)
    else:
        clips[shot_id] = entry
    if not clips:
        ledger.pop("clips", None)

    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".assembly_fixes-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    clip_dir = sys.argv[1].rstrip("/")
    retry_arg = sys.argv[2] if len(sys.argv) > 2 else None

    verdict, source, rich = find_verdict(clip_dir)
    if verdict is None:
        # Unresolved = no fresh report and the existing qa_verdict.json is older than
        # clip.mp4 (prior take). Leaving that stale FAIL in place makes run.sh's
        # json.load().get('verdict') re-bind it and burn an H3 retry on an unjudged
        # clip. Neutralise it so the gate sees an empty verdict and takes the
        # infra-retry / escalate path instead.
        verdict_path_unres = os.path.join(clip_dir, "qa_verdict.json")
        if os.path.isfile(verdict_path_unres):
            stale = (
                os.path.join(clip_dir,
                             f"qa_verdict_stale_{int(datetime.now().timestamp())}.json")
            )
            try:
                os.replace(verdict_path_unres, stale)
                print(f"qa-verdict: no fresh verdict in {clip_dir} — "
                      f"moved prior-take verdict aside -> {stale}")
            except OSError as exc:
                print(f"qa-verdict: no verdict found in {clip_dir} — "
                      f"could not neutralise prior verdict ({exc})")
        else:
            print(f"qa-verdict: no verdict found in {clip_dir} — leaving file as-is")
        return 1

    dir_shot_id = os.path.basename(os.path.abspath(clip_dir))
    report_shot_id = (rich or {}).get("shot_id")
    if report_shot_id and str(report_shot_id) != dir_shot_id:
        print(f"qa-verdict: WARNING report shot_id '{report_shot_id}' != clip dir '{dir_shot_id}' "
              f"— using the clip dir (avoids mis-filing the assembly ledger)")
    shot_id = dir_shot_id
    run_dir = run_dir_for(clip_dir)
    verdict_path = os.path.join(clip_dir, "qa_verdict.json")
    original_verdict = verdict
    downgrade = False
    blocks, fixes, dg_reason = [], [], ""

    if verdict == "FAIL":
        downgrade, blocks, fixes, dg_reason = assembly_downgrade_check(rich)
        if downgrade:
            verdict = "PASS"

    out = {
        "_note": "normalised by normalise_qa_verdict.py — see pipeline.log",
        "shot_id": shot_id,
        "verdict": verdict,
        "_verdict_source": source,
    }

    # carry the agent's review material so the reviewer's CORRECTIONS block stays useful
    for key in ("summary", "corrections", "scoring", "categories", "category_scores",
                "clip_metadata", "risk_flags", "verdict_basis", "recommended_action",
                "re_render_recommended", "category_gate_failures"):
        if isinstance(rich, dict) and key in rich:
            out[key] = rich[key]

    if downgrade:
        out["qa_verdict_original"] = "FAIL"
        out["assembly_fix_required"] = True
        out["assembly_fixes"] = fixes
        out["_downgrade_reason"] = (
            "QA verdict FAIL, but no structural defect blocks a post fix and "
            f"{dg_reason} — no H3 re-render; fix in assembly (see assembly_fixes.json)"
        )
    elif original_verdict == "FAIL":
        out["re_render_required"] = True
        if blocks:
            out["_re_render_reason"] = "; ".join(blocks)

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
        fh.write("\n")
    os.replace(tmp, verdict_path)

    scores = category_scores(rich)
    gate = sorted(c for c, s in scores.items() if s < 0.85)
    ledger_path = None
    entry = None
    if downgrade:
        entry = {
            "qa_verdict": "FAIL",
            "assembly_fix_required": True,
            # Phase 3 concatenates clip_4k60.mp4, NOT clip.mp4 — name it explicitly so the
            # editor agent cannot strip the rejected track from the wrong file.
            "audio_fix_target": (
                f"clips/{shot_id}/clip_4k60.mp4 (the file Phase 3 concatenates); "
                f"clips/{shot_id}/clip.mp4 carries the same rejected native track"
            ),
            "weighted_overall": _overall(rich),
            "threshold": (rich or {}).get("threshold"),
            "category_scores": scores,
            "categories_under_threshold": gate,
            "recommended_action": (rich or {}).get("recommended_action"),
            "downgrade_reason": dg_reason,
            "verdict_basis": (rich or {}).get("verdict_basis"),
            "fixes": fixes,
        }
        ledger_path = write_ledger(run_dir, shot_id, entry)

    print(f"qa-verdict: resolved {verdict} via {source} -> {verdict_path}")
    if downgrade:
        print(f"qa-verdict: [{shot_id}] FAIL was ASSEMBLY-FIXABLE ({dg_reason}) "
              f"— downgraded to PASS, {len(fixes)} post fix(es) -> {ledger_path}")
        for cat in gate:
            print(f"    under 0.85: {cat} {scores[cat]:.2f}")
    elif original_verdict == "FAIL":
        print(f"qa-verdict: [{shot_id}] FAIL stands — re-render required"
              + (f" ({'; '.join(blocks)})" if blocks else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
