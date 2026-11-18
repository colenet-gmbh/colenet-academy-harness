# Code notes

What a comment, a docstring or a doc carries, written for its one reader: the next agent, who
wants the overview fast and the right facts where it works. A note pays where the code cannot say why.

## The head of a package's entry file

The note that pays most. An agent orients itself by reading this head, then searching by name;
write it for that move. In Python the `__init__.py` docstring, elsewhere the index or main module:
what the package is for, the one way in, what must not be imported or called from outside, the
decision behind it with its record. A few lines, read in milliseconds.

## Top of any other file

Two to five lines: what the module is for, what calls it, what it hands on. It saves opening
three neighbours.

## Where a reader would turn wrong

One line at the spot: why it is this way and not the obvious way, the trap, or the decision with
who and when. A stale note misleads harder than none, so it moves with the decision.

| Bad | Good |
|---|---|
| A lock's docstring says "no session may write while held"; a later decision allowing the owner's session to write stands only in a plan and a test | The docstring says "held by the writer; the owner's session may write through it too (owner, date)" |

## Nothing the code already says

No retold steps, parameter lists, copied numbers or line numbers: they rot when the code moves.
Name the symbol that holds a value (`BUDGET` in `render_process.py`), not the value.

## Docs and decisions

A separate doc only for what no single file can hold (a flow across subsystems, a contract between
them): short, its first sentence saying why the code could not carry it. An important decision
goes where the next agent works (the comment at the code, the config key's comment, the glossary
line); the board, a chat and a plan's log are transport, not memory.

No test pins such text (`test-economy.md` § No test pins text): it fails on rewording, catches no defect.
