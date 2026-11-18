"""worker_supervisor.py with fake sessions, a fake clock and temp repos: seen stops, cleanup, gc."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import worker_supervisor as ws

DEAD_PID = 2**22 + 7  # above pid_max on a default kernel: no process holds it
NAME = "AC-12-impl"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _row(pid, **extra):
    return {
        "name": NAME,
        "id": "bg1",
        "sessionId": "s1",
        "kind": "background",
        "pid": pid,
        **extra,
    }


def _transcript(projects: Path, entries):
    d = projects / "-tree"
    d.mkdir(parents=True, exist_ok=True)
    (d / "s1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))


def _feed(*frames):
    """`sessions()` returning each frame once, the last one forever."""
    frames = list(frames)

    def sessions():
        return frames.pop(0) if len(frames) > 1 else frames[0]

    return sessions


@pytest.fixture
def box(tmp_path):
    state, projects = tmp_path / "state", tmp_path / "projects"
    state.mkdir()
    (state / f"{NAME}.settings.json").write_text("{}")
    (state / f"{NAME}.json").write_text(json.dumps({"deadline": "2999-01-01T00:00:00Z"}))
    sent, items = [], []
    clock = Clock()

    def run(*frames, send_ok=True):
        return ws.supervise(
            NAME,
            "/tree",
            sessions=_feed(*frames),
            clock=clock,
            sleep=clock.sleep,
            send=lambda text: sent.append(text) or send_ok,
            board=lambda _tree, iid, title, _body="": items.append((iid, title)) or True,
            state=state,
            projects=projects,
        )

    return {"state": state, "projects": projects, "sent": sent, "items": items, "run": run}


LIVE, GONE = [_row(os.getpid())], [_row(DEAD_PID)]
USAGE = {
    "type": "assistant",
    "isApiErrorMessage": True,
    "error": "rate_limit",
    "message": {"content": [{"type": "text", "text": "limit"}]},
}


@pytest.mark.parametrize(
    "entry, deadline, reason",
    [
        (USAGE, "2999-01-01T00:00:00Z", "usage-limit"),
        ({**USAGE, "error": "authentication_failed"}, "2999-01-01T00:00:00Z", "auth"),
        (
            {"type": "assistant", "message": {"content": "done"}},
            "1970-01-02T00:00:00Z",
            "time-limit",
        ),
    ],
)
def test_an_end_with_a_reason_tells_the_starter_once_and_keeps_a_resumable_workers_files(
    box, entry, deadline, reason
):
    _transcript(box["projects"], [entry])
    (box["state"] / f"{NAME}.json").write_text(json.dumps({"deadline": deadline}))
    assert box["run"](LIVE, LIVE, []) == reason
    (line,) = box["sent"]
    assert (
        f"ended ({reason})" in line and f"we-worker {NAME} --tree /tree --bg --resume s1" in line
    )
    assert not box["items"]
    kept = sorted(p.name for p in box["state"].iterdir())
    resume = f"{NAME}.resume.json"  # `--resume <name>` finds it once claude agents drops the row
    files = [f"{NAME}.json", resume, f"{NAME}.settings.json"]
    assert kept == ([resume] if reason == "time-limit" else files)
    assert ws.kept_session(box["state"], NAME) == "s1"


def test_a_starter_out_of_reach_gets_the_line_on_the_board(box):
    _transcript(box["projects"], [USAGE])
    box["run"](LIVE, GONE, send_ok=False)
    ((iid, title),) = box["items"]
    assert iid == f"W-{NAME}-end-s1" and "usage-limit" in title


def test_a_plain_end_or_a_stop_tells_nobody_and_a_resumed_name_keeps_its_files(box):
    _transcript(box["projects"], [{"type": "assistant", "message": {"content": "done"}}])
    assert box["run"](LIVE, GONE) == "ended" and not box["sent"]
    assert not list(box["state"].iterdir())
    _transcript(box["projects"], [USAGE])
    (box["state"] / f"{NAME}.json").write_text("{}")  # no settings file: we-worker stop ran
    assert box["run"](LIVE, GONE) == "stopped" and not box["sent"]
    (box["state"] / f"{NAME}.settings.json").write_text("{}")
    other = {**_row(os.getpid()), "id": "bg2", "sessionId": "s2", "pid": os.getppid()}
    box["run"](LIVE, [_row(DEAD_PID), other])
    assert (box["state"] / f"{NAME}.settings.json").exists()


def test_a_permission_prompt_older_than_15_min_is_one_board_item(box):
    _transcript(
        box["projects"],
        [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "toolu_0001abcd", "name": "Bash", "input": {}}
                    ]
                },
            }
        ],
    )
    waiting = [_row(os.getpid(), waitingFor="permission prompt")]
    frames = [LIVE] + [waiting] * 40 + [GONE]  # 40 polls of 30 s: 20 minutes on one prompt
    box["run"](*frames)
    assert [iid for iid, _t in box["items"]] == [f"W-{NAME}-0001abcd"]


def test_a_session_that_never_shows_up_keeps_its_files(box):
    assert box["run"]([]) == "never-seen"
    assert (box["state"] / f"{NAME}.settings.json").exists()


# --- gc ----------------------------------------------------------------------------------------


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    origin, main = tmp_path / "origin.git", tmp_path / "repo"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(main))
    _git(main, "commit", "-q", "--allow-empty", "-m", "init")
    _git(main, "push", "-q", "origin", "HEAD:main")
    _git(main, "remote", "set-head", "origin", "main")
    return main


def _tree(repo, name, merged, dirty=False):
    path = repo.parent / f"repo-{name}"
    _git(repo, "worktree", "add", "-q", "-b", f"feat/{name}", str(path), "origin/main")
    _git(path, "commit", "-q", "--allow-empty", "-m", name)
    if merged:
        _git(path, "push", "-q", "origin", "HEAD:main")
    gd = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--absolute-git-dir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    Path(gd, "we-worker.json").write_text(json.dumps({"name": name}))
    if dirty:
        (path / "wip.txt").write_text("x")
    return path


def test_gc_removes_only_dead_sessions_own_files_and_lists_merged_clean_trees(
    tmp_path, repo, monkeypatch
):
    monkeypatch.setenv("PATH", f"{tmp_path / 'nobin'}{os.pathsep}/usr/bin:/bin")  # no gh
    state, jobs = tmp_path / "state", tmp_path / "jobs"
    state.mkdir()
    for n in ("AC-1-live", "AC-2-dead", "AC-3-fresh"):
        (state / f"{n}.settings.json").write_text("{}")
        (state / f"{n}.json").write_text("{}")
    for j in ("j-live", "j-gone"):
        (jobs / j).mkdir(parents=True)
        (jobs / j / "state.json").write_text(json.dumps({"state": "stopped", "sessionId": j}))
    merged_tree = _tree(repo, "AC-2-dead", merged=True)
    open_tree = _tree(repo, "AC-4-open", merged=False)
    dirty = _tree(repo, "AC-5-dirty", merged=True, dirty=True)
    live_tree = _tree(repo, "AC-1-live", merged=True)
    bare = repo.parent / "repo-AC-6-bare"  # no commit of its own: in origin/main, never "merged"
    _git(repo, "worktree", "add", "-q", "-b", "feat/AC-6-bare", str(bare), "origin/main")
    Path(_gd(bare), "we-worker.json").write_text(json.dumps({"name": "AC-6-bare"}))
    old = time.time() - 3600
    for p in [
        *state.iterdir(),
        *(Path(_gd(t), "we-worker.json") for t in (merged_tree, open_tree, dirty, live_tree)),
    ]:
        if "fresh" not in p.name:
            os.utime(p, (old, old))
    rows = [
        {"name": "AC-1-live", "id": "j-live", "sessionId": "x", "pid": os.getpid()},
        {"name": "AC-2-dead", "id": "old", "sessionId": "y", "pid": DEAD_PID},
    ]
    # a dead worker on a usage limit is resumable: its files stay 7 days, then go
    projects = tmp_path / "projects"
    (projects / "-p").mkdir(parents=True)
    (projects / "-p" / "z.jsonl").write_text(json.dumps(USAGE) + "\n")
    rows.append({"name": "AC-7-quota", "id": "q", "sessionId": "z", "pid": DEAD_PID})
    week = time.time() - 8 * 24 * 3600
    for n, age in (("AC-7-quota", old), ("AC-8-quota", week)):
        (state / f"{n}.json").write_text("{}")
        os.utime(state / f"{n}.json", (age, age))
    # a stopped worker's resume file stays 7 days like a resumable one's
    for n, age in (("AC-9-stopped", old), ("AC-10-stopped", week)):
        (state / f"{n}.resume.json").write_text("{}")
        os.utime(state / f"{n}.resume.json", (age, age))
    rows.append({"name": "AC-8-quota", "id": "q8", "sessionId": "z", "pid": DEAD_PID})
    run = lambda apply: ws.gc(  # noqa: E731
        repo, apply, sessions=lambda: rows, state=state, jobs=jobs, projects=projects
    )
    dry = run(False)
    assert (state / "AC-2-dead.json").exists() and Path(_gd(open_tree), "we-worker.json").exists()
    lines = run(True)
    assert dry == [x.replace("removed", "would remove") for x in lines]
    assert sorted(p.name for p in state.iterdir()) == sorted(
        [f"{n}{s}" for n in ("AC-1-live", "AC-3-fresh") for s in (".json", ".settings.json")]
        + ["AC-7-quota.json", "AC-9-stopped.resume.json"]
    )
    assert sorted(p.name for p in jobs.iterdir()) == ["j-gone", "j-live"]  # never removed
    assert any(x.startswith("1 job directories") for x in lines)
    trees = [x for x in lines if x.startswith("worktree")]
    assert len(trees) == 1 and "repo-AC-2-dead" in trees[0] and "git worktree remove" in trees[0]
    assert Path(_gd(merged_tree), "we-worker.json").exists()  # the listed tree keeps its marker
    assert Path(_gd(live_tree), "we-worker.json").exists()
    assert not Path(_gd(open_tree), "we-worker.json").exists()
    assert f"keep {dirty}: uncommitted changes" in lines and dirty.exists()


def _gd(tree):
    return subprocess.run(
        ["git", "-C", str(tree), "rev-parse", "--absolute-git-dir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_gc_refuses_when_the_sessions_cannot_be_read(tmp_path, repo):
    def broken():
        raise ws.wl.LaunchError("claude agents --json timed out")

    with pytest.raises(ws.wl.LaunchError):
        ws.gc(repo, True, sessions=broken, state=tmp_path, jobs=tmp_path)


# --- queue -------------------------------------------------------------------------------------


@pytest.fixture
def queue(tmp_path):
    clock, sent, tried = Clock(), [], []

    def put(name, at, timeout_s=7200):
        ws.enqueue(tmp_path, name, "/tree", [name, "--bg"], "## Goal\n", "Lead", timeout_s, at)
        ws.set_waiter(tmp_path, name, os.getpid())

    def wait(name, results, on_sleep=None):
        """Run the waiter of `name`; `results` are the fake start's exit codes in turn (3: the
        capacity check refuses)."""
        codes = list(results)

        def start(doc):
            tried.append(doc["name"])
            return (codes.pop(0) if len(codes) > 1 else codes[0]), "we-worker: started"

        def sleep(s):
            clock.sleep(s)
            if on_sleep:
                on_sleep()

        return ws.wait_queue(
            tmp_path,
            name,
            start=start,
            clock=clock,
            sleep=sleep,
            send=lambda t: sent.append(t) is None,
        )

    return {
        "state": tmp_path,
        "clock": clock,
        "sent": sent,
        "tried": tried,
        "put": put,
        "wait": wait,
    }


def test_a_queued_start_waits_for_room_and_tells_its_starter(queue):
    queue["put"]("AC-1-a", queue["clock"]())
    assert queue["wait"]("AC-1-a", [3, 3, 0]) == "started"
    assert queue["tried"] == ["AC-1-a"] * 3
    (line,) = queue["sent"]
    assert "queued start of AC-1-a started after 1 min" in line
    assert not ws.queued(queue["state"])
    brief = queue["state"] / "queue" / "AC-1-a.brief.md"
    assert not brief.exists()


def test_queued_starts_go_first_come_first_served(queue):
    t = queue["clock"]()
    queue["put"]("AC-1-a", t)
    queue["put"]("AC-2-b", t + 1)
    # the older entry leaves the queue after the first poll: only then does the younger one try
    drop = lambda: ws._drop(queue["state"], "AC-1-a")  # noqa: E731
    assert queue["wait"]("AC-2-b", [0], on_sleep=drop) == "started"
    assert queue["tried"] == ["AC-2-b"]
    assert queue["clock"]() > t + ws.QUEUE_POLL_S - 1  # it waited a poll behind AC-1-a


def test_a_queued_start_times_out_and_says_so(queue):
    queue["put"]("AC-1-a", queue["clock"](), timeout_s=90)
    assert queue["wait"]("AC-1-a", [3]) == "timeout"
    (line,) = queue["sent"]
    assert "timed out" in line and "nothing started" in line
    assert not ws.queued(queue["state"])


def test_stop_removes_a_queued_start_and_its_waiter_gives_up(queue):
    queue["put"]("AC-1-a", queue["clock"]())
    stop = lambda: ws._drop(queue["state"], "AC-1-a")  # noqa: E731  (unqueue without a kill)
    assert queue["wait"]("AC-1-a", [3], on_sleep=stop) == "removed"
    assert not queue["sent"]


def test_a_name_already_queued_refuses(queue):
    queue["put"]("AC-1-a", queue["clock"]())
    with pytest.raises(ws.wl.LaunchError, match="already queued"):
        queue["put"]("AC-1-a", queue["clock"]())
