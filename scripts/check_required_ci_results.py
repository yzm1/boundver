#!/usr/bin/env python3
"""Publish the base-controlled, merge-critical CI status for one pull request.

This program is executed by ``required-pr-gate.yml`` from the pull request's
base commit after the unprivileged ``CI`` workflow completes. It never checks
out or executes pull-request code. A pull request that changes CI or either
gate control file is intentionally ineligible for automatic approval and must
use the reviewed ``docs/RELEASING.md#required-gate-control-maintenance``
procedure.
"""

from __future__ import annotations

import json
import math
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any


EVENT_PATH_ENV = "GITHUB_EVENT_PATH"
TOKEN_ENV = "BOUNDVER_GATE_TOKEN"
REPOSITORY = "yzm1/boundver"
REPOSITORY_ID = 1_226_008_327
REPOSITORY_NAME = "boundver"
BASE_BRANCH = "main"
SOURCE_WORKFLOW_NAME = "CI"
SOURCE_WORKFLOW_PATH = ".github/workflows/ci.yml"
STATUS_CONTEXT = "required-pr-gate"
API_ROOT = "https://api.github.com"
MAX_EVENT_BYTES = 1024 * 1024
MAX_API_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PULL_FILES = 300
MAX_TOKEN_BYTES = 4_096
MAX_JSON_TOKENS = 500_000
MAX_JSON_DEPTH = 128
MAX_JSON_INTEGER_DIGITS = 4_300
MAX_JSON_NUMBER_CHARS = MAX_JSON_INTEGER_DIGITS + 32
SHA_RE = re.compile(r"[0-9a-f]{40}")

EXPECTED_JOBS = (
    "Public Action contract (macos-15, Python 3.12)",
    "Public Action contract (ubuntu-latest, Python 3.12)",
    "Public Action contract (ubuntu-latest, Python 3.10)",
    "Public Action contract (windows-latest, Python 3.12)",
    "Public installation contracts (macos-15)",
    "Public installation contracts (ubuntu-latest)",
    "Public installation contracts (windows-latest)",
    "build",
    "test (macos-15, 3.12)",
    "test (macos-15-intel, 3.12)",
    "test (ubuntu-latest, 3.10)",
    "test (ubuntu-latest, 3.11)",
    "test (ubuntu-latest, 3.12)",
    "test (ubuntu-latest, 3.13)",
    "test (ubuntu-latest, 3.14)",
    "test (windows-latest, 3.10)",
    "test (windows-latest, 3.11)",
    "test (windows-latest, 3.12)",
    "test (windows-latest, 3.13)",
    "test (windows-latest, 3.14)",
)

PROTECTED_PATHS = frozenset(
    {
        ".github",
        ".github/rulesets",
        ".github/rulesets/protect-main.json",
        ".github/workflows",
        "scripts",
        "scripts/check_required_ci_results.py",
    }
)
PROTECTED_PREFIXES = (".github/workflows/",)


class RequiredCiGateError(ValueError):
    """The source run cannot safely produce the required success status."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Never replay the status-writing token after an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RequiredCiGateError("GitHub API redirects are not permitted")


def _public_tls_context() -> ssl.SSLContext:
    """Load host trust without environment-selected CAs or TLS key logging."""
    removed: dict[str, str] = {}
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE"):
        value = os.environ.pop(name, None)
        if value is not None:
            removed[name] = value
    try:
        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    finally:
        os.environ.update(removed)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


_GITHUB_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=_public_tls_context()),
    _RejectRedirects(),
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RequiredCiGateError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _bounded_json_int(value: str) -> int:
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) is None:
        raise ValueError("invalid JSON integer")
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError(
            "JSON integer exceeds the "
            f"{MAX_JSON_INTEGER_DIGITS}-decimal-digit limit"
        )
    result = 0
    first = len(digits) % 9 or 9
    for end in range(first, len(digits) + 1, 9):
        width = first if end == first else 9
        result = result * (10**width) + int(digits[end - width : end])
    return -result if negative else result


def _bounded_json_float(value: str) -> float:
    if len(value) > MAX_JSON_NUMBER_CHARS:
        raise ValueError(
            f"JSON number exceeds the {MAX_JSON_NUMBER_CHARS}-character limit"
        )
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number is not supported")
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite JSON number is not supported")


def _json_shape_within_limits(raw: bytes) -> bool:
    """Reject provably wide or deep JSON before ``json.loads`` allocates it."""
    tokens = 0
    depth = 0
    in_string = False
    escaped = False
    in_atom = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            tokens += 1
            in_string = True
            in_atom = False
        elif byte in {0x5B, 0x7B}:
            tokens += 1
            depth += 1
            in_atom = False
            if depth > MAX_JSON_DEPTH:
                return False
        elif byte in {0x5D, 0x7D}:
            depth = max(0, depth - 1)
            in_atom = False
        elif byte in {0x09, 0x0A, 0x0D, 0x20, 0x2C, 0x3A}:
            in_atom = False
        elif not in_atom:
            tokens += 1
            in_atom = True
        if tokens > MAX_JSON_TOKENS:
            return False
    return True


def _decode_json(raw: bytes, *, label: str, limit: int) -> object:
    if len(raw) > limit:
        raise RequiredCiGateError(f"{label} exceeds the {limit}-byte limit")
    if not _json_shape_within_limits(raw):
        raise RequiredCiGateError(f"{label} exceeds the JSON structural limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequiredCiGateError(f"{label} is not UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
            parse_float=_bounded_json_float,
            parse_int=_bounded_json_int,
        )
    except RequiredCiGateError:
        raise
    except (json.JSONDecodeError, OverflowError, RecursionError, ValueError) as exc:
        raise RequiredCiGateError(f"{label} is not valid JSON") from exc


def _read_event(path: str) -> dict[str, Any]:
    if type(path) is not str or not path:
        raise RequiredCiGateError(f"{EVENT_PATH_ENV} is missing or empty")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_EVENT_BYTES + 1)
    except OSError as exc:
        raise RequiredCiGateError("cannot read the workflow event") from exc
    value = _decode_json(raw, label="workflow event", limit=MAX_EVENT_BYTES)
    if type(value) is not dict:
        raise RequiredCiGateError("workflow event must be a JSON object")
    return value


def _require_dict(value: object, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise RequiredCiGateError(f"{label} must be an object")
    return value


def _require_positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise RequiredCiGateError(f"{label} must be a positive integer")
    return value


def _require_sha(value: object, label: str) -> str:
    if type(value) is not str or SHA_RE.fullmatch(value) is None:
        raise RequiredCiGateError(f"{label} must be a lowercase 40-digit SHA")
    return value


def _canonical_repo(value: object, label: str) -> dict[str, Any]:
    repo = _require_dict(value, label)
    if repo.get("id") != REPOSITORY_ID:
        raise RequiredCiGateError(f"{label} does not identify {REPOSITORY}")
    full_name = repo.get("full_name")
    name = repo.get("name")
    html_url = repo.get("html_url")
    api_url = repo.get("url")
    if full_name is not None and full_name != REPOSITORY:
        raise RequiredCiGateError(f"{label} has the wrong full name")
    if name is not None and name != REPOSITORY_NAME:
        raise RequiredCiGateError(f"{label} has the wrong repository name")
    if html_url is not None and html_url != f"https://github.com/{REPOSITORY}":
        raise RequiredCiGateError(f"{label} has the wrong HTML URL")
    if api_url is not None and api_url != f"{API_ROOT}/repos/{REPOSITORY}":
        raise RequiredCiGateError(f"{label} has the wrong API URL")
    if full_name is None and name is None and html_url is None and api_url is None:
        raise RequiredCiGateError(f"{label} has no canonical repository name")
    return repo


def _optional_head_repo(value: object, label: str) -> dict[str, Any]:
    """Validate identity shape without rejecting a legitimate fork."""
    repo = _require_dict(value, label)
    _require_positive_int(repo.get("id"), f"{label}.id")
    full_name = repo.get("full_name")
    name = repo.get("name")
    if full_name is not None and (type(full_name) is not str or "/" not in full_name):
        raise RequiredCiGateError(f"{label}.full_name is malformed")
    if name is not None and (type(name) is not str or not name):
        raise RequiredCiGateError(f"{label}.name is malformed")
    if full_name is None and name is None:
        raise RequiredCiGateError(f"{label} has no repository name")
    return repo


class GitHubClient:
    """Small bounded client constrained to this repository's GitHub API."""

    def __init__(self, token: str) -> None:
        if type(token) is not str or not token:
            raise RequiredCiGateError(f"{TOKEN_ENV} is missing or empty")
        try:
            token_bytes = token.encode("ascii")
        except UnicodeEncodeError as exc:
            raise RequiredCiGateError("GitHub token is not ASCII") from exc
        if len(token_bytes) > MAX_TOKEN_BYTES or any(
            byte < 0x21 or byte > 0x7E for byte in token_bytes
        ):
            raise RequiredCiGateError("GitHub token has an invalid shape")
        self._token = token

    def _request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, object] | None = None,
    ) -> object:
        prefix = f"/repos/{REPOSITORY}/"
        if type(endpoint) is not str or not endpoint.startswith(prefix):
            raise RequiredCiGateError(
                "refusing a GitHub API endpoint outside the repository"
            )
        body = None
        if payload is not None:
            body = json.dumps(
                payload, ensure_ascii=True, separators=(",", ":")
            ).encode("ascii")
        request = urllib.request.Request(
            API_ROOT + endpoint,
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "boundver-required-pr-gate",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with _GITHUB_OPENER.open(request, timeout=30) as response:
                raw = response.read(MAX_API_RESPONSE_BYTES + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise RequiredCiGateError("GitHub API request failed") from exc
        return _decode_json(
            raw,
            label="GitHub API response",
            limit=MAX_API_RESPONSE_BYTES,
        )

    def get(self, endpoint: str) -> object:
        return self._request("GET", endpoint)

    def status(
        self,
        sha: str,
        *,
        state: str,
        description: str,
        target_url: str,
    ) -> None:
        sha = _require_sha(sha, "commit status SHA")
        endpoint = f"/repos/{REPOSITORY}/statuses/{sha}"
        value = self._request(
            "POST",
            endpoint,
            {
                "state": state,
                "context": STATUS_CONTEXT,
                "description": description,
                "target_url": target_url,
            },
        )
        record = _require_dict(value, "commit-status response")
        if (
            record.get("state") != state
            or record.get("context") != STATUS_CONTEXT
            or record.get("description") != description
            or record.get("target_url") != target_url
            or record.get("url") != API_ROOT + endpoint
        ):
            raise RequiredCiGateError("GitHub returned a mismatched commit status")


def _validate_source_event(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("action") != "completed":
        raise RequiredCiGateError("workflow event action is not completed")
    _canonical_repo(event.get("repository"), "event repository")
    run = _require_dict(event.get("workflow_run"), "workflow_run")
    run_id = _require_positive_int(run.get("id"), "workflow_run.id")
    attempt = _require_positive_int(run.get("run_attempt"), "workflow_run.run_attempt")
    sha = _require_sha(run.get("head_sha"), "workflow_run.head_sha")
    expected_url = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
    actual_path = run.get("path")
    if isinstance(actual_path, str) and actual_path.startswith(REPOSITORY + "/"):
        actual_path = actual_path[len(REPOSITORY) + 1 :]
    checks = (
        (run.get("name") == SOURCE_WORKFLOW_NAME, "workflow name is not CI"),
        (
            actual_path == SOURCE_WORKFLOW_PATH,
            "workflow path is not the canonical CI workflow",
        ),
        (run.get("event") == "pull_request", "source run is not a pull-request run"),
        (run.get("status") == "completed", "source run is not complete"),
        (run.get("conclusion") == "success", "source CI workflow did not succeed"),
        (run.get("html_url") == expected_url, "source run URL is not canonical"),
    )
    for passed, message in checks:
        if not passed:
            raise RequiredCiGateError(message)
    return {
        "run": run,
        "run_id": run_id,
        "attempt": attempt,
        "sha": sha,
        "run_url": expected_url,
    }


def _event_pull(run: dict[str, Any], sha: str) -> dict[str, Any]:
    pulls = run.get("pull_requests")
    if type(pulls) is not list or len(pulls) != 1:
        raise RequiredCiGateError("source run must identify exactly one pull request")
    pull = _require_dict(pulls[0], "workflow_run.pull_requests[0]")
    number = _require_positive_int(pull.get("number"), "pull request number")
    head = _require_dict(pull.get("head"), "event pull-request head")
    base = _require_dict(pull.get("base"), "event pull-request base")
    if _require_sha(head.get("sha"), "event pull-request head SHA") != sha:
        raise RequiredCiGateError("event pull-request head does not match the source run")
    _optional_head_repo(head.get("repo"), "event pull-request head repository")
    if base.get("ref") != BASE_BRANCH:
        raise RequiredCiGateError("event pull request does not target main")
    _require_sha(base.get("sha"), "event pull-request base SHA")
    _canonical_repo(base.get("repo"), "event pull-request base repository")
    return {"number": number, "head": head, "base": base}


def _validate_live_pull(
    value: object,
    *,
    event_pull: dict[str, Any],
    sha: str,
) -> dict[str, Any]:
    pull = _require_dict(value, "pull-request response")
    if pull.get("number") != event_pull["number"] or pull.get("state") != "open":
        raise RequiredCiGateError("pull request is not the expected open pull request")
    head = _require_dict(pull.get("head"), "live pull-request head")
    base = _require_dict(pull.get("base"), "live pull-request base")
    if _require_sha(head.get("sha"), "live pull-request head SHA") != sha:
        raise RequiredCiGateError("pull-request head changed after the source run")
    live_head_repo = _optional_head_repo(
        head.get("repo"), "live pull-request head repository"
    )
    event_head_repo = _require_dict(event_pull["head"], "event pull-request head")[
        "repo"
    ]
    if live_head_repo.get("id") != event_head_repo.get("id"):
        raise RequiredCiGateError("pull-request head repository identity changed")
    if base.get("ref") != BASE_BRANCH:
        raise RequiredCiGateError("pull request no longer targets main")
    if base.get("sha") != event_pull["base"].get("sha"):
        raise RequiredCiGateError("pull-request base changed after the source run")
    _canonical_repo(base.get("repo"), "live pull-request base repository")
    changed_files = pull.get("changed_files")
    if type(changed_files) is not int or not 0 <= changed_files <= MAX_PULL_FILES:
        raise RequiredCiGateError(
            f"pull-request changed-file count exceeds the {MAX_PULL_FILES}-file limit"
        )
    return {"number": pull["number"], "changed_files": changed_files}


def _validate_jobs(value: object, *, run_id: int, attempt: int) -> None:
    payload = _require_dict(value, "workflow jobs response")
    jobs = payload.get("jobs")
    total_count = payload.get("total_count")
    if type(jobs) is not list or type(total_count) is not int:
        raise RequiredCiGateError("workflow jobs response has the wrong shape")
    if total_count != len(jobs):
        raise RequiredCiGateError("workflow jobs response is incomplete")
    names: list[str] = []
    failures: list[str] = []
    for index, item in enumerate(jobs):
        job = _require_dict(item, f"workflow job {index}")
        name = job.get("name")
        if type(name) is not str or not name:
            raise RequiredCiGateError("workflow job has no name")
        if job.get("run_id") != run_id or job.get("run_attempt") != attempt:
            raise RequiredCiGateError(f"workflow job {name!r} has the wrong run identity")
        names.append(name)
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            failures.append(name)
    if len(names) != len(set(names)):
        raise RequiredCiGateError("source workflow contains duplicate job names")
    expected = set(EXPECTED_JOBS)
    actual = set(names)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        raise RequiredCiGateError(
            "source workflow job topology does not match the merge contract ("
            + "; ".join(details)
            + ")"
        )
    if failures:
        raise RequiredCiGateError(
            "merge-critical jobs did not succeed: " + ", ".join(sorted(failures))
        )


def _is_protected_path(path: str) -> bool:
    return path in PROTECTED_PATHS or path.startswith(PROTECTED_PREFIXES)


def _validate_repo_path(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise RequiredCiGateError(f"{label} is malformed")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RequiredCiGateError(f"{label} is not valid UTF-8") from exc
    if len(encoded) > 4_096:
        raise RequiredCiGateError(f"{label} is malformed")
    if (
        value.startswith("/")
        or "\\" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise RequiredCiGateError(f"{label} is not a canonical repository path")
    return value


def _validate_files(
    value: object,
    *,
    base_sha: str,
    head_sha: str,
    changed_files: int,
) -> None:
    comparison = _require_dict(value, "immutable comparison response")
    compare_path = f"{base_sha}...{head_sha}"
    if (
        comparison.get("url")
        != f"{API_ROOT}/repos/{REPOSITORY}/compare/{compare_path}"
        or comparison.get("html_url")
        != f"https://github.com/{REPOSITORY}/compare/{compare_path}"
    ):
        raise RequiredCiGateError(
            "immutable comparison does not identify the validated base and head"
        )
    base_commit = _require_dict(
        comparison.get("base_commit"), "comparison base commit"
    )
    merge_base = _require_dict(
        comparison.get("merge_base_commit"), "comparison merge-base commit"
    )
    if (
        base_commit.get("sha") != base_sha
        or merge_base.get("sha") != base_sha
        or comparison.get("status") not in {"ahead", "identical"}
        or comparison.get("behind_by") != 0
    ):
        raise RequiredCiGateError(
            "immutable comparison is not anchored to the validated pull-request base"
        )
    ahead_by = comparison.get("ahead_by")
    total_commits = comparison.get("total_commits")
    if (
        type(ahead_by) is not int
        or type(total_commits) is not int
        or ahead_by < 0
        or total_commits != ahead_by
        or (changed_files == 0) != (comparison.get("status") == "identical")
    ):
        raise RequiredCiGateError("immutable comparison has inconsistent commit counts")
    files = comparison.get("files")
    if type(files) is not list or len(files) != changed_files:
        raise RequiredCiGateError("immutable comparison file listing is incomplete")
    seen: set[str] = set()
    for index, item in enumerate(files):
        record = _require_dict(item, f"comparison file {index}")
        filename = _validate_repo_path(record.get("filename"), "comparison filename")
        if filename in seen:
            raise RequiredCiGateError(
                "immutable comparison file listing is malformed or duplicated"
            )
        seen.add(filename)
        paths = [filename]
        previous = record.get("previous_filename")
        if previous is not None:
            paths.append(
                _validate_repo_path(previous, "comparison previous filename")
            )
        for path in paths:
            if _is_protected_path(path):
                raise RequiredCiGateError(
                    f"pull request changes protected gate control {path!r}"
                )
    if len(seen) != changed_files:  # pragma: no cover - defensive invariant
        raise RequiredCiGateError("immutable comparison file count is inconsistent")


def evaluate(event: dict[str, Any], fetch: Callable[[str], object]) -> dict[str, object]:
    """Validate the event, exact source jobs, current PR, and changed paths."""
    source = _validate_source_event(event)
    event_pull = _event_pull(source["run"], source["sha"])
    run_id = source["run_id"]
    attempt = source["attempt"]
    jobs = fetch(
        f"/repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"
    )
    _validate_jobs(jobs, run_id=run_id, attempt=attempt)
    number = event_pull["number"]
    live_pull = _validate_live_pull(
        fetch(f"/repos/{REPOSITORY}/pulls/{number}"),
        event_pull=event_pull,
        sha=source["sha"],
    )
    base_sha = event_pull["base"]["sha"]
    comparison = fetch(
        f"/repos/{REPOSITORY}/compare/{base_sha}...{source['sha']}?per_page=1"
    )
    _validate_files(
        comparison,
        base_sha=base_sha,
        head_sha=source["sha"],
        changed_files=live_pull["changed_files"],
    )
    return {
        "sha": source["sha"],
        "run_url": source["run_url"],
        "pull_number": number,
    }


def _safe_status_target(event: object) -> tuple[str, str] | None:
    if type(event) is not dict:
        return None
    run = event.get("workflow_run")
    if type(run) is not dict:
        return None
    run_id = run.get("id")
    sha = run.get("head_sha")
    if type(run_id) is not int or run_id <= 0:
        return None
    if type(sha) is not str or SHA_RE.fullmatch(sha) is None:
        return None
    expected = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
    if run.get("html_url") != expected:
        return None
    return sha, expected


def main() -> int:
    event: object = None
    client: GitHubClient | None = None
    try:
        event = _read_event(os.environ.get(EVENT_PATH_ENV, ""))
        client = GitHubClient(os.environ.get(TOKEN_ENV, ""))
        result = evaluate(event, client.get)
        client.status(
            result["sha"],
            state="success",
            description="All merge-critical CI jobs passed under base-controlled policy.",
            target_url=result["run_url"],
        )
    except RequiredCiGateError as exc:
        print(f"ERROR: required PR gate failed: {exc}", file=sys.stderr)
        target = _safe_status_target(event)
        if client is not None and target is not None:
            try:
                client.status(
                    target[0],
                    state="failure",
                    description="Required PR gate rejected this CI run.",
                    target_url=target[1],
                )
            except RequiredCiGateError as status_exc:
                print(
                    f"ERROR: could not publish the failure status: {status_exc}",
                    file=sys.stderr,
                )
        return 1
    print(
        f"Required PR gate passed for PR #{result['pull_number']} at {result['sha']}."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
