#!/usr/bin/env python3
"""Dispatch, apply, or verify a compatibility-alias update.

``advance`` is the sole mutation path.  It runs on the maintainer's trusted
host, revalidates the active publication, and performs one leased, monotonic
alias update.  ``dispatch`` then starts a read-only workflow from the immutable
release tag and waits for independent public-surface verification.  During
recovery that workflow loads the reviewed publication controls from current
``main`` without moving verification authority away from the tagged release.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import locale
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO, Mapping, Sequence


def _load_release_platform():
    """Load the exact adjacent helper even under isolated Python startup."""
    path = Path(__file__).resolve().with_name("_release_platform.py")
    spec = importlib.util.spec_from_file_location(
        "_boundver_alias_release_platform", path
    )
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise RuntimeError(f"cannot load release platform helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_release_platform = _load_release_platform()
sanitize_git_environment = _release_platform.sanitize_git_environment
sanitize_github_environment = _release_platform.sanitize_github_environment


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
VERIFY_CONTAINER_JOB = "Publish and verify the release container / verify-public"
ALIAS_DECISION_JOB = "Apply the explicit Action alias decision"
ACTIVE_RUN_STATES = {"requested", "pending", "queued", "in_progress", "waiting"}
JOB_LOG_ENV_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z {3}"
    r"(?P<name>RELEASE_TAG|RELEASE_SHA|COMPATIBILITY_ALIAS)(?P<rest>.*)$"
)
JOB_LOG_ENV_VALUE_RE = re.compile(r": (?P<value>\S+)")
MAX_JOB_LOG_BYTES = 32 * 1024 * 1024
MAX_COMMAND_STDOUT_BYTES = 8 * 1024 * 1024
MAX_COMMAND_STDERR_BYTES = 1024 * 1024
MAX_GITHUB_HELP_BYTES = 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_JSON_TOKENS = 500_000
MAX_JSON_DEPTH = 128
MAX_JSON_INTEGER_DIGITS = 20
MAX_JSON_NUMBER_CHARS = 128
MAX_GITHUB_ID = (1 << 64) - 1
MAX_POLL_ATTEMPTS = 300
MAX_POLL_DELAY_SECONDS = 60.0
MAX_POLL_WINDOW_SECONDS = 1_800.0
MAX_DIAGNOSTIC_CHARS = 4_096
COMMAND_TIMEOUT_SECONDS = 120
STREAM_CHUNK_BYTES = 64 * 1024


class AliasError(RuntimeError):
    """The requested alias operation is unsafe, ambiguous, or unreadable."""


def _trusted_tool(name: str, repo: Path) -> str:
    """Resolve a host tool and reject repository-local executable shadowing."""
    raw = shutil.which(name)
    if raw is None:
        raise AliasError(f"required command is unavailable: {name}")
    try:
        repo_root = repo.resolve(strict=True)
        selected = Path(os.path.abspath(raw))
        selected.relative_to(repo_root)
    except ValueError:
        pass
    except OSError as error:
        raise AliasError(f"cannot resolve required command: {name}") from error
    else:
        raise AliasError(f"required command resolves inside the repository: {name}")
    try:
        path = selected.resolve(strict=True)
        path.relative_to(repo_root)
    except ValueError:
        pass
    except OSError as error:
        raise AliasError(f"cannot resolve required command: {name}") from error
    else:
        raise AliasError(f"required command resolves inside the repository: {name}")
    try:
        metadata = path.stat()
    except OSError as error:
        raise AliasError(f"cannot inspect required command: {name}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise AliasError(f"required command is not a regular file: {name}")
    return str(path)


def _shell_single_quote(value: str) -> str:
    """Quote one trusted executable path for Git's POSIX-style helper shell."""
    if not value or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise AliasError("trusted credential-helper path is malformed")
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _git_credential_helper(repo: Path) -> str:
    """Bind Git authentication to the already trusted public-GitHub CLI."""
    executable = Path(_trusted_tool("gh", repo)).as_posix()
    return f"!{_shell_single_quote(executable)} auth git-credential"


def _git_environment(
    environment: Mapping[str, str] | None = None,
    *,
    credential_helper: str | None = None,
) -> dict[str, str]:
    """Disable ambient Git execution, transport, and credential configuration."""
    result = sanitize_git_environment(environment)
    process_config = [
        ("core.hooksPath", os.devnull),
        ("core.fsmonitor", "false"),
    ]
    if credential_helper is not None:
        # An empty helper resets any repository-local helper chain before the
        # trusted absolute gh command is added. System/global config is also
        # disabled, so no host-ambient helper or URL rewrite participates.
        process_config.extend(
            (
                ("credential.https://github.com.helper", ""),
                ("credential.https://github.com.helper", credential_helper),
            )
        )
    result.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": str(len(process_config)),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
        }
    )
    for index, (key, value) in enumerate(process_config):
        result[f"GIT_CONFIG_KEY_{index}"] = key
        result[f"GIT_CONFIG_VALUE_{index}"] = value
    return result


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except OSError:
        pass


def _read_bounded_pipe(
    process: subprocess.Popen[bytes],
    pipe: BinaryIO,
    label: str,
    limit: int,
    destination: bytearray,
    overflows: list[str],
    errors: list[str],
) -> None:
    try:
        while True:
            remaining_with_sentinel = max(1, limit - len(destination) + 1)
            reader = getattr(pipe, "read1", pipe.read)
            chunk = reader(min(STREAM_CHUNK_BYTES, remaining_with_sentinel))
            if not chunk:
                return
            if len(destination) + len(chunk) > limit:
                overflows.append(label)
                _kill_process(process)
                return
            destination.extend(chunk)
    except (OSError, ValueError) as error:
        errors.append(f"{label}: {error}")
        _kill_process(process)
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _run_bytes(
    command: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    max_stdout_bytes: int = MAX_COMMAND_STDOUT_BYTES,
    max_stderr_bytes: int = MAX_COMMAND_STDERR_BYTES,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    if (
        not command
        or max_stdout_bytes < 0
        or max_stderr_bytes < 0
        or timeout <= 0
    ):
        raise ValueError("invalid bounded subprocess arguments")
    argv = [str(item) for item in command]
    process_environment = env
    if argv[0] in {"git", "gh"}:
        original_name = argv[0]
        argv[0] = _trusted_tool(original_name, cwd)
        if original_name == "gh":
            process_environment = sanitize_github_environment(
                os.environ if env is None else env
            )
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=None if process_environment is None else dict(process_environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise AliasError(f"cannot execute required command: {command[0]}") from error
    if process.stdout is None or process.stderr is None:
        _kill_process(process)
        raise AliasError("required command pipes are unavailable")
    stdout = bytearray()
    stderr = bytearray()
    overflows: list[str] = []
    errors: list[str] = []
    readers = (
        threading.Thread(
            target=_read_bounded_pipe,
            args=(
                process,
                process.stdout,
                "stdout",
                max_stdout_bytes,
                stdout,
                overflows,
                errors,
            ),
            daemon=True,
        ),
        threading.Thread(
            target=_read_bounded_pipe,
            args=(
                process,
                process.stderr,
                "stderr",
                max_stderr_bytes,
                stderr,
                overflows,
                errors,
            ),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _kill_process(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise AliasError(f"required command timed out: {command[0]}") from error
    finally:
        for reader in readers:
            reader.join(timeout=5)
    if any(reader.is_alive() for reader in readers):
        _kill_process(process)
        raise AliasError("required command output pipes did not close")
    if errors:
        raise AliasError("required command output could not be read")
    if overflows:
        label = "stdout" if "stdout" in overflows else "stderr"
        limit = max_stdout_bytes if label == "stdout" else max_stderr_bytes
        raise AliasError(
            f"{command[0]} {label} exceeds the {limit}-byte limit"
        )
    result = subprocess.CompletedProcess(argv, returncode, bytes(stdout), bytes(stderr))
    if check and returncode != 0:
        diagnostic = result.stderr or result.stdout or b"command failed"
        detail = diagnostic.decode("utf-8", "backslashreplace").strip()
        if len(detail) > MAX_DIAGNOSTIC_CHARS:
            detail = detail[:MAX_DIAGNOSTIC_CHARS] + "..."
        raise AliasError(f"{' '.join(command)}: {detail}")
    return result


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    max_stdout_bytes: int = MAX_COMMAND_STDOUT_BYTES,
    max_stderr_bytes: int = MAX_COMMAND_STDERR_BYTES,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    raw = _run_bytes(
        command,
        cwd=cwd,
        check=check,
        env=env,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
        timeout=timeout,
    )
    encoding = locale.getpreferredencoding(False)
    return subprocess.CompletedProcess(
        raw.args,
        raw.returncode,
        raw.stdout.decode(encoding, "backslashreplace"),
        raw.stderr.decode(encoding, "backslashreplace"),
    )


def _git_result(
    repo: Path,
    arguments: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    credential_helper = (
        _git_credential_helper(repo)
        if arguments and arguments[0] in {"fetch", "ls-remote", "push"}
        else None
    )
    return _run(
        ("git", *arguments),
        cwd=repo,
        check=check,
        env=_git_environment(credential_helper=credential_helper),
    )


def _git(repo: Path, *arguments: str) -> str:
    return _git_result(repo, arguments).stdout.strip()


def _json_shape_within_limits(document: str) -> bool:
    """Reject provably wide or deep JSON before the decoder allocates it."""
    tokens = 0
    depth = 0
    in_string = False
    escaped = False
    in_atom = False
    for character in document:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            tokens += 1
            in_string = True
            in_atom = False
        elif character in "[{":
            tokens += 1
            depth += 1
            in_atom = False
            if depth > MAX_JSON_DEPTH:
                return False
        elif character in "]}":
            depth = max(0, depth - 1)
            in_atom = False
        elif character in " \t\r\n,:":
            in_atom = False
        elif not in_atom:
            tokens += 1
            in_atom = True
        if tokens > MAX_JSON_TOKENS:
            return False
    return True


def _bounded_json_int(value: str) -> int:
    if len(value.lstrip("-")) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError("oversized JSON integer")
    return int(value)


def _bounded_json_float(value: str) -> float:
    if len(value) > MAX_JSON_NUMBER_CHARS:
        raise ValueError("oversized JSON number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite JSON constant")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _strict_json_loads(document: str) -> object:
    if len(document.encode("utf-8", "surrogatepass")) > MAX_JSON_BYTES:
        raise ValueError(f"JSON exceeds the {MAX_JSON_BYTES}-byte limit")
    if not _json_shape_within_limits(document):
        raise ValueError("JSON exceeds the structural limit")
    return json.loads(
        document,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
        parse_float=_bounded_json_float,
        parse_int=_bounded_json_int,
    )


def _gh_json(repo: Path, endpoint: str) -> object:
    result = _run_bytes(
        ("gh", "api", "--hostname", "github.com", endpoint),
        cwd=repo,
        max_stdout_bytes=MAX_JSON_BYTES,
    )
    try:
        document = result.stdout.decode("utf-8", "strict")
        return _strict_json_loads(document)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise AliasError(f"GitHub API returned invalid JSON for {endpoint}") from error


def _gh_job_log(repo: Path, repository: str, job_id: int) -> str:
    _positive_int(job_id, "originating verify-release job ID")
    endpoint = f"repos/{repository}/actions/jobs/{job_id}/logs"
    help_result = _run_bytes(
        ("gh", "api", "--help"),
        cwd=repo,
        check=False,
        max_stdout_bytes=MAX_GITHUB_HELP_BYTES,
        max_stderr_bytes=MAX_GITHUB_HELP_BYTES,
    )
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
    command.extend(("--hostname", "github.com", endpoint))
    result = _run_bytes(
        command,
        cwd=repo,
        check=False,
        max_stdout_bytes=MAX_JOB_LOG_BYTES,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")
        raise AliasError(
            f"GitHub API failed for {endpoint}: "
            f"{detail.strip() or 'unknown error'}"
        )
    if not result.stdout:
        raise AliasError("originating verify-release job log is empty")
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
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 < value <= MAX_GITHUB_ID
    ):
        raise AliasError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AliasError(f"{label} must be a nonnegative integer")
    return value


def _parse_positive_int(value: str, label: str) -> int:
    if re.fullmatch(r"[1-9]\d{0,19}", value) is None:
        raise AliasError(f"{label} must be a positive decimal integer")
    parsed = int(value)
    if parsed > MAX_GITHUB_ID:
        raise AliasError(f"{label} exceeds the supported range")
    return parsed


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


def _validate_release_remote(repo: Path, repository: str, remote: str) -> None:
    """Allow only the canonical HTTPS URL or its verified ``origin`` alias."""
    canonical_urls = {
        f"https://github.com/{repository}",
        f"https://github.com/{repository}.git",
    }
    if remote in canonical_urls:
        return
    if remote != "origin":
        raise AliasError(
            "release remote must be origin or the canonical public GitHub HTTPS URL"
        )
    configured = _git(repo, "remote", "get-url", "origin")
    accepted_origin_urls = canonical_urls | {
        f"git@github.com:{repository}",
        f"git@github.com:{repository}.git",
        f"ssh://git@github.com/{repository}",
        f"ssh://git@github.com/{repository}.git",
    }
    if configured not in accepted_origin_urls:
        raise AliasError("origin does not identify the canonical GitHub repository")


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


def _release_alias_workflow_available(repo: Path) -> bool:
    """Return whether the immutable checkout contains a regular child workflow."""
    workflow = repo / WORKFLOW_PATH
    try:
        metadata = workflow.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise AliasError(f"cannot inspect {WORKFLOW_PATH}: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise AliasError(
            f"immutable release has no regular {WORKFLOW_PATH} workflow"
        )
    return True


def _require_release_alias_workflow(repo: Path, tag: str) -> None:
    """Require the immutable checkout to contain the read-only child workflow."""
    if not _release_alias_workflow_available(repo):
        raise AliasError(
            f"immutable release {tag} predates the exact-tag alias verification "
            "workflow; recovery cannot independently verify its compatibility "
            "alias handoff"
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
    _validate_release_remote(repo, repository, remote)
    _positive_int(publication_run_id, "publication run ID")
    _positive_int(publication_attempt, "publication attempt")
    if SHA_RE.fullmatch(publication_sha) is None:
        raise AliasError("publication SHA must be 40 lowercase hexadecimal characters")
    _validate_publication_control(
        publication_ref, publication_sha, tag=tag, release_sha=sha
    )
    if (
        type(attempts) is not int
        or not 0 < attempts <= MAX_POLL_ATTEMPTS
        or type(delay_seconds) not in {int, float}
        or isinstance(delay_seconds, bool)
        or not math.isfinite(delay_seconds)
        or not 0 <= delay_seconds <= MAX_POLL_DELAY_SECONDS
        or attempts * delay_seconds > MAX_POLL_WINDOW_SECONDS
    ):
        raise AliasError("polling policy exceeds its bounded time or attempt limits")

    tag_ref = f"refs/tags/{tag}"
    alias_ref = f"refs/tags/{alias}"
    if _remote_ref(repo, remote, tag_ref) != sha:
        raise AliasError("exact release tag moved or disappeared before alias dispatch")
    if _remote_ref(repo, remote, alias_ref) != sha:
        raise AliasError(
            f"{alias} must already resolve to {sha}; run the local publishing "
            "script's alias phase before approving the action-alias environment"
        )
    if not _release_alias_workflow_available(repo):
        return (
            f"{alias} already resolves to {sha}; immutable release {tag} predates "
            "the independent alias verification workflow"
        )

    title = _run_title(alias, tag, publication_run_id, publication_attempt)
    dispatched = False
    dispatch_error = ""
    for poll in range(attempts):
        run = _find_alias_run(
            repo,
            repository,
            title=title,
            control_ref=tag,
            control_sha=sha,
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
                    tag,
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
        or run.get("status") not in ACTIVE_RUN_STATES
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
        VERIFY_CONTAINER_JOB: [],
    }
    successful_attempts: dict[str, set[int]] = {
        VERIFY_PYPI_JOB: set(),
        VERIFY_RELEASE_JOB: set(),
        VERIFY_CONTAINER_JOB: set(),
    }
    active_alias_jobs: list[dict] = []
    for job in jobs["jobs"]:
        if not isinstance(job, dict):
            raise AliasError("originating publication job entry is malformed")
        name = job.get("name")
        if name not in {
            VERIFY_PYPI_JOB,
            VERIFY_RELEASE_JOB,
            VERIFY_CONTAINER_JOB,
            ALIAS_DECISION_JOB,
        }:
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
        if name == ALIAS_DECISION_JOB:
            if (
                job_attempt == publication_attempt
                and job.get("status") in ACTIVE_RUN_STATES
                and job.get("conclusion") is None
            ):
                active_alias_jobs.append({**job, "id": job_id})
            continue
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
    container_matches = successful_jobs[VERIFY_CONTAINER_JOB]
    if not pypi_matches:
        raise AliasError(
            "originating publication must contain a successful PyPI verification job"
        )
    if not release_matches:
        raise AliasError(
            "originating publication must contain a successful verify-release job"
        )
    if not container_matches:
        raise AliasError(
            "originating publication must contain a successful public-container "
            "verification job"
        )
    if len(active_alias_jobs) != 1:
        raise AliasError(
            "originating publication must contain exactly one active alias-decision "
            "job in the current attempt"
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
    _validate_release_remote(repo, repository, remote)
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
        ancestry = _git_result(
            repo,
            ("merge-base", "--is-ancestor", expected_current, sha),
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

    # Push the reviewed object ID directly so this operation leaves no mutable
    # local tag behind.  The lease rejects a concurrent creation or update.
    _git(repo, "push", lease, remote, f"{sha}:{alias_ref}")
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
    _validate_release_remote(repo, repository, remote)
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


def require_advanced_alias(
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
    """Require the locally advanced alias after revalidating its active parent."""
    verify_alias_request(
        repo=repo,
        repository=repository,
        remote=remote,
        tag=tag,
        sha=sha,
        alias=alias,
        publication_run_id=publication_run_id,
        publication_attempt=publication_attempt,
        publication_ref=publication_ref,
        publication_sha=publication_sha,
    )
    if _remote_ref(repo, remote, f"refs/tags/{alias}") != sha:
        raise AliasError(f"compatibility alias {alias} does not resolve to {sha}")
    return f"{alias} resolves exactly to {sha}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dispatch, apply, or verify a compatibility-alias update."
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
    require = subparsers.add_parser("require")
    common(require)
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
        elif args.command == "advance":
            result = advance_alias(**arguments)
        else:
            result = require_advanced_alias(**arguments)
    except (AliasError, OSError, ValueError) as error:
        print(f"Compatibility alias error: {error}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
