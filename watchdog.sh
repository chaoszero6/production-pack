#!/bin/bash
# Watchdog — uses AI agent to diagnose crashes, fix, and auto-resume.
# Usage: ./watchdog.sh <story.md>        (new production)
#        ./watchdog.sh --resume          (resume latest)

PACK_DIR="/root/production_pack"
DSH_DIR="/root/desktop/deepseek-harness"
NOTIFY="$PACK_DIR/pipeline/notify.sh"
MAX_CRASHES=5
CRASH_COUNT=0

notify() { bash "$NOTIFY" "$1" "${2:-}" 2>/dev/null; }
log()    { echo "[watchdog $(date '+%H:%M:%S')] $*"; }

ai_diagnose_and_fix() {
    local exit_code="$1"
    local run_dir
    run_dir=$(find "$PACK_DIR/output" -maxdepth 1 -type d -name "run_2*" | sort -r | head -1)
    if [ -z "$run_dir" ]; then
        log "No run directory found — retrying blind"
        notify "Watchdog: no run dir, retrying in 30s" "Watchdog"
        sleep 30
        return 0
    fi

    log "Calling AI agent to diagnose crash (exit=$exit_code) in $run_dir..."
    notify "Pipeline crashed (exit $exit_code). AI agent diagnosing..." "Watchdog"

    # Collect crash context
    local crash_context=""
    crash_context+="=== PIPELINE EXIT CODE: $exit_code ===\n"
    crash_context+="=== LAST 80 LINES OF pipeline.log ===\n"
    crash_context+="$(tail -80 "$run_dir/pipeline.log" 2>/dev/null)\n"
    crash_context+="=== LAST 40 LINES OF run_latest.log ===\n"
    crash_context+="$(tail -40 "$PACK_DIR/output/run_latest.log" 2>/dev/null)\n"
    crash_context+="=== SERVICES ===\n"
    crash_context+="comfyui: $(systemctl is-active comfyui.service 2>/dev/null)\n"
    crash_context+="qwen35: $(systemctl is-active llama-qwen35-122b.service 2>/dev/null)\n"
    crash_context+="qwen38: $(systemctl is-active qwen3.8-27b-q6k-cuda.service 2>/dev/null)\n"
    crash_context+="=== GPU ===\n"
    crash_context+="$(nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader 2>/dev/null)\n"
    crash_context+="=== DISK ===\n"
    crash_context+="$(df -h /root --output=avail 2>/dev/null | tail -1)\n"
    crash_context+="=== LAST RAW AGENT OUTPUT ===\n"
    local last_raw=$(ls -t "$run_dir"/*_raw.txt 2>/dev/null | head -1)
    if [ -n "$last_raw" ]; then
        crash_context+="$(tail -30 "$last_raw" 2>/dev/null)\n"
    fi

    # Write crash context to a temp file
    local ctx_file="$run_dir/watchdog_crash_context.txt"
    echo -e "$crash_context" > "$ctx_file"

    # Write the diagnosis prompt
    local prompt_file="$run_dir/watchdog_prompt.txt"
    cat > "$prompt_file" << 'DIAG_PROMPT'
You are the pipeline watchdog. The production pipeline just crashed. Your job:

1. READ the crash context below
2. DIAGNOSE the root cause (not symptoms)
3. FIX the problem by running shell commands
4. Report what you found and fixed

Common crash causes and fixes:
- YAML parse error in ~/.dsh/settings.yaml → restore from backup: cp $(ls -t ~/.dsh/settings.yaml.bak* | head -1) ~/.dsh/settings.yaml
- Cloud API auth failure (401/MissingSessionID) → re-copy key: cp /root/.hermes/.env /root/desktop/deepseek-harness/.env
- Cloud API rate limit (429) → just wait, report how long
- Cloud API timeout → check internet: curl -s https://opencode.ai/health
- ComfyUI crash/OOM → restart: systemctl restart comfyui.service; stop local LLMs to free VRAM
- dsh hung process → pkill -f "dsh --profile"
- Disk full → report, cannot auto-fix
- JSON parse failure from agent → likely transient, just retry
- settings.yaml agent-default-model section missing → the set_agent_model python script may have corrupted it

After fixing, output a JSON summary:
{
  "diagnosis": "what went wrong",
  "fix_applied": "what you did to fix it",
  "can_resume": true/false,
  "wait_seconds": 30
}
DIAG_PROMPT

    # Append crash context to prompt
    echo -e "\n--- CRASH CONTEXT ---\n" >> "$prompt_file"
    cat "$ctx_file" >> "$prompt_file"

    # Run the diagnosis agent using flash (cheap, fast)
    local diag_output="$run_dir/watchdog_diagnosis.json"

    # Set model to flash for diagnosis
    cd "$DSH_DIR"
    python3 - "/root/.dsh/settings.yaml" "opencode-go-deepseek" "deepseek-v4.1-flash" "low" << 'PYEOF'
import sys, re, os, tempfile
path, provider, model, effort = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
text = open(path).read()
m = re.search(r'(?m)^agent-default-model:[ \t]*$', text)
if not m:
    sys.exit(0)
head, tail = text[:m.start()], text[m.end():]
lines = tail.splitlines(keepends=True)
rest = ''
for i, ln in enumerate(lines):
    if ln.strip() and ln[:1] not in (' ', '\t', '#'):
        rest = ''.join(lines[i:])
        break
block = (
    'agent-default-model:\n'
    f'  provider: {provider}\n'
    f'  model: {model}\n'
    f'  reasoningEffort: {effort}\n'
)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.settings-', suffix='.tmp')
with os.fdopen(fd, 'w') as fh:
    fh.write(head + block + rest)
os.replace(tmp, path)
PYEOF

    # Run agent
    local prompt_text
    prompt_text=$(cat "$prompt_file")
    export DSH_PERMISSION_MODE="${DSH_PERMISSION_MODE:-danger-full-access}"

    pnpm dsh --profile headless \
        --patch "$PACK_DIR/.dsh/.agent-presets/pipeline-orchestrator/agent.cordis.yml" \
        "$prompt_text" \
        > "$run_dir/watchdog_raw.txt" \
        2>> "$run_dir/pipeline.log" || true

    # Extract diagnosis JSON
    python3 "$PACK_DIR/extract_json.py" \
        "$run_dir/watchdog_raw.txt" "$diag_output" \
        diagnosis fix_applied can_resume 2>/dev/null || true

    # Parse result
    local can_resume="true"
    local wait_secs=30
    local diagnosis="unknown"
    local fix="unknown"

    if [ -s "$diag_output" ]; then
        can_resume=$(python3 -c "import json; print(json.load(open('$diag_output')).get('can_resume', True))" 2>/dev/null || echo "true")
        wait_secs=$(python3 -c "import json; print(json.load(open('$diag_output')).get('wait_seconds', 30))" 2>/dev/null || echo "30")
        diagnosis=$(python3 -c "import json; print(json.load(open('$diag_output')).get('diagnosis', 'unknown'))" 2>/dev/null || echo "unknown")
        fix=$(python3 -c "import json; print(json.load(open('$diag_output')).get('fix_applied', 'unknown'))" 2>/dev/null || echo "unknown")
    fi

    log "Diagnosis: $diagnosis"
    log "Fix: $fix"
    log "Can resume: $can_resume, wait: ${wait_secs}s"
    notify "Diagnosis: $diagnosis\nFix: $fix\nResume: $can_resume (wait ${wait_secs}s)\nAttempt $((CRASH_COUNT+1))/$MAX_CRASHES" "Watchdog AI Diagnosis"

    if [ "$can_resume" = "False" ] || [ "$can_resume" = "false" ]; then
        log "AI agent says cannot resume. Stopping."
        return 1
    fi

    sleep "$wait_secs"
    return 0
}

# ── Main loop ──
# First run uses original args; subsequent runs use --resume with explicit run dir
FIRST_RUN=true

while true; do
    log "Starting pipeline..."
    "$PACK_DIR/run.sh" "$@"
    EXIT=$?

    if [ $EXIT -eq 0 ]; then
        log "Pipeline completed successfully."
        notify "Pipeline completed successfully!" "Watchdog"
        break
    fi

    CRASH_COUNT=$((CRASH_COUNT + 1))
    log "Pipeline exited with code $EXIT (crash $CRASH_COUNT/$MAX_CRASHES)"

    if [ $CRASH_COUNT -ge $MAX_CRASHES ]; then
        notify "Pipeline crashed $MAX_CRASHES times — AI agent could not fix it.\nLast exit: $EXIT\nManual intervention needed." "Watchdog STOPPED"
        log "Max crashes reached. Giving up."
        exit 1
    fi

    ai_diagnose_and_fix $EXIT || break

    # Switch to resume mode with explicit run dir path
    if [ "$FIRST_RUN" = "true" ]; then
        FIRST_RUN=false
        LATEST_RUN=$(ls -td "$PACK_DIR/output/run_"* 2>/dev/null | head -1)
        if [ -n "$LATEST_RUN" ] && [ -d "$LATEST_RUN" ]; then
            set -- --resume "$LATEST_RUN"
        else
            set -- --resume
        fi
    fi
done
