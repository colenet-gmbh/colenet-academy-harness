"""scripts/eval_summary.py: the committed eval result names no path and keeps the delta."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "eval_summary", Path(__file__).resolve().parent / "eval_summary.py"
)
eval_summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eval_summary)

RESULT = {
    "claudeVersion": "9.9.9",
    "startedAt": "2026-11-17T22:00:00Z",
    "costUsd": 1.5,
    "durationSeconds": 60,
    "suite": {
        "root": "/home/user/repo",
        "ablation": "with-without",
        "plugins": [{"name": "we", "version": "1.2.3", "path": "/home/user/repo/we"}],
    },
    "cases": [
        {
            "name": "worker-main-checkout",
            "dir": "evals/worker-main-checkout",
            "runsPerCase": 2,
            "aggregates": {"score": 0.5, "scoreWithout": 0.0, "delta": 0.5},
            "arms": {
                "with": [
                    {
                        "tracePath": "/tmp/x/trace.jsonl",
                        "graders": [
                            {"name": "moves-to-a-worktree", "passed": False, "scored": True},
                            {"name": "no-branch-switch", "passed": True, "scored": True},
                        ],
                    },
                ],
                "without": [
                    {
                        "tracePath": "/tmp/y/trace.jsonl",
                        "error": None,
                        "graders": [
                            {"name": "moves-to-a-worktree", "passed": False, "scored": True}
                        ],
                    },
                ],
            },
        }
    ],
    "aggregates": {"casesPassed": 0, "casesTotal": 1, "overallScore": 0.5, "meanDelta": 0.5},
}


def test_the_table_keeps_scores_delta_and_failed_graders():
    text = eval_summary.render(RESULT)
    row = next(line for line in text.splitlines() if line.startswith("| worker-main-checkout"))
    assert "| 2 | 0.50 | 0.00 | 0.50 | moves-to-a-worktree x1 | moves-to-a-worktree x1 |" in row
    assert "mean delta with - without 0.50" in text
    assert "plugin `we` 1.2.3" in text


def test_no_path_of_the_machine_reaches_the_file():
    text = eval_summary.render(RESULT)
    assert "/home/" not in text and "/tmp/" not in text and "trace" not in text


def test_a_missing_value_prints_na_not_zero():
    result = dict(RESULT, cases=[dict(RESULT["cases"][0], aggregates={"score": 1.0})])
    row = next(
        line for line in eval_summary.render(result).splitlines() if line.startswith("| worker")
    )
    assert "| 1.00 | n/a | n/a |" in row
