#!/usr/bin/env python3
"""The command centre ("Zentrale"): what lies with the owner, what is red, who waits on what, who
works on what, what CI and the accounts do, in one read-only document.

Usage:
  zentrale.py [--json] [--fast] [--board FILE] [--tickets FILE] [--until UTC]

Document: `{schema_version, project, built_at, fast, header, gaps, sources, sections}`;
`sections` in display order: `owner` (rows whose holder is the human), `red` (the main verdict,
red PRs, dead or limited workers, red accounts), `waiting` (every other open end), `working`
(`crew_view.py`'s sessions, unchanged), `ci` (per repo the running checks and the default head's
rollup, plus the locks line), `accounts`. Rows follow `open_ends.py`'s row contract; a row both
red and with the owner stands in both sections. Every row, session and running check also
carries `age`, its `age_s` as `crew_view.age_text` writes it (`2h10`): the pane prints this text
and formats no age of its own.

Fast and slow sources. The board, its logs, the flow log and `claude agents --json` (sessions,
workers) are fast; GitHub (`gh_source`), the main command (up to 30 s) and cswap (up to 10 s)
are slow. A full build reads all and keeps the slow answers in `ci_source`'s cache directory
(`zentrale-slow-<project>.json`); `--fast` reads only the fast sources and takes the slow parts
from that file with their `fetched_at`, so a pane draws at once and the full build follows.
Before any full build the slow sources are `loading` gaps.

`sources`: per source its `fetched_at`, or its gap. Every unread source is one `gaps` line; the
build goes on and exits 0. Nothing is written but the cache directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import accounts as accounts_mod  # noqa: E402
import ci_source  # noqa: E402
import ci_state  # noqa: E402
import crew_view  # noqa: E402
import flow_intervals as fi  # noqa: E402
import flow_log  # noqa: E402
import gh_source  # noqa: E402
import open_ends  # noqa: E402
import worker_launch  # noqa: E402
import worker_supervisor  # noqa: E402

SCHEMA_VERSION = 1
SECTIONS = ("owner", "red", "waiting", "working", "ci", "accounts")
SLOW = ("github", "main", "accounts")


def _slow_path(cache: Path, project: str | None) -> Path:
    digest = hashlib.sha256(str(project).encode()).hexdigest()[:12]
    return cache / f"zentrale-slow-{digest}.json"


def read_slow(
    crew_path, crew, project, until, cache, *, repos=None, gh_reader=None, cswap=None
) -> dict:
    """The slow sources, read now."""
    flow = {**(crew.get("flow") or {})}
    slugs = repos if repos is not None else gh_source.repos_of(crew_path, crew)
    return {
        "github": gh_source.read_all(slugs, until, cache, gh_reader),
        "main": ci_state.main_state(
            flow.get("main_state_command"),
            until,
            cache,
            cwd=crew_path.parent.parent if crew_path else None,
        ),
        "accounts": {**accounts_mod.read(cswap), "fetched_at": open_ends.iso(until)},
    }


def load_slow(cache: Path, project) -> dict | None:
    try:
        data = json.loads(_slow_path(cache, project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_slow(cache: Path, project, slow: dict) -> None:
    try:
        ci_source.own_cache(cache)
        _slow_path(cache, project).write_text(json.dumps(slow), encoding="utf-8")
    except OSError:
        pass


def _int_keys(slow: dict) -> dict:
    """JSON turns the PR numbers into strings; give them back as ints."""
    for repo in slow.get("github") or []:
        if isinstance(repo.get("prs"), dict):
            repo["prs"] = {int(k): v for k, v in repo["prs"].items()}
    return slow


def _main_and_accounts(slow: dict, red: list, gaps: list, sources: dict, until) -> list:
    """The main verdict first in `red`, red accounts after; their sources and gaps."""
    main = slow.get("main")
    if main is not None:
        sources["main"] = {
            "fetched_at": main.get("fetched_at"),
            "gap": None if main["state"] in ("green", "red") else main["line"],
        }
        if main["state"] == "red":
            red = [_row("main", "main", main["line"], "main", until), *red]
        elif main["state"] != "green":
            gaps.append(main["line"])
    acc = slow.get("accounts")
    if acc is not None:
        sources["accounts"] = {"fetched_at": acc.get("fetched_at"), "gap": acc.get("gap")}
        if acc.get("gap"):
            gaps.append(acc["gap"])
        red = red + [
            _row("account", "accounts", line, "accounts", until) for line in acc.get("red") or []
        ]
    return red


def _sessions(agents, board_path, until, gaps: list, sources: dict) -> tuple:
    """(red worker rows, the crew view's sessions) from one `claude agents --json` read."""
    try:
        agent_rows = worker_launch.sessions() if agents is None else agents
        sources["sessions"] = {"fetched_at": open_ends.iso(until), "gap": None}
    except worker_launch.LaunchError as exc:
        agent_rows = []
        sources["sessions"] = {"fetched_at": None, "gap": f"sessions: not read ({exc})"}
        gaps.append(sources["sessions"]["gap"])
    records = worker_supervisor.status_records(
        [dict(r) for r in agent_rows],
        None,
        worker_supervisor.projects_dir(),
        worker_supervisor.state_dir(),
        until.timestamp(),
    )
    board = board_path or Path(crew_view.boardlib.default_board())
    working = crew_view.document(
        rows=[dict(r) for r in agent_rows], board=board, now=until.timestamp()
    )["sessions"]
    return ci_state.worker_rows(records, until), working


def build(
    *,
    fast: bool = False,
    until: datetime | None = None,
    cwd: Path | None = None,
    board: str | None = None,
    tickets: str | None = None,
    cache: Path = ci_source.CACHE,
    agents=None,
    repos: list | None = None,
    gh_reader=None,
    cswap: str | None = None,
) -> dict:
    """The document (module docstring). `agents`: `claude agents --json` rows (read when None)."""
    until = until or datetime.now(timezone.utc).replace(microsecond=0)
    cwd = cwd or Path.cwd()
    crew_path, crew = open_ends.load_crew(cwd)
    board_path, _ = fi.locate_board(SimpleNamespace(board=board), cwd, crew_path, crew)
    project = fi.resolve_project(board_path, crew_path)
    sources: dict = {}
    gaps: list = []

    if fast:
        slow = load_slow(cache, project)
        if slow is None:
            slow = {"github": [], "main": None, "accounts": None}
            for name in SLOW:
                sources[name] = {"fetched_at": None, "gap": f"{name}: loading"}
                gaps.append(f"{name}: loading")
        slow = _int_keys(slow)
    else:
        slow = read_slow(
            crew_path, crew, project, until, cache, repos=repos, gh_reader=gh_reader, cswap=cswap
        )
        save_slow(cache, project, slow)

    opened = open_ends.build(
        board_path,
        crew_path,
        crew,
        until,
        ticket_file=Path(tickets) if tickets else None,
        repos=slow["github"],
    )
    gaps += opened["gaps"]
    sources["board"] = {
        "fetched_at": opened["built_at"],
        "gap": next((g for g in opened["gaps"] if g.startswith("board")), None),
    }
    for repo in slow["github"]:
        sources[f"github {repo['repo']}"] = {
            "fetched_at": repo.get("fetched_at"),
            "gap": repo.get("gap"),
        }

    rows = opened["rows"]
    owner = [r for r in rows if r["holder"] == open_ends.OWNER]
    red = [r for r in rows if r["red"]]
    waiting = [r for r in rows if r["holder"] != open_ends.OWNER and not r["red"]]

    red = _main_and_accounts(slow, red, gaps, sources, until)
    worker_red, working = _sessions(agents, board_path, until, gaps, sources)
    red += worker_red

    log_dir, tz, flow = flow_log.settings(crew)
    locks = ci_state.locks(log_dir, tz, float(flow["stale_min"]), until)
    if locks["gates"] is None:
        gaps.append(locks["line"])
    ci = {
        "repos": [ci_state.running(r, until) for r in slow["github"] if not r.get("gap")],
        "locks": locks["line"],
    }

    accounts = (slow.get("accounts") or {}).get("accounts") or []

    for item in [*owner, *red, *waiting, *working, *(c for r in ci["repos"] for c in r["checks"])]:
        item["age"] = crew_view.age_text(item.get("age_s"))  # the one age format, TS prints it
    header = list(opened["header"]) + ([opened["attribution"]] if opened["attribution"] else [])
    return open_ends.clean(
        {
            "schema_version": SCHEMA_VERSION,
            "project": project,
            "built_at": open_ends.iso(until),
            "fast": fast,
            "header": header,
            "gaps": gaps,
            "sources": sources,
            "sections": {
                "owner": owner,
                "red": red,
                "waiting": waiting,
                "working": working,
                "ci": ci,
                "accounts": accounts,
            },
        }
    )


def _row(kind, item, title, source, until) -> dict:
    return {
        "kind": kind,
        "item": item,
        "title": title,
        "holder": kind,
        "for": "",
        "master": "",
        "since": open_ends.iso(until),
        "since_note": "",
        "age_s": 0,
        "source": source,
        "classes": [],
        "red": True,
    }


def text(doc: dict) -> str:
    out = [*doc["header"], *doc["gaps"]]
    labels = {"owner": "With the owner", "red": "Red", "waiting": "Waiting"}
    for key, label in labels.items():
        out.append(f"{label}:")
        rows = doc["sections"][key]
        out += [f"  {r['item']} · {r['holder']} · {r['title']}" for r in rows] or ["  nothing"]
    out.append("Working:")
    out += [f"  {crew_view.text_line(e).strip()}" for e in doc["sections"]["working"]] or [
        "  nothing"
    ]
    out.append("CI:")
    for repo in doc["sections"]["ci"]["repos"]:
        out.append(f"  {repo['repo']}: {repo['default_head']}")
        out += [f"    #{c['pr']} {c['name']} {c['status']}" for c in repo["checks"]]
    out.append(f"  {doc['sections']['ci']['locks']}")
    out.append("Accounts:")
    out += [
        f"  {a['name']} 5h {a['five_hour']['pct']} % 7d {a['seven_day']['pct']} % {a['status']}"
        for a in doc["sections"]["accounts"]
    ] or ["  nothing"]
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="zentrale.py", description=__doc__.split("\n")[0])
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--fast", action="store_true", help="fast sources only, slow ones from the last build"
    )
    p.add_argument("--board")
    p.add_argument("--tickets")
    p.add_argument("--until")
    args = p.parse_args(argv)
    doc = build(
        fast=args.fast,
        until=fi.parse_ts(args.until) if args.until else None,
        board=args.board,
        tickets=args.tickets,
    )
    print(json.dumps(doc, ensure_ascii=False) if args.json else text(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
