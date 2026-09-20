---
name: production-pack
description: Movie production pipeline — start production from a story, check status, manage the pipeline
trigger: production pack, movie production, film production, start movie, make movie, film status, production status, clip status
---

# Production Pack Skill

## Starting a Production

### From story text in chat
When the user gives you a story directly in chat:

1. Save the story to a markdown file:
```bash
cat > /root/production_pack/output/story_input.md << 'STORY'
{paste the user's story text here exactly}
STORY
```

2. Start the production pipeline in the background:
```bash
nohup /root/production_pack/run.sh /root/production_pack/output/story_input.md \
    > /root/production_pack/output/run_latest.log 2>&1 &
```

3. Confirm to the user: "Production started! Updates will appear in #film-maker."

### From an uploaded file
When the user uploads a .md or .txt file:

1. The file is saved by Hermes to a temporary path (check the attachment path from the message)
2. Copy it and start:
```bash
cp "{attachment_path}" /root/production_pack/output/story_input.md
nohup /root/production_pack/run.sh /root/production_pack/output/story_input.md \
    > /root/production_pack/output/run_latest.log 2>&1 &
```

### Resume production
When the user says "resume production" or "continue the movie":
```bash
nohup /root/production_pack/run.sh --resume \
    > /root/production_pack/output/run_latest.log 2>&1 &
```

### Regenerate a specific clip
When the user says "redo clip S01_005" or "regenerate clip S02_003":
```bash
nohup /root/production_pack/run.sh --resume --only-clip {SHOT_ID} \
    > /root/production_pack/output/run_latest.log 2>&1 &
```

### Resume from a specific clip
When the user says "restart from clip S02_003":
```bash
nohup /root/production_pack/run.sh --resume --from-clip {SHOT_ID} \
    > /root/production_pack/output/run_latest.log 2>&1 &
```

## Checking Status

When the user asks about status:

```bash
# Check if pipeline is running
PID=$(cat /root/production_pack/output/run_latest.pid 2>/dev/null)
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    echo "Pipeline: RUNNING (PID $PID)"
else
    echo "Pipeline: NOT RUNNING"
fi

# Find latest run
LATEST=$(ls -td /root/production_pack/output/run_* 2>/dev/null | head -1)
if [ -n "$LATEST" ]; then
    echo "Run: $LATEST"

    # Progress
    TOTAL_SHOTS=$(python3 -c "
import json
try:
    with open('$LATEST/shot_list.json') as f:
        print(len(json.load(f).get('shots',[])))
except: print('?')
" 2>/dev/null)

    CLIPS_DONE=$(find "$LATEST/clips" -name "qa_verdict.json" -exec grep -l '"verdict"' {} \; 2>/dev/null | wc -l)
    CLIPS_PASSED=$(find "$LATEST/clips" -name "qa_verdict.json" -exec grep -l '"PASS"' {} \; 2>/dev/null | wc -l)
    UPSCALED=$(find "$LATEST/clips" -name "clip_4k60.mp4" 2>/dev/null | wc -l)

    echo "Shots: $CLIPS_PASSED passed / $CLIPS_DONE reviewed / $TOTAL_SHOTS total"
    echo "Upscaled: $UPSCALED"

    # Phase detection
    [ ! -f "$LATEST/story.json" ] && echo "Phase: Story creation"
    [ -f "$LATEST/story.json" ] && [ ! -f "$LATEST/shot_list.json" ] && echo "Phase: Directing"
    [ -f "$LATEST/shot_list.json" ] && [ "$CLIPS_DONE" -lt "$TOTAL_SHOTS" ] 2>/dev/null && echo "Phase: Clip generation"
    [ -f "$LATEST/final/movie_4k60.mp4" ] && echo "Phase: COMPLETE"

    # Last log lines
    echo "--- Recent activity ---"
    tail -5 "$LATEST/pipeline.log" 2>/dev/null || tail -5 /root/production_pack/output/run_latest.log 2>/dev/null
fi

# Services
echo "--- Services ---"
systemctl is-active llama-qwen35-122b.service 2>/dev/null | xargs -I{} echo "Qwen 3.5 122B: {}"
systemctl is-active qwen3.8-27b-q6k-cuda.service 2>/dev/null | xargs -I{} echo "Qwen 3.8 27B: {}"
systemctl is-active comfyui.service 2>/dev/null | xargs -I{} echo "ComfyUI: {}"
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null | xargs -I{} echo "GPU VRAM: {}"
```

Present as:
```
🎬 Production Pack Status
━━━━━━━━━━━━━━━━━━━━━━━━
Pipeline:  RUNNING / NOT RUNNING
Phase:     Story / Directing / Clip Generation (X/Y) / Post-production / Complete
Progress:  X clips passed QA / Y total
Upscaled:  X clips at 4K 60fps
GPU:       XXXX MiB / 32607 MiB
Active LLM: Qwen 3.5 122B / Qwen 3.8 27B / None
━━━━━━━━━━━━━━━━━━━━━━━━
```

## Stopping Production

```bash
PID=$(cat /root/production_pack/output/run_latest.pid 2>/dev/null)
if [ -n "$PID" ]; then
    kill "$PID" 2>/dev/null
    echo "Pipeline stopped"
fi
```

## Important Notes
- The pipeline sends Discord notifications to #film-maker automatically
- Hermes runs on qwen3.8-27b-q6k with deepseek-v4.1-flash fallback
- During production, the LLM swaps between 122B and 27B — Hermes may be unavailable during 122B phases
- After production completes, the pipeline restarts Hermes automatically
