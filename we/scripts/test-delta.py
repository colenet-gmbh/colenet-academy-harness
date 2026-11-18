#!/usr/bin/env python3
"""Print the PR body's test line: tests added and removed in the branch against its base.

    test-delta.py <base> [--junit FILE]        e.g. test-delta.py origin/main --junit .pytest.xml

Reads `git diff -M -U0 <base>...HEAD` (from the merge base, renames detected) and prints one line:

    Tests: +N / -M functions, +C / -D cases, slow new: <list|none|n/a> · ratio <r>[ > <max>: say why]
           [· tables > <max>: <file>::<test> (<rows>)…: say why] [· risk path: no cap: <file>…]

A signal for the author and the reviewer (`references/test-economy.md`), never a gate: exit 0
whatever the numbers, 1 when git fails, 2 on a usage error.

Functions, one per definition line, anchored at the line start:
- Python (`.py`): `def test_…(` and `async def test_…(`, methods included.
- JS/TS (`.js .jsx .mjs .cjs .ts .tsx`): `it(` and `test(`, with modifiers such as `.each`,
  `.only`, `.skip`.
A name both removed and added in the diff (a moved test, a reformatted signature) cancels out.

Cases: a Python test function counts the product of its `parametrize` tables (its class's tables
included), read with `ast` from the file at the merge base and at HEAD; a table that is no list or
tuple literal counts one case, and a file that does not parse falls back to one case per
definition. A JS/TS definition counts one case. Cases are matched by test name like functions.

Slow new: with `--junit FILE` (a JUnit XML file of the last local run), the new test functions
whose testcase took longer than `tests.slow_seconds`, slowest first; without the file `n/a`.

Ratio: non-blank lines added in test files over those added in code files (a source extension,
not a test file). Above `tests.max_ratio` it carries `: say why`. Tables: a test whose largest
`parametrize` table grew above `tests.max_parametrize`.

Config: `.weside/config.json` `tests` — `max_parametrize` (default 5), `slow_seconds` (1),
`max_ratio` (1.5), `exclude_paths` (globs no count looks at), `no_cap_paths` (globs that are a
risk path). A file the repo's risk classifier hits (`risk_runner.py`: the crew's `risk.command`,
else `risk_paths.py` over `.weside/risk-paths`) or `no_cap_paths` names is listed as
`risk path: no cap` and stays out of the ratio and the tables; its functions and cases count.

Not counted: `describe` blocks, `.each` table rows, fixtures' `params`, snapshot or golden files,
other languages, a definition line that starts with something other than the patterns above.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import risk_paths  # noqa: E402
import risk_runner  # noqa: E402

_PY = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(")
_JS = re.compile(r"^\s*(?:it|test)(?:\.\w+)*\s*\((.*)$")
_JS_NAME = re.compile(r"""(['"`])(.*?)\1""")
_JS_EXT = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
_CODE_EXT = (
    ".py", *_JS_EXT, ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php", ".cs", ".c", ".h",
    ".cpp", ".sql", ".sh",
)  # fmt: skip
DEFAULTS = {
    "max_parametrize": 5,
    "slow_seconds": 1,
    "max_ratio": 1.5,
    "exclude_paths": [],
    "no_cap_paths": [],
}


def _test_name(path: str, line: str) -> str | None:
    if path.endswith(".py"):
        m = _PY.match(line)
        return m.group(1) if m else None
    if path.endswith(_JS_EXT):
        m = _JS.match(line)
        if not m:
            return None
        literal = _JS_NAME.search(m.group(1))
        return literal.group(2) if literal else m.group(1).strip()
    return None


def is_test_file(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    if any(p in ("tests", "test", "__tests__") for p in parts[:-1]):
        return True
    if name.endswith(".py"):
        return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
    return bool(re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", name))


def globs(patterns: list) -> list:
    out = []
    for glob in patterns:
        if isinstance(glob, str) and glob.strip():
            out += [risk_paths._glob_regex(one) for one in risk_paths._expand(glob.strip())]
    return out


def matches(path: str, patterns: list) -> bool:
    return any(p.match(path) for p in patterns)


# --- the diff -----------------------------------------------------------------------------------


def files(diff: str) -> list:
    """Per file of a `-U0` diff: {path, old, added: [lines], removed: [lines]}."""
    out: list = []
    cur: dict | None = None
    in_header = True
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = line.rsplit(" b/", 1)[-1]
            cur = {"path": path, "old": path, "added": [], "removed": []}
            out.append(cur)
            in_header = True
            continue
        if cur is None:
            continue
        if in_header:
            if line.startswith("rename from "):
                cur["old"] = line[len("rename from ") :]
            elif line.startswith("--- a/"):
                cur["old"] = line[6:]
            elif line.startswith("--- /dev/null"):
                cur["old"] = None
            elif line.startswith("@@"):
                in_header = False
            continue
        if line.startswith("@@"):
            continue
        if line[:1] == "+":
            cur["added"].append(line[1:])
        elif line[:1] == "-":
            cur["removed"].append(line[1:])
    return out


def _count_lines(f: dict, added: Counter, removed: Counter) -> None:
    """One per test definition line of `f`, into `added` and `removed`."""
    for side, lines in ((added, f["added"]), (removed, f["removed"])):
        for line in lines:
            name = _test_name(f["path"], line)
            if name is not None:
                side[name] += 1


def definitions(changed: list) -> tuple[Counter, Counter]:
    added: Counter = Counter()
    removed: Counter = Counter()
    for f in changed:
        _count_lines(f, added, removed)
    return added, removed


# --- cases --------------------------------------------------------------------------------------


def _table_len(call: ast.Call) -> int:
    """Rows of one `parametrize(names, values, …)` call; 1 when the values are no literal."""
    values = call.args[1] if len(call.args) > 1 else None
    for kw in call.keywords:
        if kw.arg == "argvalues":
            values = kw.value
    if isinstance(values, (ast.List, ast.Tuple)):
        return max(1, len(values.elts))
    return 1


def _tables(decorators: list) -> list:
    out = []
    for dec in decorators:
        if (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr == "parametrize"
        ):
            out.append(_table_len(dec))
    return out


def cases_of(source: str) -> dict | None:
    """test name -> (cases, largest table) for one Python file; None when it does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    out: dict = {}

    def walk(body: list, outer: list) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                walk(node.body, outer + _tables(node.decorator_list))
            elif isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and node.name.startswith("test"):
                tables = outer + _tables(node.decorator_list)
                n = 1
                for t in tables:
                    n *= t
                old = out.get(node.name, (0, 0))
                out[node.name] = (old[0] + n, max(old[1], max(tables, default=0)))

    walk(tree.body, [])
    return out


def show(ref: str, path: str | None) -> str | None:
    if not path:
        return ""
    out = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True, check=False
    )
    return out.stdout if out.returncode == 0 else ""


def case_delta(changed: list, base: str, read=show) -> tuple[int, int, list]:
    """(+cases, -cases, [(path, test, rows)] whose largest table grew) over the changed files."""
    before: Counter = Counter()
    after: Counter = Counter()
    grown = []
    for f in changed:
        path = f["path"]
        if path.endswith(_JS_EXT):
            _count_lines(f, after, before)
            continue
        if not path.endswith(".py") or not (f["added"] or f["removed"]):
            continue  # a pure rename changes no case
        old, new = cases_of(read(base, f["old"]) or ""), cases_of(read("HEAD", path) or "")
        if old is None or new is None:  # unparsable: one case per definition line
            _count_lines(f, after, before)
            continue
        for name, (n, _) in old.items():
            before[name] += n
        for name, (n, rows) in new.items():
            after[name] += n
            if rows > old.get(name, (0, 0))[1]:
                grown.append((path, name, rows))
    plus = sum((after - before).values())
    minus = sum((before - after).values())
    return plus, minus, grown


# --- slow, ratio, risk --------------------------------------------------------------------------


def slow_new(junit: str | None, new_names: set, limit: float) -> str:
    if not junit:
        return "n/a"
    try:
        root = ET.parse(junit).getroot()
    except (OSError, ET.ParseError):
        return "n/a"
    times: dict = {}
    for case in root.iter("testcase"):
        name = (case.get("name") or "").split("[", 1)[0]
        try:
            took = float(case.get("time") or 0)
        except ValueError:
            continue
        if name in new_names and took > limit:
            times[name] = max(times.get(name, 0.0), took)
    if not times:
        return "none"
    ranked = sorted(times.items(), key=lambda kv: -kv[1])
    return ", ".join(f"{n} ({t:.1f} s)" for n, t in ranked[:5]) + (" …" if len(ranked) > 5 else "")


def ratio_part(test_lines: int, code_lines: int, limit: float) -> str:
    if not code_lines:
        return "ratio n/a (no code lines)" if test_lines else "ratio n/a"
    r = test_lines / code_lines
    return f"ratio {r:.1f} > {limit:g}: say why" if r > limit else f"ratio {r:.1f}"


def risk_files(base: str) -> tuple[set, str | None]:
    """(files the repo's risk classifier hits, None) or (set(), why) when it cannot run."""
    root = (risk_runner.plain_git(".", "rev-parse", "--show-toplevel") or "").strip()
    if not root:
        return set(), "no git root"
    found = risk_runner.rules(root, risk_runner.plain_git)
    if found is None:
        return set(), None
    if isinstance(found, str):
        return set(), found.split(";", 1)[0].split(": ", 1)[0]
    lines, why = risk_runner.run(root, risk_runner.plain_git, found, "HEAD", base)
    if lines is None:
        return set(), why
    return {line.split("\t", 1)[0] for line in lines[2:] if "\t" in line}, None


def load_config(root: Path) -> dict:
    try:
        doc = json.loads((root / ".weside" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    tests = doc.get("tests") if isinstance(doc, dict) else None
    cfg = dict(DEFAULTS)
    if isinstance(tests, dict):
        cfg.update({k: v for k, v in tests.items() if k in DEFAULTS})
    return cfg


def render(
    plus: int,
    minus: int,
    cplus: int | None = None,
    cminus: int | None = None,
    slow: str = "n/a",
    extra: list | None = None,
) -> str:
    cases = f"+{cplus if cplus is not None else plus} / -{cminus if cminus is not None else minus}"
    line = f"Tests: +{plus} / -{minus} functions, {cases} cases, slow new: {slow}"
    return " · ".join([line, *(extra or [])])


def line_for(diff: str, base: str, cfg: dict, risky: set, junit: str | None, read=show) -> str:
    exclude, no_cap = globs(cfg["exclude_paths"]), globs(cfg["no_cap_paths"])
    changed = [f for f in files(diff) if not matches(f["path"], exclude)]
    added, removed = definitions(changed)
    plus, minus = sum((added - removed).values()), sum((removed - added).values())
    cplus, cminus, grown = case_delta(changed, base, read)
    capped = [f for f in changed if f["path"] not in risky and not matches(f["path"], no_cap)]
    free = [f["path"] for f in changed if f not in capped]
    test_lines = code_lines = 0
    for f in capped:
        n = sum(1 for line in f["added"] if line.strip())
        if is_test_file(f["path"]):
            test_lines += n
        elif f["path"].endswith(_CODE_EXT):
            code_lines += n
    extra = [ratio_part(test_lines, code_lines, float(cfg["max_ratio"]))]
    limit = int(cfg["max_parametrize"])
    big = [f"{p}::{n} ({rows})" for p, n, rows in grown if rows > limit and p not in free]
    if big:
        extra.append(f"tables > {limit}: {', '.join(big)}: say why")
    if free:
        extra.append(f"risk path: no cap: {', '.join(sorted(free))}")
    new_names = set((added - removed).keys())
    slow = slow_new(junit, new_names, float(cfg["slow_seconds"]))
    return render(plus, minus, cplus, cminus, slow, extra)


def main(argv: list[str]) -> int:
    args = argv[1:]
    junit = None
    if "--junit" in args:
        i = args.index("--junit")
        if i + 1 >= len(args):
            print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
            return 2
        junit = args[i + 1]
        del args[i : i + 2]
    if len(args) != 1 or args[0].startswith("-"):
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    base = args[0]
    out = subprocess.run(
        ["git", "diff", "-M", "-U0", "--no-color", "--no-ext-diff", f"{base}...HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        print(f"test-delta: git diff failed: {out.stderr.strip()}", file=sys.stderr)
        return 1
    merge_base = subprocess.run(
        ["git", "merge-base", base, "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
        ).stdout.strip()
        or "."
    )
    risky, why = risk_files(base)
    text = line_for(out.stdout, merge_base or base, load_config(root), risky, junit)
    if why:
        text += f" · risk paths unknown ({why})"
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
