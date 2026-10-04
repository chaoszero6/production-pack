# Phase-Based Service Management

VRAM-safe orchestration for the RTX 5090 (32 GB) in the Production Pack.

## Problem Statement

The production pipeline runs many GPU-consuming services on a single RTX 5090 with 32 GB VRAM:

| Service | Peak VRAM | When Active |
|---------|-----------|-------------|
| `ninfer` | ~28 GB | Agent reasoning (local mode) |
| `ninfer-us` | ~31 GB | Agent reasoning (local mode, ablated variant) |
| `qwen3.8-27b-q6k-cuda` | ~27 GB | Legacy local LLM |
| ComfyUI + Qwen Image 2.1 | ~10 GB | Character/location reference generation |
| ComfyUI + MiniMax H3 | ~12–14 GB | Video clip generation |
| ComfyUI + upscale model | ~14 GB | 4K upscaling |
| Orpheus 3B TTS | ~8 GB | Emotional dialogue synthesis |
| Chatterbox/CosyVoice/Kokoro | CPU-only | Primary/multilingual/narration TTS |

**The constraint:** 28 GB (LLM) + 12 GB (H3) = 40 GB > 32 GB → **OOM crash.** Any combination that exceeds 32 GB will fail.

## Solution: Four Mutually-Exclusive Phases

Each operating state defines exactly which services may coexist:

```
┌─────────────────┬──────────────┬───────────────┬──────────────────┐
│    PHASE        │   GPU Load   │  Services ON  │  Estimated VRAM  │
├─────────────────┼──────────────┼───────────────┼──────────────────┤
│ thinking        │ LLM only     │ ninfer        │ 28 GB            │
│                 │              │ (cloud: none) │                  │
├─────────────────┼──────────────┼───────────────┼──────────────────┤
│ video_gen       │ ComfyUI      │ comfyui       │ 10–14 GB         │
│                 │ generation   │ (all other    │                  │
│                 │              │  stopped)     │                  │
├─────────────────┼──────────────┼───────────────┼──────────────────┤
│ audio_gen       │ TTS cluster  │ chatterbox    │ 8 GB             │
│                 │              │ cosyvoice     │ (Orpheus GPU)    │
│                 │              │ orpheus       │ (+ CPU TTS)      │
│                 │              │ kokoro        │                  │
├─────────────────┼──────────────┼───────────────┼──────────────────┤
│ monitoring      │ Minimal      │ ninfer/light  │ 14–28 GB         │
│                 │ health probe │               │                  │
└─────────────────┴──────────────┴───────────────┴──────────────────┘
```

### Phase Transition Flow

Every transition is atomic: **stop-all → start-new-services → verify-health**.

```
Before transition          During stop window         After transition
─────────────────          ──────────────────         ──────────────────
[ComfyUI running]          All services stopping      [ComfyUI running]
[NInfer running]           ~2 second gap              [All others stopped]
[TTS engines running]                           ←──  ←───────────────────────✓
                                                         Clean VRAM state
```

## Files

| File | Purpose |
|------|---------|
| `pipeline/phase_config.yaml` | Declarative definitions of all services, phases, VRAM budgets, health endpoints |
| `pipeline/services.sh` | Service lifecycle manager — transitions, health checks, status reporting |
| `pipeline/vram_monitor.sh` | Real-time VRAM tracker with alert thresholds and conflict detection |
| `pipeline/run_dsh_agent.sh` | Standalone agent launcher with preset→phase mapping |
| `run.sh` | Main pipeline — integrates via `run_dsh_agent()` → `_run_dsh_agent_with_phase()` |

## How It Works

### 1. Preset → Phase Mapping

Every agent preset has a required phase:

```bash
# In run.sh (or run_dsh_agent.sh)
declare -A _AGENT_PHASE=(
    # Reasoning agents → THINKING phase
    ["story-creator"]="thinking"
    ["director"]="thinking"
    ["screenplay-reviewer"]="thinking"
    ["qa-inspector"]="thinking"

    # Generation agents → VIDEO_GEN phase
    ["character-designer"]="video_gen"
    ["location-designer"]="video_gen"
    ["image-generator"]="video_gen"
    ["video-generator"]="video_gen"
    ["storyboard-artist"]="video_gen"

    # Audio agents → AUDIO_GEN phase
    ["audio-producer"]="audio_gen"
    ["music-composer"]="audio_gen"

    # Post-production → MONITORING phase
    ["subtitle-generator"]="monitoring"
    ["post-production-editor"]="monitoring"
)
```

### 2. Service Transition Pipeline

When `run_dsh_agent("video-generator", ...)` is called:

```
run_dsh_agent()
  → _run_dsh_agent_with_phase("video-generator")
    → detect phase = "video_gen"
    → stop_all_llms_for_generation()
        systemctl stop ninfer.service
        systemctl stop ninfer-us.service
        systemctl stop qwen3.8-27b-q6k-cuda.service
        docker stop qwen38-27b-q6k
        systemctl stop orpheus-tts.service
        systemctl restart comfyui.service  (clears VRAM cache)
    → ensure_comfyui()
        systemctl start/restart comfyui.service
        wait until :8188/api/system_stats responds
    → VRAM pre-check (optional vram_check_now)
    → _run_dsh_agent_internal("video-generator", ...)
        pnpm dsh --profile headless --patch agent.cordis.yml ...
```

### 3. Cloud Routing Shortcut

Under `CLOUD_ROUTING=1` (the default):

```
run_dsh_agent()
  → _run_dsh_agent_with_phase()
    → CLOUD_ROUTING == 1 && phase != video_gen
    → SKIP all service management (agents use OpenRouter cloud)
    → Just ensure_comfyui() if phase == video_gen
    → Directly call _run_dsh_agent_internal()
```

This means under cloud routing, no local LLM ever loads, leaving full 32 GB for ComfyUI during generation.

## Usage

### Interactive / Debugging

```bash
# Source everything
source pipeline/services.sh
source pipeline/vram_monitor.sh

# View current status
svc_status

# List running services
svc_list_running

# Force a phase transition
svc_transition video_gen
svc_transition audio_gen
svc_transition thinking
svc_transition monitoring

# VRAM monitoring
vram_report          # one-shot detailed report
vram_check           # quick check, exit code = severity (0=OK, 1=warn, 2=critical)
vram_watch           # continuous monitoring loop
vram list-procs      # show CUDA compute processes consuming VRAM
```

### In Pipeline Scripts

All pipeline agent calls go through `run_dsh_agent()` which automatically handles phase transitions:

```bash
# No changes needed to existing pipeline code!
run_dsh_agent "video-generator" "prompt.txt" "output.json"
# ↑ Automatically: stops LLM, starts ComfyUI, waits for health

run_dsh_agent "audio-producer" "prompt.txt" "output.json"
# ↑ Automatically: stops LLM + ComfyUI, ensures TTS engines are up

run_dsh_agent "qa-inspector" "prompt.txt" "output.json"
# ↑ Under cloud: direct call. Under local: ensures LLM is healthy.
```

### Monitoring as Background Job

```bash
# Start VRAM watcher in background
(vram_watch_loop &)

# Check VRAM programmatically before expensive operations
if ! vram_check_now; then
    log "VRAM pressure detected — transitioning to save phase"
    svc_transition monitoring
fi
```

## Conflict Detection

The monitor detects these dangerous states:

| Conflict | Why It's Bad | Detection |
|----------|--------------|-----------|
| `ninfer` + `ninfer-us` both listening | systemd `Conflicts=` violated; double VRAM | Port scan :8080 + :8081 |
| Local LLM + ComfyUI | 28+ GB + 12 GB > 32 GB → OOM | Port scan any LLM port + :8188 |
| Orpheus + any LLM | 8+ GB + 28 GB > 32 GB → OOM | Port :9883 + any LLM port |
| Stale Qwen from previous run | Silent 27 GB leak, blocks H3 | Health check on :8085 after cloud launch |

## Emergency Procedures

### GPU Memory Exhaustion

```bash
# Quick rescue — stop Orpheus (often the culprit)
systemctl stop orpheus-tts.service
systemctl stop orpheus-backend.service

# If still critical, stop the local LLM
systemctl stop ninfer.service

# Restart ComfyUI to reclaim cache fragmentation
systemctl restart comfyui.service
```

### Stale Service from Previous Run

```bash
# Common: Qwen server left from local-mode run
systemctl stop qwen3.8-27b-q6k-cuda.service
docker stop qwen38-27b-q6k 2>/dev/null || true

# Verify clean state
nvidia-smi --query-gpu=memory.used --format=csv,noheader
# Should be < 5 GB when idle (display driver overhead only)
```

### Full Reset

```bash
# Stop absolutely everything
source pipeline/services.sh
cmd_stop_all

# Start fresh for specific phase
svc_transition video_gen   # or whichever phase you need
```

## Design Principles

1. **Phases are mutually exclusive** — never run two at once
2. **Transitions are atomic** — stop all before starting anything new
3. **Cloud routing bypasses local management** — agents on OpenRouter don't touch the GPU
4. **VRAM budget is conservative** — estimated service VRAM is less than peak to allow headroom
5. **Backward compatible** — `swap_to_27b`, `ensure_local_llm_running`, `stop_all_llms` all still work as before
