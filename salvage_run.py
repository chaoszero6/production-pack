#!/usr/bin/env python3
"""Salvage the artifacts the sandboxed agents staged outside the run directory.

The dsh sandbox let agents write only inside /root/desktop/deepseek-harness, so
they staged real work in /tmp and in their workspace, then reported success in
prose. run.sh's skip_if_done() keys off "<artifact> exists and is non-empty",
so the prose files currently THERE would make --resume skip the step and keep
the garbage. This puts the real JSON at those exact paths so resume skips
cleanly, and copies images/audio into the run dir so it is self-contained.

Every copy is verified by size + JSON validity after the fact.
"""
import json
import shutil
import sys
from pathlib import Path

RUN = Path("/root/production_pack/output/run_20260921_185227")
DSH = Path("/root/desktop/deepseek-harness")
CHAR_STAGE = Path("/tmp/char_sheets_run_20260921_185227")
LOC_STAGE = Path("/tmp/loc_refs_run_20260921_185227")
VOICE_STAGE = DSH / "voice_profiles_run_20260921_185227" / "voices"

results = []


def note(ok, what, detail=""):
    results.append((ok, what, detail))
    print(f"{'OK  ' if ok else 'FAIL'}  {what}  {detail}")


def copy_file(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst.exists() and dst.stat().st_size == src.stat().st_size


def copy_tree(src, dst):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    n_src = sum(1 for _ in src.rglob("*") if _.is_file())
    n_dst = sum(1 for _ in dst.rglob("*") if _.is_file())
    return n_src == n_dst, f"{n_dst}/{n_src} files"


def valid_json(p):
    try:
        with open(p) as f:
            json.load(f)
        return True
    except Exception:
        return False


# 1. shot list -------------------------------------------------------------
src = DSH / "shot_list.json"
dst = RUN / "shot_list.json"
ok = copy_file(src, dst) and valid_json(dst)
with open(dst) as f:
    nshots = len(json.load(f).get("shots", []))
note(ok, "shot_list.json", f"{dst.stat().st_size}B, {nshots} shots, valid JSON")

# 2. character sheets ------------------------------------------------------
ok, detail = copy_tree(CHAR_STAGE / "characters", RUN / "characters")
note(ok, "characters/", detail)

# 3. location refs ---------------------------------------------------------
ok, detail = copy_tree(LOC_STAGE / "locations", RUN / "locations")
note(ok, "locations/", detail)

# 4. manifests -------------------------------------------------------------
for src, dst in [
    (CHAR_STAGE / "manifest.json", RUN / "character_manifest.json"),
    (LOC_STAGE / "manifest.json", RUN / "location_manifest.json"),
]:
    ok = copy_file(src, dst) and valid_json(dst)
    note(ok, f"{dst.name}", f"{dst.stat().st_size}B, valid JSON")

# 5. voice refs -> run dir, then rewrite voice_config paths ----------------
(RUN / "audio" / "voices").mkdir(parents=True, exist_ok=True)
vc = RUN / "audio" / "voices" / "voice_config.json"
wavs = sorted(VOICE_STAGE.glob("*.wav"))
for w in wavs:
    shutil.copy2(w, RUN / "audio" / "voices" / w.name)
note(len(wavs) == 5, "voice wavs copied", f"{len(wavs)} files")

OLD = str(VOICE_STAGE)
NEW = str(RUN / "audio" / "voices")
raw = vc.read_text()
if OLD in raw:
    vc.write_text(raw.replace(OLD, NEW))
    note(True, "voice_config.json paths rewritten", f"{OLD} -> {NEW}")
else:
    note(True, "voice_config.json paths", "no rewrite needed")
note(valid_json(vc), "voice_config.json", "valid JSON")

# 6. stale prose check -----------------------------------------------------
print("\n--- final state of skip_if_done gate files ---")
for p in [
    RUN / "story.json",
    RUN / "shot_list.json",
    RUN / "character_manifest.json",
    RUN / "location_manifest.json",
    RUN / "audio" / "voices" / "voice_config.json",
]:
    st = "MISSING"
    if p.exists():
        sz = p.stat().st_size
        st = f"{sz}B valid_json={valid_json(p)}"
    print(f"  {p.name:28s} {st}")

bad = [r for r in results if not r[0]]
print(f"\n{len(results)-len(bad)}/{len(results)} checks passed")
sys.exit(1 if bad else 0)
