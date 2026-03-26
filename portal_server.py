#!/usr/bin/env python3
"""PureBrain Portal Server — per-CIV mini server for purebrain.ai
Auth via Bearer token. JSONL-based chat history (same as TG bot).
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import subprocess
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
# This works for any user in any container without manual patching.
_encoded_home = str(Path.home()).replace("/", "-")
LOG_ROOT = Path.home() / ".claude" / "projects" / _encoded_home
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


def get_tmux_session() -> str:
    """Find the live primary Claude Code session for this container."""
    def alive(name):
        try:
            subprocess.check_output(["tmux", "has-session", "-t", name], stderr=subprocess.DEVNULL)
            return True
        except subprocess.CalledProcessError:
            return False

    # FIRST: Find the currently attached session — mirrors telegram_bridge logic.
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

    marker = Path.home() / ".current_session"
    if marker.exists():
        name = marker.read_text().strip()
        if name and alive(name):
            return name
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
        logs = sorted(LOG_ROOT.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
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

                messages.append({
                    "role": role,
                    "text": combined,
                    "timestamp": int(ts),
                    "id": entry.get("uuid", f"msg-{log_path.stem[:8]}-{len(messages)}")
                })
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


def _save_portal_message(text, role="user"):
    """Save a message sent via the portal."""
    entry = {
        "role": role,
        "text": text,
        "timestamp": int(time.time()),
        "id": f"portal-{int(time.time() * 1000)}",
    }
    try:
        with PORTAL_CHAT_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        _portal_log_ids.add(entry["id"])  # Prevent _mirror_to_portal_log from double-writing
    except Exception:
        pass
    return entry


def _parse_all_messages(last_n=100):
    """Parse messages across all recent session logs + portal log."""
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

    return deduped[-last_n:] if len(deduped) > last_n else deduped


def check_auth(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:] == BEARER_TOKEN
    return request.query_params.get("token") == BEARER_TOKEN


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


async def api_chat_send(request: Request) -> JSONResponse:
    """Inject a message into the tmux session. Response comes via /api/chat/stream or history."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        message = str(body.get("message", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    # Save to portal chat log for history
    _save_portal_message(message, role="user")

    # Tag injection source so tmux pane shows where input came from
    host = request.headers.get("referer", "")
    if "react" in host:
        tagged = f"[portal-react] {message}"
    else:
        tagged = f"[portal] {message}"

    session = get_tmux_session()
    try:
        # Leading newline clears any partial input in buffer
        subprocess.run(["tmux", "send-keys", "-t", session, "-l", f"\n{tagged}"],
                       check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "send-keys", "-t", session, "Enter"],
                       check=True, stderr=subprocess.DEVNULL)
        # 5x Enter retries (matches Telegram bridge pattern) — ensures Claude
        # processes the message even if busy with tool calls or generation
        async def _retry_enters():
            for _ in range(5):
                await asyncio.sleep(0.5)
                subprocess.run(["tmux", "send-keys", "-t", session, "Enter"],
                               check=False, stderr=subprocess.DEVNULL)
        asyncio.ensure_future(_retry_enters())
        return JSONResponse({"status": "sent", "timestamp": int(time.time())})
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


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

    entry = _save_portal_message(message, role="assistant")
    return JSONResponse({"status": "saved", "id": entry["id"], "timestamp": entry["timestamp"]})


async def ws_chat(websocket: WebSocket) -> None:
    """Stream new chat messages via WebSocket. Polls JSONL log for new entries."""
    token = websocket.query_params.get("token", "")
    if token != BEARER_TOKEN:
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
        tmux_ok = False
        try:
            # Leading newline clears any partial input in buffer
            subprocess.run(
                ["tmux", "send-keys", "-t", session, "-l", f"\n{notification}"],
                check=True, stderr=subprocess.DEVNULL
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", session, "Enter"],
                check=True, stderr=subprocess.DEVNULL
            )
            tmux_ok = True
            # 5x Enter retries — ensures Claude processes even if busy
            async def _retry_enters():
                for _ in range(5):
                    await asyncio.sleep(0.5)
                    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"],
                                   check=False, stderr=subprocess.DEVNULL)
            asyncio.ensure_future(_retry_enters())
        except Exception:
            pass  # Don't fail the upload if tmux injection fails

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


def _find_primary_pane():
    """Find the tmux pane ID running the primary Claude Code instance."""
    session = get_tmux_session()
    try:
        # List all panes with their IDs
        out = subprocess.check_output(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_id}"],
            stderr=subprocess.DEVNULL, text=True
        )
        panes = [p.strip() for p in out.splitlines() if p.strip()]
        if not panes:
            return session  # fallback to session target

        # Primary is always the first pane (index 0)
        # Team leads are spawned in subsequent panes
        return panes[0]
    except Exception:
        return session


async def ws_terminal(websocket: WebSocket) -> None:
    """Stream tmux pane content via WebSocket. Read-only."""
    token = websocket.query_params.get("token", "")
    if token != BEARER_TOKEN:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    pane_target = _find_primary_pane()
    last_content = ""

    try:
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

            await asyncio.sleep(0.5)
    except (WebSocketDisconnect, Exception):
        pass


async def api_context(request: Request) -> JSONResponse:
    """Return real context window usage from the latest Claude session JSONL."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        MAX_TOKENS = 170_000  # ~30k reserved for responses/summaries
        logs = sorted(LOG_ROOT.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not logs:
            return JSONResponse({"input_tokens": 0, "max_tokens": MAX_TOKENS, "pct": 0})

        latest = logs[0]
        input_tokens = 0
        cache_read = 0
        cache_creation = 0

        # Read last entry that has usage data
        with open(latest) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    usage = entry.get("usage") or entry.get("message", {}).get("usage")
                    if usage and isinstance(usage, dict):
                        t = usage.get("input_tokens", 0)
                        if t:
                            input_tokens = t
                            cache_read = usage.get("cache_read_input_tokens", 0)
                            cache_creation = usage.get("cache_creation_input_tokens", 0)
                except (json.JSONDecodeError, KeyError):
                    continue

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
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_resume(request: Request) -> JSONResponse:
    """Launch a new Claude instance resuming the most recent conversation session."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        logs = sorted(LOG_ROOT.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not logs:
            return JSONResponse({"error": "no sessions found"}, status_code=404)
        session_id = logs[0].stem  # UUID filename without .jsonl
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
        claude_cmd = (
            f"claude --model claude-sonnet-4-6 --dangerously-skip-permissions "
            f"--resume {session_id}"
        )
        subprocess.Popen(
            ["tmux", "new-session", "-d", "-s", tmux_session, "-c", project_dir, claude_cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return JSONResponse({"status": "resuming", "session_id": session_id, "tmux": tmux_session})
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


async def api_claude_auth_start(request: Request) -> JSONResponse:
    """Inject /login into the Claude tmux session to start OAuth flow.

    If Claude Code is not running in the pane (e.g. fresh container with only
    a bash shell), starts Claude first and waits for it to be ready before
    sending /login.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _captured_oauth_url
    _captured_oauth_url = None

    # Ensure a dedicated Claude tmux session exists.
    # On fresh containers, only 'portal-server' exists — we can't inject
    # Claude into the portal's own pane (it's running the Python server).
    session = get_tmux_session()
    if "portal" in session.lower():
        claude_session = f"{CIV_NAME}-primary"
        subprocess.run(["tmux", "new-session", "-d", "-s", claude_session],
                       stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        session = claude_session

    pane = _find_primary_pane()
    _save_portal_message(f"🔐 Auth flow started — checking Claude in {session} (pane {pane})", role="assistant")
    try:
        # CRITICAL: Resize tmux window to 500 cols BEFORE sending /login.
        # Claude prints the OAuth URL as one long line — if the window is narrow
        # (e.g. 80 cols), the URL wraps and tmux capture-pane -J can't reliably
        # un-wrap it. At 500 cols the URL fits on one line, no wrapping, clean capture.
        subprocess.run(["tmux", "resize-window", "-t", pane, "-x", "500"],
                       stderr=subprocess.DEVNULL)
        time.sleep(0.3)

        # If Claude Code is NOT running in the pane (e.g. bare bash shell on
        # a fresh container), start it first and wait for it to be ready.
        if not _is_claude_running_in_pane(pane):
            _save_portal_message("🚀 Claude not running — starting Claude Code in tmux...", role="assistant")
            subprocess.run(["tmux", "send-keys", "-t", pane, "-l",
                            "claude --dangerously-skip-permissions"],
                           check=True, stderr=subprocess.DEVNULL)
            subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"],
                           check=True, stderr=subprocess.DEVNULL)
            # Poll until Claude is the active process (up to 30 seconds)
            for _ in range(60):
                time.sleep(0.5)
                if _is_claude_running_in_pane(pane):
                    break
            else:
                _save_portal_message("⚠️ Claude didn't start within 30s — sending /login anyway", role="assistant")
            # Give Claude a moment to fully render its prompt
            time.sleep(3)

        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "/login"],
                       check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"],
                       check=True, stderr=subprocess.DEVNULL)
        # Wait for the 3-option login menu to render, then press Enter
        # to auto-select option 1 (already highlighted by default)
        time.sleep(2)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"],
                       check=False, stderr=subprocess.DEVNULL)
        _save_portal_message("⏳ /login sent — waiting for OAuth URL to appear in terminal...", role="assistant")
        return JSONResponse({"started": True})
    except subprocess.CalledProcessError as e:
        _save_portal_message(f"❌ Auth start failed: tmux error — pane={pane}, err={e}", role="assistant")
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


async def api_claude_auth_code(request: Request) -> JSONResponse:
    """Inject the OAuth authorization code into the Claude tmux session."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        code = str(body.get("code", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not code:
        return JSONResponse({"error": "empty code"}, status_code=400)
    pane = _find_primary_pane()
    _save_portal_message(f"⌨️ Auth code submitted — injecting into {get_tmux_session()}...", role="assistant")
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", code],
                       check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"],
                       check=True, stderr=subprocess.DEVNULL)
        # Mark that a human has completed the OAuth flow via the portal.
        # This lets api_claude_auth_status trust the credentials file.
        HUMAN_AUTH_MARKER.write_text(f"human-auth-initiated:{int(time.time())}")
        _save_portal_message("✅ Code injected — Claude is authenticating...", role="assistant")
        return JSONResponse({"injected": True})
    except subprocess.CalledProcessError as e:
        _save_portal_message(f"❌ Code injection failed: tmux error — pane={pane}, err={e}", role="assistant")
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


async def api_claude_auth_url(request: Request) -> JSONResponse:
    """Poll for the captured OAuth URL from tmux output."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _captured_oauth_url
    if _captured_oauth_url:
        return JSONResponse({"url": _captured_oauth_url, "ready": True})
    pane = _find_primary_pane()
    try:
        # -J joins wrapped lines so long URLs aren't truncated at terminal width
        content = subprocess.check_output(
            ["tmux", "capture-pane", "-t", pane, "-p", "-J", "-S", "-200"],
            stderr=subprocess.DEVNULL, text=True
        )
        match = OAUTH_URL_PATTERN.search(content)
        if match:
            candidate = match.group(0).strip()
            # Validate URL is complete — must contain state= parameter.
            # A truncated URL is worse than no URL (causes "missing state" error on claude.ai).
            if "state=" not in candidate:
                _save_portal_message(f"⚠️ OAuth URL found but truncated (missing state=) — retrying capture", role="assistant")
            else:
                _captured_oauth_url = candidate
                _save_portal_message(f"🔗 OAuth URL ready ({len(candidate)} chars, state= confirmed)", role="assistant")
                return JSONResponse({"url": _captured_oauth_url, "ready": True})
        # Silently return — no notification on each poll. Only notify when URL is found.
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
            logs = sorted(LOG_ROOT.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
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
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            return []

    if request.method == "POST":
        try:
            body = await request.json()
            content = body.get("content", "").strip()
            if not content:
                return JSONResponse({"error": "empty content"}, status_code=400)
            entry = {
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "author": author,
                "content": content,
            }
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
    Route("/webhook", endpoint=github_webhook, methods=["POST"]),
    Route("/api/deliverable", endpoint=api_deliverable, methods=["POST"]),
    Route("/api/whatsapp/qr", endpoint=api_whatsapp_qr),
    Route("/api/whatsapp/status", endpoint=api_whatsapp_status),
    Route("/api/evolution/status", endpoint=api_evolution_status),
    Route("/api/evolution/first-boot", endpoint=api_first_boot, methods=["POST"]),
    WebSocketRoute("/ws/chat", endpoint=ws_chat),
    WebSocketRoute("/ws/terminal", endpoint=ws_terminal),
]

app = Starlette(routes=routes + WITNESS_ROUTES + _react_assets_mount + _vendor_mount, on_startup=[_startup])

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8097))
    print(f"[portal] Starting PureBrain Portal on port {port}")
    print(f"[portal] Bearer token: {BEARER_TOKEN}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
