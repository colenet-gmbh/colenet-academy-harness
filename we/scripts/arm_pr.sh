#!/usr/bin/env bash
# shellcheck disable=SC2016  # GraphQL and jq variables stay literal
# Arm one PR's auto-merge on the head it read. It never merges, in any branch of its code.
#
# Usage: arm_pr.sh <PR> [--method merge|squash|rebase] [--repo <owner/repo>]
#
# Arms only through the GraphQL mutation `enablePullRequestAutoMerge`, with the head SHA it read as
# `expectedHeadOid`: GitHub then merges that head once the repo's required checks pass, and a push
# after the read makes the mutation fail instead of arming a head nobody looked at. `gh pr merge`
# with its auto flag is never called: when the PR looks clean (right after a push GitHub reports the
# old head's state for seconds) gh merges at once instead of arming, and a bypass actor merges past
# every check of the new head.
#
# When GitHub answers that the PR is already in a clean (or unstable) state, the helper does not
# merge: it waits until check runs of the head are registered, re-reads, arms if a required check is
# still pending, and reports `ready` once every required check of the base branch is green on that
# head, GitHub calls the PR CLEAN and no required review is missing. The caller then merges as a
# separate, explicit command with `--match-head-commit <sha>`.
#
# Before the arm, only in a checkout whose HEAD is the PR head, the risk classifier (`risk_paths.py`
# over `origin/<base>:.weside/risk-paths`) runs; a one-way class is refused: only the human merges
# it. Anywhere else a stderr note says it was not classified: the caller's class (`/we:master`
# § Close the run) stays the one-way gate.
#
# One final line and its exit code:
#   armed: auto-merge on <sha> (<method>)                                        exit 0
#   gh failed: <error>                                                           exit 2  (also usage)
#   ready: all required checks green on <sha> — merge: gh pr merge <PR> --<method> --match-head-commit <sha>
#                                                                                exit 3
#   refused: draft | refused: <state>                                            exit 4
#   refused: head moved <old> -> <new>                                           exit 5
#   refused: one-way <class> — only the human merges it                          exit 6
#   red: <names> on <sha> — not armed                                            exit 7
#   no checks on <sha> after <n>s — not armed, run again                         exit 8
#   not armed on <sha> after <n>s (<mergeStateStatus>, review <decision>) — run again   exit 8
#
# Env: ARM_INTERVAL (seconds between reads, default 10), ARM_REGISTER_TIMEOUT (default 300).
set -uo pipefail

usage() { echo "gh failed: usage: arm_pr.sh <PR> [--method merge|squash|rebase] [--repo <owner/repo>]"; exit 2; }

PR="" METHOD=merge REPO=""
while [ $# -gt 0 ]; do
  case "$1" in
    --method) METHOD=${2:-}; shift 2 || usage ;;
    --repo|-R) REPO=${2:-}; shift 2 || usage ;;
    -h|--help) sed -n '3,/^set -uo/p' "$0" | sed '$d'; exit 0 ;;
    -*) usage ;;
    *) [ -z "$PR" ] || usage; PR=$1; shift ;;
  esac
done
[[ "$PR" =~ ^[0-9]+$ ]] || usage
case "$METHOD" in merge|squash|rebase) ;; *) usage ;; esac
INTERVAL=${ARM_INTERVAL:-10}
TIMEOUT=${ARM_REGISTER_TIMEOUT:-300}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

if [ -z "$REPO" ]; then
  REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>&1) || { echo "gh failed: $(tail -n 1 <<<"$REPO")"; exit 2; }
fi
OWNER=${REPO%%/*} NAME=${REPO#*/}

# Line 1: id, state, isDraft, head, base, mergeStateStatus, reviewDecision (NONE when the repo
# requires no review). Then one line per check on the head:
# <pending|pass|fail>\t<required by the PR: true|false>\t<name>. Checks of another commit (the
# last commit lagging behind the head) are not listed.
READ='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){
  id state isDraft headRefOid baseRefName mergeStateStatus reviewDecision
  commits(last:1){nodes{commit{oid statusCheckRollup{contexts(first:100){nodes{__typename
    ... on CheckRun{name status conclusion isRequired(pullRequestNumber:$number)}
    ... on StatusContext{context state isRequired(pullRequestNumber:$number)}}}}}}}}}}'
JQ='.data.repository.pullRequest as $p
  | ([$p.id, $p.state, ($p.isDraft|tostring), $p.headRefOid, $p.baseRefName,
      ($p.mergeStateStatus // "UNKNOWN"), ($p.reviewDecision // "NONE")] | @tsv),
    ($p.commits.nodes[0].commit // {} | select(.oid == $p.headRefOid)
     | (.statusCheckRollup.contexts.nodes // [])[]
     | [(if .__typename == "StatusContext" then
           (if .state == "SUCCESS" then "pass" elif (.state == "PENDING" or .state == "EXPECTED") then "pending" else "fail" end)
         elif .status != "COMPLETED" then "pending"
         elif (.conclusion == "SUCCESS" or .conclusion == "NEUTRAL" or .conclusion == "SKIPPED") then "pass"
         else "fail" end),
        (.isRequired | tostring), (.name // .context)] | @tsv)'
ARM='mutation($id:ID!,$sha:GitObjectID!,$method:PullRequestMergeMethod!){
  enablePullRequestAutoMerge(input:{pullRequestId:$id,expectedHeadOid:$sha,mergeMethod:$method}){
    pullRequest{autoMergeRequest{enabledAt}}}}'

SHA=""
read_pr() {  # sets ID STATE DRAFT HEAD BASE MSTATE REVIEW CHECKS; exits on a gh failure or a moved head
  local out
  out=$(gh api graphql -f query="$READ" -f owner="$OWNER" -f name="$NAME" -F number="$PR" --jq "$JQ" 2>&1) \
    || { echo "gh failed: $(tail -n 1 <<<"$out")"; exit 2; }
  IFS=$'\t' read -r ID STATE DRAFT HEAD BASE MSTATE REVIEW <<<"$(head -n 1 <<<"$out")"
  CHECKS=$(tail -n +2 <<<"$out")
  [ -n "${HEAD:-}" ] || { echo "gh failed: no PR $PR in $REPO"; exit 2; }
  [ "$STATE" = OPEN ] || { echo "refused: $(tr "[:upper:]" "[:lower:]" <<<"$STATE")"; exit 4; }
  [ "$DRAFT" = false ] || { echo "refused: draft"; exit 4; }
  if [ -n "$SHA" ] && [ "$HEAD" != "$SHA" ]; then echo "refused: head moved $SHA -> $HEAD"; exit 5; fi
  SHA=$HEAD
}

one_way() {  # refuses a one-way class when this checkout is the PR head; a stderr note otherwise
  local line skip="one-way: not classified here (this checkout is not the PR head) — the class is the caller's"
  [ "$(git rev-parse HEAD 2>/dev/null)" = "$SHA" ] || { echo "$skip" >&2; return 0; }
  git cat-file -e "origin/$BASE:.weside/risk-paths" 2>/dev/null || return 0
  line=$(python3 "$HERE/risk_paths.py" "origin/$BASE" 2>/dev/null | head -n 1) || return 0
  case "$line" in "T2 one-way "*) echo "refused: one-way ${line#T2 one-way } — only the human merges it"; exit 6 ;; esac
}

REQUIRED=""
required_names() {  # the base branch's required check names: its rulesets and its branch protection
  local rules prot
  rules=$(gh api "repos/$REPO/rules/branches/$BASE" \
    --jq '.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context' 2>&1) \
    || { echo "gh failed: $(tail -n 1 <<<"$rules")"; exit 2; }
  prot=$(gh api "repos/$REPO/branches/$BASE" \
    --jq '(.protection.required_status_checks.contexts // [])[]' 2>&1) \
    || { echo "gh failed: $(tail -n 1 <<<"$prot")"; exit 2; }
  REQUIRED=$(printf '%s\n%s\n' "$rules" "$prot" | sed '/^$/d' | sort -u)
}

arm() {  # 0 armed (exits), 1 GitHub calls the PR clean, exits on any other answer
  local out
  if out=$(gh api graphql -f query="$ARM" -f id="$ID" -f sha="$SHA" -f method="$(tr "[:lower:]" "[:upper:]" <<<"$METHOD")" 2>&1); then
    echo "armed: auto-merge on $SHA ($METHOD)"; exit 0
  fi
  if grep -qiE 'is in (clean|unstable) status' <<<"$out"; then return 1; fi
  read_pr  # a head that moved since the read refuses here, with both SHAs
  echo "gh failed: $(tail -n 1 <<<"$out")"; exit 2
}

verdict() {  # pending | pass | fail:<names> for the required checks on the head
  local name v req fails="" pending="" seen=0
  if [ -n "$REQUIRED" ]; then
    while IFS= read -r name; do
      v=$(awk -F'\t' -v n="$name" '$3 == n { print $1 }' <<<"$CHECKS")
      case "$v" in
        "") pending=1 ;;  # absent is not green
        *fail*) fails="$fails, $name" ;;
        *pending*) pending=1 ;;
      esac
    done <<<"$REQUIRED"
  fi
  # A check GitHub marks required for this PR counts too; with no required check at all, every
  # registered check must pass.
  while IFS=$'\t' read -r v req name; do
    [ -n "${name:-}" ] || continue
    seen=1
    grep -qxF -- "$name" <<<"$REQUIRED" && continue  # judged above by name
    if [ "$req" = true ] || [ -z "$REQUIRED" ]; then
      case "$v" in fail) fails="$fails, $name" ;; pending) pending=1 ;; esac
    fi
  done <<<"$CHECKS"
  if [ -n "$fails" ]; then echo "fail:${fails#, }"
  elif [ "$seen" = 0 ] || [ -n "$pending" ]; then echo pending
  else echo pass; fi
}

read_pr
one_way
arm  # returns only on a clean answer
required_names
start=$(date +%s)
while :; do
  if [ $(($(date +%s) - start)) -ge "$TIMEOUT" ]; then
    if [ -z "${CHECKS:-}" ]; then echo "no checks on $SHA after ${TIMEOUT}s — not armed, run again"
    else echo "not armed on $SHA after ${TIMEOUT}s ($MSTATE, review $REVIEW) — run again"; fi
    exit 8
  fi
  sleep "$INTERVAL"
  read_pr
  [ -n "$CHECKS" ] || continue  # the head's check runs are not registered yet
  v=$(verdict)
  case "$v" in
    fail:*) echo "red: ${v#fail:} on $SHA — not armed"; exit 7 ;;
    pending) arm ;;  # a required check runs: GitHub can wait for it now
    pass)
      # Green checks are not every requirement: a review or another rule GitHub still waits for
      # leaves the PR not CLEAN, and then it is armed instead.
      [ "$MSTATE" = CLEAN ] && [ "$REVIEW" != REVIEW_REQUIRED ] && [ "$REVIEW" != CHANGES_REQUESTED ] || { arm; continue; }
      echo "ready: all required checks green on $SHA — merge: gh pr merge $PR --$METHOD --match-head-commit $SHA"
      exit 3 ;;
  esac
done
