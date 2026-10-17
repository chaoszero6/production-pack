#!/bin/bash
# wake.sh — Wake-on-LAN for the render server, from the workstation (Git Bash / Linux / macOS).
#
#   ./wake.sh                 send the magic packet to config/server.env's SERVER_MAC, wait for ssh
#   ./wake.sh AA:BB:CC:DD:EE:FF [ip]   explicit MAC (and IP to wait for)
#   ./wake.sh --no-wait       send only
#
# config/server.env is written by switch-off.sh on the server before it powers off.
# The packet goes to the subnet broadcast on UDP 9 (and 7); the server's NIC must have WoL
# enabled (switch-off.sh sets `ethtool wol g`; the BIOS must allow power-on by PCI-E/LAN).
set -uo pipefail
PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WAIT=true
MAC=""
IP=""
BCAST=""
for a in "$@"; do
    case "$a" in
        --no-wait) WAIT=false ;;
        *:*:*:*:*:*|*-*-*-*-*-*) MAC="$a" ;;
        *.*.*.*) IP="$a" ;;
    esac
done
if [ -f "$PACK_DIR/config/server.env" ]; then
    # shellcheck disable=SC1091
    . "$PACK_DIR/config/server.env"
    MAC="${MAC:-${SERVER_MAC:-}}"
    IP="${IP:-${SERVER_IP:-}}"
    BCAST="${BROADCAST:-}"
fi
IP="${IP:-192.168.0.207}"
BCAST="${BCAST:-${IP%.*}.255}"
if [ -z "$MAC" ]; then
    echo "wake.sh: no MAC — run switch-off.sh on the server once (it records config/server.env), or pass the MAC: ./wake.sh AA:BB:CC:DD:EE:FF"
    exit 2
fi
MAC_HEX=$(echo "$MAC" | tr -d ':-' | tr 'a-f' 'A-F')
if [ ${#MAC_HEX} -ne 12 ]; then
    echo "wake.sh: bad MAC '$MAC'"
    exit 2
fi
echo "[wake] magic packet for $MAC -> $BCAST:9 (and :7)"

send_with_python() {
    python3 - "$MAC_HEX" "$BCAST" <<'PYEOF'
import socket, sys, binascii
mac, bcast = sys.argv[1], sys.argv[2]
pkt = b"\xff" * 6 + binascii.unhexlify(mac) * 16
for port in (9, 7):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for _ in range(3):
        s.sendto(pkt, (bcast, port))
    s.close()
print("sent")
PYEOF
}
send_with_powershell() {
    powershell.exe -NoProfile -Command "
\$mac='$MAC_HEX'; \$bytes=[byte[]](0..5 | ForEach-Object {0xFF}); \$m=[byte[]](0..5 | ForEach-Object {[Convert]::ToByte(\$mac.Substring(\$_*2,2),16)});
\$pkt=\$bytes + (1..16 | ForEach-Object {\$m}) ;
foreach (\$port in 9,7) { \$u=New-Object System.Net.Sockets.UdpClient; \$u.EnableBroadcast=\$true; 1..3 | ForEach-Object { [void]\$u.Send(\$pkt,\$pkt.Length,'$BCAST',\$port) }; \$u.Close() }; 'sent'"
}
if command -v python3 >/dev/null 2>&1 && python3 -c 'import socket' >/dev/null 2>&1; then
    send_with_python
elif command -v powershell.exe >/dev/null 2>&1; then
    send_with_powershell
else
    echo "wake.sh: need python3 or powershell.exe"; exit 2
fi

[ "$WAIT" = "false" ] && exit 0
echo -n "[wake] waiting for ssh on $IP "
for i in $(seq 1 60); do
    if ssh -o ConnectTimeout=3 -o BatchMode=yes "root@$IP" true 2>/dev/null; then
        echo; echo "[wake] $IP is up after ~$((i * 5)) s"
        ssh "root@$IP" 'uptime | cut -c1-40; cd /root/production-pack 2>/dev/null && for u in tts-idle-monitor strata-swift-iq3-xxs; do printf "%-22s %s\n" $u "$(systemctl is-active $u)"; done' 2>/dev/null
        exit 0
    fi
    echo -n "."
    sleep 5
done
echo; echo "[wake] no ssh after 5 min — check WoL in BIOS/NIC, or power it on by hand"
exit 1
