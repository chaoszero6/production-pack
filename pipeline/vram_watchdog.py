#!/usr/bin/env python3
"""VRAM Conflict Detector — background watchdog.

Samples nvidia-smi + service state for a fixed window, logs every reading,
flags moments where two VRAM-heavy services overlap, then (after the window)
calls the local NInfer LLM to analyse the log and emit a verdict.

Usage:
    python3 vram_watchdog.py [--seconds 60] [--log /path/to/log]

Exit codes:
    0  clean (no conflict detected)
    1  conflict detected (two+ VRAM-heavy services overlapped)
    2  LLM analysis failed (raw log still written)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

NINFER_URL = "http://127.0.0.1:8080"

# Services that hold significant GPU VRAM when active.
VRAM_HEAVY_SERVICES = [
    "comfyui.service",
    "ninfer.service",
    "ninfer-us.service",
    "orpheus-backend.service",   # llama.cpp GPU backend (~8GB)
    "chatterbox-tts.service",
    "cosyvoice.service",
]


def log(msg):
    print(f"[vram-watchdog {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def gpu_snapshot():
    """Return (used_mib, total_mib, util_pct) or None on failure."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            timeout=5, text=True,
        )
        parts = out.strip().split(",")
        return int(parts[0].strip()), int(parts[1].strip()), int(parts[2].strip())
    except Exception as e:
        log(f"nvidia-smi failed: {e}")
        return None


def running_services():
    """Return set of active VRAM-heavy service names."""
    try:
        out = subprocess.check_output(
            ["systemctl", "list-units", "--type=service", "--state=running",
             "--no-legend"],
            timeout=5, text=True,
        )
        active = set()
        for line in out.splitlines():
            name = line.split()[0] if line.split() else ""
            if name in VRAM_HEAVY_SERVICES:
                active.add(name)
        return active
    except Exception as e:
        log(f"systemctl query failed: {e}")
        return set()


def sample_once():
    used, total, util = gpu_snapshot()
    svcs = running_services()
    return {
        "ts": time.strftime("%H:%M:%S"),
        "vram_used_mib": used,
        "vram_total_mib": total,
        "gpu_util_pct": util,
        "services": sorted(svcs),
        "conflict": len(svcs) >= 2,
    }


def monitor(seconds, log_path):
    """Sample for `seconds`, append each reading to log_path, return samples."""
    samples = []
    interval = max(2, seconds // 10)  # ~10 samples across the window
    end = time.time() + seconds
    with open(log_path, "a") as f:
        f.write(f"\n=== VRAM watchdog run started {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"(window={seconds}s) ===\n")
        f.flush()
        while time.time() < end:
            s = sample_once()
            samples.append(s)
            flag = "  <-- CONFLICT" if s["conflict"] else ""
            line = (f'{s["ts"]}  {s["vram_used_mib"]:>6}/{s["vram_total_mib"]} MiB '
                    f'({s["gpu_util_pct"]:>2}%)  services={",".join(s["services"]) or "-"}{flag}')
            log(line)
            f.write(line + "\n")
            f.flush()
            time.sleep(interval)
        f.write("=== VRAM watchdog run ended ===\n")
    return samples


def llm_analyse(samples, log_path):
    """Call NInfer to summarise the readings and confirm/deny a conflict."""
    import urllib.request

    conflict_count = sum(1 for s in samples if s["conflict"])
    peak = max((s["vram_used_mib"] for s in samples), default=0)
    prompt = (
        "You are a GPU resource monitor. Below are VRAM samples from an RTX 5090 "
        "(32 GB) collected over a short window. A 'conflict' means two or more "
        "VRAM-heavy services were active at once.\n\n"
        f"Peak VRAM used: {peak} MiB\n"
        f"Samples with conflict: {conflict_count}/{len(samples)}\n\n"
        "Samples:\n"
        + "\n".join(json.dumps(s) for s in samples)
        + "\n\nGive a one-paragraph verdict: was there a real VRAM conflict? Which "
          "services overlapped? Is the system at risk of OOM? Be concise."
    )
    body = json.dumps({"model": "qwen3.8-27b", "messages": [
        {"role": "user", "content": prompt}], "max_tokens": 600}).encode()
    req = urllib.request.Request(
        f"{NINFER_URL}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode())
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log(f"LLM analysis failed: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument(
        "--log",
        default=os.path.join(
            os.environ.get("PACK_DIR")
            or os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "output",
            "vram_watchdog.log",
        ),
    )
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.log), exist_ok=True)
    log(f"Monitoring VRAM for {args.seconds}s -> {args.log}")
    samples = monitor(args.seconds, args.log)

    conflict_count = sum(1 for s in samples if s["conflict"])
    peak = max((s["vram_used_mib"] for s in samples), default=0)
    summary = {
        "window_seconds": args.seconds,
        "samples": len(samples),
        "peak_vram_mib": peak,
        "conflict_samples": conflict_count,
        "conflict_detected": conflict_count > 0,
    }
    with open(args.log, "a") as f:
        f.write("\n--- summary ---\n")
        f.write(json.dumps(summary, indent=2) + "\n")

    log(f"Summary: peak={peak}MiB conflicts={conflict_count}/{len(samples)}")

    # Post-window LLM analysis via NInfer
    verdict = llm_analyse(samples, args.log)
    if verdict:
        log("LLM verdict:")
        log(verdict)
        with open(args.log, "a") as f:
            f.write("\n--- LLM verdict ---\n" + verdict + "\n")
    else:
        log("WARNING: LLM analysis unavailable; raw log retained.")
        sys.exit(2)

    sys.exit(1 if summary["conflict_detected"] else 0)


if __name__ == "__main__":
    main()
