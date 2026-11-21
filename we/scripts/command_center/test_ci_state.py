"""ci_state.py: the main verdict through a fake command, running checks, locks, the crew key."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import ci_state  # noqa: E402
import crew as crew_mod  # noqa: E402
import gh_source  # noqa: E402
import zentrale  # noqa: E402
from test_open_ends import UNTIL, at, ctx, fake_runner, pr  # noqa: E402

MINIMAL = {
    "version": 2,
    "roles": {"coordinator": "coord"},
    "members": {"coord": {"name": "Coord"}},
}

FAKE_MAIN = """#!/usr/bin/env python3
import os, sys, time
d = os.path.dirname(os.path.abspath(__file__))
n = int(open(os.path.join(d, "n")).read()) if os.path.exists(os.path.join(d, "n")) else 0
open(os.path.join(d, "n"), "w").write(str(n + 1))
if n == 0:
    print("red: run 123"); sys.exit(1)
if n == 1:
    print("green"); sys.exit(0)
time.sleep(5)
"""


# --- AC 3 ------------------------------------------------------------------------------------


def test_main_state(tmp_path, monkeypatch):
    script = tmp_path / "bin" / "main_verdict"
    script.parent.mkdir()
    script.write_text(FAKE_MAIN)
    script.chmod(0o755)
    cache = tmp_path / "cache"
    monkeypatch.setattr(ci_state, "MAIN_TIMEOUT_S", 1)
    first = ci_state.main_state(str(script), UNTIL, cache)
    assert first["state"] == "red" and first["line"] == "main: red: run 123"
    # inside the TTL the cached answer stands: the command is not asked again
    assert ci_state.main_state(str(script), UNTIL + timedelta(minutes=1), cache) == first
    later = UNTIL + timedelta(minutes=6)
    assert ci_state.main_state(str(script), later, cache)["state"] == "green"
    hung = ci_state.main_state(str(script), later + timedelta(minutes=6), cache)
    assert hung == {
        "state": "unknown",
        "line": "main state: unknown (timeout)",
        "fetched_at": hung["fetched_at"],
    }
    assert ci_state.main_state(None, UNTIL, cache)["line"] == "main state: not read (no source)"
    # the same command run in another repo is its own cache entry, not the first one's verdict
    assert ci_state.main_state(str(script), later, cache, cwd=tmp_path)["state"] != "green"


def test_main_in_red_section_and_default_head_apart(tmp_path, monkeypatch):
    script = tmp_path / "main_verdict"
    script.write_text(FAKE_MAIN)
    script.chmod(0o755)
    crew_dir = tmp_path / "repo" / ".weside"
    crew_dir.mkdir(parents=True)
    crew = {**MINIMAL, "flow": {"main_state_command": str(script)}}
    (crew_dir / "crew.json").write_text(json.dumps(crew))
    monkeypatch.setenv("WE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(gh_source, "runner", lambda *_a: fake_runner([], default_state="FAILURE"))
    doc = zentrale.build(
        until=UNTIL,
        cwd=tmp_path / "repo",
        cache=tmp_path / "c",
        agents=[],
        repos=["acme/app"],
        gh_reader=lambda *_a: [],
        cswap=str(tmp_path / "no-cswap"),
    )
    assert [r["title"] for r in doc["sections"]["red"]][:1] == ["main: red: run 123"]
    [repo] = doc["sections"]["ci"]["repos"]
    assert repo["default_head"] == "checks on default head: failure"


# --- AC 6 ------------------------------------------------------------------------------------


def sample(tmp_path, minutes_old, **locks):
    log = tmp_path / "flow"
    log.mkdir(exist_ok=True)
    line = {"ts": at(minutes_old), "type": "sample", "locks": locks}
    (log / f"sampler-{UNTIL:%Y-%m-%d}.jsonl").write_text(
        json.dumps({"ts": at(1), "type": "heartbeat"})
        + "\n"
        + json.dumps(line)
        + "\n"
        + json.dumps({"ts": at(0.5), "type": "heartbeat"})
        + "\n"
    )
    return log


def test_runs_and_locks(tmp_path):
    prs = [
        pr(40, [ctx("test", status="IN_PROGRESS", conclusion=None, started=at(12))]),
        pr(41, [ctx("lint", status="QUEUED", conclusion=None, created=at(3))]),
        pr(42, [ctx("test", done=at(5))]),
    ]
    doc = gh_source.read_repo(fake_runner(prs), "acme/app", lambda *_a: ["test"])
    got = ci_state.running(doc, UNTIL)
    assert [(c["pr"], c["name"], c["status"], c["age_s"]) for c in got["checks"]] == [
        (40, "test", "IN_PROGRESS", 12 * 60),
        (41, "lint", "QUEUED", 3 * 60),
    ]
    log = sample(
        tmp_path,
        4,
        gates={"held": True, "holder": "alpha-7"},
        tests={"held": False, "holder": None},
    )
    locks = ci_state.locks(log, "UTC", 15, UNTIL)
    assert locks["line"] == "gate lock held by alpha-7 · test slot free · sample 4 min old"
    log = sample(tmp_path, 20, gates={"held": True, "holder": "alpha-7"})
    stale = ci_state.locks(log, "UTC", 15, UNTIL)
    assert stale["line"] == "sampler stale (20 min)" and stale["gates"] is None


# --- AC 14 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flow", "ok"),
    [({"main_state_command": "scripts/ci/verdict"}, True), ({"main_state_cmd": "x"}, False)],
)
def test_main_state_key_strict(tmp_path, flow, ok):
    crew_dir = tmp_path / ".weside"
    crew_dir.mkdir()
    (crew_dir / "crew.json").write_text(json.dumps({**MINIMAL, "flow": flow}))
    out = subprocess.run(
        [
            sys.executable,
            str(HERE.parent / "crew.py"),
            "validate",
            "--strict",
            "--from",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (out.returncode == 0) is ok, out.stderr
    if not ok:
        assert "flow.main_state_cmd" in out.stderr


def test_crew_without_the_key_reads_none():
    assert crew_mod.DEFAULTS["flow"]["main_state_command"] is None
