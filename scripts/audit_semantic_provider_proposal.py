#!/usr/bin/env python3
"""Audit authoritative GitHub evidence for the semantic-provider proposal.

The proposal manifest is a declaration, not its own trust root.  This program
finds the latest commit that changed the governed proposal surface and proves
that GitHub merged an identical reviewed tree into ``main``.  It then checks
current, exact-head, non-author review evidence before allowing an acceptance,
v0.15 work, or v0.15 release gate to pass.

Only bounded Git and ``gh api`` subprocesses are used.  Review bodies are never
printed or persisted; the successful result contains only reviewer identities
and a digest of the stable GitHub snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import locale
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, BinaryIO, Optional, Sequence


MAX_STDOUT_BYTES = 8 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_REVIEWED_FILE_BYTES = 2 * 1024 * 1024
MAX_API_TOTAL_BYTES = 32 * 1024 * 1024
MAX_API_REQUESTS = 100
MAX_PAGES = 10
MAX_RECORDS = 1_000
MAX_PERMISSION_REVIEWERS = 50
MAX_REVIEW_BODY_CHARS = 256 * 1024
MAX_GITHUB_ID = (1 << 63) - 1
MAX_JSON_INTEGER = (1 << 63) - 1
COMMAND_TIMEOUT_SECONDS = 15
AUDIT_TIMEOUT_SECONDS = 55
STREAM_CHUNK_BYTES = 64 * 1024
CANONICAL_MANIFEST = Path("spec/semantic-provider-proposal.json")
# This supported contract retains merge_commit_sha, which the 2026-03-10
# response removed.  API-version migration is an explicit proposal re-review
# trigger; GitHub currently documents support through 2028-03-10.
GITHUB_REST_API_VERSION = "2022-11-28"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
USER_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
BOT_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,94}\[bot\]$")
TIMESTAMP_RE = re.compile(
    r"^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.([0-9]{1,9}))?Z$"
)
ALLOWED_PERMISSIONS = {"admin", "maintain", "write", "triage", "read", "none"}
QUALIFYING_PERMISSIONS = {"admin", "maintain", "write"}
DECISIVE_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
REVIEW_STATES = DECISIVE_REVIEW_STATES | {"COMMENTED", "PENDING"}

# A change to any of these files requires a fresh exact-content proposal audit.
# Broader explanatory docs may evolve normally; these files define the design,
# threat traceability, machine gate, and CI enforcement contract.
GOVERNED_PATHS = (
    ".github/workflows/ci.yml",
    "docs/design/semantic-provider-rfc.md",
    "docs/design/semantic-provider-threat-model.md",
    "scripts/audit_semantic_provider_proposal.py",
    "scripts/check_semantic_provider_proposal.py",
    "spec/semantic-provider-proposal.json",
    "spec/semantic-provider-proposal.schema.json",
    "tests/test_semantic_provider_proposal.py",
)
BOOTSTRAP_PATHS = (
    ".github/workflows/ci.yml",
    "scripts/audit_semantic_provider_proposal.py",
    "scripts/check_semantic_provider_proposal.py",
)
VALIDATION_PATHS = (
    ".github/workflows/ci.yml",
    "docs/design/semantic-provider-rfc.md",
    "docs/design/semantic-provider-threat-model.md",
    "scripts/check_semantic_provider_proposal.py",
    "spec/semantic-provider-proposal.json",
    "spec/semantic-provider-proposal.schema.json",
)


class AuditError(RuntimeError):
    """A deterministic, fail-closed proposal audit failure."""


def _trusted_tool(name: str, repo: Path) -> str:
    """Resolve a host tool once and reject repository-local shadowing."""
    raw = shutil.which(name)
    if raw is None:
        raise AuditError(f"required command is unavailable: {name}")
    try:
        path = Path(raw).resolve(strict=True)
        repo_root = repo.resolve(strict=True)
        path.relative_to(repo_root)
    except ValueError:
        pass
    except OSError as exc:
        raise AuditError(f"cannot resolve trusted command: {name}") from exc
    else:
        raise AuditError(f"required command resolves inside the repository: {name}")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise AuditError(f"cannot stat trusted command: {name}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise AuditError(f"required command is not a regular file: {name}")
    return str(path)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuditError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise AuditError(f"non-finite JSON number is not allowed: {value}")


def _parse_int(value: str) -> int:
    if len(value) > 20:
        raise AuditError("JSON integer exceeds the signed 64-bit limit")
    result = int(value)
    if abs(result) > MAX_JSON_INTEGER:
        raise AuditError("JSON integer exceeds the signed 64-bit limit")
    return result


def _reject_float(value: str) -> None:
    raise AuditError(f"floating-point JSON number is not allowed: {value}")


def _run_bounded(
    command: Sequence[str],
    *,
    cwd: Path,
    stdout_limit: int = MAX_STDOUT_BYTES,
    stderr_limit: int = MAX_STDERR_BYTES,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> bytes:
    """Run a command while independently bounding and draining both streams."""
    if not command or stdout_limit < 0 or stderr_limit < 0 or timeout <= 0:
        raise ValueError("invalid bounded subprocess arguments")
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AuditError(f"required command is unavailable: {command[0]}") from exc
    assert process.stdout is not None
    assert process.stderr is not None

    captured: dict[str, bytes] = {}
    overflows: list[str] = []
    read_errors: list[BaseException] = []
    state_lock = threading.Lock()

    def terminate() -> None:
        try:
            process.kill()
        except OSError:
            pass

    def read_stream(stream: BinaryIO, name: str, limit: int) -> None:
        data = bytearray()
        try:
            while True:
                remaining = limit - len(data)
                chunk = stream.read(min(STREAM_CHUNK_BYTES, remaining + 1))
                if not chunk:
                    break
                if len(chunk) > remaining:
                    with state_lock:
                        overflows.append(name)
                    terminate()
                    break
                data.extend(chunk)
        except BaseException as exc:  # pragma: no cover - OS pipe failure
            with state_lock:
                read_errors.append(exc)
            terminate()
        finally:
            captured[name] = bytes(data)
            stream.close()

    readers = (
        threading.Thread(
            target=read_stream,
            args=(process.stdout, "stdout", stdout_limit),
            daemon=True,
        ),
        threading.Thread(
            target=read_stream,
            args=(process.stderr, "stderr", stderr_limit),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate()
        returncode = process.wait()
    except BaseException:
        terminate()
        process.wait()
        raise
    finally:
        for reader in readers:
            reader.join()

    if read_errors:
        raise AuditError("failed while reading bounded subprocess output") from read_errors[0]
    if timed_out:
        raise AuditError(f"{command[0]} exceeded the {timeout}-second timeout")
    if overflows:
        stream_name = "stdout" if "stdout" in overflows else "stderr"
        limit = stdout_limit if stream_name == "stdout" else stderr_limit
        raise AuditError(f"{command[0]} {stream_name} exceeds the {limit}-byte limit")
    if returncode != 0:
        diagnostic = captured.get("stderr") or captured.get("stdout") or b""
        detail = diagnostic.decode(locale.getpreferredencoding(False), "replace").strip()
        if len(detail) > 2_000:
            detail = detail[:2_000] + "..."
        raise AuditError(detail or f"{command[0]} exited with status {returncode}")
    return captured.get("stdout", b"")


def _decode_json(raw: bytes, label: str) -> Any:
    if len(raw) > MAX_JSON_BYTES:
        raise AuditError(f"{label} exceeds the {MAX_JSON_BYTES}-byte JSON limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuditError(f"{label} is not UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
            parse_int=_parse_int,
        )
    except (json.JSONDecodeError, AuditError, RecursionError) as exc:
        raise AuditError(f"{label} is not valid bounded JSON") from exc


def _read_regular(path: Path, limit: int, label: str) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise AuditError(f"cannot stat {label}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise AuditError(f"{label} must be a bounded regular file")
    try:
        with path.open("rb") as stream:
            opened = os_fstat = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise AuditError(f"opened {label} is not a regular file")
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise AuditError(f"{label} changed while opening")
            raw = stream.read(limit + 1)
            after_read = os.fstat(stream.fileno())
    except OSError as exc:
        raise AuditError(f"cannot read {label}") from exc
    if len(raw) > limit:
        raise AuditError(f"{label} exceeds the {limit}-byte limit")
    try:
        current = path.lstat()
    except OSError as exc:
        raise AuditError(f"cannot restat {label}") from exc
    identity = (os_fstat.st_dev, os_fstat.st_ino, os_fstat.st_size, os_fstat.st_mtime_ns)
    for candidate in (after_read, current):
        if identity != (
            candidate.st_dev,
            candidate.st_ino,
            candidate.st_size,
            candidate.st_mtime_ns,
        ):
            raise AuditError(f"{label} changed while reading")
    return raw
def _git(repo: Path, *arguments: str) -> bytes:
    return _run_bounded([_trusted_tool("git", repo), *arguments], cwd=repo)


def _git_blob(repo: Path, commit: str, path: str) -> bytes:
    raw_entry = _run_bounded(
        [
            _trusted_tool("git", repo),
            "ls-tree",
            "-z",
            "--full-tree",
            commit,
            "--",
            path,
        ],
        cwd=repo,
        stdout_limit=4_096,
    )
    expected_path = path.encode("utf-8")
    if raw_entry.count(b"\0") != 1 or not raw_entry.endswith(b"\0"):
        raise AuditError(f"reviewed path has no unique Git tree entry: {path}")
    try:
        header, returned_path = raw_entry[:-1].split(b"\t", 1)
        mode, object_type, raw_blob = header.split(b" ")
        returned_path_text = returned_path.decode("utf-8", "strict")
        blob_text = raw_blob.decode("ascii", "strict")
    except (UnicodeError, ValueError) as exc:
        raise AuditError(f"reviewed path has a malformed Git tree entry: {path}") from exc
    if (
        returned_path != expected_path
        or returned_path_text != path
        or mode not in {b"100644", b"100755"}
        or object_type != b"blob"
    ):
        raise AuditError(f"reviewed path is not a regular Git file: {path}")
    blob = _sha(blob_text, f"reviewed blob for {path}")
    return _run_bounded(
        [_trusted_tool("git", repo), "cat-file", "blob", blob],
        cwd=repo,
        stdout_limit=MAX_REVIEWED_FILE_BYTES,
    )


def _materialize_validation_tree(repo: Path, commit: str, destination: Path) -> None:
    """Create a private checker input tree solely from exact reviewed blobs."""
    for relative in VALIDATION_PATHS:
        raw = _git_blob(repo, commit, relative)
        target = destination.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise AuditError(f"cannot materialize reviewed blob: {relative}") from exc


def _sha(value: Any, field: str) -> str:
    if type(value) is not str or SHA_RE.fullmatch(value) is None:
        raise AuditError(f"{field} is not a full lowercase Git commit ID")
    return value


def _positive_id(value: Any, field: str) -> int:
    if type(value) is not int or not 0 < value <= MAX_GITHUB_ID:
        raise AuditError(f"{field} is not a valid GitHub numeric ID")
    return value


def _bounded_string(
    value: Any,
    field: str,
    *,
    maximum: int = 4_096,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or (not value and not allow_empty) or len(value) > maximum:
        qualifier = "a bounded string" if allow_empty else "a non-empty bounded string"
        raise AuditError(f"{field} must be {qualifier}")
    return value


def _timestamp(value: Any, field: str) -> str:
    result = _bounded_string(value, field, maximum=64)
    match = TIMESTAMP_RE.fullmatch(result)
    if match is None:
        raise AuditError(f"{field} is not a GitHub UTC timestamp")
    try:
        datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise AuditError(f"{field} is not a valid GitHub UTC timestamp") from exc
    return result


def _timestamp_key(value: str) -> tuple[str, int]:
    match = TIMESTAMP_RE.fullmatch(value)
    if match is None:  # pragma: no cover - every caller validates first
        raise AuditError("internal timestamp was not validated")
    fraction = (match.group(2) or "").ljust(9, "0")
    return match.group(1), int(fraction or "0")


def _timestamp_datetime(value: str) -> datetime:
    match = TIMESTAMP_RE.fullmatch(value)
    if match is None:  # pragma: no cover - every caller validates first
        raise AuditError("internal timestamp was not validated")
    base = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    fraction = (match.group(2) or "")[:6].ljust(6, "0")
    return base + timedelta(microseconds=int(fraction or "0"))


def _actor(value: Any, field: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise AuditError(f"{field} is malformed")
    actor_id = _positive_id(value.get("id"), f"{field}.id")
    actor_type = value.get("type")
    if actor_type not in {"User", "Bot"}:
        raise AuditError(f"{field}.type is malformed")
    maximum = 100 if actor_type == "Bot" else 39
    login = _bounded_string(value.get("login"), f"{field}.login", maximum=maximum)
    login_pattern = BOT_LOGIN_RE if actor_type == "Bot" else USER_LOGIN_RE
    if login_pattern.fullmatch(login) is None:
        raise AuditError(f"{field}.login is malformed")
    return {"id": actor_id, "login": login, "type": actor_type}


class GitHubClient:
    """Small, bounded GitHub API client backed by the authenticated gh CLI."""

    def __init__(
        self, repo_root: Path, *, gh_executable: Optional[str] = None
    ) -> None:
        self.repo_root = repo_root
        self.gh_executable = gh_executable or _trusted_tool("gh", repo_root)
        self.deadline = time.monotonic() + AUDIT_TIMEOUT_SECONDS
        self.request_count = 0
        self.response_bytes = 0

    def api(self, arguments: Sequence[str], label: str) -> Any:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AuditError("GitHub audit exceeded its total time budget")
        self.request_count += 1
        if self.request_count > MAX_API_REQUESTS:
            raise AuditError("GitHub audit exceeded its request-count budget")
        raw = _run_bounded(
            [
                self.gh_executable,
                "api",
                "--hostname",
                "github.com",
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                f"X-GitHub-Api-Version: {GITHUB_REST_API_VERSION}",
                *arguments,
            ],
            cwd=self.repo_root,
            timeout=min(COMMAND_TIMEOUT_SECONDS, max(1, int(remaining))),
        )
        self.response_bytes += len(raw)
        if self.response_bytes > MAX_API_TOTAL_BYTES:
            raise AuditError("GitHub audit exceeded its aggregate response budget")
        if time.monotonic() > self.deadline:
            raise AuditError("GitHub audit exceeded its total time budget")
        return _decode_json(raw, label)

    def rest(self, endpoint: str, label: str) -> Any:
        return self.api(["--method", "GET", endpoint], label)

    def rest_pages(self, endpoint: str, label: str) -> list[Any]:
        records: list[Any] = []
        separator = "&" if "?" in endpoint else "?"
        for page in range(1, MAX_PAGES + 1):
            value = self.rest(
                f"{endpoint}{separator}per_page=100&page={page}",
                f"{label} page {page}",
            )
            if type(value) is not list or len(value) > 100:
                raise AuditError(f"GitHub returned malformed pagination for {label}")
            if len(records) + len(value) > MAX_RECORDS:
                raise AuditError(f"GitHub returned too many records for {label}")
            records.extend(value)
            if len(value) < 100:
                return records
        raise AuditError(f"GitHub pagination for {label} reached the page ceiling")

    def review_state(self, owner: str, name: str, number: int) -> dict[str, Any]:
        query = (
            "query($owner:String!,$repository:String!,$number:Int!,"
            "$endCursor:String){repository(owner:$owner,name:$repository){"
            "pullRequest(number:$number){reviewDecision reviewThreads("
            "first:100,after:$endCursor){nodes{id isResolved}"
            "pageInfo{hasNextPage endCursor}}}}}"
        )
        cursor: Optional[str] = None
        decision: Any = None
        decision_seen = False
        threads: list[dict[str, Any]] = []
        thread_ids: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            arguments = [
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                f"owner={owner}",
                "-f",
                f"repository={name}",
                "-F",
                f"number={number}",
            ]
            if cursor is not None:
                arguments.extend(("-f", f"endCursor={cursor}"))
            response = self.api(arguments, f"review threads page {page}")
            try:
                pull_request = response["data"]["repository"]["pullRequest"]
                thread_connection = pull_request["reviewThreads"]
                nodes = thread_connection["nodes"]
                page_info = thread_connection["pageInfo"]
            except (KeyError, TypeError) as exc:
                raise AuditError("GitHub returned malformed review-thread data") from exc
            page_decision = pull_request.get("reviewDecision")
            if page_decision not in {None, "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}:
                raise AuditError("GitHub returned an unknown review decision")
            if not decision_seen:
                decision = page_decision
                decision_seen = True
            elif decision != page_decision:
                raise AuditError("GitHub review decision changed during pagination")
            if type(nodes) is not list or len(nodes) > 100 or type(page_info) is not dict:
                raise AuditError("GitHub returned malformed review-thread pagination")
            if len(threads) + len(nodes) > MAX_RECORDS:
                raise AuditError("GitHub returned too many review threads")
            for index, node in enumerate(nodes):
                if type(node) is not dict or set(node) != {"id", "isResolved"}:
                    raise AuditError("GitHub returned a malformed review thread")
                thread_id = _bounded_string(
                    node.get("id"), f"reviewThreads[{index}].id", maximum=256
                )
                resolved = node.get("isResolved")
                if type(resolved) is not bool or thread_id in thread_ids:
                    raise AuditError("GitHub returned a duplicate or malformed review thread")
                thread_ids.add(thread_id)
                threads.append({"id": thread_id, "is_resolved": resolved})
            has_next = page_info.get("hasNextPage")
            end_cursor = page_info.get("endCursor")
            if type(has_next) is not bool:
                raise AuditError("GitHub returned malformed review-thread pagination")
            if not has_next:
                return {
                    "review_decision": decision,
                    "threads": sorted(threads, key=lambda item: item["id"]),
                }
            if (
                type(end_cursor) is not str
                or not end_cursor
                or len(end_cursor) > 1_024
                or end_cursor == cursor
            ):
                raise AuditError("GitHub returned a malformed review-thread cursor")
            cursor = end_cursor
        raise AuditError("GitHub review-thread pagination reached the page ceiling")

    def review_edit_times(
        self, owner: str, name: str, number: int
    ) -> dict[int, Optional[str]]:
        """Return edit timestamps bound to every REST review database ID."""
        query = (
            "query($owner:String!,$repository:String!,$number:Int!,"
            "$endCursor:String){repository(owner:$owner,name:$repository){"
            "pullRequest(number:$number){reviews(first:100,after:$endCursor){"
            "nodes{fullDatabaseId lastEditedAt}"
            "pageInfo{hasNextPage endCursor}}}}}"
        )
        cursor: Optional[str] = None
        edits: dict[int, Optional[str]] = {}
        for page in range(1, MAX_PAGES + 1):
            arguments = [
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                f"owner={owner}",
                "-f",
                f"repository={name}",
                "-F",
                f"number={number}",
            ]
            if cursor is not None:
                arguments.extend(("-f", f"endCursor={cursor}"))
            response = self.api(arguments, f"review edit times page {page}")
            try:
                connection = response["data"]["repository"]["pullRequest"][
                    "reviews"
                ]
                nodes = connection["nodes"]
                page_info = connection["pageInfo"]
            except (KeyError, TypeError) as exc:
                raise AuditError("GitHub returned malformed review-edit data") from exc
            if type(nodes) is not list or len(nodes) > 100 or type(page_info) is not dict:
                raise AuditError("GitHub returned malformed review-edit pagination")
            if len(edits) + len(nodes) > MAX_RECORDS:
                raise AuditError("GitHub returned too many review edit records")
            for index, node in enumerate(nodes):
                if type(node) is not dict or set(node) != {
                    "fullDatabaseId",
                    "lastEditedAt",
                }:
                    raise AuditError("GitHub returned a malformed review edit record")
                raw_identifier = node.get("fullDatabaseId")
                if (
                    type(raw_identifier) is not str
                    or not raw_identifier.isascii()
                    or not raw_identifier.isdecimal()
                    or len(raw_identifier) > 19
                ):
                    raise AuditError(
                        f"review edit record {index} has an invalid database ID"
                    )
                review_id = _positive_id(
                    int(raw_identifier), f"review edit record {index}.fullDatabaseId"
                )
                if review_id in edits:
                    raise AuditError("GitHub returned duplicate review edit records")
                last_edited = node.get("lastEditedAt")
                if last_edited is not None:
                    last_edited = _timestamp(
                        last_edited, f"review edit record {index}.lastEditedAt"
                    )
                edits[review_id] = last_edited
            has_next = page_info.get("hasNextPage")
            end_cursor = page_info.get("endCursor")
            if type(has_next) is not bool:
                raise AuditError("GitHub returned malformed review-edit pagination")
            if not has_next:
                return edits
            if (
                type(end_cursor) is not str
                or not end_cursor
                or len(end_cursor) > 1_024
                or end_cursor == cursor
            ):
                raise AuditError("GitHub returned a malformed review-edit cursor")
            cursor = end_cursor
        raise AuditError("GitHub review-edit pagination reached the page ceiling")


def _normalize_reviews(raw_reviews: list[Any]) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    review_ids: set[int] = set()
    for index, raw in enumerate(raw_reviews):
        if type(raw) is not dict:
            raise AuditError(f"reviews[{index}] is malformed")
        review_id = _positive_id(raw.get("id"), f"reviews[{index}].id")
        if review_id in review_ids:
            raise AuditError("GitHub returned duplicate review IDs")
        review_ids.add(review_id)
        state = raw.get("state")
        if state not in REVIEW_STATES:
            raise AuditError(f"reviews[{index}].state is unknown")
        submitted_at = raw.get("submitted_at")
        if submitted_at is None:
            if state != "PENDING":
                raise AuditError(f"reviews[{index}] has no submission time")
            normalized_time = ""
        else:
            normalized_time = _timestamp(submitted_at, f"reviews[{index}].submitted_at")
        commit_id = raw.get("commit_id")
        if commit_id is None:
            normalized_commit = ""
        else:
            normalized_commit = _sha(commit_id, f"reviews[{index}].commit_id")
        body = raw.get("body")
        if body is None:
            normalized_body = ""
        else:
            normalized_body = _bounded_string(
                body,
                f"reviews[{index}].body",
                maximum=MAX_REVIEW_BODY_CHARS,
                allow_empty=True,
            )
        reviewer = _actor(raw.get("user"), f"reviews[{index}].user")
        reviews.append(
            {
                "id": review_id,
                "state": state,
                "submitted_at": normalized_time,
                "commit_id": normalized_commit,
                "body": normalized_body,
                "reviewer": reviewer,
            }
        )
    return sorted(reviews, key=lambda item: item["id"])


def _bind_review_edit_times(
    reviews: list[dict[str, Any]], edit_times: dict[int, Optional[str]]
) -> None:
    review_ids = {review["id"] for review in reviews}
    if set(edit_times) != review_ids:
        raise AuditError("GitHub REST and GraphQL review identities differ")
    for review in reviews:
        review["last_edited_at"] = edit_times[review["id"]]


def _commit_tree(client: GitHubClient, repository: str, commit: str, label: str) -> str:
    record = client.rest(f"repos/{repository}/git/commits/{commit}", label)
    if type(record) is not dict:
        raise AuditError(f"GitHub returned malformed {label}")
    returned_sha = _sha(record.get("sha"), f"{label}.sha")
    tree = record.get("tree")
    if returned_sha != commit or type(tree) is not dict:
        raise AuditError(f"GitHub returned mismatched {label}")
    return _sha(tree.get("sha"), f"{label}.tree.sha")


def collect_snapshot(
    client: GitHubClient,
    repository: str,
    base_branch: str,
    record_commit: str,
    record_parent: str,
    local_tree: str,
) -> dict[str, Any]:
    """Collect one normalized, bounded proposal-review snapshot."""
    owner, name = repository.split("/", 1)
    repository_record = client.rest(f"repos/{repository}", "repository metadata")
    if type(repository_record) is not dict:
        raise AuditError("GitHub returned malformed repository metadata")
    full_name = _bounded_string(
        repository_record.get("full_name"), "repository.full_name", maximum=128
    )
    repository_owner = _actor(repository_record.get("owner"), "repository.owner")
    if full_name.casefold() != repository.casefold() or repository_owner["login"].casefold() != owner.casefold():
        raise AuditError("GitHub repository identity does not match the proposal")

    main_ref = client.rest(
        f"repos/{repository}/git/ref/heads/{base_branch}",
        "canonical base ref",
    )
    main_object = main_ref.get("object") if type(main_ref) is dict else None
    if type(main_object) is not dict or main_object.get("type") != "commit":
        raise AuditError("GitHub returned malformed canonical base ref")
    canonical_main = _sha(main_object.get("sha"), "canonical base ref SHA")
    comparison = client.rest(
        f"repos/{repository}/compare/{record_commit}...{canonical_main}?per_page=1",
        "proposal ancestry comparison",
    )
    if type(comparison) is not dict:
        raise AuditError("GitHub returned malformed proposal ancestry comparison")
    comparison_status = comparison.get("status")
    merge_base = comparison.get("merge_base_commit")
    if comparison_status not in {"ahead", "behind", "diverged", "identical"} or type(merge_base) is not dict:
        raise AuditError("GitHub returned malformed proposal ancestry status")
    merge_base_sha = _sha(
        merge_base.get("sha"), "proposal ancestry merge-base SHA"
    )

    associations = client.rest_pages(
        f"repos/{repository}/commits/{record_commit}/pulls",
        "associated pull requests",
    )
    numbers: list[int] = []
    for index, association in enumerate(associations):
        if type(association) is not dict:
            raise AuditError(f"associated pull request {index} is malformed")
        number = _positive_id(association.get("number"), f"associations[{index}].number")
        numbers.append(number)
    if len(numbers) != 1 or len(numbers) != len(set(numbers)):
        raise AuditError("proposal record commit must have exactly one associated pull request")
    number = numbers[0]

    pull_request = client.rest(f"repos/{repository}/pulls/{number}", "pull request")
    if type(pull_request) is not dict or pull_request.get("number") != number:
        raise AuditError("GitHub returned malformed pull-request metadata")
    author = _actor(pull_request.get("user"), "pull_request.author")
    head = pull_request.get("head")
    base = pull_request.get("base")
    if type(head) is not dict or type(base) is not dict:
        raise AuditError("GitHub returned malformed pull-request refs")
    head_sha = _sha(head.get("sha"), "pull_request.head.sha")
    merge_commit = _sha(
        pull_request.get("merge_commit_sha"), "pull_request.merge_commit_sha"
    )
    base_ref = _bounded_string(base.get("ref"), "pull_request.base.ref", maximum=255)
    base_sha = _sha(base.get("sha"), "pull_request.base.sha")
    base_repo = base.get("repo")
    if type(base_repo) is not dict:
        raise AuditError("GitHub returned malformed pull-request base repository")
    base_full_name = _bounded_string(
        base_repo.get("full_name"), "pull_request.base.repo.full_name", maximum=128
    )
    state = pull_request.get("state")
    merged_at = pull_request.get("merged_at")
    if state != "closed" or merged_at is None:
        raise AuditError("proposal pull request is not merged")
    normalized_merged_at = _timestamp(merged_at, "pull_request.merged_at")
    if merge_commit != record_commit:
        raise AuditError("proposal pull request does not merge as the record commit")
    if base_ref != base_branch or base_full_name.casefold() != repository.casefold():
        raise AuditError("proposal pull request was not merged into the canonical base")

    requested_reviewers_raw = pull_request.get("requested_reviewers")
    requested_teams_raw = pull_request.get("requested_teams")
    if type(requested_reviewers_raw) is not list or type(requested_teams_raw) is not list:
        raise AuditError("GitHub returned malformed pending-review metadata")
    requested_reviewers = [
        _actor(item, f"requested_reviewers[{index}]")
        for index, item in enumerate(requested_reviewers_raw)
    ]
    requested_reviewer_ids = [item["id"] for item in requested_reviewers]
    if len(requested_reviewer_ids) != len(set(requested_reviewer_ids)):
        raise AuditError("GitHub returned duplicate requested reviewers")
    requested_teams: list[dict[str, Any]] = []
    team_ids: set[int] = set()
    for index, item in enumerate(requested_teams_raw):
        if type(item) is not dict:
            raise AuditError(f"requested_teams[{index}] is malformed")
        team_id = _positive_id(item.get("id"), f"requested_teams[{index}].id")
        slug = _bounded_string(item.get("slug"), f"requested_teams[{index}].slug", maximum=100)
        if team_id in team_ids:
            raise AuditError("GitHub returned duplicate requested teams")
        team_ids.add(team_id)
        requested_teams.append({"id": team_id, "slug": slug})

    reviews = _normalize_reviews(
        client.rest_pages(
            f"repos/{repository}/pulls/{number}/reviews",
            "pull-request reviews",
        )
    )
    review_edit_times = client.review_edit_times(owner, name, number)
    _bind_review_edit_times(reviews, review_edit_times)
    review_state = client.review_state(owner, name, number)

    permission_logins = {
        review["reviewer"]["login"]
        for review in reviews
        if review["reviewer"]["type"] == "User"
        and review["reviewer"]["id"] != author["id"]
        and review["state"] in DECISIVE_REVIEW_STATES
    }
    if len(permission_logins) > MAX_PERMISSION_REVIEWERS:
        raise AuditError("proposal has too many decisive human reviewers to audit")
    permissions: dict[str, str] = {}
    for login in sorted(permission_logins, key=str.casefold):
        encoded = urllib.parse.quote(login, safe="")
        value = client.rest(
            f"repos/{repository}/collaborators/{encoded}/permission",
            f"collaborator permission for {login}",
        )
        permission = value.get("permission") if type(value) is dict else None
        if permission not in ALLOWED_PERMISSIONS:
            raise AuditError("GitHub returned malformed collaborator permission")
        permissions[login] = permission

    record_tree = _commit_tree(
        client, repository, record_commit, "proposal record commit"
    )
    reviewed_tree = _commit_tree(
        client, repository, head_sha, "reviewed pull-request head"
    )
    return {
        "repository": full_name,
        "repository_owner": repository_owner,
        "record_commit": record_commit,
        "record_parent": record_parent,
        "local_tree": local_tree,
        "record_tree": record_tree,
        "canonical_main": canonical_main,
        "main_comparison_status": comparison_status,
        "main_merge_base": merge_base_sha,
        "pull_request": {
            "number": number,
            "author": author,
            "head_sha": head_sha,
            "reviewed_tree": reviewed_tree,
            "merge_commit": merge_commit,
            "merged_at": normalized_merged_at,
            "base_repository": base_full_name,
            "base_ref": base_ref,
            "base_commit": base_sha,
            "requested_reviewers": sorted(
                requested_reviewers,
                key=lambda item: (item["id"], item["login"].casefold()),
            ),
            "requested_teams": sorted(
                requested_teams, key=lambda item: (item["id"], item["slug"])
            ),
            "review_decision": review_state["review_decision"],
            "threads": review_state["threads"],
            "reviews": reviews,
            "permissions": permissions,
        },
    }


def _security_marker_matches(body: str, marker: str, reviewed_sha: str) -> bool:
    meaningful = [line.strip() for line in body.splitlines() if line.strip()]
    return meaningful == [
        marker,
        f"Reviewed-commit: {reviewed_sha}",
        "Verdict: approved",
    ]


def evaluate_snapshot(
    snapshot: dict[str, Any],
    requirements: dict[str, Any],
    *,
    evaluated_at: datetime,
) -> dict[str, Any]:
    """Evaluate a normalized snapshot without performing network calls."""
    if snapshot["repository"].casefold() != requirements["repository"].casefold():
        raise AuditError("snapshot repository does not match review requirements")
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise AuditError("proposal audit time must be timezone-aware")
    evaluated_at = evaluated_at.astimezone(timezone.utc)
    record_commit = _sha(snapshot.get("record_commit"), "snapshot.record_commit")
    record_parent = _sha(snapshot.get("record_parent"), "snapshot.record_parent")
    local_tree = _sha(snapshot.get("local_tree"), "snapshot.local_tree")
    record_tree = _sha(snapshot.get("record_tree"), "snapshot.record_tree")
    if local_tree != record_tree:
        raise AuditError("local proposal record tree differs from GitHub")
    canonical_main = _sha(snapshot.get("canonical_main"), "snapshot.canonical_main")
    comparison_status = snapshot.get("main_comparison_status")
    main_merge_base = _sha(snapshot.get("main_merge_base"), "snapshot.main_merge_base")
    if comparison_status not in {"ahead", "identical"} or main_merge_base != record_commit:
        raise AuditError(
            "proposal record commit is no longer an ancestor of canonical main"
        )
    if comparison_status == "identical" and canonical_main != record_commit:
        raise AuditError("canonical-main comparison is internally inconsistent")
    pull_request = snapshot.get("pull_request")
    if type(pull_request) is not dict:
        raise AuditError("snapshot pull request is malformed")
    if pull_request.get("merge_commit") != record_commit:
        raise AuditError("reviewed pull request is not bound to the proposal record commit")
    if pull_request.get("reviewed_tree") != record_tree:
        raise AuditError("reviewed head and merged proposal trees differ")
    if pull_request.get("base_repository", "").casefold() != requirements["repository"].casefold():
        raise AuditError("reviewed pull request targets a different repository")
    if pull_request.get("base_ref") != requirements["base_branch"]:
        raise AuditError("reviewed pull request targets a different base branch")
    if pull_request.get("base_commit") != record_parent:
        raise AuditError(
            "proposal record parent differs from the reviewed pull-request base"
        )
    if pull_request.get("requested_reviewers") or pull_request.get("requested_teams"):
        raise AuditError("proposal has pending review requests")
    if pull_request.get("review_decision") != "APPROVED":
        raise AuditError("GitHub review decision is not APPROVED")
    threads = pull_request.get("threads")
    if type(threads) is not list or any(
        type(thread) is not dict or thread.get("is_resolved") is not True
        for thread in threads
    ):
        raise AuditError("proposal has unresolved or malformed review threads")
    author = _actor(pull_request.get("author"), "snapshot.pull_request.author")
    merged_at_text = _timestamp(
        pull_request.get("merged_at"), "snapshot.pull_request.merged_at"
    )
    merged_at = _timestamp_datetime(merged_at_text)
    if merged_at > evaluated_at:
        raise AuditError("proposal merge timestamp is in the future")
    reviewed_sha = _sha(pull_request.get("head_sha"), "snapshot.pull_request.head_sha")
    reviews = pull_request.get("reviews")
    permissions = pull_request.get("permissions")
    if type(reviews) is not list or type(permissions) is not dict:
        raise AuditError("snapshot review evidence is malformed")

    decisive_by_reviewer: dict[int, list[dict[str, Any]]] = {}
    reviewer_identity: dict[int, tuple[str, str]] = {}
    review_ids: set[int] = set()
    for index, review in enumerate(reviews):
        if type(review) is not dict:
            raise AuditError(f"snapshot reviews[{index}] is malformed")
        review_id = _positive_id(review.get("id"), f"snapshot.reviews[{index}].id")
        if review_id in review_ids:
            raise AuditError("snapshot contains duplicate review IDs")
        review_ids.add(review_id)
        state = review.get("state")
        if state not in REVIEW_STATES:
            raise AuditError("snapshot contains an unknown review state")
        reviewer = _actor(review.get("reviewer"), f"snapshot.reviews[{index}].reviewer")
        identity = (reviewer["login"].casefold(), reviewer["type"])
        previous_identity = reviewer_identity.setdefault(reviewer["id"], identity)
        if previous_identity != identity:
            raise AuditError("reviewer identity changed within the snapshot")
        if state in DECISIVE_REVIEW_STATES:
            _timestamp(review.get("submitted_at"), f"snapshot.reviews[{index}].submitted_at")
            decisive_by_reviewer.setdefault(reviewer["id"], []).append(review)
        last_edited_at = review.get("last_edited_at")
        if last_edited_at is not None:
            _timestamp(last_edited_at, f"snapshot.reviews[{index}].last_edited_at")

    qualified: list[dict[str, Any]] = []
    security_reviewers: list[str] = []
    marker = requirements["security_review_marker"]
    maximum_age = timedelta(days=requirements["maximum_review_age_days"])
    for reviewer_id, decisive in decisive_by_reviewer.items():
        decisive.sort(
            key=lambda item: (_timestamp_key(item["submitted_at"]), item["id"])
        )
        latest = decisive[-1]
        reviewer = latest["reviewer"]
        if reviewer["type"] != "User" or reviewer_id == author["id"]:
            continue
        if latest["state"] != "APPROVED" or latest.get("commit_id") != reviewed_sha:
            continue
        submitted_at = _timestamp_datetime(latest["submitted_at"])
        if submitted_at >= merged_at or submitted_at > evaluated_at:
            continue
        last_edited_at = latest.get("last_edited_at")
        if (
            last_edited_at is not None
            and _timestamp_datetime(last_edited_at) >= merged_at
        ):
            continue
        if evaluated_at - submitted_at > maximum_age:
            continue
        permission = permissions.get(reviewer["login"])
        if permission not in QUALIFYING_PERMISSIONS:
            continue
        qualified.append(reviewer)
        body = latest.get("body")
        if type(body) is str and _security_marker_matches(body, marker, reviewed_sha):
            security_reviewers.append(reviewer["login"])

    qualified_ids = {item["id"] for item in qualified}
    minimum = requirements["minimum_non_author_reviews"]
    if len(qualified_ids) < minimum:
        raise AuditError(
            f"proposal has {len(qualified_ids)} qualifying exact-head non-author "
            f"reviews; {minimum} are required"
        )
    if requirements["security_review_required"] and not security_reviewers:
        raise AuditError("proposal has no qualifying exact-head security review marker")

    return {
        "pull_request": pull_request["number"],
        "record_commit": record_commit,
        "reviewed_commit": reviewed_sha,
        "tree": record_tree,
        "reviewers": sorted({item["login"] for item in qualified}, key=str.casefold),
        "security_reviewers": sorted(set(security_reviewers), key=str.casefold),
        "unresolved_threads": 0,
        "pending_review_requests": 0,
    }


def _load_checker(repo: Path) -> ModuleType:
    path = repo / "scripts" / "check_semantic_provider_proposal.py"
    source = _read_regular(path, 2 * 1024 * 1024, "proposal checker")
    try:
        code = compile(source, str(path), "exec")
    except (SyntaxError, ValueError) as exc:
        raise AuditError("proposal checker cannot be compiled") from exc
    module = ModuleType("boundver_semantic_provider_proposal_checker_for_audit")
    module.__file__ = str(path)
    exec(code, module.__dict__)
    return module


def _load_requirements(manifest_path: Path) -> dict[str, Any]:
    raw = _read_regular(manifest_path, MAX_JSON_BYTES, "proposal manifest")
    value = _decode_json(raw, "proposal manifest")
    requirements = value.get("review_requirements") if type(value) is dict else None
    if type(requirements) is not dict:
        raise AuditError("proposal manifest has no review requirements")
    expected = {
        "repository",
        "base_branch",
        "minimum_non_author_reviews",
        "maximum_review_age_days",
        "security_review_required",
        "security_review_marker",
        "exact_commit_required",
        "resolved_threads_required",
        "no_pending_review_requests",
        "authoritative_audit",
    }
    if set(requirements) != expected:
        raise AuditError("proposal review requirements are malformed")
    if requirements.get("repository") != "yzm1/boundver":
        raise AuditError("proposal review repository is not authoritative")
    if requirements.get("base_branch") != "main":
        raise AuditError("proposal review base branch is not authoritative")
    minimum = requirements.get("minimum_non_author_reviews")
    if type(minimum) is not int or minimum < 2:
        raise AuditError("proposal requires fewer than two independent reviews")
    if requirements.get("maximum_review_age_days") != 90:
        raise AuditError("proposal review freshness window is not authoritative")
    for field in (
        "security_review_required",
        "exact_commit_required",
        "resolved_threads_required",
        "no_pending_review_requests",
    ):
        if requirements.get(field) is not True:
            raise AuditError(f"proposal review requirement {field} is not fail-closed")
    if requirements.get("security_review_marker") != "semantic-provider-security-review/v1":
        raise AuditError("proposal security-review marker is not authoritative")
    if requirements.get("authoritative_audit") != "scripts/audit_semantic_provider_proposal.py":
        raise AuditError("proposal authoritative-audit path is not fixed")
    return requirements


def _local_record(repo: Path) -> tuple[str, str, str]:
    dirty = _git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        *GOVERNED_PATHS,
    )
    if dirty:
        raise AuditError("governed proposal files have uncommitted changes")
    raw_commit = _git(
        repo,
        "log",
        "-1",
        "--first-parent",
        "--format=%H",
        "HEAD",
        "--",
        *GOVERNED_PATHS,
    ).decode("ascii", "strict").strip()
    record_commit = _sha(raw_commit, "latest governed proposal commit")
    parent = _sha(
        _git(repo, "rev-parse", f"{record_commit}^1").decode("ascii", "strict").strip(),
        "proposal record parent",
    )
    for path in BOOTSTRAP_PATHS:
        before_blob = _sha(
            _git(repo, "rev-parse", f"{parent}:{path}")
            .decode("ascii", "strict")
            .strip(),
            f"parent blob for {path}",
        )
        record_blob = _sha(
            _git(repo, "rev-parse", f"{record_commit}:{path}")
            .decode("ascii", "strict")
            .strip(),
            f"record blob for {path}",
        )
        if before_blob != record_blob:
            raise AuditError(
                "proposal acceptance commit changes bootstrap gate code; "
                "land gate changes while blocked, then use a separate acceptance commit"
            )
    raw_tree = _git(repo, "rev-parse", f"{record_commit}^{{tree}}")
    local_tree = _sha(raw_tree.decode("ascii", "strict").strip(), "local proposal tree")
    return record_commit, parent, local_tree


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--gate",
        choices=("accepted", "v0.15-work", "v0.15-release"),
        default="accepted",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    repo = args.repo.resolve()
    try:
        record_commit, record_parent, local_tree = _local_record(repo)
        with tempfile.TemporaryDirectory(
            prefix="boundver-semantic-proposal-audit-"
        ) as directory:
            validation_root = Path(directory)
            _materialize_validation_tree(repo, record_commit, validation_root)
            manifest_path = validation_root / CANONICAL_MANIFEST
            requirements = _load_requirements(manifest_path)
            client = GitHubClient(repo)
            first = collect_snapshot(
                client,
                requirements["repository"],
                requirements["base_branch"],
                record_commit,
                record_parent,
                local_tree,
            )
            second = collect_snapshot(
                client,
                requirements["repository"],
                requirements["base_branch"],
                record_commit,
                record_parent,
                local_tree,
            )
            if first != second:
                raise AuditError("GitHub proposal-review state changed during the audit")
            evaluated_at = datetime.now(timezone.utc)
            review_result = evaluate_snapshot(
                first,
                requirements,
                evaluated_at=evaluated_at,
            )
            checker = _load_checker(validation_root)
            checker_arguments = {
                "authoritative_review_passed": True,
                "require_accepted": args.gate == "accepted",
                "require_v0_15_work": args.gate == "v0.15-work",
                "require_v0_15_release": args.gate == "v0.15-release",
            }
            proposal_result = checker.validate_proposal(
                validation_root,
                manifest_path,
                **checker_arguments,
            )
        result = {
            "ok": True,
            "proposal": proposal_result["proposal"],
            "gate": args.gate,
            **review_result,
            "snapshot_sha256": _stable_digest(first),
            "verified_at": evaluated_at.isoformat().replace("+00:00", "Z"),
        }
    except (AuditError, OSError, UnicodeError, ValueError) as exc:
        if args.format == "json":
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"Semantic-provider proposal audit failed: {exc}", file=sys.stderr)
        return 1
    if args.format == "json":
        print(json.dumps(result, sort_keys=True))
    else:
        print(
            "Semantic-provider proposal audit passed: "
            f"gate={result['gate']} PR=#{result['pull_request']} "
            f"record={result['record_commit']} reviewers="
            f"{','.join(result['reviewers'])} snapshot={result['snapshot_sha256']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
