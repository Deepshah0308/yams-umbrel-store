#!/bin/sh
# YAMS: qBittorrent shares gluetun's network. Don't let it start until the VPN is
# up and healthy, then install a watchdog that stops the container if the VPN drops.

health() { wget -q -T 3 -O /dev/null http://127.0.0.1:9999 2>/dev/null; }

echo "[yams] Waiting for the VPN before starting qBittorrent..."
until health && [ ! -f /vpnstate/restarting ]; do
  sleep 5
done
echo "[yams] VPN is up. Starting qBittorrent."

mkdir -p /custom-services.d
cp /app/qbittorrent/vpn-guard.sh /custom-services.d/vpn-guard
chown root:root /custom-services.d/vpn-guard
chmod 755 /custom-services.d/vpn-guard

exec /init
