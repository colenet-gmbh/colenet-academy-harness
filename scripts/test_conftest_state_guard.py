"""The root conftest.py keeps every test off the live state directory (the brake log)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "we" / "scripts"

LEAKY = f"""
import sys
sys.path.insert(0, {str(SCRIPTS)!r})
import worker_rights


def test_drops_its_state_dir(monkeypatch):
    monkeypatch.delenv("WE_STATE_DIR", raising=False)
    worker_rights.brake_log(None, {{"type": "deny", "name": "LEAK-1-x"}})


def test_keeps_its_state_dir(own_state_dir):
    worker_rights.brake_log(None, {{"type": "deny", "name": "OWN-1-x"}})
"""


def _inner_run(tmp_path, body):
    """A pytest run under the root conftest, with a fake home standing in for the live one."""
    suite = tmp_path / "suite"
    suite.mkdir()
    shutil.copy(ROOT / "conftest.py", suite / "conftest.py")
    (suite / "test_inner.py").write_text(body)
    home = tmp_path / "home"
    env = {k: v for k, v in os.environ.items() if not k.startswith("WE_STATE")}
    env.update(HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(suite)],
        cwd=suite,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return out, home / ".claude" / "we-state" / "brakes.jsonl"


def test_a_test_writing_the_live_brake_log_turns_the_run_red(tmp_path):
    """Red arm: without the guard in `brake_log` the line lands in the live file and the run
    stays green."""
    out, live = _inner_run(tmp_path, LEAKY)
    assert not live.exists(), f"a test wrote the live brake log: {live.read_text()}"
    assert out.returncode == 1, out.stdout
    assert "1 write(s) to the live state directory refused" in out.stdout
    assert "LEAK-1-x" in out.stdout and "OWN-1-x" not in out.stdout


def test_a_test_with_its_own_state_dir_stays_green(tmp_path):
    clean = LEAKY.replace("def test_drops_its_state_dir", "def _not_collected")
    out, live = _inner_run(tmp_path, clean)
    assert out.returncode == 0, out.stdout
    assert not live.exists()
