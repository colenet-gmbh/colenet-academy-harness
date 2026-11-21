"""One redaction, two paths: every pattern of `redaction.PATTERNS` holds in the conversation hook
and in the web view (`command_center/crewd.py`)."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "command_center"))
sys.path.insert(0, str(HERE.parent / "hooks"))
import crewd  # noqa: E402
import redaction  # noqa: E402
import store_conversation_hook as hook  # noqa: E402

# one sample per pattern, in the order of PATTERNS (the count check below binds the two)
SAMPLES = [
    "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
    "AGE-SECRET-KEY-1" + "Q" * 40,
    "sk-ant-" + "b" * 40,
    "ghs_" + "c" * 36,
    "github_pat_" + "d" * 40,
    "sbp_" + "e" * 40,
    "xoxb-" + "1" * 20,
    "AKIA" + "F" * 16,
    "AIza" + "g" * 35,
    "eyJ" + "g" * 20 + "." + "h" * 20 + "." + "i" * 20,
    "Bearer " + "t" * 30,
    "password=hunter2hunter2xyz",
    "someone@example.org",
]
MORE = [
    "sk-" + "a" * 32,
    "ghp_" + "c" * 36,
    "gho_" + "c" * 36,
    "ghu_" + "c" * 36,
    "ghr_" + "c" * 36,
]


def test_every_pattern_has_a_sample():
    assert len(SAMPLES) == len(redaction.PATTERNS)
    for pattern, sample in zip(redaction.PATTERNS, SAMPLES):
        assert pattern.search(sample), pattern.pattern


def test_the_hook_redacts_every_sample():
    for s in SAMPLES + MORE:
        assert s not in hook.redact(f"value {s} end"), s
        assert "[REDACTED]" in hook.redact(f"value {s} end"), s


def test_the_web_view_redacts_every_sample():
    for s in SAMPLES + MORE:
        out = crewd.clean(f"value {s} end")
        assert s not in out and "[redacted]" in out, s


def test_ordinary_text_is_untouched():
    text = "Der Backfill hat 9799 Zeilen gefüllt, sha256 f93e8c0c3310 stimmt."
    assert hook.redact(text) == text and crewd.clean(text) == text
