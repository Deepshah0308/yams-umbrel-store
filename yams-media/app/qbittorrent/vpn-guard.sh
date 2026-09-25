#!/usr/bin/with-contenv sh
# YAMS kill switch (second layer, on top of gluetun's firewall):
# stop qBittorrent entirely if the VPN is unhealthy or about to restart.
fails=0
while true; do
  sleep 5
  if [ -f /vpnstate/restarting ]; then
    echo "[yams] VPN is reconnecting. Stopping qBittorrent until it's back."
    /run/s6/basedir/bin/halt
    exit 0
  fi
  if wget -q -T 3 -O /dev/null http://127.0.0.1:9999 2>/dev/null; then
    fails=0
  else
    fails=$((fails + 1))
  fi
  if [ "$fails" -ge 3 ]; then
    echo "[yams] VPN is down. Stopping qBittorrent so nothing leaks."
    /run/s6/basedir/bin/halt
    exit 0
  fi
done
