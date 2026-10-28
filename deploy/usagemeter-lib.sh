#!/usr/bin/env bash
# Shared helpers for apply-usagemeter.sh / rollback-usagemeter.sh (Claude usage meter, 2026-10-28).
# WP_DIR may be overridden (and PORT) to dry-run against a scratch copy.
WP="${WP_DIR:-/home/aiciv/witness-portal}"
PORT="${PORT:-8097}"
BASE="http://127.0.0.1:$PORT"

# PID of the portal process whose cwd is $WP and whose argv is exactly "python3 portal_server.py".
# (No pgrep -f: it can match its own shell.)
portal_pid() {
  local p
  for p in $(pgrep -x python3 || true); do
    [ "$(readlink "/proc/$p/cwd" 2>/dev/null)" = "$WP" ] || continue
    [ "$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)" = "python3 portal_server.py " ] && { echo "$p"; return; }
  done
}

# Stop the portal (SIGTERM, wait up to 20 s) and relaunch it with the SAME environment the old process had,
# read in-memory from /proc/<pid>/environ (never written to disk: it holds session tokens).
restart_portal() {
  local pid; pid="$(portal_pid)"
  [ -n "$pid" ] || echo "restart: no portal process running for $WP (crashed?) - launching with the caller's environment"
  # Hand the old env to the new process through an inherited fd; python execs under setsid, output appended to portal.log.
  python3 - "${pid:-}" "$WP" <<'PY'
import os, sys, time, signal
pid, wp = (int(sys.argv[1]) if sys.argv[1] else 0), sys.argv[2]
env = dict(os.environ)          # fallback when no old process exists to copy from
if pid:
    env = {}
    for kv in open(f"/proc/{pid}/environ", "rb").read().split(b"\0"):
        if b"=" in kv:
            k, v = kv.split(b"=", 1); env[k.decode()] = v.decode()
    # uvicorn's graceful shutdown waits on open websockets: SIGTERM, then a 2nd SIGTERM (forces exit), then SIGKILL.
    # Safe: chat is journaled per message and delivery is confirmed by effect, so nothing in flight is lost.
    for sig, wait in ((signal.SIGTERM, 6), (signal.SIGTERM, 6), (signal.SIGKILL, 6)):
        try: os.kill(pid, sig)
        except ProcessLookupError: break
        for _ in range(wait * 2):
            if not os.path.exists(f"/proc/{pid}"): break
            time.sleep(0.5)
        if not os.path.exists(f"/proc/{pid}"): break
    else:
        sys.exit("old portal did not exit")
    time.sleep(0.5)
if os.fork():
    sys.exit(0)
os.setsid()
log = os.open(os.path.join(wp, "portal.log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o664)
null = os.open("/dev/null", os.O_RDONLY)
os.dup2(null, 0); os.dup2(log, 1); os.dup2(log, 2)
os.chdir(wp)
os.execvpe("python3", ["python3", "portal_server.py"], env)
PY
  local i code
  for i in $(seq 1 40); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$BASE/react" || true)
    [ "$code" = "200" ] && { echo "restart: portal back (HTTP 200 on /react) after ~$((i/2))s, pid $(portal_pid)"; return 0; }
    sleep 0.5
  done
  echo "restart: portal did NOT come back within 20s"; return 1
}

http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$@" || true; }
