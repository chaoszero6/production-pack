#!/usr/bin/env python3
"""Video generation wrapper — monitors ComfyUI and manages VRAM.

Strategy (CLOUD_ROUTING=1 — current):
1. Route the video-generator agent explicitly (pipeline/set_agent_model.py)
2. Run the dsh video-generator agent (OpenRouter cloud — no local LLM)
3. Monitor ComfyUI queue — when a job appears, the agent has queued it
4. Wait for ComfyUI to finish   (no LLM to stop; full 5090 goes to H3)
5. Download output, save as clip.mp4

Strategy (CLOUD_ROUTING=0 — retired local layout):
1. Start LLM (for the video-generator agent to plan + queue the workflow)
2. Run the dsh video-generator agent
3. Monitor ComfyUI queue — when a job appears, the agent has queued it
4. Stop LLM to free VRAM for H3 generation
5. Wait for ComfyUI to finish
6. Download output, save as clip.mp4
7. Restart LLM for QA

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

# ── Cloud routing (OpenRouter) ──────────────────────────────
# With CLOUD_ROUTING=1 every agent runs on the OpenRouter cloud, so no local
# LLM exists to start or stop. The old juggle was also actively harmful:
# stop_llm() fires the moment a ComfyUI workflow appears in the queue — i.e.
# while the video-generator agent is still reasoning — which killed that
# agent's own LLM request (console: "Agent exited with code 1", every clip).
# Under cloud routing the agent's request is remote and survives generation.
# Set CLOUD_ROUTING=0 for the retired local layout.
CLOUD_ROUTING = os.environ.get("CLOUD_ROUTING", "1") == "1"


def set_agent_route(preset):
    """Point agent-default-model at this preset's model before launching dsh.

    This agent is started from a subprocess, and dsh reads the route from
    settings.yaml at launch — so without this it silently inherits whatever
    route the *previous* agent call left behind.
    """
    script = os.path.join(PACK_DIR, "pipeline", "set_agent_model.py")
    try:
        r = subprocess.run([sys.executable, script, preset],
                           capture_output=True, text=True, check=True)
        log(f"Model route: {r.stdout.strip()}")
    except subprocess.CalledProcessError as e:
        log(f"WARNING: could not set model route for {preset}: "
            f"{(e.stderr or e.stdout or '').strip()[:200]}")
        return False
    return True


ALL_LLM_SERVICES = [
    "qwen3.8-27b-q6k-cuda.service",
    "llama-qwen35-122b.service",
    "ninfer.service",
    "ninfer-us.service",
]
LOCAL_HEALTH_ENDPOINTS = [
    ("ninfer.service",    "http://127.0.0.1:8080/health"),
    ("ninfer-us.service", "http://127.0.0.1:8081/health"),
    ("qwen3.8-27b-q6k-cuda.service", "http://127.0.0.1:8085/health"),
]


def stop_llm():
    if CLOUD_ROUTING:
        return
    log("Stopping ALL LLM services to free VRAM...")
    for svc in ALL_LLM_SERVICES:
        systemctl("stop", svc)
    subprocess.run(["docker", "stop", "qwen38-27b-q6k"], capture_output=True)
    time.sleep(2)


def start_llm():
    if CLOUD_ROUTING:
        return True
    # Check if any local LLM is already healthy
    for svc, url in LOCAL_HEALTH_ENDPOINTS:
        try:
            r = requests.get(url, timeout=3)
            if r.status_code == 200:
                log(f"LLM already running: {svc}")
                return True
        except:
            pass
    # None healthy — restart ComfyUI first to free VRAM, then start preferred
    log("Starting local LLM (restarting ComfyUI first to free VRAM)...")
    systemctl("restart", "comfyui.service")
    time.sleep(5)
    # Try ninfer-us first (it's the primary fallback), then ninfer, then q6k
    for svc, url in LOCAL_HEALTH_ENDPOINTS:
        systemctl("start", svc)
        for i in range(120):
            try:
                r = requests.get(url, timeout=3)
                if r.status_code == 200:
                    log(f"LLM ready ({i+1}s): {svc}")
                    return True
            except:
                pass
            time.sleep(1)
        log(f"{svc} didn't start, trying next...")
        systemctl("stop", svc)
    log("WARNING: No local LLM started in time")
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

    # Step 1: Ensure LLM is running for the agent (no-op under cloud routing)
    start_llm()

    # Step 1b: Route this agent explicitly — dsh resolves the model from
    # settings.yaml at launch, so a subprocess would otherwise inherit the
    # previous agent call's route.
    set_agent_route("video-generator")

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
            # DO NOT restart ComfyUI — it would kill the running workflow!
            # Just stop the LLM to free ~20GB VRAM for H3 model loading.
            log(f"Workflow queued! (running={running}, pending={pending}) — stopping LLM for VRAM")
            stop_llm()
            llm_stopped = True
            queue_detected = True
            log("LLM stopped. ComfyUI keeps running with more VRAM available for H3.")

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

    # Step 5: Find the generated clip. ComfyUI's real output dir is
    # /opt/comfyui/ComfyUI/output (where the agent's own script reads from);
    # /opt/comfyui/output is an empty dir, so watching it misses every clip.
    comfyui_output = "/opt/comfyui/ComfyUI/output"
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
    # Under cloud routing QA is remote and needs no VRAM, so neither the
    # ComfyUI restart (was: "clean VRAM first") nor the LLM start is needed.
    if CLOUD_ROUTING:
        log("Cloud routing: skipping ComfyUI restart + LLM start (QA is remote)")
    else:
        log("Restarting LLM for QA...")
        restart_comfyui()  # clean VRAM first
        start_llm()

    return 0 if os.path.exists(clip_path) else 1

if __name__ == "__main__":
    sys.exit(main())
