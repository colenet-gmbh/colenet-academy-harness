"""plan_push.py: plan commits land on the default branch by push, or are refused untouched.

Each case builds a bare remote, a clone and a detached scratch worktree of it in `tmp_path`; the
secret scan is a fake scanner that flags the made-up token pattern `FAKE-SECRET-<digits>` and
writes a gitleaks-format report. No network, no real secret.

Run: `python3 -m pytest -q we/scripts/test_plan_push.py`
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import plan_push

FAKE_SCANNER = r"""
import json, re, subprocess, sys
rng, report = sys.argv[1], sys.argv[2]
log = subprocess.run(["git", "log", "-p", "--format=commit %H", rng],
                     capture_output=True, text=True, check=True).stdout
found, commit, path, line = [], "", "", 0
for raw in log.splitlines():
    if raw.startswith("commit "):
        commit = raw.split()[1]
    elif raw.startswith("+++ b/"):
        path = raw[6:]
    elif raw.startswith("@@"):
        line = int(re.search(r"\+(\d+)", raw).group(1)) - 1
    elif raw.startswith("+") and not raw.startswith("+++"):
        line += 1
        if re.search(r"FAKE-SECRET-\d+", raw):
            found.append({"File": path, "StartLine": line, "RuleID": "fake-rule",
                          "Commit": commit, "Secret": raw[1:], "Match": raw[1:]})
json.dump(found, open(report, "w"))
print("leak:", *[f["Secret"] for f in found])
sys.exit(1 if found else 0)
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def commit(root: Path, rel: str, text: str, msg: str = "docs: plan") -> None:
    write(root, rel, text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)


def configure(root: Path) -> None:
    git(root, "config", "user.email", "t@example.invalid")
    git(root, "config", "user.name", "t")


class World:
    def __init__(self, tmp: Path, plans: dict, crew: dict | None) -> None:
        self.tmp, self.remote, self.clone = tmp, tmp / "remote.git", tmp / "clone"
        self.scratch = tmp / "scratch"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(tmp, "clone", "-q", str(self.remote), str(self.clone))
        configure(self.clone)
        scanner = tmp / "fake_scanner.py"
        scanner.write_text(FAKE_SCANNER)
        plans = {"secret_scan": f"{sys.executable} {scanner} {{range}} {{report}}", **plans}
        write(self.clone, ".weside/config.json", json.dumps({"plans": plans}))
        if crew is not None:
            crew = {"version": 2, "roles": {}, "members": {}, **crew}
            write(self.clone, ".weside/crew.json", json.dumps(crew))
        commit(self.clone, "README.md", "x\n", "init")
        git(self.clone, "push", "-q", "origin", "HEAD:main")
        git(self.clone, "worktree", "add", "-q", "--detach", str(self.scratch), "origin/main")

    def remote_head(self) -> str:
        return git(self.remote, "log", "-1", "--format=%s", "main").strip()

    def other_push(self, rel: str) -> None:
        """A second session lands a plan of its own on the remote."""
        other = self.tmp / "other"
        if not other.exists():
            git(self.tmp, "clone", "-q", str(self.remote), str(other))
            configure(other)
        git(other, "pull", "-q", "--rebase")
        commit(other, rel, "other\n", f"docs: {rel}")
        git(other, "push", "-q", "origin", "HEAD:main")


@pytest.fixture()
def make(tmp_path, monkeypatch):
    def build(plans: dict | None = None, crew: dict | None = None) -> World:
        world = World(tmp_path, plans or {}, crew)
        monkeypatch.chdir(world.scratch)
        return world

    return build


def test_lands_plan_with_its_planned_receipt(make, capsys):
    world = make(crew={"oracles": {"receipt_dir": ".weside/verify"}})
    write(world.scratch, ".weside/verify/ACME-1.yaml", "state: planned\nchecks: []\n")
    commit(world.scratch, "docs/plans/ACME-1-story.md", "# plan\n", "docs(ACME-1): plan")

    assert plan_push.main([]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("landed on main: ")
    assert "A\tdocs/plans/ACME-1-story.md" in out
    assert "A\t.weside/verify/ACME-1.yaml" in out
    assert world.remote_head() == "docs(ACME-1): plan"


@pytest.mark.parametrize(
    "case, needle",
    [
        ("code path", "src/app.py"),
        ("config widens itself", ".weside/config.json"),
        ("armed receipt", ".weside/verify/ACME-1.yaml"),
        ("receipt without plan", ".weside/verify/ACME-1.yaml"),
        ("dirty", "dirty"),
        ("nothing", "nothing to land"),
        ("main worktree", "main worktree"),
        ("no scanner", "no secret scanner"),
        ("scanner missing", "secret scan did not run"),
    ],
)
def test_refuses_and_pushes_nothing(make, capsys, monkeypatch, case, needle):
    world = make(crew={"oracles": {"receipt_dir": ".weside/verify"}})
    plan = ("docs/plans/ACME-1-story.md", "# plan\n")
    if case == "code path":
        write(world.scratch, "src/app.py", "print(1)\n")
        commit(world.scratch, *plan)
    elif case == "config widens itself":
        wide = {"plans": {"push_paths": ["**"], "secret_scan": "none"}}
        write(world.scratch, ".weside/config.json", json.dumps(wide))
        write(world.scratch, "src/app.py", "print(1)\n")
        commit(world.scratch, *plan)
    elif case == "armed receipt":
        write(world.scratch, ".weside/verify/ACME-1.yaml", "checks: []\n")
        commit(world.scratch, *plan)
    elif case == "receipt without plan":
        commit(world.scratch, ".weside/verify/ACME-1.yaml", "state: planned\n")
    elif case == "dirty":
        commit(world.scratch, *plan)
        write(world.scratch, "docs/plans/ACME-2-story.md", "# draft\n")
    elif case == "main worktree":
        commit(world.clone, *plan)
        monkeypatch.chdir(world.clone)
    elif case == "no scanner":
        write(world.scratch, ".weside/config.json", json.dumps({"plans": {}}))
        git(world.scratch, "add", "-A")
        git(world.scratch, "commit", "-q", "-m", "drop scanner")
        git(world.scratch, "push", "-q", "origin", "HEAD:main")
        commit(world.scratch, *plan)
        monkeypatch.setattr(plan_push.shutil, "which", lambda _name: None)
    elif case == "scanner missing":
        absent = {"plans": {"secret_scan": "no-such-scanner-xyz git {range}"}}
        write(world.scratch, ".weside/config.json", json.dumps(absent))
        git(world.scratch, "add", "-A")
        git(world.scratch, "commit", "-q", "-m", "absent scanner")
        git(world.scratch, "push", "-q", "origin", "HEAD:main")
        commit(world.scratch, *plan)
    before = world.remote_head()

    # A scan refusal must not share 1 with a path refusal: a caller lands a 1 by PR.
    scan_cases = ("no scanner", "scanner missing")
    expected = plan_push.SCAN_REFUSED if case in scan_cases else plan_push.REFUSED
    assert plan_push.main([]) == expected == (4 if case in scan_cases else 1)
    err = capsys.readouterr().err
    assert needle in err
    if case in ("code path", "config widens itself", "armed receipt", "receipt without plan"):
        assert "land these by PR" in err
    assert world.remote_head() == before


def test_secret_finding_names_file_and_line_never_the_value(make, capsys):
    world = make()
    commit(world.scratch, "docs/plans/ACME-1-story.md", "# plan\n\ntoken FAKE-SECRET-12345\n")

    assert plan_push.main([]) == plan_push.SCAN_REFUSED == 4
    captured = capsys.readouterr()
    assert "docs/plans/ACME-1-story.md:3  (fake-rule, commit " in captured.err
    assert "FAKE-SECRET" not in captured.err + captured.out
    assert world.remote_head() == "init"


def test_retries_after_a_concurrent_push(make, capsys, monkeypatch):
    world = make()
    commit(world.scratch, "docs/plans/ACME-1-story.md", "# plan\n", "docs(ACME-1): plan")
    real_push, calls = plan_push.push, []

    def racing_push(remote, branch):
        calls.append(branch)
        if len(calls) == 1:
            world.other_push("docs/plans/ACME-2-story.md")
        return real_push(remote, branch)

    monkeypatch.setattr(plan_push, "push", racing_push)

    assert plan_push.main([]) == 0
    assert len(calls) == 2
    log = git(world.remote, "log", "--format=%s", "main").splitlines()
    assert log[:2] == ["docs(ACME-1): plan", "docs: docs/plans/ACME-2-story.md"]
    assert "A\tdocs/plans/ACME-1-story.md" in capsys.readouterr().out


def test_server_rejection_exits_with_its_own_code(make, capsys):
    world = make()
    hook = world.remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'protected branch' >&2\nexit 1\n")
    hook.chmod(0o755)
    commit(world.scratch, "docs/plans/ACME-1-story.md", "# plan\n")

    assert plan_push.main([]) == plan_push.REJECTED
    assert "this repo takes no direct push; land by PR" in capsys.readouterr().err
    assert world.remote_head() == "init"
