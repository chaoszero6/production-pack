---
name: production-pack-status
description: Check the status of the Production Pack movie generation pipeline
trigger: production pack status, movie status, film status, clip status, pipeline status
---

# Production Pack Status Skill

When the user asks about the production pack or movie generation status, check these:

## Quick Status
```bash
# Check if pipeline is running
pgrep -f "run.sh" > /dev/null && echo "Pipeline: RUNNING" || echo "Pipeline: NOT RUNNING"

# Find latest run
LATEST=$(ls -td /root/production_pack/output/run_* 2>/dev/null | head -1)
if [ -n "$LATEST" ]; then
    echo "Latest run: $LATEST"

    # Count clips
    TOTAL=$(find "$LATEST/clips" -name "clip.mp4" 2>/dev/null | wc -l)
    PASSED=$(find "$LATEST/clips" -name "qa_verdict.json" -exec grep -l '"verdict": "PASS"' {} \; 2>/dev/null | wc -l)
    UPSCALED=$(find "$LATEST/clips" -name "clip_4k60.mp4" 2>/dev/null | wc -l)
    echo "Clips: $PASSED passed, $TOTAL generated, $UPSCALED upscaled"

    # Check final movie
    [ -f "$LATEST/final/movie_4k60.mp4" ] && echo "Final movie: DONE" || echo "Final movie: NOT YET"

    # Check current phase
    [ -f "$LATEST/story.json" ] && echo "Story: done" || echo "Story: pending"
    [ -f "$LATEST/shot_list.json" ] && echo "Shot list: done" || echo "Shot list: pending"

    # Pipeline log tail
    echo "--- Last 5 log lines ---"
    tail -5 "$LATEST/pipeline.log" 2>/dev/null || echo "(no log yet)"
fi
```

## Service Status
```bash
echo "=== Services ==="
systemctl is-active llama-qwen35-122b.service 2>/dev/null && echo "Qwen 3.5 122B: RUNNING" || echo "Qwen 3.5 122B: stopped"
systemctl is-active qwen3.8-27b-q6k-cuda.service 2>/dev/null && echo "Qwen 3.8 27B: RUNNING" || echo "Qwen 3.8 27B: stopped"
systemctl is-active comfyui.service 2>/dev/null && echo "ComfyUI: RUNNING" || echo "ComfyUI: stopped"
systemctl is-active kokoro-tts.service 2>/dev/null && echo "Kokoro TTS: RUNNING" || echo "Kokoro TTS: stopped"
systemctl is-active chatterbox-tts.service 2>/dev/null && echo "Chatterbox TTS: RUNNING" || echo "Chatterbox TTS: stopped"
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null
```

## Report Format
Present as a clean status card:
```
🎬 Production Pack Status
━━━━━━━━━━━━━━━━━━━━━
Phase:    [Pre-production / Clip Generation / Post-production / Complete]
Progress: [X/Y clips passed QA]
Current:  [what's happening right now]
GPU:      [VRAM usage]
━━━━━━━━━━━━━━━━━━━━━
```
