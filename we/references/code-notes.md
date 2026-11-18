# Code notes

What a comment, a docstring or a doc carries, written for the one reader it has: the next agent,
who wants the overview fast and the right facts at the place it works. The code is the first
documentation; a note pays where the code cannot say why.

## Top of a file

Two to five lines: what the module is for and where it sits in the larger flow (what calls it,
what it hands on). It is read first and saves opening three neighbours to learn the same.

## Where a reader would turn wrong

One line at the spot: why it is this way and not the obvious other way, the trap, or the decision
behind it with who and when. A stale note there misleads harder than none, so it moves with the
decision.

| Bad | Good |
|---|---|
| A lock's docstring says "no session may write while held"; a later decision allowing the owner's session to write stands only in a plan and a test | The docstring says "held by the writer; the owner's session may write through it too (owner, date)", so the reader of the lock sees the decision |

## Nothing the code already says

No retold steps, no parameter lists, no copied numbers or line numbers: they rot when the code
moves. Name the symbol that holds the value (`BUDGET` in `render_process.py`) instead of its value.

## A separate doc is the exception

Only for what no single file can hold: a flow across subsystems, a contract between them. Keep it
short, and let its first sentence say why the code could not carry it.

## Decisions land where the next agent works

An important decision goes into the comment at the code, the config key's comment or the glossary
line. The board, a chat and a plan's log are transport, not memory: the next agent reads the code,
not last week's queue.

No test pins such text (`test-economy.md` § No test pins text): a test on prose fails on every
rewording and catches no defect.
