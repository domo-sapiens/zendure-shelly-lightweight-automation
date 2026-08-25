#!/usr/bin/env bash
# Regenerate docs/screenshots/ from the live dashboard.
#
#   tools/screenshots.sh [dashboard-url]
#
# Headless Firefox, because it is what macOS boxes tend to already have. Two
# wrinkles worth knowing:
#
#  - Firefox refuses to start a second instance while one is running, even with
#    a separate profile. Running from a *copy* of the app bundle gives it its
#    own lock, so an open browser is not disturbed.
#  - It screenshots on the load event and has no wait flag. The dashboard
#    inlines its first payload into the HTML precisely so the page is fully
#    populated by then; without that every capture came out empty.
set -euo pipefail

URL="${1:-http://zendure-log:8088}"
OUT="$(cd "$(dirname "$0")/.." && pwd)/docs/screenshots"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

SRC_APP="/Applications/Firefox.app"
[ -d "$SRC_APP" ] || { echo "Firefox not found at $SRC_APP"; exit 1; }
echo "preparing an isolated Firefox (so a running one is left alone)..."
cp -R "$SRC_APP" "$WORK/FF.app"
FF="$WORK/FF.app/Contents/MacOS/firefox"
mkdir -p "$OUT"

shoot () { # name width height url
  MOZ_NO_REMOTE=1 "$FF" --headless --profile "$WORK/profile" \
    --window-size="$2,$3" --screenshot="$WORK/$1.png" "$4" >/dev/null 2>&1 || true
  [ -s "$WORK/$1.png" ] || { echo "  FAILED: $1"; return 1; }
}

# One tall capture of the 7-day view, then crop the feature panels out of it so
# every shot shows the same moment and they cannot drift apart.
echo "capturing $URL ..."
shoot full 1500 2700 "$URL/?window=10080"
shoot mobile 430 1400 "$URL/?window=1440"

crop () { sips -c "$3" 1500 --cropOffset "$2" 0 "$WORK/full.png" --out "$OUT/$1" >/dev/null; }
crop dashboard-overview.png     0 2700
crop energy-flow.png           40  800
crop charts.png               860 1040
crop inverter-efficiency.png 2185  500
cp "$WORK/mobile.png" "$OUT/mobile.png"

# Normalise width so the README renders consistently and the repo stays light.
for f in dashboard-overview charts energy-flow inverter-efficiency; do
  sips --resampleWidth 1200 "$OUT/$f.png" >/dev/null
done

echo "written to docs/screenshots:"
for f in "$OUT"/*.png; do
  printf "  %-28s %sKB\n" "$(basename "$f")" "$(( $(stat -f%z "$f") / 1024 ))"
done
echo
echo "Crop offsets are tuned to the current card order; adjust them here if the"
echo "dashboard layout changes."
