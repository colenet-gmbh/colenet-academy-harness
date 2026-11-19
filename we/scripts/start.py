#!/usr/bin/env python3
"""The crew's main companion (``roles.coordinator``): plan its start, check a started session.

Usage:
  start.py plan [--from DIR] [--fresh] [--role ROLE]
                                         one line "<name>\t<pin 0|1>\t<workplace>\t<human>\t<role>\t
                                         <session id or empty>" for ``wclaude --start``; exit 1
                                         (reason on stderr) without a crew, without the role's member,
                                         for a role the human does not run; exit 3 while it runs
                                         ("<name> runs already (<cwd>)")
  start.py side [--from DIR] [--fresh]   the roles ``wclaude --start`` (no ``--role``) opens beside
                                         the main companion, one "<role>\t<name>\t<human>" each; a
                                         running one is a stderr line instead
  start.py roster [--from DIR]           every member with its roles (and human), one line each:
                                         running, startable here with its command, or elsewhere
  start.py check [--from DIR]            in the session (``/we:start``): one line per check of the
                                         human, WESIDE_COMPANION, WE_ROLE and the pin against the
                                         crew, exit 1 on a mismatch
  start.py restore [--from DIR]          the crew's ``launch.restore`` command, exit 1 when unset
  start.py launch <role|member> [--brief FILE] [--fresh] [--consult] [--from DIR]
                                         starts that member's session in the background
                                         (``launch_command``): its name, ``WESIDE_COMPANION``,
                                         ``WE_ROLE`` (``master``; ``worker`` with ``--consult``, a
                                         consultation), ``WE_ROLE_ID``, workplace and pin from the
                                         crew, the brief (default ``READ_STATE``) as first message,
                                         ``--resume`` of its last session unless ``--fresh``; exit 3
                                         while it runs (steer it with SendMessage instead)

The crew decides, never this code: whose machine this is (``crew.current_human``: ``WE_HUMAN``,
else ``git config user.email``), what starts here (``humans.<me>.runs``; a crew without ``humans``
runs every role on one machine), which role ``--start`` opens (``--role``, else
``roles.coordinator`` when the human runs it, else the first role key of ``runs``), under which
name (``crew.display_name``), pinned or not (``members.<slug>.pin``), where
(``members.<slug>.workplace``), the restore command (``launch.restore``). ``plan`` names the
human's other roles on stderr, one ``wclaude --start --role <key>`` each, but for ``side``:
``product`` beside the coordinator when the human runs both. A session runs when ``claude agents
--json`` (interactive tmux sessions included) lists it live under that name, or with that name as its
companion pin (``WESIDE_COMPANION``, the only key read from its ``/proc/<pid>/environ``). The last session is the
newest transcript under ``<config>/projects/<workplace slug>/`` whose title or agent name is the
display name (``--name`` writes both); ``--fresh`` skips the lookup. Python stdlib only, 3.9.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, NamedTuple

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import claude_home  # noqa: E402
import crew as crewlib  # noqa: E402
import worker_launch  # noqa: E402
import worker_supervisor  # noqa: E402

HEAD_BYTES = 64 * 1024
TAIL_BYTES = 256 * 1024
MAX_TRANSCRIPTS = 200


class StartError(Exception):
    pass


class RunningError(StartError):
    """The companion runs already: ``plan`` exits 3, ``wclaude`` starts no second one."""


class Target(NamedTuple):
    """What ``plan`` and ``check`` start or check: the human, the role and its member."""

    path: Path
    crew: dict
    human: str
    human_entry: dict
    role: str
    slug: str
    member: dict
    name: str


def _crew(start: str | Path | None) -> tuple[Path, dict[str, Any]]:
    path = crewlib.find(start)
    if path is None:
        raise StartError("no .weside/crew.json here; run /we:setup")
    try:
        return path, crewlib.load(path)
    except crewlib.CrewError as exc:
        raise StartError(str(exc)) from exc


def startable(crew: dict[str, Any], human: str) -> list[str]:
    """The roles ``human`` runs that a session opens as (``masters`` is no session of its own)."""
    return [r for r in crewlib.runs(crew, human) if r != crewlib.MASTERS]


def target(start: str | Path | None, role: str | None = None, env: Any = None) -> Target:
    env = os.environ if env is None else env
    path, crew = _crew(start)
    try:
        found = crewlib.current_human(crew, env, path.parent)
    except crewlib.CrewError as exc:
        raise StartError(str(exc)) from exc
    if found is None:
        raise StartError(
            "several humans and none is you: set WE_HUMAN=<slug> or git config user.email to "
            f"one of humans.*.email ({', '.join(crewlib.humans(crew))})"
        )
    human, entry = found
    mine = startable(crew, human)
    if role is None and not crewlib.several_humans(crew):
        role = "coordinator"  # one human: the main companion is the coordinator, as ever
    elif role is None:
        role = "coordinator" if "coordinator" in mine else next(iter(mine), None)
        if role is None:
            raise StartError(f"humans.{human}.runs names no role to start")
    elif role not in mine and crewlib.several_humans(crew):
        raise StartError(f"{human} does not run {role} (humans.{human}.runs: {', '.join(mine)})")
    slug = (crew.get("roles") or {}).get(role, role if role not in crewlib.ROLE_KEYS else "")
    member = (crew.get("members") or {}).get(slug or "")
    if not member:
        raise StartError(f"{path}: roles.{role} names no member")
    name = crewlib.display_name(crew, role, human) or member["name"]
    return Target(path, crew, human, entry, role, slug, member, name)


def coordinator(start: str | Path | None) -> tuple[Path, dict[str, Any], str, dict[str, Any]]:
    path, crew = _crew(start)
    slug = (crew.get("roles") or {}).get("coordinator")
    member = (crew.get("members") or {}).get(slug or "")
    if not member:
        raise StartError(f"{path}: roles.coordinator names no member")
    return path, crew, slug, member


def projects_dir(env: Any = None) -> Path:
    env = os.environ if env is None else env
    return claude_home.config_dir(env) / "projects"


def project_slug(cwd: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _holds_name(path: Path, needle: re.Pattern) -> bool:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(HEAD_BYTES)
            if needle.search(head):
                return True
            if size > HEAD_BYTES:
                fh.seek(max(HEAD_BYTES, size - TAIL_BYTES))
                return bool(needle.search(fh.read()))
    except OSError:
        return False
    return False


def last_session(name: str, workplace: Path, projects: Path) -> str:
    folder = projects / project_slug(workplace)
    try:
        files = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return ""
    quoted = re.escape(json.dumps(name)[1:-1]).encode()
    needle = re.compile(rb'"(?:customTitle|agentName)":\s*"' + quoted + rb'"')
    for path in files[:MAX_TRANSCRIPTS]:
        if _holds_name(path, needle):
            return path.stem
    return ""


# the only keys ever read from another process's environment; every other one holds what may be a
# secret and is dropped unread
ENV_KEYS = ("WESIDE_COMPANION", "WE_ROLE", "WE_ROLE_ID")


def env_values(pid: Any, keys: tuple[str, ...] = ENV_KEYS) -> dict[str, str] | None:
    """The ``keys`` (a subset of ``ENV_KEYS``) a live process's environment sets; every other key is
    dropped unread. None where ``/proc`` cannot be read (macOS, another user)."""
    wanted = {k.encode() + b"=": k for k in keys if k in ENV_KEYS}
    found: dict[str, str] = {}
    try:
        with open(f"/proc/{int(pid)}/environ", "rb") as fh:
            for entry in fh.read().split(b"\0"):
                head = entry[: entry.find(b"=") + 1]
                if head in wanted:
                    found[wanted[head]] = entry[len(head) :].decode(errors="replace")
    except (OSError, ValueError, TypeError):
        return None
    return found


def companion_pin(pid: Any) -> str | None:
    """``WESIDE_COMPANION`` of a live process (``env_values``), None when unset or unreadable."""
    return (env_values(pid, ("WESIDE_COMPANION",)) or {}).get("WESIDE_COMPANION")


def _rows(name: str, sessions: Any) -> list[dict]:
    try:
        return (sessions or worker_launch.sessions)()
    except worker_launch.LaunchError as exc:
        raise StartError(
            f"cannot tell whether {name} runs ({exc}); start it by hand or retry"
        ) from exc


def live(name: str, rows: list[dict]) -> dict | None:
    """The live row of that companion: its name (a ``(N)`` suffix is the same companion twice) or its
    companion pin is ``name``; None when none runs."""
    for r in rows:
        if not r.get("pid"):
            continue
        if (
            re.sub(r" \(\d+\)$", "", r.get("name") or "") == name
            or companion_pin(r["pid"]) == name
        ):
            return r
    return None


def running(name: str, sessions: Any = None) -> dict | None:
    """The live session of that companion (``live``). The list comes from ``worker_launch.sessions()``,
    the one reader of ``claude agents --json``; a list that cannot be read refuses the start
    (``StartError``) rather than risk a second main companion."""
    return live(name, _rows(name, sessions))


def _runs_already(name: str, row: dict) -> str:
    return f"{name} runs already ({row.get('cwd') or 'pid ' + str(row.get('pid'))})"


def plan(
    start: str | Path | None,
    fresh: bool,
    env: Any = None,
    sessions: Any = None,
    role: str | None = None,
) -> str:
    t = target(start, role, env)
    row = running(t.name, sessions)
    if row:
        raise RunningError(
            _runs_already(t.name, row) + "; attach to it instead of starting a second one"
        )
    try:
        place = crewlib.workplace_path(t.path, t.crew, t.slug)
    except crewlib.CrewError as exc:
        raise StartError(str(exc)) from exc
    sid = "" if fresh else last_session(t.name, place, projects_dir(env))
    return "\t".join(
        (t.name, "1" if t.member.get("pin") else "0", str(place), t.human, t.role, sid)
    )


SIDE_ROLES = ("product",)  # what ``wclaude --start`` opens beside the coordinator (Foxy, 10.11.)


def side_roles(t: Target) -> list[str]:
    """The roles opened beside the main companion: ``product`` when the human runs it and the main
    companion is the coordinator."""
    if t.role != "coordinator":
        return []
    return [
        r
        for r in SIDE_ROLES
        if r in startable(t.crew, t.human) and (t.crew.get("roles") or {}).get(r)
    ]


def side(
    start: str | Path | None, env: Any = None, sessions: Any = None
) -> tuple[list[str], list[str]]:
    """(rows "<role>\t<name>\t<human>" to open, a "runs already" line for each one that runs)."""
    t = target(start, None, env)
    rows, notes = [], []
    found = None
    for r in side_roles(t):
        name = crewlib.display_name(t.crew, r, t.human) or r
        found = _rows(name, sessions) if found is None else found
        row = live(name, found)
        if row:
            notes.append(_runs_already(name, row))
        else:
            rows.append(f"{r}\t{name}\t{t.human}")
    return rows, notes


def others(start: str | Path | None, role: str | None = None, env: Any = None) -> list[str]:
    """One line per further role the human runs (``side`` aside, when ``role`` is None): how to
    start it."""
    t = target(start, role, env)
    if not crewlib.several_humans(t.crew):
        return []
    skip = {t.role, *(side_roles(t) if role is None else [])}
    return [
        f"{t.human} also runs {r} ({crewlib.display_name(t.crew, r, t.human) or r}): "
        f"wclaude --start --role {r}"
        for r in startable(t.crew, t.human)
        if r not in skip
    ]


def roster(start: str | Path | None, env: Any = None, sessions: Any = None) -> list[str]:
    """One line per companion of the crew, ``<name>: <roles>[ (<human>)] · <state>``: ``runs (<cwd>)``,
    ``start: <command>`` when this human starts it here, else ``runs on <human>'s machine`` or
    ``not started``. A crew without ``humans`` lists every member (one human runs all)."""
    env = os.environ if env is None else env
    path, crew = _crew(start)
    try:
        me = crewlib.current_human(crew, env, path.parent)
    except crewlib.CrewError as exc:
        raise StartError(str(exc)) from exc
    several = crewlib.several_humans(crew)
    role_map = crew.get("roles") or {}
    entries: dict[tuple[str, str], list[str]] = {}  # (human, name) -> roles
    starts: dict[tuple[str, str], str] = {}
    for human in crewlib.humans(crew):
        keys = startable(crew, human)
        if not several:  # one human: every member, a specialist too
            keys = list(dict.fromkeys([*keys, *crewlib.members(crew)]))
        for key in keys:
            if key in role_map or key in crewlib.ROLE_KEYS:
                if not role_map.get(key):
                    continue
                name, label = crewlib.display_name(crew, key, human) or key, key
                command = f"wclaude --start --role {key}"
            else:
                entry = crewlib.members(crew).get(key) or {}
                if key in role_map.values() or not entry:
                    continue  # a role holder is listed under its role
                name = crewlib.display_name(crew, key, human) or key
                label = (
                    (entry.get("specialist") and "specialist") or entry.get("title") or "member"
                )
                command = f"wclaude --as {name}"
            entries.setdefault((human, name), []).append(label)
            starts.setdefault((human, name), command)
    found = _rows("the crew", sessions)
    out = []
    for (human, name), labels in entries.items():
        row = live(name, found)
        who = f" ({human})" if several else ""
        if row:
            state = f"runs ({row.get('cwd') or 'pid ' + str(row.get('pid'))})"
        elif me and me[0] == human:
            state = f"start: {starts[(human, name)]}"
        else:
            state = f"runs on {human}'s machine" if several else "not started"
        out.append(f"{name}: {', '.join(labels)}{who} · {state}")
    return out


def check(start: str | Path | None, env: Any = None) -> tuple[list[str], bool]:
    env = os.environ if env is None else env
    try:
        t = target(start, str(env.get("WE_ROLE_ID") or "").strip() or None, env)
    except StartError as exc:
        return [f"FAIL  human: {exc}"], False
    want_pin = bool(t.member.get("pin"))
    pinned = bool(env.get("CLAUDE_CONFIG_DIR"))
    rows = [
        (
            True,
            f"human {t.human} ({t.human_entry['name']}), runs "
            + ", ".join(crewlib.runs(t.crew, t.human)),
        ),
        (
            env.get("WESIDE_COMPANION") == t.name,
            f"companion {env.get('WESIDE_COMPANION') or '(none)'}, crew: {t.name}",
        ),
        (
            env.get("WE_ROLE") == "master",
            f"WE_ROLE {env.get('WE_ROLE') or '(unset)'}, wanted: master",
        ),
        (
            pinned == want_pin,
            f"pin {'yes' if pinned else 'no'}, crew: {'yes' if want_pin else 'no'}"
            + ("" if pinned == want_pin else f" — /we:pin {'on' if want_pin else 'off'}"),
        ),
    ]
    return [("ok    " if ok else "FAIL  ") + text for ok, text in rows], all(ok for ok, _ in rows)


def _plan(start: str | None, rest: list[str]) -> int:
    role = rest[rest.index("--role") + 1] if "--role" in rest[:-1] else None
    print(plan(start, "--fresh" in rest, role=role))
    for line in others(start, role):
        print(f"wclaude: {line}", file=sys.stderr)
    return 0


def _side(start: str | None, _rest: list[str]) -> int:
    rows, notes = side(start)
    for line in notes:
        print(f"wclaude: {line}", file=sys.stderr)
    if rows:
        print("\n".join(rows))
    return 0


def _roster(start: str | None, _rest: list[str]) -> int:
    print("\n".join(roster(start)))
    return 0


def _check(start: str | None, _rest: list[str]) -> int:
    lines, ok = check(start)
    print("\n".join(lines))
    return 0 if ok else 1


def _restore(start: str | None, _rest: list[str]) -> int:
    path, crew, _, _ = coordinator(start)
    command = (crew.get("launch") or {}).get("restore")
    if not command:
        print(f"start: {path} sets no launch.restore", file=sys.stderr)
        return 1
    print(command)
    return 0


READ_STATE = (
    "Fresh start: before anything else, read your state (your memory file with open commitments, "
    "your board line, the git status of your workplace), then carry on from there."
)
LAUNCH_TIMEOUT_S = 60


def _member_target(start: str | Path | None, who: str, env: Any) -> Target:
    """A role key goes through ``target``; a member slug or name names that member, its role the
    role key it holds (else ``specialist``)."""
    path, crew = _crew(start)
    if who in crewlib.ROLE_KEYS or who in (crew.get("roles") or {}):
        return target(start, who, env)
    found = crewlib.member(crew, who)
    if found is None:
        raise StartError(f"{who} is neither a role key nor a member of {path}")
    slug, entry = found
    held = [r for r, s in (crew.get("roles") or {}).items() if s == slug]
    if held:
        return target(start, held[0], env)
    try:
        human = (crewlib.current_human(crew, env, path.parent) or ("", {}))[0]
    except crewlib.CrewError as exc:
        raise StartError(str(exc)) from exc
    name = crewlib.display_name(crew, slug, human) or entry["name"]
    return Target(path, crew, human, {}, "specialist", slug, entry, name)


def launch_command(
    start: str | Path | None,
    who: str,
    brief: str | None = None,
    fresh: bool = False,
    consult: bool = False,
    env: Any = None,
    sessions: Any = None,
) -> tuple[list[str], dict[str, str], Path, dict, Path]:
    """(argv, environment, settings file, settings, cwd) that start ``who`` in the background, the way ``we-worker
    --bg`` starts a worker: ``claude --bg -n <name> --settings <file>``, the start's variables in the
    settings' ``env`` (a ``--bg`` session runs in the daemon's environment, not this one), every
    ``CLAUDE*`` variable of the caller dropped. A pinned member starts through ``$WCLAUDE_PIN``
    (refused when unset). ``RunningError`` while it runs: steer it with SendMessage instead."""
    env = os.environ if env is None else env
    t = _member_target(start, who, env)
    row = running(t.name, sessions)
    if row:
        raise RunningError(_runs_already(t.name, row) + f"; SendMessage to {t.name} instead")
    try:
        place = crewlib.workplace_path(t.path, t.crew, t.slug)
    except crewlib.CrewError as exc:
        raise StartError(str(exc)) from exc
    text = READ_STATE if brief is None else brief
    if not text.strip():
        raise StartError("the brief is blank; nothing started")
    settings = {
        "env": {
            "WESIDE_COMPANION": t.name,
            # a consultation answers one question and ends (L0 §Sessions); a companion is a master
            "WE_ROLE": "worker" if consult else "master",
            "WE_ROLE_ID": t.role if t.role in crewlib.ROLE_KEYS else "",
            "WE_HUMAN": t.human,
            "WE_START": "",
            "WE_WORKER_STARTER": "",
            "WE_STORY_KEY": "",
        }
    }
    base = ["claude"]
    if t.member.get("pin"):
        if not env.get("WCLAUDE_PIN"):
            raise StartError(f"the crew pins {t.name}, but WCLAUDE_PIN is not set")
        base = shlex.split(str(env["WCLAUDE_PIN"]))
    try:
        safe = worker_supervisor.safe_name(t.name)
    except worker_launch.LaunchError as exc:
        raise StartError(str(exc)) from exc
    # the worker's settings path: worker_supervisor.py gc ages it out once the session is gone
    settings_file = worker_supervisor.state_dir() / f"{safe}.settings.json"
    argv = [*base, "--bg", "-n", t.name, "--settings", str(settings_file)]
    if (t.crew.get("launch") or {}).get("allow_bypass"):
        argv.append("--allow-dangerously-skip-permissions")
    sid = "" if fresh else last_session(t.name, place, projects_dir(env))
    if sid:
        argv += ["--resume", sid]
    argv.append(text)
    child = {k: v for k, v in env.items() if not k.startswith("CLAUDE")}
    return argv, child, settings_file, settings, place


def launch(
    start: str | Path | None,
    who: str,
    brief: str | None = None,
    fresh: bool = False,
    consult: bool = False,
    env: Any = None,
    sessions: Any = None,
    runner: Any = subprocess.run,
) -> str:
    argv, child, settings_file, settings, place = launch_command(
        start, who, brief, fresh, consult, env, sessions
    )
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(settings, indent=2) + "\n")
    try:
        done = runner(argv, cwd=str(place), env=child, check=False, timeout=LAUNCH_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        raise StartError(f"{argv[0]} --bg failed: {exc}") from exc
    if done.returncode != 0:
        raise StartError(f"{argv[0]} --bg exited {done.returncode}; nothing started")
    name = argv[argv.index("-n") + 1]
    return f"{name} starts in the background in {place}; steer it with SendMessage to {name}"


def _launch(start: str | None, rest: list[str]) -> int:
    args = [a for a in rest if not a.startswith("--")]
    for flag in ("--from", "--brief"):
        if flag in rest[:-1]:
            args.remove(rest[rest.index(flag) + 1])
    if len(args) != 1:
        print("start: launch takes one role key or member", file=sys.stderr)
        return 2
    brief = None
    if "--brief" in rest[:-1]:
        try:
            brief = Path(rest[rest.index("--brief") + 1]).read_text(encoding="utf-8")
        except OSError as exc:
            raise StartError(f"brief not readable: {exc}") from exc
    print(launch(start, args[0], brief, "--fresh" in rest, "--consult" in rest))
    return 0


VERBS = {
    "plan": _plan,
    "side": _side,
    "roster": _roster,
    "check": _check,
    "restore": _restore,
    "launch": _launch,
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in VERBS:
        print(__doc__, file=sys.stderr)
        return 2
    rest = argv[1:]
    start = rest[rest.index("--from") + 1] if "--from" in rest[:-1] else None
    try:
        return VERBS[argv[0]](start, rest)
    except RunningError as exc:
        print(f"wclaude: {exc}", file=sys.stderr)
        return 3
    except StartError as exc:
        print(f"start: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
