#!/usr/bin/env bash
# Roll the live portal back to exactly what ran before apply-usagemeter.sh (restores source + dist, restarts once).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; . "$HERE/usagemeter-lib.sh"
R="$WP/react-portal"; LIVE="$R/dist"
BAK="$(cat "$WP/.usagemeter-backup-path" 2>/dev/null || true)"
[ -n "$BAK" ] && [ -f "$BAK/portal_server.py" ] && [ -d "$BAK/dist" ] || { echo "no recorded backup (has apply run?)"; exit 1; }
TS=$(date -u +%Y%m%dT%H%M%SZ)
cp -a "$BAK/portal_server.py" "$WP/portal_server.py.rb" && mv "$WP/portal_server.py.rb" "$WP/portal_server.py"
rm -f "$WP/claude_usage.py"
[ -d "$LIVE" ] && mv "$LIVE" "$R/dist.rolledback-usagemeter-$TS"
cp -a "$BAK/dist" "$LIVE"
echo "files restored from $BAK (meter build kept at dist.rolledback-usagemeter-$TS)"
if [ -z "${SKIP_RESTART:-}" ]; then
  restart_portal && echo "post-rollback: /react=$(http_code "$BASE/react")"
fi
echo ROLLED_BACK
