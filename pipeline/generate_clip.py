#!/usr/bin/env python3
"""Video generation wrapper — manages VRAM and calls generate_video_direct.py.

Flow:
1. Stop LLM to free VRAM for H3 (local mode only)
2. Ensure ComfyUI is running and healthy
3. Run generate_video_direct.py (builds + submits ComfyUI workflow, waits)
4. Restart LLM for QA (local mode only)

Usage:
    python3 generate_clip.py --shot-dir <dir> --run-dir <dir> --comfyui-url <url>
"""
import argparse
import json
import os
import subprocess
import sys
import time
import requests

COMFYUI = "http://127.0.0.1:8188"
PACK_DIR = os.environ.get("PACK_DIR") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

def log(msg):
    print(f"[gen-clip {time.strftime('%H:%M:%S')}] {msg}", flush=True)

def systemctl(action, service):
    subprocess.run(["systemctl", action, service], capture_output=True)

# No local LLM (2026-10-18): the GPU belongs to ComfyUI during the whole clip
# loop, so there is nothing to park before H3 and nothing to restart after it.
# Stale TTS engines are stopped so H3 gets the full card.
def stop_tts_engines():
    for svc in ("orpheus-tts.service", "orpheus-backend.service", "chatterbox-tts.service",
                "kokoro-tts.service", "cosyvoice.service", "tts-cluster.service"):
        systemctl("stop", svc)


def ensure_comfyui(comfyui_url):
    """Make sure ComfyUI is running and healthy."""
    try:
        r = requests.get(f"{comfyui_url}/api/system_stats", timeout=5)
        if r.status_code == 200:
            log("ComfyUI healthy")
            return True
    except:
        pass
    log("ComfyUI not responding, restarting...")
    systemctl("restart", "comfyui.service")
    for i in range(180):
        try:
            r = requests.get(f"{comfyui_url}/api/system_stats", timeout=3)
            if r.status_code == 200:
                log(f"ComfyUI ready ({i+1}s)")
                return True
        except:
            pass
        time.sleep(1)
    log("ERROR: ComfyUI failed to start")
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shot-dir", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--comfyui-url", default="http://127.0.0.1:8188")
    parser.add_argument("--shot-id", default="")
    args = parser.parse_args()

    global COMFYUI
    COMFYUI = args.comfyui_url
    shot_dir = args.shot_dir
    run_dir = args.run_dir
    shot_id = args.shot_id or os.path.basename(shot_dir)

    log(f"=== Generating clip: {shot_id} ===")
    gen_start_time = time.time()

    # Step 1: make sure no TTS engine holds VRAM (no local LLM exists any more)
    stop_tts_engines()

    # Step 2: Ensure ComfyUI is running
    if not ensure_comfyui(COMFYUI):
        log("ERROR: Cannot start ComfyUI")
        return 1

    # Step 3: Run direct video generation
    script = os.path.join(PACK_DIR, "pipeline", "generate_video_direct.py")
    cmd = [
        sys.executable, script,
        shot_dir, run_dir,
        "--comfyui-url", COMFYUI,
        "--shot-id", shot_id,
    ]
    log(f"Running generate_video_direct.py...")
    result = subprocess.run(cmd, timeout=1500)
    video_ok = result.returncode == 0

    clip_path = os.path.join(shot_dir, "clip.mp4")
    if video_ok and os.path.exists(clip_path):
        log(f"Clip ready: {clip_path} ({os.path.getsize(clip_path)//1024}KB)")
    else:
        log("WARNING: No clip.mp4 generated")

    # Step 4: Save generation log
    gen_log = {
        "shot_id": shot_id,
        "status": "completed" if os.path.exists(clip_path) else "failed",
        "method": "direct_comfyui_api",
        "clip_path": clip_path if os.path.exists(clip_path) else None,
        "clip_size": os.path.getsize(clip_path) if os.path.exists(clip_path) else 0,
        "video_script_exit_code": result.returncode,
        "duration_seconds": round(time.time() - gen_start_time),
    }
    with open(os.path.join(shot_dir, "generation_log.json"), "w") as f:
        json.dump(gen_log, f, indent=2)

    # Step 5: nothing to restart — QA runs in the cloud; ComfyUI keeps the GPU.
    return 0 if os.path.exists(clip_path) else 1

if __name__ == "__main__":
    sys.exit(main())
