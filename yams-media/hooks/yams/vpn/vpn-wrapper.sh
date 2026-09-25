#!/bin/sh
# YAMS: runs gluetun only once a WireGuard config exists, and restarts it in place
# (same container, same network namespace) whenever the dashboard saves a new one.
# Before a restart it raises a flag so qBittorrent shuts itself down first.

CONF=/gluetun/wireguard/wg0.conf
FLAG=/gluetun/restarting
pid=""

stop() {
  [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null && wait "$pid" 2>/dev/null
  exit 0
}
trap stop TERM INT

nap() { sleep "$1" & wait $!; }
checksum() { md5sum "$CONF" 2>/dev/null | cut -d' ' -f1; }

while true; do
  if [ ! -s "$CONF" ]; then
    echo "[yams] No VPN connected yet. Open YAMS in Umbrel and paste your WireGuard file."
    while [ ! -s "$CONF" ]; do nap 3; done
  fi

  sum=$(checksum)
  rm -f "$FLAG"
  echo "[yams] Starting VPN"
  /gluetun-entrypoint &
  pid=$!
  started=$(date +%s)

  while kill -0 "$pid" 2>/dev/null; do
    nap 3
    if [ "$(checksum)" != "$sum" ]; then
      echo "[yams] VPN settings changed. Pausing downloads, then reconnecting."
      touch "$FLAG"
      nap 12
      kill -TERM "$pid" 2>/dev/null
      break
    fi
  done
  wait "$pid" 2>/dev/null
  pid=""

  # If gluetun crashed straight away (usually a bad config), don't spin too fast.
  if [ $(( $(date +%s) - started )) -lt 20 ]; then
    echo "[yams] VPN stopped quickly. Check the WireGuard file in the YAMS dashboard."
    touch "$FLAG"
    nap 15
  fi
done
