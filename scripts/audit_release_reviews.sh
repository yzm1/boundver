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
review_query='query($owner:String!,$repository:String!,$number:Int!,$endCursor:String){repository(owner:$owner,name:$repository){pullRequest(number:$number){reviewDecision reviewThreads(first:100,after:$endCursor){nodes{isResolved} pageInfo{hasNextPage endCursor}}}}}'
failed=0
sorted_output=$(printf '%s\n' "${!release_prs[@]}" | sort -n)
mapfile -t sorted_prs <<< "$sorted_output"
for pr_number in "${sorted_prs[@]}"; do
  if ! decision=$(gh api graphql \
    -f query="$review_query" \
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
      -f query="$review_query" \
      -F owner="$owner" \
      -F repository="$repository" \
      -F number="$pr_number" \
      --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)] | length'); then
    echo "GitHub API failed while reading review threads for PR #$pr_number." >&2
    exit 1
  fi
  unresolved=0
  if [[ -n "$unresolved_output" ]]; then
    while IFS= read -r count; do
      if [[ ! "$count" =~ ^[0-9]+$ ]]; then
        echo "Invalid unresolved-thread count for PR #$pr_number: $count" >&2
        exit 1
      fi
      unresolved=$((unresolved + count))
    done <<< "$unresolved_output"
  fi
  if [[ "$decision" != "APPROVED" || "$unresolved" -ne 0 ]]; then
    echo "PR #$pr_number is not release-ready: reviewDecision=${decision:-none}, unresolvedThreads=$unresolved" >&2
    failed=1
  fi
done
if [[ "$failed" -ne 0 ]]; then
  echo "Require approval and resolve or explicitly dismiss every review thread before tagging." >&2
  exit 1
fi
