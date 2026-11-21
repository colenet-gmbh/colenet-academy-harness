#!/usr/bin/env python3
"""The one runner of a repo's risk classifier on a commit range, for the T2 panel's carry
(`lens_verdict.py`, the delta between two heads).

`rules(root, git)` picks the classifier from `RULES_FROM` (`origin/main`), fetched first: the
crew's `risk.command` blob (from `risk.rules_from`), else the plugin's `risk_paths.py` (the repo's
own blob of `we/scripts/risk_paths.py` when it carries one) over `.weside/risk-paths`, else no
rules (class T0) — unless `RULES_FROM` carries a crew file: a crew without rules is a reason, never
T0. `run(root, git, found, head, base, timeout)` writes the classifier to a temp file and runs it
with `base` in a no-checkout worktree of `head`; line 1 must match `CLASS_LINE`. The worktree and
the temp dir go whatever happens.

`risk_runner.py here <base>` runs the same classifier in the current checkout (its uncommitted
check included), for `arm_pr.sh`: stdout is the classifier's output and exit 0 when line 1 matches
`CLASS_LINE`; otherwise one line `<classifier> exit <rc>: <why>` or `<classifier> line 1: <line>`
(or the runner's reason) and exit 2. A base without `.weside/risk-paths` and without a crew
`risk.command` prints `T0`. It fetches `RULES_FROM` (and a crew's `risk.rules_from`) best-effort
and reads the rules from the ref it has.

`risk_runner.py head <sha> <base>` classifies the commit `sha` instead of the checkout: the same
classifier on `base` in a no-checkout worktree of `sha` (`exec_in_tree`), with the output and exit
codes of `here`. A base without rules is `T0` before the commit is looked at; otherwise a commit
missing from this clone is a reason (`head <sha> not here (fetch it)`), exit 2. `arm_pr.sh` runs it
when its checkout is not the PR head, so the class never depends on where the arm runs.

`git(root, *args, timeout=...)` is the caller's git: stdout, or None on any failure. Python 3.9, stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

RULES_FROM = "origin/main"
CLASS_LINE = re.compile(r"^T[02]( one-way (\S+))?$")
RISK_PATHS = Path(__file__).resolve().with_name("risk_paths.py")
CLASSIFY_TIMEOUT = 60

Git = Callable[..., Optional[str]]
Found = tuple  # (argv prefix with a {file} slot, blob spec or None, rules ref)


def plain_git(root: str, *args: str, timeout: float = 20) -> str | None:
    """stdout of `git <args>` in `root`, or None on any failure (no repo hooks)."""
    try:
        out = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=root,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def crew_present(root: str, git: Git, ref: str = RULES_FROM) -> bool:
    """`ref` carries a crew file of its own, parsing or not."""
    return git(root, "cat-file", "-e", f"{ref}:.weside/crew.json") is not None


def crew_on(root: str, git: Git, ref: str = RULES_FROM) -> dict:
    """`ref`'s `.weside/crew.json` as a dict; {} when it has none or it does not parse."""
    raw = git(root, "show", f"{ref}:.weside/crew.json")
    try:
        doc = json.loads(raw or "")
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def fetch_rules(root: str, ref: str, git: Git) -> bool:
    """Fetch `origin/<branch>` so the rules are current; False when it cannot be fetched (a ref
    outside `origin/` is not fetched and must exist)."""
    remote, _, branch = ref.partition("/")
    if remote == "origin" and branch:
        refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
        if git(root, "fetch", "-q", "--no-tags", "origin", refspec, timeout=60) is None:
            return False
    return git(root, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}") is not None


def rules(root: str, git: Git, fetch: bool = True) -> Found | None | str:
    """(argv prefix with a {file} slot, blob spec to write there or None, rules ref);
    None when the repo uses no rules (class T0); a str is the reason it cannot be computed.
    `fetch=False`: the caller fetched; `RULES_FROM` only has to exist."""
    if fetch and not fetch_rules(root, RULES_FROM, git):
        return (
            f"`git fetch origin {RULES_FROM.split('/', 1)[1]}` failed; the rules must be current"
        )
    if not fetch and git(root, "rev-parse", "--verify", "-q", f"{RULES_FROM}^{{commit}}") is None:
        return f"no {RULES_FROM} here (fetch it)"
    crew_doc = crew_on(root, git)
    risk = crew_doc.get("risk") or {}
    rules_from = risk.get("rules_from") if isinstance(risk.get("rules_from"), str) else RULES_FROM
    command = risk.get("command")
    if isinstance(command, str) and command.strip():
        parts = command.split()
        return ["{file}", *parts[1:]], f"{rules_from}:{parts[0]}", rules_from
    if git(root, "cat-file", "-e", f"{rules_from}:.weside/risk-paths") is None:
        if crew_doc or crew_present(root, git, rules_from):
            return (
                f"this repo has a crew (`.weside/crew.json` on {rules_from}) but no risk rules: "
                f"neither `risk.command` in the crew nor `.weside/risk-paths` on {rules_from}, "
                "so the hook cannot tell a one-way PR from a two-way one. Land a "
                "`.weside/risk-paths` (`/we:setup` writes a starter; without one-way sections it "
                "brakes nothing, on purpose) or a `risk.command` first; the PR that lands it is "
                "refused here too, so the owner merges that one by hand"
            )
        return None
    own = f"{rules_from}:we/scripts/risk_paths.py"
    blob = own if git(root, "cat-file", "-e", own) is not None else None
    return [sys.executable, "{file}"], blob, rules_from


def exec_in_tree(
    root: str,
    git: Git,
    blob: str | None,
    args: list[str],
    tree_ref: str,
    timeout: float,
) -> tuple[subprocess.CompletedProcess | None, str | None]:
    """(the finished run, None) or (None, why): the script `blob` (a `<ref>:<path>` spec, or None
    for the plugin's `risk_paths.py`) written to a temp file and run as `<file> <args>` in a
    no-checkout worktree of `tree_ref`. Every argv entry `{file}` is the script. The temp dir goes
    and `git worktree prune` drops the scratch tree's entry, always (never `remove --force`)."""
    tmp = tempfile.mkdtemp(prefix="we-merge-class.")
    tree = os.path.join(tmp, "head")
    try:
        script, why = _write_script(root, git, blob, tmp)
        if script is None:
            return None, why
        added = git(
            root, "worktree", "add", "--no-checkout", "--detach", tree, tree_ref, timeout=60
        )
        if added is None:
            return None, f"`git worktree add` of {tree_ref[:9]} failed"
        return _exec(script, args, tree, timeout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        git(root, "worktree", "prune")


def _write_script(
    root: str, git: Git, blob: str | None, tmp: str
) -> tuple[str | None, str | None]:
    """(path, None) of the classifier written into `tmp`, or (None, why)."""
    script = os.path.join(tmp, "classifier")
    if blob is None:
        shutil.copyfile(RISK_PATHS, script)
    else:
        body = git(root, "show", blob)
        if body is None:
            return None, f"`{blob}` is missing"
        Path(script).write_text(body, encoding="utf-8")
    os.chmod(script, 0o700)
    return script, None


def _exec(
    script: str, args: list[str], cwd: str, timeout: float
) -> tuple[subprocess.CompletedProcess | None, str | None]:
    """The classifier run as `<args>` with every `{file}` its path, in `cwd`."""
    argv = [script if a == "{file}" else a for a in args]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        out = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return None, f"the classifier ran past {timeout:.0f} s"
    except OSError as exc:
        return None, f"the classifier is not runnable: {exc.strerror}"
    return out, None


def run(
    root: str,
    git: Git,
    found: Found,
    head: str,
    base: str,
    timeout: float = CLASSIFY_TIMEOUT,
) -> tuple[list[str] | None, str | None]:
    """(output lines with the class as line 1, None) or (None, why): the classifier on `base` in
    a no-checkout worktree of `head` (`exec_in_tree`)."""
    prefix, blob, _rules_from = found
    out, why = exec_in_tree(root, git, blob, [*prefix, base], head, timeout)
    if out is None:
        return None, why
    lines = out.stdout.splitlines() or [""]
    line = lines[0].strip()
    if out.returncode != 0 or not CLASS_LINE.match(line):
        last = (out.stderr.strip().splitlines() or [line or "no output"])[-1]
        return None, f"the classifier exited {out.returncode}: {last}"
    return [line, *lines[1:]], None


def classifier_name(found: Found) -> str:
    """`risk_paths.py` for the plugin's classifier, else the crew command's script path."""
    _prefix, blob, _rules_from = found
    if blob is None or blob.endswith(":we/scripts/risk_paths.py"):
        return "risk_paths.py"
    return blob.split(":", 1)[1]


def _has_rules(root: str, git: Git, ref: str) -> bool:
    """`ref` (the PR's base) carries `.weside/risk-paths` or a crew `risk.command`."""
    if git(root, "cat-file", "-e", f"{ref}:.weside/risk-paths") is not None:
        return True
    command = (crew_on(root, git, ref).get("risk") or {}).get("command")
    return isinstance(command, str) and bool(command.strip())


def _classifier(
    root: str, base: str, git: Git, sha: str | None = None
) -> tuple[Found | None, tuple[int, str] | None]:
    """(the classifier `rules` found, None) or (None, the answer without one) for `here` and
    `head`: `(0, "T0")` when the base or the rules set none, `(2, why)` when it cannot be told.
    `sha`, when given, must be a commit in this clone. The rules are fetched best-effort: offline,
    the ref at hand decides."""
    if git(root, "rev-parse", "--verify", "-q", f"{base}^{{commit}}") is None:
        return None, (
            2,
            f"no {base} here (fetch it)",
        )  # an absent base cannot say it sets no rules
    if not _has_rules(root, git, base):
        return None, (
            0,
            "T0",
        )  # the PR's base sets no rules: nothing to classify, as without a crew
    if sha is not None and git(root, "cat-file", "-e", f"{sha}^{{commit}}") is None:
        return None, (2, f"head {sha[:12]} not here (fetch it)")
    fetch_rules(root, RULES_FROM, git)
    rules_from = (crew_on(root, git).get("risk") or {}).get("rules_from")
    if isinstance(rules_from, str) and rules_from != RULES_FROM:
        fetch_rules(root, rules_from, git)
    found = rules(root, git, fetch=False)
    if found is None:
        return None, (0, "T0")
    if isinstance(found, str):
        return None, (2, found)
    return found, None


def here(
    root: str, base: str, git: Git = plain_git, timeout: float = CLASSIFY_TIMEOUT
) -> tuple[int, str]:
    """(0, the classifier's stdout) or (2, one line why): the crew's classifier on `base` in the
    checkout `root` itself, so a risk path with uncommitted changes is seen (`arm_pr.sh`)."""
    found, answer = _classifier(root, base, git)
    if found is None:
        return answer
    prefix, blob, _rules_from = found
    name = classifier_name(found)
    tmp = tempfile.mkdtemp(prefix="we-arm-class.")
    try:
        script, why = _write_script(root, git, blob, tmp)
        out = None
        if script is not None:
            out, why = _exec(script, [*prefix, base], root, timeout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if out is None:
        return 2, f"{name}: {why}"
    return _judge(name, out)


def head(
    root: str, sha: str, base: str, git: Git = plain_git, timeout: float = CLASSIFY_TIMEOUT
) -> tuple[int, str]:
    """(0, the classifier's stdout) or (2, one line why): the crew's classifier on `base` in a
    no-checkout worktree of the commit `sha`, whatever this checkout holds (`arm_pr.sh`)."""
    found, answer = _classifier(root, base, git, sha)
    if found is None:
        return answer
    prefix, blob, _rules_from = found
    name = classifier_name(found)
    out, why = exec_in_tree(root, git, blob, [*prefix, base], sha, timeout)
    if out is None:
        return 2, f"{name}: {why}"
    return _judge(name, out)


def _judge(name: str, out: subprocess.CompletedProcess) -> tuple[int, str]:
    """(0, stdout) for a class on line 1 and exit 0; (2, one line why) otherwise."""
    line = (out.stdout.splitlines() or [""])[0].strip()
    if out.returncode != 0:
        last = line or (out.stderr.strip().splitlines() or ["no output"])[-1]
        return 2, f"{name} exit {out.returncode}: {last}"
    if not CLASS_LINE.match(line):
        return 2, f"{name} line 1: {line or 'empty'}"
    return 0, out.stdout.rstrip("\n")


def main(argv: list[str]) -> int:
    ok = (len(argv) == 2 and argv[0] == "here") or (len(argv) == 3 and argv[0] == "head")
    if not ok or any(a.startswith("-") for a in argv[1:]):
        print("usage: risk_runner.py here <base> | head <sha> <base>", file=sys.stderr)
        return 2
    root = plain_git(os.getcwd(), "rev-parse", "--show-toplevel")
    if root is None:
        print("not a git checkout")
        return 2
    if argv[0] == "head":
        code, text = head(root.strip(), argv[1], argv[2])
    else:
        code, text = here(root.strip(), argv[1])
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
