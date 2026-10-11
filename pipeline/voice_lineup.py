#!/usr/bin/env python3
"""voice_lineup.py — render the cast line-up and MEASURE that the voices differ.

Implements the voice-casting skill's Rule 2 (The Clockwork Moth shipped four
Orpheus adult-female voices within ~3 semitones; nothing measured it).

  python3 voice_lineup.py --run-dir <run_dir> [--min-semitones 4] [--no-gpu-switch]

Reads  <run_dir>/audio/voices/voice_config.json
       { "<role>": {"voice_class": "child|teen|adult-female|adult-male|elderly-female|
                                     elderly-male|creature|narrator-*",
                    "engine": "orpheus|chatterbox|kokoro", "voice": "...",
                    "reference_audio": "...", "post_fx": {...}} , ... }
       (the narrator is the role named "narrator")
Renders two shared test sentences per role via tts_render.py (GPU → TTS cluster,
then back to the LLM), measures median F0, F0 spread, speaking rate and
dynamics on each, and writes <run_dir>/audio/voices/lineup/separation.json
with the pairwise matrix and the verdict CAST_OK / CAST_FAIL.

Pair rule: |ΔF0| ≥ min_semitones, OR (different voice class AND |ΔF0| ≥ 2 st
AND (rate differs ≥ 10 % OR engine differs)). Narrator vs every character must
also differ in class. Exit 0 = CAST_OK, 1 = CAST_FAIL, 2 = bad input.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time

import numpy as np

PIPE = os.path.dirname(os.path.abspath(__file__))
SENTENCES = [
    "The clock on the wall says it is ten past seven, and the bread is still warm.",
    "Did you really think I would not notice the missing gear this morning?",
]


def log(msg: str) -> None:
    print(f"[lineup {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wav_mono16k(path: str) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", "16000",
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def f0_track(x: np.ndarray, sr: int = 16000, fmin: float = 70.0, fmax: float = 450.0) -> list[float]:
    """Autocorrelation pitch per 32 ms frame on voiced frames (RMS > 2 % and clear peak)."""
    win, hop = 512, 160
    lo, hi = int(sr / fmax), int(sr / fmin)
    out = []
    if len(x) < win:
        return out
    thr = 0.02 * max(1e-6, float(np.percentile(np.abs(x), 99)))
    for i in range(0, len(x) - win, hop):
        fr = x[i:i + win]
        if float(np.sqrt(np.mean(fr ** 2))) < thr:
            continue
        fr = fr - fr.mean()
        ac = np.correlate(fr, fr, mode="full")[win - 1:]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        seg = ac[lo:hi]
        k = int(np.argmax(seg)) + lo
        if ac[k] < 0.5:
            continue
        out.append(sr / k)
    return out


def metrics(path: str, text: str) -> dict:
    x = wav_mono16k(path)
    f0 = f0_track(x)
    frame = 160
    n = len(x) // frame
    rms = np.sqrt(np.mean(x[:n * frame].reshape(n, frame) ** 2, axis=1)) if n else np.array([0.0])
    active = rms > 0.02 * max(1e-6, float(rms.max()))
    idx = np.where(active)[0]
    speech_s = (idx[-1] - idx[0]) * frame / 16000 if len(idx) > 1 else len(x) / 16000
    words = len(text.split())
    loud = rms[active] if active.any() else rms
    med = float(np.median(f0)) if f0 else 0.0
    return {
        "dur_s": round(len(x) / 16000, 2),
        "f0_median_hz": round(med, 1),
        "f0_std_semitones": round(float(np.std(12 * np.log2(np.array(f0) / med))), 2) if f0 and med else 0.0,
        "voiced_frac": round(len(f0) / max(1, int(active.sum())), 2),
        "rate_wps": round(words / speech_s, 2) if speech_s > 0 else 0.0,
        "rms_dynamic_db": round(float(20 * np.log10((np.percentile(loud, 95) + 1e-6) / (np.percentile(loud, 10) + 1e-6))), 1),
    }


def semitones(a: float, b: float) -> float:
    return abs(12 * math.log2(a / b)) if a > 0 and b > 0 else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--voices", default=None, help="default <run_dir>/audio/voices/voice_config.json")
    ap.add_argument("--min-semitones", type=float, default=4.0)
    ap.add_argument("--no-gpu-switch", action="store_true")
    ap.add_argument("--skip-render", action="store_true", help="measure existing lineup wavs only")
    args = ap.parse_args()

    vpath = args.voices or os.path.join(args.run_dir, "audio", "voices", "voice_config.json")
    try:
        cfg = json.load(open(vpath, encoding="utf-8"))
    except Exception as exc:
        log(f"cannot read {vpath}: {exc}")
        return 2
    roles = cfg.get("roles", cfg) if isinstance(cfg, dict) else {}
    roles = {k: v for k, v in roles.items() if isinstance(v, dict) and v.get("engine")}
    if len(roles) < 2:
        log("need at least two cast roles with an engine")
        return 2
    lineup = os.path.join(args.run_dir, "audio", "voices", "lineup")
    os.makedirs(lineup, exist_ok=True)

    if not args.skip_render:
        jobs = []
        for role, v in roles.items():
            for i, sent in enumerate(SENTENCES):
                job = {"id": f"{role}_{i}", "engine": v["engine"], "text": sent,
                       "out": os.path.join(lineup, f"{role}_{i}.wav")}
                for k in ("voice", "reference_audio", "exaggeration", "cfg", "speed", "post_fx", "instruction"):
                    if k in v:
                        job[k] = v[k]
                jobs.append(job)
        jpath = os.path.join(lineup, "tts_jobs.json")
        json.dump({"jobs": jobs}, open(jpath, "w", encoding="utf-8"), indent=1)
        cmd = [sys.executable, os.path.join(PIPE, "tts_render.py"), "--jobs", jpath, "--run-dir", args.run_dir]
        if args.no_gpu_switch:
            cmd.append("--no-gpu-switch")
        rc = subprocess.run(cmd).returncode
        if rc == 2:
            return 2

    per_role = {}
    for role, v in roles.items():
        ms = []
        for i, sent in enumerate(SENTENCES):
            p = os.path.join(lineup, f"{role}_{i}.wav")
            if os.path.isfile(p) and os.path.getsize(p) > 100:
                try:
                    ms.append(metrics(p, sent))
                except Exception as exc:
                    log(f"{role}: metric error {exc}")
        if not ms:
            per_role[role] = {"error": "no lineup audio", "voice_class": v.get("voice_class"), "engine": v["engine"]}
            continue
        agg = {k: round(float(np.mean([m[k] for m in ms])), 2) for k in ms[0]}
        agg.update({"voice_class": v.get("voice_class"), "engine": v["engine"], "voice": v.get("voice") or v.get("reference_audio")})
        per_role[role] = agg

    names = list(per_role)
    pairs, failures = [], []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = per_role[names[i]], per_role[names[j]]
            if "error" in a or "error" in b:
                failures.append(f"{names[i]}|{names[j]}: missing audio")
                continue
            st = semitones(a["f0_median_hz"], b["f0_median_hz"])
            rate_diff = abs(a["rate_wps"] - b["rate_wps"]) / max(a["rate_wps"], b["rate_wps"], 0.01)
            same_class = (a["voice_class"] or "?") == (b["voice_class"] or "?")
            ok = st >= args.min_semitones or (not same_class and st >= 2.0 and (rate_diff >= 0.10 or a["engine"] != b["engine"]))
            narrator_pair = "narrator" in (names[i], names[j])
            if narrator_pair and same_class:
                ok = False
            pair = {"a": names[i], "b": names[j], "delta_semitones": round(st, 2), "rate_diff": round(rate_diff, 3),
                    "same_class": same_class, "ok": ok}
            pairs.append(pair)
            if not ok:
                failures.append(f"{names[i]}|{names[j]}: {st:.1f} st apart, {'same' if same_class else 'different'} class"
                                + (" (narrator shares a class with a character)" if narrator_pair and same_class else ""))
    verdict = "CAST_OK" if not failures else "CAST_FAIL"
    doc = {"verdict": verdict, "min_semitones": args.min_semitones, "roles": per_role, "pairs": pairs,
           "failures": failures, "sentences": SENTENCES, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    out = os.path.join(lineup, "separation.json")
    json.dump(doc, open(out, "w", encoding="utf-8"), indent=1)
    for r, m in per_role.items():
        log(f"{r:<12} {m.get('voice_class', '?'):<15} {m.get('engine')}/{m.get('voice')}  f0={m.get('f0_median_hz')}Hz rate={m.get('rate_wps')}w/s")
    for f in failures:
        log(f"FAIL {f}")
    log(f"{verdict} -> {out}")
    return 0 if verdict == "CAST_OK" else 1


if __name__ == "__main__":
    sys.exit(main())
