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
if os.environ.get("GH_NOTICE"):
    sys.stderr.write(os.environ["GH_NOTICE"] + "\n")
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
        "labels": {"nodes": [{"name": n} for n in s.get("labels", [])]},
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


def run(tmp_path, reads, arms, required=REQUIRED, merge=None, extra=(), env_extra=None):
    if merge is not None:  # a crew with a `merge` block; `.git` bounds the crew lookup at tmp_path
        (tmp_path / ".git").mkdir()
        (tmp_path / ".weside").mkdir()
        crew = {"version": 2, "roles": {}, "members": {}, "merge": merge}
        (tmp_path / ".weside" / "crew.json").write_text(json.dumps(crew))
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
        **(env_extra or {}),
    }
    res = subprocess.run(
        ["bash", str(SCRIPT), "7", "--repo", "o/r", *extra],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,  # no git checkout: the one-way classifier stays silent
        timeout=30,
        check=False,
    )
    log = tmp_path / "calls.log"
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
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


@pytest.mark.parametrize(
    "merge, labels, code, line",
    [
        (
            {"hold_labels": ["review"]},
            ["review"],
            4,
            "refused: label review — only the human merges it",
        ),
        (
            {"hold_labels": ["review"]},
            ["docs", "review"],
            4,
            "refused: label review — only the human merges it",
        ),
        ({"armer": "workflow"}, [], 4, "refused: armed by workflow — the step is gh pr ready"),
    ],
    ids=["hold-label", "hold-label-among-others", "workflow-armer"],
)
def test_crew_merge_block_refuses_before_any_arm(tmp_path, merge, labels, code, line):
    got, out, calls = run(
        tmp_path, [{"head": NEW, "checks": PENDING, "labels": labels}], ["ok"], merge=merge
    )
    assert (got, out[-1]) == (code, line)
    assert arm_calls(calls) == []


def test_a_label_outside_hold_labels_still_arms(tmp_path):
    reads = [{"head": NEW, "checks": PENDING, "labels": ["docs"]}]
    code, out, calls = run(tmp_path, reads, ["ok"], merge={"hold_labels": ["review"]})
    assert (code, out[0]) == (0, f"armed: auto-merge on {NEW} (merge)")
    assert len(arm_calls(calls)) == 1


def test_a_broken_crew_merge_block_is_a_crew_error_not_a_gh_failure(tmp_path):
    code, out, calls = run(
        tmp_path, [{"head": NEW, "checks": PENDING}], ["ok"], merge={"armer": "Workflow"}
    )
    assert code == 2 and out[-1].startswith("crew: ") and "merge.armer must be one of" in out[-1]
    assert arm_calls(calls) == []


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def base_crew(tmp_path, base_merge, head_merge):
    """A git checkout whose origin/main holds a crew with `base_merge` (None: no crew file) and
    whose working tree, the PR's own, holds a crew with `head_merge`."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    crew_file = tmp_path / ".weside" / "crew.json"
    crew_file.parent.mkdir()
    if base_merge is not None:
        crew_file.write_text(
            json.dumps({"version": 2, "roles": {}, "members": {}, "merge": base_merge})
        )
        _git(tmp_path, "add", ".weside")
    else:
        (tmp_path / "README").write_text("x")
        _git(tmp_path, "add", "README")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    crew_file.write_text(
        json.dumps({"version": 2, "roles": {}, "members": {}, "merge": head_merge})
    )


def test_a_pr_cannot_drop_the_hold_label_its_base_holds_it_with(tmp_path):
    base_crew(tmp_path, {"hold_labels": ["review"]}, {"hold_labels": []})
    reads = [{"head": NEW, "checks": PENDING, "labels": ["review"]}]
    code, out, calls = run(tmp_path, reads, ["ok"])
    assert (code, out[-1]) == (4, "refused: label review — only the human merges it")
    assert arm_calls(calls) == []


def test_a_base_without_a_crew_file_leaves_the_checkouts_block(tmp_path):
    base_crew(tmp_path, None, {"hold_labels": ["review"]})
    reads = [{"head": NEW, "checks": PENDING, "labels": ["review"]}]
    code, out, calls = run(tmp_path, reads, ["ok"])
    assert (code, out[-1]) == (4, "refused: label review — only the human merges it")
    assert arm_calls(calls) == []


def test_an_unreadable_base_is_a_crew_error_not_the_checkouts_block(tmp_path):
    base_crew(tmp_path, {"hold_labels": ["review"]}, {"hold_labels": []})
    _git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")
    reads = [{"head": NEW, "checks": PENDING, "labels": ["review"]}]
    code, out, calls = run(tmp_path, reads, ["ok"])
    assert (code, out[-1]) == (2, "crew: origin/main unreadable here (fetch it)")
    assert arm_calls(calls) == []


def checkout(tmp_path):
    """A git checkout whose HEAD is the PR head `--check` classifies (no risk-paths: no class)."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(
        tmp_path,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
    )
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.mark.parametrize(
    ("read", "code", "line"),
    [
        (
            {"checks": GREEN},
            0,
            "ready: all required checks green on {sha} — merge: gh pr merge 7 --merge --match-head-commit {sha}",
        ),
        ({"checks": PENDING}, 8, "pending: Claude Review on {sha} — not ready"),
        ({"checks": [check("lint")]}, 8, "pending: Claude Review on {sha} — not ready"),
        ({"checks": []}, 8, "no checks on {sha} — not ready"),
        (
            {"checks": GREEN, "merge": "BLOCKED", "review": "REVIEW_REQUIRED"},
            8,
            "not ready on {sha} (BLOCKED, review REVIEW_REQUIRED)",
        ),
        (
            {"checks": GREEN, "review": "CHANGES_REQUESTED"},
            8,
            "not ready on {sha} (CLEAN, review CHANGES_REQUESTED)",
        ),
        (
            {"checks": [check("lint"), check("Claude Review", conclusion="FAILURE")]},
            7,
            "red: Claude Review on {sha} — not ready",
        ),
    ],
    ids=[
        "ready",
        "pending",
        "required-absent",
        "no-checks",
        "review-required",
        "changes-requested",
        "red",
    ],
)
def test_check_reads_once_and_never_arms(tmp_path, read, code, line):
    sha = checkout(tmp_path)
    got, out, calls = run(
        tmp_path, [{"head": sha, **read}], ["ok"], extra=("--check", "--sha", sha)
    )
    assert (got, out) == (code, [line.replace("{sha}", sha)])
    assert arm_calls(calls) == []


NOTICE = "A new preview command is available: gh event listen\nHide this notice: gh config set notices false"


def test_notices_on_stderr_never_become_the_answer(tmp_path):
    sha = checkout(tmp_path)
    code, out, _ = run(
        tmp_path,
        [{"head": sha, "checks": GREEN}],
        ["ok"],
        extra=("--check", "--sha", sha),
        env_extra={"GH_NOTICE": NOTICE},
    )
    assert (code, out) == (
        0,
        [
            f"ready: all required checks green on {sha} — merge: gh pr merge 7 --merge --match-head-commit {sha}"
        ],
    )


def test_notices_on_stderr_still_arm(tmp_path):
    code, out, _ = run(
        tmp_path, [{"head": NEW, "checks": PENDING}], ["ok"], env_extra={"GH_NOTICE": NOTICE}
    )
    assert (code, out) == (0, [f"armed: auto-merge on {NEW} (merge)", "pending: Claude Review"])


def test_a_clean_answer_is_read_from_stderr_beside_a_notice(tmp_path):
    reads = [{"head": NEW, "checks": GREEN, "merge": "UNSTABLE"}]
    code, out, _ = run(tmp_path, reads, [CLEAN], env_extra={"GH_NOTICE": NOTICE})
    assert (code, out) == (
        3,
        [
            f"ready: all required checks green on {NEW} — merge: gh pr merge 7 --merge --match-head-commit {NEW}"
        ],
    )


def test_check_refuses_another_head(tmp_path):
    sha = checkout(tmp_path)
    reads = [{"head": NEW, "checks": GREEN}]
    got, out, calls = run(tmp_path, reads, ["ok"], extra=("--check", "--sha", sha))
    assert (got, out) == (5, [f"refused: head moved {sha} -> {NEW}"])
    assert arm_calls(calls) == []


def test_check_refuses_a_checkout_that_is_not_the_pr_head(tmp_path):
    checkout(tmp_path)
    got, out, calls = run(tmp_path, [{"head": NEW, "checks": GREEN}], ["ok"], extra=("--check",))
    assert (got, out) == (
        6,
        ["refused: one-way not classified (this checkout is not the PR head)"],
    )
    assert arm_calls(calls) == []


def real_crew_checkout(tmp_path):
    """A checkout whose origin/main holds a crew file the way a real repo carries one: a `merge`
    block next to `files.*` paths that exist beside it. The base's crew is read at a revision
    (`crew.py merge --rev`), so its paths have no working tree to be checked against."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "goals.md").write_text("# Goals\n")
    (tmp_path / "docs" / "dod.md").write_text("# Done\n")
    (tmp_path / ".weside").mkdir()
    crew = {
        "version": 2,
        "roles": {},
        "members": {},
        "files": {"goals": "docs/goals.md", "dod": "docs/dod.md"},
        "merge": {"armer": "session", "hold_labels": ["hold-me"]},
    }
    (tmp_path / ".weside" / "crew.json").write_text(json.dumps(crew))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.mark.parametrize(
    ("labels", "code", "line"),
    [
        (
            [],
            0,
            "ready: all required checks green on {sha} — merge: gh pr merge 7 --merge --match-head-commit {sha}",
        ),
        (["hold-me"], 4, "refused: label hold-me — only the human merges it"),
    ],
    ids=["ready", "held-by-the-base-crew"],
)
def test_the_merge_tools_load_a_real_crew_file_at_a_revision(tmp_path, labels, code, line):
    """CI dry run of the merge tools on a crew with `files.*`: red on a crew read at a revision
    whose paths are checked against the temp dir it was written to (every arm then ended in
    `crew: … files.goals … does not exist`)."""
    sha = real_crew_checkout(tmp_path)
    crew = [sys.executable, str(HERE / "crew.py"), "merge"]
    for field, want in (("armer", "session"), ("hold_labels", "hold-me")):
        got = subprocess.run(
            [*crew, field, "--rev", sha], cwd=tmp_path, capture_output=True, text=True, check=False
        )
        assert (got.returncode, got.stdout.strip()) == (0, want), got.stderr
    reads = [{"head": sha, "checks": GREEN, "labels": labels}]
    got, out, calls = run(tmp_path, reads, ["ok"], extra=("--check", "--sha", sha))
    assert (got, out) == (code, [line.replace("{sha}", sha)])
    assert arm_calls(calls) == []
