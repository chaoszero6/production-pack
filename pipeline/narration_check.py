#!/usr/bin/env python3
"""narration_check.py --run-dir D (--plan | --rendered) — one block per narration passage, once.

Canonical audio/narration/narration_meta.json:
    {"blocks": [{"id": "N1", "scene_id": "scene_001", "span_clips": ["S01_001", "S01_002"],
                 "starts_on": "S01_001", "text": "...", "file": "N1.wav",
                 "duration_s": 12.3, "status": "ok"}]}

assemble.py places every block exactly once at film_start(starts_on) + 0.4 s, so the plan must
be unambiguous.  Checks (errors unless marked warn):
  - unique block ids; starts_on == span_clips[0]; no clip in two spans
  - no span clip carries audio.dialogue in shot_list.json (narration never over a line)
  - duplicate normalised text (first 80 chars, lower-case, whitespace collapsed) -> the same
    passage planned twice (this is what produced the double narration)
  - audio/narration/tts_jobs.json ids match the block ids 1:1
  --rendered: every block has duration_s and its wav exists; duration > span duration + 0.5 s (warn)

Prints a one-screen report and writes audio/narration/narration_check.json.  Exit 0 ok / 1 errors.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def norm_text(t: str) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip().lower()[:80]


def load(path: Path):
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def shot_index(shot_list) -> tuple[list[str], dict]:
    shots = shot_list.get("shots", shot_list) if isinstance(shot_list, dict) else shot_list
    if isinstance(shots, dict):
        shots = [dict(s, shot_id=s.get("shot_id") or k) for k, s in shots.items()]
    order, meta = [], {}
    for s in shots:
        sid = s.get("shot_id") or s.get("id")
        if not sid:
            continue
        audio = s.get("audio") or {}
        lines = [d for d in (audio.get("dialogue") or []) if isinstance(d, dict)
                 and re.search(r"[A-Za-z0-9]", str(d.get("line") or d.get("text") or ""))]
        order.append(sid)
        meta[sid] = {"has_dialogue": bool(lines), "duration_s": float(s.get("duration_seconds") or 0.0)}
    return order, meta


def check(run_dir: Path, rendered: bool) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    ndir = run_dir / "audio" / "narration"
    meta = load(ndir / "narration_meta.json")
    if meta is None:
        return {"errors": [f"missing {ndir / 'narration_meta.json'}"], "warnings": [], "blocks": 0}
    blocks = meta.get("blocks") if isinstance(meta, dict) else meta
    if not isinstance(blocks, list):
        return {"errors": ["narration_meta.json: 'blocks' must be a list"], "warnings": [], "blocks": 0}
    shot_list = load(run_dir / "shot_list.json")
    order, shots = shot_index(shot_list) if shot_list is not None else ([], {})
    if shot_list is None:
        warnings.append("shot_list.json missing — span/dialogue checks skipped")

    ids: dict[str, int] = {}
    seen_clip: dict[str, str] = {}
    texts: dict[str, list[str]] = {}
    for i, b in enumerate(blocks):
        bid = str(b.get("id") or f"<block {i}>")
        if bid in ids:
            errors.append(f"duplicate block id {bid}")
        ids[bid] = i
        span = list(b.get("span_clips") or [])
        starts = b.get("starts_on")
        if not span:
            errors.append(f"{bid}: empty span_clips")
        if not starts:
            errors.append(f"{bid}: no starts_on")
        elif span and starts != span[0]:
            errors.append(f"{bid}: starts_on {starts} != span_clips[0] {span[0]}")
        for c in span:
            if c in seen_clip and seen_clip[c] != bid:
                errors.append(f"clip {c} is in two spans ({seen_clip[c]} and {bid})")
            seen_clip[c] = bid
            if shots and c not in shots:
                errors.append(f"{bid}: span clip {c} is not in shot_list.json")
            elif shots and shots[c]["has_dialogue"]:
                errors.append(f"{bid}: span clip {c} has dialogue — narration never plays over a line")
        if not str(b.get("text") or "").strip():
            errors.append(f"{bid}: empty text")
        texts.setdefault(norm_text(b.get("text")), []).append(bid)
        if rendered:
            dur = b.get("duration_s")
            if not isinstance(dur, (int, float)):
                errors.append(f"{bid}: duration_s missing (not rendered?)")
            fname = b.get("file") or f"{bid}.wav"
            f = Path(fname) if Path(fname).is_absolute() else ndir / fname
            if not f.is_file():
                errors.append(f"{bid}: audio file missing ({f})")
            if b.get("status") not in (None, "ok"):
                errors.append(f"{bid}: status {b.get('status')}")
            if isinstance(dur, (int, float)) and shots:
                span_dur = sum(shots[c]["duration_s"] for c in span if c in shots)
                if dur > span_dur + 0.5:
                    warnings.append(f"{bid}: {dur:.1f} s narration over a {span_dur:.1f} s span (runs {dur - span_dur:.1f} s long)")
    for t, bids in texts.items():
        if t and len(bids) > 1:
            errors.append(f"duplicate narration text in blocks {', '.join(bids)}: \"{t[:60]}…\"")

    jobs = load(ndir / "tts_jobs.json")
    if jobs is not None:
        jl = jobs.get("jobs") if isinstance(jobs, dict) else jobs
        job_ids = [str(j.get("id")) for j in (jl or []) if isinstance(j, dict)]
        if sorted(job_ids) != sorted(ids):
            missing = sorted(set(ids) - set(job_ids))
            extra = sorted(set(job_ids) - set(ids))
            dup = sorted({j for j in job_ids if job_ids.count(j) > 1})
            errors.append("tts_jobs.json ids do not match the blocks 1:1"
                          + (f"; blocks without a job: {missing}" if missing else "")
                          + (f"; jobs without a block: {extra}" if extra else "")
                          + (f"; duplicate job ids: {dup}" if dup else ""))
    elif not rendered:
        warnings.append("tts_jobs.json missing — job/block match not checked")
    return {"errors": errors, "warnings": warnings, "blocks": len(blocks), "mode": "rendered" if rendered else "plan"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--plan", action="store_true", help="check the plan (default)")
    g.add_argument("--rendered", action="store_true", help="also require rendered files and durations")
    a = ap.parse_args(argv)
    run_dir = Path(a.run_dir)
    res = check(run_dir, rendered=a.rendered)
    out = run_dir / "audio" / "narration" / "narration_check.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    except OSError as exc:
        res["warnings"].append(f"could not write {out}: {exc}")
    print(f"narration_check ({res.get('mode', 'plan')}): {res['blocks']} block(s), {len(res['errors'])} error(s), "
          f"{len(res['warnings'])} warning(s)")
    for e in res["errors"]:
        print(f"  ERROR   {e}")
    for w in res["warnings"]:
        print(f"  warning {w}")
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
