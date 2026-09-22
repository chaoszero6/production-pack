#!/bin/bash
# Discord notification helper for Production Pack pipeline.
# Uses Hermes send to post to the #film-maker channel.
#
# Usage:
#   notify.sh "message"
#   notify.sh "message" "title"
#   echo "long message" | notify.sh

CHANNEL="discord:#film-maker"

# Build message
TITLE="${2:-}"
if [ -n "$1" ]; then
    MSG="$1"
else
    MSG="$(cat)"
fi

if [ -n "$TITLE" ]; then
    FULL="**${TITLE}**\n${MSG}"
else
    FULL="$MSG"
fi

# ── Resume-awareness guard ────────────────────────────────────────────────────
# run.sh announces "Phase 1: Pre-production starting" and "Phase 1 complete. Phase 2:
# Clip generation starting" UNCONDITIONALLY, i.e. also on `--resume`, where every Phase 1
# step is skipped because its artifact already exists. Those two banners are therefore
# lies on a resume, and they read as "the pipeline restarted pre-production" — which is
# exactly how they get mistaken for a lost resume. Suppress/rewrite them when the active
# run already has pre-production artifacts. Safe to do here: run.sh execs this script
# fresh on every call, so this takes effect without restarting the pipeline.
PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTIVE_RUN="$(readlink -f "$PACK_DIR/output/run_latest" 2>/dev/null)"

if [ -n "$ACTIVE_RUN" ] && [ -f "$ACTIVE_RUN/story.json" ] && [ -f "$ACTIVE_RUN/shot_list.json" ]; then
    case "$MSG" in
        "Phase 1: Pre-production starting"*)
            echo "[notify] suppressed stale pre-production banner (resume: Phase 1 already complete in $(basename "$ACTIVE_RUN"))" \
                >> "$PACK_DIR/output/notify_suppressed.log"
            exit 0
            ;;
        "Phase 1 complete. Phase 2: Clip generation starting")
            MSG="Resumed run — pre-production already complete, continuing clip generation"
            FULL="**${TITLE}**\n${MSG}"
            ;;
    esac
fi

# Send via Hermes (no LLM needed, uses bot token directly)
hermes send -t "$CHANNEL" "$FULL" 2>/dev/null || {
    echo "[notify] Failed to send Discord message" >&2
}
