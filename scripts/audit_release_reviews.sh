#!/usr/bin/env bash
# Audit every PR contributing to a release immediately before its tag is pushed.
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  echo "usage: audit_release_reviews.sh RELEASE_SHA RELEASE_TAG" >&2
  exit 2
fi
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
python_command=${PYTHON:-python3}

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

readonly max_capture_bytes=$((128 * 1024 * 1024))
readonly max_small_capture_bytes=$((64 * 1024))
readonly max_records=50000
capture_temp_dir=$(mktemp -d)
capture_index=0
cleanup_capture_temp() {
  local status=$?
  rm -f -- "$capture_temp_dir"/capture-* "$capture_temp_dir/merged-tags" \
    "$capture_temp_dir/release-prs"
  rmdir -- "$capture_temp_dir" 2>/dev/null || true
  exit "$status"
}
trap cleanup_capture_temp EXIT

run_bounded_to_file() {
  local limit=$1
  local label=$2
  local target=$3
  shift 3
  local -a pipeline_status
  local size

  set +e
  "$@" | head -c "$((limit + 1))" > "$target"
  pipeline_status=("${PIPESTATUS[@]}")
  set -e
  size=$(wc -c < "$target")
  if (( size > limit )); then
    echo "$label exceeded the ${limit}-byte safety limit." >&2
    return 125
  fi
  if (( pipeline_status[0] != 0 )); then
    return "${pipeline_status[0]}"
  fi
  if (( pipeline_status[1] != 0 )); then
    echo "Bounded capture failed while reading $label." >&2
    return "${pipeline_status[1]}"
  fi
}

capture_bounded() {
  local destination=$1
  local label=$2
  local limit=$3
  shift 3
  capture_index=$((capture_index + 1))
  local target="$capture_temp_dir/capture-$capture_index"
  if ! run_bounded_to_file "$limit" "$label" "$target" "$@"; then
    rm -f -- "$target"
    return 1
  fi
  if ! printf -v "$destination" '%s' "$(<"$target")"; then
    rm -f -- "$target"
    return 1
  fi
  rm -f -- "$target"
}

git fetch --force --tags origin
merged_tags_file="$capture_temp_dir/merged-tags"
if ! run_bounded_to_file "$max_capture_bytes" "merged release tags" \
    "$merged_tags_file" git tag --merged "$release_sha"; then
  echo "Git failed while listing merged release tags." >&2
  exit 1
fi
if ! capture_bounded previous_tag "previous release tag" \
    "$max_small_capture_bytes" \
    "$python_command" -I - "$release_tag" "$merged_tags_file" <<'PY'
from pathlib import Path
import re
import sys

release_tag, merged_tags_path = sys.argv[1:]
release_version = tuple(map(int, release_tag.removeprefix("v").split(".")))
candidates = []
for tag in Path(merged_tags_path).read_text(encoding="utf-8").splitlines():
    match = re.fullmatch(r"v([0-9]+)\.([0-9]+)\.([0-9]+)", tag)
    if match is None:
        continue
    version = tuple(map(int, match.groups()))
    if version < release_version:
        candidates.append((version, tag))
print(max(candidates)[1] if candidates else "")
PY
then
  echo "Failed to determine the previous release tag." >&2
  exit 1
fi
if [[ -n "$previous_tag" ]]; then
  commit_range="$previous_tag..$release_sha"
else
  commit_range="$release_sha"
fi

release_prs_file="$capture_temp_dir/release-prs"
: > "$release_prs_file"
release_pr_record_count=0
missing_pr=0
if ! capture_bounded commit_output "release commit list" "$max_capture_bytes" \
    git rev-list "$commit_range"; then
  echo "Git failed while listing release commits." >&2
  exit 1
fi
if [[ -z "$commit_output" ]]; then
  echo "Release range $commit_range contains no commits to review." >&2
  exit 1
fi
release_commits=()
while IFS= read -r commit_sha; do
  release_commits+=("$commit_sha")
done <<< "$commit_output"
if (( ${#release_commits[@]} > max_records )); then
  echo "Release range contains too many commits to audit." >&2
  exit 1
fi
for commit_sha in "${release_commits[@]}"; do
  if ! capture_bounded associated_output \
    "associated pull requests for $commit_sha" "$max_capture_bytes" \
    gh api --paginate \
    -H "Accept: application/vnd.github+json" \
    "repos/${GITHUB_REPOSITORY}/commits/${commit_sha}/pulls" \
    --jq '.[].number'; then
    echo "GitHub API failed while resolving pull requests for $commit_sha." >&2
    exit 1
  fi
  associated_prs=()
  if [[ -n "$associated_output" ]]; then
    while IFS= read -r associated_pr; do
      associated_prs+=("$associated_pr")
    done <<< "$associated_output"
  fi
  if (( ${#associated_prs[@]} > max_records )); then
    echo "GitHub API returned too many associated pull requests for $commit_sha." >&2
    exit 1
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
    release_pr_record_count=$((release_pr_record_count + 1))
    if (( release_pr_record_count > max_records )); then
      echo "Release range contains too many associated pull requests." >&2
      exit 1
    fi
    printf '%s\n' "$pr_number" >> "$release_prs_file"
  done
done

if [[ "$missing_pr" -ne 0 || ! -s "$release_prs_file" ]]; then
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
# Keep this a finite allowlist.  A free-form suffix would let a contradictory
# finding share the authenticated clean-verdict line and still satisfy the
# release gate.
readonly codex_clean_verdict_regex="^Codex Review: Didn't find any major issues\\.( (Breezy|Hooray|Keep it up|Keep them coming)!| (Already looking forward to the next diff|More of your lovely PRs please)\\.)?$"
readonly codex_footer_open_regex='^<details>[[:space:]]+<summary>.*About Codex in GitHub</summary>$'

if ! capture_bounded repository_owner "repository ownership" \
  "$max_small_capture_bytes" gh api "repos/${GITHUB_REPOSITORY}" \
  --jq '[ (.owner.id | tostring), .owner.login, .owner.type ] | join("|")'; then
  echo "GitHub API failed while reading repository ownership." >&2
  exit 1
fi
if [[ "$repository_owner" == *$'\n'* ]]; then
  echo "GitHub API returned malformed repository ownership." >&2
  exit 1
fi
IFS='|' read -r repository_owner_id repository_owner_login \
  repository_owner_type repository_owner_extra <<< "$repository_owner"
repository_owner_login_lower=$(printf '%s' "$repository_owner_login" | \
  LC_ALL=C tr '[:upper:]' '[:lower:]')
owner_lower=$(printf '%s' "$owner" | LC_ALL=C tr '[:upper:]' '[:lower:]')
github_repository_lower=$(printf '%s' "$GITHUB_REPOSITORY" | \
  LC_ALL=C tr '[:upper:]' '[:lower:]')
if [[ ! "$repository_owner_id" =~ ^[1-9][0-9]*$ || \
      ! "$repository_owner_login" =~ ^[A-Za-z0-9-]{1,39}$ || \
      ! "$repository_owner_type" =~ ^(User|Organization)$ || \
      -n "$repository_owner_extra" || \
      "$repository_owner_login_lower" != "$owner_lower" ]]; then
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
  if ! capture_bounded output "review evidence for PR #$pr_number" \
    "$max_small_capture_bytes" \
    gh api "repos/${GITHUB_REPOSITORY}/commits/${candidate}" --jq '.sha'; then
    echo "GitHub API failed while resolving review evidence '$candidate' for PR #$pr_number." >&2
    exit 1
  fi
  if [[ ! "$output" =~ ^[0-9a-f]{40}$ ]]; then
    echo "GitHub API returned an invalid commit for review evidence '$candidate' on PR #$pr_number." >&2
    exit 1
  fi
  resolved_evidence_sha=$output
}

codex_marker_sha=
codex_body_is_clean() {
  local body=$1
  local marker_policy=$2
  local clean_count=0
  local marker_count=0
  local footer_count=0
  local footer_state=outside
  local line
  codex_marker_sha=
  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%$'\r'}

    if [[ "$footer_state" == inside ]]; then
      if [[ "$line" == "</details>" ]]; then
        footer_state=closed
      elif [[ "$line" == "<details>"* ]]; then
        return 1
      fi
      continue
    fi
    if [[ "$footer_state" == closed ]]; then
      [[ -z "$line" ]] || return 1
      continue
    fi

    if [[ -z "$line" ]]; then
      continue
    elif [[ "$line" =~ $codex_clean_verdict_regex ]]; then
      # The verdict must be the first meaningful line and may occur once.
      [[ "$clean_count" -eq 0 && "$marker_count" -eq 0 && \
         "$footer_count" -eq 0 ]] || return 1
      clean_count=$((clean_count + 1))
    elif [[ "$line" =~ $codex_marker_regex ]]; then
      [[ "$clean_count" -eq 1 && "$footer_count" -eq 0 ]] || return 1
      marker_count=$((marker_count + 1))
      codex_marker_sha=${BASH_REMATCH[1]}
    elif [[ "$line" =~ $codex_footer_open_regex ]]; then
      [[ "$clean_count" -eq 1 && "$footer_count" -eq 0 ]] || return 1
      footer_count=$((footer_count + 1))
      footer_state=inside
    else
      # A clean verdict mixed with findings, prose, or another status is not
      # machine-authenticated CLEAN evidence.
      return 1
    fi
  done <<< "$body"
  [[ "$clean_count" -eq 1 && "$footer_count" -le 1 && \
     "$footer_state" != inside ]] || return 1
  case "$marker_policy" in
    required) [[ "$marker_count" -eq 1 ]] ;;
    forbidden) [[ "$marker_count" -eq 0 ]] ;;
    *) return 1 ;;
  esac
}

codex_comment_has_unique_marker() {
  local body=$1
  local line
  codex_marker_count=0
  codex_marker_sha=
  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%$'\r'}
    if [[ "$line" =~ $codex_marker_regex ]]; then
      codex_marker_count=$((codex_marker_count + 1))
      codex_marker_sha=${BASH_REMATCH[1]}
    fi
  done <<< "$body"
  [[ "$codex_marker_count" -eq 1 ]]
}

failed=0
if ! capture_bounded sorted_output "sorted release pull requests" \
    "$max_capture_bytes" sort -nu "$release_prs_file"; then
  echo "Failed to sort associated pull requests." >&2
  exit 1
fi
rm -f -- "$release_prs_file"
if [[ -z "$sorted_output" ]]; then
  echo "Sorting associated pull requests produced no records." >&2
  exit 1
fi
sorted_prs=()
while IFS= read -r sorted_pr; do
  sorted_prs+=("$sorted_pr")
done <<< "$sorted_output"
for pr_number in "${sorted_prs[@]}"; do
  if ! capture_bounded pr_metadata "metadata for PR #$pr_number" \
    "$max_small_capture_bytes" \
    gh api "repos/${GITHUB_REPOSITORY}/pulls/${pr_number}" \
    --jq '[ (.user.id | tostring), .user.login, .user.type, .head.sha, (.merge_commit_sha // ""), ([ (.requested_reviewers // [])[] | select(.type == "User") ] | length | tostring), ([ (.requested_teams // [])[] ] | length | tostring), .state, (.merged_at // ""), (.base.repo.full_name // ""), (.base.ref // "") ] | join("|")'; then
    echo "GitHub API failed while reading metadata for PR #$pr_number." >&2
    exit 1
  fi
  if [[ "$pr_metadata" == *$'\n'* ]]; then
    echo "GitHub API returned malformed metadata for PR #$pr_number." >&2
    exit 1
  fi
  IFS='|' read -r author_id author_login author_type pr_head_sha \
    pr_merge_sha pending_reviewers pending_teams pr_state pr_merged_at \
    pr_base_repository pr_base_ref pr_metadata_extra <<< "$pr_metadata"
  pr_base_repository_lower=$(printf '%s' "$pr_base_repository" | \
    LC_ALL=C tr '[:upper:]' '[:lower:]')
  if [[ ! "$author_id" =~ ^[1-9][0-9]*$ || \
        ! "$author_login" =~ ^[A-Za-z0-9-]{1,39}$ || \
        ! "$author_type" =~ ^(User|Bot)$ || \
        ! "$pr_head_sha" =~ ^[0-9a-f]{40}$ || \
        ( -n "$pr_merge_sha" && ! "$pr_merge_sha" =~ ^[0-9a-f]{40}$ ) || \
        ! "$pending_reviewers" =~ ^[0-9]+$ || \
        ! "$pending_teams" =~ ^[0-9]+$ || \
        ! "$pr_state" =~ ^(open|closed)$ || \
        ( -n "$pr_merged_at" && \
          ! "$pr_merged_at" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$ ) || \
        ! "$pr_base_repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ || \
        -n "$pr_metadata_extra" ]]; then
    echo "GitHub API returned malformed metadata for PR #$pr_number." >&2
    exit 1
  fi
  if [[ "$pr_state" != "closed" || -z "$pr_merged_at" || \
        "$pr_base_repository_lower" != "$github_repository_lower" || \
        "$pr_base_ref" != "main" ]]; then
    echo "PR #$pr_number is not release-ready: associated PR must be merged into ${GITHUB_REPOSITORY}:main." >&2
    failed=1
    continue
  fi

  if ! capture_bounded decision "review decision for PR #$pr_number" \
    "$max_small_capture_bytes" gh api graphql \
    -f query="$decision_query" \
    -F owner="$owner" \
    -F repository="$repository" \
    -F number="$pr_number" \
    --jq '.data.repository.pullRequest.reviewDecision // ""'; then
    echo "GitHub API failed while reading review decision for PR #$pr_number." >&2
    exit 1
  fi
  if [[ ! "$decision" =~ ^(APPROVED|CHANGES_REQUESTED|REVIEW_REQUIRED)?$ ]]; then
    echo "GitHub API returned an invalid review decision for PR #$pr_number: $decision" >&2
    exit 1
  fi

  if ! capture_bounded unresolved_output "review threads for PR #$pr_number" \
      "$max_capture_bytes" gh api graphql --paginate \
      -f query="$threads_query" \
      -F owner="$owner" \
      -F repository="$repository" \
      -F number="$pr_number" \
      --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)] | length'; then
    echo "GitHub API failed while reading review threads for PR #$pr_number." >&2
    exit 1
  fi
  if [[ -z "$unresolved_output" ]]; then
    echo "GitHub API returned no review-thread page for PR #$pr_number." >&2
    exit 1
  fi
  unresolved=0
  unresolved_pages=0
  while IFS= read -r count; do
    unresolved_pages=$((unresolved_pages + 1))
    if (( unresolved_pages > max_records )); then
      echo "GitHub API returned too many review-thread pages for PR #$pr_number." >&2
      exit 1
    fi
    if [[ ! "$count" =~ ^[0-9]+$ ]]; then
      echo "Invalid unresolved-thread count for PR #$pr_number: $count" >&2
      exit 1
    fi
    unresolved=$((unresolved + count))
  done <<< "$unresolved_output"

  if ! capture_bounded reviews_output "reviews for PR #$pr_number" \
      "$max_capture_bytes" gh api --paginate \
      "repos/${GITHUB_REPOSITORY}/pulls/${pr_number}/reviews?per_page=100" \
      --jq '.[] | [ .state, ((.user.id // "") | tostring), (.user.login // ""), (.user.type // ""), (.commit_id // ""), ((.body // "") | @base64) ] | join("|")'; then
    echo "GitHub API failed while reading reviews for PR #$pr_number." >&2
    exit 1
  fi

  human_evidence=0
  codex_evidence=0
  codex_current_records=0
  codex_clean_records=0
  codex_conflict=0
  review_records=()
  if [[ -n "$reviews_output" ]]; then
    while IFS= read -r review_record; do
      review_records+=("$review_record")
    done <<< "$reviews_output"
  fi
  if (( ${#review_records[@]} > max_records )); then
    echo "GitHub API returned too many reviews for PR #$pr_number." >&2
    exit 1
  fi
  if (( ${#review_records[@]} > 0 )); then
  for review_record in "${review_records[@]}"; do
    [[ -z "$review_record" ]] && continue
    IFS='|' read -r review_state reviewer_id reviewer_login reviewer_type \
      evidence_sha encoded_review_body review_extra <<< "$review_record"
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
        if ! capture_bounded reviewer_permission \
          "repository permission for $reviewer_login" \
          "$max_small_capture_bytes" gh api \
          "repos/${GITHUB_REPOSITORY}/collaborators/${reviewer_login}/permission" \
          --jq '.permission'; then
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
      if ! capture_bounded review_body "decoded review body for PR #$pr_number" \
          "$max_capture_bytes" base64 --decode \
          <<< "$encoded_review_body" 2>/dev/null; then
        echo "GitHub API returned an invalid review body for PR #$pr_number." >&2
        exit 1
      fi
      resolved_evidence_sha=
      if resolve_evidence_sha "$evidence_sha" "$pr_number" && \
          { [[ "$resolved_evidence_sha" == "$pr_head_sha" ]] || \
            { [[ -n "$pr_merge_sha" ]] && \
              [[ "$resolved_evidence_sha" == "$pr_merge_sha" ]]; }; }; then
        codex_current_records=$((codex_current_records + 1))
        if codex_body_is_clean "$review_body" forbidden; then
          codex_clean_records=$((codex_clean_records + 1))
        else
          codex_conflict=1
        fi
      fi
    fi
  done
  fi

  if ! capture_bounded comments_output "issue comments for PR #$pr_number" \
      "$max_capture_bytes" gh api --paginate \
      "repos/${GITHUB_REPOSITORY}/issues/${pr_number}/comments?per_page=100" \
      --jq '.[] | [ ((.user.id // "") | tostring), (.user.login // ""), (.user.type // ""), ((.body // "") | @base64) ] | join("|")'; then
    echo "GitHub API failed while reading issue comments for PR #$pr_number." >&2
    exit 1
  fi
  comment_records=()
  if [[ -n "$comments_output" ]]; then
    while IFS= read -r comment_record; do
      comment_records+=("$comment_record")
    done <<< "$comments_output"
  fi
  if (( ${#comment_records[@]} > max_records )); then
    echo "GitHub API returned too many issue comments for PR #$pr_number." >&2
    exit 1
  fi
  if (( ${#comment_records[@]} > 0 )); then
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
    if ! capture_bounded comment_body "decoded issue comment for PR #$pr_number" \
        "$max_capture_bytes" base64 --decode \
        <<< "$encoded_body" 2>/dev/null; then
      echo "GitHub API returned an invalid issue-comment body for PR #$pr_number." >&2
      exit 1
    fi
    if [[ "$commenter_id" == "$trusted_codex_bot_id" && \
          "$commenter_type" == "Bot" ]]; then
      if codex_comment_has_unique_marker "$comment_body"; then
        marker_sha=$codex_marker_sha
        resolved_evidence_sha=
        if resolve_evidence_sha "$marker_sha" "$pr_number" && \
            { [[ "$resolved_evidence_sha" == "$pr_head_sha" ]] || \
              { [[ -n "$pr_merge_sha" ]] && \
                [[ "$resolved_evidence_sha" == "$pr_merge_sha" ]]; }; }; then
          codex_current_records=$((codex_current_records + 1))
          if codex_body_is_clean "$comment_body" required; then
            codex_clean_records=$((codex_clean_records + 1))
          else
            codex_conflict=1
          fi
        fi
      elif [[ "$codex_marker_count" -gt 0 ]]; then
        codex_conflict=1
      fi
    fi
  done
  fi

  if [[ "$codex_current_records" -eq 1 && \
        "$codex_clean_records" -eq 1 && "$codex_conflict" -eq 0 ]]; then
    codex_evidence=1
  elif [[ "$codex_current_records" -gt 0 ]]; then
    codex_conflict=1
  fi

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
  if [[ "$codex_conflict" -ne 0 ]]; then
    echo "PR #$pr_number is not release-ready: trusted Codex evidence for the current commit is conflicting or ambiguous." >&2
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
