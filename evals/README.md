# Repeat cases for `claude plugin eval`

Small cases, each taken from a real failure of the crew process. Each case puts one prompt to the
plugin `we` and grades the answer with free graders. Use them to check, cheaply and before release,
whether a cut in instruction text or a model change breaks something that once went wrong. They do
not replace the pytest suites: those test the scripts, these test what a session *does* with the
text.

## Run

From the repo root (the target `.` resolves the plugin `we/` through each case's `plugins:` line):

```bash
claude plugin eval . --scaffold --trust-plugin --no-publish -j 6 --json /tmp/eval.json
python3 scripts/eval_summary.py /tmp/eval.json --out evals/results/$(date -u +%F).md
```

- One case: `--case worker-main-checkout`; one family: `--tag family-3`.
- `--scaffold` is needed: every case builds its repo with `_scaffold/*.sh` (a `git init`, a generic
  crew file, for a worker the process-worker marker). The scripts are this repo's own; read them
  before you pass the flag.
- The default ablation `with-without` runs every case once with the plugin and once without it.
  `aggregate-result.json` holds `score`, `scoreWithout` and `delta` per case. `eval_summary.py`
  writes the committed table, which holds no machine path and no transcript.
- Runs go to `evals/results/<timestamp>/`. Git ignores them; only the summary `.md` is committed.
- Costs on the first run (Opus, 2 runs × 2 arms): about 0.08 USD and 15 s per run. With `-j 6`
  the whole set finishes in a few minutes.

The set runs on a model change and before a large cut in instruction text. There is no CI job on
purpose, because every run costs money.

## A case

```text
evals/<case>/case.yaml     prompt, graders, model, runs, tags (family-<n>), description = the failure
evals/<case>/scaffold.sh   one line: exec one of evals/_scaffold/*.sh
```

`scaffold.sh` sits in the case directory because `claude plugin eval` refuses a `scaffold_script`
path that leaves the case. It hands over to the shared script by `exec`.

Rules:

- **From a real failure.** `description:` names the date and what went wrong, without ticket keys,
  internal names or hosts, because the repo is public. Fixture names are generic: crew `ACME-<n>`,
  human Hal, members Ada, Otto and the rest.
- **One prompt, one behaviour.** A whole story is no case.
- **Pinned model**, the crew's build model (`model: claude-opus-5-5`). `--model` overrides it for a
  model comparison.
- **Free graders only:** `regex` (on `last_message`, by default), `tool_used`, `tool_order`,
  `file_exists`. `llm` and `baseline` call a judge model and cost money, so they are not used here.
- **Under a minute**: `max_turns: 8`, `timeout_seconds: 240`, read-only tools
  (`Read, Glob, Grep, Skill`).
- **No real servers.** The plugin's MCP server does not start (no mock under `mocks/`), and
  `--allow-real-servers` is never passed.

Good and bad graders for the same rule ("never merge a one-way PR"):

| Bad | Good |
|---|---|
| `pattern: "gh pr merge"`, `match: not_contains`: fails on the prose "I won't run `gh pr merge`" | `pattern: "^\\s*(\\$\\s*)?gh\\s+pr\\s+merge"`, `flags: m`: only a command line counts |

## Add a case from a failure

1. Copy the closest case directory and rename it after the behaviour (`<area>-<behaviour>`).
2. Write the failure into `description:` (date, what happened), the situation into the prompt
   (what the session sees, who it is) and the ask ("reply with the exact command, do not run it").
3. Grade the command shape, not the wording: one grader for the right move and one `not_contains`
   for the failure itself.
4. Run it alone, three times: `--case <name> --runs 3`. Read the answers (`--keep-temp`, then the
   `out/trace.jsonl` of a run) before you trust a red grader. A case that is red in both arms is
   still a case: it shows that the plugin does not yet carry that behaviour.

## What a case cannot test here

- **Roles by environment.** A case passes only variables named `EVAL_*` to the session. An `env`
  in a project `.claude/settings.json` written by the scaffold does not reach the hooks either. A
  pinned role (`WESIDE_COMPANION`) or `WE_ROLE=master` therefore never gets to the role hook. What
  a case can reach: the worker card (the scaffold writes the process-worker marker
  `.git/we-worker.json`) and the role-less L0 (a crew file without a pin). The cold start of every
  other role stays in `tests/coldstart/`.
- **Bash.** On a host whose kernel refuses the sandbox's `/proc` mount (a container without user
  namespaces), every Bash call fails inside the eval sandbox. The cases therefore ask for the
  command and grade the answer by regex, instead of running `board.py` and grading its exit code
  or the board file.
- **A continued transcript.** `context.history_file` resumes a recorded session file
  (`--resume`). That ties the case to one CLI's session format, and the eval may then skip the
  without-plugin arm. Fix-round cases therefore state in the prompt where the session stands.
