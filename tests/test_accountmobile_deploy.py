"""Guard for deploy/apply-account-mobile.sh + rollback-account-mobile.sh (window 2 of the usage meter:
signed-in account display + phone layout). File level, SKIP_RESTART=1, scratch dirs only.
Proves: apply swaps claude_usage.py + dist (keeping old hashed chunks for open tabs) and records a backup;
rollback restores BYTE-IDENTICAL state and removes the account-change state file; apply refuses a live
claude_usage.py that is not the first-window version, and refuses when the first window was never applied.
(The restart/auto-rollback mechanics are the same lib as window 1 and were exercised with a dummy server.)"""
import hashlib, os, subprocess
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def _sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _tree(root):
    return {str(p.relative_to(root)): _sha(p) for p in sorted(Path(root).rglob("*"))
            if p.is_file() and "backups" not in p.parts and not p.name.startswith(".usagemeter")
            and "dist.rolledback" not in str(p)}


def _mk(tmp, with_usage=True):
    wp, src = tmp / "wp", tmp / "src"
    (wp / "react-portal/dist/assets").mkdir(parents=True); (wp / "backups").mkdir()
    (wp / "portal_server.py").write_text("# server\n")
    if with_usage:
        (wp / "claude_usage.py").write_text("# window-1 usage\n")
    (wp / "react-portal/dist/index.html").write_text('<script src="/react/assets/index-W1.js"></script>')
    (wp / "react-portal/dist/assets/index-W1.js").write_text("w1")
    (src / "react-portal/dist/assets").mkdir(parents=True)
    (src / "claude_usage.py").write_text("# window-2 usage\n")
    (src / "react-portal/dist/index.html").write_text('<script src="/react/assets/index-W2.js"></script>')
    (src / "react-portal/dist/assets/index-W2.js").write_text("w2")
    return wp, src


def _run(script, wp, src, prev_sha):
    env = dict(os.environ, WP_DIR=str(wp), SRC_DIR=str(src), SKIP_RESTART="1", PREV_SHA=prev_sha)
    return subprocess.run([str(DEPLOY / script)], env=env, capture_output=True, text=True)


def test_apply_then_rollback_is_byte_identical(tmp_path):
    wp, src = _mk(tmp_path)
    before = _tree(wp)
    r = _run("apply-account-mobile.sh", wp, src, _sha(wp / "claude_usage.py"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (wp / "claude_usage.py").read_text() == "# window-2 usage\n"
    assert (wp / "portal_server.py").read_text() == "# server\n"                       # backend entry point untouched
    assert "index-W2.js" in (wp / "react-portal/dist/index.html").read_text()
    assert (wp / "react-portal/dist/assets/index-W1.js").exists()                      # old chunks kept for open tabs
    (wp / ".claude-account-last.json").write_text("{}")                                # state the new module would create
    r = _run("rollback-account-mobile.sh", wp, src, "x")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (wp / ".claude-account-last.json").exists()
    assert _tree(wp) == before


def test_apply_refuses_unexpected_live_usage_module(tmp_path):
    wp, src = _mk(tmp_path)
    r = _run("apply-account-mobile.sh", wp, src, "0" * 64)
    assert r.returncode != 0 and "not the expected first-window version" in r.stdout
    assert (wp / "claude_usage.py").read_text() == "# window-1 usage\n"
    assert "index-W1.js" in (wp / "react-portal/dist/index.html").read_text()


def test_apply_refuses_when_window_one_was_never_applied(tmp_path):
    wp, src = _mk(tmp_path, with_usage=False)
    r = _run("apply-account-mobile.sh", wp, src, "0" * 64)
    assert r.returncode != 0 and "first meter window was not applied" in r.stdout
