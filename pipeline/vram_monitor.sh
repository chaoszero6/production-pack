#!/bin/bash
# ════════════════════════════════════════════════════════════════
# Production Pack — VRAM Monitor & Alerting
# ════════════════════════════════════════════════════════════════
#
# Real-time VRAM usage tracking with phase-aware alerting thresholds.
# Detects when running services exceed safe limits and warns/alerts.
#
# Usage:
#   source pipeline/vram_monitor.sh          # load into shell
#   vram_report                               # one-shot status report
#   vram_watch_loop                           # continuous monitoring (foreground)
#   vram_check_now                            # single check, returns 0/1 for scripts
#
# Environment:
#   VRAM_WARN_MB     Warning threshold in MB (default: 26214 = 25.6 GB)
#   VRAM_CRIT_MB     Critical threshold in MB (default: 29360 = 28.7 GB)
#   VRAM_POLL_SEC    Poll interval in seconds for watch mode (default: 5)
#   VRAM_LOG         Log file path (default: $RUN_DIR/vram_monitor.log)

set -uo pipefail

PACK_DIR="${PACK_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
VRAM_WARN_MB="${VRAM_WARN_MB:-26214}"      # ~25.6 GB warning
VRAM_CRIT_MB="${VRAM_CRIT_MB:-29360}"      # ~28.7 GB critical
VRAM_POLL_SEC="${VRAM_POLL_SEC:-5}"        # poll every 5 seconds
VRAM_LOG="${VRAM_LOG:-/tmp/vram_monitor_$$}.log"

# Known service VRAM table (from phase_config.yaml)
declare -A SERVICE_VRAM=(
    [ninfer]       "28672"
    [ninfer_us]    "31744"
    [qwen_local]   "27648"
    [comfyui]      "0"        # dynamic — detected from nvidia-smi processes
    [orpheus_tts]  "8192"
    [chatterbox_tts] "0"      # CPU-only
    [cosyvoice_tts]  "0"      # CPU-only
    [kokoro_tts]     "0"      # CPU-only
)

# Port → service name mapping
declare -A PORT_TO_SVC=(
    [8080]="ninfer"
    [8081]="ninfer_us"
    [8085]="qwen_local"
    [8188]="comfyui"
    [9883]="orpheus_tts"
    [9882]="chatterbox_tts"
    [9880]="cosyvoice_tts"
    [9881]="kokoro_tts"
)

vram_log() {
    local ts
    ts=$(date '+%H:%M:%S')
    echo "[$ts] $*" | tee -a "$VRAM_LOG" 2>/dev/null || echo "[$ts] $*"
}

# Get total GPU memory from nvidia-smi
get_total_vram_mb() {
    if ! command -v nvidia-smi &>/dev/null; then
        echo "0"
        return
    fi
    nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null \
        | head -1 | tr -d ' ' || echo "0"
}

# Get used GPU memory from nvidia-smi
get_used_vram_mb() {
    if ! command -v nvidia-smi &>/dev/null; then
        echo "0"
        return
    fi
    nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null \
        | head -1 | tr -d ' ' || echo "0"
}

# Get free GPU memory from nvidia-smi
get_free_vram_mb() {
    local total used
    total=$(get_total_vram_mb)
    used=$(get_used_vram_mb)
    echo $((total - used))
}

# List all CUDA processes consuming VRAM
list_cuda_processes() {
    nvidia-smi --query-compute-apps=pid,name,used_memory --format=csv,noheader 2>/dev/null || true
}

# Calculate estimated VRAM from known running services
estimate_service_vram_mb() {
    local total=0
    local detail=""

    for port in "${!PORT_TO_SVC[@]}"; do
        if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
            local svc="${PORT_TO_SVC[$port]}"
            local vram="${SERVICE_VRAM[$svc]:-0}"
            if [[ "$vram" -gt 0 ]]; then
                total=$((total + vram))
                detail="${detail}  ${svc}: ${vram} MB\n"
            else
                # CPU-only or dynamic — no fixed allocation to add
                detail="${detail}  ${svc}: CPU/N/A\n"
            fi
        fi
    done

    echo "$total"
    if [[ -n "$detail" ]]; then
        echo "  --- Service breakdown ---" >&2
        echo -e "$detail" >&2
    fi
}

# Check for dangerous service combinations
detect_conflicts() {
    local conflicts=()

    # ninfer + ninfer-us conflict (systemd Conflicts= but may both be stuck)
    if ss -tlnp 2>/dev/null | grep -qE ':808[01] ' ; then
        local has_8080=false has_8081=false
        ss -tlnp 2>/dev/null | grep -q ':8080 ' && has_8080=true
        ss -tlnp 2>/dev/null | grep -q ':8081 ' && has_8081=true
        if $has_8080 && $has_8081; then
            conflicts+=("⚠ ninfer(:8080) AND ninfer-us(:8081) both listening — systemd Conflicts= violated!")
        fi
    fi

    # Any LLM + ComfyUI together (under local mode only)
    if [[ "${CLOUD_ROUTING:-1}" != "1" ]]; then
        local has_llm=false has_comfyui=false
        for llm_port in 8080 8081 8085; do
            ss -tlnp 2>/dev/null | grep -q ":${llm_port} " && has_llm=true
        done
        ss -tlnp 2>/dev/null | grep -q ':8188 ' && has_comfyui=true

        if $has_llm && $has_comfyui; then
            local llm_count=0
            for llm_port in 8080 8081 8085; do
                ss -tlnp 2>/dev/null | grep -q ":${llm_port} " && llm_count=$((llm_count + 1))
            done
            if [[ $llm_count -gt 0 ]]; then
                conflicts+=("⚠ Local LLM(s) + ComfyUI running simultaneously — VRAM overflow likely ($llm_count LLM(s), ${HAS_COMFYUI_MB} MB)")
            fi
        fi
    fi

    # Orpheus + any LLM (VRAM dance should prevent this)
    if ss -tlnp 2>/dev/null | grep -q ':9883 '; then
        for llm_port in 8080 8081 8085; do
            if ss -tlnp 2>/dev/null | grep -q ":${llm_port} "; then
                conflicts+=("⚠ Orpheus(:9883) + LLM(:$llm_port) — VRAM dance violation!")
                break
            fi
        done
    fi

    if [[ ${#conflicts[@]} -gt 0 ]]; then
        printf '%s\n' "${conflicts[@]}"
    fi
}

# Single VRAM snapshot report
vram_report() {
    local total used free est_svc
    total=$(get_total_vram_mb)
    used=$(get_used_vram_mb)
    free=$(get_free_vram_mb)

    echo "╔══════════════════════════════════════════════════════════╗"
    echo "║              VRAM STATUS REPORT                         ║"
    echo "╠══════════════════════════════════════════════════════════╣"
    printf "║  Total: %8s MB │ Used: %8s MB │ Free: %8s MB  ║\n" "$total" "$used" "$free"
    echo "╠══════════════════════════════════════════════════════════╣"

    # Usage percentage
    local pct=0
    if [[ "$total" -gt 0 ]]; then
        pct=$((used * 100 / total))
    fi
    local bar_len=20
    local filled=$((pct * bar_len / 100))
    local empty=$((bar_len - filled))
    local bar=""
    for ((i=0; i<filled; i++)); do bar+="█"; done
    for ((i=0; i<empty; i++)); do bar+="░"; done

    local severity="  "
    if [[ $pct -ge 90 ]]; then
        severity="🔴 CRITICAL"
    elif [[ $pct -ge 80 ]]; then
        severity="🟡 WARNING"
    else
        severity="🟢 OK"
    fi
    printf "║  Usage: [%s] %3d%% %s               ║\n" "$bar" "$pct" "$severity"
    echo "╠══════════════════════════════════════════════════════════╣"

    # Estimate from known services
    est_svc=$(estimate_service_vram_mb 2>/dev/null)
    if [[ "$est_svc" -gt 0 ]]; then
        printf "║  Est. service VRAM: %8s MB (known models)      ║\n" "$est_svc"
        local headroom=$((total - est_svc))
        printf "║  Headroom vs known: %8s MB                          ║\n" "$headroom"
    else
        echo "║  Est. service VRAM: N/A (no known services running)    ║"
    fi
    echo "╠══════════════════════════════════════════════════════════╣"

    # Active ports
    echo "║  Active Ports:                                            ║"
    local active_ports=""
    for port in 8080 8081 8085 8188 9880 9881 9882 9883; do
        if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
            active_ports="${active_ports} :${port}"
        fi
    done
    if [[ -n "$active_ports" ]]; then
        echo "║    ${active_ports}                              ║"
    else
        echo "║    (none)                                               ║"
    fi
    echo "╠══════════════════════════════════════════════════════════╣"

    # Detected conflicts
    local conflicts
    conflicts=$(detect_conflicts)
    if [[ -n "$conflicts" ]]; then
        echo "║  ⚠ CONFLICTS DETECTED:                                    ║"
        while IFS= read -r line; do
            printf "║    %s                             ║\n" "$line"
        done <<< "$conflicts"
    else
        echo "║  ✓ No conflicts detected                                ║"
    fi

    echo "╚══════════════════════════════════════════════════════════╝"
}

# Quick check returning exit code for scripting (0 = safe, 1 = warning, 2 = critical)
vram_check_now() {
    local used total pct

    used=$(get_used_vram_mb)
    total=$(get_total_vram_mb)

    if [[ "$total" -eq 0 ]]; then
        echo "WARNING: Could not detect GPU" >&2
        return 1
    fi

    pct=$((used * 100 / total))

    if [[ $pct -ge 92 ]]; then
        echo "CRITICAL: VRAM at ${pct}% (${used}/${total} MB)" >&2
        return 2
    elif [[ $pct -ge 82 ]]; then
        echo "WARNING: VRAM at ${pct}% (${used}/${total} MB)" >&2
        return 1
    else
        echo "OK: VRAM at ${pct}% (${used}/${total} MB)"
        return 0
    fi
}

# Continuous monitoring loop (runs until interrupted)
vram_watch_loop() {
    local iterations=0
    log "VRAM watcher started (poll every ${VRAM_POLL_SEC}s). Press Ctrl+C to stop."

    while true; do
        iterations=$((iterations + 1))
        vram_report >> "$VRAM_LOG" 2>&1

        local status_code
        vram_check_now > /dev/null 2>&1 && status_code=$? || status_code=$?

        case $status_code in
            0) ;; # quiet — everything fine
            1) vram_log "WARNING: VRAM usage high (${used}/${total} MB)";;
            2)
                vram_log "CRITICAL: VRAM near exhaustion (${used}/${total} MB)!"
                # Auto-detect and try to help
                _auto_rescue_vram
                ;;
        esac

        # Check for service conflicts too
        local conflicts
        conflicts=$(detect_conflicts 2>/dev/null)
        if [[ -n "$conflicts" ]]; then
            vram_log "CONFLICT: $conflicts"
        fi

        sleep "$VRAM_POLL_SEC"
    done
}

# Emergency: if VRAM is critically full, try to free it
_auto_rescue_vram() {
    # Stop orpheus first (it's often a recent addition that caused overload)
    if ss -tlnp 2>/dev/null | grep -q ':9883 '; then
        vram_log "Auto-rescue: stopping Orpheus TTS..."
        systemctl stop orpheus-tts.service 2>/dev/null || true
        systemctl stop orpheus-backend.service 2>/dev/null || true
    fi

    # If still critical, stop non-essential LLM
    local used
    used=$(get_used_vram_mb)
    if [[ $((used * 100 / $(get_total_vram_mb))) -ge 92 ]]; then
        vram_log "Auto-rescue: stopping qwen-local..."
        systemctl stop qwen3.8-27b-q6k-cuda.service 2>/dev/null || true
    fi
}


# ────────────────────────────────────────────────────────────
# ENTRY POINT — only execute when run directly, not when source-d
# ────────────────────────────────────────────────────────────
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
case "${1:-report}" in
    report)         vram_report ;;
    check)          vram_check_now ;;
    watch)          vram_watch_loop ;;
    list-procs)     list_cuda_processes ;;
    help|--help|-h)
        echo "Usage: $0 {report|check|watch|list-procs|help}"
        echo ""
        echo "  report  — Full VRAM status display (default)"
        echo "  check   — Quick check, returns 0/1/2 (exit code = severity)"
        echo "  watch   — Continuous monitoring loop"
        echo "  list-procs — Show CUDA compute processes"
        ;;
    *)
        echo "Unknown: $1"; exit 1 ;;
esac
fi
