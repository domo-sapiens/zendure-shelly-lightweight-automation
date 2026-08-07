#!/usr/bin/env bash
# Probe both devices and dump their state into notes/ (gitignored).
# Usage: tools/discover.sh <shelly-ip> <zendure-ip> [shelly-password]
set -uo pipefail

SHELLY="${1:?usage: discover.sh <shelly-ip> <zendure-ip> [shelly-password]}"
ZENDURE="${2:?usage: discover.sh <shelly-ip> <zendure-ip> [shelly-password]}"
PASS="${3:-}"

OUT="$(cd "$(dirname "$0")/.." && pwd)/notes"
mkdir -p "$OUT"

AUTH=()
[ -n "$PASS" ] && AUTH=(--digest -u "admin:$PASS")

probe() { # name url
  printf '  %-22s ' "$1"
  # ${AUTH[@]+...} keeps an empty array from tripping `set -u` on bash 3.2.
  if curl -fsS --max-time 5 ${AUTH[@]+"${AUTH[@]}"} "$2" -o "$OUT/$1.json"; then
    echo "ok -> notes/$1.json"
  else
    echo "FAILED"
  fi
}

echo "Shelly Pro 3EM @ $SHELLY"
probe shelly-info      "http://$SHELLY/rpc/Shelly.GetDeviceInfo"
probe shelly-config    "http://$SHELLY/rpc/Shelly.GetConfig"
probe shelly-status    "http://$SHELLY/rpc/Shelly.GetStatus"
probe shelly-scripts   "http://$SHELLY/rpc/Script.List"

echo "Zendure SolarFlow 800 Plus @ $ZENDURE"
probe zendure-report   "http://$ZENDURE/properties/report"

echo
echo "Key values:"
echo -n "  meter profile:  "; grep -o '"profile":"[a-z]*"' "$OUT/shelly-config.json" 2>/dev/null | head -1 || echo "?"
echo -n "  firmware:       "; grep -o '"ver":"[^"]*"' "$OUT/shelly-info.json" 2>/dev/null | head -1 || echo "?"
echo -n "  auth enabled:   "; grep -o '"auth_en":[a-z]*' "$OUT/shelly-info.json" 2>/dev/null | head -1 || echo "?"
echo
echo "notes/*.json contain serial numbers and MACs and are gitignored."
