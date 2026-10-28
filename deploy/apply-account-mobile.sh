#!/usr/bin/env bash
# Window 2 for the Claude usage meter (Russell 2026-10-28): signed-in account display + phone layout.
# Frontend (dist overlay) AND backend (claude_usage.py) are applied together so there is ONE portal restart.
# Order: backup -> stage -> swap dist + claude_usage.py -> restart (TERM, TERM, KILL escalation) -> probe -> auto-rollback on any failure.
# Usage: SRC_DIR=<checkout of feat/usage-meter-account-mobile-20261028 with react-portal/dist built> ./deploy/apply-account-mobile.sh
#        ./deploy/rollback-account-mobile.sh   (undo: back to the first meter build, exactly)
# WP_DIR / PORT / SKIP_RESTART=1 / PREV_SHA may be set to dry-run against a scratch copy.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; . "$HERE/usagemeter-lib.sh"
SRC="${SRC_DIR:-$(cd "$HERE/.." && pwd)}"
R="$WP/react-portal"; LIVE="$R/dist"
# claude_usage.py as applied by the first window (branch feat/claude-usage-meter-20261028 @95da8c1)
PREV_SHA="${PREV_SHA:-bbee57f0e885c85fbca5fd689a0cecf20429be43896524dadb1250ce0a1b0371}"
[ -f "$SRC/react-portal/dist/index.html" ] || { echo "SRC has no built dist (run npm ci && npm run build)"; exit 1; }
[ -f "$WP/claude_usage.py" ] || { echo "live claude_usage.py missing - the first meter window was not applied"; exit 1; }
LIVE_SHA="$(sha256sum "$WP/claude_usage.py" | cut -d' ' -f1)"
case "$LIVE_SHA" in "$PREV_SHA"*) ;; *) echo "live claude_usage.py ($LIVE_SHA) is not the expected first-window version - refusing"; exit 1;; esac
python3 -m py_compile "$SRC/claude_usage.py"
TS=$(date -u +%Y%m%dT%H%M%SZ); BAK="$WP/backups/usagemeter2-$TS"; TMP="$R/dist.new-usagemeter2-$TS"
mkdir -p "$BAK"
cp -a "$WP/claude_usage.py" "$BAK/claude_usage.py"; cp -a "$LIVE" "$BAK/dist"    # 1. backups of exactly what is live now
echo "$BAK" > "$WP/.usagemeter2-backup-path"
cp -a "$LIVE" "$TMP"; cp -a "$SRC/react-portal/dist/." "$TMP/"                  # 2. stage: keep OLD hashed chunks so open tabs do not 404
ASSET=$(grep -o 'assets/index-[A-Za-z0-9_-]*\.js' "$TMP/index.html" | head -1)
[ -f "$TMP/$ASSET" ] || { echo "staged index.html points at missing $ASSET"; rm -rf "$TMP"; exit 1; }
cp "$SRC/claude_usage.py" "$WP/claude_usage.py.new"
# Baseline of the panels BEFORE the swap (status codes, with and without auth) to compare after the restart.
PANELS="/health /api/status /api/context /api/witness/fleet /api/margin/corey /api/portal/owner /api/points/summary /api/usage/claude"
probe_panels() {   # prints "path noauth auth" per line; the token is read here and never echoed
  local tok=""; [ -r "$WP/.portal-token" ] && tok="$(cat "$WP/.portal-token")"
  local p a b
  for p in $PANELS; do
    # generous timeout + one retry: a busy portal answering slowly (000) must not look like a changed panel
    a=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$BASE$p" || true); [ "$a" = 000 ] && a=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$BASE$p" || true)
    if [ -n "$tok" ]; then
      b=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -H "Authorization: Bearer $tok" "$BASE$p" || true); [ "$b" = 000 ] && b=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -H "Authorization: Bearer $tok" "$BASE$p" || true)
    else b=-; fi
    echo "$p $a $b"
  done
}
if [ -z "${SKIP_RESTART:-}" ]; then probe_panels > "$BAK/panels-before.txt"; fi
mv "$LIVE" "$R/dist.old-usagemeter2-$TS" && mv "$TMP" "$LIVE"                    # 3. swap (sub-second)
mv "$WP/claude_usage.py.new" "$WP/claude_usage.py"
rm -rf "$R/dist.old-usagemeter2-$TS"                                             #    identical to $BAK/dist
echo "STAGED. backup: $BAK  asset: $ASSET"
if [ -z "${SKIP_RESTART:-}" ]; then
  restart_portal || { echo "RESTART FAILED - rolling back"; "$HERE/rollback-account-mobile.sh"; exit 2; }
  probe_panels > "$BAK/panels-after.txt"
  bad=0
  # every panel/identity endpoint must answer exactly as before; the meter endpoint must be 401 without auth and 200 with
  if ! diff <(grep -v '^/api/usage/claude' "$BAK/panels-before.txt") <(grep -v '^/api/usage/claude' "$BAK/panels-after.txt") >/dev/null; then bad=1; echo "panel status codes changed:"; diff "$BAK/panels-before.txt" "$BAK/panels-after.txt" || true; fi
  m=$(grep '^/api/usage/claude ' "$BAK/panels-after.txt")
  [ "$m" = "/api/usage/claude 401 200" ] || { bad=1; echo "meter endpoint after apply: '$m' (want '/api/usage/claude 401 200')"; }
  tok="$(cat "$WP/.portal-token")"
  curl -s --max-time 20 -H "Authorization: Bearer $tok" "$BASE/api/usage/claude" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert "account" in d, "no account field"' || { bad=1; echo "meter response has no account field"; }
  [ "$(http_code "$BASE/react")" = 200 ] || { bad=1; echo "/react is not 200"; }
  if [ "$bad" != 0 ]; then echo "POST-CHECK FAILED - rolling back"; "$HERE/rollback-account-mobile.sh"; exit 2; fi
fi
echo APPLIED
