"""test-delta.py: the PR body's test line counts definitions and cases from the merge base, moves
cancel; ratio, table and slow marks, and the per-file risk lift.

Run: `python3 -m pytest -q we/scripts/test_test_delta.py`
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("test-delta.py")
_spec = importlib.util.spec_from_file_location("test_delta", SCRIPT)
delta = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(delta)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _commit(repo: Path, files: dict[str, str | None], msg: str) -> None:
    for name, text in files.items():
        path = repo / name
        if text is None:
            _git(repo, "rm", "-q", name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)


def _run(repo: Path, base: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), base], cwd=repo, capture_output=True, text=True, check=False
    )


OLD_PY = "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n\n\ndef helper():\n    pass\n"
MOVED = "".join(f"def test_moved_{i}():\n    assert {i}\n\n\n" for i in range(5))


def test_counts_added_and_removed_and_cancels_moves(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, {"tests/test_old.py": OLD_PY, "tests/test_moved.py": MOVED}, "base")
    _git(repo, "checkout", "-qb", "feat")
    new_py = (
        OLD_PY.replace("def test_a():", "def test_a(  ):").replace(  # reformatted: cancels
            "def test_b():\n    pass\n", ""
        )  # removed
        + "\n\nasync def test_c():\n    pass\n\n\nclass T:\n    def test_d(self):\n        pass\n"
    )
    js = "describe('x', () => {\n  it('works', () => {});\n  test.each([1])('n %i', () => {});\n});\n"
    _commit(
        repo,
        {
            "tests/test_old.py": new_py,
            "tests/test_moved.py": None,
            "tests/sub/test_moved.py": MOVED,  # moved file: cancels
            "web/a.test.ts": js,
            "README.md": "def test_not_counted():\n  it('nor this', x)\n",
        },
        "feat",
    )
    out = _run(repo, "main")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == (
        "Tests: +4 / -1 functions, +4 / -1 cases, slow new: n/a · ratio n/a (no code lines)"
        " · risk paths unknown (`git fetch origin main` failed)"
    )


def test_render_and_the_ratio_mark() -> None:
    assert delta.render(0, 0) == "Tests: +0 / -0 functions, +0 / -0 cases, slow new: n/a"
    assert delta.render(1, 4, 9, 4, "none", ["ratio 0.5"]) == (
        "Tests: +1 / -4 functions, +9 / -4 cases, slow new: none · ratio 0.5"
    )
    assert delta.ratio_part(30, 20, 1.5) == "ratio 1.5"
    assert delta.ratio_part(31, 20, 1.5) == "ratio 1.6 > 1.5: say why"


def test_cases_multiply_tables_and_a_class_table() -> None:
    src = (
        "import pytest\n"
        "@pytest.mark.parametrize('a', [1, 2, 3])\n"
        "@pytest.mark.parametrize('b', (1, 2))\n"
        "def test_grid(a, b): pass\n"
        "@pytest.mark.parametrize('c', CASES)\n"
        "def test_named(c): pass\n"
        "@pytest.mark.parametrize('d', [1, 2])\n"
        "class TestK:\n"
        "    @pytest.mark.parametrize(argnames='e', argvalues=[1, 2, 3, 4])\n"
        "    def test_e(self, e): pass\n"
    )
    assert delta.cases_of(src) == {"test_grid": (6, 3), "test_named": (1, 1), "test_e": (8, 4)}
    assert delta.cases_of("def test_x(:\n") is None


RULES = "[money]\npath app/billing/**\n"
TABLE = (
    "import pytest\n\n\n@pytest.mark.parametrize('n', [{rows}])\n"
    "def test_{name}(n):\n    assert n\n"
)


def test_risk_files_lift_per_file_and_slow_new_from_junit(tmp_path: Path) -> None:
    """A risk-path file and a no_cap_paths file leave the ratio and the table mark; the other
    tests of the same PR stay capped. An excluded path counts nowhere."""
    origin, repo = tmp_path / "origin.git", tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    config = {
        "tests": {
            "max_ratio": 1.0,
            "no_cap_paths": ["tests/auth/**"],
            "exclude_paths": ["tests/archive/**"],
        }
    }
    _commit(
        repo,
        {
            ".weside/risk-paths": RULES,
            ".weside/config.json": json.dumps(config),
            "app/core.py": "x = 1\n",
        },
        "base",
    )
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(origin)], check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "checkout", "-qb", "feat")
    rows = ", ".join(str(i) for i in range(8))
    _commit(
        repo,
        {
            "app/billing/charge.py": "".join(f"a{i} = {i}\n" for i in range(20)),
            "app/billing/test_charge.py": TABLE.format(rows=rows, name="amounts"),
            "tests/auth/test_roles.py": TABLE.format(rows=rows, name="roles"),
            "tests/test_core.py": TABLE.format(rows=rows, name="core"),
            "tests/archive/test_old.py": TABLE.format(rows=rows, name="old"),
            "app/core.py": "x = 1\ny = 2\n",
        },
        "feat",
    )
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuite><testcase classname="tests.test_core" name="test_core[0]" time="2.5"/>'
        '<testcase classname="tests.test_core" name="test_core[1]" time="0.2"/>'
        '<testcase classname="app.billing" name="test_amounts[3]" time="0.4"/>'
        '<testcase classname="tests.test_x" name="test_untouched" time="9"/></testsuite>'
    )
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "origin/main", "--junit", str(junit)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == (
        "Tests: +3 / -0 functions, +24 / -0 cases, slow new: test_core (2.5 s)"
        " · ratio 4.0 > 1: say why · tables > 5: tests/test_core.py::test_core (8): say why"
        " · risk path: no cap: app/billing/charge.py, app/billing/test_charge.py,"
        " tests/auth/test_roles.py"
    )


def test_bad_base_fails_with_message(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, {"a.py": "x = 1\n"}, "base")
    out = _run(repo, "no-such-ref")
    assert out.returncode == 1
    assert "test-delta: git diff failed" in out.stderr
