#!/usr/bin/env python3
"""ideogram_frames.py — render a clip's first/last keyframes with the LOCAL Ideogram 4 model
(native ComfyUI graph) using bounding-box layout, then lock character identity with a
Qwen Image 2.1 reference pass.

Why two passes (knowledge/minimax_h3_rules.md §0.3):
  * Ideogram 4 reads a structured JSON caption whose `elements[].bbox` places every subject at
    an exact position and size ([y_min, x_min, y_max, x_max], 0-1000). That is the one control
    the Qwen Image path lacks — the Cinderquay extreme-wide drifted because nothing pinned where
    the figures stood.
  * Ideogram 4 is text-to-image ONLY (official docs: no reference images, no edit, no character
    reference), so its figures have the right place and size but a generic face/costume. Pass 2
    feeds the layout image as <Picture 1> to Qwen Image 2.1 with the character design sheets and
    asks for the same composition with the sheet identities. 0-character plates skip pass 2.
    `--refs-in-context` instead tries the Rebalance pack's "Ideogram 4 Edit" encoder (design
    sheets as in-context images on the Qwen3-VL encoder) inside pass 1 — experimental.

Native graph = the official "Ideogram v4: Text to Image" template (Comfy-Org/workflow_templates):
  UNETLoader(cond) -> CFGOverride(cfg 3 from 70 %) -> DualModelGuider(cfg 7, model_negative =
  UNETLoader(unconditional), negative = ConditioningZeroOut(positive)); CLIPLoader(qwen3vl_8b,
  type ideogram4) -> CLIPTextEncode(JSON caption); EmptyFlux2LatentImage; Ideogram4Scheduler
  (Default preset: 20 steps, mu 0.0, std 1.75); KSamplerSelect(euler); SamplerCustomAdvanced;
  VAEDecode(flux2-vae).

Reads  <shot_dir>/frame_spec.json (same file generate_frames.py reads) with optional per-frame
       "background", "placements": [{"character_id", "bbox": [ymin,xmin,ymax,xmax] 0-1000,
       "desc", "color_palette"}], "style": {aesthetics, lighting, art_style, medium, color_palette},
       "identity_pass": true|false.
Writes <shot_dir>/first_frame.png / last_frame.png (+ <role>_layout.png from pass 1) and
       frame_meta.json exactly like generate_frames.py, so generate_video_direct.py runs i2va/fl2va.

Usage: ideogram_frames.py --shot-dir <dir> --run-dir <dir> [--preset default|quality|turbo]
       [--no-identity] [--refs-in-context] [--keep-gpu] [--no-gpu-switch] [--force]
Exit 4 = the Ideogram 4 models/nodes are not available (run.sh falls back to generate_frames.py).

Model files (ComfyUI model dirs; 207 keeps them on /mnt/ChaosWares1/comfyui/models, registered
in extra_model_paths.yaml): diffusion_models/ideogram4_nvfp4_mixed.safetensors +
ideogram4_unconditional_nvfp4_mixed.safetensors, text_encoders/qwen3vl_8b_nvfp4.safetensors
(or qwen3vl_8b_fp8_scaled), vae/flux2-vae.safetensors. Licence: Ideogram 4 Non-Commercial.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import time

import requests

PIPE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PIPE)
import generate_frames as gf  # noqa: E402  (ComfyUI helpers + the Qwen reference graph)

UNET_COND = os.environ.get("IDEOGRAM4_UNET", "ideogram4_nvfp4_mixed.safetensors")
UNET_UNCOND = os.environ.get("IDEOGRAM4_UNET_UNCOND", "ideogram4_unconditional_nvfp4_mixed.safetensors")
TEXT_ENCODER = os.environ.get("IDEOGRAM4_CLIP", "qwen3vl_8b_nvfp4.safetensors")
TEXT_ENCODER_FALLBACK = "qwen3vl_8b_fp8_scaled.safetensors"
VAE = os.environ.get("IDEOGRAM4_VAE", "flux2-vae.safetensors")
PRESETS = {  # official ideogram4.sampler_configs: steps, mu, std; the last ~10 % runs at cfg 3
    "quality": (48, 0.0, 1.5),
    "default": (20, 0.0, 1.75),
    "turbo": (12, 0.5, 1.75),
}
CFG_MAIN, CFG_POLISH, POLISH_FROM = 7.0, 3.0, 0.7   # template: CFGOverride(3, 0.7, 1) + DualModelGuider cfg 7
CONTINUITY_NAMES = {"prev_clip_lastframe.png", "next_clip_firstframe.png", "scene_establishing_frame.png"}

IDENTITY_PROMPT = (
    "Recreate <Picture 1> exactly: the same camera, framing, layout, background, lighting and the "
    "same position and size of every figure. Replace each figure's face, hair, build and costume "
    "with the character shown in the matching design sheet: {pairs}. Pixar-style 3D animated "
    "feature render, clean sharp surfaces, no text."
)


def log(msg: str) -> None:
    print(f"[ideogram {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _hex_list(values, limit=16) -> list[str]:
    out = []
    for v in values or []:
        s = str(v).strip().upper()
        if len(s) == 7 and s[0] == "#" and all(c in "0123456789ABCDEF" for c in s[1:]):
            out.append(s)
    return out[:limit]


def _bbox(b) -> list[int] | None:
    """[ymin,xmin,ymax,xmax] in 0-1000, or {"x","y","w","h"} fractions -> Ideogram bbox."""
    if isinstance(b, dict):
        x, y, w, h = (float(b.get(k, 0)) for k in ("x", "y", "w", "h"))
        b = [y * 1000, x * 1000, (y + h) * 1000, (x + w) * 1000]
    if not (isinstance(b, (list, tuple)) and len(b) == 4):
        return None
    try:
        v = [max(0, min(1000, int(round(float(t))))) for t in b]
    except (TypeError, ValueError):
        return None
    ymin, xmin, ymax, xmax = v
    if ymin > ymax:
        ymin, ymax = ymax, ymin
    if xmin > xmax:
        xmin, xmax = xmax, xmin
    return [ymin, xmin, ymax, xmax]


def build_caption(frame: dict) -> str:
    """Ideogram 4 structured caption (docs/prompting.md: key order matters, minified JSON)."""
    style = frame.get("style") or {}
    caption: dict = {}
    hl = (frame.get("high_level_description") or frame.get("prompt") or "").strip()
    if hl:
        caption["high_level_description"] = hl[:600]
    sd = {
        "aesthetics": style.get("aesthetics") or "cinematic, warm, appealing, feature-animation polish",
        "lighting": style.get("lighting") or frame.get("lighting") or "soft cinematic key light with gentle rim light",
        "medium": style.get("medium") or "3d_render",
        "art_style": style.get("art_style") or "Pixar-style 3D animated feature film, soft rounded forms, rich textures",
    }
    pal = _hex_list(style.get("color_palette"))
    if pal:
        sd["color_palette"] = pal
    caption["style_description"] = sd
    elements = []
    for p in frame.get("placements") or []:
        if not isinstance(p, dict):
            continue
        el: dict = {"type": "obj"}
        bb = _bbox(p.get("bbox"))
        if bb:
            el["bbox"] = bb
        el["desc"] = (p.get("desc") or p.get("character_id") or "a figure").strip()
        pp = _hex_list(p.get("color_palette"), 5)
        if pp:
            el["color_palette"] = pp
        elements.append(el)
    if not elements:
        elements.append({"type": "obj", "desc": hl or "the subject of the shot"})
    caption["compositional_deconstruction"] = {
        "background": (frame.get("background") or "").strip() or hl or "the scene",
        "elements": elements,
    }
    return json.dumps(caption, separators=(",", ":"), ensure_ascii=False)


def ideogram_workflow(caption: str, w: int, h: int, seed: int, prefix: str, preset: str,
                      text_encoder: str, ref_names: list[str] | None = None) -> dict:
    steps, mu, std = PRESETS.get(preset, PRESETS["default"])
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET_COND, "weight_dtype": "default"}},
        "2": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET_UNCOND, "weight_dtype": "default"}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": text_encoder, "type": "ideogram4", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["5", 0]}},
        "7": {"class_type": "CFGOverride", "inputs": {"model": ["1", 0], "cfg": CFG_POLISH,
                                                      "start_percent": POLISH_FROM, "end_percent": 1.0}},
        "8": {"class_type": "DualModelGuider", "inputs": {"model": ["7", 0], "positive": ["5", 0], "cfg": CFG_MAIN,
                                                           "model_negative": ["2", 0], "negative": ["6", 0]}},
        "9": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "10": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "11": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "12": {"class_type": "Ideogram4Scheduler", "inputs": {"steps": steps, "width": w, "height": h, "mu": mu, "std": std}},
        "13": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["10", 0], "guider": ["8", 0], "sampler": ["9", 0],
                                                                  "sigmas": ["12", 0], "latent_image": ["11", 0]}},
        "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["4", 0]}},
        "15": {"class_type": "SaveImage", "inputs": {"images": ["14", 0], "filename_prefix": prefix}},
    }
    if ref_names:
        # experimental: design sheets as in-context images on the Qwen3-VL encoder (Rebalance pack)
        enc = {"text": caption, "clip": ["3", 0], "steering": 1.0, "layer_multiplier": 1.0, "enable_step": True}
        for i, name in enumerate(ref_names[:4], 1):
            wf[str(20 + i)] = {"class_type": "LoadImage", "inputs": {"image": name}}
            enc[f"image{i}"] = [str(20 + i), 0]
            enc[f"image{i}_tokens"] = "normal"
        wf["5"] = {"class_type": "Ideogram4EditRebalance", "inputs": enc}
    else:
        wf["5"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["3", 0], "text": caption}}
    return wf


def available(need_refs_node: bool) -> tuple[bool, str]:
    """Models + nodes present in this ComfyUI? (object_info lists the loader choices.)"""
    try:
        unet = requests.get(f"{gf.COMFYUI}/object_info/UNETLoader", timeout=20).json()["UNETLoader"]["input"]["required"]["unet_name"][0]
        clip = requests.get(f"{gf.COMFYUI}/object_info/CLIPLoader", timeout=20).json()["CLIPLoader"]["input"]["required"]["clip_name"][0]
        vae = requests.get(f"{gf.COMFYUI}/object_info/VAELoader", timeout=20).json()["VAELoader"]["input"]["required"]["vae_name"][0]
        nodes = requests.get(f"{gf.COMFYUI}/object_info", timeout=60).json()
    except Exception as exc:  # noqa: BLE001
        return False, f"ComfyUI object_info unreachable: {exc}"
    missing = [n for n in (UNET_COND, UNET_UNCOND) if n not in unet]
    if VAE not in vae:
        missing.append(VAE)
    te = TEXT_ENCODER if TEXT_ENCODER in clip else (TEXT_ENCODER_FALLBACK if TEXT_ENCODER_FALLBACK in clip else None)
    if te is None:
        missing.append(TEXT_ENCODER)
    for n in ("DualModelGuider", "CFGOverride", "Ideogram4Scheduler", "EmptyFlux2LatentImage"):
        if n not in nodes:
            missing.append(f"node {n}")
    if need_refs_node and "Ideogram4EditRebalance" not in nodes:
        missing.append("node Ideogram4EditRebalance (ComfyUI-Conditioning-Rebalance)")
    if missing:
        return False, "missing: " + ", ".join(missing)
    return True, te


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot-dir", required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--comfyui-url", default=gf.COMFYUI)
    ap.add_argument("--preset", default=os.environ.get("IDEOGRAM4_PRESET", "default"), choices=sorted(PRESETS))
    ap.add_argument("--no-identity", action="store_true", help="skip the Qwen identity pass")
    ap.add_argument("--refs-in-context", action="store_true",
                    help="experimental: design sheets as in-context images on the Ideogram encoder (pass 1)")
    ap.add_argument("--keep-gpu", action="store_true")
    ap.add_argument("--no-gpu-switch", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    gf.COMFYUI = args.comfyui_url.rstrip("/")
    shot_dir, run_dir = os.path.abspath(args.shot_dir), os.path.abspath(args.run_dir)
    shot_id = os.path.basename(shot_dir)
    spec_path = os.path.join(shot_dir, "frame_spec.json")
    meta_path = os.path.join(shot_dir, "frame_meta.json")
    try:
        spec = json.load(open(spec_path, encoding="utf-8"))
        assert isinstance(spec, dict)
    except Exception as exc:  # noqa: BLE001
        log(f"{shot_id}: cannot read frame_spec.json ({exc})")
        return 2
    mode = (spec.get("mode") or "reference").lower()
    frames = [f for f in (spec.get("frames") or []) if isinstance(f, dict) and (f.get("prompt") or f.get("background"))]
    vrefs = []
    for v in spec.get("video_references") or []:
        if isinstance(v, dict):
            rp = gf.resolve(v.get("image") or v.get("resolved_path") or "", shot_dir, run_dir)
            if rp:
                vrefs.append({"tag": v.get("tag"), "image": rp, "resolved_path": rp, "role": v.get("role")})
    meta = {"shot_id": shot_id, "mode": mode, "first_frame": None, "last_frame": None,
            "video_references": vrefs, "generated": [], "engine": "ideogram4+qwen",
            "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    if not frames:
        log(f"{shot_id}: mode {mode}, no frames to render — reference-only frame_meta")
        json.dump(meta, open(meta_path, "w", encoding="utf-8"), indent=2)
        return 0
    if os.path.isfile(meta_path) and not args.force:
        try:
            old = json.load(open(meta_path))
            if old.get("first_frame") and os.path.isfile(old["first_frame"]):
                log(f"{shot_id}: frame_meta.json already has rendered frames (use --force)")
                return 0
        except Exception:  # noqa: BLE001
            pass

    if not args.no_gpu_switch:
        log("handing the GPU to ComfyUI (parks the LLM)")
        if not gf.gpu("comfyui"):
            log("ComfyUI not healthy")
            return 3
    ok, info = available(args.refs_in_context)
    if not ok:
        log(f"Ideogram 4 not usable here ({info}) — caller falls back to generate_frames.py")
        return 4
    text_encoder = info

    rc = 0
    try:
        for f in frames:
            role = f.get("role") or "first_frame"
            w, h = int(f.get("width") or 1344), int(f.get("height") or 768)
            w, h = max(256, (w // 16) * 16), max(256, (h // 16) * 16)
            seed = int(f.get("seed") or random.randint(0, 2 ** 31))
            caption = build_caption(f)
            layout_dest = os.path.join(shot_dir, f"{role}_layout.png")
            dest = os.path.join(shot_dir, f"{role}.png")
            placements = f.get("placements") or []
            refs_disk = [gf.resolve(r, shot_dir, run_dir) for r in (f.get("references") or [])]
            refs_disk = [r for r in refs_disk if r and os.path.basename(r) not in CONTINUITY_NAMES]
            sheets = [r for r in refs_disk if "/characters/" in r]
            ctx_names = []
            if args.refs_in_context and sheets:
                ctx_names = [gf.upload(s) for s in sheets[:4]]
            log(f"{shot_id}: pass 1 Ideogram 4 layout {role} ({w}x{h}, {len(placements)} placements, "
                f"preset {args.preset}, seed {seed}{', in-context refs ' + str(len(ctx_names)) if ctx_names else ''})")
            try:
                pid = gf.queue(ideogram_workflow(caption, w, h, seed, f"{shot_id}/{role}_layout", args.preset,
                                                 text_encoder, ctx_names), f"ideo-{shot_id}")
                gf.download(gf.wait(pid), layout_dest)
            except Exception as exc:  # noqa: BLE001
                log(f"{shot_id}: {role} layout FAILED: {exc}")
                rc = 1
                continue
            identity = (not args.no_identity) and f.get("identity_pass", True) and bool(placements) and bool(sheets)
            if identity:
                log(f"{shot_id}: pass 2 Qwen Image 2.1 identity ({len(sheets)} sheets)")
                try:
                    names = [gf.upload(layout_dest)] + [gf.upload(s) for s in sheets[:8]]
                    pairs = "; ".join(f"figure {i} = <Picture {i + 1}> ({os.path.basename(os.path.dirname(s))})"
                                      for i, s in enumerate(sheets[:8], 1))
                    prompt = IDENTITY_PROMPT.format(pairs=pairs) + " " + (f.get("prompt") or "")
                    pid = gf.queue(gf.qwen_workflow(prompt, w, h, seed, f"{shot_id}/{role}", names), f"ideo-id-{shot_id}")
                    gf.download(gf.wait(pid), dest)
                except Exception as exc:  # noqa: BLE001
                    log(f"{shot_id}: identity pass FAILED ({exc}) — using the layout frame as-is")
                    shutil.copyfile(layout_dest, dest)
            else:
                shutil.copyfile(layout_dest, dest)
            meta[role] = dest
            meta["generated"].append({"role": role, "file": dest, "layout": layout_dest, "seed": seed,
                                      "caption": caption, "identity_pass": identity, "sheets": sheets,
                                      "in_context_refs": bool(ctx_names)})
            log(f"{shot_id}: {role} -> {dest}")
        if mode == "first_last_frame" and not meta.get("last_frame"):
            log(f"{shot_id}: first_last_frame mode but no last_frame rendered — video step will fall back")
    finally:
        json.dump(meta, open(meta_path, "w", encoding="utf-8"), indent=2)
        if not args.no_gpu_switch and not args.keep_gpu:
            log("handing the GPU back to the LLM (≈2 min)")
            gf.gpu("llm")
    return rc


if __name__ == "__main__":
    sys.exit(main())
