#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║  WITNESS-ONLY PORTAL SERVER — BESPOKE ONE-OFF                      ║
║                                                                      ║
║  THIS IS NOT THE PUREBRAIN PORTAL. NO OTHER CIV HAS OR SHOULD      ║
║  HAVE THIS CODE. DO NOT DEPLOY TO FLEET CONTAINERS. EVER.           ║
║                                                                      ║
║  The PureBrain portal that born CIVs use lives in GitHub:            ║
║    github.com/coreycottrell/purebrain-onboarding/portal/             ║
║                                                                      ║
║  This file has Witness-specific features (fleet panel, points,       ║
║  margins, BOOP capture) that do not belong in fleet CIV portals.    ║
║                                                                      ║
║  Corey directive 2026-03-30: "the SECOND you start comparing to     ║
║  YOUR portal code which is a bespoke one off" — never again.         ║
╚══════════════════════════════════════════════════════════════════════╝
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

# Witness extensions (fleet panel, margin, alerts)
try:
    from witness_extensions import WITNESS_ROUTES
except ImportError:
    WITNESS_ROUTES = []

# Ensure HOME is set correctly for the aiciv user.
# docker exec -u aiciv inherits the caller's HOME (often /root) rather than /home/aiciv.
# Fix it here so Path.home() returns the right path throughout the server.
if os.environ.get("HOME", "/root") == "/root" and os.path.isdir("/home/aiciv"):
    os.environ["HOME"] = "/home/aiciv"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
TOKEN_FILE = SCRIPT_DIR / ".portal-token"
PORTAL_HTML = SCRIPT_DIR / "portal.html"
PORTAL_PB_HTML = SCRIPT_DIR / "portal-pb-styled.html"
REACT_DIST = SCRIPT_DIR / "react-portal" / "dist"
START_TIME = time.time()

# Load .env if present
_env_file = Path.home() / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
# Auto-detect CIV_NAME and HUMAN_NAME from identity file — works in any fleet container.
# Falls back to generic defaults if identity file not found (local dev).
_identity_file = Path.home() / ".aiciv-identity.json"
try:
    _identity = json.loads(_identity_file.read_text())
    CIV_NAME = _identity.get("civ_id", "witness")
    HUMAN_NAME = _identity.get("human_name", "User")
except Exception:
    CIV_NAME = "witness"
    HUMAN_NAME = "User"
# Auto-derive Claude project JSONL directory from the home path.
# Claude encodes paths by replacing '/' with '-', so /home/aiciv → -home-aiciv.
# Claude Code appends the CWD to the encoding (e.g. -home-aiciv-civ when run from ~/civ),
# so we scan ALL matching project directories and pick the one with the most recent JSONL.
_encoded_home = str(Path.home()).replace("/", "-")
_projects_dir = Path.home() / ".claude" / "projects"

# ── CHAT-BLIND FIX (2026-06-26) ──────────────────────────────────────
# LOG_ROOT was previously computed ONCE at module import and never
# re-resolved.  After enough uptime across /clear cycles the "winner"
# project directory drifted from the LIVE session's directory
# (e.g. -home-aiciv vs -home-aiciv-civ), making assistant replies
# invisible while portal-chat.jsonl (separate file) kept working.
#
# Fix: _resolve_log_root() re-scans every call but caches for 10 s so
# hot-path polling (ws_chat 0.8 s, thinking-monitor 0.8 s) does not
# stat-storm the filesystem.  The 10 s TTL means a /clear that moves
# the live session self-heals within 10 s — no portal restart needed.
# ──────────────────────────────────────────────────────────────────────
_log_root_cache: dict = {}  # {"root": Path, "expires": float}
_LOG_ROOT_TTL = 10.0  # seconds


def _resolve_log_root() -> Path:
    """Return the project directory containing the most-recently-modified JSONL.

    Re-scans candidate directories but caches the result for _LOG_ROOT_TTL
    seconds to avoid excessive stat calls on the hot polling paths.
    """
    now = time.time()
    cached = _log_root_cache.get("root")
    expires = _log_root_cache.get("expires", 0)
    if cached and now < expires:
        return cached

    # PIN-SESSION FIX (2026-09-23): ONLY Primary's own project dir (-home-aiciv).
    # Previously any dir starting with -home-aiciv (e.g. headless `claude -p` test
    # runs under -home-aiciv-civ-state-...) could win on mtime, and their prompts
    # were rendered in portal chat as untagged user messages.
    result = _projects_dir / _encoded_home
    _log_root_cache.update(root=result, expires=now + _LOG_ROOT_TTL)
    return result


# Legacy alias — kept so the handful of one-shot reads (HISTORY_FILE etc.)
# that never depended on LOG_ROOT keep working.  All JSONL-resolution paths
# now call _resolve_log_root() instead.
LOG_ROOT = _resolve_log_root()


PRIMARY_SESSION_ID_FILE = Path.home() / ".primary-session-id"


def _sorted_session_logs(log_root: Path) -> list:
    """Session JSONLs in log_root, newest-first, with Primary's session PINNED first.

    Primary's session id comes from ~/.primary-session-id; mtime ordering is only
    the fallback within this one dir (glob is non-recursive, so subagents/ never match).
    """
    logs = sorted(log_root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    try:
        sid = PRIMARY_SESSION_ID_FILE.read_text().strip()
        if sid:
            pinned = log_root / f"{sid}.jsonl"
            if pinned.is_file():
                logs = [pinned] + [l for l in logs if l != pinned]
    except Exception:
        pass
    return logs
HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"
PORTAL_CHAT_LOG = SCRIPT_DIR / "portal-chat.jsonl"
UPLOADS_DIR = Path.home() / "portal_uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
UPLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
PAYOUT_REQUESTS_FILE = SCRIPT_DIR / "payout-requests.jsonl"
PAYOUT_MIN_AMOUNT = 25.0   # minimum payout threshold ($)
PAYOUT_COOLDOWN_DAYS = 30  # days between payout requests
MARGIN_PRIMARY = SCRIPT_DIR / "margin-primary.json"
MARGIN_COREY = SCRIPT_DIR / "margin-corey.json"

# Allowed directories for file downloads (generic — works in any customer container)
DOWNLOAD_ALLOWED_DIRS = [
    Path.home() / "exports",
    Path.home() / "to-human",
    Path.home() / "purebrain_portal",
    Path.home() / "from-acg",
    Path.home() / "portal_uploads",
]

# OAuth flow state
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
# Marker file: created when a human completes OAuth via the portal.
# Without this marker, auth status returns false even if credentials exist
# (to distinguish birth-pipeline credentials from human-initiated auth).
HUMAN_AUTH_MARKER = SCRIPT_DIR / ".portal-human-auth"
OAUTH_URL_PATTERN = re.compile(r'https://[^\s\x1b\x07\]]*oauth/authorize\?[^\s\x1b\x07\]]+')
_captured_oauth_url = None

if TOKEN_FILE.exists():
    BEARER_TOKEN = TOKEN_FILE.read_text().strip()
else:
    BEARER_TOKEN = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(BEARER_TOKEN)
    TOKEN_FILE.chmod(0o600)
    print(f"[portal] Generated new bearer token: {BEARER_TOKEN}")

# ---------------------------------------------------------------------------
# Per-operator sender attribution (Gen-34 "portal stamps authenticated sender")
# ---------------------------------------------------------------------------
# Maps a per-operator token -> operator display NAME. When an authenticated
# operator sends a message through the portal, the injected tmux text is
# prefixed with "[<Name>] " so Primary knows WHO is speaking.
#
# Backward compatible: the legacy single BEARER_TOKEN still authenticates and
# maps to the default "operator" name, so nothing breaks if this file is
# absent or a legacy client connects.
#
# To add a future operator: add a "token": "Name" entry to .portal-operators.json
# (generate a token with `python3 -c "import secrets;print(secrets.token_urlsafe(32))"`),
# chmod 600 the file, and restart the portal. The token authenticates exactly
# like the legacy token and its messages get tagged "[Name]".
OPERATORS_FILE = SCRIPT_DIR / ".portal-operators.json"
DEFAULT_OPERATOR_NAME = "operator"
OPERATOR_TOKENS: dict = {}
if OPERATORS_FILE.exists():
    try:
        _ops_raw = json.loads(OPERATORS_FILE.read_text())
        if isinstance(_ops_raw, dict):
            # token -> name; strip to be safe
            OPERATOR_TOKENS = {str(k).strip(): str(v).strip()
                               for k, v in _ops_raw.items() if str(k).strip()}
        print(f"[portal] Loaded {len(OPERATOR_TOKENS)} operator identities: "
              f"{sorted(set(OPERATOR_TOKENS.values()))}")
    except Exception as _e:
        print(f"[portal] WARN: failed to load {OPERATORS_FILE}: {_e} — "
              f"falling back to single-token mode")
        OPERATOR_TOKENS = {}


def resolve_operator(request) -> str:
    """Return the operator display name for an authenticated request.

    Checks the Bearer header first, then the ?token= query param. Per-operator
    tokens (.portal-operators.json) win; the legacy BEARER_TOKEN maps to
    DEFAULT_OPERATOR_NAME. Returns DEFAULT_OPERATOR_NAME for any authenticated
    request whose token isn't a known per-operator token (backward compatible).
    Callers should only use this AFTER check_auth() has passed.
    """
    auth = request.headers.get("authorization", "")
    tok = auth[7:] if auth.startswith("Bearer ") else request.query_params.get("token", "")
    if tok and tok in OPERATOR_TOKENS:
        return OPERATOR_TOKENS[tok]
    return DEFAULT_OPERATOR_NAME


def get_tmux_session() -> str:
    """Find the live primary Claude Code session for this container."""
    def alive(name):
        try:
            subprocess.check_output(["tmux", "has-session", "-t", name], stderr=subprocess.DEVNULL)
            return True
        except subprocess.CalledProcessError:
            return False

    # FIRST: Check .current_session marker — most reliable, set at session start.
    # This avoids grabbing team lead panes that happen to be "attached".
    marker = Path.home() / ".current_session"
    if marker.exists():
        name = marker.read_text().strip()
        if name and alive(name):
            return name

    # FALLBACK: Find the currently attached session.
    # Claude Code sessions are numbered (e.g. "28"), not named "{civ}-primary",
    # so the name-based scan below misses them. The attached session IS the active one.
    try:
        out = subprocess.check_output(
            ["tmux", "list-sessions", "-F", "#{session_name}:#{session_attached}"],
            stderr=subprocess.DEVNULL, text=True
        )
        for line in out.splitlines():
            if line.strip().endswith(":1"):
                attached = line.split(":")[0].strip()
                if attached:
                    return attached
    except Exception:
        pass
    try:
        out = subprocess.check_output(["tmux", "list-sessions", "-F", "#{session_name}"],
                                      stderr=subprocess.DEVNULL, text=True)
        sessions = out.strip().splitlines()
        for line in sessions:
            if CIV_NAME in line.lower():
                return line.strip()
        # No CIV session found — use first available session
        if sessions:
            return sessions[0].strip()
    except Exception:
        pass
    return f"{CIV_NAME}-primary"


def _find_current_session_id():
    """Find the current Claude Code session ID from history.jsonl."""
    try:
        if not HISTORY_FILE.exists():
            return None
        with HISTORY_FILE.open("r") as f:
            f.seek(0, 2)
            length = f.tell()
            window = min(16384, length)
            f.seek(max(0, length - window))
            lines = f.read().splitlines()
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                proj = entry.get("project", "")
                if proj and (CIV_NAME in proj or str(Path.home()) in proj):
                    return entry.get("sessionId")
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return None


def _get_all_session_log_paths(max_files=10):
    """Get paths to recent JSONL session logs, ordered oldest-first."""
    try:
        log_root = _resolve_log_root()
        logs = _sorted_session_logs(log_root)
        return list(reversed(logs[:max_files]))
    except Exception:
        return []


def _despace(text):
    """Collapse spaced-out text like 'H  e  l  l  o' back to 'Hello'.
    Some older JSONL sessions store text with spaces between every character."""
    if not text or len(text) < 6:
        return text
    # Check if text follows the pattern: char, spaces, char, spaces...
    # Sample first 40 chars to detect the pattern
    sample = text[:40]
    # Pattern: single non-space char followed by 1-2 spaces, repeating
    spaced_chars = 0
    i = 0
    while i < len(sample):
        if i + 1 < len(sample) and sample[i] != " " and sample[i + 1] == " ":
            spaced_chars += 1
            i += 1
            while i < len(sample) and sample[i] == " ":
                i += 1
        else:
            i += 1
    # If >60% of non-space chars are followed by spaces, it's spaced text
    non_space = sum(1 for c in sample if c != " ")
    if non_space > 0 and spaced_chars / non_space > 0.6:
        # Collapse: take every non-space char, but preserve intentional word gaps
        result = []
        i = 0
        while i < len(text):
            if text[i] != " ":
                result.append(text[i])
                i += 1
                # Skip the inter-character spaces (1-2 spaces)
                spaces = 0
                while i < len(text) and text[i] == " ":
                    spaces += 1
                    i += 1
                # 3+ spaces likely means intentional word boundary
                if spaces >= 3:
                    result.append(" ")
            else:
                i += 1
        return "".join(result)
    return text


def _is_real_user_message(text):
    """Check if a user message is a real human message (not system/teammate noise)."""
    if not text or len(text) < 2:
        return False
    # Telegram messages from user - always real
    if "[TELEGRAM" in text:
        return True
    # Portal-sent messages (stored in portal chat log)
    if text.startswith("[PORTAL]"):
        return True
    # Filter out noise
    noise_markers = [
        "<teammate-message", "<system-reminder", "system-reminder",
        "Base directory for this skill", "teammate_id=",
        "<tool_result", "<function_calls", "hook success",
        "Session Ledger", "MEMORY INJECTION", "<task-notification",
        "[Image: source:", "PHOTO saved to:",
        "This session is being continued from a previous",
        "Called the Read tool", "Called the Bash tool",
        "Called the Write tool", "Called the Glob tool",
        "Called the Grep tool", "Result of calling",
        "[from-ACG]",                  # Cross-CIV system messages
        "Context restored",
        "Summary:  ",                  # Agent task summaries
        "` regex", "` sed", "| sed",   # Code snippets leaking as messages
        "re.search(r'", "re.DOTALL",
        "<command-name>", "<command-message>",  # CLI commands
        "<command-args>", "<local-command",
        "local-command-caveat", "local-command-stdout",
        "Compacted (ctrl+o",           # Compaction messages
        "&& [ -x ", "| cut -d",        # Shell code fragments
        "[portal",                     # Portal messages from session JSONL (already in portal-chat.jsonl)
    ]
    for marker in noise_markers:
        if marker in text[:300]:
            return False
    # Whitelist known injection formats before special-char filter
    if "[AGENTMAIL" in text[:50]:
        return True
    # Skip messages that look like code/config (too many special chars)
    special = sum(1 for c in text[:200] if c in '{}[]|\\`$()#')
    if len(text) < 200 and special > len(text) * 0.15:
        return False
    return True


def _clean_user_text(text):
    """Clean up user message text for display."""
    # Strip Telegram prefix for cleaner display
    if "[TELEGRAM" in text:
        # Format: [TELEGRAM private:NNN from @Username] actual message
        idx = text.find("]")
        if idx > 0:
            return text[idx + 1:].strip()
    if "[AGENTMAIL" in text:
        # Format: [AGENTMAIL inbox:witness-aiciv from:Name <email>] Subject: Body
        idx = text.find("]")
        if idx > 0:
            return "[Email] " + text[idx + 1:].strip()
    if text.startswith("[PORTAL] "):
        return text[9:]
    return text


def _is_real_assistant_message(text):
    """Check if an assistant message is substantive (not just tool calls or noise)."""
    if not text or len(text) < 10:
        return False
    stripped = text.strip()
    # Reject short non-alphanumeric noise (pipes, brackets, stray chars)
    if len(stripped) <= 3 and not any(c.isalnum() for c in stripped):
        return False
    return True


_jsonl_cache: dict = {}  # path -> (mtime, messages)
_TAIL_BYTES = 5_000_000   # read last 5 MB of large files (session logs can be 30MB+)

# IDs already written to portal-chat.jsonl — prevents duplicate mirror writes
_portal_log_ids: set = set()

# Active WebSocket connections for pushing thinking blocks
_chat_ws_clients: set = set()

# Hashes of thinking blocks already sent — prevents duplicates across reconnects
_sent_thinking_hashes: set = set()


def _init_portal_log_ids():
    """Load IDs already in portal-chat.jsonl so we don't re-mirror them."""
    if not PORTAL_CHAT_LOG.exists():
        return
    try:
        with PORTAL_CHAT_LOG.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    mid = entry.get("id")
                    if mid:
                        _portal_log_ids.add(mid)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass


def _mirror_to_portal_log(msg):
    """Write a discovered session message to portal-chat.jsonl so it survives refreshes."""
    mid = msg.get("id")
    if not mid or mid in _portal_log_ids:
        return
    # Guard: never persist noise-only messages to the log (prevents stale pipe/char glitches)
    msg_text = msg.get("text", "").strip()
    if not msg_text or len(msg_text) < 3:
        return
    if len(msg_text) <= 2 and not any(c.isalnum() for c in msg_text):
        return  # Skip stray pipe/bracket/noise artifacts
    _portal_log_ids.add(mid)
    try:
        with PORTAL_CHAT_LOG.open("a") as f:
            f.write(json.dumps(msg) + "\n")
    except Exception:
        pass


def _parse_jsonl_messages_from_file(log_path):
    """Parse a single JSONL log into clean chat messages.
    Tail-reads large files and caches by mtime for fast repeated calls."""
    messages = []
    if not log_path or not log_path.exists():
        return messages

    try:
        stat = log_path.stat()
        mtime = stat.st_mtime
        fsize = stat.st_size
        cached = _jsonl_cache.get(str(log_path))
        # Cache key includes BOTH mtime AND file size to catch writes within same second
        if cached and cached[0] == mtime and cached[2] == fsize:
            return cached[1]

        # Read only the tail of large files to avoid parsing megabytes each poll
        with log_path.open("rb") as fb:
            if stat.st_size > _TAIL_BYTES:
                fb.seek(-_TAIL_BYTES, 2)
                fb.readline()  # skip partial first line
            raw = fb.read()
        lines_iter = raw.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return messages

    try:
        for line in lines_iter:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = entry.get("message", {})
                role = msg.get("role", entry.get("type", ""))

                if role not in ("user", "assistant"):
                    continue

                content_blocks = msg.get("content", []) or []
                text_parts = []    # For normal text blocks
                char_parts = []    # For single-character string blocks
                is_char_stream = False
                for block in content_blocks:
                    if isinstance(block, str):
                        # Single char blocks: preserve spaces for word boundaries
                        if len(block) <= 2:  # single chars including '\n'
                            char_parts.append(block)
                            is_char_stream = True
                        else:
                            s = block.strip()
                            if s:
                                text_parts.append(s)
                    elif isinstance(block, dict) and block.get("type") == "text":
                        t = (block.get("text") or "").strip()
                        if t:
                            text_parts.append(t)

                # Build combined text
                if is_char_stream and len(char_parts) > 10:
                    # Join character stream directly (preserves spaces/newlines)
                    combined = "".join(char_parts).strip()
                    # Also append any text blocks
                    if text_parts:
                        combined += "\n\n" + "\n\n".join(text_parts)
                elif text_parts:
                    combined = "\n\n".join(text_parts)
                else:
                    continue

                if not combined or len(combined) < 2:
                    continue

                # Collapse spaced-out text from older sessions
                combined = _despace(combined)

                # Filter based on role
                if role == "user":
                    if not _is_real_user_message(combined):
                        continue
                    combined = _clean_user_text(combined)
                elif role == "assistant":
                    if not _is_real_assistant_message(combined):
                        continue

                ts = entry.get("timestamp")
                if isinstance(ts, (int, float)):
                    ts = ts / 1000  # ms to seconds
                elif isinstance(ts, str):
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        ts = dt.timestamp()
                    except (ValueError, AttributeError):
                        ts = time.time()
                else:
                    ts = time.time()

                msg_entry = {
                    "role": role,
                    "text": combined,
                    "timestamp": int(ts),
                    "id": entry.get("uuid", f"msg-{log_path.stem[:8]}-{len(messages)}")
                }
                # Tag BOOP prompts so frontend can render them distinctly
                if role == "user" and "THIS IS YOUR SACRED DUTY" in combined[:100]:
                    msg_entry["source"] = "boop"
                messages.append(msg_entry)
    except Exception:
        pass

    _jsonl_cache[str(log_path)] = (mtime, messages, stat.st_size)
    return messages


def _load_portal_messages():
    """Load messages sent via the portal chat, filtering out noise."""
    messages = []
    if not PORTAL_CHAT_LOG.exists():
        return messages
    try:
        with PORTAL_CHAT_LOG.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    # Filter noise from portal log (stray pipes, single chars, etc.)
                    msg_text = entry.get("text", "").strip()
                    if not msg_text:
                        continue
                    if len(msg_text) <= 2 and not any(c.isalnum() for c in msg_text):
                        continue  # Skip stray pipe/bracket/noise artifacts
                    messages.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return messages


def _save_portal_message(text, role="user", source=None, sender=None):
    """Save a message sent via the portal.

    P20 (operator identity): `sender` is the self-selected operator name
    (Corey / Russell / other). Persisting it lets every operator's page — and
    a plain refresh — render WHO said each line instead of a hardcoded name.
    """
    entry = {
        "role": role,
        "text": text,
        "timestamp": int(time.time()),
        "id": f"portal-{int(time.time() * 1000)}",
    }
    if source:
        entry["source"] = source
    if sender:
        entry["sender"] = sender
    try:
        with PORTAL_CHAT_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        _portal_log_ids.add(entry["id"])  # Prevent _mirror_to_portal_log from double-writing
    except Exception:
        pass
    return entry


# CPU-spin fix (2026-07-09): _parse_all_messages fully re-read+re-parsed the 10
# newest session JSONLs (~20MB) PLUS the entire portal-chat.jsonl (grew to ~53MB)
# on EVERY call. ws_chat calls this every 0.8s PER connected client; with 5 live
# ws_chat clients that is ~0.47s x 5 / 0.8s = ~291% demanded CPU on the single
# asyncio event loop -> one core pinned at ~95% AND the loop permanently backlogged,
# so every HTTP request starved ~4s and "/" timed out. Same class as the P3
# /api/context fix. Fix: a short TTL cache shared across all callers/clients so N
# concurrent ws_chat clients trigger ONE parse per _PARSE_ALL_TTL, not N per 0.8s.
# Cache invalidates immediately when the portal-chat log or newest session file
# changes (mtime+size), so freshness is preserved. Backup: portal_server.py.bak.parsecache.*
_PARSE_ALL_TTL = 1.0            # seconds; rapid multi-client polls reuse one parse
_parse_all_cache: dict = {}    # last_n -> (expires_at, signature, result_list)


def _parse_all_signature():
    """Cheap change-signature: (mtime,size) of portal-chat log + newest session file.
    Stat-only — no reads. Any new message bumps one of these, invalidating the cache."""
    parts = []
    try:
        st = PORTAL_CHAT_LOG.stat()
        parts.append((st.st_mtime, st.st_size))
    except Exception:
        parts.append((0, 0))
    try:
        paths = _get_all_session_log_paths(max_files=1)
        if paths:
            st = Path(paths[0]).stat()
            parts.append((st.st_mtime, st.st_size))
    except Exception:
        parts.append((0, 0))
    return tuple(parts)


def _parse_all_messages(last_n=100):
    """Parse messages across all recent session logs + portal log.

    TTL-cached (see note above): reuses a recent parse across rapid/concurrent
    callers so ws_chat's many clients don't each re-parse tens of MB every cycle."""
    now = time.time()
    sig = _parse_all_signature()
    cached = _parse_all_cache.get(last_n)
    if cached:
        c_expires, c_sig, c_result = cached
        if c_sig == sig and now < c_expires:
            return c_result

    all_messages = []

    # JSONL session logs
    for log_path in _get_all_session_log_paths(max_files=10):
        all_messages.extend(_parse_jsonl_messages_from_file(log_path))

    # Portal-sent messages
    all_messages.extend(_load_portal_messages())

    # Sort by timestamp
    all_messages.sort(key=lambda m: m["timestamp"])

    # Deduplicate by ID — keep LAST occurrence (most complete text for streamed messages)
    seen_idx = {}
    for i, m in enumerate(all_messages):
        seen_idx[m["id"]] = i
    deduped = [all_messages[i] for i in sorted(seen_idx.values())]

    result = deduped[-last_n:] if len(deduped) > last_n else deduped
    _parse_all_cache[last_n] = (now + _PARSE_ALL_TTL, sig, result)
    return result


def check_auth(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        tok = auth[7:]
    else:
        tok = request.query_params.get("token", "")
    # Legacy single token OR any per-operator token authenticates.
    return tok == BEARER_TOKEN or tok in OPERATOR_TOKENS


# ---------------------------------------------------------------------------

# ── Favicon ──────────────────────────────────────────────────────────────

async def favicon(request: Request):
    """Serve PureBrain favicon for unified branding across all subdomains."""
    ico = SCRIPT_DIR / "favicon.ico"
    if ico.exists():
        return FileResponse(str(ico), media_type="image/x-icon")
    return Response(status_code=204)

async def favicon_png(request: Request):
    """Serve 32px favicon PNG."""
    png = SCRIPT_DIR / "favicon-32.png"
    if png.exists():
        return FileResponse(str(png), media_type="image/png")
    return Response(status_code=204)

async def apple_touch_icon(request: Request):
    """Serve Apple touch icon."""
    icon = SCRIPT_DIR / "apple-touch-icon.png"
    if icon.exists():
        return FileResponse(str(icon), media_type="image/png")
    return Response(status_code=204)

# Routes
# ---------------------------------------------------------------------------
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "civ": CIV_NAME, "uptime": int(time.time() - START_TIME)})


async def index(request: Request) -> Response:
    react_index = REACT_DIST / "index.html"
    if react_index.exists():
        return FileResponse(str(react_index), media_type="text/html")
    if PORTAL_PB_HTML.exists():
        return FileResponse(str(PORTAL_PB_HTML), media_type="text/html")
    if PORTAL_HTML.exists():
        return FileResponse(str(PORTAL_HTML), media_type="text/html")
    return Response("<h1>Portal not found</h1>", media_type="text/html", status_code=503)


async def index_pb(request: Request) -> Response:
    """Serve PureBrain-styled portal at /pb path."""
    if PORTAL_PB_HTML.exists():
        return FileResponse(str(PORTAL_PB_HTML), media_type="text/html")
    return Response("<h1>PB Portal not found</h1>", media_type="text/html", status_code=503)


async def index_react(request: Request) -> Response:
    """Serve React portal at /react path."""
    react_index = REACT_DIST / "index.html"
    if react_index.exists():
        return FileResponse(str(react_index), media_type="text/html")
    return Response("<h1>React Portal not found — run npm run build in react-portal/</h1>",
                    media_type="text/html", status_code=503)


async def api_status(request: Request) -> JSONResponse:
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    session = get_tmux_session()
    tmux_alive = False
    try:
        subprocess.check_output(["tmux", "has-session", "-t", session], stderr=subprocess.DEVNULL)
        tmux_alive = True
    except subprocess.CalledProcessError:
        pass

    claude_running = False
    try:
        out = subprocess.check_output(["pgrep", "-f", "claude"], stderr=subprocess.DEVNULL, text=True)
        claude_running = bool(out.strip())
    except subprocess.CalledProcessError:
        pass

    tg_running = False
    try:
        out = subprocess.check_output(["pgrep", "-f", "telegram"], stderr=subprocess.DEVNULL, text=True)
        tg_running = bool(out.strip())
    except subprocess.CalledProcessError:
        pass

    ctx_pct = None
    try:
        ctx_file = Path("/tmp/claude_context_used.txt")
        if ctx_file.exists():
            ctx_pct = float(ctx_file.read_text().strip())
    except Exception:
        pass

    return JSONResponse({
        "civ": CIV_NAME, "uptime": int(time.time() - START_TIME),
        "tmux_session": session, "tmux_alive": tmux_alive,
        "claude_running": claude_running, "tg_bot_running": tg_running,
        "ctx_pct": ctx_pct,
        "timestamp": int(time.time()),
    })


async def api_chat_history(request: Request) -> JSONResponse:
    """Return recent chat messages from JSONL session log."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    last_n = int(request.query_params.get("last", "100"))
    last_n = min(last_n, 500)

    messages = _parse_all_messages(last_n=last_n)

    # Mirror any session messages to portal-chat.jsonl so they survive future refreshes
    for msg in messages:
        _mirror_to_portal_log(msg)

    return JSONResponse({"messages": messages, "count": len(messages), "timestamp": int(time.time())})


# ---------------------------------------------------------------------------
# DURABLE portal->tmux delivery (task#36 fix, 2026-09-17).
#
# ROOT CAUSE of the recurring "orphan-unsent" stall: api_chat_send persisted the
# message then FIRE-AND-FORGET send-keys with only a fixed 5x0.5s (2.5s) Enter
# retry and NO confirmation. When Primary was busy (mid-gen / "Waiting for N
# workflows" / a TUI modal) the Enter was swallowed, the 2.5s window expired, and
# the text sat UNSENT in the composer forever. No queue, no readback, no replay ->
# stored-but-not-injected messages were lost. (13th occurrence when fixed.)
#
# FIX: (1) CONFIRM-BY-READBACK + NUDGE — after send, poll the tmux composer; while
# our text is still sitting there, press Enter again, bounded to ~120s (covers a
# long Primary-busy stretch). (2) PENDING QUEUE — every send is queued undelivered
# and only cleared once the composer no longer holds our text. (3) REPLAY on
# startup — re-inject anything still queued (closes the outage black-hole).
# (4) Dead/unreachable pane -> honest queued/delivered:false, never a false "sent"
# and never send-keys to a dead target. Backup: portal_server.py.bak-msgdrop-*
# ---------------------------------------------------------------------------
PENDING_DELIVERY_LOG = SCRIPT_DIR / "portal-pending-deliveries.jsonl"
_DELIVERY_DEADLINE_SECS = 120  # max window to keep nudging a busy Primary
_BORDER_CHARS = set("─—-═_")   # chars that make up the TUI composer box borders


def _dnorm(s):
    """Whitespace-normalized, case-folded — for robust needle matching."""
    return re.sub(r"\s+", " ", s or "").strip().casefold()


def _send_text(target, tagged):
    """Type + submit a tagged message into a tmux pane. Returns True on success.

    LARGE-MESSAGE FIX (task#36, 2026-09-21): the previous implementation used
    `tmux send-keys -t <pane> -l "\\n<tagged>"`. tmux HARD-REJECTS any send-keys
    command line >= 16 KiB (2^14) with "command too long" (exit 1) — the ENTIRE
    payload is dropped (a total reject, never a truncation), so every operator
    message over ~16 KB was saved to portal history but NEVER injected into
    Primary's pane, then re-queued forever (the "only big messages" drop).

    `tmux load-buffer` (from a temp file) + `paste-buffer` streams the payload
    into the pane with NO length limit, so arbitrarily large messages deliver
    intact. Downstream behaviour is preserved: the leading literal newline
    clears any partial composer input, and a separate Enter keypress submits —
    so the existing composer-readback / needle confirm loop is unchanged."""
    tmp_path = None
    buf = f"portalmsg-{os.getpid()}-{int(time.time() * 1000000)}"
    try:
        payload = f"\n{tagged}"
        fd, tmp_path = tempfile.mkstemp(prefix="portalmsg-", suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        # Named, unique-per-send buffer so concurrent injections never clobber
        # each other; -d deletes the buffer after a successful paste (cleanup).
        subprocess.run(["tmux", "load-buffer", "-b", buf, tmp_path],
                       check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "paste-buffer", "-d", "-b", buf, "-t", target],
                       check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "send-keys", "-t", target, "Enter"],
                       check=True, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        # If paste never ran, the named buffer may still exist — best-effort drop.
        try:
            subprocess.run(["tmux", "delete-buffer", "-b", buf],
                           check=False, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        return False
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _tmux_enter(target):
    """Best-effort single Enter (nudge a swallowed submit)."""
    try:
        subprocess.run(["tmux", "send-keys", "-t", target, "Enter"],
                       check=False, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _read_composer_text(target):
    """Return the normalized text currently sitting in the TUI composer input box,
    or None if the layout can't be parsed. Empty string => composer is empty
    (message was submitted). The composer is the region between the last two
    horizontal border lines near the bottom of the pane."""
    try:
        out = subprocess.check_output(
            ["tmux", "capture-pane", "-t", target, "-p"],
            stderr=subprocess.DEVNULL, text=True)
    except Exception:
        return None
    lines = out.splitlines()
    borders = [i for i, l in enumerate(lines)
               if len(l.strip()) >= 20 and set(l.strip()) <= _BORDER_CHARS]
    if len(borders) < 2:
        return None
    top, bot = borders[-2], borders[-1]
    content = " ".join(lines[top + 1:bot]).strip()
    content = re.sub(r"^[❯>›»\s]+", "", content)  # drop leading prompt marker(s)
    return _dnorm(content)


# ---------------------------------------------------------------------------
# BY-EFFECT DELIVERY CONFIRMATION (ticket 3389, 2026-09-30).
#
# ROOT CAUSE of the 2026-09-30 14:06:33Z silent drop (portal-1790777193564):
# the confirm loop above treated "our text is no longer in the composer" as
# DELIVERED. That is a proxy. If the keystrokes land anywhere other than the
# composer (a TUI overlay / footer panel / modal, a layout the parser cannot
# see), the composer never holds the text, so the loop cleared the queue row
# and the message vanished: HTTP 200, no pending row, no submit in Claude Code.
#
# FIX: DELIVERED now means Claude Code ITSELF recorded the submit — a `user`
# turn or a `queue-operation` (typed-while-busy) entry in Primary's session
# transcript, or a ~/.claude/history.jsonl prompt row — carrying our operator
# tag AND our needle, timestamped at/after the first send. Until then the row
# stays queued. An unconfirmed message is re-sent ONLY when Primary's turn has
# ENDED (so we never type blind into a busy/modal pane and never send Escape
# mid-turn, which would interrupt Primary), after a single Escape to dismiss
# any overlay. Resends are bounded per row (_MAX_TOTAL_RESENDS) so a detection
# fault can never become a duplicate storm. Every step is journaled to
# portal-delivery-journal.jsonl (the delivery path had NO log lines before).
# If Primary's transcript cannot be located at all, the old composer heuristic
# is used as a fallback (journaled), never a silent guess.
# Backup: portal_server.py.bak-pre-deliveryconfirm-*
# ---------------------------------------------------------------------------
DELIVERY_JOURNAL = SCRIPT_DIR / "portal-delivery-journal.jsonl"
_RESEND_GRACE_SECS = 10      # unconfirmed + not in composer this long after a send -> resend
_MAX_TOTAL_RESENDS = 6       # hard cap per message across all windows (no duplicate storms)
_NON_TURN_TYPES = {"queue-operation", "attachment", "file-history-snapshot",
                   "last-prompt", "ai-title", "mode", "permission-mode",
                   "atis-latch", "frame-link", "summary", "custom-title"}


def _djournal(event, msg_id, **kw):
    """Append one timestamped delivery event (the delivery path's own log)."""
    try:
        rec = {"t": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
               "event": event, "id": msg_id}
        rec.update(kw)
        with DELIVERY_JOURNAL.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _tail_lines(path, nbytes):
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        data = f.read()
    lines = data.split(b"\n")
    if size > nbytes and lines:
        lines = lines[1:]            # first line is probably partial
    return lines


def _iso_epoch(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _entry_submit_text(d):
    """Text of a transcript entry that represents operator INPUT, else None."""
    t = d.get("type")
    if t == "queue-operation":
        c = d.get("content")
    elif t == "user":
        c = (d.get("message") or {}).get("content")
    else:
        return None
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c
                        if isinstance(x, dict) and x.get("type") == "text")
    return None


def _primary_log_paths():
    try:
        return _sorted_session_logs(_resolve_log_root())[:2]
    except Exception:
        return []


def _submit_recorded(tag_norm, needle, since):
    """By-effect proof that Claude Code accepted the message. Returns a short
    'where' string, or None if not (yet) recorded."""
    if not needle:
        return None

    def _match(txt):
        n = _dnorm(txt)
        return needle in n and (not tag_norm or tag_norm in n)

    for p in _primary_log_paths():
        try:
            if p.stat().st_mtime < since - 5:
                continue
            for raw in reversed(_tail_lines(p, 4_000_000)):
                if not raw.strip():
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                ts = d.get("timestamp")
                if not ts:
                    continue
                try:
                    e = _iso_epoch(ts)
                except Exception:
                    continue
                if e < since - 120:  # entries are ~ordered; generous margin
                    break
                if e < since:
                    continue
                txt = _entry_submit_text(d)
                if txt and _match(txt):
                    return f"transcript:{p.stem[:8]}:{d.get('type')}:{ts}"
        except Exception:
            continue
    try:
        for raw in reversed(_tail_lines(HISTORY_FILE, 262144)):
            if not raw.strip():
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            e = (d.get("timestamp") or 0) / 1000.0
            if e < since - 120:
                break
            if e < since:
                continue
            blob = d.get("display") or ""
            pc = d.get("pastedContents") or {}
            if isinstance(pc, dict):
                for v in pc.values():
                    if isinstance(v, dict):
                        blob += " " + str(v.get("content", ""))
            if _match(blob):
                return f"history:{int(e)}"
    except Exception:
        pass
    return None


def _primary_turn_ended():
    """True only if Primary's latest turn has ENDED (last significant transcript
    entry is system/turn_duration). Unknown -> False, so we never send Escape or
    re-type into a pane that may be mid-turn or showing a permission modal."""
    logs = _primary_log_paths()
    if not logs:
        return False
    try:
        for raw in reversed(_tail_lines(logs[0], 400_000)):
            if not raw.strip():
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            t = d.get("type")
            if not t or t in _NON_TURN_TYPES:
                continue
            return t == "system" and d.get("subtype") == "turn_duration"
    except Exception:
        pass
    return False


def _tmux_escape(target):
    try:
        subprocess.run(["tmux", "send-keys", "-t", target, "Escape"],
                       check=False, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _tag_of(tagged):
    """'[Russell] [portal] msg' -> normalized '[russell] [portal]'."""
    m = re.match(r"^\s*(\[[^\]]*\]\s*\[[^\]]*\])", tagged or "")
    return _dnorm(m.group(1)) if m else ""


def _queue_pending(msg_id, tagged, needle):
    now = int(time.time())
    try:
        with PENDING_DELIVERY_LOG.open("a") as f:
            f.write(json.dumps({"id": msg_id, "tagged": tagged, "needle": needle,
                                "ts": now, "first_ts": now, "resends": 0}) + "\n")
    except Exception:
        pass
    _djournal("queued", msg_id, needle=needle)


def _load_pending():
    rows = []
    if not PENDING_DELIVERY_LOG.exists():
        return rows
    try:
        with PENDING_DELIVERY_LOG.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return rows


def _write_pending(rows):
    try:
        tmp = str(PENDING_DELIVERY_LOG) + ".tmp"
        with open(tmp, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        os.replace(tmp, str(PENDING_DELIVERY_LOG))
    except Exception:
        pass


def _update_pending(msg_id, **fields):
    rows = _load_pending()
    hit = False
    for r in rows:
        if r.get("id") == msg_id:
            r.update(fields)
            hit = True
    if hit:
        _write_pending(rows)


def _get_pending(msg_id):
    for r in _load_pending():
        if r.get("id") == msg_id:
            return r
    return None


def _mark_delivered(msg_id):
    """Remove a confirmed-delivered row from the pending queue. Runs on the single
    asyncio event loop, so no locking is needed (cooperative scheduling)."""
    rows = _load_pending()
    remaining = [r for r in rows if r.get("id") != msg_id]
    if len(remaining) == len(rows):
        return
    _write_pending(remaining)


async def _deliver_and_confirm(tagged, needle, msg_id, initial_send,
                               deadline=_DELIVERY_DEADLINE_SECS):
    """Deliver a message and CONFIRM BY EFFECT that Claude Code recorded it.

      - Claude Code recorded the submit (transcript user/queue entry or prompt
        history, with our tag + needle, after first send) -> DELIVERED.
      - text still sitting in the composer -> press Enter (swallowed submit).
      - not recorded, not in composer, >= grace since our last send, Primary's
        turn has ENDED -> single Escape (dismiss overlay), re-check composer,
        then re-send. Busy/unknown -> wait (never type blind, never Escape
        mid-turn). Bounded by _MAX_TOTAL_RESENDS per message.
      - deadline -> stays QUEUED; the periodic sweep resumes it.
    Re-resolves the live Primary pane each iteration (pane ids change)."""
    row = _get_pending(msg_id) or {}
    since = float(row.get("first_ts") or row.get("ts") or time.time()) - 1
    resends = int(row.get("resends") or 0)
    tag_norm = _tag_of(tagged)
    fallback = not _primary_log_paths()
    if fallback:
        _djournal("fallback_legacy_no_transcript", msg_id)
    start = time.time()
    ever_sent = not initial_send   # handler path already sent inline
    last_send = start if ever_sent else 0.0
    saw_stuck = False
    nudges = 0
    await asyncio.sleep(1.2)        # let the TUI render / settle
    while time.time() - start < deadline:
        if not fallback:
            where = _submit_recorded(tag_norm, needle, since)
            if where:
                _mark_delivered(msg_id)
                _djournal("delivered", msg_id, proof=where, resends=resends,
                          nudges=nudges, secs=round(time.time() - start, 1))
                return True
        target = _find_primary_pane()
        is_pane = isinstance(target, str) and target.startswith("%")
        if not is_pane:
            await asyncio.sleep(2)
            continue
        comp = _read_composer_text(target)
        if comp and needle and needle in comp:
            if not saw_stuck:
                _djournal("stuck_in_composer", msg_id, pane=target)
            saw_stuck = True
            nudges += 1
            _tmux_enter(target)      # swallowed submit -> nudge
            await asyncio.sleep(2)
            continue
        if fallback:
            if ever_sent and comp is not None:
                _mark_delivered(msg_id)          # legacy proxy (no transcript)
                _djournal("delivered_legacy_proxy", msg_id)
                return True
            if not ever_sent and _send_text(target, tagged):
                ever_sent, last_send = True, time.time()
            await asyncio.sleep(2)
            continue
        if ever_sent and time.time() - last_send < _RESEND_GRACE_SECS:
            await asyncio.sleep(2)
            continue
        # Unconfirmed and not in the composer: (re)send, but only safely.
        fresh = (time.time() - since) < 30
        idle = _primary_turn_ended()
        if not idle and (ever_sent or not fresh):
            await asyncio.sleep(2)   # busy/unknown: never type blind into it
            continue
        if ever_sent or not fresh:
            if resends >= _MAX_TOTAL_RESENDS:
                _djournal("resend_cap_reached_needs_attention", msg_id,
                          resends=resends)
                return False
            if idle:
                _tmux_escape(target)            # dismiss any overlay/panel
                await asyncio.sleep(0.6)
                comp = _read_composer_text(target)
                if comp and needle and needle in comp:
                    _tmux_enter(target)
                    await asyncio.sleep(2)
                    continue
        if _send_text(target, tagged):
            if ever_sent or not fresh:
                resends += 1
                _update_pending(msg_id, resends=resends)
            _djournal("resent" if ever_sent else "sent", msg_id, pane=target,
                      idle=idle, resends=resends)
            ever_sent, last_send = True, time.time()
        else:
            _djournal("send_failed", msg_id, pane=target)
        await asyncio.sleep(1.5)
    _djournal("unconfirmed_at_deadline_kept_queued", msg_id,
              ever_sent=ever_sent, saw_stuck=saw_stuck, resends=resends)
    return False                     # stays queued for the sweep


async def _replay_pending_deliveries():
    """On (re)start, redeliver anything still queued in order — so a portal or
    session restart can't black-hole operator messages. (Already-recorded rows
    are confirmed from the transcript first, never re-typed.)"""
    await asyncio.sleep(8)           # let tmux / the session settle first
    for r in sorted(_load_pending(), key=lambda x: x.get("ts", 0)):
        try:
            _djournal("replay", r.get("id", ""))
            await _deliver_and_confirm(r.get("tagged", ""), r.get("needle", ""),
                                       r.get("id", ""), initial_send=True)
        except Exception:
            pass
        await asyncio.sleep(1)


async def _periodic_delivery_sweep():
    """Drain the pending queue WITHOUT needing a restart. Every 90s, resume any
    row whose previous confirm loop has certainly ended (age > deadline+buffer),
    so there is no overlap with an in-flight loop -> no double-injection.
    `ts` is bumped per window; `first_ts` (the confirm baseline) never moves."""
    while True:
        await asyncio.sleep(90)
        try:
            now = int(time.time())
            stale = [r for r in _load_pending()
                     if now - r.get("ts", now) > _DELIVERY_DEADLINE_SECS + 30]
            for r in sorted(stale, key=lambda x: x.get("ts", 0)):
                _djournal("sweep_retry", r.get("id", ""))
                ok = await _deliver_and_confirm(r.get("tagged", ""), r.get("needle", ""),
                                                r.get("id", ""), initial_send=True)
                if not ok:
                    _update_pending(r.get("id"), ts=int(time.time()),
                                    first_ts=r.get("first_ts") or r.get("ts"))
                await asyncio.sleep(1)
        except Exception:
            pass


async def api_chat_send(request: Request) -> JSONResponse:
    """Inject a message into the tmux session. Response comes via /api/chat/stream or history."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        message = str(body.get("message", "")).strip()
        # P20 operator identity: the page self-declares WHO is speaking
        # (Corey / Russell / other). Sanitize to a short, safe display token.
        _sender_raw = str(body.get("sender", "")).strip()
        sender_sel = re.sub(r"[^\w .\-]", "", _sender_raw)[:40].strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    # PROBE LOGGING — catch ANY message containing "PROBE" for investigation
    if "PROBE" in message.upper():
        import datetime as _dt, logging as _logging, json as _json
        _client = request.client.host if request.client else "unknown"
        _headers = dict(request.headers)
        _log_entry = {
            "timestamp": _dt.datetime.utcnow().isoformat() + "Z",
            "endpoint": "chat_send",
            "client_ip": _client,
            "message": message[:500],
            "headers": _headers,
        }
        with open("/tmp/probe-trace.log", "a") as _f:
            _f.write(_json.dumps(_log_entry) + "\n")
        _logging.getLogger("uvicorn.error").warning("PROBE via chat_send: client=%s msg=%s headers=%s", _client, message[:120], _json.dumps(_headers))

    # Sender attribution: prepend the operator's name so Primary knows WHO is
    # speaking (e.g. "[Russell] [portal] <msg>"). P20: an explicit page-selected
    # operator WINS over the token-derived name (the token can be shared/stale —
    # that was the source of the blind, conflicting two-operator signals). Falls
    # back to the token-derived name, then "operator" — fully backward compatible.
    operator = sender_sel or resolve_operator(request)

    # Save to portal chat log for history (with sender so every operator's page
    # and a plain refresh render WHO said each line — P20 live attribution).
    _saved = _save_portal_message(message, role="user", sender=operator)
    msg_id = _saved.get("id", f"portal-{int(time.time() * 1000)}")

    # Tag injection source so tmux pane shows where input came from
    host = request.headers.get("referer", "")
    if "react" in host:
        source_tag = "[portal-react]"
    else:
        source_tag = "[portal]"
    tagged = f"[{operator}] {source_tag} {message}"

    # DURABLE delivery (task#36): queue first, then deliver-and-CONFIRM. A distinctive
    # tail of the operator's message is the "needle" we look for in the composer.
    needle = _dnorm(message)
    needle = needle[-24:] if len(needle) > 24 else needle
    _queue_pending(msg_id, tagged, needle)

    target = _find_primary_pane()  # resolve to actual Primary pane id, not session name
    is_pane = isinstance(target, str) and target.startswith("%")
    if is_pane and _send_text(target, tagged):
        _djournal("sent_inline", msg_id, pane=target)
        # Sent into the live pane. Confirm-by-readback + nudge (replaces the old
        # fixed 2.5s window); the queue row is cleared once the composer no longer
        # holds our text, else the replay sweep redelivers on recovery.
        asyncio.ensure_future(_deliver_and_confirm(tagged, needle, msg_id, initial_send=False))
        return JSONResponse({"status": "sent", "delivered": "pending",
                             "timestamp": int(time.time())})
    else:
        _djournal("inline_send_unavailable", msg_id, target=str(target))
        # No live Primary pane, or send-keys failed. DO NOT report a false "sent"
        # with the row already committed (that was the old 500-with-stranded-row
        # bug). The message stays queued; a background retry re-resolves the pane
        # and redelivers, and the startup replay sweep covers a full restart.
        asyncio.ensure_future(_deliver_and_confirm(tagged, needle, msg_id, initial_send=True))
        return JSONResponse({"status": "queued", "delivered": False,
                             "note": "Primary is busy/unreachable — message stored and will be delivered on recovery.",
                             "timestamp": int(time.time())})


async def api_notify(request: Request) -> JSONResponse:
    """Save a system notification to portal chat (role=assistant, no tmux injection)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        message = str(body.get("message", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    source = str(body.get("source", "")).strip() or None
    entry = _save_portal_message(message, role="assistant", source=source)
    return JSONResponse({"status": "saved", "id": entry["id"], "timestamp": entry["timestamp"]})


async def ws_chat(websocket: WebSocket) -> None:
    """Stream new chat messages via WebSocket. Polls JSONL log for new entries."""
    token = websocket.query_params.get("token", "")
    # Accept the legacy single bearer token OR any per-operator token,
    # matching REST check_auth(). Per-operator tokens (e.g. Russell's) were
    # previously rejected with 4401, which the frontend treats as permanent
    # (no reconnect) — leaving the chat with zero live message pushes.
    if token != BEARER_TOKEN and token not in OPERATOR_TOKENS:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    _chat_ws_clients.add(websocket)
    seen_texts: dict[str, int] = {}  # id -> len(text) of last sent version

    # Send initial batch of recent messages
    messages = _parse_all_messages(last_n=200)
    for msg in messages:
        seen_texts[msg["id"]] = len(msg.get("text", ""))

    try:
        _ping_counter = 0
        while True:
            messages = _parse_all_messages(last_n=200)
            for msg in messages:
                msg_id = msg["id"]
                msg_len = len(msg.get("text", ""))
                prev_len = seen_texts.get(msg_id, -1)
                # Send if new message OR if text grew significantly (streaming completion)
                if prev_len < 0 or (msg_len > prev_len + 20):
                    seen_texts[msg_id] = msg_len
                    # Guard: never send noise messages (stray pipes, empty) to frontend
                    _ws_text = msg.get("text", "").strip()
                    if not _ws_text or len(_ws_text) < 3:
                        continue
                    if len(_ws_text) <= 2 and not any(c.isalnum() for c in _ws_text):
                        continue  # Skip stray pipe/bracket/noise artifacts
                    _mirror_to_portal_log(msg)  # Persist so page refreshes don't lose messages
                    await websocket.send_text(json.dumps(msg))
            # Keepalive ping every ~30s to prevent proxy timeout
            _ping_counter += 1
            if _ping_counter >= 37:
                await websocket.send_text('{"type":"ping"}')
                _ping_counter = 0
            await asyncio.sleep(0.8)  # Fast poll for near-real-time message delivery
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _chat_ws_clients.discard(websocket)


async def api_chat_upload(request: Request) -> JSONResponse:
    """Accept a file upload, save to UPLOADS_DIR + docs/from-telegram/, log to portal chat, inject tmux notification."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        form = await request.form()
        uploaded = form.get("file")
        if not uploaded or not hasattr(uploaded, "read"):
            return JSONResponse({"error": "no file"}, status_code=400)

        caption = str(form.get("caption", "")).strip()

        content = await uploaded.read()
        if len(content) > UPLOAD_MAX_BYTES:
            return JSONResponse({"error": "file too large (max 50 MB)"}, status_code=413)

        original_name = getattr(uploaded, "filename", None) or "upload"
        # Sanitize: keep alphanumerics, dots, dashes, underscores
        safe_name = "".join(c for c in original_name if c.isalnum() or c in "._-") or "upload"
        timestamp_ms = int(time.time() * 1000)
        stored_name = f"{timestamp_ms}_{safe_name}"
        dest = UPLOADS_DIR / stored_name
        dest.write_bytes(content)

        # Also save a named copy to portal_uploads/from-portal/ for easy reference
        from_portal_dir = UPLOADS_DIR / "from-portal"
        from_portal_dir.mkdir(parents=True, exist_ok=True)
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        portal_copy_name = f"portal_{timestamp_str}_{safe_name}"
        portal_copy_path = from_portal_dir / portal_copy_name
        portal_copy_path.write_bytes(content)

        # Detect if this is an image
        is_image = safe_name.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp'))

        # Save ONE combined user message to portal chat log (image + caption together)
        # Include stored_name so frontend can render inline image via /api/chat/uploads/
        chat_text = f"[Image: {stored_name}]" if is_image else f"[PORTAL_FILE:{stored_name}:{original_name}]"
        if caption:
            chat_text += f"\n{caption}"
        user_entry = _save_portal_message(chat_text, role="user")

        # Inject notification into AI's tmux session (mirrors Telegram bridge pattern)
        # CRITICAL: Must be SINGLE LINE — multi-line paste triggers Claude Code's
        # "Pasted text" confirmation prompt and blocks automatic processing.
        notify_parts = [f"[Portal Upload from {HUMAN_NAME}] File saved to: {portal_copy_path}"]
        if caption:
            notify_parts.append(f"INSTRUCTIONS from {HUMAN_NAME}: {caption}")
        if is_image:
            notify_parts.append(f"[Image: {original_name} — USE Read tool on {portal_copy_path} TO VIEW]")
        notification = " ".join(notify_parts)

        session = get_tmux_session()
        # LARGE-MESSAGE FIX (task#36): route through _send_text so the upload
        # notification uses the same length-safe load-buffer/paste-buffer path
        # (the old inline `send-keys -l` here had the identical >=16 KiB reject
        # bug — a long caption/path could silently drop the whole notification).
        tmux_ok = _send_text(session, notification)
        if tmux_ok:
            # 5x Enter retries — ensures Claude processes even if busy
            async def _retry_enters():
                for _ in range(5):
                    await asyncio.sleep(0.5)
                    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"],
                                   check=False, stderr=subprocess.DEVNULL)
            asyncio.ensure_future(_retry_enters())

        # Auto-acknowledge in portal chat so user sees confirmation immediately
        ack_parts = [f"Received your file: {original_name}"]
        if is_image:
            ack_parts.append("(image — viewing now)")
        if caption:
            ack_parts.append(f'Instructions noted: "{caption}"')
        if tmux_ok:
            ack_parts.append("Processing...")
        else:
            ack_parts.append("(tmux injection failed — will check docs/from-telegram/ manually)")
        ack_text = " ".join(ack_parts)
        ack_entry = _save_portal_message(ack_text, role="assistant")

        return JSONResponse({
            "ok": True,
            "filename": stored_name,
            "original": original_name,
            "path": str(dest),
            "copy_path": str(portal_copy_path),
            "size": len(content),
            "ack": ack_text,
            "user_msg_id": user_entry["id"],
            "ack_msg_id": ack_entry["id"],
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_chat_serve_upload(request: Request) -> Response:
    """Serve an uploaded file. Token auth via query param or Bearer header."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    filename = request.path_params.get("filename", "")
    # Prevent path traversal
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    filepath = UPLOADS_DIR / filename
    if not filepath.exists() or not filepath.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(filepath))


async def api_download(request: Request) -> Response:
    """Serve a file download from whitelisted directories."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    filepath_str = request.query_params.get("path", "")
    if not filepath_str:
        return JSONResponse({"error": "missing 'path' query parameter"}, status_code=400)
    try:
        filepath = Path(filepath_str).resolve()
    except Exception:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    # Security: reject path traversal and check whitelist
    if ".." in filepath_str:
        return JSONResponse({"error": "path traversal not allowed"}, status_code=403)
    allowed = any(
        filepath == d or d in filepath.parents
        for d in DOWNLOAD_ALLOWED_DIRS
    )
    if not allowed:
        return JSONResponse({"error": f"path not in allowed directories"}, status_code=403)
    if not filepath.exists() or not filepath.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)
    return FileResponse(str(filepath), filename=filepath.name)


async def api_download_list(request: Request) -> JSONResponse:
    """List files in an allowed directory."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    dir_str = request.query_params.get("dir", "")
    if not dir_str:
        # Return list of allowed base directories
        dirs = [{"path": str(d), "name": d.name, "exists": d.exists()} for d in DOWNLOAD_ALLOWED_DIRS]
        return JSONResponse({"directories": dirs})
    try:
        dirpath = Path(dir_str).resolve()
    except Exception:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    allowed = any(
        dirpath == d or d in dirpath.parents
        for d in DOWNLOAD_ALLOWED_DIRS
    )
    if not allowed:
        return JSONResponse({"error": "directory not in allowed list"}, status_code=403)
    if not dirpath.exists() or not dirpath.is_dir():
        return JSONResponse({"error": "directory not found"}, status_code=404)
    items = []
    for item in sorted(dirpath.iterdir()):
        items.append({
            "name": item.name,
            "path": str(item),
            "is_dir": item.is_dir(),
            "size": item.stat().st_size if item.is_file() else None,
        })
    return JSONResponse({"dir": str(dirpath), "items": items})


# ---------------------------------------------------------------------------
# WhatsApp Bridge Endpoints
# ---------------------------------------------------------------------------

async def api_deliverable(request: Request) -> JSONResponse:
    """Accept a file deliverable from the AI, copy to uploads, post download link to portal chat."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        src_path_str = body.get("path", "").strip()
        display_name = body.get("name", "").strip()
        caption = body.get("message", "").strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not src_path_str:
        return JSONResponse({"error": "missing 'path'"}, status_code=400)
    src_path = Path(src_path_str).resolve()
    if not src_path.exists() or not src_path.is_file():
        return JSONResponse({"error": f"file not found: {src_path_str}"}, status_code=404)

    if not display_name:
        display_name = src_path.name
    safe_name = "".join(c for c in display_name if c.isalnum() or c in "._-") or "deliverable"
    stored_name = f"{int(time.time() * 1000)}_{safe_name}"
    dest = UPLOADS_DIR / stored_name
    dest.write_bytes(src_path.read_bytes())

    serve_url = f"/api/chat/uploads/{stored_name}"
    # Use PORTAL_FILE tag format — rendered by portal HTML as styled download card
    lines = []
    if caption:
        lines.append(caption)
    lines.append(f"[PORTAL_FILE:{stored_name}:{display_name}]")
    _save_portal_message("\n\n".join(lines), role="assistant")

    return JSONResponse({"ok": True, "filename": stored_name, "url": serve_url})


async def api_whatsapp_qr(request: Request) -> Response:
    """Serve the WhatsApp QR code PNG image (written by whatsapp-bridge)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    qr_path = UPLOADS_DIR / "whatsapp-qr.png"
    if not qr_path.exists():
        return JSONResponse({"error": "no_qr", "message": "No QR code available"}, status_code=404)
    return FileResponse(str(qr_path), media_type="image/png")


async def api_whatsapp_status(request: Request) -> JSONResponse:
    """Return WhatsApp connection status (written by whatsapp-bridge)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    status_path = UPLOADS_DIR / "whatsapp-status.json"
    if not status_path.exists():
        return JSONResponse({"status": "unknown", "updated": None})
    try:
        data = json.loads(status_path.read_text())
        return JSONResponse(data)
    except Exception:
        return JSONResponse({"status": "error", "updated": None})


async def github_webhook(request: Request) -> JSONResponse:
    """Handle GitHub push webhook — validate signature, deploy portal files, restart server."""
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode()
    body = await request.body()

    # Validate signature
    sig_header = request.headers.get("X-Hub-Signature-256", "")
    import hmac
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig_header):
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    # Only act on pushes to main/master
    try:
        payload = json.loads(body)
    except Exception:
        return JSONResponse({"error": "bad payload"}, status_code=400)

    ref = payload.get("ref", "")
    if ref not in ("refs/heads/main", "refs/heads/master"):
        return JSONResponse({"status": "ignored", "ref": ref})

    # Run deploy in background
    asyncio.create_task(_run_deploy())
    return JSONResponse({"status": "deploying"})


async def _run_deploy():
    """Pull latest from git, copy portal files, restart server."""
    repo = Path.home() / "purebrain-onboarding"
    portal_dir = Path.home() / "purebrain_portal"
    log_path = Path("/tmp/deploy.log")

    def log(msg):
        with log_path.open("a") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")

    try:
        log("Deploy triggered")
        result = subprocess.run(
            ["git", "pull"],
            cwd=repo,
            capture_output=True, text=True, timeout=60
        )
        log(f"git pull: {result.stdout.strip()} {result.stderr.strip()}")

        for fname in ("portal-pb-styled.html", "refer-and-earn.html", "portal_server.py"):
            src = repo / "portal" / fname
            dst = portal_dir / fname
            if src.exists():
                import shutil
                shutil.copy2(src, dst)
                log(f"Copied {fname}")

        log("Restarting portal server via detached watchdog...")
        # Launch restart in a new session so the pkill doesn't kill this process too
        restart_cmd = (
            f"sleep 2 && pkill -f portal_server.py; sleep 2 && "
            f"cd {portal_dir} && nohup python3 portal_server.py >> /tmp/portal.log 2>&1 &"
        )
        subprocess.Popen(
            ["bash", "-c", restart_cmd],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log("Deploy complete — detached restart launched")
    except Exception as e:
        log(f"Deploy error: {e}")


def _get_team_lead_pane_ids():
    """Read Claude Code team configs to find all registered team lead pane IDs."""
    team_panes = set()
    try:
        teams_dir = Path.home() / ".claude" / "teams"
        for config_path in teams_dir.glob("*/config.json"):
            try:
                with open(config_path) as f:
                    config = json.load(f)
                for member in config.get("members", []):
                    pane_id = member.get("tmuxPaneId", "")
                    if pane_id and pane_id.startswith("%"):
                        team_panes.add(pane_id)
            except Exception:
                continue
    except Exception:
        pass
    return team_panes


def _score_pane_content(pane_id):
    """Score pane content: positive for Primary markers, negative for team lead markers."""
    try:
        cap = subprocess.check_output(
            ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-30"],
            stderr=subprocess.DEVNULL, text=True
        )
        score = 0
        for marker in ["BOOP #", "[portal]", "Standing by", "sprint-mode",
                        "leader-haiku", "margin/primary", "[SUPPORT REQUEST"]:
            if marker in cap:
                score += 1
        for marker in ["You are being launched as", "Read your manifest:",
                        "SendMessage results to Primary", "@fleet-lead",
                        "@midwife-lead", "@infra-lead", "@dev-lead"]:
            if marker in cap:
                score -= 3
        return score
    except Exception:
        return 0


def _find_primary_pane():
    """Find the tmux pane ID running the primary Claude Code instance (@main).

    Strategy (layered):
    1. BEST: Exclude team lead panes (from team config) — structural, immune to content
    2. FALLBACK: Content scoring — Primary vs team lead markers
    3. LAST RESORT: pane_index=0 in session
    """
    session = get_tmux_session()
    try:
        out = subprocess.check_output(
            ["tmux", "list-panes", "-t", session, "-F",
             "#{pane_id} #{pane_current_command} #{pane_index}"],
            stderr=subprocess.DEVNULL, text=True
        )

        claude_panes = []  # (pane_id, pane_index)
        index_zero_pane = None
        for line in out.splitlines():
            parts = line.strip().split()
            if len(parts) < 3:
                continue
            pane_id, command, pane_idx = parts[0], parts[1], parts[2]
            if "claude" in command:
                claude_panes.append((pane_id, pane_idx))
                if pane_idx == "0":
                    index_zero_pane = pane_id

        if not claude_panes:
            return session  # fallback to session target

        if len(claude_panes) == 1:
            return claude_panes[0][0]  # only one claude pane — must be primary

        # Layer 1: Team config exclusion
        team_lead_panes = _get_team_lead_pane_ids()
        non_team_panes = [(pid, idx) for pid, idx in claude_panes if pid not in team_lead_panes]

        if len(non_team_panes) == 1:
            return non_team_panes[0][0]

        # Layer 2: Content scoring
        candidates = non_team_panes if non_team_panes else claude_panes
        if len(candidates) > 1:
            scored = []
            for pane_id, pane_idx in candidates:
                score = _score_pane_content(pane_id)
                scored.append((score, pane_id))
            scored.sort(reverse=True)
            if scored[0][0] > scored[1][0]:  # clear winner
                return scored[0][1]

        # Layer 3: pane_index=0 fallback
        if index_zero_pane:
            return index_zero_pane

        # Ultimate fallback: first non-team pane, or first claude pane
        if non_team_panes:
            return non_team_panes[0][0]
        return claude_panes[0][0]

    except Exception:
        return session


async def ws_terminal(websocket: WebSocket) -> None:
    """Stream tmux pane content via WebSocket. Read-only."""
    token = websocket.query_params.get("token", "")
    # Accept legacy bearer OR any per-operator token, matching REST check_auth().
    if token != BEARER_TOKEN and token not in OPERATOR_TOKENS:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    pane_target = _find_primary_pane()
    last_content = ""

    try:
        _ping_counter = 0
        _reeval_counter = 0
        while True:
            try:
                content = subprocess.check_output(
                    ["tmux", "capture-pane", "-t", pane_target, "-p"],
                    stderr=subprocess.DEVNULL, text=True
                ).strip()
            except subprocess.CalledProcessError:
                content = "[tmux session not found]"

            if content != last_content:
                await websocket.send_text(content)
                last_content = content

            # Keepalive ping every ~30s to prevent proxy timeout
            _ping_counter += 1
            if _ping_counter >= 60:
                await websocket.send_text('{"type":"ping"}')
                _ping_counter = 0

            # Re-evaluate pane target every ~30s to handle team lead lifecycle
            _reeval_counter += 1
            if _reeval_counter >= 60:  # 60 * 0.5s = 30s
                new_target = _find_primary_pane()
                if new_target != pane_target:
                    pane_target = new_target
                    last_content = ""  # force content refresh
                _reeval_counter = 0

            await asyncio.sleep(0.5)
    except (WebSocketDisconnect, Exception):
        pass


# P3 fix (2026-06-14): /api/context previously f.read() the ENTIRE multi-MB
# session JSONL on every dashboard poll (pinned portal CPU ~85%). The usage
# data we need is always near the END of the file, so we now tail-read the last
# _CONTEXT_TAIL_BYTES and cache the parsed result for _CONTEXT_TTL seconds so
# rapid polls don't re-read at all. Backup: portal_server.py.bak.p3-apicontext.*
_CONTEXT_TAIL_BYTES = 262_144   # last 256 KB — always contains many usage entries
_CONTEXT_TTL = 4.0              # seconds; rapid dashboard polls reuse this result
_context_cache: dict = {}       # path -> (mtime, fsize, expires_at, result_dict)


def _read_context_usage(latest):
    """Tail-read the latest session JSONL and return usage dict.
    Reads only the last 256 KB (vs the full multi-MB file), tolerates an
    oversized partial first line, and never decode-errors."""
    stat = latest.stat()
    mtime = stat.st_mtime
    fsize = stat.st_size
    now = time.time()

    cached = _context_cache.get(str(latest))
    # Reuse cache if file unchanged OR within the short TTL window.
    if cached:
        c_mtime, c_fsize, c_expires, c_result = cached
        if (c_mtime == mtime and c_fsize == fsize) or now < c_expires:
            return c_result

    input_tokens = 0
    cache_read = 0
    cache_creation = 0
    with open(latest, "rb") as f:
        if fsize > _CONTEXT_TAIL_BYTES:
            f.seek(-_CONTEXT_TAIL_BYTES, 2)
            f.readline()  # drop the (possibly partial) first line of the tail
        raw = f.read()
    # errors='ignore' so the ~112 KB oversized line / any byte split can't crash us
    for line in reversed(raw.decode("utf-8", errors="ignore").splitlines()):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, KeyError):
            continue  # partial/oversized line — skip gracefully
        usage = entry.get("usage") or entry.get("message", {}).get("usage")
        if usage and isinstance(usage, dict):
            t = usage.get("input_tokens", 0)
            if t:
                input_tokens = t
                cache_read = usage.get("cache_read_input_tokens", 0)
                cache_creation = usage.get("cache_creation_input_tokens", 0)
                break  # found last usage entry -- stop immediately

    result = (input_tokens, cache_read, cache_creation)
    _context_cache[str(latest)] = (mtime, fsize, now + _CONTEXT_TTL, result)
    return result


async def api_context(request: Request) -> JSONResponse:
    """Return real context window usage from the latest Claude session JSONL."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        MAX_TOKENS = 1_000_000  # Opus 4.6 with 1M context
        log_root = _resolve_log_root()
        logs = _sorted_session_logs(log_root)
        if not logs:
            return JSONResponse({"input_tokens": 0, "max_tokens": MAX_TOKENS, "pct": 0})

        latest = logs[0]
        # Tail-read + TTL cache (P3): no more full-file read on every poll
        input_tokens, cache_read, cache_creation = _read_context_usage(latest)

        total = input_tokens + cache_read + cache_creation
        pct = round(min(total / MAX_TOKENS * 100, 100), 1)
        return JSONResponse({
            "input_tokens": input_tokens,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "total_tokens": total,
            "max_tokens": MAX_TOKENS,
            "pct": pct,
            "session_id": latest.stem,
            "model": "claude-opus-5-5[1m]",
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


_RESUME_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Env knobs restart-self.sh honours for TESTING; stripped so the portal always gets production gates.
_RESUME_TEST_ENV_KEYS = ("PROJ_BASE", "IDENTITY_FILE", "MODEL", "RESUME_MAX_BYTES", "RESUME_MIN_BYTES",
                         "MIN_TURNS", "RESUME_MAX_AGE_HOURS")


def _pick_resume_target(script: "Path | None" = None, env_overrides: "dict | None" = None):
    """Select the resume target with EXACTLY restart-self.sh's gates (added 2026-09-22).

    Single source of truth: runs the shipped restart-self.sh in its read-only DRYRUN=1 mode
    (it runs pick_resume_uuid and exits before touching any process/tmux/file) and parses
    its DECISION line. Gates: own-project only, non-stub (MIN_TURNS), not crashed, not oversized
    (>RESUME_MAX_BYTES 25MB), not stale (>RESUME_MAX_AGE_HOURS 12h). restart-self.sh is NOT
    refactored because it is shipped self-contained to fleet CIVs via /from-witness/.
    Returns (uuid_or_None, selection_line). Any failure -> (None, reason) -> FRESH (fail-safe).
    """
    script = Path(script) if script else Path.home() / "civ" / "tools" / "restart-self.sh"
    if not script.is_file():
        return None, f"selector-missing:{script}"
    env = dict(os.environ)
    for k in _RESUME_TEST_ENV_KEYS:
        env.pop(k, None)
    env.update(env_overrides or {})
    env["DRYRUN"] = "1"
    try:
        out = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True,
                             timeout=60).stdout
    except Exception as e:  # timeout / exec failure -> fresh, never a blind resume
        return None, f"selector-failed:{type(e).__name__}"
    selection = next((l for l in out.splitlines() if l.startswith("Selection:")), "")
    decision = next((l for l in out.splitlines() if l.startswith("DECISION:")), "")
    parts = decision.split()
    if len(parts) == 3 and parts[1] == "RESUME" and _RESUME_UUID_RE.match(parts[2]):
        return parts[2], selection
    if decision.startswith("DECISION: FRESH"):
        return None, selection
    return None, f"selector-unparsable:{decision[:80]!r}"


async def api_resume(request: Request) -> JSONResponse:
    """Launch a new Claude instance resuming the newest VALID conversation session.

    Target chosen by _pick_resume_target (restart-self.sh's gates). If no candidate passes,
    start a FRESH memory-preserving session instead of resuming a stale/oversized/foreign/stub log.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        session_id, selection = await asyncio.to_thread(_pick_resume_target)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        tmux_session = f"{CIV_NAME}-primary-{timestamp}"
        project_dir = str(Path.home())
        # Kill any stale {civ}-primary-* sessions so prefix-matching stays unambiguous
        try:
            old = subprocess.check_output(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                stderr=subprocess.DEVNULL, text=True
            ).splitlines()
            for s in old:
                if s.startswith(f"{CIV_NAME}-primary-"):
                    subprocess.run(["tmux", "kill-session", "-t", s],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        # Write session name so portal can track it
        marker = Path.home() / ".current_session"
        marker.write_text(tmux_session)
        claude_cmd = "claude --model 'claude-opus-5-5[1m]' --dangerously-skip-permissions"
        if session_id:
            claude_cmd += f" --resume {session_id}"
        subprocess.Popen(
            ["tmux", "new-session", "-d", "-s", tmux_session, "-c", project_dir, claude_cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return JSONResponse({"status": "resuming" if session_id else "fresh",
                             "session_id": session_id, "tmux": tmux_session,
                             "selection": selection})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_restart(request: Request) -> JSONResponse:
    """Restart the Claude session via restart-self.sh or fallback to inline tmux creation."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        restart_script = Path.home() / "civ" / "tools" / "restart-self.sh"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        tmux_session = f"{CIV_NAME}-primary-{timestamp}"

        if restart_script.exists():
            # Use restart-self.sh (runs async — it sleeps internally)
            subprocess.Popen(
                ["bash", str(restart_script)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(Path.home())
            )
            return JSONResponse({
                "status": "restarting",
                "method": "restart-self.sh",
                "message": "restart-self.sh launched — session will be available in ~45 seconds"
            })
        else:
            # Fallback: create tmux session with claude directly
            project_dir = str(Path.home())
            # Kill old primary sessions
            try:
                old = subprocess.check_output(
                    ["tmux", "list-sessions", "-F", "#{session_name}"],
                    stderr=subprocess.DEVNULL, text=True
                ).splitlines()
                for s in old:
                    if s.startswith(f"{CIV_NAME}-primary-"):
                        subprocess.run(["tmux", "kill-session", "-t", s],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            # Write marker
            marker = Path.home() / ".current_session"
            marker.write_text(tmux_session)
            claude_cmd = "claude --dangerously-skip-permissions"
            subprocess.Popen(
                ["tmux", "new-session", "-d", "-s", tmux_session, "-c", project_dir, claude_cmd],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return JSONResponse({
                "status": "restarting",
                "method": "fallback",
                "tmux": tmux_session,
                "message": "New tmux session launched with Claude"
            })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_panes(request: Request) -> JSONResponse:
    """Return all tmux panes with their current content."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    session = get_tmux_session()
    try:
        out = subprocess.check_output(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_id}\t#{pane_title}\t#{session_name}:#{window_index}.#{pane_index}"],
            stderr=subprocess.DEVNULL, text=True
        )
        panes = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t", 2)
            pane_id = parts[0] if len(parts) > 0 else ""
            title = parts[1] if len(parts) > 1 else pane_id
            target = parts[2] if len(parts) > 2 else pane_id
            # Only include panes from the current CIV session
            session_name = session.split(":")[0] if ":" in session else session
            if session_name not in target and session not in target:
                continue
            try:
                capture = subprocess.check_output(
                    ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-30"],
                    stderr=subprocess.DEVNULL, text=True
                ).strip()
            except subprocess.CalledProcessError:
                capture = ""
            panes.append({"id": pane_id, "title": title or pane_id, "target": target, "content": capture})
        return JSONResponse({"panes": panes})
    except Exception as e:
        return JSONResponse({"error": str(e), "panes": []})


async def api_inject_pane(request: Request) -> JSONResponse:
    """Inject a command into a specific tmux pane by pane_id."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    pane_id = body.get("pane_id", "").strip()
    message = body.get("message", "").strip()
    # PROBE LOGGING — catch ANY message containing "PROBE" for investigation
    if "PROBE" in message.upper():
        import datetime as _dt, logging as _logging, json as _json
        _client = request.client.host if request.client else "unknown"
        _headers = dict(request.headers)
        _log_entry = {
            "timestamp": _dt.datetime.utcnow().isoformat() + "Z",
            "endpoint": "inject_pane",
            "client_ip": _client,
            "pane_id": pane_id,
            "message": message[:500],
            "headers": _headers,
        }
        with open("/tmp/probe-trace.log", "a") as _f:
            _f.write(_json.dumps(_log_entry) + "\n")
        _logging.getLogger("uvicorn.error").warning("PROBE via inject_pane: client=%s pane=%s msg=%s headers=%s", _client, pane_id, message[:120], _json.dumps(_headers))
    if not pane_id or not message:
        return JSONResponse({"error": "pane_id and message required"}, status_code=400)
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", message],
                       check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"],
                       check=True, stderr=subprocess.DEVNULL)
        return JSONResponse({"status": "sent"})
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


# ---------------------------------------------------------------------------
# BOOP / Skills Endpoints (from ACG — for Settings panel)
# ---------------------------------------------------------------------------
SKILLS_DIR = Path.home() / ".claude" / "skills"
BOOP_CONFIG_FILE = SCRIPT_DIR / "boop_config.json"


async def api_compact_status(request: Request) -> JSONResponse:
    """Check if Claude is currently compacting context (shows in tmux pane)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pane = _find_primary_pane()
    try:
        content = subprocess.check_output(
            ["tmux", "capture-pane", "-t", pane, "-p", "-S", "-20"],
            stderr=subprocess.DEVNULL, text=True
        )
        # Match the specific Claude Code compacting message (not "auto-compact" warnings)
        compacting = "Compacting (ctrl+o" in content or "Compacting…" in content
        return JSONResponse({"compacting": compacting})
    except Exception:
        return JSONResponse({"compacting": False})


async def api_boop_config(request: Request) -> JSONResponse:
    """GET: read active BOOP config. POST: update active_command and/or cadence_minutes."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.method == "POST":
        try:
            body = await request.json()
            cfg = json.loads(BOOP_CONFIG_FILE.read_text()) if BOOP_CONFIG_FILE.exists() else {}
            g = cfg.setdefault("global", {})
            if "active_command" in body:
                g["active_command"] = str(body["active_command"])
            if "cadence_minutes" in body:
                g["cadence_minutes"] = int(body["cadence_minutes"])
            if "paused" in body:
                g["paused"] = bool(body["paused"])
            BOOP_CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
            return JSONResponse({"ok": True, "active_command": g.get("active_command"),
                                 "cadence_minutes": g.get("cadence_minutes")})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    # GET
    try:
        cfg = json.loads(BOOP_CONFIG_FILE.read_text()) if BOOP_CONFIG_FILE.exists() else {}
        g = cfg.get("global", {})
        return JSONResponse({
            "active_command": g.get("active_command", "/sprint-mode"),
            "cadence_minutes": g.get("cadence_minutes", 30),
            "paused": g.get("paused", False),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_boops_list(request: Request) -> JSONResponse:
    """List available BOOP/skill entries from the skills directory."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    boops = []
    if SKILLS_DIR.exists():
        for entry in sorted(SKILLS_DIR.iterdir()):
            if entry.is_dir():
                skill_file = entry / "SKILL.md"
                if skill_file.exists():
                    boops.append({"name": entry.name, "path": str(skill_file)})
    return JSONResponse({"boops": boops})


async def api_boop_read(request: Request) -> JSONResponse:
    """Read the content of a specific BOOP/skill."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    name = request.path_params.get("name", "")
    if ".." in name or "/" in name:
        return JSONResponse({"error": "invalid name"}, status_code=400)
    skill_file = SKILLS_DIR / name / "SKILL.md"
    if not skill_file.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    content = skill_file.read_text(encoding="utf-8", errors="replace")
    return JSONResponse({"name": name, "content": content})


# BOOP daemon control — session name and script path for toggle/status
BOOP_TMUX_SESSION = "boop-daemon"
BOOP_DAEMON_SCRIPT = Path.home() / "civ" / "tools" / "boop-daemon.sh"


async def api_boop_status(request: Request) -> JSONResponse:
    """Check if the BOOP daemon tmux session is running."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", BOOP_TMUX_SESSION],
            capture_output=True
        )
        running = result.returncode == 0
        pid = None
        if running:
            try:
                pid_result = subprocess.run(
                    ["tmux", "list-panes", "-t", BOOP_TMUX_SESSION, "-F", "#{pane_pid}"],
                    capture_output=True, text=True
                )
                if pid_result.returncode == 0 and pid_result.stdout.strip():
                    pid = int(pid_result.stdout.strip().split()[0])
            except (ValueError, Exception):
                pass
        return JSONResponse({"active": running, "pid": pid})
    except Exception:
        return JSONResponse({"active": False, "pid": None})


async def api_boop_toggle(request: Request) -> JSONResponse:
    """Toggle the BOOP daemon on/off via tmux session."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", BOOP_TMUX_SESSION],
            capture_output=True
        )
        currently_running = result.returncode == 0

        if currently_running:
            subprocess.run(
                ["tmux", "kill-session", "-t", BOOP_TMUX_SESSION],
                capture_output=True
            )
            return JSONResponse({"active": False, "action": "stopped"})
        else:
            if not BOOP_DAEMON_SCRIPT.exists():
                return JSONResponse(
                    {"error": f"boop-daemon.sh not found at {BOOP_DAEMON_SCRIPT}"},
                    status_code=500
                )
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", BOOP_TMUX_SESSION,
                 f"bash {BOOP_DAEMON_SCRIPT} > /tmp/boop-daemon.log 2>&1"],
                capture_output=True
            )
            return JSONResponse({"active": True, "action": "started"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Claude OAuth Auth Endpoints
# ---------------------------------------------------------------------------
async def api_claude_auth_status(request: Request) -> JSONResponse:
    """Check if Claude is authenticated (has valid OAuth credentials).

    Uses a marker file (.portal-human-auth) to distinguish between credentials
    left by the birth pipeline and credentials from a human completing OAuth
    via the portal.  Without the marker, always returns authenticated=False
    so the auth modal appears on first visit.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        if not CREDENTIALS_FILE.exists():
            return JSONResponse({"authenticated": False, "account": None, "expires_at": None})
        creds = json.loads(CREDENTIALS_FILE.read_text())
        oauth = creds.get("claudeAiOauth", {})
        if not oauth.get("accessToken"):
            return JSONResponse({"authenticated": False, "account": None, "expires_at": None})
        # If no human has completed OAuth via the portal yet, treat as
        # unauthenticated even if birth-pipeline credentials exist.
        if not HUMAN_AUTH_MARKER.exists():
            return JSONResponse({"authenticated": False, "account": oauth.get("account"),
                                 "expires_at": oauth.get("expiresAt"),
                                 "needs_human_auth": True})
        expires_at = oauth.get("expiresAt", 0)
        now_ms = int(time.time() * 1000)
        # Claude Code refreshes tokens in memory without updating the file.
        # If the tmux session is alive and Claude is running, trust it — the
        # expiresAt in credentials.json is stale, not reality.
        tmux_alive = False
        try:
            subprocess.check_output(["tmux", "has-session", "-t", get_tmux_session()],
                                    stderr=subprocess.DEVNULL)
            tmux_alive = True
        except Exception:
            pass
        if expires_at and expires_at < now_ms and not tmux_alive:
            return JSONResponse({"authenticated": False, "account": oauth.get("account"),
                                 "expires_at": expires_at})
        return JSONResponse({
            "authenticated": True, "account": oauth.get("account"),
            "expires_at": expires_at, "subscription": oauth.get("subscriptionType"),
        })
    except Exception:
        return JSONResponse({"authenticated": False, "account": None, "expires_at": None})


def _is_claude_running_in_pane(pane: str) -> bool:
    """Check if Claude Code is the active process in the given tmux pane."""
    try:
        cmd = subprocess.check_output(
            ["tmux", "display-message", "-t", pane, "-p", "#{pane_current_command}"],
            stderr=subprocess.DEVNULL, text=True
        ).strip().lower()
        return "claude" in cmd or "node" in cmd
    except Exception:
        return False


AUTH_PANE_NAME = "auth-pane"


def _get_or_create_auth_pane() -> str:
    """Create a dedicated tmux window for the OAuth flow.

    Returns the pane target string (e.g. 'jada-primary:auth-pane').
    Always kills any stale auth window first, then creates a fresh one
    with zero scrollback history. This guarantees no stale OAuth URLs
    or error text from prior attempts.
    """
    session = get_tmux_session()
    # On fresh containers the session might be the portal's own — create a
    # proper Claude session the same way auth_start used to.
    if "portal" in session.lower():
        session = f"{CIV_NAME}-primary"
        subprocess.run(["tmux", "new-session", "-d", "-s", session],
                       stderr=subprocess.DEVNULL)
        time.sleep(0.5)

    target = f"{session}:{AUTH_PANE_NAME}"

    # Kill any leftover auth window from a prior attempt
    subprocess.run(["tmux", "kill-window", "-t", target],
                   stderr=subprocess.DEVNULL)
    time.sleep(0.3)

    # Create a brand-new window with a clean shell
    subprocess.run(["tmux", "new-window", "-t", session, "-n", AUTH_PANE_NAME],
                   check=True, stderr=subprocess.DEVNULL)
    time.sleep(0.3)

    # Widen to 500 cols so OAuth URLs don't wrap
    subprocess.run(["tmux", "resize-window", "-t", target, "-x", "500"],
                   stderr=subprocess.DEVNULL)

    # Clear any initial shell output / MOTD
    subprocess.run(["tmux", "clear-history", "-t", target],
                   stderr=subprocess.DEVNULL)

    return target


def _cleanup_auth_pane() -> None:
    """Kill the dedicated auth pane after OAuth completes."""
    session = get_tmux_session()
    if "portal" in session.lower():
        session = f"{CIV_NAME}-primary"
    target = f"{session}:{AUTH_PANE_NAME}"
    subprocess.run(["tmux", "kill-window", "-t", target],
                   stderr=subprocess.DEVNULL)


async def api_claude_auth_start(request: Request) -> JSONResponse:
    """Start OAuth by launching `claude /login` in a DEDICATED auth pane.

    Creates a fresh tmux window ('auth-pane') with zero history so there is
    no stale output to confuse URL scraping. If Claude Code is not running
    in the auth pane, starts it first.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _captured_oauth_url
    _captured_oauth_url = None

    try:
        auth_pane = _get_or_create_auth_pane()
    except subprocess.CalledProcessError as e:
        _save_portal_message(f"❌ Auth start failed: could not create auth pane — {e}", role="assistant")
        return JSONResponse({"error": f"tmux error creating auth pane: {e}"}, status_code=500)

    _save_portal_message(f"🔐 Auth flow started — dedicated auth pane: {auth_pane}", role="assistant")
    try:
        # Start Claude Code in the auth pane (it's a fresh bash shell)
        subprocess.run(["tmux", "send-keys", "-t", auth_pane, "-l",
                        "claude --dangerously-skip-permissions"],
                       check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "send-keys", "-t", auth_pane, "Enter"],
                       check=True, stderr=subprocess.DEVNULL)
        # Poll until Claude is the active process (up to 30 seconds)
        for _ in range(60):
            time.sleep(0.5)
            if _is_claude_running_in_pane(auth_pane):
                break
        else:
            _save_portal_message("⚠️ Claude didn't start within 30s — sending /login anyway", role="assistant")
        # Give Claude a moment to fully render its prompt
        time.sleep(3)

        subprocess.run(["tmux", "send-keys", "-t", auth_pane, "-l", "/login"],
                       check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "send-keys", "-t", auth_pane, "Enter"],
                       check=True, stderr=subprocess.DEVNULL)
        # Wait for the 3-option login menu to render, then press Enter
        # to auto-select option 1 (already highlighted by default)
        time.sleep(2)
        subprocess.run(["tmux", "send-keys", "-t", auth_pane, "Enter"],
                       check=False, stderr=subprocess.DEVNULL)
        _save_portal_message("⏳ /login sent in auth pane — waiting for OAuth URL...", role="assistant")
        return JSONResponse({"started": True})
    except subprocess.CalledProcessError as e:
        _save_portal_message(f"❌ Auth start failed: tmux error — pane={auth_pane}, err={e}", role="assistant")
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


async def api_claude_auth_code(request: Request) -> JSONResponse:
    """Inject the OAuth authorization code into the dedicated auth pane."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        code = str(body.get("code", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not code:
        return JSONResponse({"error": "empty code"}, status_code=400)

    session = get_tmux_session()
    if "portal" in session.lower():
        session = f"{CIV_NAME}-primary"
    auth_pane = f"{session}:{AUTH_PANE_NAME}"

    _save_portal_message(f"⌨️ Auth code submitted — injecting into auth pane...", role="assistant")
    try:
        subprocess.run(["tmux", "send-keys", "-t", auth_pane, "-l", code],
                       check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "send-keys", "-t", auth_pane, "Enter"],
                       check=True, stderr=subprocess.DEVNULL)
        # Mark that a human has completed the OAuth flow via the portal.
        HUMAN_AUTH_MARKER.write_text(f"human-auth-initiated:{int(time.time())}")
        _save_portal_message("✅ Code injected — Claude is authenticating in auth pane...", role="assistant")

        # Wait briefly for auth to complete, then clean up the auth pane.
        # The credentials file is written by Claude regardless of which pane
        # it runs in — the primary session picks them up automatically.
        await asyncio.sleep(5)
        _cleanup_auth_pane()
        _save_portal_message("🧹 Auth pane cleaned up — credentials saved.", role="assistant")

        return JSONResponse({"injected": True})
    except subprocess.CalledProcessError as e:
        _save_portal_message(f"❌ Code injection failed: tmux error — pane={auth_pane}, err={e}", role="assistant")
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


async def api_claude_auth_url(request: Request) -> JSONResponse:
    """Poll for the captured OAuth URL from the dedicated auth pane."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _captured_oauth_url
    if _captured_oauth_url:
        return JSONResponse({"url": _captured_oauth_url, "ready": True})

    session = get_tmux_session()
    if "portal" in session.lower():
        session = f"{CIV_NAME}-primary"
    auth_pane = f"{session}:{AUTH_PANE_NAME}"

    try:
        # -J joins wrapped lines so long URLs aren't truncated at terminal width
        content = subprocess.check_output(
            ["tmux", "capture-pane", "-t", auth_pane, "-p", "-J", "-S", "-200"],
            stderr=subprocess.DEVNULL, text=True
        )
        match = OAUTH_URL_PATTERN.search(content)
        if match:
            candidate = match.group(0).strip()
            # Validate URL is complete — must contain state= parameter.
            if "state=" not in candidate:
                _save_portal_message("⚠️ OAuth URL found but truncated (missing state=) — retrying capture", role="assistant")
            else:
                _captured_oauth_url = candidate
                _save_portal_message(f"🔗 OAuth URL ready ({len(candidate)} chars, state= confirmed)", role="assistant")
                return JSONResponse({"url": _captured_oauth_url, "ready": True})
        # Check for error patterns in the auth pane (no stale output concern
        # since the pane was created fresh for this auth attempt)
        if "error" in content.lower() and "oauth" in content.lower():
            _save_portal_message("⚠️ OAuth error detected in auth pane — may need to retry", role="assistant")
    except subprocess.CalledProcessError:
        # Auth pane doesn't exist yet or was killed — silently wait
        pass
    except Exception as e:
        _save_portal_message(f"❌ tmux capture failed: {e}", role="assistant")
    return JSONResponse({"url": None, "ready": False})



# ---------------------------------------------------------------------------
# Thinking Stream Monitor
# ---------------------------------------------------------------------------

async def _push_thinking_to_clients(text: str, ts: int) -> None:
    """Push a thinking block to all connected WebSocket clients."""
    msg = json.dumps({
        "role": "thinking",
        "text": text,
        "timestamp": ts,
        "id": f"thinking-{hashlib.sha256(text.encode()).hexdigest()[:12]}",
    })
    dead = set()
    for ws in list(_chat_ws_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _chat_ws_clients.discard(ws)


async def _thinking_monitor_loop() -> None:
    """Background task: tail latest JSONL session file and push thinking blocks to portal."""
    last_file: str = ""
    last_pos: int = 0

    while True:
        try:
            # Find the most recently modified JSONL session file
            log_root = _resolve_log_root()
            logs = _sorted_session_logs(log_root)
            if not logs:
                await asyncio.sleep(2)
                continue

            current_file = str(logs[0])

            # If we switched to a new file, reset position
            if current_file != last_file:
                last_file = current_file
                last_pos = 0

            # Read new lines from where we left off
            try:
                with open(current_file, "rb") as f:
                    f.seek(0, 2)
                    file_size = f.tell()
                    if file_size < last_pos:
                        # File was truncated/rotated — reset
                        last_pos = 0
                    f.seek(last_pos)
                    new_bytes = f.read()
                    last_pos = f.tell()
            except Exception:
                await asyncio.sleep(2)
                continue

            if not new_bytes:
                await asyncio.sleep(1.5)
                continue

            lines = new_bytes.decode("utf-8", errors="replace").splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Only assistant messages
                msg = entry.get("message", {})
                if not msg or msg.get("role") != "assistant":
                    continue

                content_blocks = msg.get("content", [])
                if not isinstance(content_blocks, list):
                    continue

                # Skip sidechain (background agent output)
                if entry.get("isSidechain"):
                    continue

                # Skip messages with tool_use blocks (bash/tool noise)
                has_tool_use = any(
                    isinstance(b, dict) and b.get("type") == "tool_use"
                    for b in content_blocks
                )
                if has_tool_use:
                    continue

                # Extract thinking blocks only
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "thinking":
                        continue
                    text = block.get("thinking", "").strip()
                    if not text:
                        continue

                    # Dedup via hash
                    content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
                    if content_hash in _sent_thinking_hashes:
                        continue
                    _sent_thinking_hashes.add(content_hash)

                    ts = entry.get("timestamp")
                    if isinstance(ts, str):
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            ts = int(dt.timestamp())
                        except (ValueError, AttributeError):
                            ts = int(time.time())
                    elif isinstance(ts, (int, float)):
                        ts = int(ts / 1000) if ts > 1e10 else int(ts)
                    else:
                        ts = int(time.time())

                    # Push to all connected clients (non-blocking)
                    if _chat_ws_clients:
                        await _push_thinking_to_clients(text, ts)

        except Exception:
            pass

        await asyncio.sleep(0.8)  # Fast poll — thinking must appear in near-real-time


async def _startup() -> None:
    """Start background tasks on server startup."""
    _init_portal_log_ids()
    asyncio.create_task(_thinking_monitor_loop())
    # task#36: redeliver any portal messages that were queued-but-not-confirmed
    # before this (re)start, so an outage / restart can't black-hole them.
    asyncio.create_task(_replay_pending_deliveries())
    # task#36: periodic drain so a message stuck past the 120s window is retried
    # WITHOUT waiting for a restart.
    asyncio.create_task(_periodic_delivery_sweep())


# ---------------------------------------------------------------------------
# Referral API Proxy (avoids CORS — portal fetches from itself, server relays to purebrain.ai)
# ---------------------------------------------------------------------------

async def api_referral_proxy(request: Request) -> JSONResponse:
    """Proxy referral dashboard requests to purebrain.ai to avoid CORS blocks."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    code = request.query_params.get("code", "")
    email = request.query_params.get("email", "")
    if not code and not email:
        return JSONResponse({"error": "missing code or email"}, status_code=400)
    import urllib.request
    params = f"code={code}" if code else f"email={email}"
    url = f"https://purebrain.ai/wp-json/pb-referral/v1/dashboard?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PureBrain-Portal/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": f"proxy failed: {e}"}, status_code=502)


async def api_referral_register_proxy(request: Request) -> JSONResponse:
    """Proxy referral registration to purebrain.ai to avoid CORS blocks."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import urllib.request
    try:
        body = await request.body()
        url = "https://purebrain.ai/wp-json/pb-referral/v1/register"
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"User-Agent": "PureBrain-Portal/1.0",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": f"proxy failed: {e}"}, status_code=502)


async def api_referral_lookup_proxy(request: Request) -> JSONResponse:
    """Proxy referral lookup to purebrain.ai to avoid CORS blocks."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    email = request.query_params.get("email", "")
    if not email:
        return JSONResponse({"error": "missing email"}, status_code=400)
    import urllib.request
    url = f"https://purebrain.ai/wp-json/pb-referral/v1/lookup?email={email}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PureBrain-Portal/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": f"proxy failed: {e}"}, status_code=502)


async def api_portal_owner(request: Request) -> JSONResponse:
    """Return portal owner identity for dynamic referral/share features."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    owner_file = SCRIPT_DIR / "portal_owner.json"
    try:
        owner = json.loads(owner_file.read_text())
        return JSONResponse(owner)
    except Exception:
        return JSONResponse({"name": "Portal User", "email": "", "referral_code": ""})


# ---------------------------------------------------------------------------
# Payout Request API (Phase 3a — Manual Bridge)
# ---------------------------------------------------------------------------

def _send_telegram_notification(message: str) -> bool:
    """Send a Telegram notification via tg_send.sh (searches standard locations)."""
    try:
        # Check well-known locations for the send script
        candidates = [
            Path.home() / "civ" / "tools" / "tg_send.sh",
            Path.home() / "tools" / "tg_send.sh",
        ]
        for tg_send in candidates:
            if tg_send.exists():
                subprocess.run(
                    ["bash", str(tg_send), message],
                    timeout=15, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL
                )
                return True
    except Exception:
        pass
    return False


def _read_payout_requests() -> list:
    """Read all payout requests from JSONL file."""
    requests_list = []
    if not PAYOUT_REQUESTS_FILE.exists():
        return requests_list
    try:
        with PAYOUT_REQUESTS_FILE.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    requests_list.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return requests_list


def _write_payout_request(entry: dict) -> None:
    """Append a payout request to JSONL file."""
    with PAYOUT_REQUESTS_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


async def api_referral_payout_request(request: Request) -> JSONResponse:
    """POST /api/referral/payout-request — user requests a payout.
    Body: { paypal_email, amount, referral_code }
    Validates: balance >= amount >= $25, no pending request in 30 days.
    Writes to payout-requests.jsonl, notifies admin via Telegram.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    paypal_email = str(body.get("paypal_email", "")).strip().lower()
    referral_code = str(body.get("referral_code", "")).strip()
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid amount"}, status_code=400)

    # Validate email format (basic)
    if not paypal_email or "@" not in paypal_email or "." not in paypal_email.split("@")[-1]:
        return JSONResponse({"error": "invalid paypal_email"}, status_code=400)

    if not referral_code:
        return JSONResponse({"error": "missing referral_code"}, status_code=400)

    # Validate minimum amount
    if amount < PAYOUT_MIN_AMOUNT:
        return JSONResponse(
            {"error": f"minimum payout is ${PAYOUT_MIN_AMOUNT:.0f}"},
            status_code=400
        )

    # Check cooldown: no pending request in last 30 days for this code
    existing = _read_payout_requests()
    cooldown_secs = PAYOUT_COOLDOWN_DAYS * 86400
    now_ts = time.time()
    for req in existing:
        if req.get("referral_code") == referral_code and req.get("status") in ("pending", "processing"):
            created_at = req.get("created_at_ts", 0)
            if (now_ts - created_at) < cooldown_secs:
                days_left = int((cooldown_secs - (now_ts - created_at)) / 86400) + 1
                return JSONResponse(
                    {"error": f"payout already requested. Please wait {days_left} more day(s)."},
                    status_code=429
                )

    # Fetch current balance from WP to validate amount <= earnings
    import urllib.request as _ureq
    balance_ok = False
    actual_earnings = 0.0
    try:
        url = f"https://purebrain.ai/wp-json/pb-referral/v1/dashboard?code={referral_code}"
        req_http = _ureq.Request(url, headers={"User-Agent": "PureBrain-Portal/1.0"})
        with _ureq.urlopen(req_http, timeout=10) as resp:
            wp_data = json.loads(resp.read().decode())
        actual_earnings = float(wp_data.get("earnings", 0))
        if amount <= actual_earnings:
            balance_ok = True
    except Exception:
        # If WP is unreachable, still allow — admin will verify before paying
        balance_ok = True
        actual_earnings = amount  # assume they have it

    if not balance_ok:
        return JSONResponse(
            {"error": f"requested amount ${amount:.2f} exceeds available balance ${actual_earnings:.2f}"},
            status_code=400
        )

    # Create payout request record
    request_id = f"payout-{referral_code}-{int(now_ts)}"
    entry = {
        "request_id": request_id,
        "referral_code": referral_code,
        "paypal_email": paypal_email,
        "amount": round(amount, 2),
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_at_ts": now_ts,
        "paid_at": None,
        "notes": "",
    }
    _write_payout_request(entry)

    # Notify admin via Telegram
    tg_msg = (
        f"PAYOUT REQUEST\n"
        f"Referral: {referral_code}\n"
        f"Amount: ${amount:.2f}\n"
        f"PayPal: {paypal_email}\n"
        f"Request ID: {request_id}\n"
        f"Earnings on file: ${actual_earnings:.2f}"
    )
    _send_telegram_notification(tg_msg)

    return JSONResponse({
        "ok": True,
        "request_id": request_id,
        "message": "Payout request submitted. We will process within 2 business days.",
        "amount": round(amount, 2),
        "paypal_email": paypal_email,
    })


async def api_referral_payout_history(request: Request) -> JSONResponse:
    """GET /api/referral/payout-history?referral_code=XXX
    Returns payout request history for a given referral code.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    referral_code = request.query_params.get("referral_code", "").strip()
    if not referral_code:
        return JSONResponse({"error": "missing referral_code"}, status_code=400)

    all_requests = _read_payout_requests()
    user_requests = [r for r in all_requests if r.get("referral_code") == referral_code]
    # Return most recent first
    user_requests.sort(key=lambda r: r.get("created_at_ts", 0), reverse=True)

    # Check if there's an active cooldown
    cooldown_secs = PAYOUT_COOLDOWN_DAYS * 86400
    now_ts = time.time()
    has_pending = False
    days_until_eligible = 0
    for req in user_requests:
        if req.get("status") in ("pending", "processing"):
            created_at = req.get("created_at_ts", 0)
            elapsed = now_ts - created_at
            if elapsed < cooldown_secs:
                has_pending = True
                days_until_eligible = int((cooldown_secs - elapsed) / 86400) + 1
                break

    return JSONResponse({
        "requests": user_requests,
        "has_pending": has_pending,
        "days_until_eligible": days_until_eligible,
    })


async def api_admin_payout_mark_paid(request: Request) -> JSONResponse:
    """POST /api/admin/payout/mark-paid — admin marks a payout as paid.
    Body: { request_id, notes? }
    Requires Bearer token auth. Rewrites payout-requests.jsonl with updated status.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    request_id = str(body.get("request_id", "")).strip()
    notes = str(body.get("notes", "")).strip()

    if not request_id:
        return JSONResponse({"error": "missing request_id"}, status_code=400)

    all_requests = _read_payout_requests()
    found = False
    updated = []
    paid_entry = None
    for req in all_requests:
        if req.get("request_id") == request_id:
            req["status"] = "paid"
            req["paid_at"] = datetime.now(timezone.utc).isoformat()
            if notes:
                req["notes"] = notes
            paid_entry = req
            found = True
        updated.append(req)

    if not found:
        return JSONResponse({"error": "request_id not found"}, status_code=404)

    # Rewrite the JSONL file
    try:
        with PAYOUT_REQUESTS_FILE.open("w") as f:
            for req in updated:
                f.write(json.dumps(req) + "\n")
    except Exception as e:
        return JSONResponse({"error": f"failed to update file: {e}"}, status_code=500)

    # Notify admin via Telegram
    if paid_entry:
        tg_msg = (
            f"PAYOUT MARKED PAID\n"
            f"Request: {request_id}\n"
            f"Amount: ${paid_entry.get('amount', 0):.2f}\n"
            f"PayPal: {paid_entry.get('paypal_email', '')}"
        )
        _send_telegram_notification(tg_msg)

    return JSONResponse({
        "ok": True,
        "request_id": request_id,
        "status": "paid",
        "paid_at": paid_entry.get("paid_at") if paid_entry else None,
    })


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
_react_assets_mount = (
    [Mount("/react/assets", app=StaticFiles(directory=str(REACT_DIST / "assets")))]
    if (REACT_DIST / "assets").exists()
    else []
)
_vendor_mount = (
    [Mount("/vendor", app=StaticFiles(directory=str(SCRIPT_DIR / "vendor")))]
    if (SCRIPT_DIR / "vendor").exists()
    else []
)

REFER_EARN_HTML = SCRIPT_DIR / "refer-and-earn.html"

# ---------------------------------------------------------------------------
# Evolution / First-Boot
# ---------------------------------------------------------------------------
EVOLUTION_DONE_FILE = Path.home() / "memories" / "identity" / ".evolution-done"
FIRST_BOOT_FIRED_FILE = Path.home() / ".first-boot-fired"
FIRST_BOOT_PROMPT_FILE = Path.home() / ".claude" / "skills" / "first-visit-evolution" / "prompt.txt"


async def api_evolution_status(request: Request) -> JSONResponse:
    """Check if this AiCIV needs first-boot evolution, is mid-evolution, or is done."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    evolution_done = EVOLUTION_DONE_FILE.exists()
    first_boot_fired = FIRST_BOOT_FIRED_FILE.exists()
    seed_exists = Path(Path.home() / "memories" / "identity" / "seed-conversation.md").exists()
    return JSONResponse({
        "seed_exists": seed_exists,
        "evolution_done": evolution_done,
        "first_boot_fired": first_boot_fired,
        "needs_evolution": seed_exists and not evolution_done and not first_boot_fired,
    })


async def api_first_boot(request: Request) -> JSONResponse:
    """Inject the first-visit evolution prompt after OAuth succeeds.

    Guards:
    - .evolution-done must NOT exist (already evolved)
    - .first-boot-fired must NOT exist (already injected)
    - seed-conversation.md must exist (has a seed to evolve from)
    - Claude must be at interactive prompt (ready for input)

    The prompt is read from .claude/skills/first-visit-evolution/prompt.txt
    and injected via tmux send-keys into the primary pane.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # --- Guard checks ---
    if EVOLUTION_DONE_FILE.exists():
        return JSONResponse({"status": "skipped", "reason": "evolution already complete"})
    if FIRST_BOOT_FIRED_FILE.exists():
        return JSONResponse({"status": "skipped", "reason": "first boot already fired"})
    seed_file = Path.home() / "memories" / "identity" / "seed-conversation.md"
    if not seed_file.exists():
        return JSONResponse({"status": "skipped", "reason": "no seed conversation found"})

    # --- Read the prompt ---
    if not FIRST_BOOT_PROMPT_FILE.exists():
        return JSONResponse({"error": "prompt file not found"}, status_code=500)
    prompt_text = FIRST_BOOT_PROMPT_FILE.read_text().strip()
    if not prompt_text:
        return JSONResponse({"error": "prompt file is empty"}, status_code=500)

    # --- Wait for Claude's interactive prompt ---
    pane = _find_primary_pane()
    prompt_ready = False
    for _ in range(30):  # 30 x 2s = 60s timeout
        try:
            content = subprocess.check_output(
                ["tmux", "capture-pane", "-t", pane, "-p"],
                stderr=subprocess.DEVNULL, text=True
            )
            if "\u276f" in content or "Try \"" in content or "Try '" in content:
                prompt_ready = True
                break
        except subprocess.CalledProcessError:
            pass
        await asyncio.sleep(2)

    if not prompt_ready:
        _save_portal_message("\u23f3 Claude not at interactive prompt yet \u2014 first-boot deferred", role="assistant")
        return JSONResponse({"status": "deferred", "reason": "Claude not at interactive prompt"})

    # --- Inject the prompt ---
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False,
                                          dir='/tmp', prefix='first-boot-') as f:
            f.write(prompt_text)
            tmp_path = f.name

        # Load the prompt into tmux paste buffer, then paste it
        subprocess.run(["tmux", "load-buffer", "-b", "first-boot", tmp_path],
                       check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "paste-buffer", "-b", "first-boot", "-t", pane],
                       check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"],
                       check=True, stderr=subprocess.DEVNULL)

        # Clean up temp file and tmux buffer
        Path(tmp_path).unlink(missing_ok=True)
        subprocess.run(["tmux", "delete-buffer", "-b", "first-boot"],
                       stderr=subprocess.DEVNULL)

        # Write the guard flag
        FIRST_BOOT_FIRED_FILE.write_text(f"fired at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")

        _save_portal_message("\ud83c\udf05 First-visit evolution prompt injected \u2014 your AiCIV is waking up!", role="assistant")
        return JSONResponse({"status": "fired", "prompt_length": len(prompt_text)})

    except subprocess.CalledProcessError as e:
        _save_portal_message(f"\u274c First-boot injection failed: {e}", role="assistant")
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


async def refer_and_earn(request: Request) -> Response:
    if REFER_EARN_HTML.exists():
        return FileResponse(str(REFER_EARN_HTML), media_type="text/html")
    return Response("<h1>Page not found</h1>", media_type="text/html", status_code=404)


# ---------------------------------------------------------------------------
# Margin (two-sided journal)
# ---------------------------------------------------------------------------
async def api_margin(request: Request, path: Path, author: str) -> JSONResponse:
    """Shared handler for margin GET/POST.  JSON feed: append-only array."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    def _read_feed() -> list:
        if not path.exists():
            return []
        try:
            feed = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            return []
        # Normalize: some entries were written directly with "text" or "note"
        # instead of "content". Ensure every entry has "content" for the frontend.
        for entry in feed:
            if not entry.get("content"):
                entry["content"] = entry.get("text") or entry.get("note") or ""
        return feed

    if request.method == "POST":
        try:
            body = await request.json()
            content = (body.get("content") or body.get("text") or "").strip()
            if not content:
                return JSONResponse({"error": "empty content"}, status_code=400)
            boop_id = body.get("boop_id")
            entry = {
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "author": author,
                "content": content,
            }
            if boop_id:
                entry["boop_id"] = str(boop_id)
            feed = _read_feed()
            feed.append(entry)
            path.write_text(json.dumps(feed, indent=2, ensure_ascii=False), encoding="utf-8")
            return JSONResponse({"ok": True, "entry": entry})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    # GET — return full array
    try:
        return JSONResponse(_read_feed())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_margin_primary(request: Request) -> JSONResponse:
    return await api_margin(request, MARGIN_PRIMARY, "primary")


async def api_margin_corey(request: Request) -> JSONResponse:
    return await api_margin(request, MARGIN_COREY, "corey")


# ---------------------------------------------------------------------------
# Points (ledger — read-only from portal)
# ---------------------------------------------------------------------------
POINTS_LEDGER = Path("/home/aiciv/projects/points/points.jsonl")


async def api_points_summary(request: Request) -> JSONResponse:
    """Aggregate points totals from the JSONL ledger."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    totals: dict[str, int] = {}
    if POINTS_LEDGER.exists():
        for line in POINTS_LEDGER.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                to = rec.get("to", "unknown")
                delta = int(rec.get("delta", 0))
                totals[to] = totals.get(to, 0) + delta
            except (json.JSONDecodeError, ValueError):
                continue
    # Bucket into primary / corey / team_leads
    primary = totals.pop("primary", 0)
    corey = totals.pop("corey", 0)
    team_leads = sum(totals.values())
    return JSONResponse({"primary": primary, "corey": corey, "team_leads": team_leads})


async def api_points_history(request: Request) -> JSONResponse:
    """Return full points history (most recent first)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    entries: list[dict] = []
    if POINTS_LEDGER.exists():
        for line in POINTS_LEDGER.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                entries.append({
                    "timestamp": rec.get("timestamp") or rec.get("ts", ""),
                    "recipient": rec.get("to", "unknown"),
                    "amount": int(rec.get("delta", 0)),
                    "note": rec.get("note", ""),
                })
            except (json.JSONDecodeError, ValueError):
                continue
    entries.reverse()
    return JSONResponse(entries)


# ---------------------------------------------------------------------------
# Fleet-Intel human dashboard (read-only over civ/data/witness-fleet.db)
# ---------------------------------------------------------------------------
# Additive, read-only. Surfaces DERIVED SIGNAL already present in the fleet-intel
# sweep DB. Never re-reads raw CIV content. All numbers derived LIVE per request
# from read-only SELECTs on the latest sweep_date. Handles the nested 'metrics'
# JSON blob and mixed/null column types defensively.
import sqlite3 as _sqlite3

FLEET_INTEL_DB = Path.home() / "civ" / "data" / "witness-fleet.db"


def _fi_connect():
    """Open the fleet-intel DB strictly read-only (immutable=off, mode=ro)."""
    uri = f"file:{FLEET_INTEL_DB}?mode=ro"
    conn = _sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = _sqlite3.Row
    return conn


def _fi_truthy(v):
    """Defensive truthiness for mixed-type/null boolean columns (int/str/None)."""
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "t", "y")
    return bool(v)


def _fi_model_below_floor(model):
    """True if running_model is BELOW the 4.8 constitutional floor.

    Floor-OK: anything on 4-8 (incl [1m]) or the bare 'opus' alias.
    Below-floor: 4-6 variants and other explicitly-old models.
    Unknown/None models are NOT counted as below-floor (they're 'unknown').
    """
    if not model:
        return False
    m = str(model).lower()
    if "4-8" in m or "4.8" in m:
        return False
    if m == "opus":  # bare alias resolves to current default (>= floor)
        return False
    if "4-6" in m or "4.6" in m or "4-5" in m or "sonnet" in m:
        return True
    # Fable / newer named models are not below the 4.8 floor
    if "fable" in m:
        return False
    return False


def _fi_latest_date(conn):
    row = conn.execute("SELECT MAX(sweep_date) AS d FROM fleet_intel").fetchone()
    return row["d"] if row else None


# ---- CC-version cross-ref helpers (binary vs config) ----
# The sensor stores cc_version nested inside the per-row 'metrics' JSON blob.
# We recurse to find the first 2.1.x version string. The CC binary floor for
# Opus 4.8 is 2.1.170. Below that -> the BINARY is the constraint. At/above
# but still on 4-6 -> the model is config-pinned (or a stale in-memory process
# holds an old binary even after the on-disk upgrade).
_CC_FLOOR = 170


def _fi_find_cc(d):
    """Recursively pull the first 2.1.x version string out of a metrics blob."""
    if isinstance(d, dict):
        for k, v in d.items():
            if re.search(r'cc_version|claude_version|cli_version|version', str(k), re.I):
                m = re.search(r'2\.1\.\d+', str(v))
                if m:
                    return m.group(0)
            r = _fi_find_cc(v)
            if r:
                return r
    elif isinstance(d, (list, tuple)):
        for v in d:
            r = _fi_find_cc(v)
            if r:
                return r
    elif isinstance(d, str):
        m = re.search(r'2\.1\.\d+', d)
        if m:
            return m.group(0)
    return None


def _fi_cc_below_floor(ver):
    """True if a 2.1.x version is below the 2.1.170 binary floor. None if unknown."""
    m = re.search(r'2\.1\.(\d+)', ver or '')
    return int(m.group(1)) < _CC_FLOOR if m else None


def _fi_is_46(model):
    """True if the running_model string names an opus-4-6 variant."""
    m = str(model or '').lower()
    return '4-6' in m or '4.6' in m


def _fi_cc_row_version(r):
    """Extract cc_version for one fleet_intel row from its metrics JSON blob."""
    raw = r.get("metrics")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return _fi_find_cc(parsed)


def _fi_cc_crossref(rows):
    """Classify the opus-4-6 CIVs by WHY they're below the 4.8 floor:
      old_binary     -> cc < 2.1.170 (binary itself needs upgrading)
      hard_pinned    -> cc >= 2.1.170 but still 4-6 (config-pinned OR stale process)
      binary_unknown -> cc could not be read from metrics
    """
    old_binary = []
    hard_pinned = []
    binary_unknown = 0
    for r in rows:
        if not _fi_is_46(r.get("running_model")):
            continue
        cc = _fi_cc_row_version(r)
        bf = _fi_cc_below_floor(cc)
        if bf is True:
            old_binary.append({"civ": r.get("civ"), "cc": cc})
        elif bf is False:
            hard_pinned.append({
                "civ": r.get("civ"),
                "running_model": r.get("running_model"),
                "cc": cc,
            })
        else:
            binary_unknown += 1
    old_binary.sort(key=lambda x: str(x.get("civ") or "").lower())
    hard_pinned.sort(key=lambda x: str(x.get("civ") or "").lower())
    return {
        "old_binary": {"count": len(old_binary), "civs": old_binary},
        "hard_pinned": {"count": len(hard_pinned), "civs": hard_pinned},
        "binary_unknown": {"count": binary_unknown},
    }


def _fi_below_floor_trend(conn, nights=10):
    """Per-sweep_date total_civs vs below_floor_count over the last N nights,
    chronological. below_floor here = running_model names a 4-6 variant."""
    dates = [r["d"] for r in conn.execute(
        "SELECT DISTINCT sweep_date AS d FROM fleet_intel "
        "ORDER BY sweep_date DESC LIMIT ?", (nights,)).fetchall()]
    dates.reverse()  # chronological
    out = []
    for d in dates:
        rows = conn.execute(
            "SELECT running_model FROM fleet_intel WHERE sweep_date = ?",
            (d,)).fetchall()
        total = len(rows)
        below = sum(1 for r in rows if _fi_is_46(r["running_model"]))
        out.append({"sweep_date": d, "total_civs": total,
                    "below_floor_count": below})
    return out


def _fi_build(limit_lists=50):
    """Build the full fleet-intel payload from the latest sweep. Read-only."""
    conn = _fi_connect()
    try:
        latest = _fi_latest_date(conn)
        if not latest:
            return {"error": "no sweep data", "latest_sweep": None,
                    "rollup": {}, "below_floor": [], "engagement_slip": []}

        rows = conn.execute(
            "SELECT * FROM fleet_intel WHERE sweep_date = ?", (latest,)
        ).fetchall()
        rows = [dict(r) for r in rows]

        # ---- Rollup ----
        total = len(rows)
        healthy = sum(1 for r in rows if _fi_truthy(r.get("healthy")))
        model_correct = sum(1 for r in rows if _fi_truthy(r.get("model_correct")))
        below_floor_count = sum(
            1 for r in rows if _fi_model_below_floor(r.get("running_model")))
        claude_alive = sum(1 for r in rows if _fi_truthy(r.get("claude_alive")))
        portal_alive = sum(1 for r in rows if _fi_truthy(r.get("portal_alive")))

        # model distribution (live, not hardcoded)
        model_dist = {}
        for r in rows:
            mk = r.get("running_model") or "None"
            model_dist[mk] = model_dist.get(mk, 0) + 1
        model_dist = dict(sorted(model_dist.items(), key=lambda kv: -kv[1]))

        rollup = {
            "total_civs": total,
            "healthy": healthy,
            "model_correct": model_correct,
            "below_floor_count": below_floor_count,
            "claude_alive": claude_alive,
            "portal_alive": portal_alive,
            "latest_sweep": latest,
            "model_distribution": model_dist,
        }

        # ---- CC-below-floor list (headline gold) ----
        below = [r for r in rows if _fi_model_below_floor(r.get("running_model"))]

        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return -1

        below.sort(key=lambda r: (str(r.get("running_model") or ""),
                                   str(r.get("civ") or "")))
        below_floor = [{
            "container": r.get("container"),
            "civ": r.get("civ"),
            "owner": r.get("owner"),
            "owner_email": r.get("owner_email"),
            "running_model": r.get("running_model"),
            "host": r.get("host"),
            "model_correct": _fi_truthy(r.get("model_correct")),
        } for r in below[:limit_lists]]

        # ---- Engagement-slip watchlist ----
        def _slip_key(r):
            return (_num(r.get("engagement_slip_streak")),
                    _num(r.get("days_since_human_msg")))
        watch = sorted(rows, key=_slip_key, reverse=True)
        engagement_slip = []
        for r in watch[:limit_lists]:
            streak = _num(r.get("engagement_slip_streak"))
            dshm = _num(r.get("days_since_human_msg"))
            if streak <= 0 and dshm <= 0:
                continue  # nothing notable
            engagement_slip.append({
                "container": r.get("container"),
                "civ": r.get("civ"),
                "owner": r.get("owner"),
                "engagement_class": r.get("engagement_class"),
                "engagement_slip_streak": r.get("engagement_slip_streak"),
                "days_since_human_msg": r.get("days_since_human_msg"),
                "days_since_engagement_delta": r.get("days_since_engagement_delta"),
                "time_since_last_use": r.get("time_since_last_use"),
                "boop_cadence_ok": _fi_truthy(r.get("boop_cadence_ok")),
            })

        # ---- CC-version cross-ref (binary vs config) ----
        cc_crossref = _fi_cc_crossref(rows)

        # ---- Below-floor night-over-night trend ----
        trend = _fi_below_floor_trend(conn, nights=10)

        return {
            "latest_sweep": latest,
            "rollup": rollup,
            "below_floor": below_floor,
            "engagement_slip": engagement_slip,
            "cc_crossref": cc_crossref,
            "trend": trend,
            "generated_at": int(time.time()),
        }
    finally:
        conn.close()


def _fi_civ_detail(container):
    """Return the full latest-night row for one container (by container id or civ name)."""
    conn = _fi_connect()
    try:
        latest = _fi_latest_date(conn)
        if not latest:
            return None
        row = conn.execute(
            "SELECT * FROM fleet_intel WHERE sweep_date = ? AND container = ? LIMIT 1",
            (latest, container),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM fleet_intel WHERE sweep_date = ? AND civ = ? LIMIT 1",
                (latest, container),
            ).fetchone()
        if row is None:
            return None
        rec = dict(row)
        # Pretty-parse the nested metrics blob defensively (may be null/partial).
        raw_metrics = rec.get("metrics")
        parsed = None
        if raw_metrics:
            try:
                parsed = json.loads(raw_metrics)
            except Exception:
                parsed = {"_unparsed": str(raw_metrics)[:2000]}
        rec["metrics_parsed"] = parsed
        return rec
    finally:
        conn.close()


async def api_fleet_intel(request: Request) -> JSONResponse:
    """JSON fleet-intel payload (latest sweep). Auth: same bearer pattern."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    civ = request.query_params.get("civ") or request.query_params.get("container")
    try:
        if civ:
            detail = _fi_civ_detail(civ)
            if detail is None:
                return JSONResponse({"error": "not found", "civ": civ}, status_code=404)
            return JSONResponse({"detail": detail})
        return JSONResponse(_fi_build())
    except Exception as e:
        return JSONResponse({"error": f"fleet-intel error: {e}"}, status_code=500)


_FLEET_INTEL_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Witness Fleet-Intel</title>
<style>
:root{--bg:#0d0f14;--panel:#161a22;--panel2:#1d222c;--line:#2a3140;--fg:#e6e9ef;--dim:#8b93a3;--accent:#6ea8fe;--red:#ff6b6b;--amber:#ffc857;--green:#54d18c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}
h1{font-size:18px;margin:0;font-weight:600}
.sweep{color:var(--dim);font-size:13px}
main{padding:20px 24px;max-width:1200px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .n{font-size:26px;font-weight:700}.card .l{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card.warn .n{color:var(--red)}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin-bottom:22px;overflow:hidden}
section>h2{font-size:14px;margin:0;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--panel2);font-weight:600}
section>h2 .sub{color:var(--dim);font-weight:400;font-size:12px;margin-left:8px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 16px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
tr.clk{cursor:pointer}tr.clk:hover td{background:var(--panel2)}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;border:1px solid var(--line)}
.pill.bad{color:var(--red);border-color:var(--red)}.pill.ok{color:var(--green);border-color:var(--green)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.dist{display:flex;flex-wrap:wrap;gap:8px;padding:12px 16px}
.dist .d{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:4px 10px;font-size:12px}
.dist .d.below{border-color:var(--red);color:var(--red)}
#detail{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:flex-start;justify-content:center;padding:40px 16px;overflow:auto;z-index:50}
#detail.show{display:flex}
#detailBox{background:var(--panel);border:1px solid var(--line);border-radius:12px;max-width:820px;width:100%;padding:20px}
#detailBox h3{margin:0 0 12px}
#detailBox pre{background:#0a0c10;border:1px solid var(--line);border-radius:8px;padding:14px;overflow:auto;font-size:12px;max-height:60vh}
.x{float:right;cursor:pointer;color:var(--dim);font-size:20px;line-height:1}
.err{color:var(--red);padding:20px}
.muted{color:var(--dim)}
.trend{display:flex;flex-wrap:wrap;gap:6px;padding:12px 16px;align-items:center}
.trend .tp{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:4px 9px;font-size:12px;white-space:nowrap}
.trend .tp b{font-weight:600;color:var(--dim);margin-right:4px}
.trend .tp.hi{border-color:var(--red);color:var(--red)}
.trend .tp.mid{border-color:var(--amber);color:var(--amber)}
.trend .tp.lo{border-color:var(--green);color:var(--green)}
.ccwrap{padding:8px 16px 4px}
.ccsub{margin:10px 0 6px}
.ccsub h4{margin:0 0 8px;font-size:13px;font-weight:600}
.ccsub h4 .sub2{color:var(--dim);font-weight:400;font-size:12px;margin-left:8px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer}
.chip:hover{border-color:var(--accent)}
.footnote{color:var(--dim);font-size:11.5px;padding:6px 16px 14px;line-height:1.5;border-top:1px solid var(--line);margin:8px 0 0}
</style></head>
<body>
<div style="padding:20px 24px;border-bottom:1px solid var(--line);background:linear-gradient(90deg,var(--panel2),var(--panel));text-align:center">
  <h1 style="font-size:22px;font-weight:700;margin:0;letter-spacing:.02em;color:var(--accent)">Longitudinal data worth more than gold</h1>
</div>
<header><h1>Witness Fleet-Intel</h1><span class="sweep" id="sweep">loading...</span>
<span class="muted" style="margin-left:auto;font-size:12px">read-only derived signal · witness-fleet.db</span></header>
<main id="main"><p class="muted">Loading fleet-intel...</p></main>
<div id="detail" onclick="if(event.target===this)closeDetail()">
  <div id="detailBox"><span class="x" onclick="closeDetail()">&times;</span>
  <h3 id="detailTitle"></h3><pre id="detailPre"></pre></div>
</div>
<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const H = {'Authorization':'Bearer '+TOKEN};
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
async function load(){
  let r;
  try{ r = await fetch('/api/fleet-intel',{headers:H}); }
  catch(e){ document.getElementById('main').innerHTML='<p class="err">Network error: '+esc(e)+'</p>'; return; }
  if(r.status===401){ document.getElementById('main').innerHTML='<p class="err">Unauthorized — append ?token=YOUR_TOKEN to the URL.</p>'; return; }
  const d = await r.json();
  if(d.error){ document.getElementById('main').innerHTML='<p class="err">'+esc(d.error)+'</p>'; return; }
  render(d);
}
function render(d){
  const ru=d.rollup||{};
  document.getElementById('sweep').textContent='latest sweep: '+esc(d.latest_sweep);
  let dist='';
  for(const [k,v] of Object.entries(ru.model_distribution||{})){
    const below=/4-6|4\\.6|4-5|sonnet/i.test(k);
    dist+='<span class="d'+(below?' below':'')+'">'+esc(k)+' · '+v+'</span>';
  }
  let h='';
  h+='<div class="cards">'
    +card('Total CIVs',ru.total_civs)
    +card('Healthy',ru.healthy)
    +card('Model-correct',ru.model_correct)
    +card('Below 4.8 floor',ru.below_floor_count,true)
    +card('Claude alive',ru.claude_alive)
    +card('Portal alive',ru.portal_alive)
    +'</div>';
  // below-floor night-over-night trend strip (near top rollup)
  const tr=d.trend||[];
  if(tr.length){
    let strip='';
    for(const t of tr){
      const pct=t.total_civs?Math.round(100*t.below_floor_count/t.total_civs):0;
      const cls=pct>=50?' hi':(pct>=25?' mid':' lo');
      strip+='<span class="tp'+cls+'" title="'+esc(t.sweep_date)+': '+esc(t.below_floor_count)+' of '+esc(t.total_civs)+' below 4.8 floor">'
        +'<b>'+esc(t.sweep_date)+'</b> '+esc(t.below_floor_count)+'/'+esc(t.total_civs)+'</span>';
    }
    h+='<section><h2>Below-4.8-floor trend<span class="sub">night over night · below/total · watch it fall</span></h2>'
      +'<div class="trend">'+strip+'</div></section>';
  }
  h+='<section><h2>Model distribution<span class="sub">live from latest sweep</span></h2><div class="dist">'+dist+'</div></section>';
  h+=ccPanel(d.cc_crossref);
  // below floor
  h+='<section><h2>CC below the 4.8 floor<span class="sub">'+(d.below_floor||[]).length+' CIVs · constitutional gap</span></h2>';
  if((d.below_floor||[]).length===0){ h+='<p class="muted" style="padding:14px 16px">None below floor in this sweep.</p>'; }
  else{
    h+='<table><thead><tr><th>CIV</th><th>Owner</th><th>Running model</th><th>Host</th></tr></thead><tbody>';
    for(const c of d.below_floor){
      h+='<tr class="clk" onclick="detail(\\''+esc(c.container||c.civ)+'\\')"><td>'+esc(c.civ)+'</td><td>'+esc(c.owner)
        +'</td><td class="mono"><span class="pill bad">'+esc(c.running_model)+'</span></td><td class="mono">'+esc(c.host)+'</td></tr>';
    }
    h+='</tbody></table>';
  }
  h+='</section>';
  // engagement slip
  h+='<section><h2>Engagement-slip watchlist<span class="sub">by slip streak / days since human msg</span></h2>';
  if((d.engagement_slip||[]).length===0){ h+='<p class="muted" style="padding:14px 16px">No engagement slip detected.</p>'; }
  else{
    h+='<table><thead><tr><th>CIV</th><th>Owner</th><th>Class</th><th>Slip streak</th><th>Days since human msg</th><th>BOOP</th></tr></thead><tbody>';
    for(const c of d.engagement_slip){
      h+='<tr class="clk" onclick="detail(\\''+esc(c.container||c.civ)+'\\')"><td>'+esc(c.civ)+'</td><td>'+esc(c.owner)+'</td><td>'+esc(c.engagement_class||'')
        +'</td><td>'+esc(c.engagement_slip_streak)+'</td><td>'+esc(c.days_since_human_msg)+'</td><td><span class="pill '+(c.boop_cadence_ok?'ok':'bad')+'">'+(c.boop_cadence_ok?'ok':'off')+'</span></td></tr>';
    }
    h+='</tbody></table>';
  }
  h+='</section>';
  document.getElementById('main').innerHTML=h;
}
function card(l,n,warn){return '<div class="card'+(warn?' warn':'')+'"><div class="n">'+(n==null?'—':n)+'</div><div class="l">'+esc(l)+'</div></div>';}
function ccPanel(cc){
  if(!cc) return '';
  const ob=cc.old_binary||{count:0,civs:[]};
  const hp=cc.hard_pinned||{count:0,civs:[]};
  const bu=cc.binary_unknown||{count:0};
  let s='<section><h2>Why below the 4.8 floor — binary vs config'
    +'<span class="sub">the same 4-6 gap, split by root cause</span></h2>';
  s+='<div class="ccwrap">';
  // sub-section 1: needs binary upgrade
  s+='<div class="ccsub"><h4>Needs binary upgrade ('+ob.count+')'
    +'<span class="sub2">cc &lt; 2.1.170 — the CC binary itself is the constraint</span></h4>';
  if(!ob.civs.length){ s+='<p class="muted" style="padding:6px 0">None.</p>'; }
  else{
    s+='<div class="chips">';
    for(const c of ob.civs){
      s+='<span class="chip" onclick="detail(\\''+esc(c.civ)+'\\')">'+esc(c.civ)
        +' <span class="mono muted">'+esc(c.cc||'?')+'</span></span>';
    }
    s+='</div>';
  }
  s+='</div>';
  // sub-section 2: config-pinned or stale process
  s+='<div class="ccsub"><h4>Config-pinned or stale process ('+hp.count+') — needs resolving'
    +'<span class="sub2">Latest binary, still 4-6 (config-pinned or stale process)</span></h4>';
  if(!hp.civs.length){ s+='<p class="muted" style="padding:6px 0">None.</p>'; }
  else{
    s+='<table><thead><tr><th>CIV</th><th>Running model</th><th>CC version</th></tr></thead><tbody>';
    for(const c of hp.civs){
      s+='<tr class="clk" onclick="detail(\\''+esc(c.civ)+'\\')"><td>'+esc(c.civ)
        +'</td><td class="mono"><span class="pill bad">'+esc(c.running_model)+'</span></td>'
        +'<td class="mono">'+esc(c.cc||'?')+'</td></tr>';
    }
    s+='</tbody></table>';
  }
  s+='</div>';
  if(bu.count){ s+='<p class="muted" style="padding:8px 16px">Binary version unknown for '+bu.count+' CIV(s) (metrics blob had no readable cc_version).</p>'; }
  s+='</div>';
  s+='<p class="footnote">Footnote: the "config-pinned or stale process" bucket is <b>hard-code OR stale in-memory process</b>. '
    +'The sensor reads the <b>on-disk</b> <span class="mono">claude --version</span>; a stale long-lived process can hold an old '
    +'binary in memory even after a disk upgrade. So this bucket is labelled "Latest binary, still 4-6 (config-pinned or stale process)".</p>';
  s+='</section>';
  return s;
}
async function detail(id){
  const box=document.getElementById('detail'); box.classList.add('show');
  document.getElementById('detailTitle').textContent=id;
  document.getElementById('detailPre').textContent='loading...';
  try{
    const r=await fetch('/api/fleet-intel?civ='+encodeURIComponent(id),{headers:H});
    const d=await r.json();
    document.getElementById('detailPre').textContent=JSON.stringify(d.detail||d,null,2);
  }catch(e){ document.getElementById('detailPre').textContent='error: '+e; }
}
function closeDetail(){document.getElementById('detail').classList.remove('show');}
load();
</script>
</body></html>"""


async def fleet_intel_page(request: Request) -> Response:
    """Server-rendered fleet-intel dashboard. Auth enforced client-side via the
    same bearer-token fetch pattern the rest of the /api routes use; the HTML
    shell itself is static (no secrets) and all data comes from the gated
    /api/fleet-intel endpoint."""
    return Response(_FLEET_INTEL_HTML, media_type="text/html")


routes = [
    Route("/favicon.ico", endpoint=favicon),
    Route("/favicon-32.png", endpoint=favicon_png),
    Route("/apple-touch-icon.png", endpoint=apple_touch_icon),
    Route("/", endpoint=index),
    Route("/pb", endpoint=index_pb),
    Route("/refer-and-earn.html", endpoint=refer_and_earn),
    Route("/react", endpoint=index_react),
    *_react_assets_mount,
    Route("/health", endpoint=health),
    Route("/api/status", endpoint=api_status),
    Route("/api/chat/history", endpoint=api_chat_history),
    Route("/api/chat/send", endpoint=api_chat_send, methods=["POST"]),
    Route("/api/notify", endpoint=api_notify, methods=["POST"]),
    Route("/api/chat/upload", endpoint=api_chat_upload, methods=["POST"]),
    Route("/api/chat/uploads/{filename}", endpoint=api_chat_serve_upload),
    Route("/api/auth/status", endpoint=api_claude_auth_status),
    Route("/api/auth/start", endpoint=api_claude_auth_start, methods=["POST"]),
    Route("/api/auth/code", endpoint=api_claude_auth_code, methods=["POST"]),
    Route("/api/auth/url", endpoint=api_claude_auth_url),
    Route("/api/resume", endpoint=api_resume, methods=["POST"]),
    Route("/api/restart", endpoint=api_restart, methods=["POST"]),
    Route("/api/panes", endpoint=api_panes),
    Route("/api/inject/pane", endpoint=api_inject_pane, methods=["POST"]),
    Route("/api/compact/status", endpoint=api_compact_status),
    Route("/api/context", endpoint=api_context),
    Route("/api/download", endpoint=api_download),
    Route("/api/download/list", endpoint=api_download_list),
    Route("/api/referral/dashboard", endpoint=api_referral_proxy),
    Route("/api/referral/register", endpoint=api_referral_register_proxy, methods=["POST"]),
    Route("/api/referral/lookup", endpoint=api_referral_lookup_proxy),
    Route("/api/portal/owner", endpoint=api_portal_owner),
    Route("/api/referral/payout-request", endpoint=api_referral_payout_request, methods=["POST"]),
    Route("/api/referral/payout-history", endpoint=api_referral_payout_history),
    Route("/api/admin/payout/mark-paid", endpoint=api_admin_payout_mark_paid, methods=["POST"]),
    Route("/api/boop/config", endpoint=api_boop_config, methods=["GET", "POST"]),
    Route("/api/boop/status", endpoint=api_boop_status),
    Route("/api/boop/toggle", endpoint=api_boop_toggle, methods=["POST"]),
    Route("/api/boops", endpoint=api_boops_list),
    Route("/api/boops/{name}", endpoint=api_boop_read),
    Route("/api/margin/primary", endpoint=api_margin_primary, methods=["GET", "POST"]),
    Route("/api/margin/corey", endpoint=api_margin_corey, methods=["GET", "POST"]),
    Route("/api/points/summary", endpoint=api_points_summary),
    Route("/api/points/history", endpoint=api_points_history),
    Route("/webhook", endpoint=github_webhook, methods=["POST"]),
    Route("/api/deliverable", endpoint=api_deliverable, methods=["POST"]),
    Route("/api/whatsapp/qr", endpoint=api_whatsapp_qr),
    Route("/api/whatsapp/status", endpoint=api_whatsapp_status),
    Route("/fleet-intel", endpoint=fleet_intel_page),
    Route("/api/fleet-intel", endpoint=api_fleet_intel),
    Route("/api/evolution/status", endpoint=api_evolution_status),
    Route("/api/evolution/first-boot", endpoint=api_first_boot, methods=["POST"]),
    WebSocketRoute("/ws/chat", endpoint=ws_chat),
    WebSocketRoute("/ws/terminal", endpoint=ws_terminal),
]

# ---------------------------------------------------------------------------
# Auth-gate the Witness extension routes (/api/witness/*).
#
# WITNESS_ROUTES are defined in witness_extensions.py, a leaf module with no
# access to check_auth. Left ungated, /api/witness/fleet et al. exposed every
# CIV's portal_url (= bearer token), ssh_command, host_ip and tmux_session to
# any unauthenticated caller — and this portal is customer-reachable. We gate
# them HERE, where check_auth is in scope, rather than importing check_auth into
# the leaf module (avoids a circular import). Both auth paths keep working:
# check_auth already accepts the legacy .portal-token AND per-operator tokens.
# Sender-attribution (resolve_operator) is untouched.
# ---------------------------------------------------------------------------
def _require_auth(endpoint):
    async def _gated(request: Request):
        if not check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await endpoint(request)
    return _gated


_GATED_WITNESS_ROUTES = [
    Route(r.path, endpoint=_require_auth(r.endpoint), methods=list(r.methods or []))
    for r in WITNESS_ROUTES
]

app = Starlette(routes=routes + _GATED_WITNESS_ROUTES + _react_assets_mount + _vendor_mount, on_startup=[_startup])

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8097))
    print(f"[portal] Starting PureBrain Portal on port {port}")
    print(f"[portal] Bearer token: {BEARER_TOKEN}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", ws_ping_interval=30, ws_ping_timeout=90)
