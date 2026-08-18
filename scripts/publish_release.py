#!/usr/bin/env python3
"""Fail-closed maintainer gate and dispatcher for a boundver release.

``check`` is read-only.  ``start`` repeats every check and then performs one
mutation: it dispatches ``create-release-tag.yml``.  ``resume`` validates and
reuses the retained artifacts from one failed publication run before its one
mutation: dispatching ``publish.yml`` in explicit recovery mode.  The protected
workflows, not this local process, own tag, Release, Marketplace, and
package-index writes.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10
    import tomli as tomllib


REPOSITORY = "yzm1/boundver"
TAG_RE = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
SHA_RE = re.compile(r"[0-9a-f]{40}")
ALIAS_RE = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)")
RUN_ID_RE = re.compile(r"[1-9]\d*")
ARTIFACT_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
JOB_LOG_ENV_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z {3}"
    r"(?P<name>RELEASE_TAG|RELEASE_SHA|COMPATIBILITY_ALIAS)(?P<rest>.*)$"
)
JOB_LOG_ENV_VALUE_RE = re.compile(r": (?P<value>\S+)")
MAX_JOB_LOG_BYTES = 32 * 1024 * 1024
SURFACES = (
    "repository hygiene",
    "README and documentation",
    "changelog and release notes",
    "schema URLs, configs, and locks",
    "CI and review state",
    "reproducible wheel, sdist, and standalone archive",
    "GitHub Action and Marketplace",
    "TestPyPI",
    "PyPI",
    "GitHub Release assets",
    "compatibility alias",
    "Docker",
    "pre-commit",
)


class GateError(RuntimeError):
    """A release prerequisite is absent, conflicting, or unreadable."""


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=check,
        )
    except FileNotFoundError as error:
        raise GateError(f"required command is unavailable: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "command failed").strip()
        raise GateError(f"{' '.join(command)}: {detail}") from error


def _git(repo: Path, *arguments: str, check: bool = True) -> str:
    return _run(("git", *arguments), cwd=repo, check=check).stdout.strip()


def _head(repo: Path) -> str | None:
    result = _run(("git", "rev-parse", "--verify", "HEAD"), cwd=repo, check=False)
    value = result.stdout.strip()
    return value if result.returncode == 0 and SHA_RE.fullmatch(value) else None


def _canonical_origin(value: str) -> str | None:
    value = value.strip().removesuffix(".git")
    match = re.fullmatch(
        r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
        r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)",
        value,
    )
    return match.group("repo") if match else None


def _github_ref_pattern_matches(pattern: str, ref: str) -> bool:
    """Match GitHub ruleset ref patterns with slash-aware fnmatch semantics."""
    if pattern == "~ALL":
        return True
    pattern_parts = pattern.split("/")
    ref_parts = ref.split("/")

    def match(pattern_index: int, ref_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return ref_index == len(ref_parts)
        segment = pattern_parts[pattern_index]
        if segment == "**":
            return match(pattern_index + 1, ref_index) or (
                ref_index < len(ref_parts)
                and match(pattern_index, ref_index + 1)
            )
        return (
            ref_index < len(ref_parts)
            and fnmatch.fnmatchcase(ref_parts[ref_index], segment)
            and match(pattern_index + 1, ref_index + 1)
        )

    return match(0, 0)


def _ruleset_targets_ref(ref_name: object, ref: str) -> bool:
    if not isinstance(ref_name, dict):
        return False
    includes = ref_name.get("include")
    excludes = ref_name.get("exclude")
    if not isinstance(includes, list) or not all(
        isinstance(item, str) for item in includes
    ):
        return False
    if not isinstance(excludes, list) or not all(
        isinstance(item, str) for item in excludes
    ):
        return False
    return any(_github_ref_pattern_matches(item, ref) for item in includes) and not any(
        _github_ref_pattern_matches(item, ref) for item in excludes
    )


def _environment_requires_review(item: object) -> bool:
    rules = item.get("protection_rules") if isinstance(item, dict) else None
    return any(
        isinstance(rule, dict)
        and rule.get("type") == "required_reviewers"
        and isinstance(rule.get("reviewers"), list)
        and bool(rule["reviewers"])
        for rule in rules or []
    )


def _validate_tag_rulesets(rulesets: Sequence[dict], tag: str) -> None:
    exact_ref = f"refs/tags/{tag}"
    alias_ref = f"refs/tags/{tag.rsplit('.', 1)[0]}"
    exact_update = False
    exact_deletion = False
    exact_creation = False
    alias_mutation = False
    for detail in rulesets:
        rules = detail.get("rules")
        conditions = detail.get("conditions")
        rule_types = {
            rule.get("type") for rule in rules or [] if isinstance(rule, dict)
        }
        ref_name = conditions.get("ref_name") if isinstance(conditions, dict) else None
        if _ruleset_targets_ref(ref_name, exact_ref):
            exact_update = exact_update or "update" in rule_types
            exact_deletion = exact_deletion or "deletion" in rule_types
            exact_creation = exact_creation or "creation" in rule_types
        if _ruleset_targets_ref(ref_name, alias_ref):
            alias_mutation = alias_mutation or bool(
                {"update", "creation"} & rule_types
            )
    if not exact_update or not exact_deletion:
        raise GateError(
            "active tag rulesets must block update and deletion for the exact version tag"
        )
    if exact_creation:
        raise GateError(
            "an active creation restriction targets the exact version tag and can block the workflow"
        )
    if alias_mutation:
        raise GateError(
            "an active creation/update restriction targets the mutable vMAJOR.MINOR alias"
        )


def _remote_ref(repo: Path, remote: str, ref: str) -> str | None:
    fields = _git(repo, "ls-remote", remote, ref).split()
    if not fields:
        return None
    if len(fields) != 2 or fields[1] != ref or SHA_RE.fullmatch(fields[0]) is None:
        raise GateError(f"remote returned malformed ref data for {ref}")
    return fields[0]


def _gh_json(repo: Path, repository: str, endpoint: str) -> object:
    result = _run(
        ("gh", "api", endpoint), cwd=repo, check=False
    )
    if result.returncode != 0:
        raise GateError(
            f"GitHub API failed for {endpoint}: "
            f"{(result.stderr or result.stdout).strip() or 'unknown error'}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise GateError(f"GitHub API returned invalid JSON for {endpoint}") from error


def _gh_job_log(repo: Path, job_id: int) -> str:
    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
        raise GateError("source verify-release job ID is malformed")
    endpoint = f"repos/{REPOSITORY}/actions/jobs/{job_id}/logs"
    try:
        help_result = subprocess.run(
            ["gh", "api", "--help"],
            cwd=repo,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise GateError("required command is unavailable: gh") from error
    if help_result.returncode != 0:
        detail = (help_result.stderr or help_result.stdout).decode(
            "utf-8", errors="replace"
        )
        raise GateError(
            "cannot inspect GitHub CLI API capabilities: "
            f"{detail.strip() or 'unknown error'}"
        )
    help_output = (help_result.stdout or b"") + (help_result.stderr or b"")
    command = ["gh", "api"]
    if b"--allow-escape-sequences" in help_output:
        command.append("--allow-escape-sequences")
    command.append(endpoint)
    result = subprocess.run(
        command,
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")
        raise GateError(
            f"GitHub API failed for {endpoint}: "
            f"{detail.strip() or 'unknown error'}"
        )
    if not result.stdout:
        raise GateError("source verify-release job log is empty")
    if len(result.stdout) > MAX_JOB_LOG_BYTES:
        raise GateError("source verify-release job log exceeds the inspection limit")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GateError("source verify-release job log is not valid UTF-8") from error


def _record(checks: list[Check], name: str, operation) -> object | None:
    try:
        detail = operation()
    except (GateError, OSError, ValueError, KeyError, TypeError) as error:
        checks.append(Check(name, "failed", str(error)))
        return None
    checks.append(Check(name, "passed", str(detail or "passed")))
    return detail


def _project(repo: Path, tag: str) -> str:
    try:
        project = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]
    except (OSError, UnicodeError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise GateError(f"cannot read pyproject.toml metadata: {error}") from error
    if project.get("name") != "boundver" or project.get("version") != tag[1:]:
        raise GateError("pyproject name/version does not match boundver and the release tag")
    return f"boundver {project['version']}"


def _project_at_commit(repo: Path, sha: str, tag: str) -> str:
    try:
        metadata = _git(repo, "show", f"{sha}:pyproject.toml")
        project = tomllib.loads(metadata)["project"]
    except (OSError, UnicodeError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise GateError(
            f"cannot read pyproject.toml metadata at release commit {sha}: {error}"
        ) from error
    if project.get("name") != "boundver" or project.get("version") != tag[1:]:
        raise GateError(
            "release-commit pyproject name/version does not match boundver and the release tag"
        )
    return f"boundver {project['version']} at {sha}"


def _clean(repo: Path) -> str:
    state = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if state:
        raise GateError("worktree and index must be clean (tracked, staged, and untracked)")
    git_dir = Path(_git(repo, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        if (git_dir / marker).exists():
            raise GateError(f"Git operation is still active: {marker}")
    return "clean worktree/index; no merge, rebase, cherry-pick, or revert"


def _repository_hygiene(repo: Path) -> str:
    result = _run(
        (sys.executable, "scripts/check_repo_hygiene.py", "--repo", "."),
        cwd=repo,
    )
    return result.stdout.strip() or "tracked repository tree is portable and clean"


def _repo_identity(repo: Path, remote: str) -> str:
    if Path(_git(repo, "rev-parse", "--show-toplevel")).resolve() != repo.resolve():
        raise GateError("--repo must be the repository root")
    origin = _git(repo, "remote", "get-url", remote)
    if _canonical_origin(origin) != REPOSITORY:
        raise GateError(f"{remote} is not canonical repository {REPOSITORY}")
    return f"{REPOSITORY} via {remote}"


def _main_identity(repo: Path, remote: str, sha: str) -> str:
    if _git(repo, "symbolic-ref", "--short", "HEAD") != "main":
        raise GateError("release checks must run from branch main")
    if _head(repo) != sha:
        raise GateError("HEAD changed during release checks")
    main = _remote_ref(repo, remote, "refs/heads/main")
    if main != sha:
        raise GateError(f"HEAD {sha} is not current remote main {main}")
    return sha


def _remote_release_state(repo: Path, remote: str, tag: str) -> str:
    tag_sha = _remote_ref(repo, remote, f"refs/tags/{tag}")
    if tag_sha is not None:
        raise GateError(f"exact tag already exists at {tag_sha}; use the original run to resume")
    branch = _remote_ref(repo, remote, f"refs/heads/release/{tag}")
    if branch is not None:
        raise GateError(f"legacy release branch already exists at {branch}; inspect its run")
    return "exact tag and legacy release branch are absent"


def _github_controls(
    repo: Path,
    sha: str,
    tag: str,
    *,
    allow_resumable_release: bool = False,
) -> str:
    metadata = _gh_json(repo, REPOSITORY, f"repos/{REPOSITORY}")
    if not isinstance(metadata, dict) or metadata.get("full_name") != REPOSITORY:
        raise GateError("authenticated GitHub repository identity disagrees")
    if metadata.get("default_branch") != "main" or metadata.get("archived") is not False:
        raise GateError("GitHub repository must be active with main as default branch")
    if metadata.get("visibility") != "public":
        raise GateError("GitHub repository must be public before release promotion")
    if metadata.get("homepage") != "https://github.com/marketplace/actions/boundver":
        raise GateError("GitHub repository homepage must point to the Marketplace listing")
    if not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
        raise GateError("GitHub repository description must be populated")
    topics = metadata.get("topics")
    required_topics = {"api-compatibility", "ci", "openapi", "semantic-versioning"}
    if not isinstance(topics, list) or not required_topics <= set(topics):
        raise GateError(
            "GitHub repository topics must include " + ", ".join(sorted(required_topics))
        )
    environments = _gh_json(repo, REPOSITORY, f"repos/{REPOSITORY}/environments")
    values = environments.get("environments") if isinstance(environments, dict) else None
    if not isinstance(values, list):
        raise GateError("cannot enumerate protected release environments")
    by_name = {item.get("name"): item for item in values if isinstance(item, dict)}
    for name in ("testpypi", "pypi", "marketplace"):
        item = by_name.get(name)
        if not _environment_requires_review(item):
            raise GateError(
                f"GitHub environment {name!r} must require at least one reviewer"
            )
    immutable = _gh_json(repo, REPOSITORY, f"repos/{REPOSITORY}/immutable-releases")
    if not isinstance(immutable, dict) or immutable.get("enabled") is not True:
        raise GateError("immutable GitHub Releases are not enabled")
    rulesets = _gh_json(
        repo, REPOSITORY, f"repos/{REPOSITORY}/rulesets?includes_parents=true"
    )
    if not isinstance(rulesets, list):
        raise GateError("cannot enumerate version-tag protection rulesets")
    tag_rulesets: list[dict] = []
    for summary in rulesets:
        if not isinstance(summary, dict) or summary.get("target") != "tag":
            continue
        if summary.get("enforcement") != "active":
            continue
        ruleset_id = summary.get("id")
        if not isinstance(ruleset_id, int):
            continue
        detail = _gh_json(repo, REPOSITORY, f"repos/{REPOSITORY}/rulesets/{ruleset_id}")
        if not isinstance(detail, dict):
            continue
        tag_rulesets.append(detail)
    _validate_tag_rulesets(tag_rulesets, tag)
    runs = _gh_json(
        repo,
        REPOSITORY,
        f"repos/{REPOSITORY}/actions/workflows/ci.yml/runs?head_sha={sha}&event=push&per_page=20",
    )
    workflow_runs = runs.get("workflow_runs") if isinstance(runs, dict) else None
    if not isinstance(workflow_runs, list) or not any(
        isinstance(run, dict)
        and run.get("head_sha") == sha
        and run.get("head_branch") == "main"
        and run.get("event") == "push"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        for run in workflow_runs
    ):
        raise GateError("no successful completed ci.yml push run for exact main SHA")
    active_states = {"requested", "pending", "queued", "in_progress", "waiting"}
    for workflow in ("create-release-tag.yml", "publish.yml"):
        promotions = _gh_json(
            repo,
            REPOSITORY,
            f"repos/{REPOSITORY}/actions/workflows/{workflow}/runs?per_page=100",
        )
        runs_value = (
            promotions.get("workflow_runs") if isinstance(promotions, dict) else None
        )
        if not isinstance(runs_value, list):
            raise GateError(f"cannot inspect active runs for {workflow}")
        if any(
            isinstance(run, dict) and run.get("status") in active_states
            for run in runs_value
        ):
            raise GateError(f"another release operation is active in {workflow}")
    release = _run(
        (
            "gh",
            "api",
            "--include",
            f"repos/{REPOSITORY}/releases/tags/{tag}",
        ),
        cwd=repo,
        check=False,
    )
    release_output = release.stdout + release.stderr
    status_match = re.search(r"(?m)^HTTP/\S+\s+(\d{3})\b", release_output)
    if release.returncode == 0 and status_match and status_match.group(1) == "200":
        if not allow_resumable_release:
            raise GateError("a GitHub Release already exists; use the original run to resume")
        release_detail = _gh_json(
            repo, REPOSITORY, f"repos/{REPOSITORY}/releases/tags/{tag}"
        )
        if (
            not isinstance(release_detail, dict)
            or release_detail.get("tag_name") != tag
            or not isinstance(release_detail.get("draft"), bool)
        ):
            raise GateError("GitHub Release state is malformed or disagrees with the tag")
        if release_detail["draft"] is False and (
            release_detail.get("immutable") is not True
            or release_detail.get("prerelease") is not False
            or not isinstance(release_detail.get("published_at"), str)
            or not release_detail["published_at"]
        ):
            raise GateError(
                "an existing public GitHub Release must be stable and immutable "
                "before recovery"
            )
    if status_match is None or status_match.group(1) != "404":
        if not (
            allow_resumable_release
            and release.returncode == 0
            and status_match is not None
            and status_match.group(1) == "200"
        ):
            if allow_resumable_release:
                raise GateError(
                    "cannot prove that the GitHub Release is absent, a draft, or "
                    "an immutable public release"
                )
            raise GateError("cannot prove that the GitHub Release is absent")
    return "repository, exact CI, environments, immutability, and promotion state verified"


def _resume_release_state(repo: Path, remote: str, tag: str, sha: str) -> str:
    tag_sha = _remote_ref(repo, remote, f"refs/tags/{tag}")
    if tag_sha is None:
        raise GateError(f"exact tag {tag} is absent; only the original start path may create it")
    if tag_sha != sha:
        raise GateError(f"exact tag {tag} resolves to {tag_sha}, not release SHA {sha}")
    branch = _remote_ref(repo, remote, f"refs/heads/release/{tag}")
    if branch is not None:
        raise GateError(f"legacy release branch already exists at {branch}; inspect its run")
    return f"exact tag resolves to {sha}; legacy release branch is absent"


def _release_is_on_main(repo: Path, release_sha: str, main_sha: str) -> str:
    result = _run(
        ("git", "merge-base", "--is-ancestor", release_sha, main_sha),
        cwd=repo,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if result.returncode == 1:
            detail = "release commit is not an ancestor of current main"
        raise GateError(detail or "cannot prove that the release commit is on main")
    return f"release commit {release_sha} is an ancestor of current main {main_sha}"


def _github_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise GateError(f"source artifact has invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GateError(f"source artifact has invalid {field}") from error
    if parsed.tzinfo is None:
        raise GateError(f"source artifact has invalid {field}")
    return parsed.astimezone(timezone.utc)


def _require_source_release_inputs(
    job_log: str,
    tag: str,
    sha: str,
    alias: str,
) -> str:
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
            raise GateError(
                f"source verify-release job log has malformed {match.group('name')} evidence"
            )
        observed[match.group("name")].append(value_match.group("value"))

    counts = {name: len(values) for name, values in observed.items()}
    if not all(counts.values()) or len(set(counts.values())) != 1:
        raise GateError(
            "source verify-release job log does not contain complete release input triples"
        )
    for name, expected_value in expected.items():
        values = observed[name]
        if any(value != expected_value for value in values):
            raise GateError(
                f"source verify-release job log does not bind {name} to the expected value"
            )
    return (
        f"verify-release job log binds {counts['RELEASE_TAG']} release input "
        f"triple(s) to {tag}, {sha}, and alias {alias}"
    )


def _source_release_artifacts(
    repo: Path,
    run_id: int,
    tag: str,
    sha: str,
    alias: str,
) -> str:
    run_endpoint = f"repos/{REPOSITORY}/actions/runs/{run_id}"
    run = _gh_json(repo, REPOSITORY, run_endpoint)
    if not isinstance(run, dict):
        raise GateError("source publication run response is malformed")
    repository = run.get("repository")
    attempt = run.get("run_attempt")
    if (
        run.get("id") != run_id
        or not isinstance(repository, dict)
        or repository.get("full_name") != REPOSITORY
        or run.get("path") != ".github/workflows/publish.yml"
        or run.get("event") != "workflow_dispatch"
        or run.get("status") != "completed"
        or run.get("conclusion") != "failure"
        or run.get("head_branch") != tag
        or run.get("head_sha") != sha
        or not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or attempt <= 0
    ):
        raise GateError(
            "source run must be the completed failed publish.yml workflow_dispatch "
            "for the exact release tag and SHA"
        )

    jobs_payload = _gh_json(
        repo,
        REPOSITORY,
        f"{run_endpoint}/jobs?filter=all&per_page=100",
    )
    jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else None
    total_jobs = jobs_payload.get("total_count") if isinstance(jobs_payload, dict) else None
    if (
        not isinstance(jobs, list)
        or not isinstance(total_jobs, int)
        or isinstance(total_jobs, bool)
        or total_jobs != len(jobs)
        or len(jobs) > 100
    ):
        raise GateError("cannot completely inspect jobs from the source publication run")
    verify_jobs = [
        job
        for job in jobs
        if isinstance(job, dict)
        and job.get("name") == "verify-release"
        and isinstance(job.get("id"), int)
        and not isinstance(job.get("id"), bool)
        and job["id"] > 0
        and job.get("run_id") == run_id
        and job.get("head_sha") == sha
        and job.get("status") == "completed"
        and job.get("conclusion") == "success"
        and isinstance(job.get("run_attempt"), int)
        and not isinstance(job.get("run_attempt"), bool)
        and 0 < job["run_attempt"] <= attempt
    ]
    if len(verify_jobs) != 1:
        raise GateError(
            "source run must contain exactly one successful exact verify-release job"
        )
    verify_job = verify_jobs[0]
    artifact_attempt = verify_job.get("run_attempt")
    if not isinstance(artifact_attempt, int) or isinstance(artifact_attempt, bool):
        raise GateError("source verify-release attempt is malformed")
    source_inputs = _require_source_release_inputs(
        _gh_job_log(repo, verify_job["id"]),
        tag,
        sha,
        alias,
    )

    artifacts_payload = _gh_json(
        repo,
        REPOSITORY,
        f"{run_endpoint}/artifacts?per_page=100",
    )
    artifacts = (
        artifacts_payload.get("artifacts")
        if isinstance(artifacts_payload, dict)
        else None
    )
    total_artifacts = (
        artifacts_payload.get("total_count")
        if isinstance(artifacts_payload, dict)
        else None
    )
    if (
        not isinstance(total_artifacts, int)
        or isinstance(total_artifacts, bool)
        or total_artifacts != 2
        or not isinstance(artifacts, list)
        or len(artifacts) != 2
    ):
        raise GateError("source run must have exactly two retained release artifacts")
    expected_names = {
        f"python-dist-{tag}-{run_id}-{artifact_attempt}",
        f"release-assets-{tag}-{run_id}-{artifact_attempt}",
    }
    actual_names: set[str] = set()
    now = datetime.now(timezone.utc)
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise GateError("source artifact response is malformed")
        artifact_id = artifact.get("id")
        artifact_size = artifact.get("size_in_bytes")
        name = artifact.get("name")
        association = artifact.get("workflow_run")
        if (
            not isinstance(artifact_id, int)
            or isinstance(artifact_id, bool)
            or artifact_id <= 0
            or not isinstance(artifact_size, int)
            or isinstance(artifact_size, bool)
            or artifact_size <= 0
            or not isinstance(name, str)
            or name in actual_names
            or artifact.get("expired") is not False
            or ARTIFACT_DIGEST_RE.fullmatch(str(artifact.get("digest"))) is None
            or not isinstance(association, dict)
            or association.get("id") != run_id
            or association.get("head_branch") != tag
            or association.get("head_sha") != sha
        ):
            raise GateError("source artifact identity, digest, or run association is invalid")
        if _github_timestamp(artifact.get("expires_at"), "expires_at") <= now:
            raise GateError("source publication artifact has expired")
        actual_names.add(name)
    if actual_names != expected_names:
        raise GateError("source artifact names do not match the release tag, run, and attempt")
    return (
        f"failed publish run {run_id} attempt {attempt} reuses successful "
        f"verify-release attempt {artifact_attempt} and its two exact unexpired artifacts; "
        f"{source_inputs}"
    )


def _disposable_gate(repo: Path, remote: str, sha: str, tag: str) -> str:
    with tempfile.TemporaryDirectory(prefix="boundver-release-check-") as temporary:
        checkout = Path(temporary) / "checkout"
        source = _git(repo, "remote", "get-url", remote)
        _run(("git", "clone", "--quiet", source, str(checkout)), cwd=repo)
        _run(("git", "checkout", "--quiet", "--detach", sha), cwd=checkout)
        if _head(checkout) != sha:
            raise GateError("disposable checkout did not resolve the release SHA")
        env = os.environ.copy()
        env["GITHUB_REPOSITORY"] = REPOSITORY
        if not env.get("GH_TOKEN"):
            token = _run(
                ("gh", "auth", "token", "--hostname", "github.com"), cwd=repo
            ).stdout.strip()
            if not token:
                raise GateError("GitHub CLI did not return an authentication token")
            env["GH_TOKEN"] = token
        _run((sys.executable, "scripts/verify_release_readiness.py", "--tag", tag), cwd=checkout, env=env)
        _run(("bash", "scripts/audit_release_reviews.sh", sha, tag), cwd=checkout, env=env)
        tooling = Path(temporary) / "tooling"
        _run((sys.executable, "-m", "venv", str(tooling)), cwd=checkout, env=env)
        if os.name == "nt":
            tooling_python = tooling / "Scripts" / "python.exe"
            tooling_bin = tooling / "Scripts"
        else:
            tooling_python = tooling / "bin" / "python"
            tooling_bin = tooling / "bin"
        _run(
            (
                str(tooling_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-e",
                ".[dev]",
                "twine",
            ),
            cwd=checkout,
            env=env,
        )
        tool_env = env.copy()
        tool_env["PATH"] = str(tooling_bin) + os.pathsep + tool_env.get("PATH", "")
        _run((str(tooling_python), "-m", "pytest", "-q"), cwd=checkout, env=tool_env)
        _run(("bash", "scripts/packaging_smoke.sh"), cwd=checkout, env=tool_env)
        distributions = sorted((checkout / "dist").glob("*.whl")) + sorted(
            (checkout / "dist").glob("*.tar.gz")
        )
        if len(distributions) != 2:
            raise GateError("packaging smoke did not create exactly one wheel and sdist")
        _run(
            (str(tooling_python), "-m", "twine", "check", *(str(path) for path in distributions)),
            cwd=checkout,
            env=tool_env,
        )
        python_dist = checkout / "python-dist"
        python_dist.mkdir()
        for distribution in distributions:
            (python_dist / distribution.name).write_bytes(distribution.read_bytes())
        for api_base, origin in (
            ("https://test.pypi.org/pypi", "https://test-files.pythonhosted.org"),
            ("https://pypi.org/pypi", "https://files.pythonhosted.org"),
        ):
            preflight = _run(
                (
                    str(tooling_python),
                    "scripts/verify_testpypi_release.py",
                    "preflight",
                    "--dist",
                    "python-dist",
                    "--project",
                    "boundver",
                    "--version",
                    tag[1:],
                    "--api-base",
                    api_base,
                    "--download-origin",
                    origin,
                ),
                cwd=checkout,
                env=tool_env,
            )
            if "does not exist yet" not in preflight.stdout:
                registry = "TestPyPI" if "test.pypi.org" in api_base else "PyPI"
                raise GateError(
                    f"{registry} already has exact or partial files for {tag}; "
                    "resume the original workflow run instead of starting a new one"
                )
    return "readiness, reviews, tests, reproducible build, Twine, TestPyPI, and PyPI preflights passed"


def _surface_inventory(repo: Path) -> str:
    required = {
        "repository hygiene": ("scripts/check_repo_hygiene.py", ".gitignore", ".gitattributes"),
        "README and documentation": ("README.md", "docs/RELEASING.md"),
        "changelog and release notes": ("CHANGELOG.md", "scripts/release_changelog.py"),
        "schema URLs, configs, and locks": ("boundary.config.schema.json", "spec/boundary.lock.schema.json", "boundary.lock.json"),
        "CI and review state": (".github/workflows/ci.yml", "scripts/audit_release_reviews.sh"),
        "reproducible wheel, sdist, and standalone archive": ("scripts/packaging_smoke.sh", "scripts/build_release_artifacts.py"),
        "GitHub Action and Marketplace": ("action.yml", ".github/workflows/publish.yml"),
        "TestPyPI": ("scripts/verify_testpypi_release.py",),
        "PyPI": ("scripts/verify_testpypi_release.py",),
        "GitHub Release assets": ("scripts/verify_release_surfaces.py",),
        "compatibility alias": (".github/workflows/publish.yml",),
        "Docker": ("Dockerfile",),
        "pre-commit": (".pre-commit-hooks.yaml",),
    }
    missing = [
        f"{surface}: {path}"
        for surface, paths in required.items()
        for path in paths
        if not (repo / path).exists()
    ]
    if missing:
        raise GateError("missing release surface files: " + ", ".join(missing))
    publish_workflow = (repo / ".github/workflows/publish.yml").read_text(
        encoding="utf-8"
    )
    required_jobs = (
        "publish-testpypi",
        "verify-testpypi",
        "prepare-release-draft",
        "verify-marketplace",
        "publish-pypi",
        "verify-pypi",
        "advance-compatibility-alias",
        "verify-public-surfaces",
    )
    absent_jobs = [name for name in required_jobs if f"  {name}:" not in publish_workflow]
    if absent_jobs:
        raise GateError(
            "publication workflow is missing release phases: " + ", ".join(absent_jobs)
        )
    return "; ".join(SURFACES)


def _evaluate(repo: Path, remote: str, tag: str) -> tuple[str | None, list[Check]]:
    repo = repo.resolve()
    checks: list[Check] = []
    sha = _head(repo)
    _record(checks, "release surface inventory", lambda: _surface_inventory(repo))
    _record(checks, "repository identity", lambda: _repo_identity(repo, remote))
    _record(checks, "clean repository", lambda: _clean(repo))
    _record(checks, "repository hygiene", lambda: _repository_hygiene(repo))
    _record(checks, "project version", lambda: _project(repo, tag))
    local_ready = all(item.status == "passed" for item in checks)
    if sha is None:
        checks.append(Check("main identity", "failed", "HEAD is not a full commit SHA"))
    elif local_ready:
        _record(checks, "main identity", lambda: _main_identity(repo, remote, sha))
        _record(checks, "remote release state", lambda: _remote_release_state(repo, remote, tag))
        if all(item.status == "passed" for item in checks):
            _record(checks, "GitHub controls", lambda: _github_controls(repo, sha, tag))
        if all(item.status == "passed" for item in checks):
            _record(
                checks,
                "complete release gate",
                lambda: _disposable_gate(repo, remote, sha, tag),
            )
    return sha, checks


def _evaluate_resume(
    repo: Path,
    remote: str,
    tag: str,
    alias: str,
    run_id: int,
    release_sha: str,
) -> tuple[str | None, list[Check]]:
    repo = repo.resolve()
    checks: list[Check] = []
    control_sha = _head(repo)
    _record(checks, "release surface inventory", lambda: _surface_inventory(repo))
    _record(checks, "repository identity", lambda: _repo_identity(repo, remote))
    _record(checks, "clean repository", lambda: _clean(repo))
    _record(checks, "repository hygiene", lambda: _repository_hygiene(repo))
    _record(
        checks,
        "release project version",
        lambda: _project_at_commit(repo, release_sha, tag),
    )
    local_ready = all(item.status == "passed" for item in checks)
    if control_sha is None:
        checks.append(Check("main identity", "failed", "HEAD is not a full commit SHA"))
    elif local_ready:
        _record(
            checks,
            "main identity",
            lambda: _main_identity(repo, remote, control_sha),
        )
        _record(
            checks,
            "existing release tag",
            lambda: _resume_release_state(repo, remote, tag, release_sha),
        )
        _record(
            checks,
            "release commit ancestry",
            lambda: _release_is_on_main(repo, release_sha, control_sha),
        )
        if all(item.status == "passed" for item in checks):
            _record(
                checks,
                "GitHub controls",
                lambda: _github_controls(
                    repo, control_sha, tag, allow_resumable_release=True
                ),
            )
        if all(item.status == "passed" for item in checks):
            _record(
                checks,
                "source publication artifacts",
                lambda: _source_release_artifacts(
                    repo, run_id, tag, release_sha, alias
                ),
            )
    return control_sha, checks


def _emit(
    args: argparse.Namespace,
    sha: str | None,
    checks: list[Check],
    dispatch: dict[str, str] | None,
) -> int:
    ok = all(item.status == "passed" for item in checks)
    status = "failed"
    if ok:
        status = "dispatched" if args.command in {"start", "resume"} else "ready"
    payload = {
        "schema_version": 1,
        "phase": args.command,
        "tag": args.tag,
        "sha": sha,
        "status": status,
        "checks": [asdict(item) for item in checks],
        "dispatch": dispatch,
    }
    if args.format == "json":
        print(json.dumps(payload, sort_keys=True))
    else:
        for item in checks:
            marker = "PASS" if item.status == "passed" else "FAIL"
            print(f"[{marker}] {item.name}: {item.detail}")
        if dispatch:
            print(dispatch["detail"])
        print(f"Release {args.command} {'passed' if ok else 'failed'} for {args.tag}.")
    return 0 if ok else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check, start, or safely resume a gated boundver release."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "start", "resume"):
        child = subparsers.add_parser(command)
        child.add_argument("--tag", required=True, help="Exact vMAJOR.MINOR.PATCH tag")
        child.add_argument("--repo", type=Path, default=Path("."))
        child.add_argument("--remote", default="origin")
        child.add_argument("--format", choices=("text", "json"), default="text")
        if command in {"start", "resume"}:
            child.add_argument("--alias", required=True, help="Explicit vMAJOR.MINOR alias or none")
            confirmation_help = "Exact TAG@40-character-SHA confirmation"
            if command == "resume":
                child.add_argument(
                    "--run-id",
                    required=True,
                    help="Positive decimal ID of the failed original publish run",
                )
                confirmation_help += " followed by #RUNID"
            child.add_argument("--confirm", required=True, help=confirmation_help)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if TAG_RE.fullmatch(args.tag) is None:
        parser.error("--tag must be an exact vMAJOR.MINOR.PATCH release")
    confirmation_sha: str | None = None
    if args.command in {"start", "resume"}:
        expected_alias = args.tag.rsplit(".", 1)[0]
        if args.alias != "none" and (
            ALIAS_RE.fullmatch(args.alias) is None or args.alias != expected_alias
        ):
            parser.error(f"--alias must be {expected_alias} or none")
        if args.command == "start":
            confirmation = args.confirm.partition("@")
            if (
                confirmation[0] != args.tag
                or confirmation[1] != "@"
                or SHA_RE.fullmatch(confirmation[2]) is None
            ):
                parser.error("--confirm must be the exact TAG@lowercase-40-character-SHA")
            confirmation_sha = confirmation[2]
        else:
            if RUN_ID_RE.fullmatch(args.run_id) is None:
                parser.error("--run-id must be a positive decimal with no leading zero")
            args.run_id = int(args.run_id)
            match = re.fullmatch(
                rf"{re.escape(args.tag)}@(?P<sha>[0-9a-f]{{40}})#(?P<run_id>[1-9]\d*)",
                args.confirm,
            )
            if match is None or int(match.group("run_id")) != args.run_id:
                parser.error(
                    "--confirm must be the exact TAG@lowercase-40-character-SHA#RUNID"
                )
            confirmation_sha = match.group("sha")

    control_sha: str | None = None
    if args.command == "resume":
        assert confirmation_sha is not None
        control_sha, checks = _evaluate_resume(
            args.repo,
            args.remote,
            args.tag,
            args.alias,
            args.run_id,
            confirmation_sha,
        )
        sha = confirmation_sha
    else:
        sha, checks = _evaluate(args.repo, args.remote, args.tag)
    if args.command == "start" and sha != confirmation_sha:
        checks.append(Check("explicit confirmation", "failed", "confirmation SHA does not equal HEAD"))
    if any(item.status == "failed" for item in checks):
        return _emit(args, sha, checks, None)
    if args.command == "check":
        return _emit(args, sha, checks, None)

    assert sha is not None
    # Re-read remote main immediately before the command's only mutation.
    try:
        dispatch_control_sha = control_sha if args.command == "resume" else sha
        assert dispatch_control_sha is not None
        _main_identity(args.repo.resolve(), args.remote, dispatch_control_sha)
        workflow = "create-release-tag.yml"
        fields = (
            "--field", f"release_tag={args.tag}",
            "--field", f"release_sha={sha}",
            "--field", f"compatibility_alias={args.alias}",
        )
        if args.command == "resume":
            workflow = "publish.yml"
            fields += ("--field", f"resume_run_id={args.run_id}")
        command = (
            "gh", "workflow", "run", workflow,
            "--repo", REPOSITORY,
            "--ref", "main",
            *fields,
        )
        result = _run(command, cwd=args.repo.resolve())
    except GateError as error:
        checks.append(Check("workflow dispatch", "failed", str(error)))
        return _emit(args, sha, checks, None)
    detail = result.stdout.strip() or f"{workflow} dispatch accepted"
    checks.append(Check("workflow dispatch", "passed", detail))
    dispatch = {
        "workflow": workflow,
        "ref": "main",
        "tag": args.tag,
        "sha": sha,
        "alias": args.alias,
        "detail": detail,
    }
    if args.command == "resume":
        dispatch["resume_run_id"] = str(args.run_id)
    return _emit(args, sha, checks, dispatch)


if __name__ == "__main__":
    raise SystemExit(main())
