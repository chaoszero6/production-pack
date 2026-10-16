#!/usr/bin/env python3
"""best_take.py — when a clip exhausts its retries, keep the best take, not the last one.

run.sh archives every failed take as clips/<id>/takes/take_<n>.mp4 with its verdict
(take_<n>_verdict.json). A later take can score lower than an earlier one (S01_005:
take 1 hands 0.87 / overall 0.90, take 2 hands 0.74 / overall 0.84). At escalation this
restores the highest-scoring take as clip.mp4 and records which one it was in
qa_verdict.json ("escalated_best_take"). Verdict stays FAIL so the ledger is honest.

Usage: best_take.py <clip_dir>
"""
import glob
import json
import os
import re
import shutil
import sys


def overall(v):
    for k in ("overall_score", "weighted_overall", "overall", "mean_score_scored_categories"):
        x = v.get(k)
        if isinstance(x, (int, float)):
            return float(x)
    sc = v.get("scores") or v.get("category_scores") or v.get("categories") or {}
    vals = []
    for val in (sc.values() if isinstance(sc, dict) else []):
        if isinstance(val, dict) and isinstance(val.get("score"), (int, float)):
            vals.append(float(val["score"]))
        elif isinstance(val, (int, float)):
            vals.append(float(val))
    return sum(vals) / len(vals) if vals else -1.0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    clip_dir = sys.argv[1].rstrip("/")
    takes_dir = os.path.join(clip_dir, "takes")
    best, best_score = None, -1.0
    for vp in sorted(glob.glob(os.path.join(takes_dir, "take_*_verdict.json"))):
        n = re.search(r"take_(\d+)_verdict", vp)
        if not n:
            continue
        mp4 = os.path.join(takes_dir, f"take_{n.group(1)}.mp4")
        if not os.path.isfile(mp4):
            continue
        try:
            v = json.load(open(vp))
        except Exception:  # noqa: BLE001
            continue
        s = overall(v)
        if s > best_score:
            best, best_score = (int(n.group(1)), mp4, vp), s
    if not best:
        print(f"[best_take] {os.path.basename(clip_dir)}: no archived takes — keeping the last render")
        return 0
    n, mp4, vp = best
    clip = os.path.join(clip_dir, "clip.mp4")
    cur_score = -1.0
    try:
        cur_score = overall(json.load(open(os.path.join(clip_dir, "qa_verdict.json"))))
    except Exception:  # noqa: BLE001
        pass
    if best_score <= cur_score:
        print(f"[best_take] {os.path.basename(clip_dir)}: last take ({cur_score:.3f}) is the best — kept")
        return 0
    shutil.copyfile(mp4, clip)
    v = json.load(open(vp))
    v["escalated_best_take"] = n
    v["verdict"] = "FAIL"
    v["_note"] = f"best of the archived takes (overall {best_score:.3f}) restored by best_take.py after escalation"
    with open(os.path.join(clip_dir, "qa_verdict.json"), "w") as fh:
        json.dump(v, fh, indent=2)
    print(f"[best_take] {os.path.basename(clip_dir)}: restored take {n} (overall {best_score:.3f} > last {cur_score:.3f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
