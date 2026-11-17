#!/usr/bin/env python3
"""The committed result of one `claude plugin eval` run over evals/: a Markdown table from its
aggregate-result.json (the file `--json <path>` writes).

It keeps per case the with/without score, the delta and the graders that failed, and drops
everything that names the machine: trace paths, the suite root, the plugin path. The repo is
public, so the result file carries no path and no transcript.

Usage:
    python3 scripts/eval_summary.py <aggregate-result.json> [--out evals/results/<date>.md]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAMILY = re.compile(r"^family-(\d+)$")


def _family(case_dir: str) -> str:
    """The `family-<n>` tag of the case file, '-' without one."""
    path = REPO / case_dir / "case.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "-"
    tags = re.search(r"^tags:\s*\[(.*)\]\s*$", text, re.MULTILINE)
    for tag in tags.group(1).split(",") if tags else []:
        found = FAMILY.match(tag.strip())
        if found:
            return found.group(1)
    return "-"


def _model(case_dir: str) -> str:
    """The `model:` the case file pins, '-' without one."""
    try:
        text = (REPO / case_dir / "case.yaml").read_text(encoding="utf-8")
    except OSError:
        return "-"
    found = re.search(r"^\s+model:\s*(\S+)\s*$", text, re.MULTILINE)
    return found.group(1) if found else "-"


def _num(value) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _failed(runs) -> str:
    """`grader xn` for every grader that failed in a run of this arm."""
    counts: dict[str, int] = {}
    for run in runs or []:
        if run.get("error"):
            key = "error: " + str(run["error"])[:60]
            counts[key] = counts.get(key, 0) + 1
        for grader in run.get("graders") or []:
            if grader.get("scored", True) and not grader.get("passed"):
                counts[grader["name"]] = counts.get(grader["name"], 0) + 1
    return ", ".join(f"{name} x{n}" for name, n in sorted(counts.items())) or "—"


def render(result: dict) -> str:
    suite = result.get("suite") or {}
    plugins = suite.get("plugins") or [{}]
    agg = result.get("aggregates") or {}
    cases = result.get("cases") or []
    models = (
        [suite["modelOverride"]]
        if suite.get("modelOverride")
        else sorted({_model(c.get("dir", "")) for c in cases})
    )
    lines = [
        f"# Eval run {str(result.get('startedAt', ''))[:10]}",
        "",
        f"- Claude Code {result.get('claudeVersion', '?')}, plugin `{plugins[0].get('name', '?')}` "
        f"{plugins[0].get('version', '?')}, ablation `{suite.get('ablation', '?')}`",
        f"- runs per case: {sorted({c.get('runsPerCase') for c in cases})}, model: {', '.join(models)}",
        f"- cost {result.get('costUsd', 0):.2f} USD, {result.get('durationSeconds', 0)} s",
        f"- cases passed {agg.get('casesPassed')}/{agg.get('casesTotal')}, mean score with "
        f"{_num(agg.get('overallScore'))}, mean delta with - without {_num(agg.get('meanDelta'))}",
        "",
        "| Case | Family | With | Without | Delta | Failed graders, with | Failed graders, without |",
        "|---|---|---|---|---|---|---|",
    ]
    for case in sorted(cases, key=lambda c: (_family(c.get("dir", "")).zfill(3), c["name"])):
        a = case.get("aggregates") or {}
        arms = case.get("arms") or {}
        lines.append(
            f"| {case['name']} | {_family(case.get('dir', ''))} | {_num(a.get('score'))} | "
            f"{_num(a.get('scoreWithout'))} | {_num(a.get('delta'))} | {_failed(arms.get('with'))} | "
            f"{_failed(arms.get('without'))} |"
        )
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("result")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    text = render(json.loads(Path(args.result).read_text(encoding="utf-8")))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
