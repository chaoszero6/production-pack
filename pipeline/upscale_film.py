#!/usr/bin/env python3
"""Full-film 4K60 upscale with BOUNDED DISK — RTX VSR + RIFE via ComfyUI.

Why this exists (and why upscale_clip.py cannot be used for a whole film):
  upscale_clip.py keeps every ProRes chunk AND writes an additional concat_120.mov
  before the final encode. At 3840x2160 / 120 fps ProRes that is ~180 GB for a
  10:28 film against ~43 GB free — it would fill the disk and die mid-pass.

This script bounds peak disk to roughly ONE ProRes chunk plus the growing h264
output, by transcoding each chunk to h264 immediately and deleting the ProRes
before moving on:

  per chunk:  VHS_LoadVideoPath -> RTXVideoSuperResolution(4K ULTRA)
                                -> RIFE VFI (rife47.pth, x5) -> ProRes chunk @120fps
              then: ffmpeg -> h264 part @60fps  -> DELETE the ProRes chunk

  per film:   concat parts (stream copy) + mux audio from the source film.

Source is 24 fps; RIFE multipliers are integral so 24 -> 60 is done as 24 -> 120
then decimating to 60. Chunks overlap one source frame; the duplicated frame is
dropped inside the graph (ImageFromBatch), matching upscale_clip.py exactly so
the two stay consistent.

Usage:
  python3 upscale_film.py --source final/movie.mp4 --out final/movie_4k60.mp4
  python3 upscale_film.py ... --max-chunks 2          # smoke test
  python3 upscale_film.py ... --resume                # skip finished parts
Exit: 0 ok, 2 no source, 3 ComfyUI unreachable, 4 chunk/workflow/encode failed.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests  # noqa: E402

from upscale_clip import (  # noqa: E402  (reuse the proven graph builders)
    RIFE_MULT,
    TARGET_FPS,
    WIDTH,
    HEIGHT,
    build_workflow,
    clear_queue,
    probe_frames,
    probe_video,
    wait_for,
)

COMFY_OUTPUT = "/opt/comfyui/ComfyUI/output"
DEFAULT_CHUNK_INTERVALS = 14  # ~0.6 s of source per chunk; bounds VRAM, tuned for 32 GB


def log(msg: str, logfile: str | None = None) -> None:
    line = f"[film4k60 {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if logfile:
        with open(logfile, "a") as handle:
            handle.write(line + "\n")


def free_gb(path: str) -> float:
    st = shutil.disk_usage(path)
    return st.free / (1024 ** 3)


MIN_FREE_VRAM_MIB = 12000  # 4K VSR + RIFE needs headroom; an LLM stealing ~27GB will OOM it


def free_vram_mib() -> int:
    """Free VRAM in MiB, or a large number if nvidia-smi is unavailable."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True)
        return int(out.stdout.strip().splitlines()[0])
    except Exception:
        return 10 ** 6


def wait_for_vram(chunk_idx: int, logfile: str | None) -> None:
    """Block while VRAM is squeezed — e.g. someone started a local LLM mid-pass.

    This is the guard that keeps a ~6h run from dying three hours in: the pass
    waits for the GPU instead of failing the chunk or OOMing ComfyUI.
    """
    warned = False
    while free_vram_mib() < MIN_FREE_VRAM_MIB:
        if not warned:
            log(f"[chunk {chunk_idx:04d}] VRAM low: {free_vram_mib()} MiB free, "
                f"need {MIN_FREE_VRAM_MIB} — waiting (an LLM service may have been "
                f"started)", logfile)
            warned = True
        time.sleep(30)
    if warned:
        log(f"[chunk {chunk_idx:04d}] VRAM recovered: {free_vram_mib()} MiB free", logfile)


def clean_prores(prefix: str, chunk_idx: int) -> int:
    """Delete the ProRes chunk files for this index. Returns bytes reclaimed."""
    reclaimed = 0
    for path in glob.glob(os.path.join(COMFY_OUTPUT, f"{prefix}_{chunk_idx:03d}_*")):
        try:
            reclaimed += os.path.getsize(path)
            os.remove(path)
        except OSError:
            pass
    folder = os.path.join(COMFY_OUTPUT, os.path.dirname(prefix))
    try:
        if os.path.isdir(folder) and not os.listdir(folder):
            os.rmdir(folder)
    except OSError:
        pass
    return reclaimed


def transcode_part(src_prores: str, part_path: str, crf: int, preset: str,
                   encoder: str, threads: int | None = None) -> None:
    """ProRes@120 -> h264/hevc @60 fps, video only (audio muxed at the end)."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", src_prores,
           "-vf", f"fps={TARGET_FPS}", "-an"]
    if encoder == "nvenc":
        cmd += ["-c:v", "hevc_nvenc", "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
    else:
        cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset]
        if threads:
            cmd += ["-threads", str(threads)]
    cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", part_path]
    subprocess.run(cmd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="film to upscale (gets its audio)")
    ap.add_argument("--out", required=True, help="output 4K60 mp4")
    ap.add_argument("--parts-dir", default=None, help="default: <out dir>/parts_4k60")
    ap.add_argument("--comfyui-url", default="http://127.0.0.1:8188")
    ap.add_argument("--chunk-intervals", type=int, default=DEFAULT_CHUNK_INTERVALS)
    ap.add_argument("--crf", type=int, default=16)
    ap.add_argument("--preset", default="slow")
    ap.add_argument("--encoder", choices=["x264", "nvenc"], default="x264")
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--temp-format", default="video/ProRes",
                    help="intermediate codec. KEEP ProRes: measured A/B (chunk 300, "
                         "identical frames) shows video/nvenc_h264-mp4 is SLOWER "
                         "(36.2s vs 25.0s) AND lossy (5.8 vs 2479 Mbps -> PSNR "
                         "40.8dB / SSIM 0.967, parts 1.24 vs 2.08MB)")
    ap.add_argument("--max-chunks", type=int, default=0, help="0 = all (smoke test)")
    ap.add_argument("--force", action="store_true", help="rebuild existing parts")
    args = ap.parse_args()

    server = args.comfyui_url.rstrip("/")
    source = os.path.abspath(args.source)
    out = os.path.abspath(args.out)
    parts_dir = args.parts_dir or os.path.join(os.path.dirname(out), "parts_4k60")
    logfile = out + ".log"
    prefix = f"film4k60_{os.path.splitext(os.path.basename(out))[0]}/chunk"

    if not os.path.isfile(source):
        log(f"ERROR no source {source}", logfile)
        return 2
    os.makedirs(parts_dir, exist_ok=True)

    try:
        requests.get(f"{server}/system_stats", timeout=8).raise_for_status()
    except Exception as exc:
        log(f"ERROR ComfyUI unreachable at {server}: {exc}", logfile)
        return 3

    meta = probe_video(source)
    src_fps, src_frames = meta["fps"], meta["frames"]
    intervals = src_frames - 1
    if intervals < 1:
        log("ERROR source has <2 frames", logfile)
        return 4
    intermediate_fps = src_fps * RIFE_MULT
    ci = max(1, args.chunk_intervals)
    n_chunks_total = (intervals + ci - 1) // ci
    limit = args.max_chunks if args.max_chunks > 0 else n_chunks_total

    log(f"SOURCE {os.path.basename(source)}: {src_frames}f @ {src_fps:.2f}fps "
        f"{meta['width']}x{meta['height']} ({src_frames / src_fps:.2f}s)", logfile)
    log(f"TARGET {WIDTH}x{HEIGHT} @ {TARGET_FPS}fps  (RIFE x{RIFE_MULT} -> "
        f"{intermediate_fps:.0f} -> /2)", logfile)
    log(f"chunks: {n_chunks_total} total x {ci} intervals (running {limit}) | "
        f"free disk: {free_gb(parts_dir):.1f} GB | encoder={args.encoder} "
        f"crf={args.crf} preset={args.preset} temp={args.temp_format}", logfile)
    clear_queue(server)

    parts: list[str] = []
    done_this_run = 0
    pass_start = time.time()
    peak_chunk_bytes = 0
    reclaimed = 0

    for start in range(0, intervals, ci):
        if done_this_run >= limit:
            break
        n = min(ci, intervals - start)
        chunk_idx = start // ci
        part = os.path.join(parts_dir, f"part_{chunk_idx:05d}.mp4")

        # ---- resume: reuse an already-finished part ----
        if os.path.isfile(part) and not args.force:
            try:
                if probe_frames(part) > 0:
                    parts.append(part)
                    log(f"[chunk {chunk_idx:04d}/{n_chunks_total - 1}] REUSE "
                        f"{os.path.basename(part)}", logfile)
                    continue
            except subprocess.CalledProcessError:
                pass

        # ---- 1. GPU: VSR + RIFE -> ProRes (VRAM-guarded, retried) ----
        workflow = build_workflow(source, prefix, start, n, chunk_idx, intermediate_fps,
                                  args.temp_format)
        prores = None
        for attempt in range(1, 4):
            wait_for_vram(chunk_idx, logfile)
            t0 = time.time()
            try:
                resp = requests.post(f"{server}/api/prompt",
                                     json={"prompt": workflow,
                                           "client_id": f"film4k60-{chunk_idx}"},
                                     timeout=60)
                resp.raise_for_status()
                prompt_id = resp.json()["prompt_id"]
                outputs = wait_for(requests, server, prompt_id)
            except Exception as exc:
                log(f"[chunk {chunk_idx:04d}] GPU attempt {attempt}/3 failed: "
                    f"{str(exc)[:300]}", logfile)
                if attempt == 3:
                    log(f"[chunk {chunk_idx:04d}] giving up after 3 attempts "
                        f"(finished parts are kept; rerun to resume)", logfile)
                    return 4
                clear_queue(server)
                time.sleep(30)
                continue
            entries = [e for node in outputs.values() for e in node.get("gifs", [])]
            if not entries:
                log(f"[chunk {chunk_idx:04d}] no video in outputs (attempt "
                    f"{attempt}/3)", logfile)
                if attempt == 3:
                    return 4
                time.sleep(20)
                continue
            prores = entries[0]["fullpath"]
            gpu_s = time.time() - t0
            break
        try:
            prores_bytes = os.path.getsize(prores)
        except OSError:
            prores_bytes = 0
        peak_chunk_bytes = max(peak_chunk_bytes, prores_bytes)

        # ---- 2. encode the chunk to 4K60 h264, then DELETE the ProRes ----
        t1 = time.time()
        try:
            transcode_part(prores, part, args.crf, args.preset,
                           args.encoder, args.threads)
        except subprocess.CalledProcessError as exc:
            log(f"[chunk {chunk_idx:04d}] ENCODE FAILED (ProRes kept for "
                f"inspection): {exc}", logfile)
            return 4
        enc_s = time.time() - t1
        reclaimed += clean_prores(prefix, chunk_idx)

        got = probe_frames(part)
        parts.append(part)
        done_this_run += 1
        elapsed = time.time() - pass_start
        per_chunk = elapsed / done_this_run
        remaining = (limit - done_this_run) * per_chunk
        log(f"[chunk {chunk_idx:04d}/{n_chunks_total - 1}] {got}f out | "
            f"gpu {gpu_s:.0f}s enc {enc_s:.0f}s | temp {prores_bytes / 1e6:.0f}MB "
            f"(deleted) | part {os.path.getsize(part) / 1e6:.1f}MB | "
            f"eta {remaining / 60:.0f}m | free {free_gb(parts_dir):.1f}GB", logfile)

    total_frames = sum(probe_frames(p) for p in parts)
    log(f"all {len(parts)} parts ready: {total_frames} frames @ {TARGET_FPS}fps "
        f"({total_frames / TARGET_FPS:.2f}s) | reclaimed {reclaimed / 1e9:.2f}GB "
        f"ProRes | peak chunk {peak_chunk_bytes / 1e6:.0f}MB", logfile)

    if args.max_chunks > 0:
        log(f"SMOKE TEST: stopping before concat (max-chunks={args.max_chunks})", logfile)
        return 0

    # ---- 3. concat parts + mux the source film's audio ----
    concat_list = os.path.join(parts_dir, "parts.txt")
    with open(concat_list, "w") as handle:
        for path in parts:
            handle.write(f"file '{path}'\n")
    log(f"concatenating {len(parts)} parts + muxing audio from source...", logfile)
    t2 = time.time()
    subprocess.run([
        "ffmpeg", "-y", "-v", "warning",
        "-f", "concat", "-safe", "0", "-i", concat_list,
        "-i", source,
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "copy", "-c:a", "copy",
        "-shortest", "-movflags", "+faststart", out,
    ], check=True)

    v = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries",
                        "stream=width,height,r_frame_rate,nb_frames,codec_name,bit_rate",
                        "-of", "json", out], capture_output=True, text=True, check=True)
    a = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                        "-show_entries", "stream=codec_name,channels,sample_rate",
                        "-of", "json", out], capture_output=True, text=True, check=True)
    d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", out], capture_output=True, text=True, check=True)
    info = {
        "source": source,
        "output": out,
        "method": "gpu_rtx_vsr_rife_bounded_disk",
        "video": json.loads(v.stdout)["streams"],
        "audio": json.loads(a.stdout).get("streams", []),
        "duration_s": float(d.stdout.strip()),
        "size_bytes": os.path.getsize(out),
        "parts": len(parts),
        "source_meta": meta,
        "concat_s": round(time.time() - t2, 1),
        "total_s": round(time.time() - pass_start, 1),
    }
    with open(out + ".json", "w") as handle:
        json.dump(info, handle, indent=2)
        handle.write("\n")
    log(f"DONE {out} — {info['size_bytes'] / 1e9:.2f} GB, "
        f"{info['duration_s']:.2f}s, parts={len(parts)}", logfile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
