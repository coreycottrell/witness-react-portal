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

DUMMY_TOKEN = "sk-ant-oat01-DUMMYTOKEN-must-never-leak"

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


@pytest.fixture(autouse=True)
def _fresh_cache():
    cu.clear_cache()
    yield
    cu.clear_cache()


def test_happy_path_returns_whitelisted_summary_only(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", lambda req, timeout=None: _Resp(SAMPLE_API))
    out = cu.get_usage()
    assert out["available"] is True
    assert out["five_hour"] == {"utilization": 48.0, "resets_at": "2026-10-28T16:59:59+00:00", "active": True}
    assert out["seven_day"]["utilization"] == 39.0
    assert out["plan"] == "max"
    assert set(out) == {"available", "five_hour", "seven_day", "plan", "fetched_at"}
    assert DUMMY_TOKEN not in json.dumps(out)


def test_token_only_in_authorization_header(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    seen = {}

    def fake_open(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        seen["url"] = req.full_url
        seen["beta"] = req.get_header("Anthropic-beta")
        return _Resp(SAMPLE_API)

    monkeypatch.setattr(cu._OPENER, "open", fake_open)
    cu.get_usage()
    assert seen["auth"] == "Bearer " + DUMMY_TOKEN
    assert seen["url"] == "https://api.anthropic.com/api/oauth/usage"
    assert seen["beta"] == "oauth-2025-04-20"


def test_no_credentials_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CREDENTIALS_PATH", str(tmp_path / "missing.json"))
    assert cu.get_usage() == {"available": False, "reason": "no_credentials"}


def test_expired_token_never_calls_network(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch, expires_in_s=-60)

    def boom(*a, **k):
        raise AssertionError("network must not be called with an expired token")

    monkeypatch.setattr(cu._OPENER, "open", boom)
    assert cu.get_usage() == {"available": False, "reason": "expired"}


def test_http_401_maps_to_expired_and_other_codes_to_http_n(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)

    def raiser(code):
        def _r(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b""))
        return _r

    monkeypatch.setattr(cu._OPENER, "open", raiser(401))
    assert cu.get_usage()["reason"] == "expired"
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
    assert cu.get_usage() == {"available": False, "reason": "network"}


@pytest.mark.parametrize("payload", [b"not json", b"[1,2]", {"five_hour": "x", "seven_day": None},
                                      {"seven_day": None}, {"five_hour": {"utilization": True}, "seven_day": None}])
def test_malformed_payloads_report_parse(tmp_path, monkeypatch, payload):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", lambda req, timeout=None: _Resp(payload))
    assert cu.get_usage() == {"available": False, "reason": "parse"}


def test_null_window_means_no_active_window(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    monkeypatch.setattr(cu._OPENER, "open", lambda req, timeout=None: _Resp({"five_hour": None, "seven_day": SAMPLE_API["seven_day"]}))
    out = cu.get_usage()
    assert out["available"] is True
    assert out["five_hour"] == {"utilization": 0, "resets_at": None, "active": False}


def test_result_is_cached_for_ttl(tmp_path, monkeypatch):
    _creds(tmp_path, monkeypatch)
    calls = []

    def counting(req, timeout=None):
        calls.append(1)
        return _Resp(SAMPLE_API)

    monkeypatch.setattr(cu._OPENER, "open", counting)
    cu.get_usage()
    cu.get_usage()
    assert len(calls) == 1


def test_token_never_logged(tmp_path, monkeypatch, caplog):
    _creds(tmp_path, monkeypatch, expires_in_s=-60)
    with caplog.at_level(logging.DEBUG):
        cu.get_usage()
    assert DUMMY_TOKEN not in caplog.text


def test_module_never_writes_credentials(tmp_path, monkeypatch):
    f = _creds(tmp_path, monkeypatch)
    before = f.read_bytes()
    monkeypatch.setattr(cu._OPENER, "open", lambda req, timeout=None: _Resp(SAMPLE_API))
    cu.get_usage()
    assert f.read_bytes() == before


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
    assert json.loads(resp.body) == {"available": False, "reason": "no_credentials"}


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
