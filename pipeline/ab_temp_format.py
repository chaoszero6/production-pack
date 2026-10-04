#!/usr/bin/env python3
"""A/B the two VHS intermediate codecs on IDENTICAL frames.

Renders the same chunk twice (video/ProRes vs video/nvenc_h264-mp4), transcodes
both with byte-identical ffmpeg settings, then measures SSIM/PSNR of the NVENC
path against the ProRes path. Answers: does the cheap intermediate cost quality?

Runs alongside a live film pass — it just adds prompts to the ComfyUI queue.
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

from upscale_clip import build_workflow, clear_queue, probe_frames, wait_for  # noqa: E402
from upscale_film import COMFY_OUTPUT, transcode_part  # noqa: E402

SERVER = "http://127.0.0.1:8188"
SOURCE = ("/root/production_pack/claude_output/claude_run_20260929/final/movie.mp4")
CHUNK_IDX = 300          # not yet rendered by the live pass
INTERVALS = 14
INTERMEDIATE_FPS = 120.0
CRF, PRESET, ENCODER = 16, "slow", "x264"
SCRATCH = "/root/production_pack/ab_tempfmt"


def render(fmt: str, prefix: str) -> str:
    """Submit one chunk in `fmt`, return the intermediate file path."""
    start = CHUNK_IDX * INTERVALS
    wf = build_workflow(SOURCE, prefix, start, INTERVALS, CHUNK_IDX,
                        INTERMEDIATE_FPS, fmt)
    t0 = time.time()
    r = requests.post(f"{SERVER}/api/prompt",
                      json={"prompt": wf, "client_id": f"ab-{prefix}"}, timeout=60)
    r.raise_for_status()
    pid = r.json()["prompt_id"]
    outputs = wait_for(requests, SERVER, pid)
    wall = time.time() - t0
    entries = [e for node in outputs.values() for e in node.get("gifs", [])]
    if not entries:
        raise RuntimeError(f"no video output for {fmt}")
    path = entries[0]["fullpath"]
    size = os.path.getsize(path)
    print(f"  {fmt:28s} wall {wall:5.1f}s  intermediate {size/1e6:8.2f} MB")
    return path


def main() -> int:
    os.makedirs(SCRATCH, exist_ok=True)
    # NOTE: deliberately do NOT clear the queue — a film pass may be running and
    # clearing would drop its queued chunk. We only append to the queue.

    print(f"A/B on chunk {CHUNK_IDX} (identical source frames, two intermediate codecs)")
    print("rendering...")
    prores_src = render("video/ProRes", "abprores")
    nvenc_src = render("video/nvenc_h264-mp4", "abnvenc")

    print("transcoding both with identical ffmpeg settings...")
    parts = {}
    for name, src in (("prores", prores_src), ("nvenc", nvenc_src)):
        part = os.path.join(SCRATCH, f"part_{name}.mp4")
        t0 = time.time()
        transcode_part(src, part, CRF, PRESET, ENCODER, None)
        parts[name] = part
        print(f"  {name:6s} part {os.path.getsize(part)/1e6:6.2f} MB  "
              f"({probe_frames(part)}f)  encode {time.time()-t0:.1f}s")

    # SSIM/PSNR: ProRes-derived part is the reference (near-lossless source)
    print("\nquality of NVENC-derived vs ProRes-derived (same frames):")
    for filt, label in (("ssim", "SSIM"), ("psnr", "PSNR")):
        cmd = ["ffmpeg", "-v", "info", "-i", parts["nvenc"], "-i", parts["prores"],
               "-lavfi", f"[0:v][1:v]{filt}", "-f", "null", "-"]
        out = subprocess.run(cmd, capture_output=True, text=True).stderr
        hits = [ln.strip() for ln in out.splitlines()
                if f"{filt.upper()}" in ln.upper() or "average" in ln.lower()]
        for h in hits[-2:]:
            print(f"  {label}: {h[:150]}")

    # detail proxy: high-frequency energy preserved?
    print("\nintermediate bitrates (what the quality difference comes from):")
    for name, src in (("prores", prores_src), ("nvenc", nvenc_src)):
        dur = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", src], capture_output=True, text=True).stdout.strip()
        size = os.path.getsize(src)
        try:
            mbps = size * 8 / float(dur) / 1e6
        except (ValueError, ZeroDivisionError):
            mbps = float("nan")
        print(f"  {name:6s} {size/1e6:8.2f} MB over {dur}s = {mbps:8.1f} Mbps")

    print(f"\nscratch kept for inspection: {SCRATCH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
