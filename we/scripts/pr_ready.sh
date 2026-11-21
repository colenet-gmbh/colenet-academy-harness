#!/usr/bin/env bash
# Take a PR out of draft, or put it back, with its `**Draft:**` line in one step.
#
# Usage: pr_ready.sh <PR> ready [--sha <head>] [-R <owner/repo>]
#        pr_ready.sh <PR> undo "<reason> — ready when <condition>" [-R <owner/repo>]
#   ready  checks first, edits after: a moved head (`--sha`) or a conflict with the base refuses
#          before the body is touched, so a refused ready never leaves a draft without its line.
#          Then the body's leading `**Draft:**` line goes and the PR leaves draft.
#   undo   puts `**Draft:** <reason>` first in the body (replacing a leading one), then the PR
#          goes back to draft; the reason holds no `<placeholder>` (`draft_reason_gate.py`).
# Prints one line and exits:
#   ready <PR> <sha>                                   0
#   draft <PR>: <reason>                               0
#   <PR> is already ready | <PR> is already a draft    0  (nothing changed)
#   refused: head is <sha>, not <expected>             3
#   refused: <PR> is <state>                           8
#   refused: <PR> conflicts with its base — merge it   12 (GitHub starts no CI for a ready event
#       on a conflicting PR: draft_reason_gate.py)
#   usage, a <placeholder> reason or a gh error        2  (a failed flip puts the body back)
# The repo comes from -R or `gh repo view`, never from the environment: worker_rights_gate reads
# the command line, and an exported selector would point the call at a PR it never sees.
# Env: PR_READY_WAIT (s between mergeable reads, default 3; tests only).
set -uo pipefail

USAGE='usage: pr_ready.sh <PR> ready [--sha <head>] [-R <owner/repo>] | pr_ready.sh <PR> undo "<reason> — ready when <condition>" [-R <owner/repo>]'
PR=""
VERB=""
REASON=""
EXPECT=""
REPO=""
usage() { echo "$USAGE" >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --sha) EXPECT=${2:-}; [ -n "$EXPECT" ] || usage; shift ;;
    -R|--repo) REPO=${2:-}; [ -n "$REPO" ] || usage; shift ;;
    -*) usage ;;
    *)
      if [ -z "$PR" ]; then PR=$1
      elif [ -z "$VERB" ] && { [ "$1" = ready ] || [ "$1" = undo ]; }; then VERB=$1
      elif [ "$VERB" = undo ] && [ -z "$REASON" ]; then REASON=$1
      else usage; fi ;;
  esac
  shift
done
[ -n "$PR" ] && [ -n "$VERB" ] || usage
if [ "$VERB" = undo ]; then
  REASON=$(tr '\n' ' ' <<<"$REASON" | sed 's/[[:space:]]*$//')
  [ -n "$REASON" ] || usage
  if grep -q '<[^<>]*>' <<<"$REASON"; then
    echo "refused: the reason holds a <placeholder>: $REASON" >&2; exit 2
  fi
fi
if [ -z "$REPO" ]; then
  REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null) \
    || { echo "pr_ready.sh: no repo, pass -R <owner/repo>" >&2; exit 2; }
fi

read_pr() {
  local i view
  for i in 1 2 3; do
    view=$(gh pr view "$PR" -R "$REPO" --json state,isDraft,mergeable,headRefOid \
      --jq '[.state, (.isDraft | tostring), .mergeable, .headRefOid] | @tsv' 2>/dev/null) \
      || { echo "pr_ready.sh: gh pr view $PR failed" >&2; exit 2; }
    IFS=$'\t' read -r STATE DRAFT MERGEABLE HEAD <<<"$view"
    # GitHub computes `mergeable` lazily: UNKNOWN right after a push.
    if [ "$MERGEABLE" = UNKNOWN ] && [ "$i" -lt 3 ]; then sleep "${PR_READY_WAIT:-3}"; continue; fi
    return 0
  done
}
body() {  # the PR body into RAW_FILE, read in the main shell so a failure ends the script
  gh pr view "$PR" -R "$REPO" --json body --jq .body >"$RAW_FILE" 2>/dev/null \
    || { echo "pr_ready.sh: reading the body of $PR failed" >&2; exit 2; }
}
# The body without a `**Draft:**` line that is its first non-empty line, nor the blank lines
# around it.
strip_draft() {
  awk 's == 0 && /^[[:space:]]*$/ { next }
    s == 0 && /^[[:space:]]*\*\*Draft:\*\*/ { s = 1; next }
    s == 1 && /^[[:space:]]*$/ { next }
    { s = 2; print }'
}
edit_body() {
  gh pr edit "$PR" -R "$REPO" --body-file "$1" >/dev/null \
    || { echo "pr_ready.sh: gh pr edit $PR failed" >&2; exit 2; }
}
# The flip failed after the edit: the body goes back, so the PR's draft state and its line agree.
restore() {
  gh pr edit "$PR" -R "$REPO" --body-file "$RAW_FILE" >/dev/null \
    || echo "pr_ready.sh: restoring the body of $PR failed too" >&2
  echo "pr_ready.sh: $1 $PR failed, body restored" >&2
  exit 2
}

BODY_FILE=$(mktemp) && RAW_FILE=$(mktemp) || exit 2
trap 'rm -f "$BODY_FILE" "$RAW_FILE"' EXIT
read_pr
[ "$STATE" = OPEN ] || { echo "refused: $PR is $STATE"; exit 8; }
if [ "$VERB" = ready ]; then
  [ "$DRAFT" = true ] || { echo "$PR is already ready"; exit 0; }
  case "$HEAD" in
    "${EXPECT:-$HEAD}"*) ;;
    *) echo "refused: head is $HEAD, not $EXPECT"; exit 3 ;;
  esac
  [ "$MERGEABLE" != CONFLICTING ] || { echo "refused: $PR conflicts with its base — merge it"; exit 12; }
  body
  strip_draft <"$RAW_FILE" >"$BODY_FILE"
  edit_body "$BODY_FILE"
  gh pr ready "$PR" -R "$REPO" >/dev/null || restore ready
  echo "ready $PR $HEAD"
else
  [ "$DRAFT" = false ] || { echo "$PR is already a draft"; exit 0; }
  body
  { printf '**Draft:** %s\n\n' "$REASON"; strip_draft <"$RAW_FILE"; } >"$BODY_FILE"
  edit_body "$BODY_FILE"
  gh pr ready "$PR" -R "$REPO" --undo >/dev/null || restore undo
  echo "draft $PR: $REASON"
fi
