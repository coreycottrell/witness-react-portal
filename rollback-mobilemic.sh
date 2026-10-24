#!/usr/bin/env bash
# Roll the live portal back to the build that was live before apply-mobilemic.sh (no restart).
set -euo pipefail
WP="${WP_DIR:-/home/aiciv/witness-portal}"; R="$WP/react-portal"; LIVE="$R/dist"
BAK="$(cat "$WP/.mobilemic-backup-path" 2>/dev/null || true)"
[ -n "$BAK" ] && [ -d "$BAK" ] || { echo "no recorded backup (has apply run?)"; exit 1; }
TS=$(date -u +%Y%m%dT%H%M%SZ)
mv "$LIVE" "$R/dist.rolledback-mobilemic-$TS" && cp -a "$BAK" "$LIVE"
echo "ROLLED BACK to $BAK (fixed build kept at dist.rolledback-mobilemic-$TS)"
if [ -z "${WP_DIR:-}" ]; then curl -s http://localhost:8097/react/ | grep -o 'assets/index-[A-Za-z0-9_-]*\.js' | head -1 | sed 's/^/served: /'; fi
