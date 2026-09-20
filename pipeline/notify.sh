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

# Send via Hermes (no LLM needed, uses bot token directly)
hermes send -t "$CHANNEL" "$FULL" 2>/dev/null || {
    echo "[notify] Failed to send Discord message" >&2
}
