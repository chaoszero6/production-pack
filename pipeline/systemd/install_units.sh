#!/bin/bash
# Install / refresh the pack's systemd units and exclusivity drop-ins on the
# production server (run as root on 207):
#   pipeline/systemd/install_units.sh
#
# Installs:
#   *.service                       → /etc/systemd/system/
#   dropins/<unit>.d__<file>.conf   → /etc/systemd/system/<unit>.d/<file>.conf
# then daemon-reloads, enables the monitor and prints the exclusivity graph.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for unit in "$HERE"/*.service; do
    name=$(basename "$unit")
    install -m 0644 "$unit" "/etc/systemd/system/$name"
    echo "installed /etc/systemd/system/$name"
done
for conf in "$HERE"/dropins/*.conf; do
    base=$(basename "$conf")                 # comfyui.service.d__10-exclusive.conf
    dir="/etc/systemd/system/${base%%__*}"   # comfyui.service.d
    file="${base#*__}"                       # 10-exclusive.conf
    # Only install a drop-in for a unit that exists on this machine.
    unit="${dir%.d}"; unit="$(basename "$unit")"
    if ! systemctl list-unit-files "$unit" --no-legend 2>/dev/null | grep -q .; then
        echo "skip $base ($unit not installed here)"; continue
    fi
    install -d -m 0755 "$dir"
    install -m 0644 "$conf" "$dir/$file"
    echo "installed $dir/$file"
done
systemctl daemon-reload
systemctl enable tts-idle-monitor.service >/dev/null 2>&1 || true
systemctl restart tts-idle-monitor.service
echo
echo "exclusive GPU owners (each Conflicts= the others):"
for u in comfyui.service strata-swift-iq3-xxs.service tts-cluster.service; do
    printf "  %-30s %-9s conflicts: %s\n" "$u" "$(systemctl is-active "$u" 2>/dev/null)" \
        "$(systemctl show -p Conflicts --value "$u" 2>/dev/null | tr ' ' '\n' | grep -vE 'shutdown|^$' | tr '\n' ' ')"
done
