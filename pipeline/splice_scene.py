#!/usr/bin/env python3
"""splice_scene.py <shot_list.json> <scene_id> <fix.json> — replace the scene's shots with the
Director's fixed versions. The fix must carry the SAME shot ids (the per-clip loop iterates a
fixed id list); extra or missing ids are rejected and nothing is written. Exit 0 = spliced,
1 = rejected, 2 = bad input."""
import json, os, sys, tempfile

def main():
    if len(sys.argv) < 4:
        print(__doc__); return 2
    path, scene, fix_path = sys.argv[1:4]
    try:
        sl = json.load(open(path)); fix = json.load(open(fix_path))
    except Exception as e:
        print(f"[splice] cannot read: {e}"); return 2
    new = fix.get("shots") if isinstance(fix, dict) else fix
    if not isinstance(new, list) or not new:
        print("[splice] fix has no shots"); return 1
    old_ids = [s["shot_id"] for s in sl["shots"] if s.get("scene_id") == scene]
    new_by = {s.get("shot_id"): s for s in new if isinstance(s, dict)}
    if set(new_by) != set(old_ids):
        print(f"[splice] REJECTED: ids differ (have {len(old_ids)}, fix {len(new_by)}; "
              f"missing {sorted(set(old_ids)-set(new_by))[:5]}, extra {sorted(set(new_by)-set(old_ids))[:5]})")
        return 1
    n = 0
    for i, s in enumerate(sl["shots"]):
        if s.get("scene_id") == scene:
            ns = new_by[s["shot_id"]]
            ns["scene_id"] = scene
            ns["sequence_order"] = s.get("sequence_order", i + 1)
            sl["shots"][i] = ns; n += 1
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".splice-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(sl, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    print(f"[splice] {scene}: {n} shots replaced in place")
    return 0

if __name__ == "__main__":
    sys.exit(main())
