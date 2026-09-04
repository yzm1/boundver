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

# Review history must be the exact local object graph.  Never let replace refs,
# lazy promisor fetches, interactive helpers, or an alternate gh host redefine
# the commits and GitHub repository being authorized for release.
while IFS='=' read -r variable _; do
  case "$variable" in
    GIT_CONFIG_KEY_*|GIT_CONFIG_VALUE_*|GIT_TRACE*) unset "$variable" ;;
  esac
done < <(env)
unset GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_ALLOW_PROTOCOL GIT_ASKPASS \
  GIT_CEILING_DIRECTORIES GIT_COMMON_DIR GIT_CONFIG_PARAMETERS \
  GIT_CURL_VERBOSE GIT_DIFF_OPTS GIT_DIR GIT_DISCOVERY_ACROSS_FILESYSTEM \
  GIT_EXEC_PATH GIT_EXTERNAL_DIFF GIT_FLUSH GIT_GLOB_PATHSPECS \
  GIT_GRAFT_FILE GIT_ICASE_PATHSPECS GIT_INDEX_FILE GIT_LITERAL_PATHSPECS \
  GIT_NOGLOB_PATHSPECS GIT_NAMESPACE GIT_OBJECT_DIRECTORY \
  GIT_PROTOCOL_FROM_USER GIT_QUARANTINE_PATH GIT_REDIRECT_STDERR \
  GIT_REPLACE_REF_BASE GIT_SHALLOW_FILE GIT_SSH GIT_SSH_COMMAND \
  GIT_SSH_VARIANT GIT_SSL_NO_VERIFY GIT_TEMPLATE_DIR GIT_WORK_TREE \
  SSH_ASKPASS || true
export GIT_ATTR_NOSYSTEM=1
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_KEY_0=core.hooksPath
export GIT_CONFIG_VALUE_0=/dev/null
export GIT_CONFIG_KEY_1=core.fsmonitor
export GIT_CONFIG_VALUE_1=false
export GIT_CONFIG_NOSYSTEM=1
export GIT_NO_LAZY_FETCH=1
export GIT_NO_REPLACE_OBJECTS=1
export GIT_OPTIONAL_LOCKS=0
export GIT_PAGER=cat
export GIT_TERMINAL_PROMPT=0
export GCM_INTERACTIVE=never

# An earlier step must not redirect an authenticated GitHub request through a
# repository-controlled socket, proxy, trust root, config file, or debug sink.
unset ALL_PROXY all_proxy BROWSER CLICOLOR_FORCE CURL_CA_BUNDLE CURL_HOME \
  DEBUG EDITOR GH_ACCESSIBLE_COLORS GH_ACCESSIBLE_PROMPTER GH_BROWSER \
  GH_DEBUG GH_EDITOR GH_ENTERPRISE_TOKEN GH_FORCE_TTY GH_HTTP_UNIX_SOCKET \
  GH_MDWIDTH GH_REPO GH_SPINNER_DISABLED GHES_TOKEN GITHUB_API_URL \
  GITHUB_ENTERPRISE_TOKEN GITHUB_GRAPHQL_URL GITHUB_SERVER_URL GIT_EDITOR \
  GLAMOUR_STYLE GODEBUG HTTPS_PROXY https_proxy HTTP_PROXY http_proxy \
  NODE_EXTRA_CA_CERTS NO_PROXY no_proxy REQUESTS_CA_BUNDLE SSL_CERT_DIR \
  SSL_CERT_FILE SSLKEYLOGFILE VISUAL || true
export GH_HOST=github.com
export GH_NO_EXTENSION_UPDATE_NOTIFIER=1
export GH_NO_UPDATE_NOTIFIER=1
export GH_PAGER=cat
export GH_PROMPT_DISABLED=1
export NO_COLOR=1
export NO_PROXY=github.com,api.github.com
export PAGER=cat
export TERM=dumb

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
    "$capture_temp_dir/merged-commits" \
    "$capture_temp_dir/publication-runs" \
    "$capture_temp_dir/publication-workflow" \
    "$capture_temp_dir/published-releases" "$capture_temp_dir/release-prs"
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
# GitHub's immutable flag prevents later edits; it does not prove that the
# release passed this repository's publication workflow.  Keep release, tag,
# workflow, run, and candidate-history evidence separate until the bounded
# selector below binds all five identities.
published_releases_file="$capture_temp_dir/published-releases"
if ! run_bounded_to_file "$max_capture_bytes" "published GitHub releases" \
    "$published_releases_file" gh api --paginate \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "repos/${GITHUB_REPOSITORY}/releases?per_page=100" \
    --jq '.[] | [(.id | tostring), (.tag_name // ""), (.draft | tostring), (.prerelease | tostring), (.immutable | tostring), (.published_at // "")] | @tsv'; then
  echo "GitHub API failed while listing published releases." >&2
  exit 1
fi
merged_tags_file="$capture_temp_dir/merged-tags"
if ! run_bounded_to_file "$max_capture_bytes" "merged release tags" \
    "$merged_tags_file" git for-each-ref --merged="$release_sha" \
    '--format=%(refname:short)%09%(objecttype)%09%(objectname)%09%(*objecttype)%09%(*objectname)' \
    refs/tags; then
  echo "Git failed while listing merged release tags." >&2
  exit 1
fi
merged_commits_file="$capture_temp_dir/merged-commits"
if ! run_bounded_to_file "$max_capture_bytes" "merged release commits" \
    "$merged_commits_file" git rev-list "$release_sha"; then
  echo "Git failed while listing merged release commits." >&2
  exit 1
fi
publication_workflow_file="$capture_temp_dir/publication-workflow"
if ! run_bounded_to_file "$max_small_capture_bytes" "publication workflow" \
    "$publication_workflow_file" gh api \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "repos/${GITHUB_REPOSITORY}/actions/workflows/publish.yml" \
    --jq '[.id, (.name // ""), (.path // ""), (.state // "")] | @tsv'; then
  echo "GitHub API failed while reading the publication workflow." >&2
  exit 1
fi
publication_runs_file="$capture_temp_dir/publication-runs"
if ! run_bounded_to_file "$max_capture_bytes" "publication workflow runs" \
    "$publication_runs_file" gh api --paginate \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "repos/${GITHUB_REPOSITORY}/actions/workflows/publish.yml/runs?event=workflow_dispatch&status=completed&per_page=100" \
    --jq '.workflow_runs[] | [(.id | tostring), (.name // ""), (.path // ""), (.display_title // ""), (.event // ""), (.status // ""), (.conclusion // ""), (.head_sha // ""), (.head_branch // ""), (.run_attempt | tostring), (.workflow_id | tostring), (.repository.full_name // ""), (.head_repository.full_name // ""), (.created_at // ""), (.updated_at // ""), (.html_url // "")] | @tsv'; then
  echo "GitHub API failed while listing publication workflow runs." >&2
  exit 1
fi
if ! capture_bounded previous_tag "previous release tag" \
    "$max_small_capture_bytes" \
    "$python_command" -I - "$release_tag" "$published_releases_file" \
    "$merged_tags_file" "$merged_commits_file" "$publication_workflow_file" \
    "$publication_runs_file" "$max_records" "$GITHUB_REPOSITORY" <<'PY'
import datetime
from pathlib import Path
import re
import sys

(
    release_tag,
    releases_path,
    merged_tags_path,
    merged_commits_path,
    workflow_path,
    runs_path,
    max_records_text,
    repository,
) = sys.argv[1:]
max_records = int(max_records_text)
version_pattern = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
version_match = version_pattern.fullmatch(release_tag)
if version_match is None:
    raise SystemExit("release tag is not canonical SemVer")
release_version = tuple(map(int, version_match.groups()))
timestamp_pattern = re.compile(
    r"(?P<base>[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,9}))?Z"
)
publication_title_pattern = re.compile(
    r"publish:(?P<tag>v(?:0|[1-9][0-9]{0,17})\."
    r"(?:0|[1-9][0-9]{0,17})\.(?:0|[1-9][0-9]{0,17}))"
    r"@(?P<sha>[0-9a-f]{40}):alias="
    r"(?P<alias>none|v(?:0|[1-9][0-9]{0,17})\."
    r"(?:0|[1-9][0-9]{0,17}))"
    r":resume=(?P<resume>[1-9][0-9]{0,19})?"
)
sha_pattern = re.compile(r"[0-9a-f]{40}")
max_github_id = (1 << 64) - 1


def timestamp_key(value):
    match = timestamp_pattern.fullmatch(value)
    if match is None:
        raise SystemExit("GitHub returned a malformed timestamp")
    try:
        calendar = datetime.datetime.strptime(
            match.group("base"), "%Y-%m-%dT%H:%M:%S"
        )
    except ValueError as exc:
        raise SystemExit("GitHub returned a malformed timestamp") from exc
    return (
        calendar.year,
        calendar.month,
        calendar.day,
        calendar.hour,
        calendar.minute,
        calendar.second,
        (match.group("fraction") or "").ljust(9, "0"),
    )


def positive_id(value):
    return (
        value.isascii()
        and value.isdecimal()
        and len(value) <= 20
        and 1 <= int(value) <= max_github_id
    )


tag_rows = Path(merged_tags_path).read_text(encoding="utf-8").splitlines()
if len(tag_rows) > max_records:
    raise SystemExit("Git returned too many merged tag records")
merged_tags = {}
for row in tag_rows:
    fields = row.split("\t")
    if len(fields) != 5:
        raise SystemExit("Git returned malformed merged tag metadata")
    tag, object_type, object_sha, peeled_type, peeled_sha = fields
    if version_pattern.fullmatch(tag) is None:
        continue
    if object_type == "commit" and not peeled_type and not peeled_sha:
        tag_sha = object_sha
    elif object_type == "tag" and peeled_type == "commit":
        tag_sha = peeled_sha
    else:
        raise SystemExit("semantic release tag does not resolve to a commit")
    if sha_pattern.fullmatch(tag_sha) is None or tag in merged_tags:
        raise SystemExit("Git returned malformed or duplicate merged tag metadata")
    merged_tags[tag] = tag_sha

merged_commits = Path(merged_commits_path).read_text(encoding="utf-8").splitlines()
if (
    not merged_commits
    or len(merged_commits) > max_records
    or len(merged_commits) != len(set(merged_commits))
    or any(sha_pattern.fullmatch(commit) is None for commit in merged_commits)
):
    raise SystemExit("Git returned malformed or excessive merged commit metadata")
merged_commit_names = set(merged_commits)

workflow_rows = Path(workflow_path).read_text(encoding="utf-8").splitlines()
if len(workflow_rows) != 1:
    raise SystemExit("GitHub returned malformed publication workflow metadata")
workflow_fields = workflow_rows[0].split("\t")
if len(workflow_fields) != 4:
    raise SystemExit("GitHub returned malformed publication workflow metadata")
workflow_id, workflow_name, workflow_file, workflow_state = workflow_fields
if (
    not positive_id(workflow_id)
    or workflow_name != "Promote verified release"
    or workflow_file != ".github/workflows/publish.yml"
    or workflow_state != "active"
):
    raise SystemExit("GitHub returned an untrusted publication workflow")

run_rows = Path(runs_path).read_text(encoding="utf-8").splitlines()
if len(run_rows) > max_records:
    raise SystemExit("GitHub returned too many publication runs")
seen_run_ids = set()
publication_runs = {}
for row in run_rows:
    fields = row.split("\t")
    if len(fields) != 16:
        raise SystemExit("GitHub returned malformed publication-run metadata")
    (
        run_id,
        name,
        run_path,
        title,
        event,
        status,
        conclusion,
        head_sha,
        head_branch,
        run_attempt,
        run_workflow_id,
        run_repository,
        head_repository,
        created_at,
        updated_at,
        html_url,
    ) = fields
    if (
        not positive_id(run_id)
        or run_id in seen_run_ids
        or run_path != ".github/workflows/publish.yml"
        or event != "workflow_dispatch"
        or status != "completed"
        or not conclusion
    ):
        raise SystemExit("GitHub returned malformed publication-run metadata")
    seen_run_ids.add(run_id)
    if conclusion != "success":
        continue
    match = publication_title_pattern.fullmatch(title)
    if match is None:
        continue
    tag = match.group("tag")
    tag_sha = match.group("sha")
    alias = match.group("alias")
    resume = match.group("resume")
    if (
        alias not in {"none", tag.rsplit(".", 1)[0]}
        or (resume is not None and not positive_id(resume))
        or name != title
        or run_workflow_id != workflow_id
        or not positive_id(run_attempt)
        or sha_pattern.fullmatch(head_sha) is None
        or head_sha not in merged_commit_names
        or run_repository.casefold() != repository.casefold()
        or head_repository.casefold() != repository.casefold()
        or html_url != f"https://github.com/{repository}/actions/runs/{run_id}"
        or timestamp_key(created_at) > timestamp_key(updated_at)
    ):
        raise SystemExit("GitHub returned untrusted publication-run metadata")
    if resume is None:
        if head_branch != tag or head_sha != tag_sha:
            raise SystemExit("GitHub returned untrusted publication-run metadata")
    elif head_branch != "main":
        raise SystemExit("GitHub returned untrusted publication-run metadata")
    provenance = (
        timestamp_key(updated_at),
        int(run_id),
        int(run_attempt),
        updated_at,
    )
    publication_runs.setdefault((tag, tag_sha), []).append(provenance)

trusted_publications = {
    key: max(provenance)
    for key, provenance in publication_runs.items()
}
rows = Path(releases_path).read_text(encoding="utf-8").splitlines()
if len(rows) > max_records:
    raise SystemExit("GitHub returned too many release records")
candidates = []
seen_ids = set()
seen_tags = set()
for row in rows:
    fields = row.split("\t")
    if len(fields) != 6:
        raise SystemExit("GitHub returned malformed release metadata")
    release_id, tag, draft, prerelease, immutable, published_at = fields
    match = version_pattern.fullmatch(tag)
    if match is None:
        continue
    if (
        not positive_id(release_id)
        or draft not in {"true", "false"}
        or prerelease not in {"true", "false"}
        or immutable not in {"true", "false"}
    ):
        raise SystemExit("GitHub returned malformed semantic release metadata")
    if release_id in seen_ids or tag in seen_tags:
        raise SystemExit("GitHub returned duplicate semantic release metadata")
    seen_ids.add(release_id)
    seen_tags.add(tag)
    if draft == "true" or prerelease == "true" or immutable != "true":
        continue
    published_key = timestamp_key(published_at)
    version = tuple(map(int, match.groups()))
    tag_sha = merged_tags.get(tag)
    publication = trusted_publications.get((tag, tag_sha))
    if (
        version < release_version
        and tag_sha is not None
        and publication is not None
        and published_key <= publication[0]
    ):
        candidates.append((version, tag, release_id, published_at))
print(max(candidates, key=lambda item: (item[0], item[1]))[1] if candidates else "")
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
# Keep this a positive allowlist of statuses actually emitted by the trusted
# app.  A free-form suffix would let contradictory review state share the
# authenticated clean-verdict line and still satisfy the release gate.
readonly codex_clean_bang_status_regex="(Another round soon, please|Breezy|Can't wait for the next one|Delightful|Hooray|Keep it up|Keep them coming|Nice work|Swish)!"
readonly codex_clean_period_status_regex="(Already looking forward to the next diff|Bravo|Chef's kiss|More of your lovely PRs please|You're on a roll)\."
readonly codex_clean_question_status_regex='(What shall we delve into next)\?'
readonly codex_clean_emoji_status_regex=':(rocket|tada):'
readonly codex_clean_verdict_regex="^Codex Review: Didn't find any major issues\\.( (${codex_clean_bang_status_regex}|${codex_clean_period_status_regex}|${codex_clean_question_status_regex}|${codex_clean_emoji_status_regex}))?$"
readonly codex_footer_open_regex='^<details>[[:space:]]+<summary>.*About Codex in GitHub</summary>$'
readonly codex_security_clean_verdict='Security review completed. No security issues were found in this pull request.'
readonly codex_security_report_regex='^\[View security finding report\]\(https://chatgpt\.com/codex/cloud/tasks/task_[A-Za-z0-9_-]{1,128}\)$'
readonly codex_security_notice='_Only the user who started this review can view the report in Codex._'
readonly codex_security_footer_open_regex='^<details>[[:space:]]+<summary>.*About Codex security reviews in GitHub</summary>$'
readonly github_timestamp_regex='^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.([0-9]{1,9}))?Z$'

github_timestamp_key=
github_timestamp_is_valid() {
  local value=$1
  local year month day hour minute second fraction max_day
  github_timestamp_key=
  [[ "$value" =~ $github_timestamp_regex ]] || return 1
  year=$((10#${BASH_REMATCH[1]}))
  month=$((10#${BASH_REMATCH[2]}))
  day=$((10#${BASH_REMATCH[3]}))
  hour=$((10#${BASH_REMATCH[4]}))
  minute=$((10#${BASH_REMATCH[5]}))
  second=$((10#${BASH_REMATCH[6]}))
  fraction=${BASH_REMATCH[8]:-}
  if (( year < 1 || month < 1 || month > 12 || day < 1 || \
        hour > 23 || minute > 59 || second > 59 )); then
    return 1
  fi
  case "$month" in
    1|3|5|7|8|10|12) max_day=31 ;;
    4|6|9|11) max_day=30 ;;
    2)
      max_day=28
      if (( year % 400 == 0 || (year % 4 == 0 && year % 100 != 0) )); then
        max_day=29
      fi
      ;;
    *) return 1 ;;
  esac
  (( day <= max_day )) || return 1
  while (( ${#fraction} < 9 )); do
    fraction+=0
  done
  github_timestamp_key=$(printf '%04d%02d%02d%02d%02d%02d%s' \
    "$year" "$month" "$day" "$hour" "$minute" "$second" "$fraction")
}

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
      [[ "$line" =~ ^[[:space:]]*$ ]] || return 1
      continue
    fi

    if [[ "$line" =~ ^[[:space:]]*$ ]]; then
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

codex_body_is_clean_security_review() {
  local body=$1
  local state=verdict
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
      [[ "$line" =~ ^[[:space:]]*$ ]] || return 1
      continue
    fi
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    case "$state" in
      verdict)
        [[ "$line" == "$codex_security_clean_verdict" ]] || return 1
        state=marker
        ;;
      marker)
        [[ "$line" =~ $codex_marker_regex ]] || return 1
        codex_marker_sha=${BASH_REMATCH[1]}
        state=report
        ;;
      report)
        [[ "$line" =~ $codex_security_report_regex ]] || return 1
        state=notice
        ;;
      notice)
        [[ "$line" == "$codex_security_notice" ]] || return 1
        state=footer
        ;;
      footer)
        [[ "$line" =~ $codex_security_footer_open_regex ]] || return 1
        footer_state=inside
        state=done
        ;;
      *) return 1 ;;
    esac
  done <<< "$body"
  [[ "$state" == done && "$footer_state" == closed ]]
}

codex_body_is_suggestions_review() {
  local body=$1
  local state=heading
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
      [[ "$line" =~ ^[[:space:]]*$ ]] || return 1
      continue
    fi
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    case "$state" in
      heading)
        [[ "$line" == "### "*"Codex Review" ]] || return 1
        state=summary
        ;;
      summary)
        [[ "$line" == "Here are some automated review suggestions for this pull request." ]] || return 1
        state=marker
        ;;
      marker)
        [[ "$line" =~ $codex_marker_regex ]] || return 1
        codex_marker_sha=${BASH_REMATCH[1]}
        state=footer
        ;;
      footer)
        [[ "$line" =~ $codex_footer_open_regex ]] || return 1
        footer_state=inside
        ;;
      *) return 1 ;;
    esac
  done <<< "$body"
  [[ "$state" == footer && "$footer_state" == closed ]]
}

record_codex_evidence() {
  local timestamp=$1
  local kind=$2
  local timestamp_key
  if ! github_timestamp_is_valid "$timestamp"; then
    return 1
  fi
  timestamp_key=$github_timestamp_key
  if [[ -z "$codex_latest_timestamp" || \
        "$timestamp_key" > "$codex_latest_timestamp" ]]; then
    codex_latest_timestamp=$timestamp_key
    codex_latest_kind=$kind
    codex_latest_count=1
  elif [[ "$timestamp_key" == "$codex_latest_timestamp" ]]; then
    codex_latest_count=$((codex_latest_count + 1))
  fi
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
        ! "$author_login" =~ ^[A-Za-z0-9-]{1,39}(\[bot\])?$ || \
        ! "$author_type" =~ ^(User|Bot)$ || \
        ! "$pr_head_sha" =~ ^[0-9a-f]{40}$ || \
        ( -n "$pr_merge_sha" && ! "$pr_merge_sha" =~ ^[0-9a-f]{40}$ ) || \
        ! "$pending_reviewers" =~ ^[0-9]+$ || \
        ! "$pending_teams" =~ ^[0-9]+$ || \
        ! "$pr_state" =~ ^(open|closed)$ || \
        ! "$pr_base_repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ || \
        -n "$pr_metadata_extra" ]]; then
    echo "GitHub API returned malformed metadata for PR #$pr_number." >&2
    exit 1
  fi
  if [[ -n "$pr_merged_at" ]] && \
      ! github_timestamp_is_valid "$pr_merged_at"; then
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
      --jq '.[] | [ .state, ((.id // "") | tostring), (.submitted_at // ""), ((.user.id // "") | tostring), (.user.login // ""), (.user.type // ""), (.commit_id // ""), ((.body // "") | @base64) ] | join("|")'; then
    echo "GitHub API failed while reading reviews for PR #$pr_number." >&2
    exit 1
  fi

  human_evidence=0
  codex_evidence=0
  codex_latest_timestamp=
  codex_latest_kind=
  codex_latest_count=0
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
    IFS='|' read -r review_state review_id review_submitted_at reviewer_id \
      reviewer_login reviewer_type evidence_sha encoded_review_body \
      review_extra <<< "$review_record"
    if [[ ! "$review_state" =~ ^(APPROVED|CHANGES_REQUESTED|COMMENTED|DISMISSED|PENDING)$ || \
          ! "$review_id" =~ ^[1-9][0-9]*$ || \
          ( -n "$reviewer_id" && ! "$reviewer_id" =~ ^[1-9][0-9]*$ ) || \
          ( -n "$reviewer_login" && ! "$reviewer_login" =~ ^[A-Za-z0-9-]+(\[bot\])?$ ) || \
          ( -n "$reviewer_type" && ! "$reviewer_type" =~ ^(User|Bot)$ ) || \
          -n "$review_extra" ]]; then
      echo "GitHub API returned malformed review data for PR #$pr_number." >&2
      exit 1
    fi
    if [[ -n "$review_submitted_at" ]] && \
        ! github_timestamp_is_valid "$review_submitted_at"; then
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
        if [[ -z "$review_submitted_at" ]]; then
          echo "GitHub API returned a current Codex review without a submission time for PR #$pr_number." >&2
          exit 1
        fi
        if codex_body_is_clean "$review_body" forbidden; then
          record_codex_evidence "$review_submitted_at" clean
        elif codex_body_is_clean_security_review "$review_body"; then
          review_evidence_sha=$resolved_evidence_sha
          marker_sha=$codex_marker_sha
          resolved_evidence_sha=
          if ! resolve_evidence_sha "$marker_sha" "$pr_number" || \
              [[ "$resolved_evidence_sha" != "$review_evidence_sha" ]]; then
            record_codex_evidence "$review_submitted_at" adverse
          fi
        elif codex_body_is_suggestions_review "$review_body"; then
          review_evidence_sha=$resolved_evidence_sha
          marker_sha=$codex_marker_sha
          resolved_evidence_sha=
          if resolve_evidence_sha "$marker_sha" "$pr_number" && \
              [[ "$resolved_evidence_sha" == "$review_evidence_sha" ]]; then
            record_codex_evidence "$review_submitted_at" suggestions
          else
            record_codex_evidence "$review_submitted_at" adverse
          fi
        else
          record_codex_evidence "$review_submitted_at" adverse
        fi
      fi
    fi
  done
  fi

  if ! capture_bounded comments_output "issue comments for PR #$pr_number" \
      "$max_capture_bytes" gh api --paginate \
      "repos/${GITHUB_REPOSITORY}/issues/${pr_number}/comments?per_page=100" \
      --jq '.[] | [ ((.id // "") | tostring), (.created_at // ""), ((.user.id // "") | tostring), (.user.login // ""), (.user.type // ""), ((.body // "") | @base64) ] | join("|")'; then
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
    IFS='|' read -r comment_id comment_created_at commenter_id commenter_login \
      commenter_type encoded_body comment_extra <<< "$comment_record"
    if [[ ! "$comment_id" =~ ^[1-9][0-9]*$ || \
          ( -n "$commenter_id" && ! "$commenter_id" =~ ^[1-9][0-9]*$ ) || \
          ( -n "$commenter_login" && ! "$commenter_login" =~ ^[A-Za-z0-9-]+(\[bot\])?$ ) || \
          ( -n "$commenter_type" && ! "$commenter_type" =~ ^(User|Bot)$ ) || \
          -z "$encoded_body" || -n "$comment_extra" ]]; then
      echo "GitHub API returned malformed issue-comment data for PR #$pr_number." >&2
      exit 1
    fi
    if ! github_timestamp_is_valid "$comment_created_at"; then
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
          if codex_body_is_clean "$comment_body" required; then
            record_codex_evidence "$comment_created_at" clean
          elif codex_body_is_clean_security_review "$comment_body"; then
            # Security-review evidence is an independent channel.  A strictly
            # recognized no-findings result is neutral here: it cannot satisfy
            # or supersede the required code review.  Any other marker-bearing
            # body remains adverse and fails closed below.
            :
          else
            record_codex_evidence "$comment_created_at" adverse
          fi
        fi
      elif [[ "$codex_marker_count" -gt 0 ]]; then
        codex_conflict=1
      fi
    fi
  done
  fi

  if [[ "$codex_latest_count" -gt 1 ]]; then
    codex_conflict=1
  elif [[ "$codex_latest_count" -eq 1 ]]; then
    case "$codex_latest_kind" in
      clean) codex_evidence=1 ;;
      suggestions)
        if [[ "$unresolved" -eq 0 ]]; then
          codex_evidence=1
        fi
        ;;
      *) codex_conflict=1 ;;
    esac
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
