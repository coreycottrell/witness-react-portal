#!/usr/bin/env bash
# Apply the Claude usage meter (commit on feat/claude-usage-meter-20261028) to the LIVE witness portal.
# Backend change => ONE portal restart (the portal is unsupervised; the lib relaunches it with its old env).
# Usage: SRC_DIR=<checkout of the branch, already built: react-portal/dist> ./deploy/apply-usagemeter.sh
#        ./deploy/rollback-usagemeter.sh   (undo)
# WP_DIR / PORT / SKIP_RESTART=1 may be set to dry-run against a scratch copy.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; . "$HERE/usagemeter-lib.sh"
SRC="${SRC_DIR:-$(cd "$HERE/.." && pwd)}"
R="$WP/react-portal"; LIVE="$R/dist"
BASE_SHA="${BASE_SHA:-709ef9cc014944c3d54f12d243e79075bbef52ce9e511ccd75e5aa93040d4413}"   # portal_server.py at 0da4689 == running source
[ -f "$SRC/react-portal/dist/index.html" ] || { echo "SRC has no built dist (run npm ci && npm run build)"; exit 1; }
[ "$(sha256sum "$WP/portal_server.py" | cut -d' ' -f1)" = "$BASE_SHA" ] || { echo "live portal_server.py is not the expected base - refusing"; exit 1; }
[ -e "$WP/claude_usage.py" ] && { echo "claude_usage.py already present - already applied?"; exit 1; }
python3 -m py_compile "$SRC/portal_server.py" "$SRC/claude_usage.py"
TS=$(date -u +%Y%m%dT%H%M%SZ); BAK="$WP/backups/usagemeter-$TS"; TMP="$R/dist.new-usagemeter-$TS"
mkdir -p "$BAK"
cp -a "$WP/portal_server.py" "$BAK/portal_server.py"      # 1. backups of exactly what is live now
cp -a "$LIVE" "$BAK/dist"
echo "$BAK" > "$WP/.usagemeter-backup-path"
cp -a "$LIVE" "$TMP"; cp -a "$SRC/react-portal/dist/." "$TMP/"   # 2. stage: keep OLD hashed chunks so open tabs do not 404
ASSET=$(grep -o 'assets/index-[A-Za-z0-9_-]*\.js' "$TMP/index.html" | head -1)
[ -f "$TMP/$ASSET" ] || { echo "staged index.html points at missing $ASSET"; rm -rf "$TMP"; exit 1; }
cp "$SRC/claude_usage.py" "$WP/claude_usage.py.new" && cp "$SRC/portal_server.py" "$WP/portal_server.py.new"
mv "$LIVE" "$R/dist.old-usagemeter-$TS" && mv "$TMP" "$LIVE"      # 3. swap (sub-second)
mv "$WP/claude_usage.py.new" "$WP/claude_usage.py" && mv "$WP/portal_server.py.new" "$WP/portal_server.py"
rm -rf "$R/dist.old-usagemeter-$TS"                              #    identical to $BAK/dist
echo "STAGED. backup: $BAK  asset: $ASSET"
if [ -z "${SKIP_RESTART:-}" ]; then
  restart_portal || { echo "RESTART FAILED - rolling back"; "$HERE/rollback-usagemeter.sh"; exit 2; }
  c1=$(http_code "$BASE/api/usage/claude"); c2=$(http_code "$BASE/react")
  echo "post-apply: /api/usage/claude (no auth)=$c1 (want 401), /react=$c2 (want 200)"
  if [ "$c1" != 401 ] || [ "$c2" != 200 ]; then echo "POST-CHECK FAILED - rolling back"; "$HERE/rollback-usagemeter.sh"; exit 2; fi
fi
echo APPLIED
