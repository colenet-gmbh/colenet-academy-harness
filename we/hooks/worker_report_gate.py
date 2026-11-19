#!/usr/bin/env python3
"""Stop hook: a process worker ends no turn without sending its report to the Lead.

A worker started through `agents.worker_launch` (`scripts/worker_launch.py`) is a session of its
own: its plain text never reaches the Lead, and no `SubagentStop` fires, so the prompt hook on
`^we:dev-(medium|high)$` never sees it. The worktree's marker (`<git-dir>/we-worker.json`) names
the Lead; a turn that sent no `SendMessage` to it is blocked once with the report instruction.

  * No marker in the session's worktree, or one naming another session (the Lead working in the
    worker's tree) → pass at once.
  * The turn is the transcript after the last user entry that is not only a tool result (the
    brief, a steer, a cross-session message). A `SendMessage` whose `to` is the Lead, with or
    without its ` [ref]`, passes.
  * A brief carrying the line `PR owner: worker` (`worker_launch.PR_OWNER_LINE`, written by the
    master under `build.pr_owner: worker`; found in the brief file `worker_launch.py launch` names,
    else in the transcript's first prompts) makes the worker its PR's owner: the report's last line
    is then one of the three end lines (`END_LINES`, `references/worker-dispatch.md` § PR-carrying
    worker), because the starter acts on that line alone. Any other last line blocks once.
  * Deterministic, no LLM judgement: a Stop prompt hook would run on every stop of every session.
  * `stop_hook_active` passes (one block per stop); every read error passes (fail-open).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from worker_launch import PR_OWNER_LINE, worker_marker

TAIL_BYTES = 4_000_000
BLOCK = (
    "Your turn ended without your report to the Lead. Finish the remaining steps of your brief, "
    "then send the report your brief asks for (worker-dispatch.md § Report fields) as one "
    "SendMessage to `{lead}`; your plain text never reaches it."
)

END_BLOCK = (
    "You carry this PR (your brief: `PR owner: worker`), so your report's last line is exactly one "
    "of `armed: <PR> on <sha>` · `held: <PR> (<why>)` · `parked: <branch> (<why>)` "
    "(worker-dispatch.md § PR-carrying worker); the last line you sent `{lead}` was {last!r}. "
    "Send it again with the end line last."
)
END_LINES = (
    re.compile(r"armed: \S+ on [0-9a-f]{7,40}"),
    re.compile(r"held: \S+ \(.+\)"),
    re.compile(r"parked: \S+ \(.+\)"),
)
HEAD_LINES = 400
BRIEF_REF = re.compile(r"Read (\S+we-brief\.md) and follow it")
OWNER_LINE = re.compile(rf"^{re.escape(PR_OWNER_LINE)}\b", re.MULTILINE)


def end_line_ok(message: str) -> bool:
    """The message's last non-empty line is one of `END_LINES`."""
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    return bool(lines) and any(rx.fullmatch(lines[-1]) for rx in END_LINES)


def _text(entry: dict) -> str:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def carries_pr(transcript: str) -> bool:
    """The brief holds `PR owner: worker`: in the prompts at the transcript's head, or in the
    brief file such a prompt names (`worker_launch.py launch` passes `Read <file> and follow it.`)."""
    with open(transcript, encoding="utf-8", errors="replace") as fh:
        for n, raw in enumerate(fh):
            if n >= HEAD_LINES:
                break
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "user":
                continue
            text = _text(entry)
            if OWNER_LINE.search(text):
                return True
            ref = BRIEF_REF.search(text)
            if ref:
                try:
                    return bool(OWNER_LINE.search(Path(ref.group(1)).read_text(encoding="utf-8")))
                except OSError:
                    return False
    return False


def _turn_start(entry: dict) -> bool:
    if entry.get("type") != "user":
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(
        isinstance(p, dict) and p.get("type") != "tool_result" for p in content
    )


def _sent_to(entry: dict, lead: str) -> str | None:
    """The text of the entry's last SendMessage to `lead`, else None."""
    if entry.get("type") != "assistant":
        return None
    found = None
    for part in (entry.get("message") or {}).get("content") or []:
        if isinstance(part, dict) and part.get("type") == "tool_use":
            if part.get("name") != "SendMessage":
                continue
            args = part.get("input") or {}
            to = str(args.get("to", ""))
            if to == lead or to.startswith(f"{lead} ["):
                message = args.get("message", "")
                found = message if isinstance(message, str) else ""
    return found


def last_report(transcript: str, lead: str) -> str | None:
    """The text of the current turn's last SendMessage to `lead`, else None."""
    with open(transcript, "rb") as fh:
        size = os.path.getsize(transcript)
        if size > TAIL_BYTES:
            fh.seek(size - TAIL_BYTES)
            fh.readline()  # skip the cut line
        entries = []
        for raw in fh:
            try:
                entries.append(json.loads(raw))
            except ValueError:
                continue
    for entry in reversed(entries):
        sent = _sent_to(entry, lead)
        if sent is not None:
            return sent
        if _turn_start(entry):
            return None
    return None


def reported(transcript: str, lead: str) -> bool:
    """True when the current turn sent a SendMessage to `lead` (the supervisor's idle-stop reads it)."""
    return last_report(transcript, lead) is not None


def decide(payload: dict) -> str | None:
    if payload.get("stop_hook_active") or payload.get("agent_id"):
        return None
    marker = worker_marker(payload)
    if not marker or not marker.get("lead") or not payload.get("transcript_path"):
        return None
    lead = str(marker["lead"])
    try:
        sent = last_report(payload["transcript_path"], lead)
        if sent is None:
            return BLOCK.format(lead=lead)
        if end_line_ok(sent) or not carries_pr(payload["transcript_path"]):
            return None
    except OSError:
        return None
    lines = [line.strip() for line in sent.splitlines() if line.strip()]
    return END_BLOCK.format(lead=lead, last=lines[-1][:120] if lines else "")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return
    try:
        reason = decide(payload)
    except Exception as exc:  # fail open: a comfort gate never traps a worker in its turn
        print(f"worker_report_gate: skipped ({type(exc).__name__}: {exc})", file=sys.stderr)
        return
    if reason:
        print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    main()
    sys.exit(0)
