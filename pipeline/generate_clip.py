#!/usr/bin/env python3
"""Video generation wrapper — monitors ComfyUI and manages VRAM.

Strategy:
1. Start LLM (for the video-generator agent to plan + queue the workflow)
2. Run the dsh video-generator agent
3. Monitor ComfyUI queue — when a job appears, the agent has queued it
4. Stop LLM to free VRAM for H3 generation
5. Wait for ComfyUI to finish
6. Download output, save as clip.mp4
7. Restart LLM for QA

This solves the VRAM problem: LLM is only loaded during planning,
stopped during the GPU-heavy H3 generation.

Usage:
    python3 generate_clip.py --shot-dir <dir> --run-dir <dir> --comfyui-url <url>
"""
import argparse
import json
import os
import subprocess
import sys
import time
import threading
import requests

COMFYUI = "http://127.0.0.1:8188"
PACK_DIR = "/root/production_pack"
DSH_DIR = "/root/desktop/deepseek-harness"

def log(msg):
    print(f"[gen-clip {time.strftime('%H:%M:%S')}] {msg}", flush=True)

def systemctl(action, service):
    subprocess.run(["systemctl", action, service], capture_output=True)

def stop_llm():
    log("Stopping LLM to free VRAM...")
    systemctl("stop", "qwen3.8-27b-q6k-cuda.service")
    systemctl("stop", "llama-qwen35-122b.service")
    subprocess.run(["docker", "stop", "qwen38-27b-q6k"], capture_output=True)
    time.sleep(2)

def start_llm():
    log("Starting LLM...")
    systemctl("start", "qwen3.8-27b-q6k-cuda.service")
    for i in range(60):
        try:
            r = requests.get("http://127.0.0.1:8085/health", timeout=3)
            if r.status_code == 200:
                log(f"LLM ready ({i+1}s)")
                return True
        except:
            pass
        time.sleep(1)
    log("WARNING: LLM failed to start in 60s")
    return False

def restart_comfyui():
    log("Restarting ComfyUI...")
    systemctl("restart", "comfyui.service")
    for i in range(120):
        try:
            r = requests.get(f"{COMFYUI}/api/system_stats", timeout=3)
            if r.status_code == 200:
                log(f"ComfyUI ready ({i+1}s)")
                return True
        except:
            pass
        time.sleep(1)
    log("ERROR: ComfyUI failed to start")
    return False

def get_queue_state():
    """Check ComfyUI queue."""
    try:
        r = requests.get(f"{COMFYUI}/api/queue", timeout=5)
        d = r.json()
        running = len(d.get("queue_running", []))
        pending = len(d.get("queue_pending", []))
        return running, pending
    except:
        return -1, -1

def wait_for_queue_activity(timeout=300):
    """Wait until a job appears in ComfyUI queue (agent queued something)."""
    log("Monitoring ComfyUI queue for agent-submitted workflow...")
    start = time.time()
    while time.time() - start < timeout:
        running, pending = get_queue_state()
        if running > 0 or pending > 0:
            log(f"Queue active! running={running} pending={pending}")
            return True
        time.sleep(2)
    return False

def wait_for_queue_empty(timeout=600):
    """Wait until ComfyUI queue is empty (generation complete)."""
    log("Waiting for generation to complete...")
    start = time.time()
    last_progress = time.time()
    while time.time() - start < timeout:
        running, pending = get_queue_state()
        if running == 0 and pending == 0:
            # Double check — might be between jobs
            time.sleep(3)
            running2, pending2 = get_queue_state()
            if running2 == 0 and pending2 == 0:
                log("Generation complete (queue empty)")
                return True
        if running > 0:
            last_progress = time.time()
        # If no activity for 5 min, might be stuck
        if time.time() - last_progress > 300:
            log("WARNING: No queue activity for 5 min")
        time.sleep(5)
    log("ERROR: Generation timed out")
    return False

def find_new_outputs(comfyui_output_dir, before_time):
    """Find video files created after before_time."""
    results = []
    for root, dirs, files in os.walk(comfyui_output_dir):
        for f in files:
            path = os.path.join(root, f)
            if f.endswith((".mp4", ".webm")) and os.path.getmtime(path) > before_time:
                results.append(path)
    return sorted(results, key=os.path.getmtime, reverse=True)

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

    # Record time before generation (to find new outputs)
    gen_start_time = time.time()

    # Step 1: Ensure LLM is running for the agent
    start_llm()

    # Step 2: Read the prompt for the agent
    prompt_file = os.path.join(shot_dir, "prompt_video.txt")
    if not os.path.exists(prompt_file):
        # Create it
        with open(prompt_file, "w") as f:
            f.write(f"Generate video clip for {shot_id} via MiniMax H3 in ComfyUI ({COMFYUI}). "
                    f"Read {shot_dir}/reviewed_prompt.json for the H3 prompt and mode (ref2va/i2va/fl2va). "
                    f"Use turbo LoRA: for ref2va use minimax/minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors (8 steps), "
                    f"for fl2va use minimax/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors (4 steps). "
                    f"If dialogue exists, upload {shot_dir}/dialogue.wav as audio reference. "
                    f"Queue the workflow in ComfyUI and WAIT for it to complete. "
                    f"Save clip to {shot_dir}/clip.mp4")

    # Step 3: Start the dsh agent in a background thread
    # The agent will plan and queue the ComfyUI workflow
    agent_process = None
    raw_output = os.path.join(run_dir, "video-generator_raw.txt")

    def run_agent():
        nonlocal agent_process
        prompt_text = open(prompt_file).read()
        env = os.environ.copy()
        env["DSH_PERMISSION_MODE"] = "danger-full-access"

        agent_process = subprocess.Popen(
            ["pnpm", "dsh", "--profile", "headless",
             "--patch", f"{PACK_DIR}/.dsh/.agent-presets/video-generator/agent.cordis.yml",
             prompt_text],
            stdout=open(raw_output, "w"),
            stderr=open(os.path.join(run_dir, "pipeline.log"), "a"),
            cwd=DSH_DIR,
            env=env,
        )
        agent_process.wait()
        log(f"Agent exited with code {agent_process.returncode}")

    agent_thread = threading.Thread(target=run_agent, daemon=True)
    agent_thread.start()
    log("Video-generator agent started in background")

    # Step 4: Monitor ComfyUI queue — when a job appears, stop LLM
    llm_stopped = False
    queue_detected = False

    # Give the agent time to start reasoning
    time.sleep(10)

    for _ in range(600):  # 10 min max
        running, pending = get_queue_state()

        if (running > 0 or pending > 0) and not llm_stopped:
            # Agent has queued a workflow — stop LLM to free VRAM for H3
            log(f"Workflow queued! (running={running}, pending={pending}) — stopping LLM for VRAM")
            stop_llm()
            # Restart ComfyUI to reclaim VRAM
            restart_comfyui()
            llm_stopped = True
            queue_detected = True
            log("LLM stopped, ComfyUI restarted. Full VRAM for H3 generation.")

        if llm_stopped and running == 0 and pending == 0 and queue_detected:
            # Generation might be done — wait a bit to confirm
            time.sleep(5)
            running2, pending2 = get_queue_state()
            if running2 == 0 and pending2 == 0:
                log("Generation complete!")
                break

        # Check if agent finished without queueing
        if agent_process and agent_process.poll() is not None and not queue_detected:
            log(f"Agent exited (code={agent_process.returncode}) without queueing a workflow")
            break

        time.sleep(3)

    # Step 5: Find the generated clip
    comfyui_output = "/opt/comfyui/output"
    new_videos = find_new_outputs(comfyui_output, gen_start_time)

    clip_path = os.path.join(shot_dir, "clip.mp4")
    if new_videos:
        import shutil
        src = new_videos[0]  # most recent
        shutil.copy2(src, clip_path)
        log(f"Clip saved: {clip_path} ({os.path.getsize(clip_path)//1024}KB) from {src}")
    elif os.path.exists(clip_path):
        log(f"Clip already exists at {clip_path}")
    else:
        log("WARNING: No clip.mp4 generated")

    # Step 6: Save generation log
    gen_log = {
        "shot_id": shot_id,
        "status": "completed" if os.path.exists(clip_path) else "failed",
        "queue_detected": queue_detected,
        "llm_stopped_during_gen": llm_stopped,
        "clip_path": clip_path if os.path.exists(clip_path) else None,
        "clip_size": os.path.getsize(clip_path) if os.path.exists(clip_path) else 0,
        "agent_exit_code": agent_process.returncode if agent_process else None,
        "duration_seconds": round(time.time() - gen_start_time),
    }
    with open(os.path.join(shot_dir, "generation_log.json"), "w") as f:
        json.dump(gen_log, f, indent=2)

    # Step 7: Restart LLM for QA
    log("Restarting LLM for QA...")
    restart_comfyui()  # clean VRAM first
    start_llm()

    return 0 if os.path.exists(clip_path) else 1

if __name__ == "__main__":
    sys.exit(main())
