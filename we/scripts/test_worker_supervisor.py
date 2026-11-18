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
def test_an_end_with_a_reason_tells_the_starter_once_and_cleans_up(box, entry, deadline, reason):
    _transcript(box["projects"], [entry])
    (box["state"] / f"{NAME}.json").write_text(json.dumps({"deadline": deadline}))
    assert box["run"](LIVE, LIVE, []) == reason
    (line,) = box["sent"]
    assert (
        f"ended ({reason})" in line and f"we-worker {NAME} --tree /tree --bg --resume s1" in line
    )
    assert not box["items"] and not list(box["state"].iterdir())


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


def test_a_session_that_never_shows_up_leaves_no_files(box):
    assert box["run"]([]) == "never-seen"
    assert not list(box["state"].iterdir())


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
    run = lambda apply: ws.gc(repo, apply, sessions=lambda: rows, state=state, jobs=jobs)  # noqa: E731
    dry = run(False)
    assert (state / "AC-2-dead.json").exists() and Path(_gd(open_tree), "we-worker.json").exists()
    lines = run(True)
    assert dry == [x.replace("removed", "would remove") for x in lines]
    assert sorted(p.name for p in state.iterdir()) == sorted(
        f"{n}{s}" for n in ("AC-1-live", "AC-3-fresh") for s in (".json", ".settings.json")
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
