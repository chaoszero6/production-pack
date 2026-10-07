#!/bin/bash
# Discord notification helper for Production Pack pipeline.
# Uses Hermes send to post to the #film-maker channel.
#
# Usage:
#   notify.sh "message" "title"
#   notify.sh "message" "title" "/path/to/video.mp4"

CHANNEL="discord:#film-maker"

TITLE="${2:-}"
VIDEO="${3:-}"
if [ -n "$1" ]; then
    MSG="$1"
else
    MSG="$(cat)"
fi

# ── Resume-awareness guard ────────────────────────────────────────────────────
PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTIVE_RUN="$(readlink -f "$PACK_DIR/output/run_latest" 2>/dev/null)"

if [ -n "$ACTIVE_RUN" ] && [ -f "$ACTIVE_RUN/story.json" ] && [ -f "$ACTIVE_RUN/shot_list.json" ]; then
    case "$MSG" in
        "Phase 1: Pre-production starting"*)
            echo "[notify] suppressed stale pre-production banner (resume)" \
                >> "$PACK_DIR/output/notify_suppressed.log"
            exit 0
            ;;
        "Phase 1 complete. Phase 2: Clip generation starting")
            MSG="Resumed run — pre-production already complete, continuing clip generation"
            ;;
    esac
fi

# ── Build formatted message ──────────────────────────────────────────────────
# Convert literal \n sequences to actual newlines
MSG=$(printf '%b' "$MSG")

if [ -n "$TITLE" ]; then
    FULL=$(printf '**%s**\n%s' "$TITLE" "$MSG")
else
    FULL="$MSG"
fi

# Append video as attachment if provided
if [ -n "$VIDEO" ] && [ -f "$VIDEO" ]; then
    FULL="${FULL}
MEDIA:${VIDEO}"
fi

hermes send -t "$CHANNEL" "$FULL" 2>/dev/null || {
    echo "[notify] Failed to send Discord message" >&2
}
