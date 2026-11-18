#!/usr/bin/env python3
"""The launcher's supervisor of one process worker, and its garbage collection (`we-worker gc`).

`we-worker` starts one supervisor per worker (`spawn`, detached, no model). It waits until
`claude agents --json` lists a living session of the worker's name, then polls it every 30 s:

- a permission prompt (`waitingFor: "permission prompt"`, what `we-worker waiting` reads) that
  stays on the same tool call for 15 minutes becomes one board news item for the human
  (`board.py news worker`), once per prompt;
- when the session's process is gone, the end reason comes from the transcript's last assistant
  entry (`usage-limit`, `auth`) or the timer file's passed `deadline` (`time-limit`). With a
  reason, one line with the resume command goes to the starter: into its inbox socket (the
  starter's `CLAUDE_CODE_MESSAGING_SOCKET` and `_TOKEN`, inherited at the spawn and never written
  to a file), else as a board news item. A worker stopped with `we-worker stop` (its settings
  file is already gone) ends without a line;
- then it removes the worker's settings file and timer file, unless a living session of the same
  name holds them again, or the worker ended on a usage limit or an auth error: a resumable worker
  keeps both (the timer file names its starter, limit and deadline), `gc` ages them out after 7 days. A worker never seen within 5 minutes keeps its files (a start
  held by a dialog may still read them); `gc` ages them out.

`gc` touches only the plugin's own files of sessions that no longer live (older than 10 minutes;
a worker whose last transcript entry is a usage limit or an auth error is resumable, its files stay
7 days): the settings files
`<name>.settings.json` and timer files `<name>.json` of the state dir, and the launcher's marker
(with brief and settings beside it, `worker_launch.clear_marker`) in the repo's worktrees. A
merged, clean worktree whose marker names no living session keeps its marker and is listed with
the repo's remove command (`workers.remove_worktree`, else `git worktree remove`); gc never
removes a worktree. Claude Code's own job directories (`~/.claude/jobs/<id>`) are only counted:
whether `--resume` still needs one cannot be told from outside. Without `--apply` it only prints
what it would remove (the default). A file younger than 10 minutes stays (a start in flight). When
`claude agents --json` cannot be read, gc refuses: nothing is judged dead on a guess.

Usage:
  worker_supervisor.py spawn --name <n> --tree <dir>   the supervisor, detached
  worker_supervisor.py run --name <n> --tree <dir>     the supervisor in the foreground
  worker_supervisor.py gc [--repo <dir>] [--apply | --dry-run]
State dir: `$WE_WORKER_STATE_DIR`, else `~/.claude/we-state/workers`.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import worker_launch as wl

POLL_S = 30
APPEAR_S = 300
PROMPT_AGE_S = 15 * 60
GRACE_S = 10 * 60
RESUMABLE = ("usage-limit", "auth")  # the ends `we-worker status` prints a resume command for
RESUMABLE_AGE_S = 7 * 24 * 3600
BOARD_TIMEOUT_S = 60


def state_dir() -> Path:
    return Path(
        os.environ.get("WE_WORKER_STATE_DIR") or Path.home() / ".claude" / "we-state" / "workers"
    )


def projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def jobs_dir() -> Path:
    return Path.home() / ".claude" / "jobs"


def _tail_entries(projects: Path, session_id: str | None, kind: str = "assistant"):
    """The transcript's entries of `kind`, newest first (reads the whole file once)."""
    files = glob.glob(str(projects / "*" / f"{session_id}.jsonl")) if session_id else []
    if not files:
        return []
    out = []
    with open(files[0], encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if f'"{kind}"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("type") == kind:
                out.append(entry)
    return out[::-1]


def _text(entry: dict) -> str:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content or "")


def api_mark(entry: dict | None) -> str | None:
    """`usage-limit`, `auth` or None for the last assistant entry (as `we-worker status` marks)."""
    if not entry:
        return None
    err = entry.get("error") if entry.get("isApiErrorMessage") else None
    if err == "rate_limit":
        return "usage-limit"
    if err in ("authentication_failed", "oauth_org_not_allowed"):
        return "auth"
    return "usage-limit" if re.search(r"(hit|reached) your .*limit", _text(entry)) else None


def prompt_id(projects: Path, session_id: str | None) -> str | None:
    """The id of the last tool call the session made: the one a permission prompt waits on."""
    for entry in _tail_entries(projects, session_id):
        content = (entry.get("message") or {}).get("content")
        for part in reversed(content if isinstance(content, list) else []):
            if isinstance(part, dict) and part.get("type") == "tool_use" and part.get("id"):
                return str(part["id"])
    return None


def timer_deadline(state: Path, name: str) -> float | None:
    try:
        data = json.loads((state / f"{name}.json").read_text())
        when = dt.datetime.fromisoformat(str(data["deadline"]).replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.timestamp()


def end_reason(projects: Path, state: Path, name: str, session_id: str | None, now: float):
    entries = _tail_entries(projects, session_id)
    mark = api_mark(entries[0] if entries else None)
    if mark:
        return mark
    deadline = timer_deadline(state, name)
    return "time-limit" if deadline is not None and deadline <= now else None


WHEN = {
    "usage-limit": "after the reset",
    "auth": "once the login is back",
    "time-limit": "if needed",
}


def notice(name: str, reason: str, session_id: str | None, tree: str) -> str:
    where = (
        f"cd {tree} && we-worker {name} --here"
        if wl.main_checkout(Path(tree))
        else f"we-worker {name} --tree {tree}"
    )
    resume = f"{where} --bg --resume {session_id or '<session id>'}"
    return f"we-worker: {name} ended ({reason}) in {tree}. Resume {WHEN[reason]}: {resume}"


def deliver(sock_path: str, token: str, text: str) -> None:
    """One user line into a session's inbox socket (the format of `wake_poller.deliver`)."""
    lines = (
        json.dumps({"type": "auth", "token": token})
        + "\n"
        + json.dumps({"type": "user", "message": {"role": "user", "content": text}})
        + "\n"
    )
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(10)
    try:
        conn.connect(sock_path)
        conn.sendall(lines.encode("utf-8"))
        conn.shutdown(socket.SHUT_WR)
    finally:
        conn.close()


def to_starter(text: str, environ: dict | None = None) -> bool:
    env = os.environ if environ is None else environ
    sock, token = env.get("CLAUDE_CODE_MESSAGING_SOCKET"), env.get("CLAUDE_CODE_MESSAGING_TOKEN")
    if not (sock and token):
        return False
    try:
        deliver(sock, token, text)
    except OSError:
        return False
    return True


def board_news(tree: str, item_id: str, title: str, body: str = "") -> bool:
    """One `board.py news worker` item (idempotent by id), run in the worker's tree."""
    script = Path(__file__).resolve().with_name("board.py")
    argv = [sys.executable, str(script), "news", "worker", "--id", item_id, "--title", title]
    if body:
        argv += ["--body", body]
    try:
        out = subprocess.run(
            argv, cwd=tree, capture_output=True, text=True, check=False, timeout=BOARD_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


def _named(rows: list[dict], name: str) -> list[dict]:
    return [r for r in rows if r.get("name") == name]


def _living(rows: list[dict], name: str) -> list[dict]:
    return [r for r in _named(rows, name) if wl._pid_lives(r.get("pid", "-"))]


def _remove_files(state: Path, name: str) -> list[str]:
    gone = []
    for path in (state / f"{name}.settings.json", state / f"{name}.json"):
        with contextlib.suppress(OSError):
            path.unlink()
            gone.append(path.name)
    return gone


def _watch_prompt(row, name, tree, sid, projects, now, first_seen, reported, board) -> None:
    """One board item per permission prompt that holds the same tool call for `PROMPT_AGE_S`."""
    if row.get("waitingFor") != "permission prompt":
        first_seen.clear()
        return
    prompt = prompt_id(projects, sid) or f"{sid}:prompt"
    since = first_seen.setdefault(prompt, now)
    if now - since < PROMPT_AGE_S or prompt in reported:
        return
    reported.add(prompt)
    board(
        tree,
        f"W-{name}-{prompt[-8:]}",
        f"{name} waits on a permission prompt for {int((now - since) // 60)} min",
        f"Answer it: claude attach {row.get('id') or sid}; the tool call: we-worker waiting",
    )


def supervise(
    name: str,
    tree: str,
    *,
    sessions: Callable[[], list[dict]] = wl.sessions,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    send: Callable[[str], bool] = to_starter,
    board: Callable[..., bool] = board_news,
    state: Path | None = None,
    projects: Path | None = None,
    log: Callable[[str], None] = lambda _line: None,
) -> str:
    """Watch the worker `name` until its process ends; returns what happened as one word."""
    state = state or state_dir()
    projects = projects or projects_dir()
    give_up = clock() + APPEAR_S
    row = None
    while row is None:
        with contextlib.suppress(wl.LaunchError):
            found = _living(sessions(), name)
            row = found[-1] if found else None
        if row is None:
            if clock() >= give_up:
                # its files stay: a start held by a dialog may still read them; gc ages them out
                log(f"{name}: no living session within {APPEAR_S} s")
                return "never-seen"
            sleep(POLL_S)
    pid, sid = row.get("pid"), row.get("sessionId")
    first_seen: dict[str, float] = {}
    reported: set[str] = set()
    while True:
        try:
            mine = [r for r in _named(sessions(), name) if r.get("pid") == pid]
        except wl.LaunchError:
            mine = [row]
        if not mine or not wl._pid_lives(pid):
            break
        row = mine[-1]
        _watch_prompt(row, name, tree, sid, projects, clock(), first_seen, reported, board)
        sleep(POLL_S)
    stopped = not (state / f"{name}.settings.json").exists()
    reason = None if stopped else end_reason(projects, state, name, sid, clock())
    if reason:
        line = notice(name, reason, sid, tree)
        if not send(line):
            board(tree, f"W-{name}-end-{(sid or 'x')[:8]}", line.removeprefix("we-worker: "))
    try:
        again = bool(_living(sessions(), name))
    except wl.LaunchError:
        again = True
    if not again and reason not in RESUMABLE:
        _remove_files(state, name)
    return reason or ("stopped" if stopped else "ended")


def spawn(name: str, tree: str) -> int:
    """Start `run` detached (own session, no tty); it inherits this environment, the starter's
    inbox socket and token with it. Returns the child's pid."""
    state = state_dir()
    state.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, os.path.abspath(__file__), "run", "--name", name, "--tree", tree]
    with open(state / "supervisor.log", "a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
            env={k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"},
        )
    return proc.pid


# --- gc --------------------------------------------------------------------------------------


def _resumable_names(rows: list[dict], living: set[str], projects: Path) -> set[str]:
    """Dead names whose newest session ended on a usage limit or an auth error."""
    newest = {}
    for r in sorted(rows, key=lambda r: r.get("startedAt") or 0):
        if r.get("name"):
            newest[str(r["name"])] = r.get("sessionId")
    return {n for n, sid in newest.items() if n not in living and _resumable(projects, sid)}


def _resumable(projects: Path, session_id: str | None) -> bool:
    entries = _tail_entries(projects, session_id)
    return api_mark(entries[0] if entries else None) in RESUMABLE


def _old(path: Path, now: float, floor: float = GRACE_S) -> bool:
    try:
        return now - path.stat().st_mtime >= floor
    except OSError:
        return False


def _remove(path: Path, apply: bool) -> None:
    if not apply:
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):
            path.unlink()


def _worktrees(repo: Path) -> list[dict]:
    out = wl._git(repo, "worktree", "list", "--porcelain") or ""
    trees, cur = [], {}
    for line in [*out.splitlines(), ""]:
        if not line:
            if cur:
                trees.append(cur)
            cur = {}
        elif line.startswith("worktree "):
            cur["path"] = line[len("worktree ") :]
        elif line.startswith("branch refs/heads/"):
            cur["branch"] = line[len("branch refs/heads/") :]
    return trees[1:]  # the first is the main checkout


def merged(repo: Path, branch: str, base: str) -> bool:
    """The branch's head, with commits of its own (it moved since its reflog's first entry), is in
    `origin/<base>`, or GitHub lists a merged PR from it. A branch with no commit of its own sits in
    `origin/<base>` too and is never merged by that alone."""
    head = wl._git(repo, "rev-parse", f"refs/heads/{branch}")
    born = (wl._git(repo, "reflog", "show", "--format=%H", f"refs/heads/{branch}") or "").split()
    own = bool(head and born and born[-1] != head)
    if own and wl._git(repo, "merge-base", "--is-ancestor", head, f"origin/{base}") is not None:
        return True
    if not shutil.which("gh"):
        return False
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        out = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "number"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=BOARD_TIMEOUT_S,
        )
        return out.returncode == 0 and out.stdout.strip() not in ("", "[]")
    return False


def gc(
    repo: Path,
    apply: bool = False,
    *,
    sessions: Callable[[], list[dict]] = wl.sessions,
    state: Path | None = None,
    jobs: Path | None = None,
    projects: Path | None = None,
    now: float | None = None,
) -> list[str]:
    state = state or state_dir()
    projects = projects or projects_dir()
    jobs = jobs or jobs_dir()
    now = time.time() if now is None else now
    rows = sessions()  # a LaunchError refuses: nothing is judged dead on a guess
    living = {
        str(r.get("name")) for r in rows if r.get("name") and wl._pid_lives(r.get("pid", "-"))
    }
    listed = {str(r.get(f)) for r in rows for f in ("id", "sessionId") if r.get(f)}
    verb = "removed" if apply else "would remove"
    lines = []
    resumable = _resumable_names(rows, living, projects)
    for path in sorted(state.glob("*.json")):
        name = (
            path.name[: -len(".settings.json")]
            if path.name.endswith(".settings.json")
            else path.stem
        )
        floor = RESUMABLE_AGE_S if name in resumable else GRACE_S
        if name in living or not _old(path, now, floor):
            continue
        _remove(path, apply)
        lines.append(f"{verb} {path}")
    unlisted = 0
    for job in (p for p in jobs.glob("*") if p.is_dir()):
        try:
            sid = json.loads((job / "state.json").read_text()).get("sessionId")
        except (OSError, ValueError, AttributeError):
            sid = None
        unlisted += not ({job.name, str(sid or "")} & listed)
    top = wl.main_checkout(repo) or Path(wl._git(repo, "rev-parse", "--show-toplevel") or repo)
    base = wl.default_branch(top)
    for tree in _worktrees(top):
        path, branch = tree.get("path"), tree.get("branch")
        marker = wl.read_marker(path)
        if not (path and marker) or marker.get("name") in living:
            continue
        gd = wl.git_dir(path)
        if gd is None or not _old(gd / wl.MARKER, now):
            continue
        clean = wl._git(Path(path), "status", "--porcelain") == ""
        if clean and branch and merged(top, branch, base):
            lines.append(
                f"worktree {path} ({branch} merged, {marker.get('name')} not living): remove with "
                + wl.remove_command(top, path)
            )
            continue
        if apply:
            wl.clear_marker(Path(path))
        lines.append(f"{verb} the marker of {marker.get('name')} in {path}")
        if not clean:
            lines.append(f"keep {path}: uncommitted changes")
    if unlisted:
        lines.append(
            f"{unlisted} job directories under {jobs} belong to no listed session; gc leaves "
            "them to Claude Code"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="worker_supervisor.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for verb in ("spawn", "run"):
        p = sub.add_parser(verb)
        p.add_argument("--name", required=True)
        p.add_argument("--tree", required=True)
    p = sub.add_parser("gc")
    p.add_argument("--repo", type=Path, default=Path.cwd())
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "spawn":
        print(spawn(args.name, args.tree))
        return 0
    if args.cmd == "run":

        def log(line: str) -> None:
            print(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {line}", flush=True)

        log(f"{args.name}: {supervise(args.name, args.tree, log=log)}")
        return 0
    try:
        lines = gc(args.repo.resolve(), apply=args.apply)
    except wl.LaunchError as exc:
        print(f"we-worker gc: refused, {exc}", file=sys.stderr)
        return 2
    print("\n".join(lines) if lines else "we-worker gc: nothing to remove")
    if not args.apply and any(x.startswith("would remove") for x in lines):
        print("(dry run: we-worker gc --apply removes them)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
