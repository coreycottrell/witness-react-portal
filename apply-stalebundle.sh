#!/usr/bin/env bash
# Apply the stale-bundle self-heal build (ticket 3499) to the LIVE witness portal. NO server restart needed or done:
# portal_server.py serves react-portal/dist from disk per request (REACT_DIST, portal_server.py:57).
# Usage:  ./apply-stalebundle.sh            (apply)      ./rollback-stalebundle.sh   (undo)
# WP_DIR may be overridden to dry-run against a scratch copy.
set -euo pipefail
WP="${WP_DIR:-/home/aiciv/witness-portal}"
R="$WP/react-portal"; STAGED="$R/dist-staged-stalebundle-20261006"; LIVE="$R/dist"
NEWASSET="index-DBtj74LB.js"; EXPECT_SHA="d681858754a311cc696c98754ffa5e515baa7650102be28a549d81ce320ec066"
[ -f "$STAGED/assets/$NEWASSET" ] || { echo "staged build missing"; exit 1; }
got=$(sha256sum "$STAGED/assets/$NEWASSET" | cut -d' ' -f1)
[ "$got" = "$EXPECT_SHA" ] || { echo "staged build sha mismatch ($got) - refusing"; exit 1; }
if grep -q "$NEWASSET" "$LIVE/index.html"; then echo "already applied"; exit 0; fi
TS=$(date -u +%Y%m%dT%H%M%SZ); BAK="$R/dist.bak-pre-stalebundle-$TS"; TMP="$R/dist.new-stalebundle-$TS"
cp -a "$LIVE" "$BAK"                                  # 1. backup of exactly what is live now
echo "$BAK" > "$WP/.stalebundle-backup-path"
cp -a "$LIVE" "$TMP"                                  # 2. keep OLD hashed chunks so already-open tabs don't 404
cp -a "$STAGED/." "$TMP/"                             #    overlay new index.html + new assets
mv "$LIVE" "$R/dist.old-stalebundle-$TS" && mv "$TMP" "$LIVE"   # 3. swap (two renames, sub-second window)
rm -rf "$R/dist.old-stalebundle-$TS" 2>/dev/null || true        # 3b. (identical to $BAK, which is retained)
grep -q "$NEWASSET" "$LIVE/index.html" && echo "APPLIED. backup: $BAK"
if [ -z "${WP_DIR:-}" ]; then
  curl -s http://localhost:8097/react/ | grep -o 'assets/index-[A-Za-z0-9_-]*\.js' | head -1 | sed 's/^/served: /'
fi
echo "Operators: hard-refresh the portal on the phone (content-hashed filename, normal reload should also pick it up)."
