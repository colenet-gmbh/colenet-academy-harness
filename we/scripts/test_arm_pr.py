"""arm_pr.sh against a fake `gh` that replays PR reads and arm answers and logs every call.

No case may reach a merge: the log is checked for `gh pr merge` and for a merge mutation in every
test, whatever the scenario. `ARM_PR_UNDER_TEST` swaps the script for a red arm against a variant.
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
SCRIPT = Path(os.environ.get("ARM_PR_UNDER_TEST") or HERE / "arm_pr.sh")
OLD, NEW = "a" * 40, "b" * 40
REQUIRED = ["lint", "Claude Review"]
CLEAN = "GraphQL: Pull request Pull request is in clean status (enablePullRequestAutoMerge)"
MOVED = "GraphQL: Head sha didn't match expected head oid (enablePullRequestAutoMerge)"

pytestmark = pytest.mark.skipif(not shutil.which("jq"), reason="the fake gh delegates --jq to jq")

FAKE = r"""
import json, os, subprocess, sys
d = os.environ["FAKE_DIR"]
args = sys.argv[1:]
with open(os.path.join(d, "calls.log"), "a") as f:
    f.write(json.dumps(args) + "\n")
jq = args[args.index("--jq") + 1] if "--jq" in args else None
def out(obj):
    if jq is None:
        print(json.dumps(obj)); sys.exit(0)
    sys.exit(subprocess.run(["jq", "-r", jq], input=json.dumps(obj), text=True).returncode)
def pop(name):
    items = json.load(open(os.path.join(d, name + ".json")))
    n_path = os.path.join(d, name + ".n")
    n = int(open(n_path).read()) if os.path.exists(n_path) else 0
    open(n_path, "w").write(str(n + 1))
    return items[min(n, len(items) - 1)]
if args[:2] == ["repo", "view"]:
    print("o/r"); sys.exit(0)
if args[:2] == ["api", "graphql"]:
    query = next(a[6:] for a in args if a.startswith("query="))
    if query.lstrip().startswith("mutation"):
        answer = pop("arms")
        if answer == "ok":
            out({"data": {"enablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": {}}}}})
        sys.stderr.write(answer + "\n"); sys.exit(1)
    s = pop("reads")
    nodes = [{"__typename": "CheckRun", "name": n, "status": st, "conclusion": c, "isRequired": r}
             for n, st, c, r in s.get("checks", [])]
    out({"data": {"repository": {"pullRequest": {
        "id": "PR_1", "state": s.get("state", "OPEN"), "isDraft": s.get("draft", False),
        "headRefOid": s["head"], "baseRefName": "main",
        "mergeStateStatus": s.get("merge", "CLEAN"), "reviewDecision": s.get("review"),
        "commits": {"nodes": [{"commit": {"oid": s.get("commit", s["head"]),
            "statusCheckRollup": {"contexts": {"nodes": nodes}}}}]}}}}})
if args[0] == "api" and "/rules/branches/" in args[1]:
    out([{"type": "required_status_checks", "parameters": {"required_status_checks":
        [{"context": c} for c in json.load(open(os.path.join(d, "required.json")))]}}])
if args[0] == "api" and "/branches/" in args[1]:
    out({"name": "main", "protection": {"required_status_checks": {"contexts": []}}})
sys.exit(0)  # anything else, `gh pr merge` included, "succeeds": the log is the witness
"""


def check(name, status="COMPLETED", conclusion="SUCCESS", required=True):
    return [name, status, conclusion, required]


GREEN = [check("lint"), check("Claude Review")]
PENDING = [check("lint"), check("Claude Review", "IN_PROGRESS", None)]


def run(tmp_path, reads, arms, required=REQUIRED):
    for name, items in (("reads", reads), ("arms", arms), ("required", required)):
        (tmp_path / f"{name}.json").write_text(json.dumps(items))
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(f"#!{sys.executable}\n{FAKE}")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{gh.parent}:{os.environ['PATH']}",
        "FAKE_DIR": str(tmp_path),
        "ARM_INTERVAL": "0",
        "ARM_REGISTER_TIMEOUT": "2",
    }
    res = subprocess.run(
        ["bash", str(SCRIPT), "7", "--repo", "o/r"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,  # no git checkout: the one-way classifier stays silent
        timeout=30,
        check=False,
    )
    calls = [json.loads(line) for line in (tmp_path / "calls.log").read_text().splitlines()]
    merges = [c for c in calls if c[:2] == ["pr", "merge"] or "mergePullRequest" in " ".join(c)]
    assert merges == [], f"the helper merged: {merges}"
    return res.returncode, res.stdout.strip().splitlines(), calls


def arm_calls(calls):
    return [c for c in calls if any(a.startswith("query=mutation") for a in c)]


def test_a_blocked_pr_is_armed_on_the_head_it_read(tmp_path):
    code, out, calls = run(tmp_path, [{"head": NEW, "checks": PENDING}], ["ok"])
    assert (code, out) == (0, [f"armed: auto-merge on {NEW} (merge)", "pending: Claude Review"])
    (arm,) = arm_calls(calls)
    assert f"sha={NEW}" in arm and "method=MERGE" in arm


@pytest.mark.parametrize(
    ("later", "arms", "code", "line"),
    [
        # The trap: the old head was green, a push landed, no check run is registered on the new
        # head and GitHub answers "clean". Nothing registers before the timeout.
        ([], [CLEAN], 8, f"no checks on {NEW} after 2s — not armed, run again"),
        # The head's checks register and a required one runs: the arm goes through now.
        (PENDING, [CLEAN, "ok"], 0, f"armed: auto-merge on {NEW} (merge)"),
        # A required check never registered is absent, not green, even with the rest passing.
        ([check("lint")], [CLEAN, CLEAN], 8, None),
        ([check("lint"), check("Claude Review", conclusion="FAILURE")], [CLEAN], 7, None),
        (GREEN, [CLEAN], 3, None),
        # Green checks, but a required review is missing: GitHub waits for it, so it is armed.
        (
            {"checks": GREEN, "merge": "BLOCKED", "review": "REVIEW_REQUIRED"},
            [CLEAN, "ok"],
            0,
            None,
        ),
        # A red check that is not required leaves the PR UNSTABLE, which GitHub merges.
        (
            {
                "checks": [*GREEN, check("Codex", conclusion="FAILURE", required=False)],
                "merge": "UNSTABLE",
            },
            [CLEAN],
            3,
            None,
        ),
    ],
    ids=[
        "no-checks",
        "then-pending",
        "required-absent",
        "required-red",
        "all-green",
        "review-missing",
        "unstable",
    ],
)
def test_a_clean_answer_never_merges(tmp_path, later, arms, code, line):
    first = {"head": NEW, "commit": OLD, "checks": GREEN}  # the rollup still shows the old head
    last = later if isinstance(later, dict) else {"checks": later}
    reads = [first, {"head": NEW}, {"head": NEW}, {"head": NEW, **last}]
    got, out, _ = run(tmp_path, reads, arms)
    assert got == code, out
    assert not any(o.startswith("ready") for o in out) or code == 3
    if line:  # the verdict line; `armed` adds its `pending:` line after it
        assert [o for o in out if not o.startswith("pending: ")][-1] == line
    if code == 3:
        assert out == [
            f"ready: all required checks green on {NEW} — merge: gh pr merge 7 --merge --match-head-commit {NEW}"
        ]
    if code == 7:
        assert out == [f"red: Claude Review on {NEW} — not armed"]


@pytest.mark.parametrize(
    ("reads", "arms", "code", "line"),
    [
        (
            [{"head": NEW}, {"head": "c" * 40}],
            [MOVED],
            5,
            f"refused: head moved {NEW} -> {'c' * 40}",
        ),
        (
            [{"head": NEW}, {"head": "c" * 40}],
            [CLEAN],
            5,
            f"refused: head moved {NEW} -> {'c' * 40}",
        ),
        ([{"head": NEW, "draft": True}], ["ok"], 4, "refused: draft"),
    ],
    ids=["moved-on-arm", "moved-while-waiting", "draft"],
)
def test_refusals_never_arm(tmp_path, reads, arms, code, line):
    got, out, calls = run(tmp_path, reads, arms)
    assert (got, out[-1]) == (code, line)
    if code == 4:
        assert arm_calls(calls) == []
