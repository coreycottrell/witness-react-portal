"""Guard for the by-effect delivery confirm (ticket 3389) in portal_server.py.

A portal message is only DELIVERED when Claude Code's own record (transcript or
prompt history) shows it, never because a tmux send "worked". These tests pin
that behaviour: positive proof, and the negatives that must NOT count as proof.

Run:  python3 -m pytest tests/test_delivery_confirm.py -q
Hermetic: no tmux, no live logs, no real ~/.claude, all paths monkeypatched.
"""
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import portal_server as ps  # noqa: E402


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_log(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate every file the delivery path touches."""
    log = tmp_path / "sess.jsonl"
    log.write_text("")
    monkeypatch.setattr(ps, "_primary_log_paths", lambda: [log])
    monkeypatch.setattr(ps, "HISTORY_FILE", tmp_path / "history.jsonl")
    monkeypatch.setattr(ps, "PENDING_DELIVERY_LOG", tmp_path / "pending.jsonl")
    monkeypatch.setattr(ps, "DELIVERY_JOURNAL", tmp_path / "journal.jsonl")
    return log, tmp_path


TAGGED = "[Russell] [portal] please check the deploy status now"
NEEDLE = ps._dnorm("please check the deploy status now")[-24:]
TAG = ps._tag_of(TAGGED)


def test_tag_of_extracts_normalized_tag():
    assert TAG == "[russell] [portal]"
    assert ps._tag_of("no tag here") == ""


def test_empty_needle_is_never_proof(env):
    assert ps._submit_recorded(TAG, "", time.time() - 5) is None


def test_user_entry_in_transcript_is_proof(env):
    log, _ = env
    since = time.time() - 5
    _write_log(log, [{"type": "user", "timestamp": _iso(time.time()),
                      "message": {"content": TAGGED}}])
    where = ps._submit_recorded(TAG, NEEDLE, since)
    assert where and where.startswith("transcript:") and ":user:" in where


def test_queue_operation_entry_is_proof(env):
    log, _ = env
    since = time.time() - 5
    _write_log(log, [{"type": "queue-operation", "timestamp": _iso(time.time()),
                      "content": TAGGED}])
    assert ps._submit_recorded(TAG, NEEDLE, since)


def test_list_content_blocks_are_proof(env):
    log, _ = env
    since = time.time() - 5
    _write_log(log, [{"type": "user", "timestamp": _iso(time.time()),
                      "message": {"content": [{"type": "text", "text": TAGGED}]}}])
    assert ps._submit_recorded(TAG, NEEDLE, since)


def test_assistant_echo_is_not_proof(env):
    """Primary quoting the text back must not mark the operator's message delivered."""
    log, _ = env
    since = time.time() - 5
    _write_log(log, [{"type": "assistant", "timestamp": _iso(time.time()),
                      "message": {"content": TAGGED}}])
    assert ps._submit_recorded(TAG, NEEDLE, since) is None


def test_entry_older_than_send_is_not_proof(env):
    """An identical earlier message (before this send) must not confirm a new one."""
    log, _ = env
    since = time.time() - 5
    _write_log(log, [{"type": "user", "timestamp": _iso(since - 600),
                      "message": {"content": TAGGED}}])
    assert ps._submit_recorded(TAG, NEEDLE, since) is None


def test_wrong_operator_tag_is_not_proof(env):
    log, _ = env
    since = time.time() - 5
    other = "[Corey] [portal] please check the deploy status now"
    _write_log(log, [{"type": "user", "timestamp": _iso(time.time()),
                      "message": {"content": other}}])
    assert ps._submit_recorded(TAG, NEEDLE, since) is None


def test_unrelated_text_is_not_proof(env):
    log, _ = env
    _write_log(log, [{"type": "user", "timestamp": _iso(time.time()),
                      "message": {"content": "[Russell] [portal] something else"}}])
    assert ps._submit_recorded(TAG, NEEDLE, time.time() - 5) is None


def test_prompt_history_fallback_is_proof(env):
    _, tmp = env
    now = time.time()
    (tmp / "history.jsonl").write_text(json.dumps(
        {"timestamp": int(now * 1000), "display": TAGGED}) + "\n")
    where = ps._submit_recorded(TAG, NEEDLE, now - 5)
    assert where and where.startswith("history:")


def test_nothing_recorded_is_none(env):
    assert ps._submit_recorded(TAG, NEEDLE, time.time() - 5) is None


# --- pending queue ---------------------------------------------------------

def test_queue_pending_roundtrip_and_mark_delivered(env):
    ps._queue_pending("m1", TAGGED, NEEDLE)
    ps._queue_pending("m2", "[Russell] [portal] other", "other")
    row = ps._get_pending("m1")
    assert row["needle"] == NEEDLE and row["tagged"] == TAGGED and row["resends"] == 0
    ps._update_pending("m1", resends=2)
    assert ps._get_pending("m1")["resends"] == 2
    ps._mark_delivered("m1")
    assert ps._get_pending("m1") is None
    assert ps._get_pending("m2") is not None      # only the confirmed row is cleared


def test_queue_writes_journal_event(env):
    _, tmp = env
    ps._queue_pending("m9", TAGGED, NEEDLE)
    events = [json.loads(l) for l in (tmp / "journal.jsonl").read_text().splitlines()]
    assert events[-1]["event"] == "queued" and events[-1]["id"] == "m9"


# --- the whole confirm loop -------------------------------------------------

def _no_sleep(monkeypatch):
    async def fast(_s):
        return None
    monkeypatch.setattr(ps.asyncio, "sleep", fast)


def test_deliver_and_confirm_marks_delivered_on_transcript_proof(env, monkeypatch):
    log, tmp = env
    _no_sleep(monkeypatch)
    ps._queue_pending("m3", TAGGED, NEEDLE)
    _write_log(log, [{"type": "user", "timestamp": _iso(time.time() + 1),
                      "message": {"content": TAGGED}}])
    ok = asyncio.run(ps._deliver_and_confirm(TAGGED, NEEDLE, "m3", initial_send=False, deadline=5))
    assert ok is True
    assert ps._get_pending("m3") is None
    events = [json.loads(l) for l in (tmp / "journal.jsonl").read_text().splitlines()]
    assert any(e["event"] == "delivered" and e["id"] == "m3" and e["proof"].startswith("transcript:")
               for e in events)


def test_deliver_and_confirm_without_proof_stays_queued(env, monkeypatch):
    """No record in the transcript + no pane to send to: must NOT report delivered."""
    _, tmp = env
    _no_sleep(monkeypatch)
    monkeypatch.setattr(ps, "_find_primary_pane", lambda *a, **k: None)
    ps._queue_pending("m4", TAGGED, NEEDLE)
    ok = asyncio.run(ps._deliver_and_confirm(TAGGED, NEEDLE, "m4", initial_send=False, deadline=0.05))
    assert ok is False
    assert ps._get_pending("m4") is not None       # still queued for the sweep
    events = [json.loads(l) for l in (tmp / "journal.jsonl").read_text().splitlines()]
    assert events[-1]["event"] == "unconfirmed_at_deadline_kept_queued"
