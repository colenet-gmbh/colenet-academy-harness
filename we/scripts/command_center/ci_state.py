#!/usr/bin/env python3
"""CI and red of the command centre: the main verdict, dead or limited workers, running checks,
the gate lock and the test slot. Read-only.

Usage:
  ci_state.py [--until UTC] [--no-gh] [--json]

- **Main verdict** (`main_state`): the crew's `flow.main_state_command` runs (no shell, timeout
  `MAIN_TIMEOUT_S`); exit 0 is green (nothing red), any other exit is red with the command's first
  line (`main: <line>`), a timeout or a start failure is `main state: unknown (<why>)`, never
  green. The answer is kept `MAIN_TTL_S` in `ci_source`'s cache directory. Without the key:
  `main state: not read (no source)`.
- **Workers** (`worker_rows`): `worker_supervisor.status_records`; an exited worker, and one whose
  last answer is a `usage-limit` or `auth` end, is red with its kind and its resume command.
- **Running checks** (`running`): per repo of `gh_source.read_all` each pending check on an open
  PR's head (name, PR, since), and the default head's rollup as `checks on default head`.
- **Locks** (`locks`): the last `sample` line of the flow log (`flow_log.py`: `locks.gates` and
  `locks.tests`, `{held, holder}`) with its age; older than `flow.stale_min` it reads
  `sampler stale (<n> min)` and no lock state. Without `flow.log_dir`: `locks: not read
  (flow.log_dir unset)`.

`schema_version` counts the shape; `test_zentrale.py` pins the field list.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import ci_source  # noqa: E402
import flow_intervals as fi  # noqa: E402
import flow_log  # noqa: E402
import flow_report as fr  # noqa: E402
import worker_supervisor  # noqa: E402

SCHEMA_VERSION = 1
MAIN_TIMEOUT_S = 30
MAIN_TTL_S = 300
RED_ENDS = worker_supervisor.RESUMABLE  # usage-limit, auth


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime(ci_source.ISO)


def main_state(
    command: str | None, now: datetime, cache: Path = ci_source.CACHE, cwd=None
) -> dict:
    """`{state: green|red|unknown|unread, line, fetched_at}` (module docstring § Main verdict)."""
    if not command:
        return {"state": "unread", "line": "main state: not read (no source)", "fetched_at": None}
    # the repo it runs in belongs to the key: two crews with one relative command differ
    digest = hashlib.sha256(f"{cwd}\n{command}".encode()).hexdigest()[:16]
    path = cache / f"main-state-{digest}-{int(now.timestamp()) // MAIN_TTL_S}.json"
    try:
        ci_source.own_cache(cache)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        path = None
    try:
        out = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            timeout=MAIN_TIMEOUT_S,
            check=False,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return {
            "state": "unknown",
            "line": "main state: unknown (timeout)",
            "fetched_at": iso(now),
        }
    except (OSError, ValueError) as exc:
        return {"state": "unknown", "line": f"main state: unknown ({exc})", "fetched_at": iso(now)}
    first = next((x.strip() for x in out.stdout.splitlines() if x.strip()), "")
    if out.returncode == 0:
        doc = {"state": "green", "line": first, "fetched_at": iso(now)}
    else:
        doc = {
            "state": "red",
            "line": f"main: {first or 'exit ' + str(out.returncode)}",
            "fetched_at": iso(now),
        }
    if path is not None:
        with contextlib.suppress(OSError):
            path.write_text(json.dumps(doc), encoding="utf-8")
    return doc


def worker_rows(records: list, until: datetime) -> list:
    """Red rows (`open_ends` row contract) for exited and limited workers."""
    out = []
    for r in records:
        kind = r["end"] if r["end"] in RED_ENDS else ("exited" if not r["alive"] else None)
        if kind is None:
            continue
        out.append(
            {
                "kind": "worker",
                "item": r["name"],
                "title": kind + (f" · {r['resume']}" if r["resume"] else ""),
                "holder": r["name"],
                "for": "",
                "master": "",
                "since": "unknown",
                "since_note": "",
                "age_s": None,
                "source": "workers",
                "classes": [kind],
                "red": True,
            }
        )
    return out


def running(repo_doc: dict, until: datetime) -> dict:
    """One repo's pending checks on open PR heads and its default head's rollup."""
    checks = []
    for number, pr in sorted(repo_doc["prs"].items()):
        for c in fr.head_contexts(pr):
            if c["v"] != "pending":
                continue
            since = fi.parse_ts(c["since"])
            checks.append(
                {
                    "pr": number,
                    "name": c["name"],
                    "status": c["status"],
                    "since": c["since"],
                    "age_s": int((until - since).total_seconds()) if since else None,
                }
            )
    default = repo_doc["default"]
    return {
        "repo": repo_doc["repo"],
        "fetched_at": repo_doc.get("fetched_at"),
        "default_head": f"checks on default head: {(default.get('state') or 'none').lower()}",
        "checks": checks,
    }


def last_sample(log_dir: Path, tz: str, until: datetime) -> dict | None:
    """The newest `sample` line of today's or yesterday's flow log file."""
    for back in (0, 1):
        path = flow_log.path_for(log_dir, until - timedelta(days=back), tz)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in reversed(lines):
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("type") == "sample":
                return item
    return None


def locks(log_dir: Path | None, tz: str, stale_min: float, until: datetime) -> dict:
    """`{line, gates, tests, age_s}` (module docstring § Locks)."""
    if log_dir is None:
        return {
            "line": "locks: not read (flow.log_dir unset)",
            "gates": None,
            "tests": None,
            "age_s": None,
        }
    sample = last_sample(log_dir, tz, until)
    ts = fi.parse_ts((sample or {}).get("ts"))
    if ts is None:
        return {
            "line": "locks: not read (no sample line)",
            "gates": None,
            "tests": None,
            "age_s": None,
        }
    age = int((until - ts).total_seconds())
    if age > stale_min * 60:
        return {
            "line": f"sampler stale ({age // 60} min)",
            "gates": None,
            "tests": None,
            "age_s": age,
        }
    held = sample.get("locks") or {}

    def lock(name: str) -> str:
        entry = held.get(name) or {}
        if not entry.get("held"):
            return "free"
        return f"held by {entry.get('holder') or '?'}"

    return {
        "line": f"gate lock {lock('gates')} · test slot {lock('tests')} · sample {age // 60} min old",
        "gates": lock("gates"),
        "tests": lock("tests"),
        "age_s": age,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ci_state.py", description=__doc__.split("\n")[0])
    p.add_argument("--until")
    p.add_argument("--no-gh", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    import zentrale  # noqa: PLC0415  (the aggregate owns the source wiring)

    doc = zentrale.build(fast=args.no_gh, until=fi.parse_ts(args.until) if args.until else None)
    out = {
        "schema_version": SCHEMA_VERSION,
        "red": doc["sections"]["red"],
        "ci": doc["sections"]["ci"],
    }
    print(json.dumps(out, ensure_ascii=False, indent=1 if args.json else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
