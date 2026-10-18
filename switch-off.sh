#!/bin/bash
# switch-off.sh — run ON the render server (207): park every pack service, record the NIC's
# MAC for Wake-on-LAN, then power the box off through the UPS monitor (`upsmon -c fsd`, the
# forced-shutdown path the UPS is wired for) — plain `shutdown -h now` only if upsmon is absent.
#
#   ./switch-off.sh            stop run + agents + renders + monitor, park the GPU, record MAC, power off
#   ./switch-off.sh --dry-run  do everything except the power-off
#
# Wake it again from the workstation with ./wake.sh (reads config/server.env).
set -uo pipefail
PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY=false
[ "${1:-}" = "--dry-run" ] && DRY=true
log() { echo "[switch-off $(date '+%H:%M:%S')] $*"; }

# 1. record the MAC + IP of the interface that carries the default route (for wake.sh)
IFACE=$(ip route show default 2>/dev/null | awk '/default/ {print $5; exit}')
MAC=$(cat "/sys/class/net/${IFACE:-x}/address" 2>/dev/null || true)
IP=$(ip -4 addr show "${IFACE:-x}" 2>/dev/null | awk '/inet / {print $2; exit}' | cut -d/ -f1)
if [ -n "$MAC" ]; then
    mkdir -p "$PACK_DIR/config"
    {
        echo "# written by switch-off.sh on $(hostname) $(date '+%Y-%m-%d %H:%M:%S') — read by wake.sh"
        echo "SERVER_MAC=$MAC"
        echo "SERVER_IP=${IP:-192.168.0.207}"
        echo "SERVER_IFACE=${IFACE}"
        echo "BROADCAST=${IP%.*}.255"
    } > "$PACK_DIR/config/server.env"
    log "Wake-on-LAN target recorded: $MAC ($IP on $IFACE) -> config/server.env"
    # make sure the NIC will listen for the magic packet after power-off
    if command -v ethtool >/dev/null 2>&1; then
        ethtool -s "$IFACE" wol g 2>/dev/null && log "ethtool: WoL 'g' (magic packet) enabled on $IFACE" \
            || log "WARNING: could not enable WoL on $IFACE (check BIOS 'Power On By PCI-E' too)"
    fi
else
    log "WARNING: could not read the NIC MAC — wake.sh will need SERVER_MAC set by hand"
fi

# 2. stop the pack (run, agent turns, renders, watchers, LLM monitor) and park every GPU owner
if [ -x "$PACK_DIR/stop.sh" ]; then
    "$PACK_DIR/stop.sh" --gpu none
fi
for u in comfyui.service tts-cluster.service tts-idle-monitor.service strata-swift-iq3-xxs.service strata-orca-iq3-xxs.service; do
    systemctl stop "$u" 2>/dev/null || true
done
systemctl reset-failed tts-cluster.service 2>/dev/null || true
log "GPU: $(nvidia-smi --query-gpu=memory.used --format=csv,noheader 2>/dev/null || echo '?') used"

# 3. flush and power off
sync
if [ "$DRY" = "true" ]; then
    log "dry run — not powering off"
    exit 0
fi
[ -x "$PACK_DIR/pipeline/notify.sh" ] && bash "$PACK_DIR/pipeline/notify.sh" "Render server switching off (switch-off.sh)" "Power" >/dev/null 2>&1
sleep 2
# The operator's own power-off script (/root/desktop/switch-off.sh = `upsmon -c fsd`) is the
# authority when present; the same UPS forced-shutdown is issued directly otherwise.
if [ -x /root/desktop/switch-off.sh ] || [ -f /root/desktop/switch-off.sh ]; then
    log "powering off via /root/desktop/switch-off.sh"
    bash /root/desktop/switch-off.sh
elif command -v upsmon >/dev/null 2>&1; then
    log "powering off via upsmon -c fsd"
    upsmon -c fsd
else
    log "upsmon not found — shutdown -h now"
    shutdown -h now
fi
