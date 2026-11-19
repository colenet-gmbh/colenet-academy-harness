"""worker_report_gate.py as Claude Code runs it: a subprocess with a synthetic transcript."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent / "worker_report_gate.py"


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _repo(tmp_path, lead="LEAD-1"):
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q")
    if lead:
        marker = {"name": "w1", "lead": lead, "session_id": "s"}
        (wt / ".git" / "we-worker.json").write_text(json.dumps(marker))
    return wt


def _prompt(text):
    return {"type": "user", "message": {"content": text}}


def _send(to):
    block = {"type": "tool_use", "id": "u1", "name": "SendMessage", "input": {"to": to}}
    return {"type": "assistant", "message": {"content": [block]}}


def _result():
    block = {"type": "tool_result", "tool_use_id": "u1", "content": "ok"}
    return {"type": "user", "message": {"content": [block]}}


def _say(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _run(tmp_path, wt, entries, **extra):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("".join(json.dumps(e) + "\n" for e in entries))
    payload = {"session_id": "s", "cwd": str(wt), "transcript_path": str(transcript), **extra}
    out = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout) if out.stdout.strip() else None


def test_turn_without_report_blocks(tmp_path):
    wt = _repo(tmp_path)
    out = _run(tmp_path, wt, [_prompt("brief"), _say("branch: x · commits: 2")])
    assert out["decision"] == "block" and "SendMessage to `LEAD-1`" in out["reason"]


def test_report_sent_to_the_lead_passes(tmp_path):
    wt = _repo(tmp_path)
    assert _run(tmp_path, wt, [_prompt("brief"), _send("LEAD-1"), _result(), _say("sent")]) is None
    assert _run(tmp_path, wt, [_prompt("brief"), _send("LEAD-1 [3fa9c1]"), _result()]) is None


def test_report_to_someone_else_or_in_an_earlier_turn_blocks(tmp_path):
    wt = _repo(tmp_path)
    assert _run(tmp_path, wt, [_prompt("brief"), _send("other"), _result()])["decision"] == "block"
    entries = [_prompt("brief"), _send("LEAD-1"), _result(), _prompt("steer: fix X"), _say("done")]
    assert _run(tmp_path, wt, entries)["decision"] == "block"


def test_no_marker_stop_hook_active_and_subagent_pass(tmp_path):
    entries = [_prompt("brief"), _say("done")]
    assert _run(tmp_path, _repo(tmp_path, lead=None), entries) is None
    wt = tmp_path / "wt"
    marker = {"name": "w1", "lead": "LEAD-1", "session_id": "s"}
    (wt / ".git" / "we-worker.json").write_text(json.dumps(marker))
    assert _run(tmp_path, wt, entries, stop_hook_active=True) is None
    assert _run(tmp_path, wt, entries, agent_id="a1") is None


def test_the_lead_working_in_the_worker_tree_passes(tmp_path):
    wt = _repo(tmp_path)
    assert _run(tmp_path, wt, [_prompt("fix it"), _say("done")], session_id="lead") is None


def test_outside_a_repo_passes(tmp_path):
    assert _run(tmp_path, tmp_path, [_prompt("brief")]) is None


# ---------------------------------------------------------------- the end line (PR owner: worker)

OWNER_BRIEF = '## Goal\nRun Skill("we:develop") for ABC-1.\n## Output\nPR owner: worker\n'


def _report(text):
    block = {
        "type": "tool_use",
        "id": "u1",
        "name": "SendMessage",
        "input": {"to": "LEAD-1", "message": text},
    }
    return {"type": "assistant", "message": {"content": [block]}}


@pytest.mark.parametrize(
    "last",
    [
        "armed: #41 on 3fa9c1d2e4",
        "held: #41 (one-way money)",
        "parked: feat/ABC-1-x (time limit; resume: we-worker ABC-1 --resume ABC-1)",
    ],
)
def test_a_pr_owner_report_with_an_end_line_passes(tmp_path, last):
    wt = _repo(tmp_path)
    entries = [_prompt(OWNER_BRIEF), _report(f"branch: x · commits: 2\n{last}\n"), _result()]
    assert _run(tmp_path, wt, entries) is None


@pytest.mark.parametrize("last", ["done: #41 merged", "armed: #41", "held: #41"])
def test_a_pr_owner_report_without_an_end_line_blocks(tmp_path, last):
    wt = _repo(tmp_path)
    entries = [_prompt(OWNER_BRIEF), _report(f"branch: x\n{last}"), _result()]
    out = _run(tmp_path, wt, entries)
    assert out["decision"] == "block" and "armed: <PR> on <sha>" in out["reason"]


def test_without_the_brief_line_any_report_passes(tmp_path):
    wt = _repo(tmp_path)
    entries = [_prompt("## Goal\nRun it.\n"), _report("branch: x\nblockers: none"), _result()]
    assert _run(tmp_path, wt, entries) is None


def test_the_brief_file_of_a_template_launch_carries_the_line(tmp_path):
    wt = _repo(tmp_path)
    brief = wt / ".git" / "we-brief.md"
    brief.write_text(f"Lead: LEAD-1 — send your report.\n\n{OWNER_BRIEF}")
    entries = [_prompt(f"Read {brief} and follow it."), _report("blockers: none"), _result()]
    assert _run(tmp_path, wt, entries)["decision"] == "block"
