#!/usr/bin/env bash
# Roll the live portal back to the build that was live before apply-stalebundle.sh (no restart).
set -euo pipefail
WP="${WP_DIR:-/home/aiciv/witness-portal}"; R="$WP/react-portal"; LIVE="$R/dist"
BAK="$(cat "$WP/.stalebundle-backup-path" 2>/dev/null || true)"
[ -n "$BAK" ] && [ -d "$BAK" ] || { echo "no recorded backup (has apply run?)"; exit 1; }
TS=$(date -u +%Y%m%dT%H%M%SZ)
mv "$LIVE" "$R/dist.rolledback-stalebundle-$TS" && cp -a "$BAK" "$LIVE"
echo "ROLLED BACK to $BAK (fixed build kept at dist.rolledback-stalebundle-$TS)"
if [ -z "${WP_DIR:-}" ]; then curl -s http://localhost:8097/react/ | grep -o 'assets/index-[A-Za-z0-9_-]*\.js' | head -1 | sed 's/^/served: /'; fi
