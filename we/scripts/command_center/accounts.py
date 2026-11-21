#!/usr/bin/env python3
"""The accounts of the command centre: quotas from cswap's store, through an allowlist.

Usage:
  accounts.py [--json]

One call per run, always `cswap list --json --store-only` (timeout `TIMEOUT_S`): `--store-only`
reads cswap's stores without a network request, a token refresh or a write; a plain `cswap list
--json` may refresh a token and write credentials and its usage store, so it is never called, not
as a fallback either. A cswap that refuses the flag (exit 2 naming it) gives `accounts: not read
(cswap without --store-only)`, a missing one `accounts: not read (no cswap)`.

Each account passes `allow` and carries only `ALLOWED`: `name` (the alias, else `account <n>` from cswap's slot
number; never the email, which the web view redacts to one indistinguishable marker),
`active`, `in_rotation` (not `disabled`), `five_hour` and `seven_day` (`{pct, resets_at}`),
`threshold`, `status` (cswap's `usageStatus`), `measured_at` and `age_s` (the store entry's
`storeFetchedAt`/`storeAgeSeconds`, null without one). Every other key (tokens, organisation,
plan, billing) is dropped at every depth.

Red: an account whose `status` is not `ok` (in store-only mode a healable strike stays, so `ok`
is the only green, not the absence of `relogin_required`), or every account in rotation at or over
its threshold (no target left; an account without a threshold counts at 100 %). A store entry
older than `ACCOUNTS_STALE`, or none, is `stale`: never green.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import timedelta

SCHEMA_VERSION = 1
TIMEOUT_S = 10
# cswap refreshes its own store every few minutes while any session runs it; half an hour without
# a reading means nobody reads.
ACCOUNTS_STALE = timedelta(minutes=30)
ARGV = ("list", "--json", "--store-only")
ALLOWED = (
    "name",
    "active",
    "in_rotation",
    "five_hour",
    "seven_day",
    "threshold",
    "status",
    "measured_at",
    "age_s",
    "stale",
    "red",
)


def window(usage: dict, key: str) -> dict:
    w = (usage or {}).get(key) or {}
    pct = w.get("pct")
    return {
        "pct": float(pct) if isinstance(pct, (int, float)) else None,
        "resets_at": w.get("resetsAt") if isinstance(w.get("resetsAt"), str) else None,
    }


def allow(raw: dict) -> dict:
    """One account through the allowlist (module docstring)."""
    alias = raw.get("alias")
    age = raw.get("storeAgeSeconds")
    age = int(age) if isinstance(age, (int, float)) else None
    threshold = raw.get("threshold")
    number = raw.get("number")
    slot = number if isinstance(number, int) and not isinstance(number, bool) else "?"
    return {
        "name": alias if isinstance(alias, str) and alias else f"account {slot}",
        "active": raw.get("active") is True,
        "in_rotation": raw.get("disabled") is not True,
        "five_hour": window(raw.get("usage") or {}, "fiveHour"),
        "seven_day": window(raw.get("usage") or {}, "sevenDay"),
        "threshold": float(threshold) if isinstance(threshold, (int, float)) else None,
        "status": str(raw.get("usageStatus") or "unknown"),
        "measured_at": raw.get("storeFetchedAt")
        if isinstance(raw.get("storeFetchedAt"), str)
        else None,
        "age_s": age,
        "stale": age is None or age > ACCOUNTS_STALE.total_seconds(),
        "red": False,
    }


def at_limit(a: dict) -> bool:
    limit = a["threshold"] if a["threshold"] is not None else 100.0
    pcts = [w["pct"] for w in (a["five_hour"], a["seven_day"]) if w["pct"] is not None]
    return bool(pcts) and max(pcts) >= limit


def judge(accounts: list) -> tuple:
    """(accounts with `red` set, red lines)."""
    lines = []
    for a in accounts:
        if a["status"] != "ok":
            a["red"] = True
            label = a["name"] if a["name"].startswith("account ") else f"account {a['name']}"
            lines.append(f"{label}: {a['status']}")
    rotation = [a for a in accounts if a["in_rotation"]]
    if rotation and all(at_limit(a) for a in rotation):
        lines.append("accounts: every account in rotation at its threshold")
        for a in rotation:
            a["red"] = True
    return accounts, lines


def _call(binary: str, run) -> tuple:
    """(cswap's JSON payload, None) or (None, gap line) of the one store-only call."""
    try:
        out = run([binary, *ARGV], capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except FileNotFoundError:
        return None, "accounts: not read (no cswap)"
    except subprocess.TimeoutExpired:
        return None, "accounts: not read (cswap timeout)"
    except OSError as exc:
        return None, f"accounts: not read ({exc.strerror or exc})"
    refused = "--store-only" in (out.stderr or "") or "unrecognized" in (out.stderr or "")
    if out.returncode != 0:
        why = "cswap without --store-only" if refused else f"cswap exit {out.returncode}"
        return None, f"accounts: not read ({why})"
    try:
        data = json.loads(out.stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("storeOnly") is not True:
        why = "cswap printed no JSON" if data is None else "cswap without --store-only"
        return None, f"accounts: not read ({why})"
    return data, None


def read(cswap: str | None = None, run=subprocess.run) -> dict:
    """`{schema_version, state: read|gap, gap, accounts, red}`; one cswap call at most."""
    doc = {
        "schema_version": SCHEMA_VERSION,
        "state": "gap",
        "gap": None,
        "accounts": [],
        "red": [],
    }
    binary = cswap or shutil.which("cswap")
    if not binary:
        return {**doc, "gap": "accounts: not read (no cswap)"}
    data, gap = _call(binary, run)
    if gap:
        return {**doc, "gap": gap}
    raw = [a for a in data.get("accounts") or [] if isinstance(a, dict)]
    accounts, red = judge([allow(a) for a in raw])
    return {**doc, "state": "read", "accounts": accounts, "red": red}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="accounts.py", description=__doc__.split("\n")[0])
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    doc = read()
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1))
        return 0
    if doc["gap"]:
        print(doc["gap"])
    for a in doc["accounts"]:
        print(f"{a['name']} 5h {a['five_hour']['pct']} 7d {a['seven_day']['pct']} {a['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
