"""ci_source.py against a fake `gh` on PATH that answers from fixtures and counts its calls."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import ci_source  # noqa: E402
import flow_intervals as fi  # noqa: E402
import test_flow_intervals as tfi  # noqa: E402  (its state builder)

UTC = timezone.utc
T0 = datetime(2026, 4, 1, 10, 0, tzinfo=UTC)
REQUIRED = ["Claude Review", "lint", "test"]

FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_GH_DIR"]
with open(os.path.join(d, "calls"), "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
body = json.loads(sys.stdin.read() or "{}")
q = body.get("query", "")
v = body.get("variables") or {}
if "search(" in q:
    name = "open" if "is:open" in v.get("q", "") else "merged"
elif "checkRuns" in q:
    name = "checks"
else:
    name = "suites"
print(open(os.path.join(d, name + ".json")).read())
"""


def z(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def m(minutes):
    return z(T0 + timedelta(minutes=minutes))


def run_node(name, started, completed, conclusion):
    status = "COMPLETED" if completed else ("IN_PROGRESS" if started else "QUEUED")
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "startedAt": started,
        "completedAt": completed,
    }


def fixtures(d: Path) -> None:
    """PR #7 (`ABC-1`): head o1 at T0, head o2 pushed at +55 min while o1's lint still ran."""
    pr = {
        "number": 7,
        "title": "ABC-1 export",
        "body": "",
        "headRefName": "feat/ABC-1-export",
        "state": "OPEN",
        "isDraft": False,
        "createdAt": m(0),
        "mergedAt": None,
        "labels": {"nodes": []},
        "files": {"totalCount": 1, "nodes": [{"path": "a.py"}]},
        "timelineItems": {
            "nodes": [
                {"__typename": "PullRequestCommit", "commit": {"id": "C1", "oid": "o1"}},
                {"__typename": "PullRequestCommit", "commit": {"id": "C2", "oid": "o2"}},
            ]
        },
    }
    page = {"issueCount": 1, "pageInfo": {"hasNextPage": False, "endCursor": None}}
    (d / "open.json").write_text(json.dumps({"data": {"search": {**page, "nodes": [pr]}}}))
    empty = {"issueCount": 0, "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}
    (d / "merged.json").write_text(json.dumps({"data": {"search": empty}}))
    suite = lambda oid, created: {  # noqa: E731
        "oid": oid,
        "checkSuites": {
            "nodes": [
                {
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "createdAt": created,
                    "workflowRun": {"event": "pull_request", "workflow": {"name": "CI"}},
                }
            ]
        },
    }
    (d / "suites.json").write_text(
        json.dumps({"data": {"nodes": [suite("o1", m(0)), suite("o2", m(55))]}})
    )
    checks = {
        "data": {
            "nodes": [
                {
                    "oid": "o1",
                    "checkSuites": {
                        "nodes": [
                            {
                                "createdAt": m(0),
                                "checkRuns": {
                                    "nodes": [
                                        run_node("test", m(40), m(50), "SUCCESS"),
                                        run_node("Claude Review", m(1), m(5), "SUCCESS"),
                                        run_node("lint", m(2), m(55), "CANCELLED"),
                                    ]
                                },
                            }
                        ]
                    },
                    "status": None,
                },
                {
                    "oid": "o2",
                    "checkSuites": {
                        "nodes": [
                            {
                                "createdAt": m(55),
                                "checkRuns": {
                                    "nodes": [
                                        run_node("test", None, None, None),
                                        run_node("Claude Review", m(56), m(60), "SUCCESS"),
                                        run_node("lint", m(57), m(58), "SUCCESS"),
                                    ]
                                },
                            }
                        ]
                    },
                    "status": None,
                },
            ]
        }
    }
    (d / "checks.json").write_text(json.dumps(checks))


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """A fake `gh` first on PATH; returns `calls()`, the number of calls it answered."""
    d = tmp_path / "gh"
    d.mkdir()
    fixtures(d)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(FAKE_GH)
    (bindir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_DIR", str(d))

    def calls():
        f = d / "calls"
        return len(f.read_text().splitlines()) if f.exists() else 0

    return calls


def collect(tmp_path, until, now=None):
    return ci_source.collect(
        "acme/app",
        T0 - timedelta(hours=1),
        until,
        tmp_path / "cache",
        required_reader=lambda _repo: REQUIRED,
        now=now or until,
    )


def test_queue_and_run_per_head(tmp_path, monkeypatch, gh):
    """AC5: queue and run apart per head, a push ends the head, cancelled and never-started
    checks wait to the head's end, reviews named apart, the wait only in idle stretches."""
    until = T0 + timedelta(hours=3)
    doc = collect(tmp_path, until)
    (pr,) = doc["prs"]
    h1, h2 = pr["heads"]
    assert (h1["start"], h1["end"]) == (m(0), m(55))  # the push ends head 1
    assert (h1["queue_s"], h1["run_s"]) == (40 * 60, 15 * 60)
    checks = {c["check"]: c for c in h1["checks"]}
    assert set(checks) == set(REQUIRED)  # the review is a row of its own, by its name
    assert (checks["test"]["created"], checks["test"]["started"], checks["test"]["completed"]) == (
        m(0),
        m(40),
        m(50),
    )
    assert checks["lint"]["end"] == m(55)  # cancelled: waits to the head's end, not zero
    assert (h2["start"], h2["end"]) == (m(55), z(until))  # test never started: open
    assert h2["queue_s"] == 125 * 60 and h2["run_s"] == 0

    # on the lanes: the session holding ABC-1, inside its idle stretches only
    def sessions(t):
        busy = T0 + timedelta(minutes=20) <= t <= T0 + timedelta(minutes=30)
        return [tfi.s("ABC-1-build", "busy" if busy else "idle", key="ABC-1", role="worker")]

    root, board, log_dir, mdir = tfi.make_state(
        tmp_path / "flow",
        monkeypatch=monkeypatch,
        first=T0,
        last=until,
        sessions_at=sessions,
        board_lines=[{"name": "ABC-1-build", "session_id": "sid-ABC-1-build"}],
    )
    crew = json.loads((root / ".weside" / "crew.json").read_text())
    built = fi.build(board, mdir, log_dir, "UTC", crew, T0, until, ci=doc)
    lane = [r for r in built["intervals"] if r["who"] == "ABC-1-build"]
    ci = [r for r in lane if r["kind"] == "waiting:ci"]
    assert ci and all(r["pr"] == 7 for r in ci)
    assert any(r["start"] == m(30) and r["queue_s"] == 40 * 60 for r in ci)
    work = [r for r in lane if r["kind"] == "working"]
    assert work and not any(
        c["start"] < w["end"] and w["start"] < c["end"] for c in ci for w in work
    )
    assert built["ci_line"] == f"CI wait: gh check runs, fetched_at {doc['fetched_at']}"
    page = fi.page_data(built)
    assert page["header"][2].startswith("CI wait: gh check runs, fetched_at ")
    # the legend names only the reserved kinds without rows: waiting:ci has a swatch now
    assert page["reserved"] == ["waiting:lock"]
    assert "'waiting: CI'" in fi.render_page(page)


def test_ttl_cache_and_gap_without_gh(tmp_path, monkeypatch, gh, capsys):
    """AC6: a second run inside the TTL asks gh nothing and keeps the first `fetched_at`; without
    gh the gap is named and no CI row exists; `--no-ci` runs no gh."""
    until = T0 + timedelta(hours=3)
    path = os.environ["PATH"]  # the fake gh first
    first = collect(tmp_path, until)
    n = gh()
    assert n > 0
    again = collect(tmp_path, until, now=until + timedelta(seconds=30))
    assert gh() == n and again["fetched_at"] == first["fetched_at"]
    later = collect(tmp_path, until, now=until + timedelta(minutes=6))
    assert gh() > n  # the open PR's 5-minute bucket is past

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    gone = collect(tmp_path / "other", until)
    assert gone["state"] == "gap" and gone["gap"] == "no data: ci (gh not found)"
    assert later["prs"] and not gone["prs"]

    # a cache directory this user does not own is a gap, never read
    cache = tmp_path / "theirs"
    cache.mkdir()
    uid = cache.stat().st_uid
    monkeypatch.setattr(ci_source.os, "getuid", lambda: uid + 1)
    theirs = ci_source.collect("acme/app", T0, until, cache, required_reader=lambda _r: REQUIRED)
    assert theirs["state"] == "gap" and "not this user" in theirs["gap"]
    monkeypatch.setattr(ci_source.os, "getuid", lambda: uid)
    monkeypatch.setenv("PATH", path)
    root, board, log_dir, mdir = tfi.make_state(
        tmp_path / "flow",
        monkeypatch=monkeypatch,
        first=T0,
        last=until,
        sessions_at=lambda _t: [tfi.s("ABC-1-build", "idle", key="ABC-1")],
        board_lines=[{"name": "ABC-1-build", "session_id": "sid-ABC-1-build"}],
    )
    crew = json.loads((root / ".weside" / "crew.json").read_text())
    built = fi.build(board, mdir, log_dir, "UTC", crew, T0, until, ci=gone)
    assert not any(r["kind"] == "waiting:ci" for r in built["intervals"])
    assert "no data: ci (gh not found)" in [g["reason"] for g in built["gaps"]]
    assert fi.page_data(built)["reserved"] == ["waiting:ci", "waiting:lock"]
    doc = fi.json_doc(built)
    assert doc["ci"]["gap"] == "no data: ci (gh not found)"
    calls = gh()
    assert tfi.run_cli(root, capsys, "--board", str(board), "--json")[0] == 0  # adds --no-ci
    assert gh() == calls


def test_ci_source_json_snapshot(tmp_path, gh):
    """AC7: the document's field list, its `schema_version` and `fetched_at`."""
    doc = collect(tmp_path, T0 + timedelta(hours=3))
    assert sorted(doc) == ["fetched_at", "prs", "repo", "required", "schema_version", "state"]
    assert doc["schema_version"] == 1 and doc["state"] == "gh"
    (pr,) = doc["prs"]
    assert sorted(pr) == ["branch", "heads", "number", "state", "title"]
    assert sorted(pr["heads"][0]) == [
        "branch",
        "checks",
        "end",
        "kind",
        "oid",
        "open",
        "pr",
        "queue_s",
        "run_s",
        "start",
        "state",
    ]
    assert sorted(pr["heads"][0]["checks"][0]) == [
        "check",
        "completed",
        "conclusion",
        "created",
        "end",
        "kind",
        "start",
        "started",
    ]
