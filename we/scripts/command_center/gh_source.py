#!/usr/bin/env python3
"""The command centre's GitHub reader: per repo the open PRs with `arm_pr.sh`'s state, the required
names of each PR's base, and the default head's rollup.

No cache of its own: every GraphQL answer goes through `flow_report.gh_runner` in `ci_source`'s
cache directory (made 0700 by `ci_source.own_cache`), keyed by a `TTL_S` bucket, so a second build
inside the bucket asks GitHub nothing and carries the same `fetched_at` (the cached file's
mtime). The required names go through `ci_source.cached_required` with `REQUIRED_TTL_S`.

Repos (`repos_of`): the GitHub `origin` of the crew's repo, then every member's `extra_repos` that
exists here and has a GitHub `origin`.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import ci_source  # noqa: E402
import crew as crew_mod  # noqa: E402
import flow_report as fr  # noqa: E402

TTL_S = 60
REQUIRED_TTL_S = 600


def runner(cache: Path, now: datetime):
    """`flow_report.gh_runner` with the `TTL_S` bucket of `now` as its window key."""
    return fr.gh_runner(cache, f"zentrale:{int(now.timestamp()) // TTL_S}")


def read_repo(run, repo: str, required_reader) -> dict:
    """One repo: `{repo, prs: {n: node}, required: {base: names}, default: {branch, oid, state,
    contexts}}`; raises `SourceMissingError` (and `KeyError`/`TypeError` on a strange answer)."""
    owner, name = repo.split("/", 1)
    data = run({"query": fr.OPEN_PRS_QUERY, "variables": {"owner": owner, "name": name}})
    node = data["data"]["repository"] or {}
    numbers = [n["number"] for n in (node.get("pullRequests") or {}).get("nodes") or [] if n]
    more = bool(((node.get("pullRequests") or {}).get("pageInfo") or {}).get("hasNextPage"))
    prs = fr.open_pr_states(run, repo, numbers)
    required: dict = {}
    for pr in prs.values():
        base = pr.get("baseRefName") or ""
        if base not in required:
            required[base] = sorted(required_reader(repo, base))
    head = node.get("defaultBranchRef") or {}
    target = head.get("target") or {}
    rollup = target.get("statusCheckRollup") or {}
    return {
        "repo": repo,
        "prs": prs,
        "more": more,
        "required": required,
        "default": {
            "branch": head.get("name"),
            "oid": target.get("oid"),
            "state": rollup.get("state"),
            "contexts": (rollup.get("contexts") or {}).get("nodes") or [],
        },
    }


def read_all(repos: list, now: datetime, cache: Path = ci_source.CACHE, reader=None) -> list:
    """`read_repo` per repo with `fetched_at` (the oldest answer it stands on), or `{repo, gap}`."""
    try:
        ci_source.own_cache(cache)
    except OSError as exc:
        return [{"repo": r, "gap": f"cache {cache}: {exc}"} for r in repos]
    out = []
    for repo in repos:
        run = runner(cache, now)

        def required(repo_, base, run_reader=reader):
            return ci_source.cached_required(
                repo_, cache, now, run_reader, branch=base or None, ttl=REQUIRED_TTL_S
            )

        try:
            doc = read_repo(run, repo, required)
        except fr.SourceMissingError as exc:
            out.append({"repo": repo, "gap": str(exc)})
            continue
        except (KeyError, TypeError, ValueError) as exc:
            out.append({"repo": repo, "gap": f"gh: unexpected answer ({exc})"})
            continue
        except OSError as exc:
            out.append({"repo": repo, "gap": f"cache {cache}: {exc}"})
            continue
        stamp = min(run.fetched) if run.fetched else now.timestamp()
        doc["fetched_at"] = datetime.fromtimestamp(stamp, timezone.utc).strftime(ci_source.ISO)
        out.append(doc)
    return out


def repos_of(crew_path: Path | None, crew: dict) -> list:
    """The crew repo's GitHub `origin`, then each member's existing extra repo with one."""
    roots = [Path.cwd()] if crew_path is None else [crew_path.parent.parent]
    for slug in (crew.get("members") or {}) if crew_path is not None else []:
        try:
            present, _missing = crew_mod.extra_repos(crew_path, crew, slug)
        except crew_mod.CrewError:
            continue
        roots += present
    out = []
    for root in roots:
        url = fr.git(root, "remote", "get-url", "origin") or ""
        slug = fr.norm_slug(url) if "github.com" in url or url.count("/") == 1 else None
        if slug and slug not in out:
            out.append(slug)
    return out
