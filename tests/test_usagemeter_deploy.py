"""Guard for deploy/apply-usagemeter.sh + rollback-usagemeter.sh (file level, SKIP_RESTART=1, scratch dirs only).
Proves: apply swaps source+dist and records a backup; rollback restores BYTE-IDENTICAL state; apply refuses a
live source that is not the expected base. (Restart mechanics were exercised separately with a dummy server.)"""
import hashlib, os, subprocess
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def _sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _tree(root):
    return {str(p.relative_to(root)): _sha(p) for p in sorted(Path(root).rglob("*"))
            if p.is_file() and "backups" not in p.parts and not p.name.startswith(".usagemeter")
            and not p.parent.name.startswith("dist.rolledback")
            and "dist.rolledback-usagemeter" not in str(p)}


def _mk(tmp):
    wp, src = tmp / "wp", tmp / "src"
    (wp / "react-portal/dist/assets").mkdir(parents=True); (wp / "backups").mkdir()
    (wp / "portal_server.py").write_text("# base\n")
    (wp / "react-portal/dist/index.html").write_text('<script src="/react/assets/index-OLD.js"></script>')
    (wp / "react-portal/dist/assets/index-OLD.js").write_text("old")
    (src / "react-portal/dist/assets").mkdir(parents=True)
    (src / "portal_server.py").write_text("# new\n"); (src / "claude_usage.py").write_text("# usage\n")
    (src / "react-portal/dist/index.html").write_text('<script src="/react/assets/index-NEW.js"></script>')
    (src / "react-portal/dist/assets/index-NEW.js").write_text("new")
    return wp, src


def _run(script, wp, src, base_sha):
    env = dict(os.environ, WP_DIR=str(wp), SRC_DIR=str(src), SKIP_RESTART="1", BASE_SHA=base_sha)
    return subprocess.run([str(DEPLOY / script)], env=env, capture_output=True, text=True)


def test_apply_then_rollback_is_byte_identical(tmp_path):
    wp, src = _mk(tmp_path)
    before = _tree(wp)
    r = _run("apply-usagemeter.sh", wp, src, _sha(wp / "portal_server.py"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (wp / "claude_usage.py").exists() and (wp / "portal_server.py").read_text() == "# new\n"
    assert "index-NEW.js" in (wp / "react-portal/dist/index.html").read_text()
    assert (wp / "react-portal/dist/assets/index-OLD.js").exists()      # old chunks kept for open tabs
    r = _run("rollback-usagemeter.sh", wp, src, "x")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (wp / "claude_usage.py").exists()
    assert _tree(wp) == before


def test_apply_refuses_unexpected_live_source(tmp_path):
    wp, src = _mk(tmp_path)
    r = _run("apply-usagemeter.sh", wp, src, "0" * 64)
    assert r.returncode != 0 and "not the expected base" in r.stdout
    assert (wp / "portal_server.py").read_text() == "# base\n"
