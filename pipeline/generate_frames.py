#!/usr/bin/env python3
"""generate_frames.py — render a shot's keyframes (Qwen Image 2.1 via ComfyUI)
from the image-generator agent's frame_spec.json, with the GPU handed to ComfyUI.

Why: ComfyUI and the local LLM are exclusive GPU owners (systemd Conflicts=),
so the agent cannot call ComfyUI during its turn. It PLANS (frame_spec.json);
this script renders and writes frame_meta.json for generate_video_direct.py.

frame_spec.json
{
  "mode": "composited_i2v | first_frame_only | first_last_frame | reference",
  "frames": [
    {"role": "first_frame", "prompt": "…", "references": ["/abs/sheet.png", "/abs/plate.png"],
     "width": 1344, "height": 768, "seed": 12345},
    {"role": "last_frame", ...}
  ],
  "video_references": [{"tag": "<Subject 1>", "image": "/abs/path.png", "role": "character identity"}]
}
References go into TextEncodeQwenImage21 as images.image_1..N (edit/composite
mode, same graph studio.py used for The Clockwork Moth keyframes).

frame_meta.json (written here):
  {"mode", "first_frame": "<abs>", "last_frame": "<abs>|null", "video_references": [...],
   "generated": [...], "ts"}

  python3 generate_frames.py --shot-dir <clip_dir> --run-dir <run_dir>
                             [--comfyui-url U] [--keep-gpu] [--no-gpu-switch] [--force]
Exit 0 ok, 1 render failed, 2 bad spec, 3 ComfyUI unreachable.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time

import requests

PIPE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PIPE)
from generate_character_sheets import (  # noqa: E402  (same model files + sampler settings)
    CLIP_NAME, NEGATIVE_PROMPT, SAMPLER, SCHEDULER, STEPS, UNET_NAME, VAE_NAME,
)

COMFYUI = "http://127.0.0.1:8188"


def log(msg: str) -> None:
    print(f"[frames {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def gpu(owner: str) -> bool:
    return subprocess.run(["bash", os.path.join(PIPE, "gpu_owner.sh"), owner]).returncode == 0


def upload(path: str) -> str:
    with open(path, "rb") as fh:
        r = requests.post(f"{COMFYUI}/upload/image", files={"image": (os.path.basename(path), fh)},
                          data={"overwrite": "true"}, timeout=120)
    r.raise_for_status()
    j = r.json()
    return (j.get("subfolder") + "/" if j.get("subfolder") else "") + j["name"]


def qwen_workflow(prompt: str, w: int, h: int, seed: int, prefix: str, ref_names: list[str]) -> dict:
    enc = {"clip": ["3", 0], "prompt": prompt, "negative_prompt": NEGATIVE_PROMPT, "vae": ["4", 0],
           "resolution": 1024}
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET_NAME, "weight_dtype": "default"}},
        "2": {"class_type": "QwenImage21Cache", "inputs": {"model": ["1", 0], "device": "auto", "dtype": "default"}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP_NAME, "type": "qwen_image"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_NAME}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "7": {"class_type": "KSampler", "inputs": {"model": ["2", 0], "seed": seed, "steps": STEPS, "cfg": 1.0,
                                                   "sampler_name": SAMPLER, "scheduler": SCHEDULER,
                                                   "positive": ["5", 0], "negative": ["5", 1],
                                                   "latent_image": ["6", 0], "denoise": 1.0}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["4", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}},
    }
    for i, name in enumerate(ref_names):
        nid = str(20 + i)
        wf[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        enc[f"images.image_{i + 1}"] = [nid, 0]
    wf["5"] = {"class_type": "TextEncodeQwenImage21", "inputs": enc}
    return wf


def queue(wf: dict, client_id: str) -> str:
    r = requests.post(f"{COMFYUI}/api/prompt", json={"prompt": wf, "client_id": client_id}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"queue failed ({r.status_code}): {r.text[:400]}")
    return r.json()["prompt_id"]


def wait(pid: str, timeout: float = 1800) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            h = requests.get(f"{COMFYUI}/api/history/{pid}", timeout=30).json().get(pid)
        except Exception:
            h = None
        if h and h.get("status", {}).get("status_str") in ("success", "error"):
            if h["status"]["status_str"] != "success":
                raise RuntimeError("ComfyUI reported error: " + json.dumps(h["status"])[:400])
            return h
        time.sleep(5)
    raise TimeoutError(f"prompt {pid} not finished after {timeout:.0f}s")


def download(h: dict, dest: str) -> None:
    for node in h.get("outputs", {}).values():
        for img in node.get("images", []):
            params = {"filename": img["filename"], "type": img.get("type", "output")}
            if img.get("subfolder"):
                params["subfolder"] = img["subfolder"]
            r = requests.get(f"{COMFYUI}/api/view", params=params, timeout=120)
            r.raise_for_status()
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(r.content)
            return
    raise RuntimeError("no image in ComfyUI outputs")


def resolve(path: str, shot_dir: str, run_dir: str) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return None
    p = path.strip()
    cands = [p] if os.path.isabs(p) else [os.path.join(shot_dir, p), os.path.join(run_dir, p),
                                          os.path.join(run_dir, p[len("output/"):] if p.startswith("output/") else p)]
    for c in cands:
        if os.path.isfile(c):
            return os.path.abspath(c)
    # A planner-invented variant (harbour_square_prologue.png) falls back to the canonical
    # file in the same directory, as generate_video_direct._sibling_ref does; without it the
    # keyframe renders with no plate at all (S01_001 take 3 rendered with "0 refs").
    for c in cands:
        d = os.path.dirname(c)
        if os.path.isdir(d):
            stem = os.path.basename(d)
            for name in (f"{stem}.png", f"{stem}_side.png", f"{stem}_three_quarter.png", f"{stem}_front.png"):
                alt = os.path.join(d, name)
                if os.path.isfile(alt):
                    log(f"  reference not found, using sibling: {os.path.basename(c)} -> {name}")
                    return os.path.abspath(alt)
    return None


def main() -> int:
    global COMFYUI
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot-dir", required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--comfyui-url", default=COMFYUI)
    ap.add_argument("--keep-gpu", action="store_true", help="leave ComfyUI as GPU owner (video step follows)")
    ap.add_argument("--no-gpu-switch", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    COMFYUI = args.comfyui_url.rstrip("/")
    shot_dir, run_dir = os.path.abspath(args.shot_dir), os.path.abspath(args.run_dir)
    shot_id = os.path.basename(shot_dir)
    spec_path = os.path.join(shot_dir, "frame_spec.json")
    meta_path = os.path.join(shot_dir, "frame_meta.json")

    try:
        spec = json.load(open(spec_path, encoding="utf-8"))
        assert isinstance(spec, dict)
    except Exception as exc:
        log(f"{shot_id}: cannot read frame_spec.json ({exc}) — an agent that crashed leaves prose here")
        return 2
    mode = (spec.get("mode") or "reference").lower()
    frames = [f for f in (spec.get("frames") or []) if isinstance(f, dict) and f.get("prompt")]
    vrefs = []
    for v in spec.get("video_references") or []:
        if isinstance(v, dict):
            rp = resolve(v.get("image") or v.get("resolved_path") or "", shot_dir, run_dir)
            if rp:
                vrefs.append({"tag": v.get("tag"), "image": rp, "resolved_path": rp, "role": v.get("role")})
    meta = {"shot_id": shot_id, "mode": mode, "first_frame": None, "last_frame": None,
            "video_references": vrefs, "generated": [], "ts": time.strftime("%Y-%m-%d %H:%M:%S")}

    if not frames:
        log(f"{shot_id}: mode {mode}, no frames to render — writing reference-only frame_meta")
        json.dump(meta, open(meta_path, "w", encoding="utf-8"), indent=2)
        return 0
    if os.path.isfile(meta_path) and not args.force:
        try:
            old = json.load(open(meta_path))
            if old.get("first_frame") and os.path.isfile(old["first_frame"]):
                log(f"{shot_id}: frame_meta.json already has rendered frames (use --force)")
                return 0
        except Exception:
            pass

    if not args.no_gpu_switch:
        log("handing the GPU to ComfyUI (parks the LLM)")
        if not gpu("comfyui"):
            log("ComfyUI not healthy")
            return 3
    try:
        requests.get(f"{COMFYUI}/api/system_stats", timeout=8).raise_for_status()
    except Exception as exc:
        log(f"ComfyUI unreachable: {exc}")
        return 3

    rc = 0
    try:
        for f in frames:
            role = f.get("role") or "first_frame"
            refs = []
            for r in f.get("references") or []:
                rp = resolve(r, shot_dir, run_dir)
                if rp:
                    try:
                        refs.append(upload(rp))
                    except Exception as exc:
                        log(f"  upload failed {rp}: {exc}")
                else:
                    log(f"  reference not found, skipped: {r}")
            w, h = int(f.get("width") or 1344), int(f.get("height") or 768)
            seed = int(f.get("seed") or random.randint(0, 2 ** 31))
            prefix = f"{shot_id}/{role}"
            dest = os.path.join(shot_dir, f"{role}.png")
            log(f"{shot_id}: rendering {role} ({w}x{h}, {len(refs)} refs, seed {seed})")
            try:
                pid = queue(qwen_workflow(f["prompt"], w, h, seed, prefix, refs), f"frames-{shot_id}")
                hist = wait(pid)
                download(hist, dest)
            except Exception as exc:
                log(f"{shot_id}: {role} FAILED: {exc}")
                rc = 1
                continue
            meta[role] = dest
            meta["generated"].append({"role": role, "file": dest, "seed": seed, "references": refs,
                                      "prompt": f["prompt"]})
            log(f"{shot_id}: {role} -> {dest}")
        if mode == "first_last_frame" and not meta.get("last_frame"):
            log(f"{shot_id}: first_last_frame mode but no last_frame rendered — video step will fall back")
    finally:
        json.dump(meta, open(meta_path, "w", encoding="utf-8"), indent=2)
        if not args.no_gpu_switch and not args.keep_gpu:
            log("handing the GPU back to the LLM (≈2 min)")
            gpu("llm")
    return rc


if __name__ == "__main__":
    sys.exit(main())
