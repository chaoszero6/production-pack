#!/usr/bin/env python3
"""sfx_render.py — a synced effects/ambience track per approved clip (MMAudio v2v via ComfyUI).

Port of studio.py's cmd_sfx (The Clockwork Moth): the clip itself is the conditioning —
MMAudio watches the picture (synchformer + CLIP) and renders foley that lands on the action.
The prompt is the shot's audio.sfx[] plus the location's ambience, with every clause that
would make MMAudio synthesise a human voice removed (the notes sometimes say "his voice
echoing" / "a hushed whisper"); the negative prompt bans speech, music and singing outright.

Why a separate script: ComfyUI and the TTS cluster are the exclusive GPU owners (no local LLM), so no agent turn can
call MMAudio. This runs after QA with the GPU handed to ComfyUI.

Per clip (clips/<SID>/): clip.mp4 (untrimmed) -> sfx.wav (48 kHz mono) + sfx_prompt.txt.
Only approved clips are rendered: qa_verdict.json verdict == PASS, or a MANUAL_PASS marker.
A clip is skipped when sfx.wav is newer than clip.mp4 (use --force).
Results -> clips/sfx_render_log.json.

  python3 sfx_render.py --run-dir D [--only S01_001,S01_002] [--force]
                        [--no-gpu-switch] [--keep-gpu] [--comfy http://127.0.0.1:8188]
Exit 0 all rendered, 1 at least one failed, 2 bad input, 3 ComfyUI / MMAudio nodes missing.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

PIPE = os.path.dirname(os.path.abspath(__file__))
COMFY_DEFAULT = "http://127.0.0.1:8188"

MMA_MODEL = "mmaudio_large_44k_v2_fp16.safetensors"
MMA_VAE = "mmaudio_vae_44k_fp16.safetensors"
MMA_SYNC = "mmaudio_synchformer_fp16.safetensors"
MMA_CLIP = "apple_DFN5B-CLIP-ViT-H-14-384_fp16.safetensors"
MMA_NODES = ("LoadVideo", "GetVideoComponents", "MMAudioModelLoader",
             "MMAudioFeatureUtilsLoader", "MMAudioSampler", "SaveAudio")
MMA_MAX_SECONDS = 30.0
SFX_STEPS = 25
SFX_CFG = 4.5

SFX_NEG = ("speech, voice, voices, talking, dialogue, babble, babbling, mumbling, whisper, whispering, "
           "background chatter, crowd, crowd noise, singing, hummed, hummed lullaby, choir, backing vocals, "
           "music, melody, song, narration, vocal, vocoder")

VOCAL_RE = re.compile(r"\b(voices?|whisper(ing|s)?|talk(ing|s)?|sing(ing|s|er|ers)?|sung|hum(med|ming|s)?|"
                      r"song|chant(ing|s)?|mumbl(e|ing)|babbler?|chatter|dialogue|narrat(ion|or)|"
                      r"laugh(ing|ter|s)?|shout(ing|s)?|scream(ing|s)?|cr(y|ying|ies)|sob(bing|s)?|"
                      r"gasp(ing|s)?|speech|spoken|lyrics?|vocals?|choir)\b", re.I)


def log(msg: str) -> None:
    print(f"[sfx-render {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── pure functions (unit-testable, no network) ─────────────────────────────────
def strip_vocal_clauses(text: str) -> list[str]:
    """Split a sound note on commas/semicolons and drop every clause that names a human voice."""
    parts = [p.strip(" ,.;") for p in re.split(r"[,;]", text or "")]
    return [p for p in parts if p and not VOCAL_RE.search(p)]


def sfx_prompt(sfx_list, ambience_bits) -> str:
    """shot audio.sfx[] + location ambience, voice clauses removed, MMAudio house suffix."""
    clauses: list[str] = []
    for item in list(sfx_list or []) + list(ambience_bits or []):
        if not isinstance(item, str):
            continue
        for c in strip_vocal_clauses(item):
            if c.lower() not in [x.lower() for x in clauses]:
                clauses.append(c)
    base = ", ".join(clauses) or "quiet room tone"
    return (f"{base}. Realistic foley and ambience for an animated film. "
            f"Absolutely no voices, no speech, no whispering, no music.")


def location_ambience(story: dict, location_id: str | None) -> list[str]:
    """Ambience hints for a location from story.json (whichever of the usual fields exists)."""
    if not isinstance(story, dict) or not location_id:
        return []
    for loc in story.get("locations") or []:
        if isinstance(loc, dict) and loc.get("id") == location_id:
            bits: list[str] = []
            for key in ("ambience", "ambient_sound", "soundscape", "sounds", "sound_effects", "atmospheric_effects"):
                v = loc.get(key)
                if isinstance(v, str):
                    bits.append(v)
                elif isinstance(v, list):
                    bits.extend(x for x in v if isinstance(x, str))
            return bits
    return []


def mmaudio_graph(video_name: str, prompt: str, duration: float, seed: int, prefix: str,
                  vocoder: str | None = None, negative: str = SFX_NEG) -> dict:
    """LoadVideo -> GetVideoComponents -> MMAudio loaders -> MMAudioSampler(images) -> SaveAudio."""
    utils = {"vae_model": MMA_VAE, "synchformer_model": MMA_SYNC, "clip_model": MMA_CLIP,
             "mode": "44k", "precision": "fp16"}
    if vocoder:
        utils["bigvgan_vocoder_model"] = vocoder
    return {
        "1": {"class_type": "LoadVideo", "inputs": {"file": video_name}},
        "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}},
        "3": {"class_type": "MMAudioModelLoader", "inputs": {"mmaudio_model": MMA_MODEL, "base_precision": "fp16"}},
        "4": {"class_type": "MMAudioFeatureUtilsLoader", "inputs": utils},
        "5": {"class_type": "MMAudioSampler", "inputs": {
            "mmaudio_model": ["3", 0], "feature_utils": ["4", 0], "duration": round(float(duration), 2),
            "steps": SFX_STEPS, "cfg": SFX_CFG, "seed": int(seed), "prompt": prompt, "negative_prompt": negative,
            "mask_away_clip": False, "force_offload": True, "images": ["2", 0]}},
        "6": {"class_type": "SaveAudio", "inputs": {"audio": ["5", 0], "filename_prefix": prefix}},
    }


def clip_approved(clip_dir: str) -> bool:
    if os.path.isfile(os.path.join(clip_dir, "MANUAL_PASS")):
        return True
    try:
        v = json.load(open(os.path.join(clip_dir, "qa_verdict.json"), encoding="utf-8"))
        return str(v.get("verdict", "")).upper() == "PASS"
    except Exception:
        return False


# ── ComfyUI http (urllib only — no third-party deps on the render host) ───────
def http_json(url: str, payload=None, timeout: float = 60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url} -> {e.code}: {e.read().decode(errors='replace')[:1200]}")


def http_bytes(url: str, timeout: float = 600) -> bytes:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url} -> {e.code}: {e.read().decode(errors='replace')[:800]}")


def comfy_alive(comfy: str) -> bool:
    try:
        http_json(f"{comfy}/api/system_stats", timeout=8)
        return True
    except Exception:
        return False


def node_info(comfy: str, name: str) -> dict | None:
    try:
        d = http_json(f"{comfy}/api/object_info/{name}", timeout=60)
    except Exception:
        return None
    return d.get(name) if isinstance(d, dict) and d.get(name) else None


def preflight_nodes(comfy: str, names=MMA_NODES) -> list[str]:
    """Return the node class names that ComfyUI does not know."""
    return [n for n in names if node_info(comfy, n) is None]


def free_comfy(comfy: str) -> None:
    try:
        http_json(f"{comfy}/api/free", {"unload_models": True, "free_memory": True}, timeout=120)
    except Exception as exc:
        log(f"/api/free failed (continuing): {exc}")


def upload_file(comfy: str, path: str, name: str) -> str:
    """Multipart upload into ComfyUI's input dir (works for mp4 as well as images)."""
    boundary = uuid.uuid4().hex
    with open(path, "rb") as fh:
        payload = fh.read()
    body = bytearray()
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n".encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n" + payload + b"\r\n"
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{comfy}/upload/image", data=bytes(body),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        j = json.loads(r.read())
    return (j.get("subfolder") + "/" if j.get("subfolder") else "") + j["name"]


def queue(comfy: str, wf: dict, client_id: str) -> str:
    return http_json(f"{comfy}/api/prompt", {"prompt": wf, "client_id": client_id})["prompt_id"]


def wait(comfy: str, pid: str, timeout: float = 1800, poll: float = 5) -> dict:
    t0 = time.time()
    down_since = None
    while time.time() - t0 < timeout:
        try:
            h = http_json(f"{comfy}/api/history/{pid}", timeout=20)
            down_since = None
        except Exception:
            h = {}
            down_since = down_since or time.time()
            if time.time() - down_since > 180:
                raise RuntimeError("ComfyUI unreachable for 3 min")
        e = h.get(pid) if isinstance(h, dict) else None
        if e:
            s = e.get("status", {}).get("status_str")
            if s == "success":
                return e
            if s == "error":
                msgs = [m for m in e["status"].get("messages", []) if m and m[0] == "execution_error"]
                raise RuntimeError(f"ComfyUI error: {json.dumps(msgs)[:1200]}")
        time.sleep(poll)
    raise TimeoutError(f"{pid} not done after {timeout:.0f}s")


def audio_outputs(entry: dict):
    for node in entry.get("outputs", {}).values():
        for key in ("audio", "images", "videos", "gifs"):
            for it in node.get(key, []) or []:
                if it.get("filename", "").lower().endswith((".flac", ".wav", ".mp3", ".ogg")):
                    yield it


def download(comfy: str, item: dict, dest: str) -> None:
    q = urllib.parse.urlencode({"filename": item["filename"], "subfolder": item.get("subfolder", ""),
                                "type": item.get("type", "output")})
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with open(tmp, "wb") as fh:
        fh.write(http_bytes(f"{comfy}/api/view?{q}", timeout=600))
    os.replace(tmp, dest)


def ffprobe_dur(path: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=60)
    return float(r.stdout.strip() or 0)


def ff(*args) -> None:
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *map(str, args)], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr[-1500:])


def gpu(owner: str) -> bool:
    return subprocess.run(["bash", os.path.join(PIPE, "gpu_owner.sh"), owner]).returncode == 0


# ── main ───────────────────────────────────────────────────────────────────────
def load_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--only", default="", help="comma-separated shot IDs")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-gpu-switch", action="store_true", help="assume ComfyUI already owns the GPU")
    ap.add_argument("--keep-gpu", action="store_true", help="accepted for compatibility (ComfyUI stays owner either way)")
    ap.add_argument("--comfy", default=COMFY_DEFAULT)
    args = ap.parse_args()
    comfy = args.comfy.rstrip("/")
    run_dir = os.path.abspath(args.run_dir)
    clips_dir = os.path.join(run_dir, "clips")
    log_path = os.path.join(clips_dir, "sfx_render_log.json")

    try:
        shot_list = load_json(os.path.join(run_dir, "shot_list.json"))
    except Exception as exc:
        log(f"cannot read shot_list.json: {exc}")
        return 2
    shots = {s.get("shot_id"): s for s in (shot_list.get("shots") or []) if isinstance(s, dict) and s.get("shot_id")}
    if not shots:
        log("shot_list.json has no shots")
        return 2
    try:
        story = load_json(os.path.join(run_dir, "story.json"))
    except Exception:
        story = {}
        log("story.json not readable — no location ambience in the prompts")

    only = [s.strip() for s in args.only.split(",") if s.strip()]
    order = only or sorted(shots)
    unknown = [s for s in order if s not in shots]
    if unknown:
        log(f"unknown shot IDs: {', '.join(unknown)}")
        return 2

    # which clips need work (before touching the GPU)
    todo, results = [], {}
    for sid in order:
        d = os.path.join(clips_dir, sid)
        clip, out = os.path.join(d, "clip.mp4"), os.path.join(d, "sfx.wav")
        if not os.path.isfile(clip):
            results[sid] = {"status": "skipped", "reason": "no clip.mp4"}
            continue
        if not clip_approved(d):
            results[sid] = {"status": "skipped", "reason": "not approved (qa_verdict PASS or MANUAL_PASS required)"}
            continue
        if os.path.isfile(out) and not args.force and os.path.getmtime(out) > os.path.getmtime(clip):
            results[sid] = {"status": "skipped", "reason": "sfx.wav newer than clip.mp4", "out": out}
            continue
        todo.append(sid)
    if not todo:
        log("nothing to render")
        os.makedirs(clips_dir, exist_ok=True)
        json.dump({"clips": results, "ok": 0, "failed": []}, open(log_path, "w", encoding="utf-8"), indent=1)
        return 0
    log(f"{len(todo)} clip(s) to render: {', '.join(todo)}")

    if not args.no_gpu_switch:
        log("handing the GPU to ComfyUI")
        if not gpu("comfyui"):
            log("ComfyUI did not come up healthy")
            return 3
    if not comfy_alive(comfy):
        log(f"ComfyUI unreachable at {comfy}")
        return 3
    missing = preflight_nodes(comfy)
    if missing:
        log(f"ComfyUI lacks node(s) {', '.join(missing)} — install comfyui-mmaudio (setup.sh --optional) "
            f"and the Kijai/MMAudio_safetensors weights under models/mmaudio/")
        return 3
    fu = node_info(comfy, "MMAudioFeatureUtilsLoader") or {}
    opts = ((fu.get("input") or {}).get("optional") or {}).get("bigvgan_vocoder_model", [[]])[0]
    vocoder = next((o for o in opts if isinstance(o, str) and o.endswith("bigvgan_generator.pt")), None)
    free_comfy(comfy)

    ok, failed = 0, []
    try:
        for sid in todo:
            shot = shots[sid]
            d = os.path.join(clips_dir, sid)
            clip, out = os.path.join(d, "clip.mp4"), os.path.join(d, "sfx.wav")
            audio = shot.get("audio") if isinstance(shot.get("audio"), dict) else {}
            prompt = sfx_prompt(audio.get("sfx") or [], location_ambience(story, shot.get("location_id")))
            with open(os.path.join(d, "sfx_prompt.txt"), "w", encoding="utf-8") as fh:
                fh.write(prompt + "\n")
            t0 = time.time()
            try:
                dur = min(ffprobe_dur(clip), MMA_MAX_SECONDS)
                if dur <= 0:
                    raise RuntimeError("ffprobe reports zero duration")
                name = upload_file(comfy, clip, f"sfx_{sid}.mp4")
                seed = random.randint(1, 2 ** 31)
                wf = mmaudio_graph(name, prompt, dur, seed, f"sfx/{sid}", vocoder)
                log(f"{sid}: MMAudio {dur:.1f}s — {prompt[:90]}…")
                entry = wait(comfy, queue(comfy, wf, f"sfx-{sid}"), timeout=1200)
                item = next(audio_outputs(entry), None)
                if not item:
                    raise RuntimeError("no audio in ComfyUI outputs")
                raw = os.path.join(d, "sfx_raw" + os.path.splitext(item["filename"])[1])
                download(comfy, item, raw)
                ff("-i", raw, "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", out)
                os.remove(raw)
                results[sid] = {"status": "ok", "out": out, "seconds": ffprobe_dur(out), "seed": seed,
                                "prompt": prompt, "render_s": round(time.time() - t0, 1)}
                ok += 1
                log(f"{sid}: sfx.wav {results[sid]['seconds']:.1f}s in {int(time.time() - t0)}s")
            except Exception as exc:
                results[sid] = {"status": "error", "error": str(exc)[:400], "prompt": prompt}
                failed.append(sid)
                log(f"{sid}: FAILED {exc}")
    finally:
        json.dump({"clips": results, "ok": ok, "failed": failed, "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
                  open(log_path, "w", encoding="utf-8"), indent=1)
    log(f"{ok}/{len(todo)} rendered -> {log_path}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
