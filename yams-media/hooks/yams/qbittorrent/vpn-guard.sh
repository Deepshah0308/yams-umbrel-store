#!/usr/bin/with-contenv sh
# YAMS kill switch (second layer, on top of gluetun's firewall):
# stop qBittorrent if the VPN tunnel disappears or the VPN is about to restart.
fails=0
while true; do
  sleep 3
  if [ -f /vpnstate/restarting ]; then
    echo "[yams] VPN is reconnecting. Stopping qBittorrent until it's back."
    /run/s6/basedir/bin/halt
    exit 0
  fi
  if grep -q "tun0:" /proc/net/dev 2>/dev/null; then
    fails=0
  else
    fails=$((fails + 1))
  fi
  if [ "$fails" -ge 3 ]; then
    echo "[yams] VPN tunnel is gone. Stopping qBittorrent so nothing leaks."
    /run/s6/basedir/bin/halt
    exit 0
  fi
done
