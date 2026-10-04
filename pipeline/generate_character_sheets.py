#!/usr/bin/env python3
"""Generate multi-angle character reference sheets with Qwen Image 2.1 via ComfyUI.

Reads characters from a production story.json, generates front / three-quarter /
side / back full-body views at 2048x2048 for each character, saves them under
{run_dir}/characters/{char_id}/, and writes {run_dir}/character_manifest.json.

Workflow (matches the proven Qwen Image 2.1 T2I config on this box —
qwen_image_2_1_t2i_bf16.json + the run_20260925 location generator):
  UNETLoader (qwen_image_2.1_bf16) -> QwenImage21Cache
  + CLIPLoader (qwen3vl_8b_bf16, type=qwen_image) + VAELoader (qwen_image_2.1_vae_bf16)
  -> TextEncodeQwenImage21 -> EmptyLatentImage (2048x2048)
  -> KSampler (steps 25, cfg 1, euler, simple) -> VAEDecode -> SaveImage.

Note: qwen3vl_8b (NOT t5xxl) is the matching text encoder, and Qwen Image 2.1
is CFG-distilled — cfg must be 1 with scheduler `simple`. cfg 7 / scheduler
`normal` / t5xxl together decode to pure noise.

Usage:
    python3 generate_character_sheets.py --run-dir <run_dir> [--story story.json]
                                         [--only tobias,tick] [--force]
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from story_ref_ids import is_creature, resolve_characters

COMFYUI = "http://127.0.0.1:8188"
UNET_NAME = "qwen_image_2.1_bf16.safetensors"
CLIP_NAME = "qwen3vl_8b_bf16.safetensors"
VAE_NAME = "qwen_image_2.1_vae_bf16.safetensors"
RESOLUTION = 2048
STEPS = 25
CFG = 1.0
SAMPLER = "euler"
SCHEDULER = "simple"

NEGATIVE_PROMPT = (
    "photorealistic, photograph, 2d illustration, flat coloring, sketch, line art, "
    "text, watermark, logo, signature, frame, border, multiple characters, cropped, "
    "cut off at edges, extra limbs, extra fingers, deformed hands, messy tangle, "
    "blurry, low quality, oversaturated, harsh shadows"
)

STYLE_SUFFIX = (
    "Pixar-style 3D animation character concept art. Soft volumetric lighting, warm "
    "saturated palette with magical blue-gold accents, subsurface scattering on skin, "
    "exaggerated expressive features, rich material detail. Clean neutral studio "
    "background with a soft gray gradient, even diffuse studio lighting, full body "
    "visible with generous margin around the figure, sharp focus, high detail render."
)

# view key -> (file suffix, pose direction)
VIEWS = {
    "front": (
        "front",
        "Full body front view. The character stands upright in a relaxed neutral pose, "
        "facing the camera directly, both arms hanging naturally at the sides, feet "
        "slightly apart.",
    ),
    "three_quarter": (
        "three_quarter",
        "Full body three-quarter view. The character is turned about 45 degrees to the "
        "left of the camera, body and head at a three-quarter angle, in a relaxed "
        "neutral standing pose.",
    ),
    "side": (
        "side",
        "Full body side view. The character is in exact side profile facing left, "
        "standing upright in a neutral pose, both arms at the sides.",
    ),
    "back": (
        "back",
        "Full body back view. The character is seen directly from behind, facing away "
        "from the camera, standing upright in a neutral pose with arms at the sides.",
    ),
}

# Creature subjects (not humanoids): the standing-pose wording would mislead the model.
CREATURE_POSES = {
    "front": (
        "front",
        "Full body front view. The creature hovers facing the camera, both wings fully "
        "spread and symmetric, body centered.",
    ),
    "three_quarter": (
        "three_quarter",
        "Full body three-quarter view. The creature hovers turned about 45 degrees to "
        "the left of the camera, wings spread so both the side of the body and the wing "
        "structure are visible.",
    ),
    "side": (
        "side",
        "Full body side view. The creature is in exact side profile facing left, wings "
        "folded slightly back to show the body and the gear-wing cross-section.",
    ),
    "back": (
        "back",
        "Full body back view. The creature is seen from directly behind with both wings "
        "fully spread, the back of the body and the inner wing structure visible.",
    ),
}


def log(msg):
    print(f"[char-sheets {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def build_prompt(char: dict, view: str) -> str:
    """Compose the full prompt for one character + view from story.json data."""
    parts = []
    if is_creature(char):
        poses = CREATURE_POSES
        subject = f"Character reference sheet of a magical mechanical creature: {char['name']}."
    else:
        poses = VIEWS
        age = char.get("age", "")
        extra = f", {age} years old" if age else ""
        subject = f"Character reference sheet of {char['name']}, {char.get('type', 'character')}{extra}."
    parts.append(subject)
    parts.append(char["visual_description"])
    anchors = char.get("identity_anchors") or []
    visual_anchors = [a for a in anchors if "voice" not in a.lower() and "audio" not in a.lower()]
    if visual_anchors:
        parts.append("Key identity details to keep exact: " + "; ".join(visual_anchors) + ".")
    parts.append(poses[view][1])
    parts.append(STYLE_SUFFIX)
    return " ".join(parts)


def build_workflow(prompt: str, seed: int, filename_prefix: str) -> dict:
    """ComfyUI API-format prompt graph for one Qwen Image 2.1 generation."""
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET_NAME, "weight_dtype": "default"}},
        "2": {"class_type": "QwenImage21Cache", "inputs": {"model": ["1", 0], "device": "auto", "dtype": "default"}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP_NAME, "type": "qwen_image"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_NAME}},
        "5": {
            "class_type": "TextEncodeQwenImage21",
            "inputs": {
                "clip": ["3", 0],
                "prompt": prompt,
                "negative_prompt": NEGATIVE_PROMPT,
                "vae": ["4", 0],
                "resolution": RESOLUTION,
            },
        },
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": RESOLUTION, "height": RESOLUTION, "batch_size": 1}},
        "7": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["2", 0],
                "seed": seed,
                "steps": STEPS,
                "cfg": CFG,
                "sampler_name": SAMPLER,
                "scheduler": SCHEDULER,
                "positive": ["5", 0],
                "negative": ["5", 1],
                "latent_image": ["6", 0],
                "denoise": 1.0,
            },
        },
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["4", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": filename_prefix}},
    }


def comfy_api(path: str = "", method: str = "GET", **kwargs):
    resp = requests.request(method, f"{COMFYUI}/api/{path}", timeout=kwargs.pop("timeout", 30), **kwargs)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def queue(workflow: dict, client_id: str) -> str:
    payload = {"prompt": workflow, "client_id": client_id}
    resp = requests.post(f"{COMFYUI}/api/prompt", json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Queue failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()["prompt_id"]


def wait_all(prompt_ids: dict, timeout: float) -> dict:
    """Wait until every prompt_id has a history entry. Returns {pid: history}."""
    done: dict = {}
    start = time.time()
    while time.time() - start < timeout:
        for pid in list(prompt_ids):
            if pid in done:
                continue
            try:
                hist = comfy_api(f"history/{pid}")
            except requests.RequestException:
                continue
            entry = hist.get(pid)
            if entry and entry.get("status", {}).get("status_str") in ("success", "error"):
                done[pid] = entry
                status = "ok" if entry["status"]["status_str"] == "success" else "ERROR"
                log(f"{prompt_ids[pid]}: {status} ({len(done)}/{len(prompt_ids)})")
        if len(done) == len(prompt_ids):
            return done
        time.sleep(15)
    raise TimeoutError(f"{len(prompt_ids) - len(done)} job(s) still running after {timeout:.0f}s")


def download_image(filename: str, subfolder: str, dest: Path, out_type: str = "output") -> None:
    # /api/view rejects type=result on this ComfyUI build — history reports the
    # real type, so pass it through instead of hard-coding.
    params = {"filename": filename, "type": out_type or "output"}
    if subfolder:
        params["subfolder"] = subfolder
    resp = requests.get(f"{COMFYUI}/api/view", params=params, timeout=120)
    if resp.status_code == 400 and params["type"] != "result":
        params["type"] = "result"
        resp = requests.get(f"{COMFYUI}/api/view", params=params, timeout=120)
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)


def find_save_outputs(history: dict) -> list:
    """Extract SaveImage outputs (filename/subfolder) from a history entry."""
    out = []
    for node_out in history.get("outputs", {}).values():
        for item in node_out.get("images", []):
            out.append(item)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--story", default=None, help="story.json path (default: {run-dir}/story.json)")
    ap.add_argument("--only", default="", help="comma-separated character ids to generate")
    ap.add_argument("--force", action="store_true", help="regenerate even if the file exists")
    ap.add_argument("--seed-base", type=int, default=20260925)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    story_path = Path(args.story) if args.story else run_dir / "story.json"
    story = json.loads(story_path.read_text())
    characters = story["characters"]
    resolve_characters(characters, run_dir)
    if args.only:
        wanted = set(args.only.split(","))
        characters = [c for c in characters if c["id"] in wanted]
    if not characters:
        log("ERROR: no characters matched")
        return 1

    manifest_path = run_dir / "character_manifest.json"

    # ── Plan jobs (skip existing unless --force) ─────────────────────
    jobs = []  # (char, view_key, prompt, seed, dest, prefix)
    for ci, char in enumerate(characters):
        char_dir = run_dir / "characters" / char["id"]
        for vi, view in enumerate(VIEWS):
            suffix = (CREATURE_POSES if is_creature(char) else VIEWS)[view][0]
            dest = char_dir / f"{char['id']}_{suffix}.png"
            if dest.exists() and not args.force:
                log(f"skip {char['id']}/{view} (exists)")
                jobs.append((char, view, None, 0, dest, None))
                continue
            prompt = build_prompt(char, view)
            seed = args.seed_base + ci * 4 + vi
            prefix = f"charsheet_{char['id']}_{suffix}"
            jobs.append((char, view, prompt, seed, dest, prefix))

    to_run = [j for j in jobs if j[2] is not None]
    log(f"{len(jobs)} total jobs, {len(to_run)} to generate, {len(jobs) - len(to_run)} cached")
    if not to_run:
        log("nothing to do")
    else:
        log(f"queuing {len(to_run)} generations ({STEPS} steps, cfg {CFG}, {SAMPLER}/{SCHEDULER}, {RESOLUTION}x{RESOLUTION})")
        prompt_ids = {}
        meta = {}
        for char, view, prompt, seed, dest, prefix in to_run:
            label = f"{char['id']}/{view}"
            workflow = build_workflow(prompt, seed, prefix)
            pid = queue(workflow, client_id=f"char-sheets-{label.replace('_', '-')}")
            prompt_ids[pid] = label
            meta[pid] = {"char": char, "view": view, "prompt": prompt, "seed": seed,
                         "dest": dest, "prefix": prefix, "workflow": workflow}
            log(f"queued {label} -> {pid}")

        timeout = 60 * 60 * max(1, len(to_run))  # generous: model load + per-image time
        histories = wait_all(prompt_ids, timeout=timeout)

        # ── Download outputs ──────────────────────────────────────────
        for pid, label in prompt_ids.items():
            entry = histories[pid]
            m = meta[pid]
            status = entry.get("status", {}).get("status_str", "unknown")
            if status != "success":
                msg = entry.get("status", {}).get("messages", [])
                log(f"{label}: FAILED {msg}")
                continue
            outputs = find_save_outputs(entry)
            if not outputs:
                log(f"{label}: no output images found")
                continue
            download_image(outputs[0]["filename"], outputs[0].get("subfolder", ""),
                           m["dest"], outputs[0].get("type", "output"))
            log(f"{label}: saved {m['dest']}")
            # record per-file provenance alongside the image
            provenance = {
                "character_id": m["char"]["id"],
                "character_name": m["char"]["name"],
                "view": m["view"],
                "model": UNET_NAME,
                "text_encoder": CLIP_NAME,
                "vae": VAE_NAME,
                "resolution": RESOLUTION,
                "steps": STEPS,
                "cfg": CFG,
                "sampler": SAMPLER,
                "scheduler": SCHEDULER,
                "seed": m["seed"],
                "prompt": m["prompt"],
                "negative_prompt": NEGATIVE_PROMPT,
                "comfyui_prompt_id": pid,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            m["dest"].with_suffix(".json").write_text(json.dumps(provenance, indent=2) + "\n")

    # ── Write manifest ────────────────────────────────────────────────
    manifest = {
        "schema": "production-pack/character-manifest/1",
        "run_dir": str(run_dir),
        "story": str(story_path),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": "pipeline/generate_character_sheets.py",
        "settings": {
            "comfyui": COMFYUI,
            "model": UNET_NAME,
            "text_encoder": CLIP_NAME,
            "vae": VAE_NAME,
            "resolution": RESOLUTION,
            "width": RESOLUTION,
            "height": RESOLUTION,
            "steps": STEPS,
            "cfg": CFG,
            "sampler": SAMPLER,
            "scheduler": SCHEDULER,
            "views": list(VIEWS.keys()),
        },
        "characters": [],
        "files": [],
    }
    all_ok = True
    for char in characters:
        char_dir = run_dir / "characters" / char["id"]
        views = {}
        for vi, view in enumerate(VIEWS):
            suffix = (CREATURE_POSES if is_creature(char) else VIEWS)[view][0]
            dest = char_dir / f"{char['id']}_{suffix}.png"
            exists = dest.exists()
            rel = str(dest.relative_to(run_dir))
            rec = {
                "view": view,
                "file": rel,
                "exists": exists,
                "size_bytes": dest.stat().st_size if exists else None,
            }
            # restore prompt/seed from provenance if present
            prov_path = dest.with_suffix(".json")
            if prov_path.exists():
                try:
                    prov = json.loads(prov_path.read_text())
                    rec.update(prompt=prov.get("prompt"), seed=prov.get("seed"),
                               comfyui_prompt_id=prov.get("comfyui_prompt_id"))
                except json.JSONDecodeError:
                    pass
            views[view] = rec
            if exists:
                manifest["files"].append(rel)
            else:
                all_ok = False
        manifest["characters"].append({
            "id": char["id"],
            "name": char["name"],
            "role": char.get("role", ""),
            "directory": str(char_dir.relative_to(run_dir)),
            "views": views,
        })
    manifest["total_files"] = len(manifest["files"])
    manifest["status"] = "complete" if all_ok else "partial"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    log(f"manifest written: {manifest_path} ({manifest['status']}, {manifest['total_files']} files)")
    return 0 if all_ok else 2


if __name__ == "__main__":
    sys.exit(main())
