#!/usr/bin/env python3
"""edit_check.py — does every clip-to-clip boundary CUT, or does it jump?

Measures what the editing-grammar skill (.dsh/skills/editing-grammar.md) asks for,
on the ORDERED list of approved clips, before assembly:

  per boundary A|B
    jump_cut        same subject + same shot size + near-identical boundary frames
    dead_boundary   tail of A and head of B are both static holds of the same subject
    light_mismatch  luma / warmth jump between the boundary frames of one scene
    rhythm          third consecutive clip of the same shot size
    internal_cut    a hard cut INSIDE a clip (frame-difference spike) — H3 did a second shot
    axis / readable props cannot be measured here -> listed as "manual" for the reviewer

  cut_score 0..1 per boundary (threshold 0.80) + fix suggestion, written to
  <run_dir>/final/edit_report.json, plus a contact sheet of every boundary pair
  (<run_dir>/final/edit_boundaries.jpg, A-tail | B-head tiles) when Pillow is available.

Inputs (one of):
  --run-dir <dir>        reads <dir>/shot_list.json (Director schema) and <dir>/clips/<id>/clip.mp4
  --concat-list <txt>    ffmpeg concat file in play order (no shot metadata: structural checks only)
  --clips a.mp4 b.mp4 …  explicit order
Optional --shots <json> supplies metadata for --concat-list/--clips (studio.py shots.json or
Director shot_list.json are both understood).

Exit 0 = every boundary >= threshold, 1 = at least one boundary below, 2 = bad input.
Needs ffmpeg/ffprobe + numpy. Pillow optional (contact sheet).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

import numpy as np

SAMPLE_FPS = 10
EDGE_S = 0.6                 # head/tail window analysed on each side of a boundary
TH = 120                     # analysis resolution (w = TH*16/9)
TW = TH * 16 // 9
THRESHOLD = 0.80
SIZE_LADDER = ["extreme-wide", "wide", "medium-wide", "medium", "medium-close-up",
               "close-up", "extreme-close-up"]


# ---------------------------------------------------------------- helpers
def shot_size(text: str) -> str | None:
    t = (text or "").lower().replace("_", "-").replace(" ", "-")
    if "extreme-close" in t or "ecu" == t:
        return "extreme-close-up"
    if "medium-close" in t or "mcu" == t:
        return "medium-close-up"
    if "close" in t or t in ("cu", "closeup"):
        return "close-up"
    if "medium-wide" in t or "cowboy" in t:
        return "medium-wide"
    if "extreme-wide" in t or "establishing" in t or "landscape" in t:
        return "extreme-wide"
    if "medium" in t or t in ("ms", "two-shot"):
        return "medium"
    if "wide" in t or "full-body" in t or "full" in t:
        return "wide"
    if "insert" in t or "pov" in t:
        return "close-up"
    return None


def size_steps(a: str | None, b: str | None) -> int | None:
    if a is None or b is None:
        return None
    return abs(SIZE_LADDER.index(a) - SIZE_LADDER.index(b))


def ffprobe_dur(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def frames(path: str, start: float | None = None, dur: float | None = None) -> np.ndarray:
    """Decode a window of the clip at SAMPLE_FPS into (n, TH, TW, 3) uint8."""
    cmd = ["ffmpeg", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    cmd += ["-i", path]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-vf", f"fps={SAMPLE_FPS},scale={TW}:{TH}", "-f", "rawvideo",
            "-pix_fmt", "rgb24", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    n = len(raw) // (TH * TW * 3)
    return np.frombuffer(raw[: n * TH * TW * 3], dtype=np.uint8).reshape(n, TH, TW, 3)


def luma(f: np.ndarray) -> np.ndarray:
    return 0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]


def mad(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(np.abs(a.astype(np.float32) - b.astype(np.float32))))


def hist_corr(a: np.ndarray, b: np.ndarray) -> float:
    """Correlation of 3-channel 16-bin colour histograms (1.0 = same palette/layout)."""
    ha = np.concatenate([np.histogram(a[..., c], bins=16, range=(0, 255))[0] for c in range(3)]).astype(np.float64)
    hb = np.concatenate([np.histogram(b[..., c], bins=16, range=(0, 255))[0] for c in range(3)]).astype(np.float64)
    ha -= ha.mean()
    hb -= hb.mean()
    d = np.sqrt((ha ** 2).sum() * (hb ** 2).sum())
    return float((ha * hb).sum() / d) if d else 0.0


def motion(win: np.ndarray) -> float:
    if len(win) < 2:
        return 0.0
    return float(np.mean([mad(luma(win[i]), luma(win[i + 1])) for i in range(len(win) - 1)]))


def warmth(f: np.ndarray) -> float:
    r, b = float(f[..., 0].mean()) + 1e-3, float(f[..., 2].mean()) + 1e-3
    return r / b


# ---------------------------------------------------------------- metadata
def load_shots(run_dir: str | None, shots_path: str | None) -> tuple[list[str], dict]:
    """Return (ordered shot ids, meta[id]) from a Director shot_list.json or a
    studio.py shots.json. meta: scene, chars (sorted list), size, loc, relation, transition."""
    path = shots_path or (run_dir and os.path.join(run_dir, "shot_list.json"))
    if not path or not os.path.isfile(path):
        return [], {}
    data = json.load(open(path, encoding="utf-8"))
    order, meta = [], {}
    shots = data.get("shots", data)
    items = shots.items() if isinstance(shots, dict) else [(s.get("shot_id") or s.get("id"), s) for s in shots]
    for sid, s in items:
        if not sid:
            continue
        cam = s.get("camera") or {}
        cam_text = cam.get("shot_type") if isinstance(cam, dict) else str(cam)
        cam_text = cam_text or s.get("cam", "")
        chars = s.get("characters_in_frame") or s.get("chars") or []
        chars = sorted(c.get("character_id") if isinstance(c, dict) else str(c) for c in chars)
        edit = s.get("edit") or {}
        trans = s.get("transition_to_next") or {}
        meta[sid] = {
            "scene": s.get("scene_id", s.get("scene")),
            "chars": chars,
            "size": shot_size(cam_text),
            "cam": cam_text,
            "loc": s.get("location_id", s.get("loc")),
            "relation": edit.get("relation_to_prev"),
            "transition": trans.get("type") if isinstance(trans, dict) else trans,
            "reuse_of": s.get("reuse_of"),
        }
        order.append(sid)
    return order, meta


def clips_from_concat(path: str) -> list[str]:
    base = os.path.dirname(os.path.abspath(path))
    out = []
    for raw in open(path, encoding="utf-8"):
        m = re.match(r"\s*file\s+'(.*)'\s*$", raw) or re.match(r"\s*file\s+(\S+)\s*$", raw)
        if m:
            p = m.group(1)
            out.append(p if os.path.isabs(p) else os.path.join(base, p))
    return out


# ---------------------------------------------------------------- analysis
def analyse_clip(path: str) -> dict:
    dur = ffprobe_dur(path)
    head = frames(path, 0.0, EDGE_S)
    tail = frames(path, max(0.0, dur - EDGE_S), EDGE_S)
    full = frames(path)
    diffs = [mad(luma(full[i]), luma(full[i + 1])) for i in range(len(full) - 1)]
    med = float(np.median(diffs)) if diffs else 0.0
    spikes = [round((i + 1) / SAMPLE_FPS, 1) for i, d in enumerate(diffs)
              if d > max(45.0, 6.0 * med) and 0 < i < len(diffs) - 1]
    return {"dur": dur, "head": head, "tail": tail, "head_motion": motion(head),
            "tail_motion": motion(tail), "internal_cuts": spikes,
            "first": full[0] if len(full) else None, "last": full[-1] if len(full) else None}


def score_boundary(a_id: str, b_id: str, A: dict, B: dict, ma: dict, mb: dict,
                   size_run: int) -> dict:
    fa, fb = A["last"], B["first"]
    m = mad(fa, fb)
    hc = hist_corr(fa, fb)
    la, lb = float(luma(fa).mean()), float(luma(fb).mean())
    luma_delta = abs(la - lb) / max(la, lb, 1.0)
    warm_delta = abs(warmth(fa) - warmth(fb)) / max(warmth(fa), warmth(fb), 1e-3)
    same_scene = (ma.get("scene") is not None and ma.get("scene") == mb.get("scene"))
    same_chars = bool(ma.get("chars")) and ma.get("chars") == mb.get("chars")
    steps = size_steps(ma.get("size"), mb.get("size"))
    deliberate = (mb.get("relation") in ("time-jump", "scene-change")
                  or (ma.get("transition") or "cut") not in ("cut", "match-cut", None))
    flags, score, fixes = [], 1.0, []

    # structural (metadata) checks
    if same_chars and steps is not None and steps <= 1 and not deliberate and (hc > 0.85 or m < 30):
        flags.append("jump_cut")
        score = min(score, 0.4)
        fixes.append(f"same subject at the same size ({ma.get('size')}->{mb.get('size')}): "
                     "trim into handles for a cut on action, insert a reaction/cutaway, or "
                     "re-render B two sizes away")
    elif steps is not None and steps <= 1 and same_scene and hc > 0.93 and m < 18 and not deliberate:
        flags.append("jump_cut_visual")
        score = min(score, 0.5)
        fixes.append("boundary frames are near-identical: cut on action or change size/angle")
    if same_chars and A["tail_motion"] < 1.5 and B["head_motion"] < 1.5 and not deliberate:
        flags.append("dead_boundary")
        score = min(score, 0.6)
        fixes.append("both sides are static holds: trim A earlier (mid-motion) and B later, "
                     "or J-cut the next line over A's tail")
    if same_scene and (luma_delta > 0.12 or warm_delta > 0.12) and not deliberate:
        flags.append("light_mismatch")
        score = min(score, 0.6 if max(luma_delta, warm_delta) < 0.25 else 0.4)
        fixes.append(f"luma delta {luma_delta:.0%}, warmth delta {warm_delta:.0%}: grade B to A if "
                     "small, re-render B with A's lighting line if large")
    if size_run >= 3 and not deliberate:
        flags.append("rhythm")
        score = min(score, 0.75)
        fixes.append(f"third consecutive {mb.get('size')}: vary shot size")
    if B["internal_cuts"]:
        flags.append("internal_cut_in_B")
        score = min(score, 0.5)
        fixes.append(f"B has a hard cut inside the clip at {B['internal_cuts']}s: trim B before "
                     "it or re-render (one continuous shot)")
    manual = []
    if same_chars or (ma.get("chars") and mb.get("chars")):
        manual.append("eyeline / 180-degree axis")
    manual.append("readable props vs ledger")
    return {
        "boundary": f"{a_id}|{b_id}",
        "cut_score": round(score, 2),
        "pass": score >= THRESHOLD,
        "flags": flags,
        "fix": fixes,
        "manual_review": manual,
        "metrics": {"frame_mad": round(m, 1), "hist_corr": round(hc, 3),
                    "luma_delta": round(luma_delta, 3), "warmth_delta": round(warm_delta, 3),
                    "a_tail_motion": round(A["tail_motion"], 2),
                    "b_head_motion": round(B["head_motion"], 2),
                    "size_steps": steps, "same_chars": same_chars, "same_scene": same_scene,
                    "a_size": ma.get("size"), "b_size": mb.get("size")},
    }


def contact_sheet(pairs: list[tuple[str, np.ndarray, np.ndarray, dict]], dest: str) -> bool:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return False
    tw, th, pad = TW, TH, 6
    cols = 2
    rows = (len(pairs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (2 * tw + 3 * pad), rows * (th + 24 + pad)), (20, 20, 20))
    draw = ImageDraw.Draw(sheet)
    for i, (label, fa, fb, res) in enumerate(pairs):
        x0 = (i % cols) * (2 * tw + 3 * pad) + pad
        y0 = (i // cols) * (th + 24 + pad) + 20
        sheet.paste(Image.fromarray(fa), (x0, y0))
        sheet.paste(Image.fromarray(fb), (x0 + tw + pad, y0))
        col = (120, 230, 120) if res["pass"] else (255, 90, 90)
        draw.text((x0, y0 - 16), f"{label}  {res['cut_score']:.2f} {','.join(res['flags']) or 'ok'}", fill=col)
    sheet.save(dest, quality=88)
    return True


def check_shot_list(order: list[str], meta: dict, raw: dict) -> list[dict]:
    """Metadata-only pre-generation check (no clips yet): jump cuts, rhythm, handles,
    coverage. Returns a list of issues; empty = shot list is cuttable."""
    issues = []
    size_run = 1
    scene_cov: dict = {}
    for i, sid in enumerate(order):
        s = raw.get(sid, {})
        m = meta[sid]
        edit = s.get("edit") or {}
        has_dialogue = bool((s.get("audio") or {}).get("dialogue") or s.get("lines"))
        if edit.get("handle_head_s", 0) < 1.0 or edit.get("handle_tail_s", 0) < 1.0:
            issues.append({"shot": sid, "issue": "handles",
                           "detail": "edit.handle_head_s / handle_tail_s must be >= 1.0 s (included in duration)"})
        if not edit.get("cut_in") or not edit.get("cut_out"):
            issues.append({"shot": sid, "issue": "cut_points",
                           "detail": "edit.cut_in and edit.cut_out are required (pose, gaze, prop state, motion phase)"})
        if m["scene"] is not None:
            cov = scene_cov.setdefault(m["scene"], {"dialogue": False, "reaction": False, "insert": False})
            cov["dialogue"] |= has_dialogue
            rel = m.get("relation")
            cov["reaction"] |= rel == "reaction"
            cov["insert"] |= rel == "insert" or (not m["chars"] and m["size"] in ("close-up", "extreme-close-up"))
        if i == 0:
            continue
        p = meta[order[i - 1]]
        if p["scene"] != m["scene"]:
            size_run = 1
            continue
        deliberate = m.get("relation") in ("time-jump", "scene-change")
        steps = size_steps(p["size"], m["size"])
        angle = edit.get("angle_change_deg_from_prev")
        same_chars = bool(m["chars"]) and p["chars"] == m["chars"]
        if same_chars and (steps is not None and steps <= 1) and (angle is None or angle < 30) and not deliberate:
            issues.append({"shot": sid, "issue": "jump_cut",
                           "detail": f"same characters {m['chars']} at {p['size']}->{m['size']} after {order[i-1]} "
                                     "(need a different subject, >= 2 size steps or >= 30 degrees; "
                                     "or a reaction/insert between)"})
        size_run = size_run + 1 if (p["size"] and p["size"] == m["size"]) else 1
        if size_run >= 3 and not deliberate:
            issues.append({"shot": sid, "issue": "rhythm",
                           "detail": f"third consecutive {m['size']} - vary shot size"})
    for scene, cov in scene_cov.items():
        if cov["dialogue"] and not cov["reaction"]:
            issues.append({"shot": scene, "issue": "coverage",
                           "detail": "dialogue scene has no reaction single (edit.relation_to_prev: reaction)"})
        if cov["dialogue"] and not cov["insert"]:
            issues.append({"shot": scene, "issue": "coverage",
                           "detail": "dialogue scene has no insert/cutaway (edit.relation_to_prev: insert)"})
    return issues


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir")
    ap.add_argument("--concat-list")
    ap.add_argument("--clips", nargs="*")
    ap.add_argument("--shots", help="shot metadata JSON (Director shot_list.json or studio shots.json)")
    ap.add_argument("--shot-list", action="store_true",
                    help="metadata-only check of the shot list (before any clip exists): jump cuts, "
                         "rhythm, handles, cut points, coverage")
    ap.add_argument("--out", help="report path (default <run_dir>/final/edit_report.json)")
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    args = ap.parse_args()

    order, meta = load_shots(args.run_dir, args.shots)

    if args.shot_list:
        if not order:
            print("edit_check: --shot-list needs --shots <shot_list.json> or --run-dir", file=sys.stderr)
            return 2
        path = args.shots or os.path.join(args.run_dir, "shot_list.json")
        data = json.load(open(path, encoding="utf-8"))
        shots = data.get("shots", data)
        raw = shots if isinstance(shots, dict) else {(s.get("shot_id") or s.get("id")): s for s in shots}
        issues = check_shot_list(order, meta, raw)
        out_path = args.out or os.path.join(os.path.dirname(os.path.abspath(path)), "shot_list_edit_report.json")
        json.dump({"shots": len(order), "issues": issues}, open(out_path, "w", encoding="utf-8"), indent=1)
        for it in issues:
            print(f"  {it['issue']:<10} {it['shot']:<10} {it['detail']}")
        hard = [i for i in issues if i["issue"] in ("jump_cut", "handles", "cut_points")]
        print(f"[edit_check] shot list: {len(issues)} issues ({len(hard)} blocking) -> {out_path}")
        return 1 if hard else 0
    if args.clips:
        clips = args.clips
        ids = [os.path.splitext(os.path.basename(c))[0] for c in clips]
        ids = [re.sub(r"[_-]?clip$", "", i) for i in ids]
    elif args.concat_list:
        clips = clips_from_concat(args.concat_list)
        ids = [re.sub(r"[_-]?clip$", "", os.path.basename(os.path.dirname(c)) if os.path.basename(c) == "clip.mp4"
                      else os.path.splitext(os.path.basename(c))[0]) for c in clips]
    elif args.run_dir and order:
        clips, ids = [], []
        for sid in order:
            src = (meta.get(sid) or {}).get("reuse_of") or sid
            p = os.path.join(args.run_dir, "clips", src, "clip.mp4")
            if not os.path.isfile(p):
                p = os.path.join(args.run_dir, "shots", src, "clip.mp4")
            if os.path.isfile(p):
                clips.append(p)
                ids.append(sid)
            else:
                print(f"[edit_check] missing clip for {sid} - skipped", file=sys.stderr)
    else:
        print("edit_check: need --run-dir with shot_list.json, --concat-list, or --clips", file=sys.stderr)
        return 2
    if len(clips) < 2:
        print("edit_check: fewer than two clips", file=sys.stderr)
        return 2

    out_path = args.out or os.path.join(args.run_dir or os.path.dirname(clips[0]), "final", "edit_report.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    print(f"[edit_check] analysing {len(clips)} clips, {len(clips) - 1} boundaries", flush=True)
    ana = [analyse_clip(c) for c in clips]
    results, pairs = [], []
    size_run = 1
    for i in range(len(clips) - 1):
        a_id, b_id = ids[i], ids[i + 1]
        ma, mb = meta.get(a_id, {}), meta.get(b_id, {})
        size_run = size_run + 1 if (ma.get("size") and ma.get("size") == mb.get("size")) else 1
        res = score_boundary(a_id, b_id, ana[i], ana[i + 1], ma, mb, size_run)
        results.append(res)
        pairs.append((res["boundary"], ana[i]["last"], ana[i + 1]["first"], res))
    internal = {ids[i]: a["internal_cuts"] for i, a in enumerate(ana) if a["internal_cuts"]}
    failed = [r for r in results if r["cut_score"] < args.threshold]
    report = {
        "threshold": args.threshold,
        "clips": len(clips),
        "boundaries": len(results),
        "failed": len(failed),
        "internal_cuts": internal,
        "results": results,
    }
    json.dump(report, open(out_path, "w", encoding="utf-8"), indent=1)
    sheet = os.path.join(os.path.dirname(out_path), "edit_boundaries.jpg")
    made = contact_sheet(pairs, sheet)

    for r in results:
        mark = "ok  " if r["pass"] else "FAIL"
        print(f"  {mark} {r['boundary']:<18} {r['cut_score']:.2f}  {', '.join(r['flags']) or '-'}")
        for f in r["fix"]:
            print(f"         -> {f}")
    if internal:
        print(f"[edit_check] clips with a hard cut INSIDE them: {internal}")
    print(f"[edit_check] {len(failed)}/{len(results)} boundaries below {args.threshold} -> {out_path}"
          + (f" (+ {sheet})" if made else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
