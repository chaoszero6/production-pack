#!/usr/bin/env python3
"""Generate H3 video directly via ComfyUI API — no dsh agent needed.

Reads reviewed_prompt.json + frame_meta.json, builds a known-good API
workflow, uploads reference images, submits to ComfyUI, monitors queue,
and copies output to clip.mp4.

Supports modes: ref2va, i2va, fl2va

Usage: generate_video_direct.py <shot_dir> <run_dir> [--comfyui-url URL]
"""
import argparse
import json
import os
import random
import shutil
import sys
import time
import urllib.request
import urllib.parse

COMFYUI = "http://127.0.0.1:8188"

# ── Model paths (relative to ComfyUI model dirs) ─────────────
UNET_REF2VA = "cicalooo__10Eros-Max-h3-int8-convrot__10Eros_Max_h3_ref2va_beta2_pruned_int8_convrot.safetensors"
UNET_FL2VA = "cicalooo__10Eros-Max-h3-int8-convrot__10Eros_Max_h3_fl2va_beta2_pruned_int8_convrot_skip_edges.safetensors"
CLIP_ENCODER = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"
TURBO_LORA_REF2V = "minimax/minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors"
TURBO_LORA_FL2V = "minimax/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"

OUTPUT_PREFIX = "video/production_pack"
WIDTH = 1344
HEIGHT = 768
FPS = 24


def log(msg):
    print(f"[video-direct {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _shot_path(shot_dir, p):
    """frame_meta stores either an absolute path or a name relative to the
    clip dir; resolve it, and treat blank/non-string values as absent."""
    if not isinstance(p, str) or not p.strip():
        return None
    p = p.strip()
    return p if os.path.isabs(p) else os.path.join(shot_dir, p)


def h3_frame_count(duration_s):
    """Calculate H3-compatible frame count from duration in seconds."""
    raw = max(5, round(duration_s * FPS))
    remainder = raw % 17
    if remainder != 5 % 17:
        raw += (5 - (raw % 17)) % 17
    return max(22, raw)


def upload_image(image_path, comfyui_url):
    """Upload an image to ComfyUI and return the filename it's stored as."""
    import http.client
    import mimetypes

    basename = os.path.basename(image_path)
    content_type = mimetypes.guess_type(image_path)[0] or "image/png"

    boundary = f"----PythonBoundary{random.randint(100000, 999999)}"
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="image"; filename="{basename}"\r\n'.encode()
    body += f"Content-Type: {content_type}\r\n\r\n".encode()
    with open(image_path, "rb") as f:
        body += f.read()
    body += f"\r\n--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="overwrite"\r\n\r\n'
    body += b'true'
    body += f"\r\n--{boundary}--\r\n".encode()

    parsed = urllib.parse.urlparse(comfyui_url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 8188)
    conn.request(
        "POST",
        "/upload/image",
        body=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    resp = conn.getresponse()
    data = resp.read()
    conn.close()

    if resp.status != 200:
        log(f"Upload failed ({resp.status}): {data[:200]}")
        return None

    result = json.loads(data)
    name = result.get("name", basename)
    log(f"  Uploaded {basename} -> {name}")
    return name


def build_ref2va_workflow(prompt_text, ref_images, duration_s, dialogue_wav=None):
    """Build ComfyUI API workflow for ref2va mode."""
    length = h3_frame_count(duration_s)
    seed = random.randint(0, 2**53)
    steps = 8

    log(f"  ref2va: {len(ref_images)} refs, {duration_s}s -> {length} frames, {steps} steps")

    wf = {}

    # 1. UNET Loader
    wf["1"] = {
        "class_type": "UNETLoader",
        "inputs": {"unet_name": UNET_REF2VA, "weight_dtype": "default"},
    }

    # 2. Turbo LoRA
    wf["2"] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {
            "model": ["1", 0],
            "lora_name": TURBO_LORA_REF2V,
            "strength_model": 1.0,
        },
    }

    # 3. CLIP Loader
    wf["3"] = {
        "class_type": "CLIPLoader",
        "inputs": {"clip_name": CLIP_ENCODER, "type": "minimax", "device": "default"},
    }

    # 4. Video VAE
    wf["4"] = {
        "class_type": "VAELoader",
        "inputs": {"vae_name": VIDEO_VAE},
    }

    # 5. Audio VAE
    wf["5"] = {
        "class_type": "VAELoader",
        "inputs": {"vae_name": AUDIO_VAE},
    }

    # 6-9. Load reference images
    ref_node_ids = []
    for i, img_name in enumerate(ref_images):
        nid = str(6 + i)
        wf[nid] = {
            "class_type": "LoadImage",
            "inputs": {"image": img_name},
        }
        ref_node_ids.append(nid)

    # 10. MiniMaxH3ReferenceToVideo
    r2v_inputs = {
        "clip": ["3", 0],
        "prompt": prompt_text,
        "width": WIDTH,
        "height": HEIGHT,
        "length": length,
        "ref_image_size": "match",
    }
    # Optional VAEs
    r2v_inputs["vae"] = ["4", 0]
    r2v_inputs["audio_vae"] = ["5", 0]

    # Reference images
    for i, nid in enumerate(ref_node_ids):
        r2v_inputs[f"ref_images.ref_image_{i}"] = [nid, 0]

    # Dialogue audio reference (for lip sync)
    if dialogue_wav:
        wf["50"] = {
            "class_type": "LoadAudio",
            "inputs": {"audio": dialogue_wav},
        }
        r2v_inputs["ref_audios.ref_audio_0"] = ["50", 0]

    wf["10"] = {
        "class_type": "MiniMaxH3ReferenceToVideo",
        "inputs": r2v_inputs,
    }

    # 11. BasicScheduler
    wf["11"] = {
        "class_type": "BasicScheduler",
        "inputs": {
            "model": ["2", 0],
            "scheduler": "beta",
            "steps": steps,
            "denoise": 1.0,
        },
    }

    # 12. KSamplerSelect
    wf["12"] = {
        "class_type": "KSamplerSelect",
        "inputs": {"sampler_name": "ddim"},
    }

    # 13. RandomNoise
    wf["13"] = {
        "class_type": "RandomNoise",
        "inputs": {"noise_seed": seed},
    }

    # 14. BasicGuider
    wf["14"] = {
        "class_type": "BasicGuider",
        "inputs": {
            "model": ["2", 0],
            "conditioning": ["10", 0],
        },
    }

    # 15. SamplerCustomAdvanced
    wf["15"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["13", 0],
            "guider": ["14", 0],
            "sampler": ["12", 0],
            "sigmas": ["11", 0],
            "latent_image": ["10", 1],
        },
    }

    # 16. VAEDecode (video)
    wf["16"] = {
        "class_type": "VAEDecode",
        "inputs": {
            "samples": ["15", 0],
            "vae": ["4", 0],
        },
    }

    # 17. VAEDecodeAudio
    wf["17"] = {
        "class_type": "VAEDecodeAudio",
        "inputs": {
            "samples": ["15", 0],
            "vae": ["5", 0],
        },
    }

    # 18. CreateVideo
    wf["18"] = {
        "class_type": "CreateVideo",
        "inputs": {
            "images": ["16", 0],
            "audio": ["17", 0],
            "fps": FPS,
            "bit_depth": 8,
            "color_space": "sRGB",
            "codec": "none",
        },
    }

    # 19. SaveVideo
    wf["19"] = {
        "class_type": "SaveVideo",
        "inputs": {
            "video": ["18", 0],
            "filename_prefix": OUTPUT_PREFIX,
            "format": "auto",
            "codec": "auto",
        },
    }

    return wf


def build_fl2va_workflow(prompt_text, first_frame, last_frame, duration_s, dialogue_wav=None):
    """Build ComfyUI API workflow for fl2va (first-last-frame) mode.

    Uses MiniMaxH3ImageToVideo with both first_frame and last_frame.
    No audio_vae (node doesn't support it) — audio is overlaid in post.
    """
    length = h3_frame_count(duration_s)
    seed = random.randint(0, 2**53)
    steps = 4

    log(f"  fl2va: {duration_s}s -> {length} frames, {steps} steps")

    wf = {}

    wf["1"] = {
        "class_type": "UNETLoader",
        "inputs": {"unet_name": UNET_FL2VA, "weight_dtype": "default"},
    }
    wf["2"] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {"model": ["1", 0], "lora_name": TURBO_LORA_FL2V, "strength_model": 1.0},
    }
    wf["3"] = {
        "class_type": "CLIPLoader",
        "inputs": {"clip_name": CLIP_ENCODER, "type": "minimax", "device": "default"},
    }
    wf["4"] = {"class_type": "VAELoader", "inputs": {"vae_name": VIDEO_VAE}}

    wf["6"] = {"class_type": "LoadImage", "inputs": {"image": first_frame}}
    wf["7"] = {"class_type": "LoadImage", "inputs": {"image": last_frame}}

    wf["10"] = {
        "class_type": "MiniMaxH3ImageToVideo",
        "inputs": {
            "clip": ["3", 0],
            "vae": ["4", 0],
            "prompt": prompt_text,
            "width": WIDTH,
            "height": HEIGHT,
            "length": length,
            "first_frame": ["6", 0],
            "last_frame": ["7", 0],
        },
    }

    wf["11"] = {
        "class_type": "BasicScheduler",
        "inputs": {"model": ["2", 0], "scheduler": "beta", "steps": steps, "denoise": 1.0},
    }
    wf["12"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "ddim"}}
    wf["13"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
    wf["14"] = {
        "class_type": "BasicGuider",
        "inputs": {"model": ["2", 0], "conditioning": ["10", 0]},
    }
    wf["15"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["13", 0], "guider": ["14", 0], "sampler": ["12", 0],
            "sigmas": ["11", 0], "latent_image": ["10", 1],
        },
    }
    wf["16"] = {"class_type": "VAEDecode", "inputs": {"samples": ["15", 0], "vae": ["4", 0]}}
    wf["18"] = {
        "class_type": "CreateVideo",
        "inputs": {"images": ["16", 0], "fps": FPS},
    }
    wf["19"] = {
        "class_type": "SaveVideo",
        "inputs": {"video": ["18", 0], "filename_prefix": OUTPUT_PREFIX, "format": "auto", "codec": "auto"},
    }

    return wf


def build_i2va_workflow(prompt_text, image_name, duration_s, dialogue_wav=None):
    """Build ComfyUI API workflow for i2va (image-to-video) mode.

    Uses MiniMaxH3ImageToVideo with first_frame only (composited scene).
    No audio_vae — audio overlaid in post.
    """
    length = h3_frame_count(duration_s)
    seed = random.randint(0, 2**53)
    steps = 8

    log(f"  i2va: {duration_s}s -> {length} frames, {steps} steps")

    wf = {}
    wf["1"] = {"class_type": "UNETLoader", "inputs": {"unet_name": UNET_REF2VA, "weight_dtype": "default"}}
    wf["2"] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {"model": ["1", 0], "lora_name": TURBO_LORA_REF2V, "strength_model": 1.0},
    }
    wf["3"] = {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP_ENCODER, "type": "minimax", "device": "default"}}
    wf["4"] = {"class_type": "VAELoader", "inputs": {"vae_name": VIDEO_VAE}}
    wf["6"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}

    wf["10"] = {
        "class_type": "MiniMaxH3ImageToVideo",
        "inputs": {
            "clip": ["3", 0],
            "vae": ["4", 0],
            "prompt": prompt_text,
            "width": WIDTH,
            "height": HEIGHT,
            "length": length,
            "first_frame": ["6", 0],
        },
    }

    wf["11"] = {"class_type": "BasicScheduler", "inputs": {"model": ["2", 0], "scheduler": "beta", "steps": steps, "denoise": 1.0}}
    wf["12"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "ddim"}}
    wf["13"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
    wf["14"] = {"class_type": "BasicGuider", "inputs": {"model": ["2", 0], "conditioning": ["10", 0]}}
    wf["15"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["13", 0], "guider": ["14", 0], "sampler": ["12", 0],
            "sigmas": ["11", 0], "latent_image": ["10", 1],
        },
    }
    wf["16"] = {"class_type": "VAEDecode", "inputs": {"samples": ["15", 0], "vae": ["4", 0]}}
    wf["18"] = {
        "class_type": "CreateVideo",
        "inputs": {"images": ["16", 0], "fps": FPS},
    }
    wf["19"] = {
        "class_type": "SaveVideo",
        "inputs": {"video": ["18", 0], "filename_prefix": OUTPUT_PREFIX, "format": "auto", "codec": "auto"},
    }

    return wf


def submit_workflow(workflow, comfyui_url):
    """Submit workflow to ComfyUI and return prompt_id."""
    payload = json.dumps({"prompt": workflow}).encode()
    req = urllib.request.Request(
        f"{comfyui_url}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        prompt_id = data.get("prompt_id")
        log(f"  Workflow queued: {prompt_id}")
        return prompt_id
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:1000]
        log(f"  Submit failed ({e.code}): {body}")
        return None


def wait_for_completion(prompt_id, comfyui_url, timeout=1200):
    """Poll ComfyUI history until the workflow completes or fails."""
    log(f"  Waiting for completion (timeout {timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = urllib.request.urlopen(
                f"{comfyui_url}/history/{prompt_id}", timeout=10
            )
            data = json.loads(resp.read())
            if prompt_id in data:
                entry = data[prompt_id]
                status = entry.get("status", {})
                status_str = status.get("status_str", "")
                if status_str == "success":
                    elapsed = int(time.time() - start)
                    log(f"  Generation complete ({elapsed}s)")
                    return entry
                elif status_str == "error":
                    msgs = status.get("messages", [])
                    error_detail = ""
                    for msg in msgs:
                        if isinstance(msg, (list, tuple)) and len(msg) >= 2:
                            if msg[0] == "execution_error":
                                error_detail = str(msg[1])[:500]
                    log(f"  Generation FAILED: {error_detail or status}")
                    return None
        except Exception:
            pass
        time.sleep(10)

    log(f"  TIMEOUT after {timeout}s")
    return None


def find_output_video(history_entry, comfyui_url):
    """Extract output video path from ComfyUI history entry."""
    outputs = history_entry.get("outputs", {})
    for node_id, node_out in outputs.items():
        # SaveVideo uses "images" key with animated=True
        for key in ("videos", "images"):
            if key in node_out:
                items = node_out[key]
                if not isinstance(items, list):
                    items = [items]
                for v in items:
                    filename = v.get("filename", "")
                    subfolder = v.get("subfolder", "")
                    if filename.endswith((".mp4", ".webm", ".mkv")):
                        return filename, subfolder
    return None, None


def download_output(filename, subfolder, comfyui_url, dest_path):
    """Download generated video from ComfyUI."""
    params = urllib.parse.urlencode({
        "filename": filename,
        "subfolder": subfolder,
        "type": "output",
    })
    url = f"{comfyui_url}/view?{params}"
    try:
        resp = urllib.request.urlopen(url, timeout=120)
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(resp, f)
        size_mb = os.path.getsize(dest_path) / (1024 * 1024)
        log(f"  Downloaded: {dest_path} ({size_mb:.1f}MB)")
        return True
    except Exception as e:
        log(f"  Download failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("shot_dir")
    parser.add_argument("run_dir")
    parser.add_argument("--comfyui-url", default="http://127.0.0.1:8188")
    parser.add_argument("--shot-id", default="")
    parser.add_argument("--duration", type=float, default=0)
    args = parser.parse_args()

    global COMFYUI
    COMFYUI = args.comfyui_url
    shot_dir = args.shot_dir
    run_dir = args.run_dir
    shot_id = args.shot_id or os.path.basename(shot_dir)

    log(f"=== Direct video generation: {shot_id} ===")

    # Read reviewed prompt
    rp_path = os.path.join(shot_dir, "reviewed_prompt.json")
    if not os.path.exists(rp_path):
        log("ERROR: reviewed_prompt.json not found")
        return 1

    try:
        with open(rp_path) as f:
            rp = json.load(f)
    except Exception as e:
        log(f"ERROR: reviewed_prompt.json is not valid JSON ({e})")
        return 1
    h3_mode = rp.get("h3_mode", "ref2va")
    prompt_text = rp.get("final_prompt", "")

    if not prompt_text:
        log("ERROR: empty final_prompt in reviewed_prompt.json")
        return 1

    # Get duration from args or shot_list
    duration_s = args.duration
    if duration_s <= 0:
        sl_path = os.path.join(run_dir, "shot_list.json")
        if os.path.exists(sl_path):
            with open(sl_path) as f:
                sl = json.load(f)
            shot = next((s for s in sl["shots"] if s["shot_id"] == shot_id), None)
            if shot:
                duration_s = shot.get("duration_seconds", 7)
        if duration_s <= 0:
            duration_s = 7

    # Read frame_meta for reference images. frame_meta.json is written by the
    # image-generator agent; a crashed agent leaves raw stdout in it (the
    # 47-byte "[ELIFECYCLE] ..." string), and ref2va does not need it at all —
    # so an unreadable file must degrade to {} instead of raising.
    fm_path = os.path.join(shot_dir, "frame_meta.json")
    frame_meta = {}
    if os.path.exists(fm_path):
        try:
            with open(fm_path) as f:
                frame_meta = json.load(f)
            if not isinstance(frame_meta, dict):
                raise ValueError("not a JSON object")
        except Exception as e:
            log(f"  WARNING: frame_meta.json unreadable ({e}) — continuing without it")
            frame_meta = {}

    # Check for dialogue audio
    dialogue_wav = os.path.join(shot_dir, "dialogue.wav")
    dialogue_name = None
    if os.path.exists(dialogue_wav):
        dialogue_name = upload_image(dialogue_wav, COMFYUI)
        if dialogue_name:
            log(f"  Dialogue audio uploaded for lip sync")

    # Upload reference images and build workflow
    if h3_mode == "ref2va":
        ref_assignments = rp.get("reference_assignments", [])
        video_refs = frame_meta.get("video_references", [])

        ref_paths = []
        for ref in (video_refs or ref_assignments):
            if not isinstance(ref, dict):
                continue
            # A prose-wrapped reviewed_prompt can carry null/empty paths; an
            # empty string would resolve to run_dir itself and then be handed
            # to upload_image() as a directory.
            raw = ref.get("resolved_path") or ref.get("image") or ref.get("declared_path") or ""
            if not isinstance(raw, str) or not raw.strip():
                log(f"  WARNING: ref entry has no usable path — skipped ({ref.get('tag', '?')})")
                continue
            path = raw.strip()
            if not os.path.isabs(path):
                # Pack-relative refs are written as "output/<run>/..." while
                # run_dir already IS output/<run> — drop the leading "output/"
                # (exactly that prefix; lstrip("output/") also eats real chars).
                rel = path[len("output/"):] if path.startswith("output/") else path
                path = os.path.join(run_dir, rel)
            if os.path.isfile(path):
                ref_paths.append(path)
            else:
                log(f"  WARNING: ref image not found: {path}")

        if not ref_paths:
            log("ERROR: no reference images found for ref2va mode")
            return 1

        uploaded = []
        for p in ref_paths[:10]:
            name = upload_image(p, COMFYUI)
            if name:
                uploaded.append(name)

        if not uploaded:
            log("ERROR: failed to upload any reference images")
            return 1

        workflow = build_ref2va_workflow(prompt_text, uploaded, duration_s, dialogue_name)

    elif h3_mode == "fl2va":
        first_frame = _shot_path(shot_dir, frame_meta.get("first_frame"))
        last_frame = _shot_path(shot_dir, frame_meta.get("last_frame"))

        if not first_frame or not last_frame:
            log("ERROR: fl2va requires first_frame and last_frame in frame_meta")
            return 1

        ff_name = upload_image(first_frame, COMFYUI)
        lf_name = upload_image(last_frame, COMFYUI)

        if not ff_name or not lf_name:
            log("ERROR: failed to upload first/last frames")
            return 1

        workflow = build_fl2va_workflow(prompt_text, ff_name, lf_name, duration_s, dialogue_name)

    elif h3_mode in ("i2va", "composited_i2v"):
        composited = os.path.join(shot_dir, "composited_frame.png")
        first_frame = _shot_path(shot_dir, frame_meta.get("first_frame"))
        img_path = composited if os.path.exists(composited) else first_frame

        if not img_path or not os.path.exists(img_path):
            log("ERROR: no image found for i2va mode")
            return 1

        img_name = upload_image(img_path, COMFYUI)
        if not img_name:
            log("ERROR: failed to upload image for i2va")
            return 1

        workflow = build_i2va_workflow(prompt_text, img_name, duration_s, dialogue_name)

    else:
        log(f"ERROR: unsupported h3_mode: {h3_mode}")
        return 1

    # Save workflow for debugging
    wf_path = os.path.join(shot_dir, "comfyui_workflow.json")
    with open(wf_path, "w") as f:
        json.dump(workflow, f, indent=2)

    # Submit to ComfyUI
    prompt_id = submit_workflow(workflow, COMFYUI)
    if not prompt_id:
        log("ERROR: failed to submit workflow to ComfyUI")
        return 1

    # Wait for completion
    result = wait_for_completion(prompt_id, COMFYUI, timeout=1200)
    if not result:
        log("ERROR: video generation failed or timed out")
        return 1

    # Find and download output
    filename, subfolder = find_output_video(result, COMFYUI)
    if not filename:
        log("ERROR: no video in workflow output")
        return 1

    clip_path = os.path.join(shot_dir, "clip.mp4")
    if not download_output(filename, subfolder, COMFYUI, clip_path):
        log("ERROR: failed to download generated video")
        return 1

    log(f"SUCCESS: {clip_path} ({os.path.getsize(clip_path) // 1024}KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
