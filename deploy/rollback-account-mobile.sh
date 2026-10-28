#!/usr/bin/env bash
# Undo apply-account-mobile.sh: restore claude_usage.py and dist exactly as the first meter window left them (restarts once).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; . "$HERE/usagemeter-lib.sh"
R="$WP/react-portal"; LIVE="$R/dist"
BAK="$(cat "$WP/.usagemeter2-backup-path" 2>/dev/null || true)"
[ -n "$BAK" ] && [ -f "$BAK/claude_usage.py" ] && [ -d "$BAK/dist" ] || { echo "no recorded backup (has apply-account-mobile run?)"; exit 1; }
TS=$(date -u +%Y%m%dT%H%M%SZ)
cp -a "$BAK/claude_usage.py" "$WP/claude_usage.py.rb" && mv "$WP/claude_usage.py.rb" "$WP/claude_usage.py"
rm -f "$WP/.claude-account-last.json"          # the account-change state file did not exist before this window
[ -d "$LIVE" ] && mv "$LIVE" "$R/dist.rolledback-usagemeter2-$TS"
cp -a "$BAK/dist" "$LIVE"
echo "files restored from $BAK (account/mobile build kept at dist.rolledback-usagemeter2-$TS)"
if [ -z "${SKIP_RESTART:-}" ]; then
  restart_portal && echo "post-rollback: /react=$(http_code "$BASE/react")"
fi
echo ROLLED_BACK
