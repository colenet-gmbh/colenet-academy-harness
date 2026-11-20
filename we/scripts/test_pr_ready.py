"""pr_ready.sh against a fake `gh` that answers PR reads and logs every call.

Red arm of the ready path: a body edit ran before the conflict check, so a refused ready left a
draft without its `**Draft:**` line, which the draft hook then refused to put back.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SCRIPT = HERE / "pr_ready.sh"
HEAD = "a" * 40

pytestmark = pytest.mark.skipif(not shutil.which("jq"), reason="the fake gh delegates --jq to jq")

FAKE = r"""
import json, os, subprocess, sys
d = os.environ["FAKE_DIR"]
args = sys.argv[1:]
with open(os.path.join(d, "calls.log"), "a") as f:
    f.write(json.dumps(args) + "\n")
pr = json.load(open(os.path.join(d, "pr.json")))
if args[:2] == ["pr", "view"]:
    jq = args[args.index("--jq") + 1]
    sys.exit(subprocess.run(["jq", "-r", jq], input=json.dumps(pr), text=True).returncode)
if args[:2] == ["pr", "edit"]:
    src = args[args.index("--body-file") + 1]
    open(os.path.join(d, "body.out"), "w").write(open(src).read())
sys.exit(0)
"""


def run(tmp_path, pr, *args):
    (tmp_path / "pr.json").write_text(json.dumps(pr))
    gh = tmp_path / "gh"
    gh.write_text(f'#!/bin/sh\nexec {sys.executable} {tmp_path / "fake.py"} "$@"\n')
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "fake.py").write_text(FAKE)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FAKE_DIR": str(tmp_path),
        "PR_READY_WAIT": "0",
    }
    res = subprocess.run(
        ["bash", str(SCRIPT), "7", *args, "-R", "o/r"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    log = tmp_path / "calls.log"
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    body = (tmp_path / "body.out").read_text() if (tmp_path / "body.out").exists() else None
    return res.returncode, res.stdout.strip(), calls, body


def pr(**kw):
    return {
        "state": "OPEN",
        "isDraft": True,
        "mergeable": "MERGEABLE",
        "headRefOid": HEAD,
        "body": "**Draft:** waits for the API — ready when #6 merged\n\nTicket: X-1\n\nWhy.",
        **kw,
    }


def verbs(calls):
    return [
        c[:2] + (["--undo"] if "--undo" in c else []) for c in calls if c[:2] != ["pr", "view"]
    ]


def test_ready_drops_the_draft_line_then_readies(tmp_path):
    code, out, calls, body = run(tmp_path, pr(), "ready", "--sha", HEAD[:12])
    assert (code, out) == (0, f"ready 7 {HEAD}")
    assert body == "Ticket: X-1\n\nWhy.\n"
    assert verbs(calls) == [["pr", "edit"], ["pr", "ready"]]


@pytest.mark.parametrize(
    "patch, args, code, line",
    [
        ({"mergeable": "CONFLICTING"}, (), 12, "refused: 7 conflicts with its base — merge it"),
        ({}, ("--sha", "b" * 12), 3, f"refused: head is {HEAD}, not {'b' * 12}"),
        ({"state": "MERGED"}, (), 8, "refused: 7 is MERGED"),
    ],
    ids=["conflict", "moved-head", "merged"],
)
def test_a_refused_ready_never_touches_the_body(tmp_path, patch, args, code, line):
    got, out, calls, body = run(tmp_path, pr(**patch), "ready", *args)
    assert (got, out) == (code, line)
    assert body is None and verbs(calls) == []


def test_undo_puts_the_reason_first_and_replaces_an_old_line(tmp_path):
    ready = pr(isDraft=False)
    code, out, calls, body = run(tmp_path, ready, "undo", "open question — ready when answered")
    assert (code, out) == (0, "draft 7: open question — ready when answered")
    assert body == "**Draft:** open question — ready when answered\n\nTicket: X-1\n\nWhy.\n"
    assert verbs(calls) == [["pr", "edit"], ["pr", "ready", "--undo"]]


def test_undo_refuses_a_placeholder_reason_before_any_call(tmp_path):
    code, _out, calls, body = run(tmp_path, pr(isDraft=False), "undo", "<reason> — ready when x")
    assert code == 2 and body is None and calls == []


def test_nothing_to_do_changes_nothing(tmp_path):
    code, out, calls, _ = run(tmp_path, pr(isDraft=False), "ready")
    assert (code, out) == (0, "7 is already ready")
    assert verbs(calls) == []
