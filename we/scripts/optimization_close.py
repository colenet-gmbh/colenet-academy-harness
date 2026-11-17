#!/usr/bin/env python3
"""Close the optimization inbox entries that settled themselves: the first step of `/we:optimize`.

Usage: optimization_close.py [--store <dir>] [--config <file>] [--prs-json <file>] [--today YYYY-MM-DD]

For every open entry in `<store>/inbox/` (default `.weside/optimization`; format:
`references/optimization-store.md` § Inbox entry), in this order:

  applied  a merged PR whose title or body names the entry's key (a whole token, any case) and that
           merged on or after the entry's first-seen date carried it (an older PR cannot have: the
           entry is a recurrence after an earlier apply): a ledger row `applied` with that PR as `change`, metric `recurrence`, `before` the
           entry's `retro` evidence lines, `after` pending, `review_by` today plus
           `optimization.review_days` (default 30)
  expired  the entry has fewer than two evidence lines and its first-seen date (the file name's) is
           `optimization.expire_days` (default 14) or more days old: a ledger row `expired`
  open     anything else: left for the decider

A closed entry's row goes on top of `LEDGER.md`'s table (created with its header when absent) and
its inbox file is deleted; the caller commits both with the rest of the run. Merged PRs come from
`gh pr list --state merged --limit 200 --json number,title,body,url,mergedAt` run in the store's repo, or
from `--prs-json` (the same JSON). Prints one line per closed entry (`applied <key> · <url>`,
`expired <key> · <n> days, <m> evidence line(s)`) and a final `closed <a> applied, <e> expired,
<o> open`. Exit 0; 2 when the store, the config or the PR list cannot be read.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

HEADER = (
    "| date | key | target | decision | evidence | metric | before → after | review_by | change |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
)
EXPIRE_DAYS = 14
REVIEW_DAYS = 30
_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)\.md$")
_FIELD = re.compile(r"^(?P<k>[a-z_]+):\s*(?P<v>.*?)\s*(?:#.*)?$")


class CloseError(Exception):
    pass


def frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    head = text[3:].split("\n---", 1)[0]
    out = {}
    for line in head.splitlines():
        found = _FIELD.match(line.strip())
        if found:
            out[found.group("k")] = found.group("v")
    return out


def evidence(text: str) -> list[str]:
    """The `- …` lines under `## Evidence`, up to the next level-2 heading."""
    out, inside = [], False
    for line in text.split("\n"):
        if line.startswith("## "):
            inside = line.strip() == "## Evidence"
            continue
        if inside and line.startswith("- "):
            out.append(line.rstrip())
    return out


def names_key(key: str, text: str) -> bool:
    return (
        re.search(rf"(?<![a-z0-9:-]){re.escape(key)}(?![a-z0-9:-])", text, re.IGNORECASE)
        is not None
    )


def carried(pr: dict, key: str, seen: datetime.date | None) -> bool:
    """The PR names the key and did not merge before the entry was first seen."""
    if not names_key(key, f"{pr.get('title') or ''}\n{pr.get('body') or ''}"):
        return False
    merged_at = str(pr.get("mergedAt") or "")[:10]
    try:
        return seen is None or not merged_at or datetime.date.fromisoformat(merged_at) >= seen
    except ValueError:
        return True


def merged_prs(store: Path, prs_json: Path | None) -> list[dict]:
    try:
        if prs_json is not None:
            raw = prs_json.read_text()
        else:
            raw = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--state",
                    "merged",
                    "--limit",
                    "200",
                    "--json",
                    "number,title,body,url,mergedAt",
                ],
                cwd=store,
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            ).stdout
        prs = json.loads(raw)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise CloseError(f"merged PRs: {exc}") from exc
    if not isinstance(prs, list):
        raise CloseError("merged PRs: not a JSON list")
    return [p for p in prs if isinstance(p, dict)]


def settings(config: Path) -> tuple[int, int]:
    if not config.exists():
        return EXPIRE_DAYS, REVIEW_DAYS
    try:
        section = json.loads(config.read_text()).get("optimization") or {}
        return int(section.get("expire_days", EXPIRE_DAYS)), int(
            section.get("review_days", REVIEW_DAYS)
        )
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise CloseError(f"{config}: {exc}") from exc


def cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def row(*cells: object) -> str:
    return "| " + " | ".join(cell(c) for c in cells) + " |\n"


def add_rows(ledger: Path, rows: list[str]) -> None:
    text = ledger.read_text() if ledger.exists() else ""
    lines = text.splitlines(keepends=True)
    sep = next(
        (i for i, line in enumerate(lines) if re.match(r"^\|(\s*:?-+:?\s*\|)+\s*$", line)), None
    )
    if sep is None:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + HEADER + "".join(rows)
    else:
        text = "".join(lines[: sep + 1]) + "".join(rows) + "".join(lines[sep + 1 :])
    ledger.write_text(text)


def close(
    store: Path, prs: list[dict], today: datetime.date, expire_days: int, review_days: int
) -> list[str]:
    inbox = store / "inbox"
    if not inbox.is_dir():
        raise CloseError(f"{inbox}: no inbox")
    out, rows, counts = [], [], {"applied": 0, "expired": 0, "open": 0}
    for path in sorted(inbox.glob("*.md")):
        found = _NAME.match(path.name)
        text = path.read_text()
        meta = frontmatter(text)
        key = meta.get("key") or (found.group(2) if found else path.stem)
        target = meta.get("target", "—")
        lines = evidence(text)
        try:
            seen = datetime.date.fromisoformat(found.group(1)) if found else None
        except ValueError:
            seen = None
        pr = next((p for p in prs if carried(p, key, seen)), None)
        if pr is not None:
            url = pr.get("url") or f"#{pr.get('number')}"
            retro = sum(1 for line in lines if " retro " in f" {line} ")
            review_by = (today + datetime.timedelta(days=review_days)).isoformat()
            rows.append(
                row(
                    today,
                    key,
                    target,
                    "applied",
                    f"merged PR {url}",
                    "recurrence",
                    f"{retro} → pending",
                    review_by,
                    url,
                )
            )
            out.append(f"applied {key} · {url}")
            counts["applied"] += 1
            path.unlink()
            continue
        age = (today - seen).days if seen else None
        if seen is not None and len(lines) < 2 and age >= expire_days:
            rows.append(
                row(
                    today,
                    key,
                    target,
                    "expired",
                    f"{len(lines)} evidence line(s) in {age} days",
                    "—",
                    "—",
                    "—",
                    "—",
                )
            )
            out.append(f"expired {key} · {age} days, {len(lines)} evidence line(s)")
            counts["expired"] += 1
            path.unlink()
            continue
        counts["open"] += 1
    if rows:
        add_rows(store / "LEDGER.md", rows)
    out.append(
        f"closed {counts['applied']} applied, {counts['expired']} expired, {counts['open']} open"
    )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", type=Path, default=Path(".weside/optimization"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--prs-json", type=Path)
    parser.add_argument("--today")
    args = parser.parse_args(argv)
    try:
        today = (
            datetime.date.fromisoformat(args.today)
            if args.today
            else datetime.datetime.now(datetime.timezone.utc).date()
        )
        config = args.config or args.store.resolve().parent / "config.json"
        expire_days, review_days = settings(config)
        if not (args.store / "inbox").is_dir():
            raise CloseError(f"{args.store / 'inbox'}: no inbox")
        prs = merged_prs(args.store, args.prs_json)
        for line in close(args.store, prs, today, expire_days, review_days):
            print(line)
    except (CloseError, ValueError, OSError) as exc:
        print(f"optimization_close: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
