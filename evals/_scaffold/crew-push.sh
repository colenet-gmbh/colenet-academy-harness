#!/usr/bin/env bash
# The generic crew repo of crew.sh, whose config lands plans by push (`plans.land: push`).
set -eu
bash "$(dirname "$0")/crew.sh"
cat > .weside/config.json <<'J'
{ "plans": { "land": "push", "push_paths": ["docs/plans/**"] } }
J
git add -A && git commit -qm "config: plans land by push"
