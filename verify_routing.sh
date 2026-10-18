#!/usr/bin/env bash
# verify_routing.sh — show (and optionally exercise) the per-agent cloud routes.
#
#   ./verify_routing.sh            print every preset's route + probe each provider's /models (free)
#   ./verify_routing.sh --live     ALSO send "Reply with exactly: OK" through every preset
#                                  (one real call per preset — costs tokens on both keys)
#
# Exit 0 when every route resolves and every probe passes; non-zero otherwise.
# There is no local LLM and no fallback: a failing probe is the answer, not a reroute.
set -uo pipefail
PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DSH_DIR="${DSH_DIR:-/root/desktop/deepseek-harness}"
LIVE=false
[ "${1:-}" = "--live" ] && LIVE=true

if [ -f /root/.hermes/.env ]; then
    set -a
    # shellcheck source=/dev/null
    . /root/.hermes/.env 2>/dev/null || true
    set +a
fi

echo "== routes"
python3 "$PACK_DIR/pipeline/set_agent_model.py" --dry-run-all || exit 2
echo
echo "== provider probes"
python3 "$PACK_DIR/pipeline/set_agent_model.py" --probe-all || exit 3

if [ "$LIVE" = "true" ]; then
    echo
    echo "== live calls"
    rc=0
    for preset_dir in "$PACK_DIR"/.dsh/.agent-presets/*/; do
        preset=$(basename "$preset_dir")
        if ! python3 "$PACK_DIR/pipeline/set_agent_model.py" "$preset" >/dev/null; then
            echo "FAIL route $preset"; rc=1; continue
        fi
        out=$(cd "$DSH_DIR" && timeout 180 pnpm dsh --profile headless \
            --patch "$preset_dir/agent.cordis.yml" "Reply with exactly: OK" 2>/tmp/verify_routing_$preset.err)
        if echo "$out" | grep -q "OK"; then
            echo "ok   $preset"
        else
            echo "FAIL $preset — $(tail -c 200 /tmp/verify_routing_$preset.err | tr '\n' ' ')"; rc=1
        fi
    done
    exit $rc
fi
