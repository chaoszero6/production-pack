#!/usr/bin/env python3
"""Generate location reference images (establishing + medium shots) with Qwen Image 2.1 via ComfyUI.

Reads locations from a production story.json, generates for each location an
establishing wide shot and a medium shot at 2048x1152 (16:9) with
mood-appropriate lighting, saves them under {run_dir}/locations/{location_id}/,
and writes {run_dir}/location_manifest.json.

Workflow (matches the proven run_20260925 location generator on this box):
  UNETLoader (qwen_image_2.1_bf16) -> QwenImage21Cache
  + CLIPLoader (qwen3vl_8b_bf16, type=qwen_image) + VAELoader (qwen_image_2.1_vae_bf16)
  -> TextEncodeQwenImage21 -> EmptyLatentImage (2048x1152)
  -> KSampler (steps 25, cfg 1, euler, simple) -> VAEDecode -> SaveImage.

Note: qwen3vl_8b (NOT t5xxl) is the matching text encoder, and Qwen Image 2.1
is CFG-distilled — cfg must be 1 with scheduler `simple`. cfg 7 / scheduler
`normal` / t5xxl together decode to pure noise.

Queues one validation job first; only queues the remaining jobs if it succeeds.

Usage:
    python3 generate_location_refs.py --run-dir <run_dir> [--story story.json]
                                      [--force]
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from story_ref_ids import name_for_id, resolve_id

COMFYUI = "http://127.0.0.1:8188"
UNET_NAME = "qwen_image_2.1_bf16.safetensors"
CLIP_NAME = "qwen3vl_8b_bf16.safetensors"
VAE_NAME = "qwen_image_2.1_vae_bf16.safetensors"
WIDTH, HEIGHT = 2048, 1152
STEPS = 25
CFG = 1.0
SAMPLER = "euler"
SCHEDULER = "simple"

NEGATIVE_PROMPT = (
    "photorealistic, photograph, 2d illustration, flat coloring, sketch, line art, "
    "text, watermark, logo, signature, frame, border, people, person, humans, humanoid, "
    "characters, animals, creatures, insects, figures, crowds, cropped, cut off at edges, "
    "blurry, low quality, oversaturated, harsh shadows, fisheye distortion"
)

STYLE_SUFFIX = (
    "3D animated film environment reference in the style of Pixar animation, lush "
    "detailed environment with rich textures, soft volumetric lighting, cinematic "
    "composition, high detail render, warm saturated palette with magical blue-gold accents"
)


def build_prompt(body: str) -> str:
    return f"Location reference. {body} {STYLE_SUFFIX}"


# One entry per story.json location id. `medium` is the sub-area focus for the
# medium shot (filename suffix {location_id}_{medium}.png), matching the
# reference paths used in shot_list.json where applicable (e.g. nana_repair_shop_workroom).
LOCATIONS = [
    {
        "id": "tobias_bedroom",
        "medium": "workbench",
        "mood_establishing": "Wide establishing shot of an empty small cozy bedroom belonging to a young inventor, built into the thick curved inner stone wall of a colossal ancient clock bell. Wooden shelves crammed with broken toys, loose gears, coiled springs and half-finished clockwork inventions. One large arched window with an ornate black iron frame overlooking the village square. Warm amber fairy lights strung from the ceiling. A small wooden workbench by the window, a nightstand with a tea cup. Evening: golden sunset fading to deep blue dusk outside the window, soft fireflies drifting past, warm amber interior glow, gentle magical wonder. No people.",
        "mood_medium": "Medium shot of the small wooden workbench beside the large arched iron-framed window in a cozy inventor's bedroom. The workbench is cluttered with loose brass gears, coiled springs, small hand tools, a half-disassembled music box and scattered clockwork parts. Warm amber fairy lights glowing softly overhead, honey wood shelves with toys and inventions in the background. Evening: blue dusk with fading golden afterglow outside the open window, fireflies outside, a faint pale-blue magical glow on the workbench. No people.",
        "lighting_establishing": "Golden sunset fading to blue dusk, fireflies, warm amber fairy-light interior; wonder, gentle tension",
        "lighting_medium": "Blue dusk with fading golden afterglow, fireflies, faint pale-blue moonstone glow on the workbench; tender, magical",
    },
    {
        "id": "nana_repair_shop",
        "medium": "workroom",
        "mood_establishing": "Wide establishing shot of a cluttered warm antique clock repair shop interior in a village. Walls lined with clocks of every shape and size - cuckoo clocks, mantel clocks, wall clocks, pendulum clocks - all running at different speeds. Workbenches with tools and half-repaired objects, a large ornate grandfather clock dominating the back wall, a kitchen corner with a small wooden table. Walnut wood surfaces, warm cream clock faces, brass details. Bright late morning: warm golden sunlight streaming through the shop windows, soft volumetric god rays, cozy domestic warmth, lived-in clutter. No people.",
        "mood_medium": "Medium shot of the main workroom of a village clock repair shop. A sturdy workbench with tools laid out, a half-repaired pocket watch, small gears and an oil can; a large ornate grandfather clock standing against the back wall; shelves crowded with clocks of every size, warm cream faces and brass details. Walnut wood, late morning: bright warm golden window light, soft volumetric god rays, cozy lived-in atmosphere. No people.",
        "lighting_establishing": "Bright late-morning warm golden window light, volumetric god rays; cozy domestic warmth",
        "lighting_medium": "Late-morning golden light in the main workroom; warm, lived-in",
    },
    {
        "id": "village_square",
        "medium": "fountain",
        "mood_establishing": "Wide establishing shot of an empty cobblestone village square at night, a stone fountain in the center. The square is surrounded by terracotta cottages built into the curved inner wall of a colossal ancient bronze clock bell, the enormous bell shell rising behind the village, half-buried in a misty green valley. Old iron lanterns with a warm orange glow light the cobblestones, brass bell wind chimes hang from the eaves. Night: moonlit blue sky with stars, silver-blue moonlight, gentle mist in the valley, quiet adventure with a hint of mystery. No people.",
        "mood_medium": "Medium shot of an old weathered stone fountain in a cobblestone village square at night. The stone basin is covered in moss and dust, a hidden old dust-covered stone staircase spirals down behind the fountain base. A nearby iron lantern casts warm orange light, moonlit blue ambient light, soft mist, quiet and slightly mysterious. No people.",
        "lighting_establishing": "Night, moonlit blue sky, warm orange lantern glow; quiet tension, adventure",
        "lighting_medium": "Night, orange lantern light against moonlit blue; mystery around the hidden staircase",
    },
    {
        "id": "underbell",
        "medium": "gears",
        "mood_establishing": "Wide establishing shot of a vast subterranean cavern beneath a village, cathedral scale. Colossal stone gears the size of houses are embedded in the cavern walls, frozen, moss-covered and rusted in places, silent. Massive tarnished brass chains hang from the ceiling like stalactites. Bioluminescent blue moss covers stone and metal, the whole space glowing like an underwater dream. Shafts of pale moonlight pierce from above through cracks in the ancient bell shell far overhead, soft volumetric light shafts. Deep cavern blue-black shadows, awe and mystery. No people.",
        "mood_medium": "Medium shot inside the cavern of a frozen time machine, focusing on a colossal stone gear embedded in the cavern wall, caked with moss, rusted, its teeth outlined in bioluminescent blue moss. Tarnished brass chains hang down from above, pale stone gray and deep cavern blue-black shadows, a shaft of pale moonlight crossing the frame, the space glows like an underwater dream. No people.",
        "lighting_establishing": "Bioluminescent blue moss glow, moonlight shafts from above; awe, underwater-dream atmosphere",
        "lighting_medium": "Blue bioluminescence on frozen moss-caked gears, one moonlight shaft; unease, silence",
    },
    {
        "id": "great_gear_chamber",
        "medium": "hub",
        "mood_establishing": "Wide establishing shot of the heart of a subterranean cavern: a single enormous gear, its teeth carved from pale stone, reaching up toward the cavern ceiling, nearly stopped and caked in moss. Its hub is a massive crystalline structure with a faint dying pulse of pale blue light, like a heartbeat about to stop. Ancient runic inscriptions cover the stone surface, glowing faint warm gold. Deep shadow, one thin shaft of moonlight, awe and melancholy. No people.",
        "mood_medium": "Medium shot of the massive crystalline hub at the heart of an enormous pale stone gear in a dark cavern. The crystal structure pulses with a faint dying pale blue light, like a heartbeat about to stop; ancient runic inscriptions glow warm gold across the stone around it. Deep shadow, bioluminescent blue moss at the edges, one shaft of pale moonlight, reverent melancholic atmosphere. No people.",
        "lighting_establishing": "Near-darkness, faint dying crystalline blue pulse, faint gold runes, one moonlight shaft; awe, sadness",
        "lighting_medium": "Dying pale-blue crystal pulse with warm gold runic glow in deep shadow; vulnerability, revelation",
    },
]


def log(msg):
    print(f"[loc-refs {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def build_workflow(prompt: str, seed: int, filename_prefix: str) -> dict:
    """ComfyUI API-format prompt graph for one Qwen Image 2.1 T2I generation at 2048x1152."""
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
                "resolution": 1024,
            },
        },
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": WIDTH, "height": HEIGHT, "batch_size": 1}},
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
    """Wait until every prompt_id has a terminal history entry. Returns {pid: entry}."""
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
    ap.add_argument("--force", action="store_true", help="regenerate even if the file exists")
    ap.add_argument("--seed-base", type=int, default=20260925)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    story_path = Path(args.story) if args.story else run_dir / "story.json"
    # story.json may omit the canonical location ids the script's LOCATIONS
    # table (and shot_list.json) use — recover them from the names.
    known_ids = {loc["id"] for loc in LOCATIONS}
    try:
        story_locations = json.loads(story_path.read_text()).get("locations", [])
    except (OSError, json.JSONDecodeError):
        story_locations = []
    story_ids = {
        loc.get("id") or resolve_id(loc.get("name", ""), "", known_ids)
        for loc in story_locations
    }
    missing = [l["id"] for l in LOCATIONS if l["id"] not in story_ids]
    if missing:
        log(f"WARNING: locations in script not in story.json: {missing}")

    # ── Plan jobs ────────────────────────────────────────────────────
    jobs = []  # (loc, shot_key, prompt, seed, dest, prefix, lighting)
    for li, loc in enumerate(LOCATIONS):
        loc_dir = run_dir / "locations" / loc["id"]
        plan = [
            ("establishing", f"{loc['id']}.png", loc["mood_establishing"], loc["lighting_establishing"]),
            ("medium", f"{loc['id']}_{loc['medium']}.png", loc["mood_medium"], loc["lighting_medium"]),
        ]
        for si, (shot, fname, prompt_body, lighting) in enumerate(plan):
            dest = loc_dir / fname
            if dest.exists() and not args.force:
                log(f"skip {loc['id']}/{shot} (exists)")
                jobs.append((loc, shot, None, 0, dest, None, lighting))
                continue
            prompt = build_prompt(prompt_body)
            seed = args.seed_base + li * 2 + si
            prefix = f"locref_{loc['id']}_{shot}"
            jobs.append((loc, shot, prompt, seed, dest, prefix, lighting))

    to_run = [j for j in jobs if j[2] is not None]
    log(f"{len(jobs)} total jobs, {len(to_run)} to generate, {len(jobs) - len(to_run)} cached")
    if not to_run:
        log("nothing to do")
    else:
        # ── Phase 1: validation job (catches graph/VRAM errors early) ─
        prompt_ids = {}
        meta = {}
        for loc, shot, prompt, seed, dest, prefix, lighting in to_run[:1]:
            label = f"{loc['id']}/{shot}"
            pid = queue(build_workflow(prompt, seed, prefix), client_id=f"loc-refs-{label.replace('_', '-')}")
            prompt_ids[pid] = label
            meta[pid] = {"loc": loc, "shot": shot, "prompt": prompt, "seed": seed,
                         "dest": dest, "lighting": lighting}
            log(f"queued {label} (validation) -> {pid}")
        first_hist = wait_all(prompt_ids, timeout=60 * 60)
        failed = []
        for pid, label in prompt_ids.items():
            entry = first_hist[pid]
            if entry.get("status", {}).get("status_str") != "success":
                log(f"{label}: VALIDATION FAILED {entry.get('status', {}).get('messages', [])}")
                failed.append(label)
        if failed:
            log("aborting remaining jobs after validation failure")
        else:
            # ── Phase 2: queue the rest ──────────────────────────────
            for loc, shot, prompt, seed, dest, prefix, lighting in to_run[1:]:
                label = f"{loc['id']}/{shot}"
                pid = queue(build_workflow(prompt, seed, prefix), client_id=f"loc-refs-{label.replace('_', '-')}")
                prompt_ids[pid] = label
                meta[pid] = {"loc": loc, "shot": shot, "prompt": prompt, "seed": seed,
                             "dest": dest, "lighting": lighting}
                log(f"queued {label} -> {pid}")
            rest_hist = wait_all({p: l for p, l in prompt_ids.items() if p not in first_hist},
                                 timeout=60 * 60 * max(1, len(to_run)))
            first_hist.update(rest_hist)

        # ── Download outputs ──────────────────────────────────────────
        for pid, label in prompt_ids.items():
            entry = first_hist[pid]
            m = meta[pid]
            status = entry.get("status", {}).get("status_str", "unknown")
            if status != "success":
                log(f"{label}: FAILED {entry.get('status', {}).get('messages', [])}")
                continue
            outputs = find_save_outputs(entry)
            if not outputs:
                log(f"{label}: no output images found")
                continue
            download_image(outputs[0]["filename"], outputs[0].get("subfolder", ""),
                           m["dest"], outputs[0].get("type", "output"))
            log(f"{label}: saved {m['dest']}")
            provenance = {
                "location_id": m["loc"]["id"],
                "location_name": m["loc"]["id"].replace("_", " ").title(),
                "shot": m["shot"],
                "lighting": m["lighting"],
                "model": UNET_NAME,
                "text_encoder": CLIP_NAME,
                "vae": VAE_NAME,
                "width": WIDTH,
                "height": HEIGHT,
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
        "schema": "production-pack/location-manifest/1",
        "run_dir": str(run_dir),
        "story": str(story_path),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": "pipeline/generate_location_refs.py",
        "settings": {
            "comfyui": COMFYUI,
            "model": UNET_NAME,
            "text_encoder": CLIP_NAME,
            "vae": VAE_NAME,
            "width": WIDTH,
            "height": HEIGHT,
            "steps": STEPS,
            "cfg": CFG,
            "sampler": SAMPLER,
            "scheduler": SCHEDULER,
            "shots": ["establishing", "medium"],
        },
        "locations": [],
        "files": [],
    }
    all_ok = True
    for loc in LOCATIONS:
        loc_dir = run_dir / "locations" / loc["id"]
        shots = {}
        for shot, fname in (("establishing", f"{loc['id']}.png"),
                            ("medium", f"{loc['id']}_{loc['medium']}.png")):
            dest = loc_dir / fname
            exists = dest.exists()
            rel = str(dest.relative_to(run_dir))
            lighting = loc[f"lighting_{shot}"]
            rec = {
                "file": rel,
                "exists": exists,
                "size_bytes": dest.stat().st_size if exists else None,
                "lighting": lighting,
            }
            prov_path = dest.with_suffix(".json")
            if prov_path.exists():
                try:
                    prov = json.loads(prov_path.read_text())
                    rec.update(prompt=prov.get("prompt"), seed=prov.get("seed"),
                               comfyui_prompt_id=prov.get("comfyui_prompt_id"))
                except json.JSONDecodeError:
                    pass
            shots[shot] = rec
            if exists:
                manifest["files"].append(rel)
            else:
                all_ok = False
        manifest["locations"].append({
            "id": loc["id"],
            "name": story_lookup_name(story_path, loc["id"]),
            "directory": str(loc_dir.relative_to(run_dir)),
            "shots": shots,
        })
    manifest["total_files"] = len(manifest["files"])
    manifest["status"] = "complete" if all_ok else "partial"
    manifest_path = run_dir / "location_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    log(f"manifest written: {manifest_path} ({manifest['status']}, {manifest['total_files']} files)")
    return 0 if all_ok else 2


_story_cache = {}


def story_lookup_name(story_path: Path, loc_id: str) -> str:
    if str(story_path) not in _story_cache:
        try:
            _story_cache[str(story_path)] = json.loads(story_path.read_text())
        except (OSError, json.JSONDecodeError):
            _story_cache[str(story_path)] = {}
    locations = _story_cache[str(story_path)].get("locations", [])
    for loc in locations:
        if loc.get("id") == loc_id:
            return loc.get("name", loc_id)
    return name_for_id(loc_id, [loc.get("name", "") for loc in locations])


if __name__ == "__main__":
    sys.exit(main())
