#!/usr/bin/env bash
# crew-push.sh plus the process-worker marker: a worker in a repo whose plans land by push.
set -eu
bash "$(dirname "$0")/crew-push.sh"
echo '{"name": "acme-worker"}' > .git/we-worker.json
