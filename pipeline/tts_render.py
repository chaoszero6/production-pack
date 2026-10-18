#!/usr/bin/env python3
"""tts_render.py — render a batch of TTS jobs with the GPU handed to the TTS cluster.

Why a job file: agents run in the cloud and never call a TTS port; the TTS engines are
exclusive GPU owners (systemd Conflicts=). An agent turn therefore CANNOT call a
TTS API — the engines are down while the agent runs. The agent writes a job
file instead, and the pipeline renders it here with the LLM parked, then hands
the GPU back to the LLM.

Job file (JSON):
{
  "jobs": [
    {"id": "narr_01", "engine": "orpheus",    "voice": "tara", "text": "…", "out": "audio/narration/N1.wav"},
    {"id": "tobias",  "engine": "chatterbox", "reference_audio": "audio/voices/refs/child_boy_11.wav",
                      "text": "…", "exaggeration": 0.5, "cfg": 0.5, "out": "…"},
    {"id": "nana",    "engine": "kokoro",     "voice": "bf_emma", "speed": 0.92, "text": "…", "out": "…"},
    {"id": "x",       "engine": "orpheus", "voice": "leah", "text": "…", "out": "…",
                      "post_fx": {"pitch_semitones": -2, "tempo": 0.92}}
  ]
}
Relative paths resolve against --run-dir (default: the job file's directory).

Engines: orpheus (:9883, OpenAI /v1/audio/speech), chatterbox (:9882, /tts/json),
kokoro (:9881, /tts). cosyvoice is deployed only as a Gradio webui (:50000) —
jobs for it fail with a clear message; cast those roles on chatterbox/orpheus.

Writes <jobs>.results.json: per job {status, out, duration_s, error}.
Exit 0 = every job rendered, 1 = at least one failed, 2 = bad input.

  python3 tts_render.py --jobs <file> [--run-dir D] [--keep-gpu] [--no-gpu-switch]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.request

PIPE = os.path.dirname(os.path.abspath(__file__))
ENGINES = {
    "orpheus":    "http://127.0.0.1:9883",
    "chatterbox": "http://127.0.0.1:9882",
    "kokoro":     "http://127.0.0.1:9881",
}


def log(msg: str) -> None:
    print(f"[tts-render {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def post(url: str, body: dict, timeout: float = 300) -> bytes:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def ffprobe_dur(path: str) -> float | None:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except Exception:
        return None


def post_fx(path: str, fx: dict) -> None:
    """Pitch (semitones) and tempo via asetrate/atempo — enough for an elderly/child
    shift of ±3 st / ±10 %; anything larger needs a real voice, not an effect."""
    st = float(fx.get("pitch_semitones", 0) or 0)
    tempo = float(fx.get("tempo", 1.0) or 1.0)
    if abs(st) < 0.01 and abs(tempo - 1.0) < 0.01:
        return
    ratio = 2 ** (st / 12.0)
    sr = 24000
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=sample_rate", "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30)
        sr = int(p.stdout.strip())
    except Exception:
        pass
    # asetrate shifts pitch AND speed; the atempo undoes the speed part, then applies tempo.
    atempo = tempo / ratio
    filters = [f"asetrate={int(sr * ratio)}", f"aresample={sr}"]
    while atempo < 0.5:
        filters.append("atempo=0.5"); atempo /= 0.5
    while atempo > 2.0:
        filters.append("atempo=2.0"); atempo /= 2.0
    filters.append(f"atempo={atempo:.5f}")
    tmp = path + ".fx.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-af", ",".join(filters), tmp], check=True)
    os.replace(tmp, path)


def render(job: dict, run_dir: str) -> dict:
    eng = (job.get("engine") or "").lower()
    text = (job.get("text") or "").strip()
    out = job.get("out") or ""
    if not text or not out:
        return {"status": "error", "error": "text and out are required"}
    if not os.path.isabs(out):
        out = os.path.join(run_dir, out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        if eng == "orpheus":
            data = post(f"{ENGINES[eng]}/v1/audio/speech",
                        {"model": "orpheus", "input": text, "voice": job.get("voice", "tara"),
                         "response_format": "wav"})
        elif eng == "chatterbox":
            ref = job.get("reference_audio") or ""
            if ref and not os.path.isabs(ref):
                ref = os.path.join(run_dir, ref)
            body = {"text": text, "reference_audio_path": ref}
            for k in ("exaggeration", "cfg"):
                if k in job:
                    body[k] = job[k]
            data = post(f"{ENGINES[eng]}/tts/json", body)
        elif eng == "kokoro":
            data = post(f"{ENGINES[eng]}/tts", {"text": text, "voice": job.get("voice", "bm_george"),
                                                 "speed": float(job.get("speed", 1.0))})
        elif eng == "cosyvoice":
            return {"status": "error", "error": "cosyvoice has no REST endpoint on this server "
                                                "(Gradio webui on :50000 only) — cast this role on chatterbox or orpheus"}
        else:
            return {"status": "error", "error": f"unknown engine {eng!r}"}
    except Exception as exc:
        return {"status": "error", "error": f"{eng}: {str(exc)[:200]}"}
    if len(data) < 100:
        return {"status": "error", "error": f"{eng} returned {len(data)} bytes"}
    with open(out, "wb") as fh:
        fh.write(data)
    try:
        if job.get("post_fx"):
            post_fx(out, job["post_fx"])
    except Exception as exc:
        return {"status": "error", "out": out, "error": f"post_fx failed: {exc}"}
    return {"status": "ok", "out": out, "duration_s": ffprobe_dur(out)}


def gpu(owner: str) -> bool:
    r = subprocess.run(["bash", os.path.join(PIPE, "gpu_owner.sh"), owner], capture_output=False)
    return r.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", required=True)
    ap.add_argument("--run-dir", default=None)
    ap.add_argument("--results", default=None)
    ap.add_argument("--keep-gpu", action="store_true", help="leave the TTS cluster up afterwards")
    ap.add_argument("--no-gpu-switch", action="store_true", help="assume the TTS cluster is already up")
    args = ap.parse_args()

    try:
        doc = json.load(open(args.jobs, encoding="utf-8"))
    except Exception as exc:
        log(f"cannot read {args.jobs}: {exc}")
        return 2
    jobs = doc.get("jobs") if isinstance(doc, dict) else doc
    if not isinstance(jobs, list) or not jobs:
        log("no jobs")
        return 2
    run_dir = args.run_dir or os.path.dirname(os.path.abspath(args.jobs))
    results_path = args.results or (os.path.splitext(args.jobs)[0] + ".results.json")

    if not args.no_gpu_switch:
        log("handing the GPU to the TTS cluster (parks the LLM)")
        if not gpu("tts"):
            log("WARNING: TTS cluster did not report fully healthy — trying anyway")
    results = []
    ok = 0
    try:
        for i, job in enumerate(jobs):
            jid = job.get("id") or f"job{i:03d}"
            res = {"id": jid, "engine": job.get("engine")}
            res.update(render(job, run_dir))
            results.append(res)
            if res["status"] == "ok":
                ok += 1
                log(f"{jid}: {res['engine']} -> {os.path.relpath(res['out'], run_dir)} ({res.get('duration_s') or 0:.2f}s)")
            else:
                log(f"{jid}: FAILED {res.get('error')}")
    finally:
        json.dump({"jobs": len(jobs), "ok": ok, "results": results}, open(results_path, "w", encoding="utf-8"), indent=1)
        if not args.no_gpu_switch and not args.keep_gpu:
            # No local LLM: ComfyUI is the resting owner of the GPU.
            log("handing the GPU back to ComfyUI")
            gpu("comfyui")
    log(f"{ok}/{len(jobs)} rendered -> {results_path}")
    return 0 if ok == len(jobs) else 1


if __name__ == "__main__":
    sys.exit(main())
