#!/usr/bin/env python3
"""The worker rights and the never-list of a process worker: one owner of the rules, read by
`hooks/worker_rights_gate.py` (the brake) and by `worker_launch.py settings` (the record).

No boundary. This is a brake against a misguided worker (a wrong brief, a prompt injection in a PR
comment), not a barrier against one that goes around it on purpose: a script file it writes and runs,
another `gh` binary, `curl` with the token all pass every text check. The boundaries are server-side
(branch rules, the merge gate, the app rights of the token).

Rights (crew `worker_rights`, read from the default branch of origin, never from the worktree; opt-in
per repo, all off by default): in a repo whose `origin` is one
of `worker_rights.repos`, a worker with a story key, on a branch that is not the default branch and
carries that key, may run without asking
  `gh pr ready` (`pr_ready`) and `gh pr comment` (`pr_comment`), with no selector or one naming the PR
  whose head is its branch in its repo (`gh pr view --json` resolves it; a `gh` failure denies);
  the crew's `branch_rc` command prefix (a branch RC: the repo's pre-push guard holds the receipt).
PR owner rights (`build.pr_owner: worker` of `.weside/config.json`, read from the default branch of
origin like the crew; `pr_owner_on_default`): the worker carries its own PR, so under the same key and
branch conditions it may also run `git push` (`push`: every destination branch carries its key),
`gh pr create` (`pr_create`: no argument, `--head` only its branch), `gh pr edit` and `gh pr comment`
(`pr_edit`, `pr_comment`: its own PR), `arm_pr.sh` (`arm`: its own PR), `gh pr merge` (`merge`: its
own PR, either `--disable-auto` alone or `--match-head-commit <its checkout's HEAD>`, the line
`arm_pr.sh` prints as `ready`, never `--admin` or `--auto`; every other `gh pr merge` is denied) and
`gh api graphql` with `resolveReviewThread` as its only mutation (`resolve_thread`). The switch is the
opt-in: `worker_rights.repos` is not needed for these. Under `master` (the default) none of them is
judged here, as before.
A held right in a command of its own is allowed outright; in a compound command it only passes this
gate. A right whose conditions fail is denied with the reason.

Never (every process worker, whatever its key; `never` names the item): secrets (`gh secret`,
`gh variable`, a `gh api` write on a `/secrets`, `/variables` or `/environments` path, the crew's
`deny_commands`); delete (`gh <noun> delete…`, `--delete-branch`, `gh pr comment --delete-last`,
`gh api` with method DELETE, `git push --delete|-d`, a `:<ref>` refspec); main (a `git push` whose
destination is the default branch, `main` or `master`, also a bare push from it, `--all`, `--mirror`;
`ALLOW_COMMIT_TO_MAIN`); guard bypass (a `git push` of a tag, `ALLOW_PUSH_WITHOUT_MAIN`,
`--no-verify`, `git commit -n`, `core.hooksPath`); the crew's `deny_text` anywhere in the command.
A worker never merges: the worker card says so; no hook judges a merge outside the PR owner rights.

Parse depth: compound commands, `bash|sh -c '…'`, `$( … )`, backticks, `<( … )`, `eval` of a literal
and heredocs fed to a shell are split into simple commands; inline code of `python -c`, `node -e`,
`perl -e`, `ruby -e` and heredocs fed to them are checked as text; `eval`, `source` or `.` of a
non-literal argument, a nesting deeper than `MAX_DEPTH` and an unparsable command are refused.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
INLINE = {"python": "-c", "python3": "-c", "node": "-e", "perl": "-e", "ruby": "-e"}
PREFIXES = {"nohup", "builtin"}
KEYWORDS = {"if", "then", "do", "else", "elif", "while", "until", "!", "{", "}", "fi", "done"}
FLAG_PREFIXES = {"sudo", "env", "timeout", "xargs", "nice", "stdbuf", "time", "command", "exec"}
MAX_DEPTH = 4
GIT_TIMEOUT_S = 10
GH_TIMEOUT_S = 30
MAIN_NAMES = ("main", "master")
GUARD_WORDS = ("ALLOW_PUSH_WITHOUT_MAIN", "--no-verify", "core.hooksPath")
WRITE_FIELDS = ("-f", "-F", "--field", "--raw-field", "--input")
SECRET_PATH = re.compile(r"(^|/)(secrets|variables|environments)(/|$|\?)")
HEREDOC = re.compile(
    r"<<-?[ \t]*(['\"]?)([^\s'\"]+)\1(?P<rest>[^\n]*)\n(?P<body>.*?)^\t*\2[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


@dataclass
class Parsed:
    """A command split into simple commands (argv lists), inline code texts and refusals."""

    commands: list[list[str]] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    compound: bool = False


def _substitutions(text: str) -> list[str]:
    """The bodies of `$( … )`, `<( … )`, `>( … )` and backticks in `text`, outermost first, as bash
    sees them: nothing inside single quotes, no escaped backtick or `$`."""
    out: list[str] = []
    i, quote = 0, None
    while i < len(text):
        ch = text[i]
        if quote == "'":
            quote = None if ch == "'" else quote
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch == "'" and quote is None:
            quote = "'"
        elif ch == '"':
            quote = None if quote == '"' else '"'
        elif ch == "`":
            j = i + 1
            while j < len(text) and text[j] != "`":
                j += 2 if text[j] == "\\" else 1
            if j >= len(text):
                break
            out.append(text[i + 1 : j])
            i = j + 1
            continue
        elif ch in "$<>" and text[i + 1 : i + 2] == "(" and text[i + 2 : i + 3] != "(":
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            out.append(text[i + 2 : j - 1])
            i = j
            continue
        i += 1
    return out


def _tokens(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _is_op(tok: str) -> bool:
    return bool(tok) and all(c in "();|&" for c in tok)


def _is_redirect(tok: str) -> bool:
    return bool(tok) and all(c in "<>&" for c in tok) and ("<" in tok or ">" in tok)


def _strip_prefixes(argv: list[str]) -> list[str]:
    """`argv` without leading assignments, keywords and wrappers (`env X=1`, `timeout 30`, `sudo -u x`)."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in KEYWORDS or tok in PREFIXES or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tok):
            i += 1
        elif tok in FLAG_PREFIXES:
            i += 1
            while i < len(argv) and (argv[i].startswith("-") or "=" in argv[i]):
                i += (
                    2
                    if argv[i]
                    in ("-u", "-g", "-s", "-k", "-I", "-n", "-P", "-d", "-a", "-o", "-e", "-i")
                    else 1
                )
            if tok == "timeout" and i < len(argv) and re.fullmatch(r"[\d.]+[smhd]?", argv[i]):
                i += 1
        else:
            break
    return argv[i:]


def parse(command: str, depth: int = 0, bodies: list[str] | None = None) -> Parsed:
    """Split `command` (see the module docstring); recursion stops at `MAX_DEPTH` with a refusal.
    `bodies` is shared down the recursion: a heredoc lifted out here may be used inside a `$( … )`."""
    out = Parsed()
    if depth > MAX_DEPTH:
        out.problems.append(f"nested deeper than {MAX_DEPTH} levels")
        return out
    bodies = [] if bodies is None else bodies

    expanded: list[str] = []  # bodies of unquoted heredocs: bash expands `$( … )` in them

    def take(match: re.Match[str]) -> str:
        bodies.append(match.group("body"))
        if not match.group(1):
            expanded.append(match.group("body"))
        return f"<<HEREDOC{len(bodies) - 1} " + match.group("rest")

    stripped = HEREDOC.sub(take, command.replace("\\\n", " "))
    for inner in [s for text in (stripped, *expanded) for s in _substitutions(text)]:
        out.compound = True
        _merge(out, parse(inner, depth + 1, bodies))
    try:
        tokens = _tokens(stripped.replace("\n", " ; "))
    except ValueError as exc:
        out.problems.append(f"unparsable ({exc})")
        return out
    simple: list[list[str]] = [[]]
    heredoc_of: dict[int, int] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _is_op(tok):
            simple.append([])
        elif tok in ("<<", "<<-") and i + 1 < len(tokens) and tokens[i + 1].startswith("HEREDOC"):
            heredoc_of[len(simple) - 1] = int(tokens[i + 1][7:])
            i += 1
        elif _is_redirect(tok):
            if simple[-1] and simple[-1][-1].isdigit():
                simple[-1].pop()
            i += 1  # its target
        else:
            simple[-1].append(tok)
        i += 1
    commands = [(n, c) for n, c in enumerate(simple) if c]
    if len(commands) > 1:
        out.compound = True
    for n, argv in commands:
        body = bodies[heredoc_of[n]] if n in heredoc_of else None
        _descend(out, _strip_prefixes(argv), body, depth)
    return out


def _merge(out: Parsed, inner: Parsed) -> None:
    out.commands += inner.commands
    out.texts += inner.texts
    out.problems += inner.problems


def _descend(out: Parsed, argv: list[str], heredoc: str | None, depth: int) -> None:  # noqa: PLR0912
    if not argv:
        return
    out.commands.append(argv)
    name = os.path.basename(argv[0])
    if name in SHELLS:
        out.compound = True
        for k, tok in enumerate(argv[1:], 1):
            if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", tok) and k + 1 < len(argv):
                _merge(out, parse(argv[k + 1], depth + 1))
                break
        else:
            if heredoc is not None:
                _merge(out, parse(heredoc, depth + 1))
                out.texts.append(heredoc)
    elif re.fullmatch(r"python3?(\.\d+)?|node|perl|ruby", name):
        out.compound = True
        flag = INLINE.get(re.sub(r"\.\d+$", "", name), "-c")
        for k, tok in enumerate(argv[1:], 1):
            if tok == flag and k + 1 < len(argv):
                out.texts.append(argv[k + 1])
                break
        if heredoc is not None:
            out.texts.append(heredoc)
    elif name == "eval":
        out.compound = True
        code = " ".join(argv[1:])
        if "$" in code or "`" in code:
            out.problems.append("eval of a non-literal argument")
        else:
            _merge(out, parse(code, depth + 1))
    elif name in ("source", ".") and len(argv) > 1 and ("$" in argv[1] or "`" in argv[1]):
        out.problems.append(f"{name} of a non-literal argument")


# ---------------------------------------------------------------- the repo a command runs in


def _git(cwd: str | Path | None, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd or "."), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def slug_of(url: str | None) -> str | None:
    """`owner/name` of a GitHub remote URL or a `[HOST/]OWNER/REPO` argument, lowercased."""
    if not url:
        return None
    found = re.search(r"(?:[:/]|^)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url.strip())
    return f"{found.group(1)}/{found.group(2)}".lower() if found else None


def origin_slug(cwd: str | Path | None) -> str | None:
    return slug_of(_git(cwd, "remote", "get-url", "origin"))


def default_branch(cwd: str | Path | None) -> str:
    head = _git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    return head.split("/", 1)[1] if head and "/" in head else "main"


def protected_branches(cwd: str | Path | None) -> set[str]:
    return {default_branch(cwd), *MAIN_NAMES}


def current_branch(cwd: str | Path | None) -> str:
    return _git(cwd, "branch", "--show-current") or ""


# ---------------------------------------------------------------- the never-list


def _gh_split(argv: list[str]) -> tuple[list[str], str | None]:
    """(args after the global flags, the `-R`/`--repo` value) of a `gh` command."""
    repo, i = None, 1
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] in ("-R", "--repo") and i + 1 < len(argv):
            repo, i = argv[i + 1], i + 2
        elif argv[i].startswith("--repo="):
            repo, i = argv[i].split("=", 1)[1], i + 1
        else:
            i += 1
    return argv[i:], repo


def _repo_flag(args: list[str]) -> str | None:
    for k, tok in enumerate(args):
        if tok in ("-R", "--repo") and k + 1 < len(args):
            return args[k + 1]
        if tok.startswith("--repo="):
            return tok.split("=", 1)[1]
        if re.fullmatch(r"-R\S+", tok):
            return tok[2:]
    return None


def _api_never(args: list[str]) -> str | None:
    method, path, writes = None, None, False
    k = 1
    while k < len(args):
        tok = args[k]
        if tok in ("-X", "--method") and k + 1 < len(args):
            method, k = args[k + 1].upper(), k + 2
            continue
        if tok.startswith("--method="):
            method = tok.split("=", 1)[1].upper()
        elif re.fullmatch(r"-X[A-Za-z]+", tok):
            method = tok[2:].upper()
        elif tok in WRITE_FIELDS or tok.startswith(
            tuple(f"{f}=" for f in WRITE_FIELDS if f[1] == "-")
        ):
            writes = True
            k += 2 if "=" not in tok else 1
            continue
        elif tok in (
            "-H",
            "--header",
            "-q",
            "--jq",
            "-t",
            "--template",
            "--hostname",
            "--cache",
            "-p",
            "--preview",
        ):
            k += 2
            continue
        elif not tok.startswith("-") and path is None:
            path = tok
        k += 1
    if method == "DELETE":
        return "delete (gh api with method DELETE)"
    if path and SECRET_PATH.search(path.split("?")[0]) and (writes or method not in (None, "GET")):
        return f"secrets (gh api write on {path})"
    return None


def _gh_never(argv: list[str]) -> str | None:  # noqa: PLR0911
    args, _repo = _gh_split(argv)
    if not args:
        return None
    noun, verb = args[0], args[1] if len(args) > 1 else ""
    if noun in ("secret", "variable"):
        return f"secrets (gh {noun})"
    if noun == "api":
        return _api_never(args)
    if verb.startswith("delete"):
        return f"delete (gh {noun} {verb})"
    if "--delete-branch" in args or (noun == "pr" and verb == "close" and "-d" in args):
        return "delete (--delete-branch)"
    if noun == "pr" and verb == "comment" and "--delete-last" in args:
        return "delete (gh pr comment --delete-last)"
    return None


def _git_split(argv: list[str]) -> tuple[list[str], str | None, list[str]]:
    """(args from the subcommand on, the `-C` dir, the `-c` settings) of a `git` command."""
    cdir, confs, i = None, [], 1
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] == "-C" and i + 1 < len(argv):
            cdir, i = argv[i + 1], i + 2
        elif argv[i] == "-c" and i + 1 < len(argv):
            confs.append(argv[i + 1])
            i += 2
        else:
            i += 1
    return argv[i:], cdir, confs


PUSH_VALUE_FLAGS = ("-o", "--push-option", "--receive-pack", "--exec", "--repo")


def _push_never(args: list[str], cwd: str | None) -> str | None:  # noqa: PLR0911, PLR0912
    positional: list[str] = []
    k = 1
    while k < len(args):
        tok = args[k]
        if _abbrev(tok, "--delete", 4) or re.fullmatch(r"-[a-zA-Z]*d[a-zA-Z]*", tok):
            return "delete (git push --delete)"
        if _abbrev(tok, "--no-verify", 6):
            return "guard bypass (--no-verify)"
        if tok in ("--tags", "--follow-tags"):
            return f"guard bypass (git push {tok}: a tag)"
        if any(_abbrev(tok, flag, 4) for flag in ("--all", "--mirror", "--branches")):
            return f"main (git push {tok})"
        if tok in PUSH_VALUE_FLAGS:
            k += 2
            continue
        if not tok.startswith("-"):
            positional.append(tok)
        k += 1
    protected = protected_branches(cwd)
    refspecs = positional[1:]
    if not refspecs:
        branch = current_branch(cwd)
        return f"main (a bare git push from {branch})" if branch in protected else None
    for n, raw in enumerate(refspecs):
        spec = raw.lstrip("+")
        if spec == "tag" or (n and refspecs[n - 1] == "tag"):
            return "guard bypass (git push of a tag)"
        if spec.startswith(":"):
            return f"delete (git push {spec})"
        src, _, dst = spec.partition(":")
        dst = dst or src
        if dst.startswith("refs/tags/") or (
            not dst.startswith("refs/")
            and _git(cwd, "rev-parse", "-q", "--verify", f"refs/tags/{dst}")
        ):
            return f"guard bypass (git push of the tag {dst})"
        name = dst.removeprefix("refs/heads/")
        if "*" in dst:
            return f"main (git push of the pattern {dst})"
        if name == "HEAD":
            name = current_branch(cwd)
        if name in protected:
            return f"main (git push to {name})"
    return None


def _abbrev(tok: str, option: str, shortest: int) -> bool:
    """`tok` is `option` or an abbreviation git accepts for it (at least `shortest` characters)."""
    word = tok.split("=", 1)[0]
    return len(word) >= shortest and option.startswith(word)


def _git_never(argv: list[str], cwd: str | None) -> str | None:
    args, cdir, confs = _git_split(argv)
    if any(c.split("=", 1)[0].lower() == "core.hookspath" for c in confs):
        return "guard bypass (core.hooksPath)"
    where = os.path.join(cwd or ".", cdir) if cdir else cwd
    if not args:
        return None
    if args[0] == "push":
        return _push_never(args, where)
    if args[0] in ("commit", "merge", "rebase", "am") and any(
        _abbrev(t, "--no-verify", 6) for t in args
    ):
        return "guard bypass (--no-verify)"
    if args[0] == "commit" and any(
        t == "-n" or re.fullmatch(r"-[a-mo-zA-Z]*n[a-zA-Z]*", t)
        for t in args[1:]
        if not t.startswith("--")
    ):
        return "guard bypass (git commit -n)"
    return None


def _runs(argv: list[str]) -> list[list[str]]:
    """The command itself, and the script a shell or interpreter runs (`bash x.sh`)."""
    out = [argv]
    if len(argv) > 1 and re.fullmatch(
        r"(ba|z|da|k)?sh|python3?(\.\d+)?", os.path.basename(argv[0])
    ):
        rest = [t for t in argv[1:] if not t.startswith("-")]
        if rest:
            out.append(rest)
    return out


def _starts_with(argv: list[str], entry: str) -> bool:
    words = entry.split()
    if not words or len(argv) < len(words):
        return False
    head = os.path.basename(argv[0])
    return (head == words[0] or argv[0] == words[0]) and argv[1 : len(words)] == words[1:]


def _word_hit(text: str, entry: str) -> bool:
    tail = r"(?![\w-])" if re.match(r"\w", entry[-1:]) else ""
    return re.search(r"(?<![\w-])" + re.escape(entry) + tail, text) is not None


TEXT_RULES = (
    (re.compile(r"\bgh\s+(secret|variable)\b"), "secrets (gh {1} in inline code)"),
    (
        re.compile(r"\bgh\s+api\b[^\n]*?/(secrets|variables|environments)\b"),
        "secrets (gh api on /{1} in inline code)",
    ),
    (re.compile(r"\bgh\s+[\w-]+\s+delete"), "delete (gh … delete in inline code)"),
    (re.compile(r"(-X\s*|--method[\s=]+)DELETE\b"), "delete (method DELETE in inline code)"),
    (
        re.compile(r"\bgit\s+push\b[^\n;|&]*(\s--delete\b|\s-d\b|\s\+?:\S)"),
        "delete (git push --delete in inline code)",
    ),
    (
        re.compile(r"\bgit\s+push\b[^\n;|&]*(--tags|--follow-tags|refs/tags/|\stag\s)"),
        "guard bypass (git push of a tag in inline code)",
    ),
    (
        re.compile(r"\bgit\s+push\b[^\n;|&]*[\s:](refs/heads/)?(main|master)\b(?![\w/-])"),
        "main (git push to {2} in inline code)",
    ),
)


def _normalized(text: str) -> str:
    """Inline code with list-of-strings argv joined (`["gh", "secret"]` → `gh secret`)."""
    return re.sub(r"""['"]\s*,\s*['"]""", " ", text)


def never(  # noqa: PLR0911, PLR0912
    command: str, cwd: str | None, rights: dict | None = None, parsed: Parsed | None = None
) -> str | None:
    """The never-list item `command` hits for a worker in `cwd`, else None."""
    rights = rights or {}
    parsed = parsed or parse(command)
    if parsed.problems:
        return f"unreadable command: {parsed.problems[0]}"
    seen = [HEREDOC.sub("", command), *parsed.texts]  # a heredoc is data unless a shell runs it
    if any("ALLOW_COMMIT_TO_MAIN" in t for t in seen):
        return "main (ALLOW_COMMIT_TO_MAIN)"
    for word in GUARD_WORDS:
        if any(word.lower() in t.lower() for t in seen):
            return f"guard bypass ({word})"
    for text in rights.get("deny_text") or []:
        if text and any(text in t for t in seen):
            return f"crew deny_text ({text})"
    for argv in parsed.commands:
        name = os.path.basename(argv[0])
        if name in ("cd", "pushd") and len(argv) > 1:  # a later bare push runs there
            cwd = os.path.join(cwd or ".", os.path.expanduser(argv[1]))
            continue
        hit = _gh_never(argv) if name == "gh" else _git_never(argv, cwd) if name == "git" else None
        if hit:
            return hit
        for run in _runs(argv):
            for entry in rights.get("deny_commands") or []:
                if entry and _starts_with(run, entry):
                    return f"secrets (crew deny_commands: {entry})"
    for raw in parsed.texts:
        text = _normalized(raw)
        for rx, label in TEXT_RULES:
            found = rx.search(text)
            if found:
                return label.format(found.group(0), *found.groups())
        for entry in rights.get("deny_commands") or []:
            if entry and _word_hit(text, entry):
                return f"secrets (crew deny_commands: {entry} in inline code)"
    return None


# ---------------------------------------------------------------- the rights


RIGHT_NAMES = ("pr_ready", "pr_comment", "branch_rc")


def _on_default(root: str, rel: str) -> dict | None:
    """The JSON object at `rel` on the default branch of the repo at `root` (origin's), else None."""
    text = _git(
        root,
        "show",
        f"{_git(root, 'rev-parse', '--abbrev-ref', 'origin/HEAD') or 'origin/main'}:{rel}",
    )
    try:
        data = json.loads(text) if text else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _crew_on_default(cwd: str | Path | None) -> dict | None:
    """The crew in force for `cwd` as the default branch holds it: its `.weside/crew.json`, or the
    crew file its `.weside/config.json` points at, each read from that repo's origin/HEAD. A file in
    the worktree never counts: the worker could edit the rules that brake it."""
    root = _git(cwd, "rev-parse", "--show-toplevel")
    if not root:
        return None
    own = _on_default(root, ".weside/crew.json")
    if own is not None:
        return own
    pointer = (_on_default(root, ".weside/config.json") or {}).get("crew")
    if not isinstance(pointer, str) or not pointer.strip():
        return None
    target = (Path(root) / pointer.strip()).resolve()
    other = _git(target.parent, "rev-parse", "--show-toplevel")
    if not other:
        return None
    return _on_default(other, str(target.relative_to(Path(other).resolve())))


def crew_rights(cwd: str | Path | None) -> dict:
    """The crew's `worker_rights` for `cwd` from the default branch, over the defaults; a block of
    the wrong shape counts as the defaults (no rights; the built-in never-list still holds)."""
    import crew as crew_mod  # noqa: PLC0415

    defaults = crew_mod.DEFAULTS["worker_rights"]
    block = (_crew_on_default(cwd) or {}).get("worker_rights")
    if not isinstance(block, dict):
        return dict(defaults)
    merged = {**defaults, **{k: v for k, v in block.items() if k in defaults}}
    for key, default in defaults.items():
        value = merged[key]
        ok = (
            isinstance(value, list) and all(isinstance(v, str) for v in value)
            if isinstance(default, list)
            else isinstance(value, bool)
            if isinstance(default, bool)
            else value is None or isinstance(value, str)
        )
        if not ok:
            print(
                f"worker_rights: crew worker_rights.{key} has the wrong type; no rights",
                file=sys.stderr,
            )
            return dict(defaults)
    return merged


def opted_in(rights: dict, slug: str | None) -> bool:
    return bool(slug) and slug.lower() in {str(r).lower() for r in rights.get("repos") or []}


def right_of(argv: list[str], rights: dict, owner: str = "master") -> str | None:  # noqa: PLR0911
    """Which right a simple command asks for, else None; the PR owner rights only under `owner`
    `worker`, so under `master` a push, `gh pr create` or `gh pr merge` stays out of this gate."""
    name = os.path.basename(argv[0])
    if name == "gh":
        args, _repo = _gh_split(argv)
        if args[:2] == ["pr", "ready"]:
            return "pr_ready"
        if args[:2] == ["pr", "comment"]:
            return "pr_comment"
        if owner == "worker" and args[:1] == ["pr"] and args[1:2] and args[1] in PR_VERBS:
            return PR_VERBS[args[1]]
        if (
            owner == "worker"
            and args[:2] == ["api", "graphql"]
            and "resolveReviewThread" in " ".join(args)
        ):
            return "resolve_thread"
    if owner == "worker":
        if name == "git" and _git_split(argv)[0][:1] == ["push"]:
            return "push"
        if _arm_argv(argv):
            return "arm"
    rc = rights.get("branch_rc")
    if rc and _starts_with(argv, rc):
        return "branch_rc"
    return None


PR_VALUE_FLAGS = ("-b", "--body", "-F", "--body-file", "-R", "--repo")


def _selector(args: list[str]) -> str | None:
    k = 2
    while k < len(args):
        tok = args[k]
        if tok in PR_VALUE_FLAGS:
            k += 2
            continue
        if not tok.startswith("-"):
            return tok
        k += 1
    return None


def _gh_pr_head(selector: str, slug: str, cwd: str | None) -> dict:
    """`gh pr view <selector> --json headRefName,headRepository,headRepositoryOwner`; raises on failure."""
    out = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            selector,
            "--repo",
            slug,
            "--json",
            "headRefName,headRepository,headRepositoryOwner",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=GH_TIMEOUT_S,
        cwd=cwd or None,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"gh exited {out.returncode}")
    data = json.loads(out.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("gh printed no object")
    return data


# ---------------------------------------------------------------- the PR owner's rights

PR_OWNER_RIGHTS = ("push", "pr_create", "pr_edit", "pr_comment", "arm", "merge", "resolve_thread")
PR_VERBS = {"create": "pr_create", "edit": "pr_edit", "merge": "merge"}
# the value-taking flags per verb: a value read as the PR selector resolves to no own PR and is
# denied, so a flag missing here fails closed
VALUE_FLAGS = {
    "pr_create": (
        *("-t", "--title", "-b", "--body", "-F", "--body-file", "-B", "--base", "-H", "--head"),
        *("-l", "--label", "-a", "--assignee", "-r", "--reviewer", "-m", "--milestone"),
        *("-p", "--project", "-T", "--template", "-R", "--repo"),
    ),
    "pr_edit": (
        *(
            "-t",
            "--title",
            "-b",
            "--body",
            "-F",
            "--body-file",
            "-B",
            "--base",
            "-m",
            "--milestone",
        ),
        *("--add-label", "--remove-label", "--add-reviewer", "--remove-reviewer"),
        *(
            "--add-assignee",
            "--remove-assignee",
            "--add-project",
            "--remove-project",
            "-R",
            "--repo",
        ),
    ),
    "merge": (
        *("-b", "--body", "-F", "--body-file", "-t", "--subject", "-A", "--author-email"),
        *("--match-head-commit", "-R", "--repo"),
    ),
}
MERGE_FLAGS = ("--merge", "--squash", "--rebase", "-m", "-s", "-r", *VALUE_FLAGS["merge"])
SHA = re.compile(r"[0-9a-f]{7,40}")


def pr_owner_on_default(cwd: str | Path | None) -> str:
    """`build.pr_owner` as the default branch of origin holds it (`worker_launch.pr_owner_of`), never
    the worktree's file: the worker could edit the switch that opens its rights. Unreadable or
    another value: `master`, so no PR owner right opens."""
    import worker_launch  # noqa: PLC0415

    root = _git(cwd, "rev-parse", "--show-toplevel")
    if not root:
        return "master"
    try:
        return worker_launch.pr_owner_of(_on_default(root, ".weside/config.json") or {})
    except worker_launch.LaunchError as exc:
        print(f"worker_rights: {exc}; no PR owner rights", file=sys.stderr)
        return "master"


def _flag_value(args: list[str], *names: str) -> str | None:
    for k, tok in enumerate(args):
        word, eq, value = tok.partition("=")
        if word in names:
            return value if eq else (args[k + 1] if k + 1 < len(args) else "")
    return None


def _pr_selector(args: list[str], value_flags: tuple[str, ...]) -> str | None:
    k = 2
    while k < len(args):
        tok = args[k]
        if tok in value_flags:
            k += 2
            continue
        if not tok.startswith("-"):
            return tok
        k += 1
    return None


def _push_refusal(argv: list[str], key: str, cwd: str | None) -> str | None:
    """A worker pushes only branches that carry its key: its own branch or its `wip/` checkpoint."""
    args, cdir, _confs = _git_split(argv)
    where = os.path.join(cwd or ".", cdir) if cdir else cwd
    positional: list[str] = []
    k = 1
    while k < len(args):
        if args[k] in PUSH_VALUE_FLAGS:
            k += 2
            continue
        if not args[k].startswith("-"):
            positional.append(args[k])
        k += 1
    branch = current_branch(where)
    for raw in positional[1:] or [branch]:
        src, _, dst = raw.lstrip("+").partition(":")
        name = (dst or src).removeprefix("refs/heads/")
        name = branch if name == "HEAD" else name
        if not name or not _carries(name, key):
            return f"git push to {name or 'a detached HEAD'}: a worker pushes only branches carrying {key}"
    return None


def _create_refusal(args: list[str], branch: str) -> str | None:
    head = _flag_value(args, "-H", "--head")
    if head is not None and head.rsplit(":", 1)[-1] != branch:
        return f"gh pr create --head {head}: a worker opens the PR of its own branch {branch}"
    extra = _pr_selector(args, VALUE_FLAGS["pr_create"])
    if extra is not None:
        return f"gh pr create takes no argument ({extra})"
    return None


def _merge_refusal(args: list[str], cwd: str | None) -> str | None:
    """`gh pr merge` holds only as `--disable-auto` alone (a fix round begins with it) or as the
    command `arm_pr.sh` prints with `ready`: `--match-head-commit` on this checkout's HEAD. That
    HEAD is the one `arm_pr.sh` classified (it runs the one-way check only when the checkout is the
    PR head), and GitHub refuses the merge when the PR's head moved since."""
    flags = [t.split("=", 1)[0] for t in args[2:] if t.startswith("-")]
    if "--disable-auto" in flags:
        extra = [f for f in flags if f not in ("--disable-auto", "-R", "--repo")]
        return f"gh pr merge --disable-auto takes no other flag ({extra[0]})" if extra else None
    sha = _flag_value(args, "--match-head-commit")
    if not sha:
        return "gh pr merge without --match-head-commit: a worker merges only the line arm_pr.sh prints as ready"
    for flag in flags:
        if flag not in MERGE_FLAGS:
            return f"gh pr merge {flag}: not in the line arm_pr.sh prints"
    head = _git(cwd, "rev-parse", "HEAD") or ""
    if not SHA.fullmatch(sha) or not head.startswith(sha):
        return f"gh pr merge --match-head-commit {sha}: not this checkout's HEAD {head[:12] or '(none)'}"
    return None


def _arm_argv(argv: list[str]) -> list[str]:
    """`arm_pr.sh` and its arguments, run directly or by a shell (`bash arm_pr.sh 7`), else []."""
    for k, tok in enumerate(argv[:2]):
        if os.path.basename(tok) == "arm_pr.sh" and (
            k == 0 or os.path.basename(argv[0]) in SHELLS
        ):
            return argv[k:]
    return []


def _arm_refusal(argv: list[str], slug: str | None, branch: str, cwd: str | None) -> str | None:
    run = _arm_argv(argv)
    repo = _flag_value(run[1:], "--repo", "-R")
    if repo and slug_of(repo) != slug:
        return f"{repo} is another repo than {slug}"
    selector, k = None, 1
    while k < len(run):
        if run[k] in ("--method", "--repo", "-R"):
            k += 2
            continue
        if not run[k].startswith("-"):
            selector = run[k]
        k += 1
    if selector is None:
        return "arm_pr.sh without a PR"
    return _own_pr_refusal(selector, slug, branch, cwd)


def _thread_refusal(args: list[str]) -> str | None:
    if any(t.split("=", 1)[0] == "--input" for t in args):
        return "gh api graphql --input: a worker resolves review threads by a query field only"
    queries = [
        v.split("=", 1)[1]
        for k, t in enumerate(args)
        if t in WRITE_FIELDS and k + 1 < len(args)
        for v in [args[k + 1]]
        if v.startswith("query=")
    ]
    mutations = {m for q in queries for m in re.findall(r"(\w+)\s*\(\s*input\s*:", q)}
    if len(queries) == 1 and mutations == {"resolveReviewThread"}:
        return None
    return "gh api graphql: a worker runs resolveReviewThread as the only mutation"


def _carries(name: str, key: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(key)}(?![\d])", name) is not None


def right_refusal(  # noqa: PLR0911, PLR0912
    argv: list[str],
    right: str,
    rights: dict,
    key: str | None,
    cwd: str | None,
    owner: str = "master",
) -> str | None:
    """Why a worker may not use `right` with this command here, else None (it holds)."""
    slug = origin_slug(cwd)
    if not (owner == "worker" and right in PR_OWNER_RIGHTS):
        if not opted_in(rights, slug):
            return f"{slug or 'this repo'} grants no worker rights (crew worker_rights.repos)"
        if not rights.get(right):
            return f"{right} is not granted in {slug} (crew worker_rights.{right})"
    if not key:
        return "this worker was started without a story key"
    branch = current_branch(cwd)
    if not branch or branch in protected_branches(cwd):
        return f"{branch or 'a detached HEAD'} is the default branch, not the worker's own"
    if not _carries(branch, key):
        return f"branch {branch} does not carry the key {key}"
    if right == "branch_rc":
        return None
    if right == "push":
        return _push_refusal(argv, key, cwd)
    if right == "arm":
        return _arm_refusal(argv, slug, branch, cwd)
    args, global_repo = _gh_split(argv)
    named = global_repo or _repo_flag(args)
    if named and slug_of(named) != slug:
        return f"{named} is another repo than {slug}"
    if right == "resolve_thread":
        return _thread_refusal(args)
    if right == "pr_create":
        return _create_refusal(args, branch)
    if right == "merge":
        problem = _merge_refusal(args, cwd)
        if problem:
            return problem
    if right in ("pr_edit", "merge"):
        selector = _pr_selector(args, VALUE_FLAGS[right])
    else:
        selector = _selector(args)
    if selector is None:
        return None
    return _own_pr_refusal(selector, slug, branch, cwd)


def _own_pr_refusal(selector: str, slug: str | None, branch: str, cwd: str | None) -> str | None:
    """Why PR `selector` is not the PR whose head is `branch` in `slug`, else None."""
    try:
        head = _gh_pr_head(selector, slug, cwd)
    except Exception as exc:  # fail closed: an unresolved PR is no own PR
        return f"PR {selector} could not be resolved ({exc})"
    owner = ((head.get("headRepositoryOwner") or {}).get("login") or "").lower()
    repo = ((head.get("headRepository") or {}).get("name") or "").lower()
    if head.get("headRefName") != branch:
        return f"PR {selector} is not this branch's PR (its head is {head.get('headRefName')})"
    if owner and repo and f"{owner}/{repo}" != slug:
        return f"PR {selector} comes from {owner}/{repo}"
    return None


# ---------------------------------------------------------------- the brake log


BRAKE_LOG = "brakes.jsonl"


def brake_log(cwd: str | Path | None, record: dict) -> Path | None:
    """Append one JSON line to `<state dir>/brakes.jsonl` (`crew.state_dir`: `WE_STATE_DIR`, else
    the crew's `state_dir`, else `~/.claude/we-state`); best effort, None when nothing was written.

    Line shape (this function owns it): `ts` (UTC ISO, stamped here), `type`, then per type
      `deny`  (the worker rights gate): `gate`, `item` (the never-list item or the failed right),
              `name`, `key`, `session_id`, `argv0` (the first word of the command; never the
              command itself, which may carry a secret value)
      `card`  (a card worker's verdict, written by its starter where it posts it; `main` below):
              `verdict` (`PASS` or `BLOCKING`), `pr`
      `outcome` (added later by the starter): `kind` (the line's type it judges, e.g. `card`),
              `pr`, `danger` (`yes`: the danger it is built against was there; `no`: a false alarm)
    One line per event, so a brake's false alarms can be counted.

    `WE_STATE_FORBIDDEN` (directories, `os.pathsep`-separated; the test run's `conftest.py` sets
    it to the live state directories) refuses a write there: the line goes to the file named by
    `WE_STATE_LEAKS` instead, and the test run turns red on it."""
    import contextlib  # noqa: PLC0415
    import datetime as dt  # noqa: PLC0415

    import crew as crew_mod  # noqa: PLC0415

    with contextlib.suppress(Exception):
        try:
            path, crew = crew_mod.find_and_load(cwd or Path.cwd())
        except crew_mod.CrewError:
            path, crew = None, None
        out = crew_mod.state_dir(path, crew) / BRAKE_LOG
        line = {"ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **record}
        forbidden = {
            os.path.realpath(os.path.expanduser(d))
            for d in os.environ.get("WE_STATE_FORBIDDEN", "").split(os.pathsep)
            if d.strip()
        }
        if os.path.realpath(str(out.parent)) in forbidden:
            leaks = os.environ.get("WE_STATE_LEAKS", "").strip()
            if leaks:
                with open(leaks, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"path": str(out), **line}, ensure_ascii=False) + "\n")
            return None
        out.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(out), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return out
    return None


def argv0(command: str) -> str:
    """The first word of `command` (its basename), for a log line that must not carry values."""
    try:
        words = _strip_prefixes(_tokens(HEREDOC.sub("", command).split("\n", 1)[0]))
    except ValueError:
        words = command.split()[:1]
    return os.path.basename(words[0]) if words else ""


# ---------------------------------------------------------------- the settings record


DELETE_NOUNS = (
    "repo",
    "release",
    "label",
    "run",
    "cache",
    "gist",
    "ssh-key",
    "gpg-key",
    "project",
    "codespace",
    "ruleset",
    "workflow",
)


def never_rules(protected: set[str]) -> list[str]:
    rules = ["Bash(*gh secret *)", "Bash(*gh variable *)"]
    for path in ("secrets", "variables", "environments"):
        for flag in ("-X", "--method", "-f ", "-F ", "--field", "--raw-field", "--input"):
            rules.append(f"Bash(*gh api*/{path}*{flag}*)")
    rules += [f"Bash(*gh {noun} delete*)" for noun in DELETE_NOUNS]
    rules += [
        "Bash(*--delete-branch*)",
        "Bash(*gh api*-X DELETE*)",
        "Bash(*gh api*-XDELETE*)",
        "Bash(*gh api*--method DELETE*)",
        "Bash(*gh api*--method=DELETE*)",
    ]
    rules += ["Bash(*git push*--delete*)", "Bash(*git push* -d *)", "Bash(*git push* :*)"]
    for name in sorted(protected):
        rules += [
            f"Bash(*git push* {name})",
            f"Bash(*git push* {name} *)",
            f"Bash(*git push*:{name})",
            f"Bash(*git push*:{name} *)",
            f"Bash(*git push*refs/heads/{name}*)",
        ]
    rules += [
        "Bash(*git push*--tags*)",
        "Bash(*git push*--follow-tags*)",
        "Bash(*git push*refs/tags/*)",
        "Bash(*git push* tag *)",
        "Bash(*git push*--all*)",
        "Bash(*git push*--mirror*)",
    ]
    rules += [
        "Bash(*ALLOW_PUSH_WITHOUT_MAIN*)",
        "Bash(*ALLOW_COMMIT_TO_MAIN*)",
        "Bash(*--no-verify*)",
        "Bash(*core.hooksPath*)",
    ]
    return rules


# the PR owner rights as allow rules, so a worker in the default mode is not asked; the gate's
# conditions (key, own PR, the merge line) still decide each call
PR_OWNER_ALLOW = (
    "Bash(git push*)",
    "Bash(gh pr create*)",
    "Bash(gh pr edit*)",
    "Bash(gh pr comment*)",
    "Bash(*arm_pr.sh *)",
    "Bash(gh pr merge *--disable-auto*)",
    "Bash(gh pr merge *--match-head-commit*)",
    "Bash(gh api graphql*resolveReviewThread*)",
)


def settings(repo: str | Path, key: str) -> dict:
    """The `--settings` document of a worker with `key` in `repo`: allow = the granted rights
    (with `build.pr_owner: worker` also `PR_OWNER_ALLOW`), deny = the never-list (and the crew's `deny_commands`, `deny_text`; a `deny_text` path also as a
    Read rule)."""
    rights = crew_rights(repo)
    allow: list[str] = []
    if opted_in(rights, origin_slug(repo)):
        if rights.get("pr_ready"):
            allow.append("Bash(gh pr ready*)")
        if rights.get("pr_comment"):
            allow.append("Bash(gh pr comment*)")
        if rights.get("branch_rc"):
            allow.append(f"Bash({rights['branch_rc']}*)")
    if pr_owner_on_default(repo) == "worker":
        allow += [rule for rule in PR_OWNER_ALLOW if rule not in allow]
    deny = never_rules(protected_branches(repo))
    deny += [f"Bash(*{c}*)" for c in rights.get("deny_commands") or [] if c]
    for text in rights.get("deny_text") or []:
        if text:
            deny.append(f"Bash(*{text}*)")
            if text.startswith("/"):
                deny.append(f"Read(/{text}**)")
    return {"permissions": {"allow": allow, "deny": deny}, "env": worker_env(key)}


def worker_env(key: str, environ: Any = None) -> dict:
    """The `env` block of a worker's `--settings`: what `we-worker` sets and clears at its start.

    A `claude --bg` session runs in the daemon's environment, not the launcher's (the daemon may
    carry `WE_ROLE=worker` and another story's key, or a pin), so every variable the start decides
    is stated here; a cleared one is "": readers take it as unset, and an empty `WESIDE_COMPANION`
    also marks a deliberate worker (`materialize_on_start.py` skips its user warning). The
    launcher's own start variables (`WE_START`, `WE_WORKER_STARTER`) are cleared the same way: a
    worker never runs its starter's start check, and its own workers name it, not its starter, as
    theirs. `WE_HUMAN` passes on when the launcher has it. Never a credential: this block is
    written to a file."""
    env = os.environ if environ is None else environ
    doc = {
        "WE_ROLE": "worker",
        "WE_STORY_KEY": key,
        "WESIDE_COMPANION": "",
        "WE_ROLE_ID": "",
        "WE_START": "",
        "WE_WORKER_STARTER": "",
    }
    if env.get("WE_HUMAN"):
        doc["WE_HUMAN"] = env["WE_HUMAN"]
    return doc


# ---------------------------------------------------------------- the brake log CLI

CARD_OVERALL = re.compile(r"^\**Overall:\s*(PASS|BLOCKING)\b", re.MULTILINE)


def main(argv: list[str]) -> int:
    """`worker_rights.py brake card --pr <ref> --file <verdict file>`: one `card` line, the verdict
    read from the file's last `Overall:` line; `worker_rights.py brake outcome --pr <ref> --kind
    <type> --danger yes|no`: one `outcome` line. Prints the log's path; exit 2 on a bad call or a
    file without a verdict, 1 when nothing was written."""
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="worker_rights.py")
    verbs = parser.add_subparsers(dest="noun", required=True)
    brake = verbs.add_parser("brake").add_subparsers(dest="verb", required=True)
    card = brake.add_parser("card")
    card.add_argument("--pr", required=True)
    card.add_argument("--file", required=True, type=Path)
    outcome = brake.add_parser("outcome")
    outcome.add_argument("--pr", required=True)
    outcome.add_argument("--kind", required=True)
    outcome.add_argument("--danger", required=True, choices=("yes", "no"))
    args = parser.parse_args(argv)
    if args.verb == "card":
        try:
            found = CARD_OVERALL.findall(args.file.read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"worker_rights: {exc}", file=sys.stderr)
            return 2
        if not found:
            print(
                f"worker_rights: {args.file} has no `Overall: PASS|BLOCKING` line", file=sys.stderr
            )
            return 2
        record = {"type": "card", "verdict": found[-1], "pr": args.pr}
    else:
        record = {"type": "outcome", "kind": args.kind, "pr": args.pr, "danger": args.danger}
    out = brake_log(None, record)
    if out is None:
        print("worker_rights: no line written", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
