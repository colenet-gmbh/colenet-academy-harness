#!/usr/bin/env bash
# crew.sh plus the process-worker marker: the session gets the worker card and the worker L0.
set -eu
bash "$(dirname "$0")/crew.sh"
echo '{"name": "acme-worker"}' > .git/we-worker.json
