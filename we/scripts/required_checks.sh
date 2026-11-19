#!/usr/bin/env bash
# The required check names of a branch, live from GitHub: its rulesets and its branch protection.
# The one reader of required checks: `arm_pr.sh` and `flow_report.py` both call it.
#
# Usage: required_checks.sh --repo <owner/repo> [<branch>]   (default: the repo's default branch)
#
# Prints one name per line, sorted, none when the branch requires no check (exit 0); on a gh
# failure one line `gh failed: <error>` and exit 2.
set -uo pipefail

REPO="" BRANCH=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo|-R) REPO=${2:-}; shift 2 || { echo "gh failed: usage: required_checks.sh --repo <owner/repo> [<branch>]"; exit 2; } ;;
    *) BRANCH=$1; shift ;;
  esac
done
[ -n "$REPO" ] || { echo "gh failed: usage: required_checks.sh --repo <owner/repo> [<branch>]"; exit 2; }
# stdout only is the value: gh prints notices (upgrade, preview commands) on stderr, even on success.
GH_ERR=$(mktemp) && trap 'rm -f "$GH_ERR"' EXIT
gh_failed() { echo "gh failed: $(sed '/^[[:space:]]*$/d' "$GH_ERR" | tail -n 1)"; exit 2; }
if [ -z "$BRANCH" ]; then
  BRANCH=$(gh api "repos/$REPO" --jq .default_branch 2>"$GH_ERR") || gh_failed
fi
rules=$(gh api "repos/$REPO/rules/branches/$BRANCH" \
  --jq '.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context' 2>"$GH_ERR") \
  || gh_failed
prot=$(gh api "repos/$REPO/branches/$BRANCH" \
  --jq '(.protection.required_status_checks.contexts // [])[]' 2>"$GH_ERR") \
  || gh_failed
printf '%s\n%s\n' "$rules" "$prot" | sed '/^$/d' | sort -u
