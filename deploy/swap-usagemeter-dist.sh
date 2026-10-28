#!/usr/bin/env bash
# Frontend-only follow-up (no restart): overlay a rebuilt dist onto the live one, keeping old hashed chunks so open tabs do not 404.
# Backup of the previous dist goes to $WP/backups/dist-swap-<TS>; undo = ./deploy/swap-usagemeter-dist.sh --undo
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; WP="${WP_DIR:-/home/aiciv/witness-portal}"; R="$WP/react-portal"; LIVE="$R/dist"
if [ "${1:-}" = "--undo" ]; then
  BAK="$(cat "$WP/.usagemeter-dist-backup-path")"; [ -d "$BAK" ] || { echo "no backup"; exit 1; }
  TS=$(date -u +%Y%m%dT%H%M%SZ); mv "$LIVE" "$R/dist.rolledback-distswap-$TS" && cp -a "$BAK" "$LIVE"; echo "restored $BAK"; exit 0
fi
SRC="${SRC_DIR:-$(cd "$HERE/.." && pwd)}"; [ -f "$SRC/react-portal/dist/index.html" ] || { echo "SRC has no built dist"; exit 1; }
TS=$(date -u +%Y%m%dT%H%M%SZ); BAK="$WP/backups/dist-swap-$TS"; TMP="$R/dist.new-distswap-$TS"
cp -a "$LIVE" "$BAK"; echo "$BAK" > "$WP/.usagemeter-dist-backup-path"
cp -a "$LIVE" "$TMP"; cp -a "$SRC/react-portal/dist/." "$TMP/"
ASSET=$(grep -o 'assets/index-[A-Za-z0-9_-]*\.js' "$TMP/index.html" | head -1); [ -f "$TMP/$ASSET" ] || { echo "missing $ASSET"; rm -rf "$TMP"; exit 1; }
mv "$LIVE" "$R/dist.old-distswap-$TS" && mv "$TMP" "$LIVE" && rm -rf "$R/dist.old-distswap-$TS"
echo "SWAPPED $ASSET (backup $BAK)"
