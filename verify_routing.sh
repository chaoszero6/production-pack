#!/bin/bash
# Verify every pack preset actually launches through its routed OpenRouter model.
# Read-only apart from rewriting agent-default-model (which the pipeline does anyway).
cd /root/desktop/deepseek-harness || exit 1
set -a; . /root/.hermes/.env >/dev/null 2>&1; set +a
export DSH_PERMISSION_MODE=danger-full-access
export CLOUD_ROUTING=1

PACK=/root/production_pack
printf "%-23s %-42s %-6s %s\n" "PRESET" "ROUTE" "EXIT" "OUTPUT"
for d in "$PACK"/.dsh/.agent-presets/*/; do
    preset=$(basename "$d")
    route=$(python3 "$PACK/pipeline/set_agent_model.py" "$preset" 2>&1 | head -1)
    out=$(timeout 180 pnpm dsh --profile headless \
            --patch "$PACK/.dsh/.agent-presets/$preset/agent.cordis.yml" \
            "Reply with exactly: OK" 2>/tmp/dsh_verify_err.txt | tail -3 | tr '\n' ' ')
    ec=$?
    printf "%-23s %-42s %-6s %s\n" "$preset" "$(echo "$route" | sed 's/ -> /→/')" "$ec" "${out:0:60}"
    if [ "$ec" != "0" ]; then
        echo "      stderr: $(grep -iE 'error|denied|no api key|unsupported' /tmp/dsh_verify_err.txt | head -2 | tr '\n' ' ' | cut -c1-160)"
    fi
done
