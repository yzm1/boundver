#!/usr/bin/env bash
# Audit every PR contributing to a release immediately before its tag is pushed.
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  echo "usage: audit_release_reviews.sh RELEASE_SHA RELEASE_TAG" >&2
  exit 2
fi
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

release_sha=$1
release_tag=$2
if [[ ! "$release_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Invalid release SHA: $release_sha" >&2
  exit 2
fi
if [[ ! "$release_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Invalid release tag: $release_tag" >&2
  exit 2
fi

git fetch --force --tags origin
previous_tag=$(python3 - "$release_sha" "$release_tag" <<'PY'
import re
import subprocess
import sys

release_sha, release_tag = sys.argv[1:]
release_version = tuple(map(int, release_tag.removeprefix("v").split(".")))
candidates = []
for tag in subprocess.check_output(
    ["git", "tag", "--merged", release_sha], text=True
).splitlines():
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
    if match is None:
        continue
    version = tuple(map(int, match.groups()))
    if version < release_version:
        candidates.append((version, tag))
print(max(candidates)[1] if candidates else "")
PY
)
if [[ -n "$previous_tag" ]]; then
  commit_range="$previous_tag..$release_sha"
else
  commit_range="$release_sha"
fi

declare -A release_prs=()
missing_pr=0
commit_output=$(git rev-list "$commit_range")
if [[ -z "$commit_output" ]]; then
  echo "Release range $commit_range contains no commits to review." >&2
  exit 1
fi
mapfile -t release_commits <<< "$commit_output"
for commit_sha in "${release_commits[@]}"; do
  if ! associated_output=$(gh api --paginate \
    -H "Accept: application/vnd.github+json" \
    "repos/${GITHUB_REPOSITORY}/commits/${commit_sha}/pulls" \
    --jq '.[].number'); then
    echo "GitHub API failed while resolving pull requests for $commit_sha." >&2
    exit 1
  fi
  associated_prs=()
  if [[ -n "$associated_output" ]]; then
    mapfile -t associated_prs <<< "$associated_output"
  fi
  if [[ "${#associated_prs[@]}" -eq 0 ]]; then
    echo "Release commit $commit_sha has no associated pull request." >&2
    missing_pr=1
    continue
  fi
  for pr_number in "${associated_prs[@]}"; do
    if [[ ! "$pr_number" =~ ^[1-9][0-9]*$ ]]; then
      echo "GitHub API returned an invalid pull request number: $pr_number" >&2
      exit 1
    fi
    release_prs["$pr_number"]=1
  done
done

if [[ "$missing_pr" -ne 0 || "${#release_prs[@]}" -eq 0 ]]; then
  echo "Every release commit must arrive through a reviewed pull request." >&2
  exit 1
fi

owner=${GITHUB_REPOSITORY%%/*}
repository=${GITHUB_REPOSITORY#*/}
if [[ -z "$owner" || -z "$repository" || "$repository" == "$GITHUB_REPOSITORY" ]]; then
  echo "Invalid GITHUB_REPOSITORY: $GITHUB_REPOSITORY" >&2
  exit 2
fi

# The numeric account ID is stable even if the GitHub App bot is renamed.
readonly trusted_codex_bot_id=199175422
readonly codex_marker_regex='^\*\*Reviewed commit:\*\* `([0-9a-fA-F]{10,40})`$'

if ! repository_owner=$(gh api "repos/${GITHUB_REPOSITORY}" \
  --jq '[ (.owner.id | tostring), .owner.login, .owner.type ] | join("|")'); then
  echo "GitHub API failed while reading repository ownership." >&2
  exit 1
fi
if [[ "$repository_owner" == *$'\n'* ]]; then
  echo "GitHub API returned malformed repository ownership." >&2
  exit 1
fi
IFS='|' read -r repository_owner_id repository_owner_login \
  repository_owner_type repository_owner_extra <<< "$repository_owner"
if [[ ! "$repository_owner_id" =~ ^[1-9][0-9]*$ || \
      ! "$repository_owner_login" =~ ^[A-Za-z0-9-]{1,39}$ || \
      ! "$repository_owner_type" =~ ^(User|Organization)$ || \
      -n "$repository_owner_extra" || \
      "${repository_owner_login,,}" != "${owner,,}" ]]; then
  echo "GitHub API returned malformed repository ownership." >&2
  exit 1
fi

decision_query='query($owner:String!,$repository:String!,$number:Int!){repository(owner:$owner,name:$repository){pullRequest(number:$number){reviewDecision}}}'
threads_query='query($owner:String!,$repository:String!,$number:Int!,$endCursor:String){repository(owner:$owner,name:$repository){pullRequest(number:$number){reviewThreads(first:100,after:$endCursor){nodes{isResolved} pageInfo{hasNextPage endCursor}}}}}'

resolved_evidence_sha=
resolve_evidence_sha() {
  local candidate=$1
  local pr_number=$2
  local output
  if [[ ! "$candidate" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
    return 1
  fi
  if ! output=$(gh api "repos/${GITHUB_REPOSITORY}/commits/${candidate}" \
    --jq '.sha'); then
    echo "GitHub API failed while resolving review evidence '$candidate' for PR #$pr_number." >&2
    exit 1
  fi
  if [[ ! "$output" =~ ^[0-9a-f]{40}$ ]]; then
    echo "GitHub API returned an invalid commit for review evidence '$candidate' on PR #$pr_number." >&2
    exit 1
  fi
  resolved_evidence_sha=$output
}

failed=0
sorted_output=$(printf '%s\n' "${!release_prs[@]}" | sort -n)
mapfile -t sorted_prs <<< "$sorted_output"
for pr_number in "${sorted_prs[@]}"; do
  if ! pr_metadata=$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${pr_number}" \
    --jq '[ (.user.id | tostring), .user.login, .user.type, .head.sha, (.merge_commit_sha // ""), ([ (.requested_reviewers // [])[] | select(.type == "User") ] | length | tostring), ([ (.requested_teams // [])[] ] | length | tostring) ] | join("|")'); then
    echo "GitHub API failed while reading metadata for PR #$pr_number." >&2
    exit 1
  fi
  if [[ "$pr_metadata" == *$'\n'* ]]; then
    echo "GitHub API returned malformed metadata for PR #$pr_number." >&2
    exit 1
  fi
  IFS='|' read -r author_id author_login author_type pr_head_sha \
    pr_merge_sha pending_reviewers pending_teams pr_metadata_extra <<< "$pr_metadata"
  if [[ ! "$author_id" =~ ^[1-9][0-9]*$ || \
        ! "$author_login" =~ ^[A-Za-z0-9-]{1,39}$ || \
        ! "$author_type" =~ ^(User|Bot)$ || \
        ! "$pr_head_sha" =~ ^[0-9a-f]{40}$ || \
        ( -n "$pr_merge_sha" && ! "$pr_merge_sha" =~ ^[0-9a-f]{40}$ ) || \
        ! "$pending_reviewers" =~ ^[0-9]+$ || \
        ! "$pending_teams" =~ ^[0-9]+$ || \
        -n "$pr_metadata_extra" ]]; then
    echo "GitHub API returned malformed metadata for PR #$pr_number." >&2
    exit 1
  fi

  if ! decision=$(gh api graphql \
    -f query="$decision_query" \
    -F owner="$owner" \
    -F repository="$repository" \
    -F number="$pr_number" \
    --jq '.data.repository.pullRequest.reviewDecision // ""'); then
    echo "GitHub API failed while reading review decision for PR #$pr_number." >&2
    exit 1
  fi
  if [[ ! "$decision" =~ ^(APPROVED|CHANGES_REQUESTED|REVIEW_REQUIRED)?$ ]]; then
    echo "GitHub API returned an invalid review decision for PR #$pr_number: $decision" >&2
    exit 1
  fi

  if ! unresolved_output=$(gh api graphql --paginate \
      -f query="$threads_query" \
      -F owner="$owner" \
      -F repository="$repository" \
      -F number="$pr_number" \
      --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)] | length'); then
    echo "GitHub API failed while reading review threads for PR #$pr_number." >&2
    exit 1
  fi
  if [[ -z "$unresolved_output" ]]; then
    echo "GitHub API returned no review-thread page for PR #$pr_number." >&2
    exit 1
  fi
  unresolved=0
  while IFS= read -r count; do
    if [[ ! "$count" =~ ^[0-9]+$ ]]; then
      echo "Invalid unresolved-thread count for PR #$pr_number: $count" >&2
      exit 1
    fi
    unresolved=$((unresolved + count))
  done <<< "$unresolved_output"

  if ! reviews_output=$(gh api --paginate \
      "repos/${GITHUB_REPOSITORY}/pulls/${pr_number}/reviews?per_page=100" \
      --jq '.[] | [ .state, ((.user.id // "") | tostring), (.user.login // ""), (.user.type // ""), (.commit_id // "") ] | join("|")'); then
    echo "GitHub API failed while reading reviews for PR #$pr_number." >&2
    exit 1
  fi

  human_evidence=0
  codex_evidence=0
  review_records=()
  if [[ -n "$reviews_output" ]]; then
    mapfile -t review_records <<< "$reviews_output"
  fi
  for review_record in "${review_records[@]}"; do
    [[ -z "$review_record" ]] && continue
    IFS='|' read -r review_state reviewer_id reviewer_login reviewer_type \
      evidence_sha review_extra <<< "$review_record"
    if [[ ! "$review_state" =~ ^(APPROVED|CHANGES_REQUESTED|COMMENTED|DISMISSED|PENDING)$ || \
          ( -n "$reviewer_id" && ! "$reviewer_id" =~ ^[1-9][0-9]*$ ) || \
          ( -n "$reviewer_login" && ! "$reviewer_login" =~ ^[A-Za-z0-9-]+(\[bot\])?$ ) || \
          ( -n "$reviewer_type" && ! "$reviewer_type" =~ ^(User|Bot)$ ) || \
          -n "$review_extra" ]]; then
      echo "GitHub API returned malformed review data for PR #$pr_number." >&2
      exit 1
    fi

    if [[ "$review_state" == "APPROVED" && "$reviewer_type" == "User" && \
          -n "$reviewer_id" && "$reviewer_id" != "$author_id" && \
          -n "$reviewer_login" ]]; then
      resolved_evidence_sha=
      if resolve_evidence_sha "$evidence_sha" "$pr_number" && \
          { [[ "$resolved_evidence_sha" == "$pr_head_sha" ]] || \
            { [[ -n "$pr_merge_sha" ]] && \
              [[ "$resolved_evidence_sha" == "$pr_merge_sha" ]]; }; }; then
        if ! reviewer_permission=$(gh api \
          "repos/${GITHUB_REPOSITORY}/collaborators/${reviewer_login}/permission" \
          --jq '.permission'); then
          echo "GitHub API failed while reading repository permission for $reviewer_login on PR #$pr_number." >&2
          exit 1
        fi
        if [[ ! "$reviewer_permission" =~ ^(admin|maintain|write|triage|read|none)$ ]]; then
          echo "GitHub API returned an invalid repository permission for $reviewer_login on PR #$pr_number." >&2
          exit 1
        fi
        if [[ "$reviewer_permission" =~ ^(admin|maintain|write)$ ]]; then
          human_evidence=1
        fi
      fi
    fi

    if [[ "$review_state" == "COMMENTED" && \
          "$reviewer_id" == "$trusted_codex_bot_id" && \
          "$reviewer_type" == "Bot" ]]; then
      resolved_evidence_sha=
      if resolve_evidence_sha "$evidence_sha" "$pr_number" && \
          { [[ "$resolved_evidence_sha" == "$pr_head_sha" ]] || \
            { [[ -n "$pr_merge_sha" ]] && \
              [[ "$resolved_evidence_sha" == "$pr_merge_sha" ]]; }; }; then
        codex_evidence=1
      fi
    fi
  done

  if ! comments_output=$(gh api --paginate \
      "repos/${GITHUB_REPOSITORY}/issues/${pr_number}/comments?per_page=100" \
      --jq '.[] | [ ((.user.id // "") | tostring), (.user.login // ""), (.user.type // ""), ((.body // "") | @base64) ] | join("|")'); then
    echo "GitHub API failed while reading issue comments for PR #$pr_number." >&2
    exit 1
  fi
  comment_records=()
  if [[ -n "$comments_output" ]]; then
    mapfile -t comment_records <<< "$comments_output"
  fi
  for comment_record in "${comment_records[@]}"; do
    [[ -z "$comment_record" ]] && continue
    IFS='|' read -r commenter_id commenter_login commenter_type encoded_body \
      comment_extra <<< "$comment_record"
    if [[ ( -n "$commenter_id" && ! "$commenter_id" =~ ^[1-9][0-9]*$ ) || \
          ( -n "$commenter_login" && ! "$commenter_login" =~ ^[A-Za-z0-9-]+(\[bot\])?$ ) || \
          ( -n "$commenter_type" && ! "$commenter_type" =~ ^(User|Bot)$ ) || \
          -z "$encoded_body" || -n "$comment_extra" ]]; then
      echo "GitHub API returned malformed issue-comment data for PR #$pr_number." >&2
      exit 1
    fi
    if ! comment_body=$(printf '%s' "$encoded_body" | base64 --decode 2>/dev/null); then
      echo "GitHub API returned an invalid issue-comment body for PR #$pr_number." >&2
      exit 1
    fi
    if [[ "$commenter_id" == "$trusted_codex_bot_id" && \
          "$commenter_type" == "Bot" ]]; then
      marker_count=0
      marker_sha=
      while IFS= read -r comment_line; do
        if [[ "$comment_line" =~ $codex_marker_regex ]]; then
          marker_count=$((marker_count + 1))
          marker_sha=${BASH_REMATCH[1]}
        fi
      done <<< "$comment_body"
      if [[ "$marker_count" -eq 1 ]]; then
        resolved_evidence_sha=
        if resolve_evidence_sha "$marker_sha" "$pr_number" && \
            { [[ "$resolved_evidence_sha" == "$pr_head_sha" ]] || \
              { [[ -n "$pr_merge_sha" ]] && \
                [[ "$resolved_evidence_sha" == "$pr_merge_sha" ]]; }; }; then
          codex_evidence=1
        fi
      fi
    fi
  done

  if [[ "$decision" == "CHANGES_REQUESTED" ]]; then
    echo "PR #$pr_number is not release-ready: reviewDecision=CHANGES_REQUESTED." >&2
    failed=1
  fi
  if [[ "$unresolved" -ne 0 ]]; then
    echo "PR #$pr_number is not release-ready: unresolvedThreads=$unresolved." >&2
    failed=1
  fi
  if [[ "$pending_reviewers" -ne 0 || "$pending_teams" -ne 0 ]]; then
    echo "PR #$pr_number is not release-ready: pending human review requests (users=$pending_reviewers, teams=$pending_teams)." >&2
    failed=1
  fi

  owner_authored=0
  if [[ "$repository_owner_type" == "User" && \
        "$author_id" == "$repository_owner_id" ]]; then
    owner_authored=1
  fi
  if [[ "$human_evidence" -eq 0 && \
        ! ( "$owner_authored" -eq 1 && "$codex_evidence" -eq 1 ) ]]; then
    if [[ "$owner_authored" -eq 1 ]]; then
      expected_evidence="a non-author human with push access or the trusted Codex bot"
    else
      expected_evidence="a non-author human with push access"
    fi
    echo "PR #$pr_number is not release-ready: no current exact-commit review evidence from $expected_evidence." >&2
    failed=1
  fi
done
if [[ "$failed" -ne 0 ]]; then
  echo "Resolve blocking and pending reviews, resolve every thread, and obtain current exact-commit review evidence before tagging." >&2
  exit 1
fi
