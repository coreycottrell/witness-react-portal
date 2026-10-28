"""claude_usage.py: Claude plan usage limits (5-hour window + weekly) for the portal.

Adapted for the Witness portal from PureBrain portal 2.5.9 / 2.5.12 claude_usage.py
(same response contract, so a later PB adoption is a drop-in). The route handler
lives in portal_server.py (api_usage_claude) because check_auth is defined there.

Reads the Claude Code OAuth credentials of the user running the portal
(env CLAUDE_CREDENTIALS_PATH, else ~/.claude/.credentials.json), calls the
usage endpoint server-side and returns ONLY a whitelisted summary:

    {"available": true,
     "five_hour": {"utilization", "resets_at", "active"},
     "seven_day": {"utilization", "resets_at", "active"},
     "plan": <subscriptionType>, "fetched_at": <ISO UTC>}

Any failure returns {"available": false, "reason": <reason>} where reason is
one of no_credentials | expired | http_<code> | network | parse (the route
adds "error" for an unexpected exception). An explicit null window from the
API means no active window: it maps to utilization 0, resets_at null,
active false and the result stays available.

Security: the token is read in-process, sent only in the Authorization header
of the outbound request, never logged, never returned. Redirects are refused
so the token-bearing request is never replayed to another location. This
module never writes the credentials file and does no token refresh.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger("portal.claude_usage")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_BETA = "oauth-2025-04-20"
USAGE_TIMEOUT = 5
USAGE_CACHE_TTL = 60

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a 3xx surfaces as HTTPError -> http_<code>."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect)


_OPENER = _build_opener()

_cache_lock = threading.Lock()
_cache: dict = {"at": None, "value": None}


def _now() -> float:
    return time.time()


def clear_cache() -> None:
    with _cache_lock:
        _cache["at"] = None
        _cache["value"] = None


def _credentials_path() -> str:
    return os.environ.get("CLAUDE_CREDENTIALS_PATH") or os.path.expanduser("~/.claude/.credentials.json")


def _fail(reason: str) -> dict:
    logger.info("claude usage unavailable: %s", reason)
    return {"available": False, "reason": reason}


_ABSENT = object()


def _window(raw) -> dict | None:
    """Map one API window. None = malformed (caller reports parse)."""
    if raw is _ABSENT:
        return None
    if raw is None:
        return {"utilization": 0, "resets_at": None, "active": False}
    if not isinstance(raw, dict):
        return None
    util = raw.get("utilization")
    if util is not None and (isinstance(util, bool) or not isinstance(util, (int, float))):
        return None
    resets = raw.get("resets_at")
    if resets is not None and not isinstance(resets, str):
        return None
    return {"utilization": util, "resets_at": resets, "active": True}


def _fetch() -> dict:
    try:
        with open(_credentials_path(), encoding="utf-8") as fh:
            creds = json.load(fh)
    except (OSError, ValueError):
        return _fail("no_credentials")
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return _fail("no_credentials")
    token = oauth["accessToken"]
    if not isinstance(token, str) or "\r" in token or "\n" in token:
        return _fail("no_credentials")

    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and not isinstance(expires, bool):
        # Claude Code stores expiresAt in epoch milliseconds.
        if expires / 1000.0 <= _now():
            return _fail("expired")

    plan = oauth.get("subscriptionType")
    req = urllib.request.Request(USAGE_URL, method="GET", headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": USAGE_BETA,
        "Content-Type": "application/json",
    })
    try:
        with _OPENER.open(req, timeout=USAGE_TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        return _fail("expired" if exc.code == 401 else "http_%d" % exc.code)
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException):
        return _fail("network")
    except ValueError:
        # http.client puts the header value (the token) in this message: never log it.
        return _fail("no_credentials")

    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return _fail("parse")
    if not isinstance(data, dict):
        return _fail("parse")
    five = _window(data.get("five_hour", _ABSENT))
    week = _window(data.get("seven_day", _ABSENT))
    if five is None or week is None:
        return _fail("parse")
    return {
        "available": True,
        "five_hour": five,
        "seven_day": week,
        "plan": plan if isinstance(plan, str) else None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def get_usage() -> dict:
    """Cached usage summary. Blocking (up to USAGE_TIMEOUT s); call off the event loop."""
    now = _now()
    with _cache_lock:
        at, value = _cache["at"], _cache["value"]
        if value is not None and at is not None and now - at < USAGE_CACHE_TTL:
            return dict(value)
    value = _fetch()
    with _cache_lock:
        _cache["at"] = _now()
        _cache["value"] = value
    return dict(value)
