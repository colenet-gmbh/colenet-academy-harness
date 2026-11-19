#!/usr/bin/env python3
"""Which version of the we plugin a session loaded, which one is installed, and the compact handover.

A running Claude Code session keeps the plugin version it started with: its skills, hooks and the
templates its hooks render come from that version's cache folder until the process ends; only a
fresh start loads a newer one (``references/companion-session.md`` § Refresh). This module is the
one reader of both sides:

- ``installs(env)``: the ``we`` entries of ``installed_plugins.json`` in Claude Code's config dir
  (``claude_home.config_dir``).
- ``install_for(cwd, env)``: the entry a fresh start in ``cwd`` loads: the project- or local-scope
  entry of ``cwd`` or its nearest ancestor, else the newest user-scope entry; the same rule as the
  shell resolver ``RESOLVER`` in ``scripts/validate-consistency.py``. ``session_refresh.py`` reads
  it for the installed column, the SessionStart hooks for the compact handover.
- ``loaded_versions(pid, roots)``: the versions a live session process holds. Claude Code writes
  ``<cache>/<publisher>/<plugin>/<version>/.in_use/<pid>`` (JSON ``{pid, procStart}``) for every
  version a process loads; a file counts only when its ``procStart`` is the process's start time
  (``/proc/<pid>/stat`` field 22), so a reused pid is not mistaken for the old session. Chosen over a
  ``plugin_version`` field in ``l0-served.jsonl``: it covers every session (started before this
  release, outside a crew, a worker), needs no write, and reads one directory per version. Not
  readable (macOS, no ``/proc``, a Claude Code that stops writing it): an empty list, read as
  unknown.
- ``handover(hook, hook_input, env, timeout)``: on ``source: compact`` in a session whose own
  plugin root is older than ``install_for``, runs the same hook of the installed version (its
  ``hooks/<hook>``, ``CLAUDE_PLUGIN_ROOT`` set to it, ``WE_HANDOVER_FROM`` set so it never hands on)
  and returns (its ``additionalContext``, own version, installed version); ``None`` on equal versions, another source, an unreadable
  folder, a failing, slow or silent hook: the caller then prints its own text, silently.
  ``stale_line`` is the one line that names the gap.

Python stdlib only, 3.9.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import claude_home  # noqa: E402

PLUGIN = "we"
INSTALLED = Path("plugins") / "installed_plugins.json"
IN_USE = ".in_use"
HANDOVER_ENV = "WE_HANDOVER_FROM"
OWN_ROOT = _HERE.parent


def version_key(text: object) -> tuple[int, ...] | None:
    """``9.4.0`` → ``(9, 4, 0)``; a pre-release suffix is ignored; None without a number."""
    parts = re.findall(r"\d+", str(text or "").split("-", 1)[0])
    return tuple(int(p) for p in parts[:3]) if parts else None


def root_version(root: Path | str) -> str | None:
    """The ``version`` of ``<root>/.claude-plugin/plugin.json``, None when unreadable."""
    try:
        doc = json.loads((Path(root) / ".claude-plugin" / "plugin.json").read_text())
    except (OSError, ValueError):
        return None
    value = doc.get("version") if isinstance(doc, dict) else None
    return str(value) if value else None


def own_root(env: Any = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("CLAUDE_PLUGIN_ROOT") or OWN_ROOT)


def installs(env: Any = None) -> list[dict]:
    """Every ``we`` entry of ``installed_plugins.json``; [] when unreadable."""
    path = claude_home.config_dir(env) / INSTALLED
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [
            e
            for key, rows in (data.get("plugins") or {}).items()
            if key.split("@", 1)[0] == PLUGIN and isinstance(rows, list)
            for e in rows
            if isinstance(e, dict)
        ]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def _holds(entry: dict, here: Path) -> Path | None:
    if entry.get("scope") not in ("project", "local") or not entry.get("projectPath"):
        return None
    place = Path(str(entry["projectPath"])).resolve()
    return place if here == place or place in here.parents else None


def install_for(
    cwd: str | Path, env: Any = None, entries: list[dict] | None = None
) -> dict | None:
    """The entry a fresh start in ``cwd`` loads (module docstring); None without one."""
    rows = installs(env) if entries is None else entries
    here = Path(cwd).resolve()
    near = [(p, e) for e in rows for p in [_holds(e, here)] if p is not None]
    if near:
        return max(near, key=lambda pe: len(pe[0].parts))[1]
    users = [e for e in rows if e.get("scope") == "user" and version_key(e.get("version"))]
    return max(users, key=lambda e: version_key(e.get("version"))) if users else None


def cache_roots(env: Any = None) -> list[Path]:
    """The publisher folders holding ``we`` versions, from every install's ``installPath``."""
    roots = []
    for e in installs(env):
        path = Path(str(e.get("installPath") or ""))
        if e.get("installPath") and path.parent not in roots:
            roots.append(path.parent)
    return roots


def proc_start(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
    except (OSError, ValueError):
        return None
    fields = stat.rsplit(")", 1)[-1].split()
    return fields[19] if len(fields) > 19 else None


def loaded_versions(pid: Any, roots: list[Path]) -> list[str]:
    """The versions process ``pid`` holds (module docstring), oldest first; [] when unknown."""
    try:
        num = int(pid)
    except (TypeError, ValueError):
        return []
    start = proc_start(num)
    if start is None:
        return []
    found = []
    for root in roots:
        try:
            marks = list(root.glob(f"*/{IN_USE}/{num}"))
        except OSError:
            continue
        for mark in marks:
            try:
                doc = json.loads(mark.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and str(doc.get("procStart")) == start:
                found.append(mark.parent.parent.name)
    keyed = [v for v in set(found) if version_key(v)]
    return sorted(keyed, key=version_key)


def stale_line(old: str, new: str) -> str:
    return (
        f"plugin {old} loaded, {new} installed: skills and hooks stay {old} until a fresh start; "
        "save your state and ask the coordinator for a restart"
    )


def newer_root(hook_input: dict, env: Any = None) -> tuple[str, str, Path] | None:
    """(own version, installed version, installed root) when the installed one for this cwd is
    newer than this hook's own root and readable; else None."""
    env = os.environ if env is None else env
    if env.get(HANDOVER_ENV):
        return None
    old = root_version(own_root(env))
    entry = install_for(str(hook_input.get("cwd") or os.getcwd()), env)
    if not entry or not old or not entry.get("installPath"):
        return None
    root = Path(str(entry["installPath"]))
    new = root_version(root)
    if not new or (version_key(new) or ()) <= (version_key(old) or ()):
        return None
    return old, new, root


def handover(
    hook: str, hook_input: dict, env: Any = None, timeout: float = 10
) -> tuple[str, str, str] | None:
    """The installed version's ``hooks/<hook>`` output for this compaction (module docstring)."""
    env = os.environ if env is None else env
    if hook_input.get("source") != "compact":
        return None
    found = newer_root(hook_input, env)
    if found is None:
        return None
    old, new, root = found
    script = root / "hooks" / hook
    if not script.is_file():
        return None
    child = {**env, "CLAUDE_PLUGIN_ROOT": str(root), HANDOVER_ENV: old}
    try:
        done = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(hook_input),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child,
            cwd=str(hook_input.get("cwd") or os.getcwd()),
            check=False,
        )
        data = json.loads(done.stdout) if done.returncode == 0 and done.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    text = ((data or {}).get("hookSpecificOutput") or {}).get("additionalContext")
    return (str(text), old, new) if text else None
