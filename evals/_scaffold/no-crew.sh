#!/usr/bin/env bash
# A git repo without a crew file: no L0, no role card.
set -eu
git init -q -b main .
printf '# acme\n' > README.md
