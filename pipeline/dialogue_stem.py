#!/usr/bin/env python3
"""dialogue_stem.py — render a clip's dialogue stem from the shot list and the voice casting.

Replaces generate_dialogue_audio.py (Orpheus-only, Clockwork Moth character names) in run.sh
step 2b. Reads the shot's `audio.dialogue` lines and `audio/voices/voice_config.json`, writes a
tts_render.py job file, renders it with the GPU handed to the TTS cluster, concatenates the
lines (0.35 s gaps) into <clip_dir>/dialogue.wav and writes dialogue_meta.json:
  {"status": "ok", "character_id", "engine_used", "voice", "text", "anchor_s", "duration_s",
   "lines": [{"text", "start_s", "duration_s", "out"}]}
anchor_s = the first line's start_time from the shot list, else edit.handle_head_s + 0.4.
generate_video_direct.py anchors the stem at that frame with MiniMaxH3AddGuide (i2va) or as
<Audio 1> (ref2va); assembly muxes the same stem at the same offset.

Fallbacks: a Chatterbox role whose reference_audio is missing uses its own line-up stem
(audio/voices/lineup/<role>_0.wav — the cast voice); a role with no config or a failed engine
falls back to Kokoro (child/teen → af_sky, female → af_heart, male → bm_george).
Lines with no letters ("…", "—") are silence beats and are skipped; a shot with no speakable
line gets status "no_speech" and no wav.

Usage: dialogue_stem.py <shot_list.json> <shot_id> <clip_dir> [--run-dir D] [--keep-gpu] [--no-gpu-switch]
Exit 0 = stem written or no_speech, 1 = every engine failed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

PIPE = os.path.dirname(os.path.abspath(__file__))
GAP_S = 0.35
KOKORO_BY_CLASS = {"child": "af_sky", "teen": "af_sky", "adult-female": "af_heart", "elderly-female": "bf_emma",
                   "adult-male": "bm_george", "elderly-male": "bm_lewis", "creature": "bm_lewis"}


def log(msg: str) -> None:
    print(f"[dialogue {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def speakable(text: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]", text or ""))


def ffprobe_dur(path: str) -> float:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def concat(paths: list[str], dest: str) -> None:
    if len(paths) == 1:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", paths[0], "-ar", "44100", "-ac", "1", dest], check=True)
        return
    args = ["ffmpeg", "-y", "-loglevel", "error"]
    for p in paths:
        args += ["-i", p]
    chain = "".join(f"[{i}]aresample=44100,aformat=channel_layouts=mono,apad=pad_dur={GAP_S if i < len(paths) - 1 else 0}[a{i}];"
                    for i in range(len(paths)))
    chain += "".join(f"[a{i}]" for i in range(len(paths))) + f"concat=n={len(paths)}:v=0:a=1[out]"
    args += ["-filter_complex", chain, "-map", "[out]", dest]
    subprocess.run(args, check=True)


def job_for(role: str, cfg: dict | None, text: str, out: str, run_dir: str, lineup_fallback: bool) -> dict:
    cfg = cfg or {}
    eng = (cfg.get("engine") or "").lower()
    job = {"id": role, "text": text, "out": out}
    if cfg.get("post_fx"):
        job["post_fx"] = cfg["post_fx"]
    if eng == "chatterbox":
        ref = cfg.get("reference_audio") or ""
        if not ref or not os.path.isfile(ref):
            lu = os.path.join(run_dir, "audio", "voices", "lineup", f"{role}_0.wav")
            if os.path.isfile(lu):
                log(f"  {role}: reference_audio missing ({ref or 'none'}) -> using the cast line-up stem {lu}")
                ref = lu
        if ref and os.path.isfile(ref):
            job.update({"engine": "chatterbox", "reference_audio": ref,
                        "exaggeration": cfg.get("exaggeration", 0.5), "cfg": cfg.get("cfg", 0.5)})
            return job
        eng = ""  # no usable reference → fall through to kokoro
    if eng == "orpheus":
        job.update({"engine": "orpheus", "voice": cfg.get("voice") or "tara"})
        return job
    if eng == "kokoro" or not eng or lineup_fallback:
        job.update({"engine": "kokoro", "voice": cfg.get("voice") if eng == "kokoro" and cfg.get("voice")
                    else KOKORO_BY_CLASS.get(str(cfg.get("voice_class") or "adult-male"), "bm_george"),
                    "speed": float(cfg.get("speed", 1.0))})
        return job
    job.update({"engine": "kokoro", "voice": "bm_george"})
    return job


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("shot_list")
    ap.add_argument("shot_id")
    ap.add_argument("clip_dir")
    ap.add_argument("--run-dir", default=None)
    ap.add_argument("--keep-gpu", action="store_true")
    ap.add_argument("--no-gpu-switch", action="store_true")
    a = ap.parse_args()
    clip_dir = os.path.abspath(a.clip_dir)
    run_dir = os.path.abspath(a.run_dir or os.path.dirname(os.path.dirname(clip_dir)))
    meta_path = os.path.join(clip_dir, "dialogue_meta.json")
    try:
        shots = json.load(open(a.shot_list))["shots"]
        shot = next(s for s in shots if s.get("shot_id") == a.shot_id)
    except Exception as exc:  # noqa: BLE001
        log(f"cannot read shot {a.shot_id}: {exc}")
        return 1
    lines = [d for d in ((shot.get("audio") or {}).get("dialogue") or []) if isinstance(d, dict)]
    lines = [d for d in lines if speakable(d.get("line") or d.get("text") or "")]
    if not lines:
        json.dump({"status": "no_speech", "shot_id": a.shot_id, "note": "no speakable dialogue line (silence beat)"},
                  open(meta_path, "w"), indent=2)
        log(f"{a.shot_id}: no speakable dialogue line — no stem")
        return 0
    try:
        vc = json.load(open(os.path.join(run_dir, "audio", "voices", "voice_config.json")))
    except Exception:
        vc = {}
    jobs = []
    for i, d in enumerate(lines):
        role = str(d.get("character_id") or d.get("speaker") or "narrator").lower()
        text = (d.get("line") or d.get("text") or "").strip()
        cfg = vc.get(role) or next((v for k, v in vc.items() if k.lower() == role), None)
        if cfg is None:
            log(f"  {role}: no entry in voice_config.json — kokoro fallback")
        out = os.path.join(clip_dir, f"dialogue_line_{i + 1}.wav")
        j = job_for(role, cfg, text, out, run_dir, False)
        j["id"] = f"{role}_{i + 1}"
        j["_role"] = role
        j["_start"] = d.get("start_time")
        jobs.append(j)
    jobs_path = os.path.join(clip_dir, "tts_jobs.json")
    json.dump({"jobs": [{k: v for k, v in j.items() if not k.startswith("_")} for j in jobs]}, open(jobs_path, "w"), indent=2)
    cmd = [sys.executable, os.path.join(PIPE, "tts_render.py"), "--jobs", jobs_path, "--run-dir", run_dir]
    if a.keep_gpu:
        cmd.append("--keep-gpu")
    if a.no_gpu_switch:
        cmd.append("--no-gpu-switch")
    log(f"{a.shot_id}: rendering {len(jobs)} line(s): " + ", ".join(f"{j['_role']}/{j['engine']}" for j in jobs))
    subprocess.run(cmd)
    try:
        results = json.load(open(jobs_path.replace(".json", ".results.json")))
    except Exception:
        results = {}
    res_list = results if isinstance(results, list) else results.get("results", [])
    res_by = {}
    for i, r in enumerate(res_list):   # tts_render writes results in job order; "id" may be absent
        if isinstance(r, dict):
            res_by[r.get("id") or (jobs[i]["id"] if i < len(jobs) else i)] = r
    # retry failed lines on kokoro (TTS cluster is still up when --keep-gpu; otherwise tts_render re-hands)
    retry = []
    for j in jobs:
        r = res_by.get(j["id"]) or {}
        if r.get("status") != "ok" or not os.path.isfile(j["out"]):
            log(f"  {j['id']} failed on {j['engine']}: {r.get('error', 'no output')} — retrying on kokoro")
            cfg = vc.get(j["_role"]) or {}
            k = job_for(j["_role"], {"voice_class": cfg.get("voice_class"), "engine": "kokoro", "post_fx": cfg.get("post_fx")},
                        j["text"], j["out"], run_dir, True)
            k["id"] = j["id"]
            retry.append(k)
    if retry:
        rp = os.path.join(clip_dir, "tts_jobs_retry.json")
        json.dump({"jobs": retry}, open(rp, "w"), indent=2)
        cmd2 = [sys.executable, os.path.join(PIPE, "tts_render.py"), "--jobs", rp, "--run-dir", run_dir, "--no-gpu-switch"]
        if not a.keep_gpu and not a.no_gpu_switch:
            cmd2 = cmd2[:-1] + (["--keep-gpu"] if a.keep_gpu else [])
        subprocess.run(cmd2)
        for k in retry:
            for j in jobs:
                if j["id"] == k["id"]:
                    j["engine"], j["voice"] = "kokoro", k["voice"]
    ok = [j for j in jobs if os.path.isfile(j["out"]) and os.path.getsize(j["out"]) > 100]
    if not ok:
        json.dump({"status": "failed", "failed": True, "shot_id": a.shot_id, "reason": "every engine failed"},
                  open(meta_path, "w"), indent=2)
        log(f"{a.shot_id}: every engine failed")
        return 1
    dest = os.path.join(clip_dir, "dialogue.wav")
    concat([j["out"] for j in ok], dest)
    edit = shot.get("edit") or {}
    anchor = ok[0].get("_start")
    if not isinstance(anchor, (int, float)):
        anchor = float(edit.get("handle_head_s") or 1.0) + 0.4
    t = float(anchor)
    line_meta = []
    for j in ok:
        dur = ffprobe_dur(j["out"])
        line_meta.append({"text": j["text"], "start_s": round(t, 3), "duration_s": round(dur, 3), "out": j["out"],
                          "engine": j["engine"], "voice": j.get("voice") or os.path.basename(j.get("reference_audio", ""))})
        t += dur + GAP_S
    meta = {"status": "ok", "shot_id": a.shot_id, "character_id": ok[0]["_role"], "engine_used": ok[0]["engine"],
            "voice": ok[0].get("voice") or os.path.basename(ok[0].get("reference_audio", "")),
            "text": " ".join(j["text"] for j in ok), "anchor_s": round(float(anchor), 3),
            "duration_s": round(ffprobe_dur(dest), 3), "lines": line_meta,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    json.dump(meta, open(meta_path, "w"), indent=2)
    log(f"{a.shot_id}: dialogue.wav {meta['duration_s']} s, anchor {meta['anchor_s']} s, {meta['engine_used']}/{meta['voice']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
