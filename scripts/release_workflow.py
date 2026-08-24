#!/usr/bin/env python3
"""Testable release-workflow primitives kept out of GitHub Actions YAML.

These helpers validate retained recovery artifacts, bind them to the original
publication policy, and compare downloaded archive bytes with the extraction
performed by ``actions/download-artifact``. The command-line adapter performs
bounded read-only GitHub API calls; it performs no publication mutations.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence


TAG_RE = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
POSITIVE_INT_RE = re.compile(r"[1-9]\d*")
JOB_LOG_ENV_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z {3}"
    r"(?P<name>RELEASE_TAG|RELEASE_SHA|COMPATIBILITY_ALIAS)(?P<rest>.*)$"
)
JOB_LOG_ENV_VALUE_RE = re.compile(r": (?P<value>\S+)")

MAX_GITHUB_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_GITHUB_TOTAL_BYTES = 16 * 1024 * 1024
MAX_GITHUB_HELP_BYTES = 1024 * 1024
MAX_GITHUB_STDERR_BYTES = 64 * 1024
MAX_JOB_LOG_BYTES = 32 * 1024 * 1024
MAX_JOB_LOG_LINES = 500_000
MAX_POLICY_TRIPLES = 10_000
MAX_JSON_INTEGER_DIGITS = 20
MAX_GITHUB_ID = (1 << 64) - 1
MAX_ARTIFACT_NAME_BYTES = 1024
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 16
MAX_ARCHIVE_METADATA_BYTES = 1024 * 1024
MAX_ARCHIVE_PATH_BYTES = 1024
MAX_ARCHIVE_MEMBER_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 256 * 1024 * 1024
MAX_CHECKSUM_BYTES = 4096
MAX_PROBE_BYTES = 32 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024


class ReleaseWorkflowError(ValueError):
    """A workflow payload is malformed, incomplete, stale, or conflicting."""


def _bounded_json_int(value: str) -> int:
    if len(value.lstrip("-")) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError("oversized JSON integer")
    return int(value)


def _bounded_json_float(value: str) -> float:
    if len(value) > 128:
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
    return json.loads(
        document,
        object_pairs_hook=_unique_json_object,
        parse_int=_bounded_json_int,
        parse_float=_bounded_json_float,
        parse_constant=_reject_json_constant,
    )


def _read_bounded_file(path: Path, limit: int, label: str) -> bytes:
    """Read one stable regular file without trusting its advertised size."""
    try:
        initial = path.lstat()
        if not stat.S_ISREG(initial.st_mode) or not 0 <= initial.st_size <= limit:
            raise ReleaseWorkflowError(f"{label} exceeds the byte limit")
        chunks: list[bytes] = []
        total = 0
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (initial.st_dev, initial.st_ino) != (opened.st_dev, opened.st_ino)
                or not 0 <= opened.st_size <= limit
            ):
                raise ReleaseWorkflowError(f"{label} exceeds the byte limit")
            while total < limit:
                requested = min(READ_CHUNK_BYTES, limit - total)
                chunk = stream.read(requested)
                if not chunk:
                    break
                if len(chunk) > requested:
                    raise ReleaseWorkflowError(f"{label} exceeded a bounded read")
                chunks.append(chunk)
                total += len(chunk)
            if stream.read(1):
                raise ReleaseWorkflowError(f"{label} exceeds the byte limit")
            finished = os.fstat(stream.fileno())
        current = path.lstat()
    except ReleaseWorkflowError:
        raise
    except OSError as error:
        raise ReleaseWorkflowError(f"cannot read {label}: {error}") from error
    if (
        not stat.S_ISREG(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        or opened.st_size != finished.st_size
        or opened.st_mtime_ns != finished.st_mtime_ns
        or finished.st_size != total
        or current.st_size != finished.st_size
        or current.st_mtime_ns != finished.st_mtime_ns
    ):
        raise ReleaseWorkflowError(f"{label} changed while being read")
    return b"".join(chunks)


def _read_bounded_text(path: Path, limit: int, label: str) -> str:
    try:
        return _read_bounded_file(path, limit, label).decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise ReleaseWorkflowError(f"{label} is not UTF-8") from error


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except OSError:
        pass


def _read_bounded_pipe(
    process: subprocess.Popen[bytes],
    pipe,
    label: str,
    limit: int,
    buffer: bytearray,
    overflows: list[str],
    errors: list[tuple[str, str]],
) -> None:
    try:
        while True:
            remaining_with_sentinel = max(1, limit - len(buffer) + 1)
            reader = getattr(pipe, "read1", pipe.read)
            chunk = reader(min(READ_CHUNK_BYTES, remaining_with_sentinel))
            if not chunk:
                return
            if len(buffer) + len(chunk) > limit:
                overflows.append(label)
                _kill_process(process)
                return
            buffer.extend(chunk)
    except (OSError, ValueError) as error:
        errors.append((label, str(error)))
        _kill_process(process)
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _run_bounded(command: Sequence[str], stdout_limit: int) -> bytes:
    try:
        process = subprocess.Popen(
            [str(item) for item in command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise ReleaseWorkflowError("cannot execute trusted GitHub CLI") from error
    if process.stdout is None or process.stderr is None:
        _kill_process(process)
        raise ReleaseWorkflowError("GitHub CLI command pipes are unavailable")
    stdout = bytearray()
    stderr = bytearray()
    overflows: list[str] = []
    errors: list[tuple[str, str]] = []
    readers = [
        threading.Thread(
            target=_read_bounded_pipe,
            args=(process, process.stdout, "stdout", stdout_limit, stdout, overflows, errors),
            daemon=True,
        ),
        threading.Thread(
            target=_read_bounded_pipe,
            args=(
                process,
                process.stderr,
                "stderr",
                MAX_GITHUB_STDERR_BYTES,
                stderr,
                overflows,
                errors,
            ),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=90)
    except subprocess.TimeoutExpired as error:
        _kill_process(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise ReleaseWorkflowError("GitHub CLI command timed out") from error
    finally:
        for reader in readers:
            reader.join(timeout=5)
    if any(reader.is_alive() for reader in readers):
        _kill_process(process)
        raise ReleaseWorkflowError("GitHub CLI command pipes did not close")
    if errors:
        raise ReleaseWorkflowError("GitHub CLI response read failed")
    if overflows:
        raise ReleaseWorkflowError("GitHub CLI response exceeds the byte limit")
    if returncode != 0:
        detail = stderr.decode("utf-8", "backslashreplace").strip()
        raise ReleaseWorkflowError(f"GitHub CLI request failed: {detail}")
    return bytes(stdout)


def _trusted_gh() -> Path:
    workspace_value = os.environ.get("GITHUB_WORKSPACE")
    if not workspace_value:
        raise ReleaseWorkflowError("GITHUB_WORKSPACE is unavailable")
    workspace = Path(workspace_value).resolve()
    executable = shutil.which("gh")
    if executable is None:
        raise ReleaseWorkflowError("trusted GitHub CLI is unavailable")
    gh = Path(executable).resolve()
    if not gh.is_file() or gh == workspace or workspace in gh.parents:
        raise ReleaseWorkflowError("trusted GitHub CLI is unavailable")
    return gh


def _decode_strict_json(payload: bytes, label: str) -> object:
    try:
        document = payload.decode("utf-8", "strict")
        return _strict_json_loads(document)
    except (UnicodeDecodeError, ValueError) as error:
        raise ReleaseWorkflowError(f"{label} returned malformed JSON") from error


def fetch_recovery_payloads(repository: str, run_id: int) -> tuple[object, object, object]:
    """Read the three recovery API payloads through a bounded trusted CLI."""
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise ReleaseWorkflowError("repository name is malformed")
    _positive_int(run_id, "source publication run ID")
    gh = _trusted_gh()
    endpoint = f"repos/{repository}/actions/runs/{run_id}"
    consumed = 0
    payloads: list[object] = []
    for suffix, label, limit in (
        ("", "source publication run API", 1024 * 1024),
        ("/jobs?filter=all&per_page=100", "source publication jobs API", MAX_GITHUB_RESPONSE_BYTES),
        ("/artifacts?per_page=100", "source publication artifacts API", MAX_GITHUB_RESPONSE_BYTES),
    ):
        remaining = MAX_GITHUB_TOTAL_BYTES - consumed
        if remaining < 1:
            raise ReleaseWorkflowError("recovery API responses exceed the aggregate limit")
        response = _run_bounded([str(gh), "api", endpoint + suffix], min(limit, remaining))
        consumed += len(response)
        payloads.append(_decode_strict_json(response, label))
    return payloads[0], payloads[1], payloads[2]


def fetch_verification_job_log(repository: str, job_id: int) -> str:
    """Read one source verification log through a bounded trusted CLI."""
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise ReleaseWorkflowError("repository name is malformed")
    _positive_int(job_id, "source verification job ID")
    gh = _trusted_gh()
    help_output = _run_bounded([str(gh), "api", "--help"], MAX_GITHUB_HELP_BYTES)
    command = [str(gh), "api"]
    if b"--allow-escape-sequences" in help_output:
        command.append("--allow-escape-sequences")
    command.append(f"repos/{repository}/actions/jobs/{job_id}/logs")
    payload = _run_bounded(command, MAX_JOB_LOG_BYTES)
    if not payload:
        raise ReleaseWorkflowError("source verification job log is empty")
    try:
        return payload.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise ReleaseWorkflowError("source verification job log is not UTF-8") from error


@dataclass(frozen=True)
class RecoverySelection:
    source_run_id: int
    source_run_attempt: int
    verification_job_id: int
    artifact_attempt: int
    python_dist_artifact_id: int
    python_dist_artifact_digest: str
    release_assets_artifact_id: int
    release_assets_artifact_digest: str
    release_note_artifact_count: int

    def outputs(self) -> dict[str, str]:
        return {
            "source-run-id": str(self.source_run_id),
            "verification-job-id": str(self.verification_job_id),
            "python-dist-artifact-id": str(self.python_dist_artifact_id),
            "python-dist-artifact-digest": self.python_dist_artifact_digest,
            "release-assets-artifact-id": str(self.release_assets_artifact_id),
            "release-assets-artifact-digest": self.release_assets_artifact_digest,
        }


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_GITHUB_ID:
        raise ReleaseWorkflowError(f"{label} must be a positive integer")
    return value


def _positive_int_text(value: str, label: str) -> int:
    if POSITIVE_INT_RE.fullmatch(value) is None or len(value) > 20:
        raise ReleaseWorkflowError(f"{label} must be a positive decimal integer")
    parsed = int(value)
    if parsed > MAX_GITHUB_ID:
        raise ReleaseWorkflowError(f"{label} exceeds the supported range")
    return parsed


def _digest(value: str, label: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value):
        value = f"sha256:{value}"
    if DIGEST_RE.fullmatch(value) is None:
        raise ReleaseWorkflowError(f"{label} must be a SHA-256 digest")
    return value


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ReleaseWorkflowError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseWorkflowError(f"{label} is invalid") from error
    if parsed.tzinfo is None:
        raise ReleaseWorkflowError(f"{label} is invalid")
    return parsed.astimezone(timezone.utc)


def _require_identity(tag: str, sha: str) -> None:
    if TAG_RE.fullmatch(tag) is None:
        raise ReleaseWorkflowError(f"invalid release tag: {tag!r}")
    if SHA_RE.fullmatch(sha) is None:
        raise ReleaseWorkflowError("release SHA must be 40 lowercase hexadecimal characters")


def select_recovery_artifacts(
    run: object,
    jobs_payload: object,
    artifacts_payload: object,
    *,
    repository: str,
    run_id: int,
    tag: str,
    sha: str,
    now: datetime | None = None,
) -> RecoverySelection:
    """Select the sole verified retained artifact pair from a failed run."""
    _require_identity(tag, sha)
    _positive_int(run_id, "source publication run ID")
    if not isinstance(run, dict):
        raise ReleaseWorkflowError("source publication run response is malformed")
    attempt = _positive_int(run.get("run_attempt"), "source publication run attempt")
    run_repository = run.get("repository")
    expected_run = {
        "id": run_id,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "failure",
        "path": ".github/workflows/publish.yml",
        "head_branch": tag,
        "head_sha": sha,
    }
    disagreements = [
        key for key, expected in expected_run.items() if run.get(key) != expected
    ]
    if disagreements:
        raise ReleaseWorkflowError(
            "source publication run does not match the exact release tag and SHA: "
            + ", ".join(disagreements)
        )
    if (
        not isinstance(run_repository, dict)
        or run_repository.get("full_name") != repository
    ):
        raise ReleaseWorkflowError("source publication repository disagrees")

    if not isinstance(jobs_payload, dict):
        raise ReleaseWorkflowError("source publication jobs response is malformed")
    jobs = jobs_payload.get("jobs")
    total_jobs = jobs_payload.get("total_count")
    if (
        not isinstance(jobs, list)
        or type(total_jobs) is not int
        or total_jobs != len(jobs)
        or len(jobs) > 100
        or any(not isinstance(job, dict) for job in jobs)
    ):
        raise ReleaseWorkflowError(
            "cannot completely inspect the source publication jobs response"
        )
    successful_verify_jobs = [
        job
        for job in jobs
        if job.get("name") == "verify-release"
        and type(job.get("id")) is int
        and 0 < job["id"] <= MAX_GITHUB_ID
        and type(job.get("run_id")) is int
        and job.get("run_id") == run_id
        and job.get("head_sha") == sha
        and job.get("status") == "completed"
        and job.get("conclusion") == "success"
        and type(job.get("run_attempt")) is int
        and 0 < job["run_attempt"] <= attempt
    ]

    if not isinstance(artifacts_payload, dict):
        raise ReleaseWorkflowError("source publication artifacts response is malformed")
    artifacts = artifacts_payload.get("artifacts")
    total_artifacts = artifacts_payload.get("total_count")
    if (
        type(total_artifacts) is not int
        or not isinstance(artifacts, list)
        or total_artifacts != len(artifacts)
        or not 2 <= total_artifacts <= 100
        or any(not isinstance(artifact, dict) for artifact in artifacts)
    ):
        raise ReleaseWorkflowError(
            "source run artifact response is incomplete or exceeds the inspection limit"
        )

    artifact_name_res = {
        "python-dist": re.compile(
            rf"python-dist-{re.escape(tag)}-{run_id}-([1-9][0-9]*)"
        ),
        "release-assets": re.compile(
            rf"release-assets-{re.escape(tag)}-{run_id}-([1-9][0-9]*)"
        ),
    }
    release_notes_re = re.compile(
        rf"release-notes-{re.escape(sha)}-{run_id}-([1-9][0-9]*)"
    )
    actual_names: set[str] = set()
    artifact_attempts = {label: set() for label in artifact_name_res}
    retained: dict[tuple[str, int], tuple[int, str]] = {}
    release_note_count = 0
    current_time = now or datetime.now(timezone.utc)
    for artifact in artifacts:
        artifact_id = artifact.get("id")
        artifact_size = artifact.get("size_in_bytes")
        artifact_digest = artifact.get("digest")
        name = artifact.get("name")
        association = artifact.get("workflow_run")
        try:
            name_size = len(name.encode("utf-8")) if isinstance(name, str) else -1
        except UnicodeEncodeError:
            name_size = -1
        if (
            type(artifact_id) is not int
            or not 0 < artifact_id <= MAX_GITHUB_ID
            or type(artifact_size) is not int
            or not 0 < artifact_size <= MAX_ARTIFACT_BYTES
            or not isinstance(name, str)
            or not 0 <= name_size <= MAX_ARTIFACT_NAME_BYTES
            or name in actual_names
            or artifact.get("expired") is not False
            or not isinstance(artifact_digest, str)
            or DIGEST_RE.fullmatch(artifact_digest) is None
            or not isinstance(association, dict)
            or type(association.get("id")) is not int
            or association.get("id") != run_id
            or association.get("head_branch") != tag
            or association.get("head_sha") != sha
        ):
            raise ReleaseWorkflowError(
                "source artifact identity, digest, or run association is invalid"
            )
        if _timestamp(artifact.get("expires_at"), "source artifact expiry") <= current_time:
            raise ReleaseWorkflowError("source publication artifact has expired")

        matched_artifact = False
        for label, name_re in artifact_name_res.items():
            match = name_re.fullmatch(name)
            if match is None:
                continue
            artifact_attempt = int(match.group(1))
            if artifact_attempt > attempt:
                raise ReleaseWorkflowError(
                    "source artifact names do not match the release tag, run, and attempt"
                )
            artifact_attempts[label].add(artifact_attempt)
            retained[(label, artifact_attempt)] = (artifact_id, artifact_digest)
            matched_artifact = True
            break
        if not matched_artifact:
            match = release_notes_re.fullmatch(name)
            if match is None or int(match.group(1)) > attempt:
                raise ReleaseWorkflowError(
                    "source artifact names do not match the release tag, run, and attempt"
                )
            release_note_count += 1
        actual_names.add(name)

    retained_attempts = set.union(*artifact_attempts.values())
    if (
        len(retained_attempts) != 1
        or any(attempts != retained_attempts for attempts in artifact_attempts.values())
    ):
        raise ReleaseWorkflowError(
            "source artifact names do not match the release tag, run, and attempt"
        )
    artifact_attempt = retained_attempts.pop()
    verify_jobs = [
        job
        for job in successful_verify_jobs
        if job.get("run_attempt") == artifact_attempt
    ]
    if len(verify_jobs) != 1:
        raise ReleaseWorkflowError(
            "source run must contain exactly one successful exact verify-release job "
            "for the retained artifact attempt"
        )
    verification = verify_jobs[0]
    python_id, python_digest = retained[("python-dist", artifact_attempt)]
    release_id, release_digest = retained[("release-assets", artifact_attempt)]
    return RecoverySelection(
        source_run_id=run_id,
        source_run_attempt=attempt,
        verification_job_id=verification["id"],
        artifact_attempt=artifact_attempt,
        python_dist_artifact_id=python_id,
        python_dist_artifact_digest=python_digest,
        release_assets_artifact_id=release_id,
        release_assets_artifact_digest=release_digest,
        release_note_artifact_count=release_note_count,
    )


def require_release_input_evidence(
    job_log: str, *, tag: str, sha: str, alias: str
) -> str:
    """Require complete, exact release-policy triples in a verify job log."""
    try:
        if len(job_log.encode("utf-8")) > MAX_JOB_LOG_BYTES:
            raise ReleaseWorkflowError("source verify-release log exceeds the byte limit")
    except UnicodeEncodeError as error:
        raise ReleaseWorkflowError("source verify-release log is not valid UTF-8") from error
    expected = [
        ("RELEASE_TAG", tag),
        ("RELEASE_SHA", sha),
        ("COMPATIBILITY_ALIAS", alias),
    ]
    observed: list[tuple[str, str]] = []
    for line_number, line in enumerate(io.StringIO(job_log), start=1):
        if line_number > MAX_JOB_LOG_LINES:
            raise ReleaseWorkflowError("source verify-release log has too many lines")
        line = line.rstrip("\r\n")
        match = JOB_LOG_ENV_RE.fullmatch(line)
        if match is None:
            continue
        value_match = JOB_LOG_ENV_VALUE_RE.fullmatch(match.group("rest"))
        if value_match is None:
            raise ReleaseWorkflowError(
                f"source verify-release log has malformed {match.group('name')} evidence"
            )
        observed.append((match.group("name"), value_match.group("value")))
        if len(observed) > MAX_POLICY_TRIPLES * len(expected):
            raise ReleaseWorkflowError(
                "source verify-release log has too many release input triples"
            )
    if not observed or len(observed) % len(expected) != 0:
        raise ReleaseWorkflowError(
            "source verify-release log lacks complete release input triples"
        )
    for offset in range(0, len(observed), len(expected)):
        actual = observed[offset : offset + len(expected)]
        if actual != expected:
            differing_name = next(
                (
                    expected_name
                    for (actual_name, actual_value), (expected_name, expected_value)
                    in zip(actual, expected)
                    if (actual_name, actual_value) != (expected_name, expected_value)
                ),
                "release-policy triple",
            )
            raise ReleaseWorkflowError(
                "source verify-release log does not match the expected value for "
                f"{differing_name}"
            )
    count = len(observed) // len(expected)
    return (
        f"verified {count} release input triple(s): "
        f"tag {tag}, SHA {sha}, alias {alias}"
    )


def select_artifact_values(
    *,
    resume_run_id: str,
    current_run_id: str,
    fresh_python_id: str,
    fresh_python_digest: str,
    fresh_release_id: str,
    fresh_release_digest: str,
    recovered_run_id: str,
    recovered_python_id: str,
    recovered_python_digest: str,
    recovered_release_id: str,
    recovered_release_digest: str,
) -> dict[str, str]:
    """Normalize the fresh or recovered immutable artifact outputs."""
    if resume_run_id:
        values = {
            "source-run-id": recovered_run_id,
            "python-dist-artifact-id": recovered_python_id,
            "python-dist-artifact-digest": recovered_python_digest,
            "release-assets-artifact-id": recovered_release_id,
            "release-assets-artifact-digest": recovered_release_digest,
        }
        if recovered_run_id != resume_run_id:
            raise ReleaseWorkflowError("selected recovery run ID disagrees")
    else:
        values = {
            "source-run-id": current_run_id,
            "python-dist-artifact-id": fresh_python_id,
            "python-dist-artifact-digest": fresh_python_digest,
            "release-assets-artifact-id": fresh_release_id,
            "release-assets-artifact-digest": fresh_release_digest,
        }
    for key in (
        "source-run-id",
        "python-dist-artifact-id",
        "release-assets-artifact-id",
    ):
        _positive_int_text(values[key], f"selected {key}")
    for key in (
        "python-dist-artifact-digest",
        "release-assets-artifact-digest",
    ):
        values[key] = _digest(values[key], f"selected {key}")
    return values


def _hash_exact(stream, size: int, label: str) -> bytes:
    digest = hashlib.sha256()
    total = 0
    while total < size:
        requested = min(READ_CHUNK_BYTES, size - total)
        chunk = stream.read(requested)
        if not chunk:
            raise ReleaseWorkflowError(f"{label} is truncated")
        if len(chunk) > requested:
            raise ReleaseWorkflowError(f"{label} exceeded a bounded read")
        digest.update(chunk)
        total += len(chunk)
    if stream.read(1):
        raise ReleaseWorkflowError(f"{label} exceeds its advertised size")
    return digest.digest()


def _hash_stable_file(path: Path, limit: int, label: str) -> tuple[int, bytes]:
    try:
        initial = path.lstat()
        if not stat.S_ISREG(initial.st_mode) or not 0 <= initial.st_size <= limit:
            raise ReleaseWorkflowError(f"{label} exceeds the byte limit")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (initial.st_dev, initial.st_ino) != (opened.st_dev, opened.st_ino)
                or not 0 <= opened.st_size <= limit
            ):
                raise ReleaseWorkflowError(f"{label} exceeds the byte limit")
            digest = _hash_exact(stream, opened.st_size, label)
            finished = os.fstat(stream.fileno())
        current = path.lstat()
    except ReleaseWorkflowError:
        raise
    except OSError as error:
        raise ReleaseWorkflowError(f"cannot read {label}: {error}") from error
    if (
        not stat.S_ISREG(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        or opened.st_size != finished.st_size
        or opened.st_mtime_ns != finished.st_mtime_ns
        or current.st_size != finished.st_size
        or current.st_mtime_ns != finished.st_mtime_ns
    ):
        raise ReleaseWorkflowError(f"{label} changed while being read")
    return opened.st_size, digest


def _valid_flat_name(name: str) -> bool:
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (
        bool(name)
        and len(encoded) <= MAX_ARCHIVE_PATH_BYTES
        and PurePosixPath(name).name == name
        and "/" not in name
        and "\\" not in name
        and name not in {".", ".."}
    )


def _read_exact(stream, size: int, label: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < size:
        requested = min(READ_CHUNK_BYTES, size - total)
        chunk = stream.read(requested)
        if not chunk:
            raise ReleaseWorkflowError(f"{label} is truncated")
        if len(chunk) > requested:
            raise ReleaseWorkflowError(f"{label} exceeded a bounded read")
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _preflight_zip(path: Path) -> int:
    source_size = path.stat().st_size
    if not 0 < source_size <= MAX_ARCHIVE_BYTES:
        raise ReleaseWorkflowError("artifact archive exceeds the compressed-byte limit")
    tail_size = min(source_size, 65_557)
    with path.open("rb") as stream:
        stream.seek(source_size - tail_size)
        tail = _read_exact(stream, tail_size, "ZIP footer")
        eocd_index = tail.rfind(b"PK\x05\x06")
        if eocd_index < 0 or len(tail) - eocd_index < 22:
            raise ReleaseWorkflowError("artifact archive has no valid ZIP footer")
        try:
            eocd = struct.unpack_from("<4s4H2LH", tail, eocd_index)
        except struct.error as error:
            raise ReleaseWorkflowError(
                "artifact archive has a malformed ZIP footer"
            ) from error
        disk_number, central_disk = eocd[1], eocd[2]
        disk_entries, total_entries = eocd[3], eocd[4]
        central_size, central_offset = eocd[5], eocd[6]
        comment_size = eocd[7]
        if eocd_index + 22 + comment_size != len(tail):
            raise ReleaseWorkflowError("artifact archive has a malformed ZIP footer")
        if (
            disk_number != 0
            or central_disk != 0
            or disk_entries != total_entries
            or total_entries == 0xFFFF
            or central_size == 0xFFFFFFFF
            or central_offset == 0xFFFFFFFF
            or central_size > MAX_ARCHIVE_METADATA_BYTES
        ):
            raise ReleaseWorkflowError("artifact archive uses unsupported ZIP metadata")
        if not 1 <= total_entries <= MAX_ARCHIVE_MEMBERS:
            raise ReleaseWorkflowError("artifact archive exceeds the member-count limit")
        eocd_offset = source_size - tail_size + eocd_index
        central_end = central_offset + central_size
        if central_end > eocd_offset:
            raise ReleaseWorkflowError("artifact archive central directory is malformed")
        stream.seek(central_offset)
        aggregate = 0
        names: set[str] = set()
        for _ in range(total_entries):
            header = _read_exact(stream, 46, "ZIP central directory")
            try:
                fields = struct.unpack("<4s6H3I5H2I", header)
            except struct.error as error:
                raise ReleaseWorkflowError(
                    "artifact archive central directory is malformed"
                ) from error
            if fields[0] != b"PK\x01\x02":
                raise ReleaseWorkflowError(
                    "artifact archive central directory is malformed"
                )
            flags = fields[3]
            compressed_size = fields[8]
            uncompressed_size = fields[9]
            filename_size, extra_size, item_comment_size = fields[10:13]
            local_offset = fields[16]
            if (
                flags & 1
                or compressed_size == 0xFFFFFFFF
                or compressed_size > MAX_ARCHIVE_BYTES
                or uncompressed_size == 0xFFFFFFFF
                or local_offset == 0xFFFFFFFF
                or local_offset >= central_offset
                or filename_size > MAX_ARCHIVE_PATH_BYTES
            ):
                raise ReleaseWorkflowError(
                    "artifact archive member metadata is unsupported"
                )
            name_bytes = _read_exact(stream, filename_size, "ZIP member name")
            encoding = "utf-8" if flags & 0x800 else "cp437"
            try:
                name = name_bytes.decode(encoding)
            except UnicodeDecodeError as error:
                raise ReleaseWorkflowError(
                    "artifact archive contains an invalid member name"
                ) from error
            if not _valid_flat_name(name) or name in names:
                raise ReleaseWorkflowError(
                    "artifact archive is not a unique flat file set"
                )
            names.add(name)
            if uncompressed_size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ReleaseWorkflowError(
                    "artifact archive member exceeds the size limit"
                )
            aggregate += uncompressed_size
            if aggregate > MAX_ARCHIVE_TOTAL_BYTES:
                raise ReleaseWorkflowError(
                    "artifact archive exceeds the aggregate size limit"
                )
            stream.seek(extra_size + item_comment_size, 1)
            if stream.tell() > central_end:
                raise ReleaseWorkflowError(
                    "artifact archive central directory is malformed"
                )
        if stream.tell() != central_end:
            raise ReleaseWorkflowError("artifact archive central directory is malformed")
    return total_entries


def verify_artifact_archive(
    archive_path: Path, extracted_path: Path, expected_digest: str
) -> None:
    """Verify raw artifact ZIP identity and action-extracted flat file bytes."""
    expected = _digest(expected_digest, "artifact archive digest").removeprefix(
        "sha256:"
    )
    archive_size, archive_digest = _hash_stable_file(
        archive_path, MAX_ARCHIVE_BYTES, "recovered artifact archive"
    )
    if archive_digest.hex() != expected:
        raise ReleaseWorkflowError("recovered artifact archive digest mismatch")
    expected_count = _preflight_zip(archive_path)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len(entries) != expected_count:
                raise ReleaseWorkflowError(
                    "artifact archive member count changed after preflight"
                )
            names = [entry.filename for entry in entries]
            if (
                any(entry.is_dir() for entry in entries)
                or len(names) != len(set(names))
                or any(not _valid_flat_name(name) for name in names)
                or any(entry.file_size > MAX_ARCHIVE_MEMBER_BYTES for entry in entries)
                or sum(entry.file_size for entry in entries) > MAX_ARCHIVE_TOTAL_BYTES
                or any(
                    ((entry.external_attr >> 16) & 0o170000) not in {0, stat.S_IFREG}
                    for entry in entries
                )
            ):
                raise ReleaseWorkflowError(
                    "artifact archive is not a unique flat file set"
                )
            extracted: list[Path] = []
            for path in extracted_path.iterdir():
                if len(extracted) >= MAX_ARCHIVE_MEMBERS:
                    raise ReleaseWorkflowError(
                        "download action output has too many entries"
                    )
                extracted.append(path)
            if {path.name for path in extracted} != set(names) or len(extracted) != len(
                names
            ):
                raise ReleaseWorkflowError(
                    "download action output disagrees with artifact archive"
                )
            for entry in entries:
                path = extracted_path / entry.filename
                if not path.is_file() or path.is_symlink():
                    raise ReleaseWorkflowError(
                        "downloaded artifact entry is not a regular file"
                    )
                if path.stat().st_size != entry.file_size:
                    raise ReleaseWorkflowError(
                        "downloaded artifact entry size disagrees"
                    )
                with archive.open(entry) as archived_stream:
                    archived_hash = _hash_exact(
                        archived_stream, entry.file_size, "archived artifact member"
                    )
                extracted_size, extracted_hash = _hash_stable_file(
                    path, MAX_ARCHIVE_MEMBER_BYTES, "downloaded artifact member"
                )
                if extracted_size != entry.file_size:
                    raise ReleaseWorkflowError(
                        "downloaded artifact entry size disagrees"
                    )
                if archived_hash != extracted_hash:
                    raise ReleaseWorkflowError(
                        f"download action changed artifact bytes: {entry.filename}"
                    )
    except zipfile.BadZipFile as error:
        raise ReleaseWorkflowError("recovered artifact archive is not a ZIP file") from error
    final_size, final_digest = _hash_stable_file(
        archive_path, MAX_ARCHIVE_BYTES, "recovered artifact archive"
    )
    if final_size != archive_size or final_digest != archive_digest:
        raise ReleaseWorkflowError("recovered artifact archive changed during validation")


def validate_recovered_payload(
    python_dist: Path, release_assets: Path, tag: str
) -> None:
    """Validate exact release files, checksums, and duplicated dist bytes."""
    if TAG_RE.fullmatch(tag) is None:
        raise ReleaseWorkflowError(f"invalid release tag: {tag!r}")
    version = tag.removeprefix("v")
    wheel = f"boundver-{version}-py3-none-any.whl"
    sdist = f"boundver-{version}.tar.gz"
    pyz = f"boundver-{version}.pyz"

    def require_exact(root: Path, expected: set[str]) -> list[Path]:
        entries: list[Path] = []
        for entry in root.iterdir():
            if len(entries) >= len(expected):
                raise ReleaseWorkflowError(f"{root} has too many entries")
            entries.append(entry)
        if {entry.name for entry in entries} != expected:
            raise ReleaseWorkflowError(
                f"{root} is not the expected flat payload: "
                + ", ".join(sorted(entry.name for entry in entries))
            )
        if any(not entry.is_file() or entry.is_symlink() for entry in entries):
            raise ReleaseWorkflowError(f"{root} contains a non-regular file")
        return entries

    python_entries = require_exact(python_dist, {wheel, sdist})
    release_entries = require_exact(
        release_assets, {wheel, sdist, pyz, "SHA256SUMS"}
    )
    checksum_lines = _read_bounded_text(
        release_assets / "SHA256SUMS", MAX_CHECKSUM_BYTES, "SHA256SUMS"
    ).splitlines()
    checksums: dict[str, str] = {}
    for line in checksum_lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/]+)", line)
        if match is None or match.group(2) in checksums:
            raise ReleaseWorkflowError("SHA256SUMS contains a malformed entry")
        checksums[match.group(2)] = match.group(1)
    if set(checksums) != {wheel, sdist, pyz}:
        raise ReleaseWorkflowError("SHA256SUMS does not cover the exact release payload")
    release_sizes = {
        entry.name: entry.stat().st_size
        for entry in release_entries
        if entry.name != "SHA256SUMS"
    }
    python_sizes = {entry.name: entry.stat().st_size for entry in python_entries}
    if (
        any(not 0 <= size <= MAX_ARCHIVE_MEMBER_BYTES for size in release_sizes.values())
        or any(not 0 <= size <= MAX_ARCHIVE_MEMBER_BYTES for size in python_sizes.values())
        or sum(release_sizes.values()) > MAX_ARCHIVE_TOTAL_BYTES
        or sum(python_sizes.values()) > MAX_ARCHIVE_TOTAL_BYTES
    ):
        raise ReleaseWorkflowError("recovered release payload exceeds the size limit")
    release_hashes: dict[str, bytes] = {}
    for name, expected in checksums.items():
        _, actual = _hash_stable_file(
            release_assets / name,
            MAX_ARCHIVE_MEMBER_BYTES,
            f"recovered release asset {name}",
        )
        release_hashes[name] = actual
        if actual.hex() != expected:
            raise ReleaseWorkflowError(f"SHA256SUMS mismatch for {name}")
    for name in (wheel, sdist):
        python_size, python_hash = _hash_stable_file(
            python_dist / name,
            MAX_ARCHIVE_MEMBER_BYTES,
            f"recovered Python distribution {name}",
        )
        if python_size != release_sizes[name] or python_hash != release_hashes[name]:
            raise ReleaseWorkflowError(
                f"Python distribution and release-asset copies differ: {name}"
            )


def parse_github_release_probe(response: str) -> tuple[str, str]:
    """Parse the final HTTP response from ``gh api --include``."""
    try:
        if len(response.encode("utf-8")) > MAX_PROBE_BYTES:
            raise ReleaseWorkflowError("GitHub Release probe exceeds the byte limit")
    except UnicodeEncodeError as error:
        raise ReleaseWorkflowError("GitHub Release probe is not valid UTF-8") from error
    normalized = response.replace("\r\n", "\n").replace("\r", "\n")
    statuses = re.findall(r"(?m)^HTTP/\S+\s+(\d{3})\b", normalized)
    if not statuses:
        raise ReleaseWorkflowError("GitHub Release probe returned no HTTP status")
    status = statuses[-1]
    if status == "404":
        return status, "absent"
    if status != "200":
        return status, "error"
    status_lines = list(re.finditer(r"(?m)^HTTP/\S+\s+200\b", normalized))
    body_start = normalized.find("\n\n", status_lines[-1].end())
    if body_start < 0:
        raise ReleaseWorkflowError("GitHub Release probe returned no response body")
    try:
        payload = _strict_json_loads(normalized[body_start + 2 :])
    except (TypeError, ValueError) as error:
        raise ReleaseWorkflowError(
            f"GitHub Release probe returned invalid JSON: {error}"
        ) from error
    if not isinstance(payload, dict) or type(payload.get("draft")) is not bool:
        raise ReleaseWorkflowError(
            "GitHub Release probe returned no boolean draft state"
        )
    return status, "draft" if payload["draft"] else "public"


def _json_file(path: Path) -> object:
    try:
        return _strict_json_loads(
            _read_bounded_text(path, MAX_GITHUB_RESPONSE_BYTES, "GitHub API payload")
        )
    except (ReleaseWorkflowError, ValueError) as error:
        raise ReleaseWorkflowError(f"invalid JSON payload: {path}") from error


def _write_outputs(path: Path, values: Mapping[str, str]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    recovery = commands.add_parser("select-recovery")
    recovery.add_argument("--repository", required=True)
    recovery.add_argument("--run-id", required=True)
    recovery.add_argument("--tag", required=True)
    recovery.add_argument("--sha", required=True)
    recovery.add_argument("--run-json", type=Path, required=True)
    recovery.add_argument("--jobs-json", type=Path, required=True)
    recovery.add_argument("--artifacts-json", type=Path, required=True)
    recovery.add_argument("--output", type=Path, required=True)

    fetch = commands.add_parser("recover-artifacts")
    fetch.add_argument("--repository", required=True)
    fetch.add_argument("--run-id", required=True)
    fetch.add_argument("--tag", required=True)
    fetch.add_argument("--sha", required=True)
    fetch.add_argument("--output", type=Path, required=True)

    evidence = commands.add_parser("verify-input-log")
    evidence_source = evidence.add_mutually_exclusive_group(required=True)
    evidence_source.add_argument("--log", type=Path)
    evidence_source.add_argument("--job-id")
    evidence.add_argument("--repository")
    evidence.add_argument("--tag", required=True)
    evidence.add_argument("--sha", required=True)
    evidence.add_argument("--alias", required=True)

    selection = commands.add_parser("select-artifacts")
    for name in (
        "resume-run-id",
        "current-run-id",
        "fresh-python-id",
        "fresh-python-digest",
        "fresh-release-id",
        "fresh-release-digest",
        "recovered-run-id",
        "recovered-python-id",
        "recovered-python-digest",
        "recovered-release-id",
        "recovered-release-digest",
    ):
        selection.add_argument(f"--{name}", default="")
    selection.add_argument("--output", type=Path, required=True)

    archive = commands.add_parser("verify-archive")
    archive.add_argument("--archive", type=Path, required=True)
    archive.add_argument("--extracted", type=Path, required=True)
    archive.add_argument("--digest", required=True)

    payload = commands.add_parser("validate-payload")
    payload.add_argument("--python-dist", type=Path, required=True)
    payload.add_argument("--release-assets", type=Path, required=True)
    payload.add_argument("--tag", required=True)

    probe = commands.add_parser("probe-release")
    probe.add_argument("--response", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "select-recovery":
            selection = select_recovery_artifacts(
                _json_file(args.run_json),
                _json_file(args.jobs_json),
                _json_file(args.artifacts_json),
                repository=args.repository,
                run_id=_positive_int_text(args.run_id, "source publication run ID"),
                tag=args.tag,
                sha=args.sha,
            )
            _write_outputs(args.output, selection.outputs())
            detail = (
                f"selected run {selection.source_run_id} attempt "
                f"{selection.artifact_attempt} retained artifacts"
            )
        elif args.command == "recover-artifacts":
            run_id = _positive_int_text(args.run_id, "source publication run ID")
            selection = select_recovery_artifacts(
                *fetch_recovery_payloads(args.repository, run_id),
                repository=args.repository,
                run_id=run_id,
                tag=args.tag,
                sha=args.sha,
            )
            _write_outputs(args.output, selection.outputs())
            detail = (
                f"selected run {selection.source_run_id} attempt "
                f"{selection.artifact_attempt} retained artifacts; validated "
                f"{selection.release_note_artifact_count} release-note artifact(s)"
            )
        elif args.command == "verify-input-log":
            if args.job_id is not None:
                if not args.repository:
                    raise ReleaseWorkflowError(
                        "--repository is required with --job-id"
                    )
                job_log = fetch_verification_job_log(
                    args.repository,
                    _positive_int_text(args.job_id, "source verification job ID"),
                )
            else:
                job_log = _read_bounded_text(
                    args.log, MAX_JOB_LOG_BYTES, "source verification job log"
                )
            detail = require_release_input_evidence(
                job_log,
                tag=args.tag,
                sha=args.sha,
                alias=args.alias,
            )
        elif args.command == "select-artifacts":
            values = select_artifact_values(
                **{
                    name.replace("-", "_"): getattr(args, name.replace("-", "_"))
                    for name in (
                        "resume-run-id",
                        "current-run-id",
                        "fresh-python-id",
                        "fresh-python-digest",
                        "fresh-release-id",
                        "fresh-release-digest",
                        "recovered-run-id",
                        "recovered-python-id",
                        "recovered-python-digest",
                        "recovered-release-id",
                        "recovered-release-digest",
                    )
                }
            )
            _write_outputs(args.output, values)
            detail = f"selected immutable artifacts from run {values['source-run-id']}"
        elif args.command == "verify-archive":
            verify_artifact_archive(args.archive, args.extracted, args.digest)
            detail = f"verified artifact archive {args.archive}"
        elif args.command == "validate-payload":
            validate_recovered_payload(
                args.python_dist, args.release_assets, args.tag
            )
            detail = f"validated recovered payload for {args.tag}"
        else:
            status, state = parse_github_release_probe(
                _read_bounded_text(
                    args.response, MAX_PROBE_BYTES, "GitHub Release probe"
                )
            )
            detail = f"{status}|{state}"
    except (OSError, ReleaseWorkflowError) as error:
        print(f"Release workflow error: {error}", file=sys.stderr)
        return 1
    print(detail)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
