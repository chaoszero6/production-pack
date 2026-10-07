#!/usr/bin/env python3
"""Full-film 4K60 upscale with BOUNDED DISK — RTX VSR + RIFE via ComfyUI — CUT-AWARE.

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

CUT-AWARENESS (added 2026-10-07 after *The Clockwork Moth* review — "each cut had a
small dissolve in it, likely frame interpolation occurring on the finished cut"):
  RIFE interpolates between every pair of consecutive source frames. If a chunk
  spans a cut, the last frame of shot A and the first frame of shot B get FOUR
  blended frames between them = a visible micro-dissolve on every edit.
  The pass therefore needs the cut list (source frame indices where a new shot
  starts), given as --concat-list (the ffmpeg concat file assembly used; frame
  counts are probed) or --cuts (JSON list). Chunks never cross a cut: each shot
  is interpolated on its own, its last frame is HELD for the 4 missing
  sub-frames (tpad clone), and 120->60 decimation uses a GLOBAL parity so the
  film keeps exactly ceil(5*F/2) frames — no drift at the cuts.
  Running without a cut list requires --no-cuts and is only correct for a
  single-shot source.

Source is 24 fps; RIFE multipliers are integral so 24 -> 60 is done as 24 -> 120
then decimating to 60. Chunks within a shot overlap one source frame; the
duplicated frame is dropped inside the graph (ImageFromBatch), matching
upscale_clip.py exactly so the two stay consistent.

Usage:
  python3 upscale_film.py --source final/movie.mp4 --out final/movie_4k60.mp4 \
                          --concat-list final/concat.txt
  python3 upscale_film.py ... --cuts final/cuts.json        # [0, 192, 311, ...]
  python3 upscale_film.py ... --max-chunks 2                # smoke test
  python3 upscale_film.py ... --resume                      # skip finished parts
Exit: 0 ok, 2 no source / no cut list, 3 ComfyUI unreachable, 4 chunk/workflow/encode failed.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
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


# ---------------------------------------------------------------------------
# Cut list + chunk planning (pure functions — unit-testable without a GPU)
# ---------------------------------------------------------------------------

def cuts_from_concat_list(concat_path: str) -> list[int]:
    """Source frame index at which each shot starts, derived from an ffmpeg concat
    file (`file '<path>'` lines). Frame counts are probed; relative paths resolve
    against the concat file's directory."""
    base = os.path.dirname(os.path.abspath(concat_path))
    cuts, pos = [], 0
    with open(concat_path, encoding="utf-8") as handle:
        for raw in handle:
            m = re.match(r"\s*file\s+'(.*)'\s*$", raw) or re.match(r"\s*file\s+(\S+)\s*$", raw)
            if not m:
                continue
            path = m.group(1)
            if not os.path.isabs(path):
                path = os.path.join(base, path)
            cuts.append(pos)
            pos += probe_frames(path)
    return cuts


def load_cuts(path: str) -> list[int]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        data = data.get("cuts", data.get("boundaries"))
    return sorted({int(x) for x in data})


def segments_from_cuts(cuts: list[int], total_frames: int) -> list[tuple[int, int]]:
    """[(start_frame, end_frame_exclusive), ...] covering the whole source."""
    pts = sorted({c for c in cuts if 0 < c < total_frames})
    bounds = [0] + pts + [total_frames]
    return [(a, b) for a, b in zip(bounds, bounds[1:]) if b > a]


def plan_chunks(segments: list[tuple[int, int]], chunk_intervals: int) -> list[dict]:
    """Split every segment into RIFE chunks that never cross a segment boundary.

    Each chunk dict: start (source frame), n (intervals), first (first chunk of its
    segment -> keep RIFE frame 0), last (last chunk of its segment -> hold the final
    frame 4 extra sub-frames), seg (segment index), idx (global chunk index).
    A one-frame segment (b - a == 1) cannot be interpolated; it is emitted as a
    chunk with n=0 and handled as a still (5 clones of that frame).
    """
    plan: list[dict] = []
    ci = max(1, chunk_intervals)
    for seg_i, (a, b) in enumerate(segments):
        intervals = b - a - 1
        if intervals <= 0:
            plan.append({"start": a, "n": 0, "first": True, "last": True,
                         "seg": seg_i, "idx": len(plan)})
            continue
        for start in range(a, a + intervals, ci):
            n = min(ci, a + intervals - start)
            plan.append({"start": start, "n": n, "first": start == a,
                         "last": start + n == a + intervals,
                         "seg": seg_i, "idx": len(plan)})
    return plan


def chunk_j_range(chunk: dict) -> tuple[int, int]:
    """Inclusive range of GLOBAL 120fps indices j = 5*src_frame + sub produced by the
    chunk after the in-graph overlap drop and the tail hold."""
    s, n = chunk["start"], chunk["n"]
    if n == 0:  # still frame: 5 clones
        return 5 * s, 5 * s + RIFE_MULT - 1
    j_lo = RIFE_MULT * s + (0 if chunk["first"] else 1)
    j_hi = RIFE_MULT * (s + n) + (RIFE_MULT - 1 if chunk["last"] else 0)
    return j_lo, j_hi


def expected_prores_frames(chunk: dict) -> int:
    if chunk["n"] == 0:
        return 1
    return RIFE_MULT * chunk["n"] + 1 - (0 if chunk["first"] else 1)


def expected_part_frames(chunk: dict) -> int:
    """Frames the 60fps part must contain: the even global j in the chunk's range."""
    j_lo, j_hi = chunk_j_range(chunk)
    return (j_hi // 2) - ((j_lo - 1) // 2)


def total_target_frames(total_src_frames: int) -> int:
    return (RIFE_MULT * total_src_frames + 1) // 2  # ceil(5F/2)


# ---------------------------------------------------------------------------
# ComfyUI graph + per-part transcode
# ---------------------------------------------------------------------------

def build_chunk_workflow(source: str, prefix: str, chunk: dict, intermediate_fps: float,
                         temp_format: str) -> dict:
    """Reuse upscale_clip.build_workflow but decide the overlap drop from the chunk's
    place in ITS SEGMENT (not from chunk_idx == 0): the first chunk after a cut keeps
    RIFE frame 0 because there is no overlap with the previous shot."""
    if chunk["n"] == 0:
        # Still: load one frame, VSR it, no RIFE. Build the normal graph and bypass RIFE.
        wf = build_workflow(source, prefix, chunk["start"], 1, chunk["idx"], intermediate_fps,
                            temp_format)
        wf["1"]["inputs"]["frame_load_cap"] = 1
        wf["4"]["inputs"]["image"] = ["2", 0]
        wf["4"]["inputs"]["batch_index"] = 0
        wf.pop("3", None)
        return wf
    wf = build_workflow(source, prefix, chunk["start"], chunk["n"], chunk["idx"],
                        intermediate_fps, temp_format)
    wf["4"]["inputs"]["batch_index"] = 0 if chunk["first"] else 1
    return wf


def transcode_part(src_prores: str, part_path: str, crf: int, preset: str,
                   encoder: str, chunk: dict, threads: int | None = None) -> None:
    """ProRes@120 -> h264/hevc @60 fps, video only (audio muxed at the end).

    Decimation is `select` on the GLOBAL sub-frame index (not `fps=60`), so parity
    is identical across parts and shots and the film never drifts. The last chunk
    of a shot clones its final frame 4x first (the sub-frames RIFE could not make
    because the next frame belongs to another shot). A one-frame shot is 5 clones.
    """
    j_lo, _ = chunk_j_range(chunk)
    filters = []
    if chunk["n"] == 0:
        filters.append(f"tpad=stop={RIFE_MULT - 1}:stop_mode=clone")
    elif chunk["last"]:
        filters.append(f"tpad=stop={RIFE_MULT - 1}:stop_mode=clone")
    filters.append(f"select='not(mod(n+{j_lo % 2},2))'")
    filters.append(f"setpts=N/({TARGET_FPS}*TB)")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", src_prores,
           "-vf", ",".join(filters), "-r", str(TARGET_FPS), "-an"]
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
    cuts = ap.add_mutually_exclusive_group()
    cuts.add_argument("--concat-list", default=None,
                      help="ffmpeg concat file the source was assembled from; shot "
                           "boundaries are derived by probing each listed file")
    cuts.add_argument("--cuts", default=None,
                      help="JSON list (or {'cuts': [...]}) of source frame indices where "
                           "a new shot begins")
    cuts.add_argument("--no-cuts", action="store_true",
                      help="source is ONE continuous shot (interpolating across a cut "
                           "makes a dissolve at every edit — never use this on a film)")
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
    if not (args.concat_list or args.cuts or args.no_cuts):
        log("ERROR a cut list is required (--concat-list final/concat.txt or --cuts "
            "cuts.json). RIFE across a cut blends the last frame of one shot into the "
            "first frame of the next = a dissolve on every edit (The Clockwork Moth, "
            "2026-10). Pass --no-cuts ONLY for a single-shot source.", logfile)
        return 2
    os.makedirs(parts_dir, exist_ok=True)

    try:
        requests.get(f"{server}/system_stats", timeout=8).raise_for_status()
    except Exception as exc:
        log(f"ERROR ComfyUI unreachable at {server}: {exc}", logfile)
        return 3

    meta = probe_video(source)
    src_fps, src_frames = meta["fps"], meta["frames"]
    if src_frames < 2:
        log("ERROR source has <2 frames", logfile)
        return 4
    intermediate_fps = src_fps * RIFE_MULT

    if args.concat_list:
        cut_list = cuts_from_concat_list(args.concat_list)
        probed_total = None
        try:
            probed_total = cut_list and sum(
                probe_frames(os.path.join(os.path.dirname(os.path.abspath(args.concat_list)), p)
                             if not os.path.isabs(p) else p)
                for p in re.findall(r"file\s+'(.*)'", open(args.concat_list, encoding="utf-8").read()))
        except Exception:
            pass
        if probed_total and probed_total != src_frames:
            log(f"WARNING concat list totals {probed_total} frames but source has {src_frames}; "
                f"boundaries may be off — verify final/concat.txt matches the source", logfile)
    elif args.cuts:
        cut_list = load_cuts(args.cuts)
    else:
        cut_list = []
    segments = segments_from_cuts(cut_list, src_frames)
    plan = plan_chunks(segments, args.chunk_intervals)
    n_chunks_total = len(plan)
    limit = args.max_chunks if args.max_chunks > 0 else n_chunks_total

    plan_path = os.path.join(parts_dir, "plan.json")
    plan_doc = {"source": source, "frames": src_frames, "cuts": segments and [a for a, _ in segments],
                "chunk_intervals": args.chunk_intervals, "chunks": plan}
    if os.path.isfile(plan_path) and not args.force:
        try:
            old = json.load(open(plan_path, encoding="utf-8"))
            if (old.get("cuts"), old.get("chunk_intervals"), old.get("frames")) != \
                    (plan_doc["cuts"], plan_doc["chunk_intervals"], plan_doc["frames"]):
                log("ERROR existing parts were planned with a different cut list / chunk size; "
                    "pass --force to rebuild them", logfile)
                return 4
        except (OSError, ValueError):
            pass
    with open(plan_path, "w", encoding="utf-8") as handle:
        json.dump(plan_doc, handle, indent=1)

    log(f"SOURCE {os.path.basename(source)}: {src_frames}f @ {src_fps:.2f}fps "
        f"{meta['width']}x{meta['height']} ({src_frames / src_fps:.2f}s)", logfile)
    log(f"TARGET {WIDTH}x{HEIGHT} @ {TARGET_FPS}fps  (RIFE x{RIFE_MULT} -> "
        f"{intermediate_fps:.0f} -> /2) -> {total_target_frames(src_frames)} frames", logfile)
    log(f"cuts: {len(segments)} shots (interpolation never crosses a cut) | chunks: "
        f"{n_chunks_total} x <= {args.chunk_intervals} intervals (running {limit}) | "
        f"free disk: {free_gb(parts_dir):.1f} GB | encoder={args.encoder} "
        f"crf={args.crf} preset={args.preset} temp={args.temp_format}", logfile)
    clear_queue(server)

    parts: list[str] = []
    done_this_run = 0
    pass_start = time.time()
    peak_chunk_bytes = 0
    reclaimed = 0

    for chunk in plan:
        if done_this_run >= limit:
            break
        chunk_idx = chunk["idx"]
        part = os.path.join(parts_dir, f"part_{chunk_idx:05d}.mp4")
        want = expected_part_frames(chunk)

        # ---- resume: reuse an already-finished part ----
        if os.path.isfile(part) and not args.force:
            try:
                if probe_frames(part) == want:
                    parts.append(part)
                    log(f"[chunk {chunk_idx:04d}/{n_chunks_total - 1}] REUSE "
                        f"{os.path.basename(part)}", logfile)
                    continue
                log(f"[chunk {chunk_idx:04d}] existing part has wrong frame count, "
                    f"rebuilding", logfile)
            except subprocess.CalledProcessError:
                pass

        # ---- 1. GPU: VSR + RIFE -> ProRes (VRAM-guarded, retried) ----
        workflow = build_chunk_workflow(source, prefix, chunk, intermediate_fps,
                                        args.temp_format)
        prores = None
        gpu_s = 0.0
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
            got_prores = probe_frames(prores)
            if got_prores != expected_prores_frames(chunk):
                log(f"[chunk {chunk_idx:04d}] ProRes has {got_prores} frames, expected "
                    f"{expected_prores_frames(chunk)} (start={chunk['start']} n={chunk['n']} "
                    f"first={chunk['first']})", logfile)
                return 4
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
                           args.encoder, chunk, args.threads)
        except subprocess.CalledProcessError as exc:
            log(f"[chunk {chunk_idx:04d}] ENCODE FAILED (ProRes kept for "
                f"inspection): {exc}", logfile)
            return 4
        enc_s = time.time() - t1
        got = probe_frames(part)
        if got != want:
            log(f"[chunk {chunk_idx:04d}] part has {got} frames, expected {want} — "
                f"frame accounting is off, refusing to continue (ProRes kept)", logfile)
            return 4
        reclaimed += clean_prores(prefix, chunk_idx)

        parts.append(part)
        done_this_run += 1
        elapsed = time.time() - pass_start
        per_chunk = elapsed / done_this_run
        remaining = (limit - done_this_run) * per_chunk
        log(f"[chunk {chunk_idx:04d}/{n_chunks_total - 1}] shot {chunk['seg']} "
            f"{'HEAD ' if chunk['first'] else ''}{'TAIL ' if chunk['last'] else ''}| {got}f out | "
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

    if total_frames != total_target_frames(src_frames):
        log(f"ERROR assembled {total_frames} frames, expected "
            f"{total_target_frames(src_frames)} — refusing to mux (would drift)", logfile)
        return 4

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
        "method": "gpu_rtx_vsr_rife_bounded_disk_cut_aware",
        "video": json.loads(v.stdout)["streams"],
        "audio": json.loads(a.stdout).get("streams", []),
        "duration_s": float(d.stdout.strip()),
        "size_bytes": os.path.getsize(out),
        "parts": len(parts),
        "shots": len(segments),
        "cuts": [a for a, _ in segments][1:],
        "source_meta": meta,
        "concat_s": round(time.time() - t2, 1),
        "total_s": round(time.time() - pass_start, 1),
    }
    with open(out + ".json", "w", encoding="utf-8") as handle:
        json.dump(info, handle, indent=2)
    log(f"DONE {out} ({info['size_bytes'] / 1e9:.2f} GB, {info['duration_s']:.2f}s, "
        f"{len(segments)} shots, cut-aware)", logfile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
