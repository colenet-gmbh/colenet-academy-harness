"""The swimlanes draw every lane kind `flow_intervals.py` writes: its `LANE_KINDS` and the pane's
`GLYPH` keys (`hooks/board/zentrale.ts`) are one list."""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import flow_intervals as fi  # noqa: E402

PANE = HERE.parents[1] / "hooks" / "board" / "zentrale.ts"


def test_glyph_keys_are_the_lane_kinds():
    block = re.search(r"const GLYPH[^=]*= \{(.*?)\n\}", PANE.read_text(), re.DOTALL)
    assert block, "GLYPH not found in zentrale.ts"
    keys = re.findall(r"^\s*'?([\w:<>-]+)'?: \{", block.group(1), re.MULTILINE)
    assert keys == list(fi.LANE_KINDS)
