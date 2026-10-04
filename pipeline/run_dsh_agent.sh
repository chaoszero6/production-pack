#!/bin/bash
# ════════════════════════════════════════════════════════════════
# Production Pack — Phase-Aware Agent Launcher
# ════════════════════════════════════════════════════════════════
#
# Wraps every dsh agent call with a phase transition guarantee.
# Maps each agent preset → required service phase, then ensures
# the correct services are running before launching the agent.
#
# This is the single integration point between the pipeline and
# the VRAM-aware service manager. Both run.sh (direct) and any
# subprocess calling agents should use this wrapper.
#
# Usage:
#   source pipeline/run_dsh_agent.sh        # loads run_preset_agent()
#   run_preset_agent <preset> <prompt_file> <output_file> [args...]
#
# Or run standalone for debugging:
#   pipeline/run_dsh_agent.sh status        # show current state
#   pipeline/run_dsh_agent.sh list-phases   # show preset→phase mapping
#
# Environment inherited from caller:
#   CLOUD_ROUTING     1 or 0
#   RUN_DIR           Output directory path
#   DSH_DIR           Harness checkout path
#   PACK_DIR          Production pack root

set -euo pipefail

PACK_DIR="${PACK_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
DSH_DIR="${DSH_DIR:-/root/desktop/deepseek-harness}"
SVC_SCRIPT="${PACK_DIR}/pipeline/services.sh"
LOG_FILE="/tmp/agent_launcher_$$"

# ────────────────────────────────────────────────────────────
# PRESET → PHASE MAPPING
# Each agent preset belongs to one of four phases:
#   thinking    = reasoning, planning, analysis (text-only LLM work)
#   video_gen   = image/video generation (ComfyUI only)
#   audio_gen   = TTS synthesis (TTS engines only)
#   monitoring  = QA inspection (LLM + some vision; treated as thinking)
# ────────────────────────────────────────────────────────────

declare -A AGENT_PHASE=(
    # ── Reasoning/Planning agents → THINKING phase ──
    ["story-creator"]="thinking"
    ["director"]="thinking"
    ["screenplay-reviewer"]="thinking"

    # ── Image generation → VIDEO_GEN phase (uses ComfyUI + Qwen Image) ──
    ["character-designer"]="video_gen"
    ["location-designer"]="video_gen"
    ["image-generator"]="video_gen"
    ["storyboard-artist"]="video_gen"

    # ── Video generation → VIDEO_GEN phase (uses ComfyUI + H3) ──
    ["video-generator"]="video_gen"

    # ── Audio synthesis → AUDIO_GEN phase (uses TTS engines) ──
    ["audio-producer"]="audio_gen"
    ["music-composer"]="audio_gen"

    # ── QA / Vision → THINKING phase (needs LLM but no generation) ──
    ["qa-inspector"]="thinking"

    # ── Post-production → MONITORING phase (lightweight text processing) ──
    ["subtitle-generator"]="monitoring"
    ["post-production-editor"]="monitoring"
    ["pipeline-orchestrator"]="monitoring"
)

# Map individual TTS ports back to their service name (for VRAM safety checks)
declare -A TTS_PORTS=("9882"="chatterbox" "9880"="cosyvoice" "9883"="orpheus" "9881"="kokoro")

# Get the required phase for a given preset
agent_required_phase() {
    local preset="$1"
    echo "${AGENT_PHASE[$preset]:-thinking}"  # default to thinking if unknown
}

# Check that the current service state matches the expected phase
verify_phase_compliance() {
    local phase="$1"
    local errors=""

    case "$phase" in
        video_gen)
            # Must have ComfyUI, must NOT have any LLM
            if ! ss -tlnp 2>/dev/null | grep -q ':8188 '; then
                errors+="⚠ ComfyUI not running during video_gen phase\n"
            fi
            for llm_port in 8080 8081 8085; do
                if ss -tlnp 2>/dev/null | grep -q ":${llm_port} "; then
                    errors+="⚠ Local LLM on :${llm_port} running during video_gen!\n"
                fi
            done
            ;;
        audio_gen)
            # Must NOT have ComfyUI or LLMs active
            if ss -tlnp 2>/dev/null | grep -q ':8188 '; then
                errors+="⚠ ComfyUI still running during audio_gen!\n"
            fi
            for llm_port in 8080 8081 8085; do
                if ss -tlnp 2>/dev/null | grep -q ":${llm_port} "; then
                    errors+="⚠ LLM on :${llm_port} running during audio_gen!\n"
                fi
            done
            ;;
        thinking|monitoring)
            # May have LLM running (under local mode), no generation required
            # No conflicts to check here
            ;;
    esac

    if [[ -n "$errors" ]]; then
        printf "%b" "$errors"
        return 1
    fi
    return 0
}

# Main agent launcher — called by run.sh and other pipeline scripts
run_preset_agent() {
    local preset="$1"
    shift
    local prompt_file="${1:-}"
    local output_file="${2:-}"
    shift 2 || true  # consume prompt_file and output_file args

    local phase
    phase=$(agent_required_phase "$preset")

    log() {
        local ts
        ts=$(date '+%H:%M:%S')
        echo "[$ts] [agent:$preset/$phase] $*" | tee -a "$LOG_FILE" 2>&1 || echo "[$ts] [agent:$preset/$phase] $*"
    }

    log "Starting agent '$preset' (phase: $phase)"

    # Cloud routing shortcut: under cloud mode, skip all local service management
    if [[ "${CLOUD_ROUTING:-1}" == "1" ]] && [[ "$phase" != "video_gen" ]]; then
        log "Cloud routing: skipping local service management"
    elif [[ "${CLOUD_ROUTING:-1}" == "1" ]] && [[ "$phase" == "video_gen" ]]; then
        log "Cloud routing + video_gen: ensuring ComfyUI is healthy"
    fi

    # Transition to the required phase if not already compliant
    local compliance_check
    compliance_check=$(verify_phase_compliance "$phase" 2>&1) || true

    if [[ -n "$compliance_check" ]]; then
        log "Phase compliance issues detected — transitioning..."
        log "$compliance_check"
        if [[ -f "$SVC_SCRIPT" ]]; then
            source "$SVC_SCRIPT"
            svc_transition "$phase" 2>/dev/null || {
                log "Warning: Phase transition had issues — continuing anyway"
            }
        fi
    else
        # Quick check: if phase doesn't require starting anything special
        # (e.g., cloud thinking needs nothing), verify it's fine and move on
        if [[ "${CLOUD_ROUTING:-1}" == "1" ]] && [[ "$phase" == "thinking" ]]; then
            log "Cloud mode, thinking phase — no local services needed, proceeding"
        elif [[ "${CLOUD_ROUTING:-1}" == "1" ]] && [[ "$phase" == "monitoring" ]]; then
            log "Cloud mode, monitoring phase — no local services needed, proceeding"
        elif [[ "$phase" == "video_gen" ]]; then
            # Ensure ComfyUI is up regardless of cloud/local mode
            if [[ -f "$SVC_SCRIPT" ]]; then
                source "$SVC_SCRIPT"
                if ! svc_is_healthy "comfyui"; then
                    _restart_systemd "comfyui.service" 2>/dev/null || true
                    for i in $(seq 1 60); do
                        curl -sf --max-time 3 "http://127.0.0.1:8188/api/system_stats" > /dev/null 2>&1 && break
                        sleep 1
                    done
                    log "ComfyUI ensured healthy"
                else
                    log "ComfyUI already healthy"
                fi
            fi
        elif [[ "$phase" == "audio_gen" ]]; then
            # Under cloud mode, we still need TTS engines available
            log "Audio phase — TTS engines should be available"
            # Verify at least one TTS port is open
            local tts_any_up=false
            for tport in 9880 9881 9882 9883; do
                ss -tlnp 2>/dev/null | grep -q ":${tport} " && tts_any_up=true && break
            done
            if ! $tts_any_up; then
                log "WARNING: No TTS engine responding — may need manual start"
            fi
        fi
    fi

    # Build the actual dsh command
    local patch_file
    patch_file="$PACK_DIR/.dsh/.agent-presets/$preset/agent.cordis.yml"

    if [[ ! -f "$patch_file" ]]; then
        log "ERROR: Preset config not found: $patch_file"
        return 1
    fi

    # Execute the agent
    log "Running: pnpm dsh --profile headless --patch $patch_file"
    log "Prompt file: $prompt_file"
    log "Output file: $output_file"

    local raw_output="${RUN_DIR:-/tmp}/${preset}_raw_$$.txt"
    local err_output="${RUN_DIR:-/tmp}/${preset}_stderr_$$.log"

    cd "$DSH_DIR"
    local agent_exit=0

    if [[ -n "$prompt_file" ]] && [[ -f "$prompt_file" ]]; then
        local prompt_text
        prompt_text=$(cat "$prompt_file")
        pnpm dsh --profile headless \
            --patch "$patch_file" \
            "$prompt_text" \
            > "$raw_output" 2>"$err_output" || agent_exit=$?
    else
        # Fallback: use stdin
        pnpm dsh --profile headless \
            --patch "$patch_file" \
            <&0 \
            > "$raw_output" 2>"$err_output" || agent_exit=$?
    fi

    # Log stderr to pipeline log if any
    if [[ -s "$err_output" ]]; then
        cat "$err_output" >> "${RUN_DIR:-/tmp}/pipeline.log" 2>/dev/null || true
    fi

    # Restore working directory
    if [[ -n "${RUN_DIR:-}" ]]; then
        cd - >/dev/null 2>&1 || true
    fi

    if [[ $agent_exit -ne 0 ]]; then
        log "WARNING: Agent '$preset' exited with code $agent_exit"
        if [[ -f "$err_output" ]] && grep -qiE '(402|429|OOM|cuda error)' "$err_output" 2>/dev/null; then
            log "Potential resource issue detected in stderr"
        fi
    fi

    log "Agent '$preset' done (exit=$agent_exit)"
    return $agent_exit
}

# Handle direct invocation for debugging/status
# ────────────────────────────────────────────────────────────
# ENTRY POINT — only execute when run directly, not when source-d
# ────────────────────────────────────────────────────────────
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
case "${1:-help}" in
    help|--help|-h)
        echo "Usage: $0 {help|list-phases|status|transition <phase>|check-agent <preset>}"
        echo ""
        echo "Commands:"
        echo "  help                  Show this message"
        echo "  list-phases           Show preset→phase mapping"
        echo "  status                Show current service state"
        echo "  transition <phase>    Force a phase transition"
        echo "  check-agent <preset>  Show what phase an agent needs"
        ;;
    list-phases)
        echo "╔══════════════════════════════════════════════════════════╗"
        echo "║              PRESET → PHASE MAPPING                      ║"
        echo "╠══════════════════════════════════════════════════════════╣"
        for preset in "${!AGENT_PHASE[@]}"; do
            printf "║ %-24s → %-12s ║\n" "$preset" "${AGENT_PHASE[$preset]}"
        done | sort
        echo "╚══════════════════════════════════════════════════════════╝"
        ;;
    status)
        if [[ -f "$SVC_SCRIPT" ]]; then
            source "$SVC_SCRIPT"
            svc_status
        else
            echo "Services script not found at: $SVC_SCRIPT"
        fi
        ;;
    transition)
        if [[ -f "$SVC_SCRIPT" ]]; then
            source "$SVC_SCRIPT"
            svc_transition "${2:-thinking}"
        fi
        ;;
    check-agent)
        local preset="${2:-}"
        if [[ -z "$preset" ]]; then
            echo "Usage: $0 check-agent <preset-name>" >&2
            exit 1
        fi
        local phase
        phase=$(agent_required_phase "$preset")
        echo "Agent '$preset' requires phase: $phase"
        local desc=""
        case "$phase" in
            thinking) desc="Text reasoning via LLM (cloud or local)" ;;
            video_gen) desc="ComfyUI generation, all LLMs stopped" ;;
            audio_gen) desc="TTS engines only, ComfyUI/LLM stopped" ;;
            monitoring) desc="Lightweight tasks, minimal services" ;;
        esac
        echo "  → This means: $desc"
        ;;
    *)
        echo "Unknown command: $1" >&2
        echo "Run '$0 help' for usage." >&2
        exit 1
        ;;
esac
fi
