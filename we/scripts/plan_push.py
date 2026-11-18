#!/usr/bin/env python3
"""Land plan commits from a scratch worktree on the default branch by push.

Usage (inside the detached scratch worktree of `references/plan-commit.md`):

  plan_push.py [--remote origin] [--branch <default>]

The worktree holds one or more commits on top of the default branch. The script

1. refuses the main worktree, a dirty tree and a tree with nothing to land;
2. fetches and rebases the commits onto `<remote>/<default>`;
3. refuses any path the commits touch outside the allowed globs: `plans.push_paths` in
   `.weside/config.json` (default `docs/plans/**`), plus a staging-check file under the crew's
   `oracles.receipt_dir` that travels with a plan and still carries `state: planned`. Config and
   crew are read from this tree after the rebase, i.e. as committed on the default branch (a path
   that changes them is refused), never from the main checkout's uncommitted copy;
4. runs the secret scan over the commit range: `plans.secret_scan`, a command template with
   `{range}` and an optional `{report}` (a JSON report path in gitleaks' format), default
   `gitleaks git --log-opts={range} --redact --no-banner --report-format json --report-path {report}`
   when `gitleaks` is on PATH. No scanner and no configured command refuses; `"none"` skips the scan.
   A finding prints file and line from the report, never the matched value, and never the
   scanner's own output;
5. pushes `HEAD:<default>`; a non-fast-forward fetches, rebases, checks again and retries, at most
   three times;
6. prints the landed commit and one line per file.

Globs: `**/` matches zero or more whole directories, a bare `**` anything, `*` and `?` stay inside
one directory, `{a,b}` expands (the grammar of `risk_paths.py`).

Exit codes:
  0  landed
  1  refused (main worktree, dirty tree, nothing to land, a path outside the globs, a secret
     finding, no scanner); nothing was pushed
  2  failed (a git error, a rebase conflict, retries used up); nothing was pushed
  3  the server rejected the push: this repo takes no direct push, land by PR
Pinned by `scripts/test_plan_push.py`.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import crew  # noqa: E402
from risk_paths import _expand, _glob_regex  # noqa: E402

DEFAULT_PATHS = ["docs/plans/**"]
DEFAULT_SCAN = (
    "gitleaks git --log-opts={range} --redact --no-banner "
    "--report-format json --report-path {report}"
)
RETRIES = 3
PLANNED = re.compile(r"^state:\s*planned\s*(#.*)?$", re.M)
REFUSED, FAILED, REJECTED = 1, 2, 3
NO_PUSH_LINE = "this repo takes no direct push; land by PR"


class PushError(Exception):
    """Ends the run with an exit code and a message."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    out = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if check and out.returncode != 0:
        raise PushError(FAILED, f"git {' '.join(args)}: {(out.stderr or out.stdout).strip()}")
    return out


def push(remote: str, branch: str) -> subprocess.CompletedProcess:
    return git("push", "--porcelain", remote, f"HEAD:refs/heads/{branch}", check=False)


def default_branch(remote: str) -> str:
    head = git("symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD", check=False)
    if head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip().split("/", 1)[1]
    for name in ("main", "master"):
        if (
            git(
                "rev-parse", "--verify", "-q", f"refs/remotes/{remote}/{name}", check=False
            ).returncode
            == 0
        ):
            return name
    raise PushError(FAILED, f"no default branch on {remote}; pass --branch")


def load_config(top: Path) -> dict[str, Any]:
    path = top / ".weside" / "config.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PushError(FAILED, f"{path}: {exc}") from exc
    plans = data.get("plans") if isinstance(data, dict) else None
    return plans if isinstance(plans, dict) else {}


def receipt_dir(top: Path) -> str | None:
    path = crew.at(top)
    try:
        data = crew.load(path) if path else None
    except crew.CrewError:
        return None
    value = ((data or {}).get("oracles") or {}).get("receipt_dir")
    return value.strip("/") if isinstance(value, str) and value.strip("/") else None


def preflight() -> Path:
    if git("rev-parse", "--is-inside-work-tree", check=False).stdout.strip() != "true":
        raise PushError(REFUSED, "not inside a git worktree")
    git_dir = Path(git("rev-parse", "--absolute-git-dir").stdout.strip())
    common = Path(git("rev-parse", "--git-common-dir").stdout.strip())
    if git_dir.resolve() == (Path.cwd() / common).resolve():
        raise PushError(
            REFUSED,
            "refused: this is the main worktree; commit in a detached scratch worktree "
            "(references/plan-commit.md step 2)",
        )
    if git("status", "--porcelain").stdout.strip():
        raise PushError(REFUSED, "refused: the tree is dirty; commit or remove the changes first")
    return Path(git("rev-parse", "--show-toplevel").stdout.strip())


def rebase(base: str) -> None:
    git("fetch", "--quiet", *base.split("/", 1))
    if git("rebase", "--quiet", base, check=False).returncode != 0:
        git("rebase", "--abort", check=False)
        raise PushError(
            FAILED, f"rebase onto {base} conflicts; resolve it in this tree and run again"
        )
    if git("rev-list", "--count", f"{base}..HEAD").stdout.strip() == "0":
        raise PushError(REFUSED, f"refused: nothing to land, HEAD has no commit beyond {base}")


def touched(base: str) -> list[str]:
    out = git("log", "--no-renames", "--name-only", "--format=", f"{base}..HEAD").stdout
    return sorted({line for line in out.splitlines() if line.strip()})


def outside(paths: list[str], globs: list[str], receipts: str | None) -> list[str]:
    patterns = [_glob_regex(g) for glob in globs for g in _expand(glob)]
    plans = [p for p in paths if any(r.match(p) for r in patterns)]
    rest = [p for p in paths if p not in plans]
    if not plans or not receipts:
        return rest
    return [p for p in rest if not (p.startswith(receipts + "/") and _planned(p))]


def _planned(path: str) -> bool:
    shown = git("show", f"HEAD:{path}", check=False)
    return shown.returncode == 0 and bool(PLANNED.search(shown.stdout))


def scan(base: str, setting: Any) -> None:
    if setting == "none":
        return
    if isinstance(setting, str) and setting.strip():
        template = setting
    elif setting is None and shutil.which("gitleaks"):
        template = DEFAULT_SCAN
    else:
        raise PushError(
            REFUSED,
            "refused: no secret scanner; install gitleaks or set plans.secret_scan in "
            '.weside/config.json (a command with {range}, or "none")',
        )
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        argv = [
            t.replace("{range}", f"{base}..HEAD").replace("{report}", str(report))
            for t in shlex.split(template)
        ]
        try:
            run = subprocess.run(argv, capture_output=True, text=True, check=False)
        except OSError as exc:
            raise PushError(REFUSED, f"refused: secret scan did not run: {exc.strerror}") from exc
        if run.returncode == 0:
            return
        lines = _findings(report)
    detail = "\n".join(f"  {line}" for line in lines) or (
        f"  exit {run.returncode}, no report; run the scan yourself: {template}"
    )
    raise PushError(REFUSED, f"refused: the secret scan found something:\n{detail}")


def _findings(report: Path) -> list[str]:
    try:
        items = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    lines = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            where = f"{item.get('File', '?')}:{item.get('StartLine', '?')}"
            rule = item.get("RuleID") or "finding"
            commit = str(item.get("Commit") or "")[:9]
            lines.append(f"{where}  ({rule}{', commit ' + commit if commit else ''})")
    return lines


def check(base: str, plans: dict[str, Any], top: Path) -> None:
    globs = plans.get("push_paths") or DEFAULT_PATHS
    if isinstance(globs, str):
        globs = [globs]
    bad = outside(touched(base), globs, receipt_dir(top))
    if bad:
        listed = "\n".join(f"  {p}" for p in bad)
        raise PushError(
            REFUSED,
            f"refused: paths outside plans.push_paths ({', '.join(globs)}):\n{listed}\n"
            "land these by PR (references/plan-commit.md step 5, the PR way)",
        )
    scan(base, plans.get("secret_scan"))


def land(remote: str, branch: str | None) -> list[str]:
    top = preflight()
    branch = branch or default_branch(remote)
    base = f"{remote}/{branch}"
    for _attempt in range(RETRIES + 1):
        rebase(base)
        check(base, load_config(top), top)
        before = git("rev-parse", base).stdout.strip()
        out = push(remote, branch)
        if out.returncode == 0:
            return _landed(before, branch)
        text = out.stdout + out.stderr
        if "[remote rejected]" in text or "declined" in text:
            raise PushError(REJECTED, NO_PUSH_LINE)
        if not re.search(r"rejected|non-fast-forward|fetch first", text):
            raise PushError(FAILED, f"push failed: {text.strip()}")
    raise PushError(FAILED, f"push lost the race {RETRIES + 1} times; run again")


def _landed(before: str, branch: str) -> list[str]:
    head = git("log", "-1", "--format=%h %s").stdout.strip()
    files = git("diff", "--no-renames", "--name-status", f"{before}..HEAD").stdout.splitlines()
    return [f"landed on {branch}: {head}", *(f"  {line}" for line in files)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Land plan commits on the default branch by push."
    )
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default=None)
    args = parser.parse_args(argv)
    try:
        lines = land(args.remote, args.branch)
    except PushError as stop:
        print(str(stop), file=sys.stderr)
        return stop.code
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
