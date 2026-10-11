#!/bin/bash
# ════════════════════════════════════════════════════════════════
# Production Pack — Service Lifecycle Manager
# ════════════════════════════════════════════════════════════════
#
# Centralized VRAM-aware service management for the RTX 5090 (32 GB).
# Reads phase_config.yaml to decide which services to start/stop
# when transitioning between operating phases:
#   thinking | video_gen | audio_gen | monitoring
#
# Usage:
#   source pipeline/services.sh          # load functions into shell
#   svc_transition <phase_name>          # switch to a phase
#   svc_is_healthy <service_name>        # check if a service responds
#   svc_list_running                     # list all running services
#   svc_status                          # full status report
#
# Environment:
#   CLOUD_ROUTING=1|0   Default: 1 (cloud, no local LLM)
#   SVC_LOG             Log file path (default: $RUN_DIR/svc_transition.log)

set -euo pipefail

PACK_DIR="${PACK_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PHASE_CONFIG="${PACK_DIR}/pipeline/phase_config.yaml"
DSH_DIR="${DSH_DIR:-/root/desktop/deepseek-harness}"

# The local LLM (Strata Swift, Qwen 3.8 Flash Next — unit, port, 2-minute start
# timeout) is defined once in local_llm.sh; every start/stop below goes through it.
# shellcheck source=/dev/null
source "${PACK_DIR}/pipeline/local_llm.sh"

# Log file — falls back to stderr if RUN_DIR not set yet
SVC_LOG="${SVC_LOG:-/tmp/production_svc_$$}"
log() {
    local ts
    ts=$(date '+%H:%M:%S')
    echo "[$ts] [svc] $*" | tee -a "$SVC_LOG" 2>/dev/null || echo "[$ts] [svc] $*"
}

log_warn() {
    local ts
    ts=$(date '+%H:%M:%S')
    echo "[$ts] [WARN] $*" | tee -a "$SVC_LOG" 2>/dev/null || echo "[$ts] [WARN] $*"
}

log_error() {
    local ts
    ts=$(date '+%H:%M:%S')
    echo "[$ts] [ERROR] $*" >&2
    echo "[$ts] [ERROR] $*" >> "$SVC_LOG" 2>/dev/null || true
}

# ────────────────────────────────────────────────────────────
# YAML PARSING HELPERS
# Parse key values from phase_config.yaml using grep/sed.
# No Python/YAML dependency needed for basic reads.
# ────────────────────────────────────────────────────────────

# Extract health-check URL for a service name
svc_health_url() {
    local svc="$1"
    if [[ ! -f "$PHASE_CONFIG" ]]; then
        log_warn "No phase config found at $PHASE_CONFIG"; return 1; fi
    # Find the line matching our service's health endpoint
    sed -n "/^health_checks:/,/^[^ ]/p" "$PHASE_CONFIG" \
        | grep -E "^  ${svc}:" | head -1 \
        | sed 's/.*"\(.*\)"/\1/' 2>/dev/null || true
}

# Get estimated VRAM for a service (MB)
svc_vram_mb() {
    local svc="$1"
    if [[ ! -f "$PHASE_CONFIG" ]]; then return 0; fi
    local val
    val=$(awk '/^  '"$svc"':/{found=1} found && /vram_mb:/{print $NF; exit}' "$PHASE_CONFIG" 2>/dev/null)
    echo "${val:-0}"
}

# Check if a service is retired
svc_is_retired() {
    local svc="$1"
    if [[ ! -f "$PHASE_CONFIG" ]]; then return 1; fi
    grep -A10 "^  ${svc}:" "$PHASE_CONFIG" 2>/dev/null | grep -q "retired: true"
}

# ────────────────────────────────────────────────────────────
# HEALTH CHECKS
# ────────────────────────────────────────────────────────────

# Wait for a service to become healthy on its port
svc_wait_for_ready() {
    local svc_name="$1"
    local timeout_sec="${2:-60}"
    local url
    url=$(svc_health_url "$svc_name")

    if [[ -z "$url" ]]; then
        log "No health URL for $svc_name — skipping wait"
        return 0
    fi

    for i in $(seq 1 "$timeout_sec"); do
        if curl -sf --max-time 3 "$url" > /dev/null 2>&1; then
            log "$svc_name ready on port $(echo "$url" | grep -oE '[0-9]+')"
            return 0
        fi
        sleep 1
    done
    log_warn "$svc_name did not become ready within ${timeout_sec}s"
    return 1
}

# Check if any service on a port is listening
svc_port_in_use() {
    local port="$1"
    ss -tlnp 2>/dev/null | grep -q ":${port} " && return 0 || return 1
}

# ────────────────────────────────────────────────────────────
# LOW-LEVEL SERVICE ACTIONS
# ────────────────────────────────────────────────────────────

# Stop a systemd service by unit name
_stop_systemd() {
    local unit="$1"
    systemctl stop "$unit" 2>/dev/null && return 0
    return 0  # ignore failures — service may already be stopped
}

# Start a systemd service and optionally restart its backend
_start_systemd() {
    local unit="$1"
    systemctl start "$unit" 2>/dev/null && return 0
    log_warn "Failed to start $unit"
    return 1
}

# Restart a systemd service (stop + start)
_restart_systemd() {
    local unit="$1"
    systemctl restart "$unit" 2>/dev/null && return 0
    log_warn "Failed to restart $unit"
    return 1
}

# Stop a Docker container
_stop_docker() {
    local name="$1"
    docker stop "$name" 2>/dev/null && return 0
    return 0
}

# Start Orpheus TTS with its required backend
_start_orpheus_full() {
    _start_systemd "orpheus-backend.service"
    sleep 3
    _start_systemd "orpheus-tts.service"
    sleep 2
}

_stop_orpheus_full() {
    _stop_systemd "orpheus-tts.service"
    _stop_systemd "orpheus-backend.service"
    sleep 1
}

# ────────────────────────────────────────────────────────────
# ALIAS RESOLUTION
# For aliases like "ninfer_or_ninfer_us", pick the best available.
# ────────────────────────────────────────────────────────────

# Resolve an alias reference to actual service(s). Returns space-separated names.
_resolve_alias() {
    local alias_name="$1"
    case "$alias_name" in
        ninfer_or_ninfer_us|local_llm)
            # The Strata unit is the only local LLM in service; the ninfer
            # units are masked on 207 and only matter as stragglers to stop.
            if svc_is_healthy "strata"; then
                echo "strata"
            elif svc_is_healthy "ninfer"; then
                echo "ninfer"
            elif svc_is_healthy "ninfer_us"; then
                echo "ninfer_us"
            else
                echo "strata"  # default recommendation
            fi
            ;;
        *)
            echo "$alias_name"
            ;;
    esac
}

# ────────────────────────────────────────────────────────────
# PHASE TRANSITIONS
# ────────────────────────────────────────────────────────────

# Transition to a specific phase. This is the main entry point.
# Stops ALL conflicting services, then starts only what the target phase needs.
svc_transition() {
    local target_phase="$1"
    local force="${2:-false}"

    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "TRANSITION → $target_phase (force=$force)"
    log "  CLOUD_ROUTING=${CLOUD_ROUTING:-1}"

    # Validate phase exists
    if ! grep -qE "^  ${target_phase}:" "$PHASE_CONFIG" 2>/dev/null; then
        log_error "Unknown phase: $target_phase"
        log_error "Available phases: thinking, video_gen, audio_gen, monitoring"
        return 1
    fi

    # ── Phase 1: STOP EVERYTHING that conflicts ──
    _transition_stop_all "$target_phase"

    # ── Phase 2: START services needed for this phase ──
    _transition_start_needed "$target_phase"

    # ── Phase 3: Verify health ──
    _transition_verify "$target_phase"

    log "TRANSITION COMPLETE → $target_phase"
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# Identify and stop services that must NOT run in the target phase
_transition_stop_all() {
    local target_phase="$1"

    # Cloud mode shortcut: no local LLM ever runs
    if [[ "${CLOUD_ROUTING:-1}" == "1" ]]; then
        log "Cloud routing active — skipping local LLM stops"
        # Still stop ComfyUI during audio generation
        if [[ "$target_phase" == "audio_gen" ]]; then
            _transition_stop_service_by_name "comfyui" "ComfyUI"
        fi
        return
    fi

    # Under local mode, we're aggressive about stopping
    case "$target_phase" in
        video_gen)
            # Stop ALL LLMs and ALL TTS — give ComfyUI everything
            _transition_stop_llm_services
            _transition_stop_tts_services
            ;;
        audio_gen)
            # Stop LLMs and ComfyUI — TTS engines need clean GPU
            _transition_stop_llm_services
            _transition_stop_service_by_name "comfyui" "ComfyUI"
            ;;
        thinking)
            # Stop ComfyUI under local mode to free VRAM for LLM
            _transition_stop_service_by_name "comfyui" "ComfyUI"
            # Don't stop TTS — they're CPU-only anyway
            ;;
        monitoring)
            # Minimal: just stop heavy stuff
            _transition_stop_llm_services_except_ninfer
            _transition_stop_tts_services
            ;;
    esac
}

# Stop all three possible LLM variants
_transition_stop_llm_services() {
    # Park the Strata LLM (31.8 GB) and any legacy unit; mark it parked so
    # llm_monitor.py does not bring it straight back while ComfyUI/TTS own the GPU.
    mkdir -p "${PACK_DIR}/output" 2>/dev/null || true
    touch "${PACK_DIR}/output/.llm_parked" 2>/dev/null || true
    llm_stop
}

# Stop every LLM except the current local one (for monitoring/thinking phases)
_transition_stop_llm_services_except_ninfer() {
    llm_stop_legacy
    sleep 1
}

# Stop all TTS engines
_transition_stop_tts_services() {
    systemctl stop tts-cluster.service 2>/dev/null || true
    bash "${PACK_DIR}/pipeline/tts_cluster.sh" stop
}

# Helper: stop a named service using the alias resolution
_transition_stop_service_by_name() {
    local svc_ref="$1"       # e.g., "comfyui" or "ninfer_or_ninfer_us"
    local display_name="$2"  # human-readable name

    if [[ "$svc_ref" == *"_or_"* ]]; then
        local resolved
        resolved=$(_resolve_alias "$svc_ref")
        for r in $resolved; do
            # Convert snake_case service ref to systemd unit
            local unit="${r/_/-}"
            if [[ "$r" == "ninfer_us" ]]; then unit="ninfer-us.service"; fi
            _stop_systemd "$unit"
        done
    else
        # Direct service name → convert to common unit names
        case "$svc_ref" in
            comfyui)    _stop_systemd "comfyui.service" ;;
            strata)     _stop_systemd "$LLM_UNIT" ;;
            ninfer)     _stop_systemd "ninfer.service" ;;
            ninfer_us)  _stop_systemd "ninfer-us.service" ;;
            qwen_local) _stop_systemd "qwen3.8-27b-q6k-cuda.service" ;;
            qwen_docker)_stop_docker "qwen38-27b-q6k" ;;
            chatterbox_tts) _stop_systemd "chatterbox-tts.service" ;;
            cosyvoice_tts)  _stop_systemd "cosyvoice.service" ;;
            kokoro_tts)     _stop_systemd "kokoro-tts.service" ;;
            orpheus_tts)    _stop_orpheus_full ;;
            hermes_gateway) _stop_systemd "hermes-stack.service" ;;
        esac
    fi
}

# Start services required by the target phase
_transition_start_needed() {
    local target_phase="$1"

    case "$target_phase" in
        thinking)
            if [[ "${CLOUD_ROUTING:-1}" != "1" ]]; then
                # Local mode: the Strata LLM must be serving (≈2 min to start)
                rm -f "${PACK_DIR}/output/.llm_parked" 2>/dev/null || true
                if ! svc_is_healthy_any_llm; then
                    log "Starting $LLM_UNIT for local agent reasoning (≈2 min)..."
                    llm_start "$LLM_START_TIMEOUT" || true
                fi
            fi
            ;;
        video_gen)
            # Start/ensure ComfyUI is running
            _ensure_comfyui_running
            ;;
        audio_gen)
            # Start all TTS engines
            _start_all_tts
            ;;
        monitoring)
            # Ensure one lightweight LLM is running
            _ensure_monitoring_llm
            ;;
    esac
}

# ────────────────────────────────────────────────────────────
# COMFYUI MANAGEMENT
# ────────────────────────────────────────────────────────────

_ensure_comfyui_running() {
    # ComfyUI conflicts with the LLM and the TTS cluster; gpu_owner parks the
    # LLM (marker + stop), starts comfyui.service and waits for :8188.
    if svc_is_healthy "comfyui" && ! llm_active; then
        log "ComfyUI already running"
        return 0
    fi
    bash "${PACK_DIR}/pipeline/gpu_owner.sh" comfyui || {
        log_error "ComfyUI failed to become healthy"
        return 1
    }
}

# ────────────────────────────────────────────────────────────
# TTS MANAGEMENT
# ────────────────────────────────────────────────────────────

_start_all_tts() {
    # One unit for the whole set; its Conflicts= park the LLM and ComfyUI.
    mkdir -p "${PACK_DIR}/output" 2>/dev/null || true
    touch "${PACK_DIR}/output/.llm_parked" 2>/dev/null || true
    bash "${PACK_DIR}/pipeline/tts_cluster.sh" start 240 || log_warn "TTS cluster not fully healthy"
}

# ────────────────────────────────────────────────────────────
# MONITORING LLM SELECTION
# ────────────────────────────────────────────────────────────

_ensure_monitoring_llm() {
    rm -f "${PACK_DIR}/output/.llm_parked" 2>/dev/null || true
    if svc_is_healthy_any_llm; then
        return 0
    fi
    log "Starting monitoring LLM ($LLM_UNIT, ≈2 min)..."
    llm_start "$LLM_START_TIMEOUT" || {
        log_warn "$LLM_UNIT not available for monitoring"
        return 1
    }
}

# ────────────────────────────────────────────────────────────
# HEALTH VERIFICATION
# ────────────────────────────────────────────────────────────

_transition_verify() {
    local target_phase="$1"

    case "$target_phase" in
        thinking)
            if [[ "${CLOUD_ROUTING:-1}" != "1" ]]; then
                if ! svc_is_healthy_any_llm; then
                    log_warn "No LLM healthy after transition — agents will use cloud API"
                fi
            fi
            ;;
        video_gen)
            if ! svc_is_healthy "comfyui"; then
                log_error "ComfyUI unhealthy after video_gen transition!"
                return 1
            fi
            ;;
        audio_gen)
            if ! svc_is_healthy "orpheus_tts"; then
                log_warn "Orpheus TTS unhealthy — some dialogue may fail"
            fi
            ;;
        monitoring)
            if ! svc_is_healthy_any_llm; then
                log_warn "No LLM healthy for monitoring"
            fi
            ;;
    esac
}

# Health check helpers
svc_is_healthy() {
    local svc_name="$1"
    local url
    url=$(svc_health_url "$svc_name")
    if [[ -z "$url" ]]; then
        # Fallback: check port directly
        local port
        port=$(case "$svc_name" in
            strata) echo "$LLM_PORT" ;;
            ninfer) echo 8080 ;;
            ninfer_us) echo 8081 ;;
            qwen_local) echo 8085 ;;
            comfyui) echo 8188 ;;
            chatterbox_tts) echo 9882 ;;
            cosyvoice_tts) echo 9880 ;;
            orpheus_tts) echo 9883 ;;
            kokoro_tts) echo 9881 ;;
            *) echo "" ;;
        esac)
        if [[ -n "$port" ]]; then
            svc_port_in_use "$port"
            return $?
        fi
        return 1
    fi
    curl -sf --max-time 3 "$url" > /dev/null 2>&1
    return $?
}

svc_is_healthy_any_llm() {
    llm_healthy || svc_is_healthy "ninfer" || svc_is_healthy "ninfer_us" || svc_is_healthy "qwen_local"
}

# ────────────────────────────────────────────────────────────
# STATUS REPORTING
# ────────────────────────────────────────────────────────────

svc_list_running() {
    log "--- Running Services ---"
    local ports=("$LLM_PORT" 8080 8081 8085 8188 9880 9881 9882 9883)
    for port in "${ports[@]}"; do
        if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
            echo "  ✓ :${port} (LISTENING)"
        else
            echo "  ✗ :${port} (closed)"
        fi
    done
}

svc_status() {
    echo "╔══════════════════════════════════════════════════════════╗"
    echo "║           PRODUCTION PACK — SERVICE STATUS              ║"
    echo "╠══════════════════════════════════════════════════════════╣"

    # VRAM usage
    echo "║ ── GPU Memory ─────────────────────────────────────── ║"
    if command -v nvidia-smi &>/dev/null; then
        local gpu_info
        gpu_info=$(nvidia-smi --query-gpu=name,memory.total,memory.used,memory.free --format=csv,noheader 2>/dev/null || echo "unknown")
        echo "║   Device: $gpu_info | CLOUD_ROUTING=${CLOUD_ROUTING:-unset} ║"
    fi

    # Service health table
    echo "║ ── Services ───────────────────────────────────────── ║"
    local services=("strata:$LLM_PORT" "ninfer:8080" "ninfer-us:8081" "qwen-local:8085" "comfyui:8188"
                    "orpheus:9883" "chatterbox:9882" "cosyvoice:9880" "kokoro:9881")
    for entry in "${services[@]}"; do
        local name="${entry%%:*}"
        local port="${entry##*:}"
        if svc_port_in_use "$port"; then
            local vram
            vram=$(svc_vram_mb "$name" 2>/dev/null || echo "?")
            echo "║   ✓ $name (:$port) — ~${vram} MB VRAM         ║"
        else
            echo "║   ✗ $name (:$port) — stopped                        ║"
        fi
    done

    echo "╚══════════════════════════════════════════════════════════╝"
}

# ────────────────────────────────────────────────────────────
# QUICK COMMAND-FORWARDING
# Allow running as a sub-script:
#   services.sh status
#   services.sh list
#   services.sh transition <phase>
#   services.sh stop-all
#   services.sh safe-start-orpheus (VRAM dance for audio gen)
# ────────────────────────────────────────────────────────────

cmd_status() { svc_status; }

cmd_list() { svc_list_running; }

cmd_transition() {
    local phase="${1:-thinking}"
    svc_transition "$phase"
}

cmd_stop_all() {
    log "Stopping ALL services..."
    touch "${PACK_DIR}/output/.llm_parked" 2>/dev/null || true
    llm_stop
    systemctl stop comfyui.service 2>/dev/null || true
    systemctl stop hermes-stack.service 2>/dev/null || true
    _stop_orpheus_full
    systemctl stop tts-cluster.service chatterbox-tts.service cosyvoice.service kokoro-tts.service 2>/dev/null || true
    sleep 2
    log "All services stopped."
}

# VRAM-safe Orpheus start: ensures no other GPU-heavy service competes.
# This is the "VRAM dance" for audio generation:
#   1. Stop NInfer (or whatever LLM is running)
#   2. Start Orpheus
#   3. Return — caller starts/stops Orpheus as needed, then calls safe-stop-orpheus
cmd_safe_start_orpheus() {
    log "Safe-start Orpheus: checking for VRAM conflicts..."
    # If ComfyUI is running, warn (TTS+ComfyUI = risk)
    if svc_is_healthy "comfyui"; then
        log_warn "ComfyUI is still running — consider switching to video_gen phase first"
    fi
    # Stop any running LLM
    if svc_is_healthy_any_llm; then
        log "Stopping LLM to free VRAM for Orpheus..."
        touch "${PACK_DIR}/output/.llm_parked" 2>/dev/null || true
        llm_stop
    fi
    _start_orpheus_full
    svc_wait_for_ready "orpheus_tts" 30 || {
        log_error "Orpheus failed to start"
        return 1
    }
    log "Orpheus ready. Call safe-stop-orpheus when done."
}

# Stop Orpheus and restore previous state
cmd_safe_stop_orpheus() {
    log "Stopping Orpheus TTS..."
    _stop_orpheus_full
    rm -f "${PACK_DIR}/output/.llm_parked" 2>/dev/null || true
    log "Orpheus stopped. Restarting the local LLM (≈2 min)..."
    llm_start "$LLM_START_TIMEOUT" || log_warn "$LLM_UNIT did not come back"
}

cmd_stop_one_of() {
    for unit in "$@"; do
        _stop_systemd "$unit"
    done
}

# ────────────────────────────────────────────────────────────
# ENTRY POINT — only execute when run directly, not when source-d
# ────────────────────────────────────────────────────────────
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
case "${1:-help}" in
    status)      cmd_status ;;
    list)        cmd_list ;;
    transition)  cmd_transition "${2:-thinking}" ;;
    stop-all)    cmd_stop_all ;;
    safe-start-orpheus)  cmd_safe_start_orpheus ;;
    safe-stop-orpheus)   cmd_safe_stop_orpheus ;;
    help|--help|-h)
        echo "Usage: $0 {status|list|transition <phase>|stop-all|safe-start-orpheus|safe-stop-orpheus}"
        echo ""
        echo "Phases: thinking | video_gen | audio_gen | monitoring"
        echo ""
        echo "Example:"
        echo "  $0 transition video_gen    # Switch to video generation phase"
        ;;
    *)
        echo "Unknown command: $1" >&2
        echo "Run '$0 help' for usage." >&2
        exit 1
        ;;
esac
fi
