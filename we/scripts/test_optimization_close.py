"""optimization_close.py against a fixture store: one entry closed by a merged PR, one expired,
one left open."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent / "optimization_close.py"


def entry(key, *evidence):
    lines = "\n".join(f"- {e}" for e in evidence)
    return (
        f"---\nkey: {key}\npattern: gap\ntarget: AGENTS.md\ntarget_repo: this\naction: add\n"
        f"confidence: Medium\nsource: retro\n---\n\nThe finding.\n\n## Evidence\n\n{lines}\n"
    )


def store(tmp_path):
    s = tmp_path / ".weside" / "optimization"
    (s / "inbox").mkdir(parents=True)
    (s / "inbox" / "2026-11-10-gap--agents-md.md").write_text(
        entry("gap--agents-md", "2026-11-10 retro pr-7: missed", "2026-11-12 retro pr-9: again")
    )
    (s / "inbox" / "2026-10-20-g2-volatile--readme-md.md").write_text(
        entry("g2-volatile--readme-md", "2026-10-20 audit a.md #1: stale")
    )
    (s / "inbox" / "2026-11-15-gap--rules-x-md.md").write_text(
        entry("gap--rules-x-md", "2026-11-15 retro pr-11: seen once")
    )
    (s / "inbox" / "2026-10-01-gap--old-md.md").write_text(
        entry("gap--old-md", "2026-10-01 retro pr-1: a", "2026-10-03 retro pr-2: b")
    )
    (s / "LEDGER.md").write_text(
        "# Ledger\n\n| date | key | target | decision | evidence | metric | before → after | review_by | change |\n"
        "|---|---|---|---|---|---|---|---|---|\n| 2026-10-01 | k | t | rejected | e | — | — | — | — |\n"
    )
    prs = [
        {
            "number": 41,
            "title": "docs: rule for gap--agents-md",
            "body": "",
            "url": "https://x/pull/41",
            "mergedAt": "2026-11-14T10:00:00Z",
        },
        {
            "number": 7,
            "title": "older",
            "body": "applies GAP--RULES-X-MD",
            "url": "https://x/pull/7",
            "mergedAt": "2026-11-01T10:00:00Z",
        },
        {
            "number": 9,
            "title": "retro: gap--rules-x-md",
            "body": "",
            "url": "https://x/pull/9",
            "mergedAt": "2026-11-16T10:00:00Z",
            "files": [{"path": ".weside/optimization/inbox/2026-11-15-gap--rules-x-md.md"}],
        },
        {
            "number": 8,
            "title": "near miss",
            "body": "gap--agents-md-extra",
            "url": "https://x/pull/8",
            "mergedAt": "2026-11-16T10:00:00Z",
        },
    ]
    (tmp_path / "prs.json").write_text(json.dumps(prs))
    return s


def run(tmp_path, s, *extra):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--store",
            str(s),
            "--prs-json",
            str(tmp_path / "prs.json"),
            "--today",
            "2026-11-17",
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_one_applied_by_a_merged_pr_one_expired_the_rest_open(tmp_path):
    s = store(tmp_path)
    out = run(tmp_path, s)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == [
        "expired g2-volatile--readme-md · 28 days, 1 evidence line(s)",
        "applied gap--agents-md · https://x/pull/41",
        "closed 1 applied, 1 expired, 2 open",
    ]
    assert (
        sorted(p.name for p in (s / "inbox").iterdir())
        == [
            "2026-10-01-gap--old-md.md",  # old, but two evidence lines
            "2026-11-15-gap--rules-x-md.md",  # one line, young; one PR naming it is older, one a store PR
        ]
    )
    rows = (s / "LEDGER.md").read_text().splitlines()
    assert (
        rows[4]
        == "| 2026-11-17 | g2-volatile--readme-md | AGENTS.md | expired | 1 evidence line(s) in 28 days | — | — | — | — |"
    )
    assert rows[5] == (
        "| 2026-11-17 | gap--agents-md | AGENTS.md | applied | merged PR https://x/pull/41 | recurrence"
        " | 2 → pending | 2026-12-17 | https://x/pull/41 |"
    )
    assert rows[6].startswith("| 2026-10-01 | k |")


def test_expire_days_comes_from_the_config(tmp_path):
    s = store(tmp_path)
    (s.parent / "config.json").write_text(json.dumps({"optimization": {"expire_days": 60}}))
    out = run(tmp_path, s)
    assert out.stdout.splitlines()[-1] == "closed 1 applied, 0 expired, 3 open"


def test_a_missing_ledger_is_created_with_its_header(tmp_path):
    s = store(tmp_path)
    (s / "LEDGER.md").unlink()
    assert run(tmp_path, s).returncode == 0
    rows = (s / "LEDGER.md").read_text().splitlines()
    assert rows[0].startswith("| date | key |") and len(rows) == 4


def test_no_inbox_exits_2(tmp_path):
    (tmp_path / "prs.json").write_text("[]")
    out = run(tmp_path, tmp_path / "none")
    assert out.returncode == 2 and "no inbox" in out.stderr
