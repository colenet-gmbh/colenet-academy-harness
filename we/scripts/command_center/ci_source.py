#!/usr/bin/env python3
"""CI source: how long each PR head waited on CI, split into queue (no runner yet) and run.

Usage:
  ci_source.py [--repo OWNER/NAME] [--since 7d] [--until UTC] [--cache DIR] [--json]

Reads GitHub through ``flow_report``: ``fetch_prs`` (PRs merged in the window and every open PR,
each with its heads), ``required_checks`` (the default branch's required names) and
``CHECKS_QUERY`` (per head commit every check run with its suite's ``createdAt``, ``startedAt``,
``completedAt``). ``flow_intervals.py`` calls ``collect`` and lays the heads on the lanes; this
script prints the same document alone. It writes nothing but its cache.

Contract, per PR head and required check (the latest run of that name on the head): ``created``
= its suite's ``createdAt``, ``started``, ``completed``. The head's ``waiting:ci`` interval runs
from its first required ``created`` to the last required ``completed``; a push ends it earlier (the
next head's first suite: cancel-in-progress), else the window end. A required check that never
started, or ended ``CANCELLED``, waits until the head's end, never zero. ``queue_s`` = the last
required ``started`` - head start (the head's end when one never started); ``run_s`` = the rest.
Every required check is also a row of its own (``check: <name>``), so tests and reviews
(``Claude Review``, ``Codex Review``: required checks too) stay apart by name.

Cache, one layer: every GitHub answer goes through ``flow_report.gh_runner`` (a file per hash of
the request body and its ``window`` key, in ``--cache``, default ``/tmp/we-ci-cache-<uid>``, made
0700; one this user does not own is refused as a gap). The TTL
sits in that key: requests about open PRs carry a 5-minute bucket, about merged PRs a day bucket,
so a run inside the bucket reads the file and a later one asks afresh (an edge is up to 5 min or
24 h old). The required names are kept beside them per repo and day bucket (``required-*.json``),
because ``required_checks.sh`` is a script, not a GraphQL body. ``fetched_at`` is the oldest
answer the document stands on. gh missing or failing raises nothing: the document carries
``state: gap`` and ``gap: "no data: ci (gh …)"``, and no head.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import flow_report as fr  # noqa: E402

SCHEMA_VERSION = 1
# per user under the temp dir: another user's directory of that name is refused, never read
CACHE = Path(tempfile.gettempdir()) / f"we-ci-cache-{os.getuid()}"
OPEN_TTL_S = 300
CLOSED_TTL_S = 24 * 3600
CHECK_BATCH = 10  # CHECKS_QUERY asks 30 suites x 50 runs per commit: 100 commits exceed gh's limit
NOT_DONE = {"CANCELLED", "STALE", "STARTUP_FAILURE"}
ISO = "%Y-%m-%dT%H:%M:%SZ"


def iso(ts: datetime | None) -> str | None:
    return ts.astimezone(timezone.utc).strftime(ISO) if ts else None


def bucketed_runner(cache: Path, now: datetime):
    """`gh_runner` with its window key set by the request: an open-PR search and any check or
    suite query (open heads move) take the 5-minute bucket, a merged-PR search the day bucket."""
    t = int(now.timestamp())
    runners = {
        "open": fr.gh_runner(cache, f"open:{t // OPEN_TTL_S}"),
        "closed": fr.gh_runner(cache, f"closed:{t // CLOSED_TTL_S}"),
    }

    def run(body: dict) -> dict:
        q = str((body.get("variables") or {}).get("q") or "")
        return runners["closed" if "is:merged" in q else "open"](body)

    run.runners = runners  # type: ignore[attr-defined]
    return run


def cached_required(repo: str, cache: Path, now: datetime, reader=None) -> list:
    """`required_checks` through a file per repo and day bucket in the same cache directory."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", repo)
    path = cache / f"required-{slug}-{int(now.timestamp()) // CLOSED_TTL_S}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    names = sorted((reader or fr.required_checks)(repo))
    cache.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(names), encoding="utf-8")
    return names


def own_cache(cache: Path) -> None:
    """Create the cache directory 0700, or refuse one this user does not own (`PermissionError`)."""
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    if cache.stat().st_uid != os.getuid():
        raise PermissionError(f"owned by uid {cache.stat().st_uid}, not this user")


def check_runs(run, ids: list) -> dict:
    """`{oid: {name: {created, started, completed, conclusion}}}`: the latest run of each name."""
    out: dict = {}
    unique = list(dict.fromkeys(ids))
    for i in range(0, len(unique), CHECK_BATCH):
        data = run({"query": fr.CHECKS_QUERY, "variables": {"ids": unique[i : i + CHECK_BATCH]}})
        for node in data["data"]["nodes"]:
            if not node or not node.get("oid"):
                continue
            latest: dict = {}
            for suite in (node.get("checkSuites") or {}).get("nodes") or []:
                created = fr.parse_ts(suite.get("createdAt"))
                for cr in (suite.get("checkRuns") or {}).get("nodes") or []:
                    item = {
                        "created": created,
                        "started": fr.parse_ts(cr.get("startedAt")),
                        "completed": fr.parse_ts(cr.get("completedAt")),
                        "conclusion": cr.get("conclusion"),
                    }
                    low = datetime.min.replace(tzinfo=timezone.utc)
                    held = latest.get(cr["name"])
                    if held is None or (item["created"] or low) >= (held["created"] or low):
                        latest[cr["name"]] = item
            out[node["oid"]] = latest
    return out


def head_rows(pr: dict, heads: list, required: list, until: datetime) -> list:
    """The heads of one PR with their `waiting:ci` contract (module docstring § Contract)."""
    out = []
    for i, head in enumerate(heads):
        checks = head.get("ci") or {}
        mine = {n: checks[n] for n in required if n in checks}
        if not mine:
            continue
        starts = [c["created"] for c in mine.values() if c["created"]]
        if not starts:
            continue
        start = min(starts)
        nxt = None
        if i + 1 < len(heads):
            later = [c["created"] for c in (heads[i + 1].get("ci") or {}).values() if c["created"]]
            nxt = min(later) if later else heads[i + 1].get("first")
        bound = min(x for x in (nxt, until) if x is not None)
        done = [
            c["completed"]
            for c in mine.values()
            if c["completed"] and c["conclusion"] not in NOT_DONE
        ]
        all_done = len(done) == len(mine)
        end = min(max(done), bound) if all_done else bound
        started = [c["started"] for c in mine.values()]
        queue_end = min(max(started), end) if all(started) else end
        queue_s = max(0.0, (queue_end - start).total_seconds())
        total = max(0.0, (end - start).total_seconds())
        rows = []
        for name, c in sorted(mine.items()):
            c_end = c["completed"] if c["completed"] and c["conclusion"] not in NOT_DONE else end
            rows.append(
                {
                    "check": name,
                    "created": iso(c["created"]),
                    "started": iso(c["started"]),
                    "completed": iso(c["completed"]),
                    "conclusion": c["conclusion"],
                    "start": iso(c["created"] or start),
                    "end": iso(min(c_end, end) if c_end else end),
                    "kind": "waiting:ci",
                }
            )
        out.append(
            {
                "pr": pr["number"],
                "branch": pr.get("branch"),
                "state": pr.get("state"),
                "oid": head["oid"],
                "start": iso(start),
                "end": iso(end),
                "kind": "waiting:ci",
                "queue_s": round(queue_s),
                "run_s": round(total - queue_s),
                "open": not all_done and nxt is None,
                "checks": rows,
            }
        )
    return out


def collect(
    repo: str | None,
    since: datetime,
    until: datetime,
    cache: Path = CACHE,
    *,
    required_reader=None,
    now: datetime | None = None,
) -> dict:
    """The CI document; a gap instead of heads when GitHub cannot answer."""
    doc = {"schema_version": SCHEMA_VERSION, "repo": repo, "state": "gh", "fetched_at": None}
    if not repo:
        return {**doc, "state": "gap", "gap": "no data: ci (GitHub repo not found)", "prs": []}
    now = now or datetime.now(timezone.utc)
    try:
        own_cache(cache)
    except OSError as exc:
        return {**doc, "state": "gap", "gap": f"no data: ci (cache {cache}: {exc})", "prs": []}
    run = bucketed_runner(cache, now)
    try:
        required = cached_required(repo, cache, now, required_reader)
        prs = fr.fetch_prs(run, repo, since, until, checks=False)
        heads = [h for p in prs for h in p["heads"]]
        found = check_runs(run, [h["node_id"] for h in heads if h.get("node_id")])
    except fr.SourceMissingError as exc:
        return {**doc, "state": "gap", "gap": f"no data: ci ({exc})", "prs": []}
    except (KeyError, TypeError, ValueError) as exc:
        return {
            **doc,
            "state": "gap",
            "gap": f"no data: ci (gh: unexpected answer {exc})",
            "prs": [],
        }
    except OSError as exc:
        return {**doc, "state": "gap", "gap": f"no data: ci (cache {cache}: {exc})", "prs": []}
    out = []
    for p in prs:
        for h in p["heads"]:
            h["ci"] = found.get(h["oid"], {})
        rows = head_rows(p, p["heads"], required, until)
        out.append(
            {
                "number": p["number"],
                "title": p.get("title"),
                "branch": p.get("branch"),
                "state": p.get("state"),
                "heads": rows,
            }
        )
    times = [t for r in run.runners.values() for t in r.fetched]
    fetched = datetime.fromtimestamp(min(times), timezone.utc) if times else now
    return {**doc, "fetched_at": iso(fetched), "required": required, "prs": out}


def by_key(doc: dict, transitions: list, pattern: str) -> dict:
    """`{story key: [head row]}`: a PR's key from the transitions' `pr` fields, else from its
    branch and title (`flow_report.story_key`)."""
    pr_key: dict = {}
    for t in transitions:
        prs = t.get("pr") if isinstance(t.get("pr"), list) else [t.get("pr")]
        for ref in prs:
            found = re.search(r"/pull/(\d+)|^#?(\d+)$", str(ref or ""))
            if found and isinstance(t.get("key"), str):
                pr_key[int(found.group(1) or found.group(2))] = t["key"]
    out: dict = defaultdict(list)
    for p in doc.get("prs") or []:
        key = pr_key.get(p["number"]) or fr.story_key(
            {"branch": p.get("branch") or "", "title": p.get("title") or ""}, pattern
        )
        if key:
            for h in p["heads"]:
                out[key].append({**h, "key": key})
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ci_source.py", description=__doc__.split("\n")[0])
    p.add_argument("--repo")
    p.add_argument("--since", default="7d")
    p.add_argument("--until")
    p.add_argument("--cache", type=Path, default=CACHE)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    until = now
    if args.until:
        until = fr.parse_ts(args.until if args.until.endswith("Z") else args.until + "Z") or now
    since = fr.flow_intervals.parse_since(args.since, until)
    repo = args.repo or fr.norm_slug(fr.git(Path.cwd(), "remote", "get-url", "origin"))
    doc = collect(repo, since, until, args.cache)
    doc["generated_at"] = iso(now)
    if args.json:
        print(json.dumps(doc, indent=1, ensure_ascii=False))
        return 0
    if doc["state"] == "gap":
        print(doc["gap"])
        return 0
    for pr in doc["prs"]:
        for h in pr["heads"]:
            print(
                f"#{pr['number']} {h['oid'][:8]} {h['start']} → {h['end']}"
                f" queue {h['queue_s'] // 60} min, run {h['run_s'] // 60} min"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
