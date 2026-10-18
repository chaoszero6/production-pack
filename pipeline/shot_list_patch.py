#!/usr/bin/env python3
"""shot_list_patch.py shot_list.json patch.json [--renumber] [--run-dir D] [--report out.json]

Applies a Director PATCH to the shot list instead of a full rewrite (the fix rounds after
edit_check / shot_list_lint / story_check). run.sh inlines this schema into the fix prompt.

PATCH SCHEMA (patch_version 1)
  {
    "patch_version": 1,
    "replace": [ <full shot object, same shot_id as the one it replaces>, ... ],
    "insert_after": [ {"after": "S01_003", "shot": { ...full shot..., "shot_id": "S01_003a"}}, ... ],
    "delete": [ "S01_009", ... ],
    "notes": "what changed and why"
  }
  - every shot object is COMPLETE (shot-list-schema): shot_id, scene_id, camera,
    duration_seconds (3–15), characters_in_frame, audio, edit, keyframe_strategy;
  - "replace" targets an existing shot_id; "insert_after" names an existing anchor and a
    NEW shot_id (anchor id + a letter is the convention; --renumber makes it canonical);
    "delete" names existing ids; a shot cannot be both replaced and deleted;
  - DIALOGUE GUARD: every audio.dialogue[].line present before the patch must be present
    after it (exact string). A dropped line rejects the whole patch — move lines, never
    drop them;
  - --renumber: per scene, shots become Sxx_001, Sxx_002 … in list order; every reference
    (audio.narration.span_clips / starts_on, keyframe_strategy.first_frame_source, any
    *_clip field, sequence_order) is rewritten. REFUSED when --run-dir is given and
    clips/<old_id>/ exists for any shot (a rendered clip would lose its id).

Exit 0 = applied, 1 = rejected (nothing written), 2 = bad input.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile

REQUIRED_SHOT_KEYS = ("shot_id", "scene_id", "camera", "duration_seconds",
                      "characters_in_frame", "audio", "edit", "keyframe_strategy")
DICT_KEYS = ("camera", "audio", "edit", "keyframe_strategy")
MIN_S, MAX_S = 3.0, 15.0
_ID_RE = re.compile(r"^(S\d+)_(\d+)([a-z]*)$")


# ----------------------------------------------------------------------------- io
def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def atomic_write_json(path, data):
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".shot_list_patch-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ----------------------------------------------------------------------------- validation
def validate_shot(shot) -> list[str]:
    """Problems with one shot object (empty list = valid)."""
    if not isinstance(shot, dict):
        return ["shot is not an object"]
    probs = [f"missing '{k}'" for k in REQUIRED_SHOT_KEYS if k not in shot]
    sid = shot.get("shot_id")
    if "shot_id" in shot and (not isinstance(sid, str) or not sid.strip()):
        probs.append("shot_id must be a non-empty string")
    for k in DICT_KEYS:
        if k in shot and not isinstance(shot[k], dict):
            probs.append(f"'{k}' must be an object")
    if "characters_in_frame" in shot and not isinstance(shot["characters_in_frame"], list):
        probs.append("'characters_in_frame' must be a list")
    dur = shot.get("duration_seconds")
    if "duration_seconds" in shot:
        if not isinstance(dur, (int, float)) or isinstance(dur, bool):
            probs.append("'duration_seconds' must be a number")
        elif not MIN_S <= float(dur) <= MAX_S:
            probs.append(f"'duration_seconds' {dur} outside {MIN_S}–{MAX_S} s")
    audio = shot.get("audio")
    if isinstance(audio, dict):
        dl = audio.get("dialogue")
        if dl is not None and not isinstance(dl, list):
            probs.append("'audio.dialogue' must be a list")
        else:
            for i, d in enumerate(dl or []):
                if not isinstance(d, dict) or not isinstance(d.get("line"), str):
                    probs.append(f"'audio.dialogue[{i}]' needs a string 'line'")
    return probs


def dialogue_lines(shots: list) -> list[tuple[str, str]]:
    """(shot_id, line) for every dialogue entry."""
    out = []
    for s in shots:
        if not isinstance(s, dict):
            continue
        dl = (s.get("audio") or {}).get("dialogue") or []
        if isinstance(dl, dict):
            dl = [dl]
        for d in dl:
            if isinstance(d, dict) and isinstance(d.get("line"), str):
                out.append((str(s.get("shot_id")), d["line"]))
    return out


def dialogue_guard(old_shots: list, new_shots: list) -> list[str]:
    """Lines present before and missing after (exact string match, multiset-aware)."""
    after = {}
    for _, line in dialogue_lines(new_shots):
        after[line] = after.get(line, 0) + 1
    dropped = []
    for sid, line in dialogue_lines(old_shots):
        if after.get(line, 0) > 0:
            after[line] -= 1
        else:
            dropped.append(f"{sid}: {line}")
    return dropped


# ----------------------------------------------------------------------------- apply
class PatchError(Exception):
    pass


def apply(sl: dict, patch: dict) -> tuple[dict, dict]:
    """Returns (new shot list, summary). Raises PatchError on rejection."""
    if not isinstance(patch, dict):
        raise PatchError("patch is not an object")
    ver = patch.get("patch_version", 1)
    if ver != 1:
        raise PatchError(f"unsupported patch_version {ver!r}")
    replace = patch.get("replace") or []
    inserts = patch.get("insert_after") or []
    delete = patch.get("delete") or []
    for name, val in (("replace", replace), ("insert_after", inserts), ("delete", delete)):
        if not isinstance(val, list):
            raise PatchError(f"'{name}' must be a list")
    if not (replace or inserts or delete):
        raise PatchError("empty patch (no replace / insert_after / delete)")

    old_shots = sl.get("shots")
    if not isinstance(old_shots, list):
        raise PatchError("shot list has no 'shots' list")
    existing = {s.get("shot_id"): i for i, s in enumerate(old_shots) if isinstance(s, dict)}
    errors = []

    del_ids = []
    for d in delete:
        if d not in existing:
            errors.append(f"delete: '{d}' does not exist")
        elif d in del_ids:
            errors.append(f"delete: '{d}' listed twice")
        else:
            del_ids.append(d)

    rep_by = {}
    for r in replace:
        probs = validate_shot(r)
        rid = r.get("shot_id") if isinstance(r, dict) else None
        if probs:
            errors.append(f"replace '{rid}': " + "; ".join(probs))
            continue
        if rid not in existing:
            errors.append(f"replace: '{rid}' does not exist (use insert_after for new shots)")
        elif rid in del_ids:
            errors.append(f"replace: '{rid}' is also deleted")
        elif rid in rep_by:
            errors.append(f"replace: '{rid}' listed twice")
        else:
            rep_by[rid] = r

    ins_by: dict[str, list] = {}
    new_ids = set()
    for ins in inserts:
        if not isinstance(ins, dict) or "shot" not in ins:
            errors.append("insert_after: entry needs 'after' and 'shot'")
            continue
        after, shot = ins.get("after"), ins.get("shot")
        probs = validate_shot(shot)
        nid = shot.get("shot_id") if isinstance(shot, dict) else None
        if probs:
            errors.append(f"insert_after '{after}' shot '{nid}': " + "; ".join(probs))
            continue
        if after not in existing:
            errors.append(f"insert_after: anchor '{after}' does not exist")
        elif after in del_ids:
            errors.append(f"insert_after: anchor '{after}' is deleted in the same patch")
        if nid in existing:
            errors.append(f"insert_after: new shot_id '{nid}' already exists (use replace)")
        elif nid in new_ids:
            errors.append(f"insert_after: new shot_id '{nid}' inserted twice")
        else:
            new_ids.add(nid)
            ins_by.setdefault(after, []).append(shot)
    if errors:
        raise PatchError("; ".join(errors))

    new_shots = []
    for s in old_shots:
        sid = s.get("shot_id") if isinstance(s, dict) else None
        if sid in del_ids:
            continue
        if sid in rep_by:
            r = copy.deepcopy(rep_by[sid])
            r.setdefault("scene_id", s.get("scene_id"))
            new_shots.append(r)
        else:
            new_shots.append(copy.deepcopy(s))
        for ins in ins_by.get(sid, []):
            n = copy.deepcopy(ins)
            if not n.get("scene_id"):
                n["scene_id"] = s.get("scene_id")
            new_shots.append(n)

    dropped = dialogue_guard(old_shots, new_shots)
    if dropped:
        raise PatchError("dialogue guard: these lines were present before and are missing after — "
                         + " | ".join(dropped))

    for i, s in enumerate(new_shots, 1):
        s["sequence_order"] = i
    out = copy.deepcopy(sl)
    out["shots"] = new_shots
    if "total_shots" in out:
        out["total_shots"] = len(new_shots)
    summary = {"replaced": sorted(rep_by), "inserted": sorted(new_ids), "deleted": del_ids,
               "shots_before": len(old_shots), "shots_after": len(new_shots)}
    return out, summary


# ----------------------------------------------------------------------------- renumber
def _remap(value, mapping: dict):
    """Rewrite every string equal to an old shot id, anywhere in the structure."""
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [_remap(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: _remap(v, mapping) for k, v in value.items()}
    return value


def renumber(sl: dict) -> dict:
    """Per scene sequential Sxx_NNN in list order; rewrites every reference. Returns old→new."""
    shots = sl.get("shots") or []
    scene_prefix: dict[str, str] = {}
    counters: dict[str, int] = {}
    mapping: dict[str, str] = {}
    next_scene_no = 1
    for s in shots:
        if not isinstance(s, dict):
            continue
        scene = str(s.get("scene_id") or "")
        old = str(s.get("shot_id") or "")
        if scene not in scene_prefix:
            m = _ID_RE.match(old)
            prefix = m.group(1) if m else None
            if not prefix or prefix in scene_prefix.values():
                while f"S{next_scene_no:02d}" in scene_prefix.values():
                    next_scene_no += 1
                prefix = f"S{next_scene_no:02d}"
            scene_prefix[scene] = prefix
            counters[scene] = 0
        counters[scene] += 1
        new = f"{scene_prefix[scene]}_{counters[scene]:03d}"
        if old != new:
            mapping[old] = new
    if mapping:
        sl["shots"] = [_remap(s, mapping) for s in shots]
    for i, s in enumerate(sl["shots"], 1):
        if isinstance(s, dict):
            s["sequence_order"] = i
    return mapping


def rendered_clip_dirs(run_dir: str, shots: list) -> list[str]:
    out = []
    for s in shots:
        sid = s.get("shot_id") if isinstance(s, dict) else None
        if sid and os.path.isdir(os.path.join(run_dir, "clips", str(sid))):
            out.append(str(sid))
    return out


# ----------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("shot_list")
    ap.add_argument("patch")
    ap.add_argument("--renumber", action="store_true")
    ap.add_argument("--run-dir")
    ap.add_argument("--report")
    a = ap.parse_args(argv)

    report = {"ok": False, "shot_list": a.shot_list, "patch": a.patch}

    def finish(code: int) -> int:
        if a.report:
            try:
                atomic_write_json(a.report, report)
            except OSError as e:
                print(f"[shot_list_patch] cannot write report: {e}")
        return code

    try:
        sl = load_json(a.shot_list)
        patch = load_json(a.patch)
        if not isinstance(sl, dict) or not isinstance(sl.get("shots"), list):
            raise ValueError("shot list has no 'shots' list")
    except Exception as e:  # noqa: BLE001
        print(f"[shot_list_patch] cannot read input: {e}")
        report["error"] = str(e)
        return finish(2)

    try:
        new_sl, summary = apply(sl, patch)
    except PatchError as e:
        print(f"[shot_list_patch] REJECTED: {e}")
        report["error"] = str(e)
        return finish(1)
    report.update(summary)
    if isinstance(patch, dict) and patch.get("notes"):
        report["notes"] = patch["notes"]

    if a.renumber:
        if a.run_dir:
            rendered = rendered_clip_dirs(a.run_dir, new_sl["shots"])
            if rendered:
                msg = f"renumber refused: clips/ already exist for {rendered[:6]} under {a.run_dir}"
                print(f"[shot_list_patch] REJECTED: {msg}")
                report["error"] = msg
                return finish(1)
        mapping = renumber(new_sl)
        report["renumber_map"] = mapping
        if mapping:
            print(f"[shot_list_patch] renumbered {len(mapping)} shots: "
                  + ", ".join(f"{o}→{n}" for o, n in list(mapping.items())[:8])
                  + (" …" if len(mapping) > 8 else ""))

    atomic_write_json(a.shot_list, new_sl)
    report["ok"] = True
    print(f"[shot_list_patch] applied: {len(summary['replaced'])} replaced, {len(summary['inserted'])} inserted, "
          f"{len(summary['deleted'])} deleted → {summary['shots_after']} shots")
    return finish(0)


if __name__ == "__main__":
    sys.exit(main())
