#!/usr/bin/env python3
"""Dispatch and apply a verified compatibility-alias update.

``dispatch`` starts a narrowly scoped workflow from the exact commit controlling
the active publication and waits for it.  A first publication is controlled by
the immutable release tag itself; recovery is controlled by reviewed current
``main`` even when the older tag predates the alias workflow.  ``advance``
revalidates the originating publication and immutable release checkout before
performing one leased, monotonic alias update.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence


TAG_RE = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
ALIAS_RE = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)")
SHA_RE = re.compile(r"[0-9a-f]{40}")
REPOSITORY_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?"
)
WORKFLOW_FILE = "advance-release-alias.yml"
WORKFLOW_PATH = f".github/workflows/{WORKFLOW_FILE}"
PUBLICATION_WORKFLOW_PATH = ".github/workflows/publish.yml"
VERIFY_PYPI_JOB = "Verify PyPI bytes, installation, and provenance"
VERIFY_RELEASE_JOB = "verify-release"
ACTIVE_RUN_STATES = {"requested", "pending", "queued", "in_progress", "waiting"}
JOB_LOG_ENV_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z {3}"
    r"(?P<name>RELEASE_TAG|RELEASE_SHA|COMPATIBILITY_ALIAS)(?P<rest>.*)$"
)
JOB_LOG_ENV_VALUE_RE = re.compile(r": (?P<value>\S+)")
MAX_JOB_LOG_BYTES = 32 * 1024 * 1024


class AliasError(RuntimeError):
    """The requested alias operation is unsafe, ambiguous, or unreadable."""


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            text=True,
            capture_output=True,
            check=check,
        )
    except FileNotFoundError as error:
        raise AliasError(f"required command is unavailable: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "command failed").strip()
        raise AliasError(f"{' '.join(command)}: {detail}") from error


def _git(repo: Path, *arguments: str) -> str:
    return _run(("git", *arguments), cwd=repo).stdout.strip()


def _gh_json(repo: Path, endpoint: str) -> object:
    result = _run(("gh", "api", endpoint), cwd=repo)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AliasError(f"GitHub API returned invalid JSON for {endpoint}") from error


def _gh_job_log(repo: Path, repository: str, job_id: int) -> str:
    _positive_int(job_id, "originating verify-release job ID")
    endpoint = f"repos/{repository}/actions/jobs/{job_id}/logs"
    try:
        help_result = subprocess.run(
            ["gh", "api", "--help"],
            cwd=repo,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise AliasError("required command is unavailable: gh") from error
    if help_result.returncode != 0:
        detail = (help_result.stderr or help_result.stdout).decode(
            "utf-8", errors="replace"
        )
        raise AliasError(
            "cannot inspect GitHub CLI API capabilities: "
            f"{detail.strip() or 'unknown error'}"
        )
    help_output = (help_result.stdout or b"") + (help_result.stderr or b"")
    command = ["gh", "api"]
    if b"--allow-escape-sequences" in help_output:
        command.append("--allow-escape-sequences")
    command.append(endpoint)
    result = subprocess.run(command, cwd=repo, capture_output=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")
        raise AliasError(
            f"GitHub API failed for {endpoint}: "
            f"{detail.strip() or 'unknown error'}"
        )
    if not result.stdout:
        raise AliasError("originating verify-release job log is empty")
    if len(result.stdout) > MAX_JOB_LOG_BYTES:
        raise AliasError("originating verify-release job log exceeds the inspection limit")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AliasError("originating verify-release job log is not valid UTF-8") from error


def _require_release_input_evidence(
    job_log: str,
    *,
    tag: str,
    sha: str,
    alias: str,
) -> None:
    expected = {
        "RELEASE_TAG": tag,
        "RELEASE_SHA": sha,
        "COMPATIBILITY_ALIAS": alias,
    }
    observed: dict[str, list[str]] = {name: [] for name in expected}
    for line in job_log.splitlines():
        match = JOB_LOG_ENV_RE.fullmatch(line)
        if match is None:
            continue
        value_match = JOB_LOG_ENV_VALUE_RE.fullmatch(match.group("rest"))
        if value_match is None:
            raise AliasError(
                f"originating verify-release log has malformed {match.group('name')} evidence"
            )
        observed[match.group("name")].append(value_match.group("value"))
    counts = {name: len(values) for name, values in observed.items()}
    if not all(counts.values()) or len(set(counts.values())) != 1:
        raise AliasError(
            "originating verify-release log lacks complete release input triples"
        )
    for name, expected_value in expected.items():
        if any(value != expected_value for value in observed[name]):
            raise AliasError(
                f"originating verify-release log does not bind {name} exactly"
            )


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AliasError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AliasError(f"{label} must be a nonnegative integer")
    return value


def _parse_positive_int(value: str, label: str) -> int:
    if re.fullmatch(r"[1-9]\d*", value) is None:
        raise AliasError(f"{label} must be a positive decimal integer")
    return int(value)


def _validate_release_identity(
    tag: str,
    sha: str,
    alias: str,
    repository: str,
) -> None:
    match = TAG_RE.fullmatch(tag)
    if match is None:
        raise AliasError(f"invalid exact release tag: {tag!r}")
    if SHA_RE.fullmatch(sha) is None:
        raise AliasError("release SHA must be 40 lowercase hexadecimal characters")
    if ALIAS_RE.fullmatch(alias) is None:
        raise AliasError(f"invalid compatibility alias: {alias!r}")
    if alias != f"v{match.group(1)}.{match.group(2)}":
        raise AliasError(f"compatibility alias must be the release line for {tag}")
    if REPOSITORY_RE.fullmatch(repository) is None:
        raise AliasError(f"invalid GitHub repository: {repository!r}")


def _remote_ref(repo: Path, remote: str, ref: str) -> str | None:
    fields = _git(repo, "ls-remote", remote, ref).split()
    if not fields:
        return None
    if len(fields) != 2 or fields[1] != ref or SHA_RE.fullmatch(fields[0]) is None:
        raise AliasError(f"remote returned malformed ref data for {ref}")
    return fields[0]


def _run_title(alias: str, tag: str, publication_run_id: int, attempt: int) -> str:
    return (
        f"Advance {alias} to {tag} for publication {publication_run_id} "
        f"attempt {attempt}"
    )


def _validate_publication_control(
    publication_ref: str,
    publication_sha: str,
    *,
    tag: str,
    release_sha: str,
) -> None:
    """Require an exact-tag initial control or an exact-main recovery control."""
    if publication_ref not in {tag, "main"}:
        raise AliasError("publication ref must be the exact release tag or main")
    if publication_ref == tag and publication_sha != release_sha:
        raise AliasError("exact-tag publication control must match the release SHA")


def _matching_alias_runs(
    payload: object,
    *,
    title: str,
    control_ref: str,
    control_sha: str,
) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(
        payload.get("workflow_runs"), list
    ):
        raise AliasError("alias workflow run listing is malformed")
    total_count = _nonnegative_int(
        payload.get("total_count"), "alias workflow run count"
    )
    if total_count != len(payload["workflow_runs"]):
        raise AliasError("alias workflow run listing is incomplete")
    runs = []
    for item in payload["workflow_runs"]:
        if not isinstance(item, dict):
            raise AliasError("alias workflow run entry is malformed")
        if item.get("display_title") != title:
            continue
        if (
            item.get("path") != WORKFLOW_PATH
            or item.get("event") != "workflow_dispatch"
            or item.get("head_branch") != control_ref
            or item.get("head_sha") != control_sha
        ):
            raise AliasError("matching alias workflow run is bound to different inputs")
        _positive_int(item.get("id"), "alias workflow run ID")
        _positive_int(item.get("run_attempt"), "alias workflow run attempt")
        if not isinstance(item.get("status"), str):
            raise AliasError("alias workflow run status is malformed")
        conclusion = item.get("conclusion")
        if conclusion is not None and not isinstance(conclusion, str):
            raise AliasError("alias workflow run conclusion is malformed")
        if not isinstance(item.get("html_url"), str) or not item["html_url"]:
            raise AliasError("alias workflow run URL is malformed")
        runs.append(item)
    if len(runs) > 1:
        raise AliasError("multiple alias workflow runs match one publication attempt")
    return runs


def _find_alias_run(
    repo: Path,
    repository: str,
    *,
    title: str,
    control_ref: str,
    control_sha: str,
) -> dict | None:
    endpoint = (
        f"repos/{repository}/actions/workflows/{WORKFLOW_FILE}/runs"
        f"?event=workflow_dispatch&head_sha={control_sha}&per_page=100"
    )
    runs = _matching_alias_runs(
        _gh_json(repo, endpoint),
        title=title,
        control_ref=control_ref,
        control_sha=control_sha,
    )
    return runs[0] if runs else None


def dispatch_alias_workflow(
    *,
    repo: Path,
    repository: str,
    remote: str,
    tag: str,
    sha: str,
    alias: str,
    publication_run_id: int,
    publication_attempt: int,
    publication_ref: str,
    publication_sha: str,
    attempts: int = 240,
    delay_seconds: float = 5.0,
) -> str:
    _validate_release_identity(tag, sha, alias, repository)
    _positive_int(publication_run_id, "publication run ID")
    _positive_int(publication_attempt, "publication attempt")
    if SHA_RE.fullmatch(publication_sha) is None:
        raise AliasError("publication SHA must be 40 lowercase hexadecimal characters")
    _validate_publication_control(
        publication_ref, publication_sha, tag=tag, release_sha=sha
    )
    if attempts <= 0 or delay_seconds < 0:
        raise AliasError("poll attempts must be positive and delay cannot be negative")

    tag_ref = f"refs/tags/{tag}"
    alias_ref = f"refs/tags/{alias}"
    if _remote_ref(repo, remote, tag_ref) != sha:
        raise AliasError("exact release tag moved or disappeared before alias dispatch")
    if _remote_ref(repo, remote, alias_ref) == sha:
        return f"{alias} already resolves to {sha}"

    title = _run_title(alias, tag, publication_run_id, publication_attempt)
    dispatched = False
    dispatch_error = ""
    for poll in range(attempts):
        run = _find_alias_run(
            repo,
            repository,
            title=title,
            control_ref=publication_ref,
            control_sha=publication_sha,
        )
        if run is None and not dispatched:
            result = _run(
                (
                    "gh",
                    "workflow",
                    "run",
                    WORKFLOW_FILE,
                    "--repo",
                    repository,
                    "--ref",
                    publication_ref,
                    "--field",
                    f"release_tag={tag}",
                    "--field",
                    f"release_sha={sha}",
                    "--field",
                    f"compatibility_alias={alias}",
                    "--field",
                    f"publication_run_id={publication_run_id}",
                    "--field",
                    f"publication_attempt={publication_attempt}",
                    "--field",
                    f"publication_ref={publication_ref}",
                    "--field",
                    f"publication_sha={publication_sha}",
                ),
                cwd=repo,
                check=False,
            )
            dispatched = True
            if result.returncode != 0:
                dispatch_error = (
                    result.stderr or result.stdout or "workflow dispatch failed"
                ).strip()
        elif run is not None:
            status = run["status"]
            if status == "completed":
                if run.get("conclusion") != "success":
                    raise AliasError(
                        "release-control alias workflow failed: " + run["html_url"]
                    )
                if _remote_ref(repo, remote, alias_ref) != sha:
                    raise AliasError(
                        "alias workflow succeeded but the remote alias is not exact"
                    )
                return run["html_url"]
            if status not in ACTIVE_RUN_STATES:
                raise AliasError(f"unexpected alias workflow status: {status!r}")

        if poll + 1 < attempts:
            time.sleep(delay_seconds)

    detail = f": {dispatch_error}" if dispatch_error else ""
    raise AliasError(f"alias workflow did not complete within the polling window{detail}")


def _validate_publication_payloads(
    run: object,
    jobs: object,
    *,
    repository: str,
    publication_run_id: int,
    publication_attempt: int,
    publication_ref: str,
    publication_sha: str,
    tag: str,
    release_sha: str,
) -> int:
    if not isinstance(run, dict):
        raise AliasError("originating publication run is malformed")
    run_id = _positive_int(run.get("id"), "originating publication run ID")
    run_attempt = _positive_int(
        run.get("run_attempt"), "originating publication run attempt"
    )
    _validate_publication_control(
        publication_ref,
        publication_sha,
        tag=tag,
        release_sha=release_sha,
    )
    if (
        run_id != publication_run_id
        or run_attempt != publication_attempt
        or run.get("event") != "workflow_dispatch"
        or run.get("status") != "in_progress"
        or run.get("conclusion") is not None
        or run.get("path") != PUBLICATION_WORKFLOW_PATH
        or run.get("head_sha") != publication_sha
        or run.get("head_branch") != publication_ref
    ):
        raise AliasError("originating publication run is not the active exact workflow")
    repository_payload = run.get("repository")
    if not isinstance(repository_payload, dict) or repository_payload.get(
        "full_name"
    ) != repository:
        raise AliasError("originating publication repository is malformed or different")

    if not isinstance(jobs, dict) or not isinstance(jobs.get("jobs"), list):
        raise AliasError("originating publication job listing is malformed")
    total_count = _nonnegative_int(
        jobs.get("total_count"), "originating publication job count"
    )
    if total_count != len(jobs["jobs"]):
        raise AliasError("originating publication job count is inconsistent")
    successful_jobs: dict[str, list[dict]] = {
        VERIFY_PYPI_JOB: [],
        VERIFY_RELEASE_JOB: [],
    }
    successful_attempts: dict[str, set[int]] = {
        VERIFY_PYPI_JOB: set(),
        VERIFY_RELEASE_JOB: set(),
    }
    for job in jobs["jobs"]:
        if not isinstance(job, dict):
            raise AliasError("originating publication job entry is malformed")
        name = job.get("name")
        if name not in {VERIFY_PYPI_JOB, VERIFY_RELEASE_JOB}:
            continue
        job_id = _positive_int(
            job.get("id"), "originating PyPI verification job ID"
        )
        job_run_id = _positive_int(
            job.get("run_id"), "originating PyPI verification run ID"
        )
        job_attempt = _positive_int(
            job.get("run_attempt"), "originating PyPI verification attempt"
        )
        if (
            job_run_id != publication_run_id
            or job_attempt > publication_attempt
            or job.get("head_sha") != publication_sha
        ):
            raise AliasError("originating release gate job is not bound to this run")
        # A failed-jobs rerun advances the run attempt without rerunning already
        # successful prerequisites. Historical failures are harmless; require
        # at least one exact success and reject duplicate successes in any one
        # attempt instead of making retries depend on the latest attempt only.
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            continue
        if job_attempt in successful_attempts[name]:
            raise AliasError(
                f"originating publication has duplicate successful {name!r} "
                f"jobs in attempt {job_attempt}"
            )
        successful_attempts[name].add(job_attempt)
        successful_jobs[name].append({**job, "id": job_id})
    pypi_matches = successful_jobs[VERIFY_PYPI_JOB]
    release_matches = successful_jobs[VERIFY_RELEASE_JOB]
    if not pypi_matches:
        raise AliasError(
            "originating publication must contain a successful PyPI verification job"
        )
    if not release_matches:
        raise AliasError(
            "originating publication must contain a successful verify-release job"
        )
    latest_release = max(
        release_matches,
        key=lambda job: (job["run_attempt"], job["id"]),
    )
    return latest_release["id"]


def verify_originating_publication(
    *,
    repo: Path,
    repository: str,
    publication_run_id: int,
    publication_attempt: int,
    publication_ref: str,
    publication_sha: str,
    tag: str,
    release_sha: str,
    alias: str,
) -> None:
    run = _gh_json(repo, f"repos/{repository}/actions/runs/{publication_run_id}")
    jobs = _gh_json(
        repo,
        f"repos/{repository}/actions/runs/{publication_run_id}/jobs"
        "?filter=all&per_page=100",
    )
    verify_release_job_id = _validate_publication_payloads(
        run,
        jobs,
        repository=repository,
        publication_run_id=publication_run_id,
        publication_attempt=publication_attempt,
        publication_ref=publication_ref,
        publication_sha=publication_sha,
        tag=tag,
        release_sha=release_sha,
    )
    _require_release_input_evidence(
        _gh_job_log(repo, repository, verify_release_job_id),
        tag=tag,
        sha=release_sha,
        alias=alias,
    )


def _same_line_patch_for_sha(alias: str, sha: str, remote_tags: str) -> int:
    patches = []
    pattern = re.compile(r"refs/tags/" + re.escape(alias) + r"\.([0-9]+)")
    for line in remote_tags.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or SHA_RE.fullmatch(fields[0]) is None:
            raise AliasError("remote returned malformed same-line tag data")
        match = pattern.fullmatch(fields[1])
        if match is not None and fields[0] == sha:
            patches.append(int(match.group(1)))
    if not patches:
        raise AliasError(
            "existing compatibility alias does not point to an exact same-line release"
        )
    return max(patches)


def advance_alias(
    *,
    repo: Path,
    repository: str,
    remote: str,
    tag: str,
    sha: str,
    alias: str,
    publication_run_id: int,
    publication_attempt: int,
    publication_ref: str,
    publication_sha: str,
) -> str:
    _validate_release_identity(tag, sha, alias, repository)
    _positive_int(publication_run_id, "publication run ID")
    _positive_int(publication_attempt, "publication attempt")
    if SHA_RE.fullmatch(publication_sha) is None:
        raise AliasError("publication SHA must be 40 lowercase hexadecimal characters")
    _validate_publication_control(
        publication_ref, publication_sha, tag=tag, release_sha=sha
    )

    verify_originating_publication(
        repo=repo,
        repository=repository,
        publication_run_id=publication_run_id,
        publication_attempt=publication_attempt,
        publication_ref=publication_ref,
        publication_sha=publication_sha,
        tag=tag,
        release_sha=sha,
        alias=alias,
    )
    tag_ref = f"refs/tags/{tag}"
    alias_ref = f"refs/tags/{alias}"
    if _remote_ref(repo, remote, tag_ref) != sha:
        raise AliasError("exact release tag moved or disappeared before alias update")
    expected_current = _remote_ref(repo, remote, alias_ref)
    if expected_current == sha:
        return f"{alias} already resolves to {sha}"

    release_patch = int(TAG_RE.fullmatch(tag).group(3))  # type: ignore[union-attr]
    if expected_current is None:
        lease = f"--force-with-lease={alias_ref}:"
    else:
        local_before = "refs/boundver-release/compatibility-alias-before"
        _git(repo, "fetch", "--no-tags", remote, f"{alias_ref}:{local_before}")
        ancestry = _run(
            ("git", "merge-base", "--is-ancestor", expected_current, sha),
            cwd=repo,
            check=False,
        )
        if ancestry.returncode != 0:
            raise AliasError(
                f"refusing non-ancestral or rollback alias move: {expected_current} -> {sha}"
            )
        remote_tags = _git(
            repo,
            "ls-remote",
            "--tags",
            "--refs",
            remote,
            f"refs/tags/{alias}.*",
        )
        current_patch = _same_line_patch_for_sha(alias, expected_current, remote_tags)
        if current_patch >= release_patch:
            raise AliasError(
                f"refusing compatibility alias rollback from patch {current_patch} "
                f"to patch {release_patch}"
            )
        lease = f"--force-with-lease={alias_ref}:{expected_current}"

    # Surface verification can involve registry retries.  Revalidate the active
    # parent and immutable anchor immediately before the only ref mutation.
    verify_originating_publication(
        repo=repo,
        repository=repository,
        publication_run_id=publication_run_id,
        publication_attempt=publication_attempt,
        publication_ref=publication_ref,
        publication_sha=publication_sha,
        tag=tag,
        release_sha=sha,
        alias=alias,
    )
    if _remote_ref(repo, remote, tag_ref) != sha:
        raise AliasError("exact release tag moved at the alias mutation boundary")

    # Push a local tag ref from a workflow whose own github.sha is the release
    # SHA.  This keeps the default token from introducing a different workflow
    # definition while the lease rejects concurrent alias changes.
    _git(repo, "tag", "--force", alias, sha)
    _git(repo, "push", lease, remote, alias_ref)
    if _remote_ref(repo, remote, alias_ref) != sha:
        raise AliasError("remote compatibility alias does not match after push")
    return f"advanced {alias} to {sha}"


def verify_alias_request(
    *,
    repo: Path,
    repository: str,
    remote: str,
    tag: str,
    sha: str,
    alias: str,
    publication_run_id: int,
    publication_attempt: int,
    publication_ref: str,
    publication_sha: str,
) -> str:
    """Fail early unless the immutable tag and parent publication are exact."""
    _validate_release_identity(tag, sha, alias, repository)
    _positive_int(publication_run_id, "publication run ID")
    _positive_int(publication_attempt, "publication attempt")
    if SHA_RE.fullmatch(publication_sha) is None:
        raise AliasError("publication SHA must be 40 lowercase hexadecimal characters")
    _validate_publication_control(
        publication_ref, publication_sha, tag=tag, release_sha=sha
    )
    if _remote_ref(repo, remote, f"refs/tags/{tag}") != sha:
        raise AliasError("exact release tag moved or disappeared before alias verification")
    verify_originating_publication(
        repo=repo,
        repository=repository,
        publication_run_id=publication_run_id,
        publication_attempt=publication_attempt,
        publication_ref=publication_ref,
        publication_sha=publication_sha,
        tag=tag,
        release_sha=sha,
        alias=alias,
    )
    return "originating publication and immutable release tag are exact"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dispatch or apply a verified compatibility-alias update."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--repo-root", type=Path, default=Path("."))
        subparser.add_argument("--repository", required=True)
        subparser.add_argument("--remote", default="origin")
        subparser.add_argument("--tag", required=True)
        subparser.add_argument("--sha", required=True)
        subparser.add_argument("--alias", required=True)
        subparser.add_argument("--publication-run-id", required=True)
        subparser.add_argument("--publication-attempt", required=True)
        subparser.add_argument("--publication-ref", required=True)
        subparser.add_argument("--publication-sha", required=True)

    dispatch = subparsers.add_parser("dispatch")
    common(dispatch)
    dispatch.add_argument("--attempts", type=int, default=240)
    dispatch.add_argument("--delay-seconds", type=float, default=5.0)

    verify = subparsers.add_parser("verify")
    common(verify)

    advance = subparsers.add_parser("advance")
    common(advance)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        publication_run_id = _parse_positive_int(
            args.publication_run_id, "publication run ID"
        )
        publication_attempt = _parse_positive_int(
            args.publication_attempt, "publication attempt"
        )
        arguments = dict(
            repo=args.repo_root.resolve(),
            repository=args.repository,
            remote=args.remote,
            tag=args.tag,
            sha=args.sha,
            alias=args.alias,
            publication_run_id=publication_run_id,
            publication_attempt=publication_attempt,
            publication_ref=args.publication_ref,
            publication_sha=args.publication_sha,
        )
        if args.command == "dispatch":
            result = dispatch_alias_workflow(
                **arguments,
                attempts=args.attempts,
                delay_seconds=args.delay_seconds,
            )
        elif args.command == "verify":
            result = verify_alias_request(**arguments)
        else:
            result = advance_alias(**arguments)
    except (AliasError, OSError, ValueError) as error:
        print(f"Compatibility alias error: {error}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
