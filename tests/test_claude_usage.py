"""Guard for the Claude plan usage meter backend (GET /api/usage/claude).

Pins: the response contract the header meter reads, every degrade path
(no_credentials / expired / http_<code> / network / parse), that the OAuth token
never appears in a response or a log line, that redirects are refused, and that
the route is registered behind check_auth.

Run:  python3 -m pytest tests/test_claude_usage.py -q
Hermetic: no network, no real ~/.claude (credentials path is monkeypatched).
"""
import asyncio
import io
import json
import logging
import sys
import time
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_usage as cu  # noqa: E402

DUMMY_TOKEN = "DUMMYTOKEN-must-never-leak"   # deliberately NOT shaped like a real token (push secret-scan)

SAMPLE_API = {
    "five_hour": {"utilization": 48.0, "resets_at": "2026-10-28T16:59:59+00:00"},
    "seven_day": {"utilization": 39.0, "resets_at": "2026-11-01T14:59:59+00:00"},
    "limits": [{"kind": "session", "percent": 48}],
}


class _Resp:
    def __init__(self, payload):
        self._b = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _creds(tmp_path, monkeypatch, expires_in_s=3600, token=DUMMY_TOKEN, plan="max"):
    f = tmp_path / "creds.json"
    oauth = {"accessToken": token, "subscriptionType": plan}
    if expires_in_s is not None:
        oauth["expiresAt"] = int((time.time() + expires_in_s) * 1000)
    f.write_text(json.dumps({"claudeAiOauth": oauth}))
    monkeypatch.setenv("CLAUDE_CREDENTIALS_PATH", str(f))
    return f


PROFILE_A = {
    "account": {"uuid": "11111111-aaaa-bbbb-cccc-000000000001", "full_name": "Russell K", "display_name": "Russell",
                "email": "russell@example.com", "has_claude_max": True, "has_claude_pro": False, "created_at": "2025-01-01"},
    "organization": {"uuid": "org-1", "name": "Russell's Org", "organization_type": "claude_max",
                     "rate_limit_tier": "default_claude_max_20x", "billing_type": "stripe_subscription"},
    "application": {"uuid": "app", "name": "Claude Code", "slug": "claude-code"},
}
PROFILE_B = {
    "account": {"uuid": "22222222-aaaa-bbbb-cccc-000000000002", "full_name": "Corey C", "display_name": "Corey",
                "email": "corey@example.com", "has_claude_max": False, "has_claude_pro": True},
    "organization": {"organization_type": "claude_pro", "rate_limit_tier": "default_claude_pro"},
}


def _route(profile=PROFILE_A, usage=SAMPLE_API, seen=None):
    """URL-aware fake for cu._OPENER.open: profile and usage are separate upstream calls."""
    def fake_open(req, timeout=None):
        if seen is not None:
            seen.append(req.full_url)
        payload = profile if req.full_url.endswith("/profile") else usage
        if isinstance(payload, Exception):
            raise payload
        return _Resp(payload)
    return fake_open


@pytest.fixture(autouse=True)
def _fresh_cache(tmp_path, monkeypatch):
    # The account-change state file must never land in the repo or the live portal dir.
    monkeypatch.setenv("CLAUDE_ACCOUNT_STATE_PATH", str(tmp_path / "account-state.json"))
    cu.clear_cache()
    cu.clear_account_cache()
    yield
    cu.clear_cache()
    cu.clear_account_cache()


def test_happy_path_returns_whitelisted_summary_only(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", _route())
    out = cu.get_usage()
    assert out["available"] is True
    assert out["five_hour"] == {"utilization": 48.0, "resets_at": "2026-10-28T16:59:59+00:00", "active": True}
    assert out["seven_day"]["utilization"] == 39.0
    assert out["plan"] == "max"
    assert set(out) == {"available", "five_hour", "seven_day", "plan", "fetched_at", "account"}
    assert DUMMY_TOKEN not in json.dumps(out)


def test_token_only_in_authorization_header(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    seen = {}

    reqs = []

    def fake_open(req, timeout=None):
        reqs.append((req.full_url, req.get_header("Authorization"), req.get_header("Anthropic-beta"), req.get_method()))
        return _Resp(PROFILE_A if req.full_url.endswith("/profile") else SAMPLE_API)

    monkeypatch.setattr(cu._OPENER, "open", fake_open)
    cu.get_usage()
    assert {u for u, *_ in reqs} == {"https://api.anthropic.com/api/oauth/usage",
                                     "https://api.anthropic.com/api/oauth/profile"}
    for _, auth, beta, method in reqs:
        assert auth == "Bearer " + DUMMY_TOKEN
        assert beta == "oauth-2025-04-20"
        assert method == "GET"


def test_no_credentials_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CREDENTIALS_PATH", str(tmp_path / "missing.json"))
    out = cu.get_usage()
    assert out == {"available": False, "reason": "no_credentials",
                   "account": {"available": False, "reason": "no_credentials"}}


def test_expired_token_never_calls_network(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch, expires_in_s=-60)

    def boom(*a, **k):
        raise AssertionError("network must not be called with an expired token")

    monkeypatch.setattr(cu._OPENER, "open", boom)
    out = cu.get_usage()
    assert out == {"available": False, "reason": "expired", "account": {"available": False, "reason": "expired"}}


def test_http_401_maps_to_expired_and_other_codes_to_http_n(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)

    def raiser(code):
        def _r(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b""))
        return _r

    monkeypatch.setattr(cu._OPENER, "open", raiser(401))
    out = cu.get_usage()
    assert out["reason"] == "expired" and out["account"] == {"available": False, "reason": "expired"}
    cu.clear_cache()
    monkeypatch.setattr(cu._OPENER, "open", raiser(429))
    assert cu.get_usage()["reason"] == "http_429"
    cu.clear_cache()
    monkeypatch.setattr(cu._OPENER, "open", raiser(302))  # redirects are refused -> HTTPError
    assert cu.get_usage()["reason"] == "http_302"


def test_redirect_handler_refuses_every_redirect():
    h = cu._NoRedirect()
    assert h.redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None


def test_network_error(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)

    def down(req, timeout=None):
        raise urllib.error.URLError("dns")

    monkeypatch.setattr(cu._OPENER, "open", down)
    assert cu.get_usage() == {"available": False, "reason": "network",
                              "account": {"available": False, "reason": "network"}}


@pytest.mark.parametrize("payload", [b"not json", b"[1,2]", {"five_hour": "x", "seven_day": None},
                                      {"seven_day": None}, {"five_hour": {"utilization": True}, "seven_day": None}])
def test_malformed_payloads_report_parse(tmp_path, monkeypatch, payload):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", _route(usage=payload))
    out = cu.get_usage()
    assert out["available"] is False and out["reason"] == "parse"
    assert out["account"]["available"] is True        # the account read is independent of a bad usage payload


def test_null_window_means_no_active_window(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", _route(usage={"five_hour": None, "seven_day": SAMPLE_API["seven_day"]}))
    out = cu.get_usage()
    assert out["available"] is True
    assert out["five_hour"] == {"utilization": 0, "resets_at": None, "active": False}


def test_result_is_cached_for_ttl(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    calls = []

    monkeypatch.setattr(cu._OPENER, "open", _route(seen=calls))
    cu.get_usage()
    cu.get_usage()
    assert len(calls) == 2          # one usage + one profile call, then served from the 60 s cache


def test_token_never_logged(tmp_path, monkeypatch, caplog):
    _creds(tmp_path, monkeypatch, expires_in_s=-60)
    with caplog.at_level(logging.DEBUG):
        cu.get_usage()
    assert DUMMY_TOKEN not in caplog.text


def test_module_never_writes_credentials(tmp_path, monkeypatch):
    f = _creds(tmp_path, monkeypatch)
    before = f.read_bytes()
    mtime = f.stat().st_mtime_ns
    monkeypatch.setattr(cu._OPENER, "open", _route())
    cu.get_usage()
    assert f.read_bytes() == before and f.stat().st_mtime_ns == mtime


# ── Route wiring in portal_server.py ────────────────────────────────────────

def _ps():
    import portal_server as ps
    return ps


class _Req:
    def __init__(self, token=None):
        self.headers = {"authorization": "Bearer " + token} if token else {}
        self.query_params = {}


def test_route_registered_get_and_behind_check_auth(monkeypatch):
    ps = _ps()
    paths = {getattr(r, "path", None): r for r in ps.app.routes}
    assert "/api/usage/claude" in paths
    resp = asyncio.run(ps.api_usage_claude(_Req()))
    assert resp.status_code == 401


def test_route_returns_200_available_false_on_failure_never_401(tmp_path, monkeypatch):
    ps = _ps()
    monkeypatch.setattr(ps, "check_auth", lambda request: True)
    monkeypatch.setenv("CLAUDE_CREDENTIALS_PATH", str(tmp_path / "missing.json"))
    cu.clear_cache()
    resp = asyncio.run(ps.api_usage_claude(_Req("x")))
    assert resp.status_code == 200
    assert json.loads(resp.body) == {"available": False, "reason": "no_credentials",
                                     "account": {"available": False, "reason": "no_credentials"}}


def test_route_swallows_unexpected_error_without_leaking_message(monkeypatch):
    ps = _ps()
    monkeypatch.setattr(ps, "check_auth", lambda request: True)

    def explode():
        raise RuntimeError("secret " + DUMMY_TOKEN)

    monkeypatch.setattr(ps.claude_usage, "get_usage", explode)
    resp = asyncio.run(ps.api_usage_claude(_Req("x")))
    assert resp.status_code == 200
    body = resp.body.decode()
    assert DUMMY_TOKEN not in body
    assert json.loads(body) == {"available": False, "reason": "error"}



# ── Signed-in account (Russell 2026-10-28: show which Claude account is logged in) ───────

def test_account_whitelist_and_plan(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", _route())
    acct = cu.get_usage()["account"]
    assert acct["available"] is True
    assert acct["email"] == "russell@example.com"
    assert acct["name"] == "Russell"
    assert acct["plan"] == "max" and acct["tier"] == "20x"
    assert acct["changed"] is None
    assert set(acct) == {"available", "email", "name", "plan", "tier", "key", "changed"}
    blob = json.dumps(acct)
    for secret in ("11111111-aaaa", "org-1", "Russell's Org", "Claude Code", DUMMY_TOKEN):
        assert secret not in blob                       # ids / org / app never returned
    assert len(acct["key"]) == 12


def test_account_pro_plan_and_credential_fallback(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch, plan="pro")
    monkeypatch.setattr(cu._OPENER, "open", _route(profile=PROFILE_B))
    acct = cu.get_usage()["account"]
    assert acct["plan"] == "pro" and acct["tier"] is None
    cu.clear_cache()
    bare = {"account": {"uuid": "u-3", "email": "x@example.com"}}
    monkeypatch.setattr(cu._OPENER, "open", _route(profile=bare))
    assert cu.get_usage()["account"]["plan"] == "pro"   # falls back to the credential file's subscriptionType


@pytest.mark.parametrize("failure,reason", [
    (urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(b"")), "expired"),
    (urllib.error.HTTPError("u", 403, "x", {}, io.BytesIO(b"")), "http_403"),
    (urllib.error.URLError("dns"), "network"),
    (b"not json", "parse"),
    ({"organization": {}}, "parse"),                     # no account object
    ({"account": {"email": "x@example.com", "uuid": None}}, None),   # email is enough to identify
    ({"account": {"display_name": "NoIdentity"}}, "parse"),           # nothing to key on
])
def test_account_unknown_marker_never_a_guess(tmp_path, monkeypatch, failure, reason):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", _route(profile=failure))
    out = cu.get_usage()
    assert out["available"] is True                       # usage itself still works
    if reason is None:
        assert out["account"]["available"] is True
    else:
        assert out["account"] == {"available": False, "reason": reason}


def test_failed_read_does_not_resurrect_the_previous_account(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", _route())
    assert cu.get_usage()["account"]["email"] == "russell@example.com"
    cu.clear_cache()
    _creds(tmp_path, monkeypatch, token=DUMMY_TOKEN + "-next")   # a new token cannot borrow the old token's name
    monkeypatch.setattr(cu._OPENER, "open", _route(profile=urllib.error.URLError("down")))
    acct = cu.get_usage()["account"]
    assert acct == {"available": False, "reason": "network"}    # unknown, not the stale name


def test_malicious_profile_values_are_sanitised(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    evil = {"account": {"uuid": "u", "email": "<script>alert(1)</script>@x.com", "display_name": "A\x00B\n" + "x" * 500}}
    monkeypatch.setattr(cu._OPENER, "open", _route(profile=evil))
    acct = cu.get_usage()["account"]
    assert acct["email"] is None                       # fails the conservative email pattern
    assert "\x00" not in acct["name"] and "\n" not in acct["name"] and len(acct["name"]) <= 80


def test_account_change_is_flagged_and_persists_across_restart(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", _route(profile=PROFILE_A))
    first = cu.get_usage()["account"]
    assert first["changed"] is None                      # first ever read: no baseline, no flag
    cu.clear_cache()
    assert cu.get_usage()["account"]["changed"] is None  # same account again: still no flag
    cu.clear_cache()
    _creds(tmp_path, monkeypatch, token=DUMMY_TOKEN + "-B")        # signing in as another account = another token
    monkeypatch.setattr(cu._OPENER, "open", _route(profile=PROFILE_B))
    switched = cu.get_usage()["account"]
    assert switched["email"] == "corey@example.com"
    assert switched["key"] != first["key"]
    assert switched["changed"]["previous_email"] == "russell@example.com"
    assert switched["changed"]["at"]
    # "restart": module caches gone, only the state file remains
    cu.clear_cache()
    again = cu.get_usage()["account"]
    assert again["changed"]["previous_email"] == "russell@example.com"
    assert (tmp_path / "account-state.json").stat().st_mode & 0o777 == 0o600
    assert DUMMY_TOKEN not in (tmp_path / "account-state.json").read_text()


def test_change_flag_expires_after_the_window(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", _route(profile=PROFILE_A))
    cu.get_usage()
    cu.clear_cache()
    _creds(tmp_path, monkeypatch, token=DUMMY_TOKEN + "-B")
    monkeypatch.setattr(cu._OPENER, "open", _route(profile=PROFILE_B))
    assert cu.get_usage()["account"]["changed"] is not None
    cu.clear_cache()
    real = time.time()
    monkeypatch.setattr(cu, "_now", lambda: real + cu.CHANGE_WINDOW_S + 5)
    # keep the access token valid at the shifted clock
    _creds(tmp_path, monkeypatch, token=DUMMY_TOKEN + "-B", expires_in_s=cu.CHANGE_WINDOW_S + 3600)
    assert cu.get_usage()["account"]["changed"] is None


def test_same_account_new_email_is_not_a_switch(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", _route(profile=PROFILE_A))
    cu.get_usage()
    cu.clear_cache()
    _creds(tmp_path, monkeypatch, token=DUMMY_TOKEN + "-refreshed")   # a refreshed token, same account
    renamed = json.loads(json.dumps(PROFILE_A))
    renamed["account"]["email"] = "russell.new@example.com"
    monkeypatch.setattr(cu._OPENER, "open", _route(profile=renamed))
    acct = cu.get_usage()["account"]
    assert acct["email"] == "russell.new@example.com" and acct["changed"] is None


def test_corrupt_or_unwritable_state_never_breaks_the_meter(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    (tmp_path / "account-state.json").write_text("{not json")
    monkeypatch.setattr(cu._OPENER, "open", _route())
    out = cu.get_usage()
    assert out["available"] is True and out["account"]["available"] is True
    cu.clear_cache()
    monkeypatch.setenv("CLAUDE_ACCOUNT_STATE_PATH", str(tmp_path / "no-such-dir" / "x.json"))
    out = cu.get_usage()
    assert out["available"] is True and out["account"]["available"] is True


def test_profile_token_never_logged_or_returned(tmp_path, monkeypatch, caplog):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", _route(profile=urllib.error.URLError("boom")))
    with caplog.at_level(logging.DEBUG):
        out = cu.get_usage()
    assert DUMMY_TOKEN not in caplog.text and DUMMY_TOKEN not in json.dumps(out)


def test_route_includes_account_and_stays_behind_auth(tmp_path, monkeypatch):
    ps = _ps()
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", _route())
    assert asyncio.run(ps.api_usage_claude(_Req())).status_code == 401
    monkeypatch.setattr(ps, "check_auth", lambda request: True)
    body = json.loads(asyncio.run(ps.api_usage_claude(_Req("x"))).body)
    assert body["account"]["email"] == "russell@example.com"


# ── upstream budget: the usage API rate-limits fast (live 2026-10-28: Retry-After 0 on a 429) ──────────

def test_profile_is_asked_once_per_access_token(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(cu._OPENER, "open", _route(seen=seen))
    cu.get_usage()
    cu.clear_cache()                       # result cache expired, same token
    out = cu.get_usage()
    assert sum(1 for u in seen if u.endswith("/profile")) == 1
    assert sum(1 for u in seen if u.endswith("/usage")) == 2
    assert out["account"]["email"] == "russell@example.com"
    cu.clear_cache()
    _creds(tmp_path, monkeypatch, token=DUMMY_TOKEN + "-B")        # new token -> asked again
    monkeypatch.setattr(cu._OPENER, "open", _route(profile=PROFILE_B, seen=seen))
    assert cu.get_usage()["account"]["email"] == "corey@example.com"
    assert sum(1 for u in seen if u.endswith("/profile")) == 2


def test_cached_account_keeps_the_change_window_honest(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", _route(profile=PROFILE_A))
    cu.get_usage()
    cu.clear_cache()
    _creds(tmp_path, monkeypatch, token=DUMMY_TOKEN + "-B")
    monkeypatch.setattr(cu._OPENER, "open", _route(profile=PROFILE_B))
    assert cu.get_usage()["account"]["changed"] is not None
    cu.clear_cache()
    real = time.time()
    monkeypatch.setattr(cu, "_now", lambda: real + cu.CHANGE_WINDOW_S + 5)
    _creds(tmp_path, monkeypatch, token=DUMMY_TOKEN + "-B", expires_in_s=cu.CHANGE_WINDOW_S + 3600)
    # same token again: served from the per-token cache, and the flag has aged out without any profile call
    monkeypatch.setattr(cu._OPENER, "open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no profile call expected")) if a[0].full_url.endswith("/profile") else _Resp(SAMPLE_API))
    assert cu.get_usage()["account"]["changed"] is None


@pytest.mark.parametrize("failure", [
    urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b"")),
    urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(b"")),
    urllib.error.URLError("dns"),
])
def test_transient_usage_failure_serves_last_good_numbers_marked_stale(tmp_path, monkeypatch, failure):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", _route())
    good = cu.get_usage()
    # expire only the result cache, keep the last-good numbers
    with cu._cache_lock:
        cu._cache["at"] = None
        cu._cache["value"] = None
    monkeypatch.setattr(cu._OPENER, "open", _route(usage=failure))
    out = cu.get_usage()
    assert out["available"] is True
    assert out["five_hour"] == good["five_hour"] and out["seven_day"] == good["seven_day"]
    assert out["stale"]["since"] == good["fetched_at"]
    assert out["stale"]["reason"] in ("http_429", "http_503", "network")
    assert out["account"]["email"] == "russell@example.com"          # the account is still the live read


def test_stale_numbers_are_not_served_forever_or_for_hard_failures(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", _route())
    cu.get_usage()
    with cu._cache_lock:
        cu._cache["at"] = None
        cu._cache["value"] = None
    real = time.time()
    monkeypatch.setattr(cu, "_now", lambda: real + cu.STALE_MAX_S + 5)
    _creds(tmp_path, monkeypatch, expires_in_s=cu.STALE_MAX_S + 3600)
    monkeypatch.setattr(cu._OPENER, "open", _route(usage=urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b""))))
    out = cu.get_usage()
    assert out["available"] is False and out["reason"] == "http_429"      # too old: honest n/a
    cu.clear_cache()
    monkeypatch.setattr(cu, "_now", time.time)
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", _route())
    cu.get_usage()
    with cu._cache_lock:
        cu._cache["at"] = None
        cu._cache["value"] = None
    monkeypatch.setattr(cu._OPENER, "open", _route(usage=urllib.error.HTTPError("u", 403, "x", {}, io.BytesIO(b""))))
    assert cu.get_usage()["available"] is False                          # a 403 is not transient: no stand-in


def test_after_a_429_the_next_ask_waits_longer(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(cu._OPENER, "open", _route(usage=urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b"")), seen=calls))
    cu.get_usage()
    n = len(calls)
    real = time.time()
    monkeypatch.setattr(cu, "_now", lambda: real + cu.USAGE_CACHE_TTL + 5)       # past the normal 60 s
    cu.get_usage()
    assert len(calls) == n                                                        # still cached: rate-limit window
    monkeypatch.setattr(cu, "_now", lambda: real + cu.RATE_LIMIT_TTL + 5)
    _creds(tmp_path, monkeypatch, expires_in_s=cu.RATE_LIMIT_TTL + 3600)
    cu.get_usage()
    assert len(calls) > n
