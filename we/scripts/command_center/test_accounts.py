"""accounts.py against a fake `cswap` script: the allowlist with its red arm, and the one call."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import accounts  # noqa: E402

TOKEN = "sk-ant-oat01-FAKE-TOKEN-VALUE"
PAYLOAD = {
    "schemaVersion": 1,
    "storeOnly": True,
    "activeAccountNumber": 1,
    "accounts": [
        {
            "number": 1,
            "email": "one@example.com",
            "alias": "dev",
            "active": True,
            "usageStatus": "ok",
            "threshold": 80.0,
            "accessToken": TOKEN,
            "refreshToken": TOKEN + "-refresh",
            "organizationUuid": "org-uuid-1",
            "plan": {"label": "Max 5x", "billingType": "stripe"},
            "usage": {
                "fiveHour": {"pct": 40.0, "resetsAt": "2026-04-01T14:00:00Z", "token": TOKEN},
                "sevenDay": {"pct": 70.0, "resetsAt": "2026-04-05T10:00:00Z"},
            },
            "storeFetchedAt": "2026-04-01T11:58:00Z",
            "storeAgeSeconds": 120,
        },
        {
            "number": 2,
            "email": "two@example.com",
            "active": False,
            "usageStatus": "token_expired",
            "usage": {"fiveHour": {"pct": 10.0, "resetsAt": None}},
            "storeFetchedAt": "2026-04-01T11:00:00Z",
            "storeAgeSeconds": 3600,
        },
    ],
}

FAKE = """#!/usr/bin/env python3
import json, os, sys
d = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(d, "calls"), "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
mode = open(os.path.join(d, "mode")).read().strip()
if mode == "old" and "--store-only" in sys.argv:
    sys.stderr.write("usage: cswap list [-h] [--json]\\ncswap: error: unrecognized arguments: --store-only\\n")
    sys.exit(2)
print(open(os.path.join(d, "payload.json")).read())
"""


def fake_cswap(tmp_path: Path, mode="new", payload=PAYLOAD) -> Path:
    exe = tmp_path / "cswap"
    exe.write_text(FAKE)
    exe.chmod(0o755)
    (tmp_path / "mode").write_text(mode)
    (tmp_path / "payload.json").write_text(json.dumps(payload))
    return exe


def calls(tmp_path: Path) -> list:
    path = tmp_path / "calls"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def keys_and_values(obj, out=None):
    out = out if out is not None else (set(), [])
    if isinstance(obj, dict):
        for k, v in obj.items():
            out[0].add(k)
            keys_and_values(v, out)
    elif isinstance(obj, list):
        for v in obj:
            keys_and_values(v, out)
    else:
        out[1].append(obj)
    return out


def test_allowlist_red_arm(tmp_path, monkeypatch):
    exe = fake_cswap(tmp_path)
    # control first: without the allowlist the fake's token reaches the output
    monkeypatch.setattr(
        accounts,
        "allow",
        lambda raw: {
            **raw,
            "red": False,
            "status": "ok",
            "in_rotation": True,
            "five_hour": {"pct": None},
            "seven_day": {"pct": None},
            "threshold": None,
            "name": "x",
        },
    )
    leaked = accounts.read(str(exe))
    assert "accessToken" in keys_and_values(leaked)[0]
    monkeypatch.undo()

    doc = accounts.read(str(exe))
    names, values = keys_and_values(doc["accounts"])
    assert names <= set(accounts.ALLOWED) | {"pct", "resets_at"}
    assert TOKEN not in json.dumps(doc) and "org-uuid-1" not in json.dumps(doc)
    one, two = doc["accounts"]
    assert one == {
        "name": "dev",
        "active": True,
        "in_rotation": True,
        "five_hour": {"pct": 40.0, "resets_at": "2026-04-01T14:00:00Z"},
        "seven_day": {"pct": 70.0, "resets_at": "2026-04-05T10:00:00Z"},
        "threshold": 80.0,
        "status": "ok",
        "measured_at": "2026-04-01T11:58:00Z",
        "age_s": 120,
        "stale": False,
        "red": False,
    }
    # without an alias the slot number names it, never the email (the web view redacts an email
    # to one marker, so two such accounts would read the same)
    assert two["name"] == "account 2" and two["red"] and two["stale"]
    assert doc["red"] == ["account 2: token_expired"]
    assert "two@example.com" not in json.dumps(doc)
    assert accounts.allow({"email": "x@example.com"})["name"] == "account ?"
    assert (
        all(c == list(accounts.ARGV) for c in calls(tmp_path))
        and "--store-only" in calls(tmp_path)[0]
    )
    assert values  # the walk reached the leaves


def test_every_account_at_threshold_is_red(tmp_path):
    full = json.loads(json.dumps(PAYLOAD))
    full["accounts"][0]["usage"]["sevenDay"]["pct"] = 85.0
    full["accounts"][1].update(usageStatus="ok", disabled=False)
    full["accounts"][1]["usage"]["fiveHour"]["pct"] = 100.0
    doc = accounts.read(str(fake_cswap(tmp_path, payload=full)))
    assert doc["red"] == ["accounts: every account in rotation at its threshold"]
    full["accounts"][1]["disabled"] = True  # out of rotation: the first alone is at its limit
    full["accounts"][0]["usage"]["sevenDay"]["pct"] = 50.0
    assert accounts.read(str(fake_cswap(tmp_path, payload=full)))["red"] == []


@pytest.mark.parametrize(
    ("mode", "gap"),
    [
        ("old", "accounts: not read (cswap without --store-only)"),
        ("missing", "accounts: not read (no cswap)"),
    ],
)
def test_no_store_only_no_fallback(tmp_path, mode, gap):
    exe = fake_cswap(tmp_path, mode=mode) if mode == "old" else tmp_path / "absent" / "cswap"
    doc = accounts.read(str(exe))
    assert doc["gap"] == gap and doc["accounts"] == []
    made = calls(tmp_path)
    assert made == (
        [list(accounts.ARGV)] if mode == "old" else []
    )  # one call, never without the flag
