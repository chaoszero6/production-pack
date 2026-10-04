#!/usr/bin/env python3
"""GPU 4K60 upscale for one clip via ComfyUI (RTX VSR + RIFE).

Pipeline per temporal chunk (bounds 32 GB VRAM):
  VHS_LoadVideoPath -> RTXVideoSuperResolution (3840x2160, ULTRA)
                   -> RIFE VFI (rife47.pth, x5) -> ProRes chunk @ 120 fps

Source is 24 fps; RIFE multipliers are integral, so 24 -> 60 is done as
24 -> 120 (x5) then selecting every second frame (120 -> 60). Chunks overlap
by one source frame and drop the duplicated first interpolated frame.

Usage:
  python3 upscale_clip.py --shot-dir <clips/SHOT_ID> [--comfyui-url URL]
                          [--force] [--chunk-intervals N]

Exit: 0 ok, 2 no source, 3 ComfyUI unreachable, 4 workflow/run failed.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time

import requests

WIDTH, HEIGHT = 3840, 2160
RIFE_MULT = 5
INTERMEDIATE_FPS_MULT = RIFE_MULT  # src_fps * 5
TARGET_FPS = 60
DEFAULT_CHUNK_INTERVALS = 14


def log(msg: str) -> None:
    print(f"[upscale {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def probe_video(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-count_frames", "-show_entries",
         "stream=width,height,avg_frame_rate,r_frame_rate,nb_read_frames",
         "-of", "json", path],
        capture_output=True, text=True, check=True)
    st = json.loads(out.stdout)["streams"][0]
    fps = eval(st.get("avg_frame_rate") or st.get("r_frame_rate") or "24/1", {"__builtins__": {}})  # noqa: S307
    return {
        "width": int(st["width"]),
        "height": int(st["height"]),
        "fps": float(fps),
        "frames": int(st["nb_read_frames"]),
    }


def probe_frames(path: str) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def wait_for(requests_mod, server: str, prompt_id: str, timeout_s: float = 3600.0) -> dict:
    started = time.time()
    while True:
        if time.time() - started > timeout_s:
            raise RuntimeError(f"prompt {prompt_id} exceeded {timeout_s:.0f}s")
        time.sleep(4)
        record = requests_mod.get(f"{server}/api/history/{prompt_id}", timeout=30).json().get(prompt_id)
        if record is None:
            continue
        status = record.get("status", {})
        if status.get("status_str") == "error" or status.get("completed") is False:
            raise RuntimeError(
                f"prompt {prompt_id} failed:\n{json.dumps(status, indent=2)[:4000]}")
        if record.get("outputs"):
            return record["outputs"]


def build_workflow(source: str, prefix: str, start: int, intervals: int,
                   chunk_idx: int, intermediate_fps: float,
                   temp_format: str = "video/ProRes") -> dict:
    """Build the VSR+RIFE graph writing an intermediate at intermediate_fps.

    temp_format selects the intermediate codec. KEEP 'video/ProRes'.

    Measured A/B on chunk 300 (identical source frames, same 4K60 VSR+RIFE
    graph), recorded so nobody repeats the experiment: 'video/nvenc_h264-mp4'
    is worse on BOTH axes —

      * 36.2s vs 25.0s per chunk (45% SLOWER, not faster)
      * intermediate falls from 2479 Mbps to 5.8 Mbps, and the final CRF16
        encode cannot recover it: PSNR 40.8dB / SSIM 0.967 against the ProRes
        path, final parts 1.24MB vs 2.08MB (~40% less detail retained)

    The ~180MB ProRes write is NOT the bottleneck: GPU sits at 15-26% during
    the pass and each chunk is bound by VSR + the 4K frame round-trip, so
    moving the encode to NVENC buys nothing. ProRes is deleted immediately
    after each chunk transcode, so peak disk stays ~6GB regardless.
    """
    first = chunk_idx == 0
    return {
        "1": {
            "class_type": "VHS_LoadVideoPath",
            "inputs": {
                "video": source,
                "force_rate": 0,
                "custom_width": 0,
                "custom_height": 0,
                "frame_load_cap": intervals + 1,
                "skip_first_frames": start,
                "select_every_nth": 1,
                "format": "None",
            },
        },
        "2": {
            "class_type": "RTXVideoSuperResolution",
            "inputs": {
                "images": ["1", 0],
                "resize_type": "target dimensions",
                "resize_type.width": WIDTH,
                "resize_type.height": HEIGHT,
                "quality": "ULTRA",
            },
        },
        "3": {
            "class_type": "RIFE VFI",
            "inputs": {
                "ckpt_name": "rife47.pth",
                "frames": ["2", 0],
                "clear_cache_after_n_frames": 10,
                "multiplier": RIFE_MULT,
                "fast_mode": True,
                "ensemble": True,
                "scale_factor": 1.0,
            },
        },
        "4": {
            "class_type": "ImageFromBatch",
            "inputs": {
                "image": ["3", 0],
                "batch_index": 0 if first else 1,
                "length": 4096,
            },
        },
        "5": {
            "class_type": "VHS_VideoCombine",
            "inputs": {
                "images": ["4", 0],
                "frame_rate": float(intermediate_fps),
                "loop_count": 0,
                "filename_prefix": f"{prefix}_{chunk_idx:03d}",
                "format": temp_format,
                "profile": "standard",
                "pingpong": False,
                "save_output": True,
            },
        },
    }


def clear_queue(server: str) -> None:
    try:
        requests.post(f"{server}/queue", json={"clear": True}, timeout=30)
        log("ComfyUI queue cleared")
    except Exception as e:
        log(f"WARNING: could not clear queue: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot-dir", required=True)
    ap.add_argument("--comfyui-url", default="http://127.0.0.1:8188")
    ap.add_argument("--force", action="store_true",
                    help="Rebuild even if clip_4k60.mp4 already exists")
    ap.add_argument("--chunk-intervals", type=int, default=DEFAULT_CHUNK_INTERVALS)
    args = ap.parse_args()

    server = args.comfyui_url.rstrip("/")
    clip_dir = args.shot_dir.rstrip("/")
    source = os.path.join(clip_dir, "clip.mp4")
    final = os.path.join(clip_dir, "clip_4k60.mp4")
    shot_id = os.path.basename(clip_dir)
    work = f"/opt/comfyui/ComfyUI/output/{shot_id}_4k60"
    comfy_output = "/opt/comfyui/ComfyUI/output"
    prefix = f"{shot_id}_4k60/chunk"

    if not os.path.isfile(source):
        log(f"ERROR no source {source}")
        return 2
    if os.path.isfile(final) and not args.force:
        log(f"skip: {final} exists (use --force to rebuild)")
        return 0

    try:
        requests.get(f"{server}/system_stats", timeout=5).raise_for_status()
    except Exception as e:
        log(f"ERROR ComfyUI unreachable at {server}: {e}")
        return 3

    meta = probe_video(source)
    src_fps = meta["fps"]
    src_frames = meta["frames"]
    intervals = src_frames - 1
    if intervals < 1:
        log("ERROR source has <2 frames")
        return 4
    intermediate_fps = src_fps * RIFE_MULT
    chunk_intervals = max(1, args.chunk_intervals)

    if abs(src_fps - 24.0) > 0.05:
        log(f"NOTE source fps={src_fps} (pipeline assumes ~24; RIFE x{RIFE_MULT} "
            f"-> {intermediate_fps:.2f} intermediate, decimate to {TARGET_FPS})")

    log(f"{shot_id}: {src_frames}f @ {src_fps:.2f}fps {meta['width']}x{meta['height']}"
        f" -> 4K {TARGET_FPS}fps (RIFE x{RIFE_MULT} -> {intermediate_fps:.0f} then /2)")
    os.makedirs(work, exist_ok=True)
    clear_queue(server)

    chunk_files: list[str] = []
    n_chunks = 0
    for start in range(0, intervals, chunk_intervals):
        n = min(chunk_intervals, intervals - start)
        chunk_idx = len(chunk_files)
        first = chunk_idx == 0
        expected = RIFE_MULT * n + 1 - (0 if first else 1)
        existing = sorted(glob.glob(os.path.join(comfy_output, f"{prefix}_{chunk_idx:03d}_*.mov")))
        reused = False
        if existing and not args.force:
            try:
                if probe_frames(existing[0]) == expected:
                    log(f"[chunk {chunk_idx:03d}] reuse {existing[0]} ({expected} frames)")
                    chunk_files.append(existing[0])
                    reused = True
            except subprocess.CalledProcessError:
                pass
        if reused:
            continue

        workflow = build_workflow(source, prefix, start, n, chunk_idx, intermediate_fps)
        t0 = time.time()
        try:
            resp = requests.post(f"{server}/api/prompt",
                                 json={"prompt": workflow,
                                       "client_id": f"upscale-{shot_id}"},
                                 timeout=60)
            resp.raise_for_status()
            prompt_id = resp.json()["prompt_id"]
        except Exception as e:
            log(f"[chunk {chunk_idx:03d}] queue failed: {e}")
            return 4
        log(f"[chunk {chunk_idx:03d}] start={start} intervals={n} prompt={prompt_id} queued")
        try:
            outputs = wait_for(requests, server, prompt_id)
        except Exception as e:
            log(f"[chunk {chunk_idx:03d}] FAILED: {e}")
            return 4
        entries = [e for node in outputs.values() for e in node.get("gifs", [])]
        if not entries:
            log(f"[chunk {chunk_idx:03d}] no video in outputs")
            return 4
        path = entries[0]["fullpath"]
        got = probe_frames(path)
        if got != expected:
            log(f"[chunk {chunk_idx:03d}] expected {expected} frames, got {got}")
            return 4
        log(f"[chunk {chunk_idx:03d}] done in {time.time() - t0:.0f}s ({got} frames)")
        chunk_files.append(path)
        n_chunks += 1

    total = sum(probe_frames(p) for p in chunk_files)
    log(f"All {len(chunk_files)} chunks ready: {total} frames @ {intermediate_fps:.0f} fps")

    concat_list = os.path.join(work, "concat.txt")
    concat_mov = os.path.join(work, "concat_120.mov")
    with open(concat_list, "w") as handle:
        for path in chunk_files:
            handle.write(f"file '{path}'\n")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", concat_list, "-c", "copy", concat_mov], check=True)

    # 120 fps -> 60 fps (every second frame) and mux original audio.
    subprocess.run([
        "ffmpeg", "-y", "-v", "warning", "-i", concat_mov, "-i", source,
        "-filter_complex", f"[0:v]fps={TARGET_FPS}[v]",
        "-map", "[v]", "-map", "1:a:0?",
        "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-shortest", "-movflags", "+faststart", final,
    ], check=True)

    v = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,r_frame_rate,nb_frames,codec_name",
                        "-of", "json", final], capture_output=True, text=True, check=True)
    a = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                        "-show_entries", "stream=codec_name,channels,sample_rate",
                        "-of", "json", final], capture_output=True, text=True, check=True)
    info = {
        "shot_id": shot_id,
        "method": "gpu_rtx_vsr_rife",
        "video": json.loads(v.stdout)["streams"],
        "audio": json.loads(a.stdout).get("streams", []),
        "size_bytes": os.path.getsize(final),
        "source": {"frames": src_frames, "fps": src_fps},
        "chunks": len(chunk_files),
    }
    with open(os.path.join(clip_dir, "upscale_log.json"), "w") as handle:
        json.dump(info, handle, indent=2)
        handle.write("\n")
    log(f"Wrote {final} ({info['size_bytes']} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
