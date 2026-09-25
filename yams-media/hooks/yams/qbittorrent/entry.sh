#!/bin/sh
# YAMS: qBittorrent shares gluetun's network. It only starts once the VPN tunnel
# (tun0) exists, and a watchdog stops it again if the tunnel disappears.
# tun0 only exists while gluetun is running, and gluetun's firewall blocks all
# non-VPN traffic from the moment it starts, so nothing can leak.

vpn_up() {
  grep -q "tun0:" /proc/net/dev 2>/dev/null && [ ! -f /vpnstate/restarting ]
}

echo "[yams] Waiting for the VPN tunnel before starting qBittorrent..."
waited=0
until vpn_up; do
  sleep 3
  waited=$((waited + 3))
  if [ $((waited % 60)) -eq 0 ]; then
    echo "[yams] Still waiting for the VPN tunnel (${waited}s). Check the VPN logs in the YAMS dashboard."
  fi
done
echo "[yams] VPN tunnel is up. Starting qBittorrent."

mkdir -p /custom-services.d
cp /yams-app/qbittorrent/vpn-guard.sh /custom-services.d/vpn-guard
chown -R root:root /custom-services.d
chmod 755 /custom-services.d /custom-services.d/vpn-guard

exec /init
