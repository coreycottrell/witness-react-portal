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
     "plan": <subscriptionType>, "fetched_at": <ISO UTC>,
     "account": {"available": true, "email", "name", "plan", "tier", "key",
                 "changed": null | {"at", "previous_email"}}}

Which Claude account is signed in is read from Claude's own profile endpoint
(/api/oauth/profile) with the SAME credential, in the same pass. "account" is
{"available": false, "reason": <reason>} when it cannot be read: the UI then
shows an unknown marker, never a stale name (the result is cached 60 s as a
whole, and a failed read is never papered over with the previous account).
"key" is a short one-way fingerprint of the account id (the id itself is never
returned). "changed" is set for CHANGE_WINDOW_S after the account differs from
the previous read (persisted in a small state file next to this module, so it
survives a portal restart).

While the usage API is rate-limiting or unreachable (network, 429, 5xx) the last good numbers
(at most STALE_MAX_S old) are returned with "stale": {"reason", "since"} so the meter shows them
marked stale instead of "n/a"; the account is never stale (it is the live read for the current token).

Any failure returns {"available": false, "reason": <reason>, "account": {...}}
where reason is one of no_credentials | expired | http_<code> | network | parse
(the route adds "error" for an unexpected exception). An explicit null window from the
API means no active window: it maps to utilization 0, resets_at null,
active false and the result stays available.

Security: the token is read in-process, sent only in the Authorization header
of the outbound request, never logged, never returned. Redirects are refused
so the token-bearing request is never replayed to another location. This
module never writes the credentials file and does no token refresh.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger("portal.claude_usage")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
USAGE_BETA = "oauth-2025-04-20"
USAGE_TIMEOUT = 5
USAGE_CACHE_TTL = 60
CHANGE_WINDOW_S = 2 * 3600   # how long a server-side "account changed" flag stays up after the switch was seen
RATE_LIMIT_TTL = 120         # the usage API 429s quickly: after one, wait longer before asking again
STALE_MAX_S = 30 * 60        # last good usage numbers may stand in (flagged stale) for this long after a transient failure

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a 3xx surfaces as HTTPError -> http_<code>."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect)


_OPENER = _build_opener()

_cache_lock = threading.Lock()
_cache: dict = {"at": None, "value": None}
# last successful usage windows, kept to stand in (flagged stale) while the usage API is rate-limiting us
_good: dict = {"at": None, "value": None}
# account of the CURRENT access token, keyed by a fingerprint of it: a token belongs to exactly one account, so
# the profile is asked for once per token (a switch or a refresh = a new token = a fresh, correct read). Never
# persisted, never returned.
_acct_lock = threading.Lock()
_acct_cache: dict = {"fp": None, "value": None}


def _now() -> float:
    return time.time()


def clear_cache() -> None:
    """Drop the cached usage result (and the stale stand-in). The per-token account cache is separate."""
    with _cache_lock:
        _cache["at"] = None
        _cache["value"] = None
        _good["at"] = None
        _good["value"] = None


def clear_account_cache() -> None:
    with _acct_lock:
        _acct_cache["fp"] = None
        _acct_cache["value"] = None


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


def _get_json(url: str, token: str):
    """GET url with the bearer token. Returns (parsed_json, None) or (None, reason).

    The token only ever travels in the Authorization header of this request; redirects are
    refused; nothing here logs or returns it.
    """
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": USAGE_BETA,
        "Content-Type": "application/json",
    })
    try:
        with _OPENER.open(req, timeout=USAGE_TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        return None, ("expired" if exc.code == 401 else "http_%d" % exc.code)
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException):
        return None, "network"
    except ValueError:
        # http.client puts the header value (the token) in this message: never log it.
        return None, "no_credentials"
    try:
        return json.loads(body), None
    except (ValueError, TypeError):
        return None, "parse"


# ── Which account is signed in (read-only, same credential) ─────────────────────────────

_EMAIL_RE = re.compile(r"^[^\s@<>\"']{1,64}@[A-Za-z0-9.-]{1,190}$")
_TIER_RE = re.compile(r"(\d+x)$")
_state_lock = threading.Lock()


def _state_path() -> str:
    return os.environ.get("CLAUDE_ACCOUNT_STATE_PATH") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".claude-account-last.json")


def _clean_text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    return cleaned[:limit] or None


def _plan_from_profile(account: dict, org: dict, fallback) -> tuple:
    """(plan, tier) e.g. ("max", "20x"). Falls back to the credential file's subscriptionType."""
    plan = None
    if account.get("has_claude_max") is True:
        plan = "max"
    elif account.get("has_claude_pro") is True:
        plan = "pro"
    if plan is None and isinstance(fallback, str) and fallback:
        plan = fallback
    tier = None
    rlt = org.get("rate_limit_tier") if isinstance(org, dict) else None
    if isinstance(rlt, str):
        m = _TIER_RE.search(rlt)
        tier = m.group(1) if m else None
    return plan, tier


def _load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    """Atomic, 0600, and never fatal: a failed write must not break the meter."""
    path = _state_path()
    try:
        fd, tmp = tempfile.mkstemp(prefix=".claude-account-", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:  # class name only
        logger.info("claude account state not saved: %s", type(exc).__name__)


def _note_account(key: str, email: str | None, now: float) -> dict | None:
    """Compare with the previous read. Returns the change record while it is still fresh."""
    with _state_lock:
        state = _load_state()
        if state.get("key") != key:
            had_previous = bool(state.get("key"))
            state = {
                "key": key,
                "email": email,
                "since": now,
                "previous_email": state.get("email") if had_previous else None,
                "changed_at": now if had_previous else None,
            }
            _save_state(state)
        elif state.get("email") != email:
            state["email"] = email          # same account, new address: no "change" flag
            _save_state(state)
        changed_at = state.get("changed_at")
        if isinstance(changed_at, (int, float)) and 0 <= now - changed_at < CHANGE_WINDOW_S:
            return {
                "at": datetime.fromtimestamp(changed_at, timezone.utc).isoformat(),
                "previous_email": _clean_text(state.get("previous_email"), 254),
            }
        return None


def _account(token: str, fallback_plan) -> dict:
    """Whitelisted summary of the signed-in account, or {"available": False, "reason"}."""
    data, reason = _get_json(PROFILE_URL, token)
    if reason:
        return {"available": False, "reason": reason}
    acct = data.get("account") if isinstance(data, dict) else None
    if not isinstance(acct, dict):
        return {"available": False, "reason": "parse"}
    email = _clean_text(acct.get("email"), 254)
    if email is not None and not _EMAIL_RE.match(email):
        email = None
    name = _clean_text(acct.get("display_name") or acct.get("full_name"), 80)
    ident = _clean_text(acct.get("uuid"), 64) or (email.lower() if email else None)
    if ident is None:
        return {"available": False, "reason": "parse"}
    key = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:12]
    org = data.get("organization")
    plan, tier = _plan_from_profile(acct, org if isinstance(org, dict) else {}, fallback_plan)
    return {
        "available": True,
        "email": email,
        "name": name,
        "plan": plan,
        "tier": tier,
        "key": key,
        "changed": _note_account(key, email, _now()),
    }


def _account_for_token(token: str, fallback_plan) -> dict:
    """_account(), but the profile endpoint is only asked once per access token."""
    fp = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    with _acct_lock:
        cached = _acct_cache["value"] if _acct_cache["fp"] == fp else None
    if cached is not None:
        out = dict(cached)
        out["changed"] = _note_account(out["key"], out["email"], _now())
        return out
    out = _account(token, fallback_plan)
    if out.get("available"):
        with _acct_lock:
            _acct_cache["fp"] = fp
            _acct_cache["value"] = dict(out)
    return out


def _unavailable_account(reason: str) -> dict:
    return {"available": False, "reason": reason}


def _is_transient(reason: str) -> bool:
    return reason in ("network", "http_429") or (reason.startswith("http_5") and len(reason) == 8)


def _stale_standin(reason: str, account: dict) -> dict | None:
    """While the usage API is rate-limiting/unreachable, show the last good numbers marked stale instead of n/a.

    Only usage numbers stand in (they drift slowly); the account is always the live read for the current token.
    """
    if not _is_transient(reason):
        return None
    with _cache_lock:
        at, value = _good["at"], _good["value"]
    if value is None or at is None or _now() - at > STALE_MAX_S:
        return None
    logger.info("claude usage unavailable (%s): serving last good numbers as stale", reason)
    return dict(value, stale={"reason": reason, "since": value.get("fetched_at")}, account=account)


def _fetch() -> dict:
    try:
        with open(_credentials_path(), encoding="utf-8") as fh:
            creds = json.load(fh)
    except (OSError, ValueError):
        out = _fail("no_credentials")
        out["account"] = _unavailable_account("no_credentials")
        return out
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        out = _fail("no_credentials")
        out["account"] = _unavailable_account("no_credentials")
        return out
    token = oauth["accessToken"]
    if not isinstance(token, str) or "\r" in token or "\n" in token:
        out = _fail("no_credentials")
        out["account"] = _unavailable_account("no_credentials")
        return out

    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and not isinstance(expires, bool):
        # Claude Code stores expiresAt in epoch milliseconds. We never refresh.
        if expires / 1000.0 <= _now():
            out = _fail("expired")
            out["account"] = _unavailable_account("expired")
            return out

    plan = oauth.get("subscriptionType")
    account = _account_for_token(token, plan)

    data, reason = _get_json(USAGE_URL, token)
    if reason:
        stale = _stale_standin(reason, account)
        if stale is not None:
            return stale
        out = _fail(reason)
        out["account"] = account
        return out
    if not isinstance(data, dict):
        out = _fail("parse")
        out["account"] = account
        return out
    five = _window(data.get("five_hour", _ABSENT))
    week = _window(data.get("seven_day", _ABSENT))
    if five is None or week is None:
        out = _fail("parse")
        out["account"] = account
        return out
    good = {
        "available": True,
        "five_hour": five,
        "seven_day": week,
        "plan": plan if isinstance(plan, str) else None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    with _cache_lock:
        _good["at"] = _now()
        _good["value"] = dict(good)
    return dict(good, account=account)


def get_usage() -> dict:
    """Cached usage summary. Blocking (up to USAGE_TIMEOUT s); call off the event loop."""
    now = _now()
    with _cache_lock:
        at, value = _cache["at"], _cache["value"]
        ttl = USAGE_CACHE_TTL
        if isinstance(value, dict) and (value.get("reason") == "http_429" or (value.get("stale") or {}).get("reason") == "http_429"):
            ttl = RATE_LIMIT_TTL
        if value is not None and at is not None and now - at < ttl:
            return dict(value)
    value = _fetch()
    with _cache_lock:
        _cache["at"] = _now()
        _cache["value"] = value
    return dict(value)
