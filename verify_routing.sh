#!/bin/bash
# Verify every pack preset actually launches through its routed model (local Strata / cloud vision).
# Read-only apart from rewriting agent-default-model (which the pipeline does anyway).
cd /root/desktop/deepseek-harness || exit 1
set -a; . /root/.hermes/.env >/dev/null 2>&1; set +a
export DSH_PERMISSION_MODE=danger-full-access
# Reasoning + execution run on the local Strata LLM; make sure it is serving first.
export CLOUD_ROUTING=0
bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pipeline/gpu_owner.sh" llm >/dev/null || echo "WARNING: local LLM not healthy"

PACK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
printf "%-23s %-42s %-6s %s\n" "PRESET" "ROUTE" "EXIT" "OUTPUT"
for d in "$PACK"/.dsh/.agent-presets/*/; do
    preset=$(basename "$d")
    route=$(python3 "$PACK/pipeline/set_agent_model.py" "$preset" 2>&1 | head -1)
    out=$(timeout 180 pnpm dsh --profile headless \
            --patch "$PACK/.dsh/.agent-presets/$preset/agent.cordis.yml" \
            "Reply with exactly: OK" 2>/tmp/dsh_verify_err.txt | tail -3 | tr '\n' ' ')
    ec=$?
    printf "%-23s %-42s %-6s %s\n" "$preset" "$(echo "$route" | sed 's/ -> /â†’/')" "$ec" "${out:0:60}"
    if [ "$ec" != "0" ]; then
        echo "      stderr: $(grep -iE 'error|denied|no api key|unsupported' /tmp/dsh_verify_err.txt | head -2 | tr '\n' ' ' | cut -c1-160)"
    fi
done
