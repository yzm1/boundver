#!/usr/bin/env python3
"""Audit authoritative GitHub evidence for the semantic-provider proposal.

The proposal manifest is a declaration, not its own trust root.  This program
finds the latest commit that changed the governed proposal surface and proves
that GitHub merged an identical reviewed tree into ``main``.  It then checks
current, exact-head, non-author review evidence before allowing proposal
acceptance or semantic-provider work. The semantic-provider release gate
additionally proves that a separate release-candidate pull request reviewed the
exact tree being released.

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
MAX_REVIEW_BODY_CHARS = 256 * 1024
MAX_GITHUB_ID = (1 << 63) - 1
MAX_JSON_INTEGER = (1 << 63) - 1
COMMAND_TIMEOUT_SECONDS = 15
AUDIT_TIMEOUT_SECONDS = 90
STREAM_CHUNK_BYTES = 64 * 1024
CANONICAL_MANIFEST = Path("spec/semantic-provider-proposal.json")
SEMANTIC_RELEASE_TAG = "v0.16.0"
SEMANTIC_RELEASE_MARKER = "semantic-provider-v0.16-release-review/v1"
SEMANTIC_PRODUCT_REVIEW_MARKER = "semantic-provider-v0.16-product-review/v1"
PROPOSAL_SECURITY_REVIEW_MARKER = "semantic-provider-security-review/v1"
PROPOSAL_PRODUCT_REVIEW_MARKER = "semantic-provider-product-review/v1"
REVIEWER_INDEPENDENCE_ATTESTATION = "Independent-reviewer: confirmed"
REVIEW_AUTHORITY_SOURCE = "github-account-owned-public-gist/v1"
REVIEW_ROSTER_GIST_ID = "0caedb798d168b974f9d9fb63c377f73"
REVIEW_ROSTER_GIST_NODE_ID = (
    "G_kwDOAVZrFNoAIDBjYWVkYjc5OGQxNjhiOTc0ZjlkOWZiNjNjMzc3Zjcz"
)
REVIEW_ROSTER_GIST_DESCRIPTION = (
    "boundver semantic-provider independent reviewer roster"
)
REVIEW_ROSTER_GIST_FILENAME = "semantic-provider-review-roster.txt"
REVIEW_ROLES = ("product", "security")
REVIEW_ROSTER_FIELDS = {
    "product": "Product-reviewer",
    "security": "Security-reviewer",
}
SEMANTIC_RELEASE_ATTESTATIONS = (
    "Full-source-bug-scan: passed",
    "Full-issue-audit: passed",
    "Full-security-scan: passed",
    "All-blockers: closed",
    "Supported-platforms: passed",
    "Publication-gates: passed",
)
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
DECISIVE_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
REVIEW_STATES = DECISIVE_REVIEW_STATES | {"COMMENTED", "PENDING"}

# A change to any of these files requires a fresh exact-content proposal audit.
# Broader explanatory docs may evolve normally; these files define the design,
# threat traceability, machine gate, and CI enforcement contract.
GOVERNED_PATHS = (
    ".github/workflows/ci.yml",
    ".github/workflows/create-release-tag.yml",
    ".github/workflows/publish.yml",
    "docs/design/semantic-provider-rfc.md",
    "docs/design/semantic-provider-threat-model.md",
    "scripts/audit_semantic_provider_proposal.py",
    "scripts/check_semantic_provider_proposal.py",
    "scripts/publish_release.py",
    "spec/semantic-provider-proposal.json",
    "spec/semantic-provider-proposal.schema.json",
    "tests/test_create_release_tag_review_state.py",
    "tests/test_distribution_contract.py",
    "tests/test_semantic_provider_proposal.py",
)
BOOTSTRAP_PATHS = (
    ".github/workflows/ci.yml",
    ".github/workflows/create-release-tag.yml",
    ".github/workflows/publish.yml",
    "scripts/audit_semantic_provider_proposal.py",
    "scripts/check_semantic_provider_proposal.py",
    "scripts/publish_release.py",
)
VALIDATION_PATHS = (
    ".github/workflows/ci.yml",
    ".github/workflows/create-release-tag.yml",
    ".github/workflows/publish.yml",
    "docs/design/semantic-provider-rfc.md",
    "docs/design/semantic-provider-threat-model.md",
    "scripts/check_semantic_provider_proposal.py",
    "scripts/publish_release.py",
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
        raise AuditError(
            "failed while reading bounded subprocess output"
        ) from read_errors[0]
    if timed_out:
        raise AuditError(f"{command[0]} exceeded the {timeout}-second timeout")
    if overflows:
        stream_name = "stdout" if "stdout" in overflows else "stderr"
        limit = stdout_limit if stream_name == "stdout" else stderr_limit
        raise AuditError(f"{command[0]} {stream_name} exceeds the {limit}-byte limit")
    if returncode != 0:
        diagnostic = captured.get("stderr") or captured.get("stdout") or b""
        detail = diagnostic.decode(
            locale.getpreferredencoding(False), "replace"
        ).strip()
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
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size > limit
    ):
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
    identity = (
        os_fstat.st_dev,
        os_fstat.st_ino,
        os_fstat.st_size,
        os_fstat.st_mtime_ns,
    )
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
        raise AuditError(
            f"reviewed path has a malformed Git tree entry: {path}"
        ) from exc
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
    if (
        type(value) is not str
        or (not value and not allow_empty)
        or len(value) > maximum
    ):
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


def _parse_review_roster_body(
    body: Any,
    *,
    repository_id: int,
    repository_owner_id: int,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Parse the canonical, owner-attested public reviewer roster."""
    text = _bounded_string(body, "review roster gist content", maximum=4_096)
    lines = text.splitlines()
    legacy_unconfigured = [
        "semantic-provider-review-roster/v1",
        f"Repository-id: {repository_id}",
        f"Repository-owner-id: {repository_owner_id}",
        "Security-reviewer: unconfigured",
        "Product-reviewer: unconfigured",
        "Independent-beneficial-owners-attested: false",
        f"Attested-by: {repository_owner_id}:yzm1",
    ]
    if lines == legacy_unconfigured and text == "\n".join(lines):
        raise AuditError("semantic review roster is not configured and attested")
    if text != "\n".join(lines) or len(lines) != 8:
        raise AuditError("semantic review roster body is not canonical")
    expected_literals = {
        0: "semantic-provider-review-roster/v2",
        1: f"Repository-id: {repository_id}",
        2: f"Repository-owner-id: {repository_owner_id}",
        5: "Independent-beneficial-owners-attested: true",
        6: "Owner-exclusive-mutation-authority-attested: true",
        7: f"Attested-by: {repository_owner_id}:yzm1",
    }
    if any(lines[index] != expected for index, expected in expected_literals.items()):
        if (
            lines[3] == "Security-reviewer: unconfigured"
            or lines[4] == "Product-reviewer: unconfigured"
            or lines[5] == "Independent-beneficial-owners-attested: false"
            or lines[6] == "Owner-exclusive-mutation-authority-attested: false"
        ):
            raise AuditError("semantic review roster is not configured and attested")
        raise AuditError("semantic review roster body is not authoritative")

    reviewers: dict[str, dict[str, Any]] = {}
    for role, line_index in (("security", 3), ("product", 4)):
        prefix = f"{REVIEW_ROSTER_FIELDS[role]}: "
        line = lines[line_index]
        if not line.startswith(prefix):
            raise AuditError(f"semantic review roster has no canonical {role} entry")
        raw_identity = line[len(prefix) :]
        if raw_identity == "unconfigured":
            raise AuditError("semantic review roster is not configured and attested")
        identifier, separator, login = raw_identity.partition(":")
        if (
            separator != ":"
            or not identifier.isascii()
            or not identifier.isdecimal()
            or len(identifier) > 19
            or USER_LOGIN_RE.fullmatch(login) is None
        ):
            raise AuditError(f"semantic review roster {role} identity is malformed")
        reviewer_id = _positive_id(int(identifier), f"review roster {role} reviewer.id")
        if str(reviewer_id) != identifier:
            raise AuditError(f"semantic review roster {role} identity is not canonical")
        reviewers[role] = {"id": reviewer_id, "login": login, "type": "User"}
    if reviewers["security"]["id"] == reviewers["product"]["id"]:
        raise AuditError("semantic review roster must name distinct human reviewers")
    if reviewers["security"]["login"].casefold() == reviewers["product"][
        "login"
    ].casefold():
        raise AuditError("semantic review roster binds one login to multiple IDs")
    return text, reviewers


class GitHubClient:
    """Small, bounded GitHub API client backed by the authenticated gh CLI."""

    def __init__(self, repo_root: Path, *, gh_executable: Optional[str] = None) -> None:
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
                raise AuditError(
                    "GitHub returned malformed review-thread data"
                ) from exc
            page_decision = pull_request.get("reviewDecision")
            if page_decision not in {
                None,
                "APPROVED",
                "CHANGES_REQUESTED",
                "REVIEW_REQUIRED",
            }:
                raise AuditError("GitHub returned an unknown review decision")
            if not decision_seen:
                decision = page_decision
                decision_seen = True
            elif decision != page_decision:
                raise AuditError("GitHub review decision changed during pagination")
            if (
                type(nodes) is not list
                or len(nodes) > 100
                or type(page_info) is not dict
            ):
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
                    raise AuditError(
                        "GitHub returned a duplicate or malformed review thread"
                    )
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
                connection = response["data"]["repository"]["pullRequest"]["reviews"]
                nodes = connection["nodes"]
                page_info = connection["pageInfo"]
            except (KeyError, TypeError) as exc:
                raise AuditError("GitHub returned malformed review-edit data") from exc
            if (
                type(nodes) is not list
                or len(nodes) > 100
                or type(page_info) is not dict
            ):
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


def _collect_review_authority(
    client: GitHubClient,
    requirements: dict[str, Any],
) -> dict[str, Any]:
    """Read the account-owned public roster without granting repository access."""
    repository = requirements["repository"]
    gist_id = requirements["review_roster_gist_id"]
    gist = client.rest(
        f"gists/{gist_id}",
        "semantic review roster gist",
    )
    expected_owner = {
        "id": requirements["repository_owner_id"],
        "login": "yzm1",
        "type": "User",
    }
    expected_owner_permissions = {
        "admin": True,
        "maintain": True,
        "push": True,
        "triage": True,
        "pull": True,
    }
    collaborator_records = client.rest_pages(
        f"repos/{repository}/collaborators",
        "repository collaborators",
    )
    if len(collaborator_records) != 1 or type(collaborator_records[0]) is not dict:
        raise AuditError("repository mutation authority is not owner-exclusive")
    collaborator_actor = _actor(
        collaborator_records[0], "repository collaborator"
    )
    collaborator_permissions = collaborator_records[0].get("permissions")
    if (
        requirements.get("owner_exclusive_repository_collaborators_required")
        is not True
        or collaborator_actor != expected_owner
        or collaborator_records[0].get("role_name") != "admin"
        or type(collaborator_permissions) is not dict
        or set(collaborator_permissions) != set(expected_owner_permissions)
        or any(
            type(collaborator_permissions[field]) is not bool
            or collaborator_permissions[field] is not expected
            for field, expected in expected_owner_permissions.items()
        )
    ):
        raise AuditError("repository mutation authority is not owner-exclusive")
    repository_mutation_authority = {
        "owner": expected_owner,
        "owner_attested_exclusive_mutation_authority": True,
        "repository_collaborators": [
            {
                "actor": collaborator_actor,
                "role_name": "admin",
                "permissions": expected_owner_permissions,
            }
        ],
    }

    def normalize_gist_record(
        record: Any,
        *,
        label: str,
        expected_url: str,
        expected_revision: Optional[str] = None,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        if type(record) is not dict:
            raise AuditError(f"GitHub returned malformed {label}")
        returned_id = _bounded_string(record.get("id"), f"{label}.id", maximum=64)
        node_id = _bounded_string(
            record.get("node_id"), f"{label}.node_id", maximum=128
        )
        url = _bounded_string(record.get("url"), f"{label}.url", maximum=512)
        html_url = _bounded_string(
            record.get("html_url"), f"{label}.html_url", maximum=512
        )
        owner = _actor(record.get("owner"), f"{label}.owner")
        if (
            returned_id != gist_id
            or node_id != requirements["review_roster_gist_node_id"]
            or url != expected_url
            or html_url != f"https://gist.github.com/yzm1/{gist_id}"
            or owner != expected_owner
            or record.get("public") is not True
            or record.get("user") is not None
            or record.get("truncated") is not False
            or record.get("description") != REVIEW_ROSTER_GIST_DESCRIPTION
        ):
            raise AuditError(f"{label} identity is not authoritative")
        created_at = _timestamp(record.get("created_at"), f"{label}.created_at")
        updated_at = _timestamp(record.get("updated_at"), f"{label}.updated_at")
        if _timestamp_datetime(created_at) > _timestamp_datetime(updated_at):
            raise AuditError(f"{label} timestamps are inconsistent")

        history = record.get("history")
        if type(history) is not list or not history or len(history) > 100:
            raise AuditError(f"{label} revision history is malformed or excessive")
        latest = history[0]
        if type(latest) is not dict:
            raise AuditError(f"{label} latest revision is malformed")
        version = _sha(latest.get("version"), f"{label}.history[0].version")
        committed_at = _timestamp(
            latest.get("committed_at"), f"{label}.history[0].committed_at"
        )
        revision_owner = _actor(
            latest.get("user"), f"{label}.history[0].user"
        )
        change_status = latest.get("change_status")
        if (
            (expected_revision is not None and version != expected_revision)
            or latest.get("url") != f"https://api.github.com/gists/{gist_id}/{version}"
            or revision_owner != expected_owner
            or _timestamp_datetime(committed_at) > _timestamp_datetime(updated_at)
            or type(change_status) is not dict
            or set(change_status) != {"total", "additions", "deletions"}
            or any(
                type(change_status[field]) is not int or change_status[field] < 0
                for field in change_status
            )
            or change_status.get("total")
            != change_status.get("additions") + change_status.get("deletions")
        ):
            raise AuditError(f"{label} latest revision is not owner-authored")

        files = record.get("files")
        if type(files) is not dict or set(files) != {REVIEW_ROSTER_GIST_FILENAME}:
            raise AuditError(f"{label} must contain exactly the roster file")
        file_record = files[REVIEW_ROSTER_GIST_FILENAME]
        expected_file_fields = {
            "filename",
            "type",
            "language",
            "raw_url",
            "size",
            "truncated",
            "content",
            "encoding",
        }
        if type(file_record) is not dict or set(file_record) != expected_file_fields:
            raise AuditError(f"{label} roster file metadata is malformed")
        content = _bounded_string(
            file_record.get("content"), f"{label}.files.roster.content", maximum=4_096
        )
        size = file_record.get("size")
        raw_url = file_record.get("raw_url")
        raw_pattern = re.compile(
            rf"^https://gist\.githubusercontent\.com/yzm1/{re.escape(gist_id)}/"
            rf"raw/[0-9a-f]{{40}}/{re.escape(REVIEW_ROSTER_GIST_FILENAME)}$"
        )
        if (
            file_record.get("filename") != REVIEW_ROSTER_GIST_FILENAME
            or file_record.get("type") != "text/plain"
            or file_record.get("language") != "Text"
            or file_record.get("encoding") != "utf-8"
            or file_record.get("truncated") is not False
            or type(size) is not int
            or size != len(content.encode("utf-8"))
            or type(raw_url) is not str
            or raw_pattern.fullmatch(raw_url) is None
        ):
            raise AuditError(f"{label} roster file is not canonical and complete")
        body, configured = _parse_review_roster_body(
            content,
            repository_id=requirements["repository_id"],
            repository_owner_id=requirements["repository_owner_id"],
        )
        return (
            {
                "id": returned_id,
                "node_id": node_id,
                "description": REVIEW_ROSTER_GIST_DESCRIPTION,
                "url": url,
                "html_url": html_url,
                "owner": owner,
                "public": True,
                "created_at": created_at,
                "updated_at": updated_at,
                "latest_revision": {
                    "version": version,
                    "committed_at": committed_at,
                    "owner": revision_owner,
                    "url": latest["url"],
                    "change_status": change_status,
                },
                "file": {
                    "filename": REVIEW_ROSTER_GIST_FILENAME,
                    "type": "text/plain",
                    "language": "Text",
                    "raw_url": raw_url,
                    "size": size,
                    "truncated": False,
                    "content": body,
                    "encoding": "utf-8",
                },
            },
            configured,
        )

    current, configured_reviewers = normalize_gist_record(
        gist,
        label="semantic review roster gist",
        expected_url=f"https://api.github.com/gists/{gist_id}",
    )
    revision = current["latest_revision"]["version"]
    immutable_record = client.rest(
        f"gists/{gist_id}/{revision}",
        "semantic review roster gist revision",
    )
    immutable, immutable_reviewers = normalize_gist_record(
        immutable_record,
        label="semantic review roster gist revision",
        expected_url=f"https://api.github.com/gists/{gist_id}/{revision}",
        expected_revision=revision,
    )
    current_without_url = {key: value for key, value in current.items() if key != "url"}
    immutable_without_url = {
        key: value for key, value in immutable.items() if key != "url"
    }
    if (
        current_without_url != immutable_without_url
        or configured_reviewers != immutable_reviewers
    ):
        raise AuditError("semantic review roster gist changed during collection")

    reviewers: dict[str, dict[str, Any]] = {}
    for role in REVIEW_ROLES:
        reviewer = configured_reviewers[role]
        encoded_reviewer = urllib.parse.quote(reviewer["login"], safe="")
        permission_record = client.rest(
            f"repos/{repository}/collaborators/{encoded_reviewer}/permission",
            f"{role} reviewer repository permission",
        )
        permission_user = (
            permission_record.get("user") if type(permission_record) is dict else None
        )
        permission_actor = _actor(
            permission_user, f"{role} reviewer repository permission.user"
        )
        permission_flags = (
            permission_user.get("permissions")
            if type(permission_user) is dict
            else None
        )
        expected_flags = {
            "admin": False,
            "maintain": False,
            "push": False,
            "triage": False,
            "pull": True,
        }
        if (
            permission_actor != reviewer
            or permission_record.get("permission") != "read"
            or permission_record.get("role_name") != "read"
            or type(permission_flags) is not dict
            or set(permission_flags) != set(expected_flags)
            or any(
                type(permission_flags[field]) is not bool
                or permission_flags[field] is not expected
                for field, expected in expected_flags.items()
            )
        ):
            raise AuditError(f"{role} reviewer has repository mutation authority")
        reviewers[role] = {
            "reviewer": reviewer,
            "repository_permission": {
                "permission": "read",
                "role_name": "read",
                "permissions": expected_flags,
            },
        }
    return {
        "source": REVIEW_AUTHORITY_SOURCE,
        "roster": current,
        "reviewers": reviewers,
        "repository_mutation_authority": repository_mutation_authority,
    }


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
    requirements: dict[str, Any],
    record_commit: str,
    record_parent: str,
    local_tree: str,
) -> dict[str, Any]:
    """Collect one normalized, bounded proposal-review snapshot."""
    repository = requirements["repository"]
    base_branch = requirements["base_branch"]
    owner, name = repository.split("/", 1)
    repository_record = client.rest(f"repos/{repository}", "repository metadata")
    if type(repository_record) is not dict:
        raise AuditError("GitHub returned malformed repository metadata")
    full_name = _bounded_string(
        repository_record.get("full_name"), "repository.full_name", maximum=128
    )
    repository_id = _positive_id(repository_record.get("id"), "repository.id")
    repository_owner = _actor(repository_record.get("owner"), "repository.owner")
    if (
        full_name.casefold() != repository.casefold()
        or repository_owner["login"].casefold() != owner.casefold()
    ):
        raise AuditError("GitHub repository identity does not match the proposal")
    review_authority = _collect_review_authority(client, requirements)

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
    if (
        comparison_status not in {"ahead", "behind", "diverged", "identical"}
        or type(merge_base) is not dict
    ):
        raise AuditError("GitHub returned malformed proposal ancestry status")
    merge_base_sha = _sha(merge_base.get("sha"), "proposal ancestry merge-base SHA")

    associations = client.rest_pages(
        f"repos/{repository}/commits/{record_commit}/pulls",
        "associated pull requests",
    )
    numbers: list[int] = []
    for index, association in enumerate(associations):
        if type(association) is not dict:
            raise AuditError(f"associated pull request {index} is malformed")
        number = _positive_id(
            association.get("number"), f"associations[{index}].number"
        )
        numbers.append(number)
    if len(numbers) != 1 or len(numbers) != len(set(numbers)):
        raise AuditError(
            "proposal record commit must have exactly one associated pull request"
        )
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
    if (
        type(requested_reviewers_raw) is not list
        or type(requested_teams_raw) is not list
    ):
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
        slug = _bounded_string(
            item.get("slug"), f"requested_teams[{index}].slug", maximum=100
        )
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

    record_tree = _commit_tree(
        client, repository, record_commit, "proposal record commit"
    )
    reviewed_tree = _commit_tree(
        client, repository, head_sha, "reviewed pull-request head"
    )
    return {
        "repository": full_name,
        "repository_id": repository_id,
        "repository_owner": repository_owner,
        "review_authority": review_authority,
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
        },
    }


def _review_marker_matches(
    body: str,
    marker: str,
    reviewed_sha: str,
    independence_attestation: str,
    attestations: Sequence[str] = (),
) -> bool:
    meaningful: list[str] = []
    for line in body.splitlines():
        if not line.strip():
            continue
        if line != line.strip():
            return False
        meaningful.append(line)
    return meaningful == [
        marker,
        f"Reviewed-commit: {reviewed_sha}",
        independence_attestation,
        *attestations,
        "Verdict: approved",
    ]


def _evaluate_review_authority(
    snapshot: dict[str, Any],
    requirements: dict[str, Any],
    *,
    repository_owner: dict[str, Any],
    pull_request_author: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    authority = snapshot.get("review_authority")
    if type(authority) is not dict or set(authority) != {
        "source",
        "roster",
        "reviewers",
        "repository_mutation_authority",
    }:
        raise AuditError("snapshot review authority is malformed")
    if authority.get("source") != requirements["reviewer_authority"]:
        raise AuditError("snapshot review authority source is not authoritative")
    roster = authority.get("roster")
    expected_roster_fields = {
        "id",
        "node_id",
        "description",
        "url",
        "html_url",
        "owner",
        "public",
        "created_at",
        "updated_at",
        "latest_revision",
        "file",
    }
    if type(roster) is not dict or set(roster) != expected_roster_fields:
        raise AuditError("snapshot review roster gist is malformed")
    gist_id = requirements["review_roster_gist_id"]
    expected_owner = {
        "id": requirements["repository_owner_id"],
        "login": "yzm1",
        "type": "User",
    }
    mutation_authority = authority.get("repository_mutation_authority")
    if type(mutation_authority) is not dict or set(mutation_authority) != {
        "owner",
        "owner_attested_exclusive_mutation_authority",
        "repository_collaborators",
    }:
        raise AuditError("snapshot repository mutation authority is malformed")
    collaborators = mutation_authority.get("repository_collaborators")
    if type(collaborators) is not list or len(collaborators) != 1:
        raise AuditError("snapshot repository mutation authority is not exclusive")
    collaborator = collaborators[0]
    expected_owner_permissions = {
        "admin": True,
        "maintain": True,
        "push": True,
        "triage": True,
        "pull": True,
    }
    permissions = collaborator.get("permissions") if type(collaborator) is dict else None
    if (
        requirements.get("owner_exclusive_repository_collaborators_required")
        is not True
        or requirements.get("owner_exclusive_mutation_authority_attestation_required")
        is not True
        or _actor(
            mutation_authority.get("owner"),
            "snapshot.repository_mutation_authority.owner",
        )
        != expected_owner
        or mutation_authority.get("owner_attested_exclusive_mutation_authority")
        is not True
        or type(collaborator) is not dict
        or set(collaborator) != {"actor", "role_name", "permissions"}
        or _actor(
            collaborator.get("actor"),
            "snapshot.repository_mutation_authority.repository_collaborator",
        )
        != expected_owner
        or collaborator.get("role_name") != "admin"
        or type(permissions) is not dict
        or set(permissions) != set(expected_owner_permissions)
        or any(
            type(permissions[field]) is not bool
            or permissions[field] is not expected
            for field, expected in expected_owner_permissions.items()
        )
    ):
        raise AuditError("snapshot repository mutation authority is not owner-exclusive")
    if (
        _bounded_string(roster.get("id"), "snapshot.review_roster.id", maximum=64)
        != gist_id
        or _bounded_string(
            roster.get("node_id"), "snapshot.review_roster.node_id", maximum=128
        )
        != requirements["review_roster_gist_node_id"]
        or roster.get("description") != REVIEW_ROSTER_GIST_DESCRIPTION
        or roster.get("url") != f"https://api.github.com/gists/{gist_id}"
        or roster.get("html_url") != f"https://gist.github.com/yzm1/{gist_id}"
        or _actor(roster.get("owner"), "snapshot.review_roster.owner")
        != expected_owner
        or roster.get("public") is not True
    ):
        raise AuditError("snapshot review roster gist identity changed")
    created_at_text = _timestamp(
        roster.get("created_at"), "snapshot.review_roster.created_at"
    )
    updated_at_text = _timestamp(
        roster.get("updated_at"), "snapshot.review_roster.updated_at"
    )
    created_at = _timestamp_datetime(created_at_text)
    updated_at = _timestamp_datetime(updated_at_text)
    if created_at > updated_at:
        raise AuditError("snapshot review roster gist timestamps conflict")

    revision = roster.get("latest_revision")
    expected_revision_fields = {
        "version",
        "committed_at",
        "owner",
        "url",
        "change_status",
    }
    if type(revision) is not dict or set(revision) != expected_revision_fields:
        raise AuditError("snapshot review roster revision is malformed")
    version = _sha(revision.get("version"), "snapshot.review_roster.revision.version")
    committed_at_text = _timestamp(
        revision.get("committed_at"),
        "snapshot.review_roster.revision.committed_at",
    )
    change_status = revision.get("change_status")
    if (
        _actor(revision.get("owner"), "snapshot.review_roster.revision.owner")
        != expected_owner
        or revision.get("url") != f"https://api.github.com/gists/{gist_id}/{version}"
        or _timestamp_datetime(committed_at_text) > updated_at
        or type(change_status) is not dict
        or set(change_status) != {"total", "additions", "deletions"}
        or any(
            type(change_status[field]) is not int or change_status[field] < 0
            for field in change_status
        )
        or change_status.get("total")
        != change_status.get("additions") + change_status.get("deletions")
    ):
        raise AuditError("snapshot review roster revision is not owner-authored")

    file_record = roster.get("file")
    expected_file_fields = {
        "filename",
        "type",
        "language",
        "raw_url",
        "size",
        "truncated",
        "content",
        "encoding",
    }
    if type(file_record) is not dict or set(file_record) != expected_file_fields:
        raise AuditError("snapshot review roster file metadata is malformed")
    content = _bounded_string(
        file_record.get("content"),
        "snapshot.review_roster.file.content",
        maximum=4_096,
    )
    size = file_record.get("size")
    raw_url = file_record.get("raw_url")
    raw_pattern = re.compile(
        rf"^https://gist\.githubusercontent\.com/yzm1/{re.escape(gist_id)}/"
        rf"raw/[0-9a-f]{{40}}/{re.escape(REVIEW_ROSTER_GIST_FILENAME)}$"
    )
    if (
        file_record.get("filename") != REVIEW_ROSTER_GIST_FILENAME
        or file_record.get("type") != "text/plain"
        or file_record.get("language") != "Text"
        or file_record.get("encoding") != "utf-8"
        or file_record.get("truncated") is not False
        or type(size) is not int
        or size != len(content.encode("utf-8"))
        or type(raw_url) is not str
        or raw_pattern.fullmatch(raw_url) is None
    ):
        raise AuditError("snapshot review roster file is not canonical and complete")
    _, configured_reviewers = _parse_review_roster_body(
        content,
        repository_id=requirements["repository_id"],
        repository_owner_id=requirements["repository_owner_id"],
    )
    reviewers = authority.get("reviewers")
    if type(reviewers) is not dict or set(reviewers) != set(REVIEW_ROLES):
        raise AuditError("snapshot review roster identities are malformed")

    result: dict[int, dict[str, Any]] = {}
    actor_id_by_login: dict[tuple[str, str], int] = {}
    for actor in (repository_owner, pull_request_author):
        identity = (actor["login"].casefold(), actor["type"])
        previous_id = actor_id_by_login.setdefault(identity, actor["id"])
        if previous_id != actor["id"]:
            raise AuditError("actor login is bound to multiple GitHub IDs")
    for role in REVIEW_ROLES:
        entry = reviewers.get(role)
        if type(entry) is not dict or set(entry) != {
            "reviewer",
            "repository_permission",
        }:
            raise AuditError(f"snapshot {role} review roster entry is malformed")
        reviewer = _actor(entry.get("reviewer"), f"snapshot.{role}_reviewer")
        if reviewer != configured_reviewers[role]:
            raise AuditError(f"snapshot {role} reviewer differs from roster body")
        permission = entry.get("repository_permission")
        expected_flags = {
            "admin": False,
            "maintain": False,
            "push": False,
            "triage": False,
            "pull": True,
        }
        flags = permission.get("permissions") if type(permission) is dict else None
        if (
            type(permission) is not dict
            or set(permission) != {"permission", "role_name", "permissions"}
            or permission.get("permission") != "read"
            or permission.get("role_name") != "read"
            or type(flags) is not dict
            or set(flags) != set(expected_flags)
            or any(
                type(flags[field]) is not bool or flags[field] is not expected
                for field, expected in expected_flags.items()
            )
        ):
            raise AuditError(f"snapshot {role} reviewer is not read-only")
        if reviewer["type"] != "User":
            raise AuditError("review roster must designate human reviewers")
        if reviewer["id"] in {repository_owner["id"], pull_request_author["id"]}:
            raise AuditError(
                "review roster must designate external non-author reviewers"
            )
        identity = (reviewer["login"].casefold(), reviewer["type"])
        previous_id = actor_id_by_login.setdefault(identity, reviewer["id"])
        if previous_id != reviewer["id"] or reviewer["id"] in result:
            raise AuditError("review roster must designate distinct identities")
        result[reviewer["id"]] = {
            "role": role,
            "reviewer": reviewer,
            "authority_updated_at": updated_at,
        }
    if (
        requirements.get("distinct_roster_reviewers_required") is not True
        or len(result) != requirements["minimum_non_author_reviews"]
    ):
        raise AuditError("review authority does not contain two distinct reviewers")
    return result


def evaluate_snapshot(
    snapshot: dict[str, Any],
    requirements: dict[str, Any],
    *,
    evaluated_at: datetime,
    attestations: Sequence[str] = (),
) -> dict[str, Any]:
    """Evaluate a normalized snapshot without performing network calls."""
    if snapshot["repository"].casefold() != requirements["repository"].casefold():
        raise AuditError("snapshot repository does not match review requirements")
    if snapshot.get("repository_id") != requirements["repository_id"]:
        raise AuditError("snapshot repository ID does not match review requirements")
    repository_owner = _actor(
        snapshot.get("repository_owner"), "snapshot.repository_owner"
    )
    if repository_owner["id"] != requirements["repository_owner_id"]:
        raise AuditError(
            "snapshot repository owner ID does not match review requirements"
        )
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
    if (
        comparison_status not in {"ahead", "identical"}
        or main_merge_base != record_commit
    ):
        raise AuditError(
            "proposal record commit is no longer an ancestor of canonical main"
        )
    if comparison_status == "identical" and canonical_main != record_commit:
        raise AuditError("canonical-main comparison is internally inconsistent")
    pull_request = snapshot.get("pull_request")
    if type(pull_request) is not dict:
        raise AuditError("snapshot pull request is malformed")
    if pull_request.get("merge_commit") != record_commit:
        raise AuditError(
            "reviewed pull request is not bound to the proposal record commit"
        )
    if pull_request.get("reviewed_tree") != record_tree:
        raise AuditError("reviewed head and merged proposal trees differ")
    if (
        pull_request.get("base_repository", "").casefold()
        != requirements["repository"].casefold()
    ):
        raise AuditError("reviewed pull request targets a different repository")
    if pull_request.get("base_ref") != requirements["base_branch"]:
        raise AuditError("reviewed pull request targets a different base branch")
    if pull_request.get("base_commit") != record_parent:
        raise AuditError(
            "proposal record parent differs from the reviewed pull-request base"
        )
    if pull_request.get("requested_reviewers") or pull_request.get("requested_teams"):
        raise AuditError("proposal has pending review requests")
    if pull_request.get("review_decision") not in {None, "APPROVED"}:
        raise AuditError("GitHub aggregate review decision is blocking")
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
    if type(reviews) is not list:
        raise AuditError("snapshot review evidence is malformed")
    designated = _evaluate_review_authority(
        snapshot,
        requirements,
        repository_owner=repository_owner,
        pull_request_author=author,
    )

    decisive_by_reviewer: dict[int, list[dict[str, Any]]] = {}
    reviewer_identity: dict[int, tuple[str, str]] = {}
    actor_id_by_login: dict[tuple[str, str], int] = {}
    for actor in (repository_owner, author):
        identity = (actor["login"].casefold(), actor["type"])
        previous_id = actor_id_by_login.setdefault(identity, actor["id"])
        if previous_id != actor["id"]:
            raise AuditError("actor login is bound to multiple GitHub IDs")
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
        previous_id = actor_id_by_login.setdefault(identity, reviewer["id"])
        if previous_id != reviewer["id"]:
            raise AuditError("actor login is bound to multiple GitHub IDs")
        if state in DECISIVE_REVIEW_STATES:
            _timestamp(
                review.get("submitted_at"), f"snapshot.reviews[{index}].submitted_at"
            )
            decisive_by_reviewer.setdefault(reviewer["id"], []).append(review)
        last_edited_at = review.get("last_edited_at")
        if last_edited_at is not None:
            _timestamp(last_edited_at, f"snapshot.reviews[{index}].last_edited_at")

    qualified: list[dict[str, Any]] = []
    qualified_records: list[dict[str, Any]] = []
    security_reviewers: list[str] = []
    product_reviewers: list[str] = []
    maximum_age = timedelta(days=requirements["maximum_review_age_days"])
    for reviewer_id, authority in designated.items():
        decisive = decisive_by_reviewer.get(reviewer_id, [])
        if not decisive:
            continue
        decisive.sort(
            key=lambda item: (_timestamp_key(item["submitted_at"]), item["id"])
        )
        latest = decisive[-1]
        reviewer = latest["reviewer"]
        if reviewer != authority["reviewer"]:
            raise AuditError("designated reviewer identity changed in review evidence")
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
        if submitted_at <= authority["authority_updated_at"]:
            continue
        body = latest.get("body")
        role = authority["role"]
        role_attestations = attestations if role == "security" else ()
        is_role_review = type(body) is str and _review_marker_matches(
            body,
            requirements[f"{role}_review_marker"],
            reviewed_sha,
            requirements["reviewer_independence_attestation"],
            role_attestations,
        )
        if not is_role_review:
            continue
        qualified.append(reviewer)
        qualified_records.append(
            {
                "reviewer": reviewer,
                "valid_until": submitted_at + maximum_age,
                "role": role,
            }
        )
        if role == "security":
            security_reviewers.append(reviewer["login"])
        if role == "product":
            product_reviewers.append(reviewer["login"])

    qualified_ids = {item["id"] for item in qualified}
    minimum = requirements["minimum_non_author_reviews"]
    if requirements["security_review_required"] and not security_reviewers:
        raise AuditError("proposal has no qualifying exact-head security review marker")
    if requirements["product_review_required"] and not product_reviewers:
        raise AuditError("proposal has no qualifying exact-head product review marker")
    if len(qualified_ids) < minimum:
        raise AuditError(
            f"proposal has {len(qualified_ids)} qualifying exact-head non-author "
            f"reviews; {minimum} are required"
        )
    validity_candidates = {item["valid_until"] for item in qualified_records}
    valid_until = max(
        candidate
        for candidate in validity_candidates
        if sum(item["valid_until"] >= candidate for item in qualified_records)
        >= minimum
        and any(
            item["role"] == "security" and item["valid_until"] >= candidate
            for item in qualified_records
        )
        and any(
            item["role"] == "product" and item["valid_until"] >= candidate
            for item in qualified_records
        )
    )

    return {
        "pull_request": pull_request["number"],
        "record_commit": record_commit,
        "reviewed_commit": reviewed_sha,
        "tree": record_tree,
        "reviewers": sorted({item["login"] for item in qualified}, key=str.casefold),
        "security_reviewers": sorted(set(security_reviewers), key=str.casefold),
        "product_reviewers": sorted(set(product_reviewers), key=str.casefold),
        "valid_until": valid_until.isoformat().replace("+00:00", "Z"),
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
        "repository_id",
        "repository_owner_id",
        "base_branch",
        "reviewer_authority",
        "review_roster_gist_id",
        "review_roster_gist_node_id",
        "review_roster_gist_description",
        "review_roster_gist_filename",
        "distinct_roster_reviewers_required",
        "owner_exclusive_repository_collaborators_required",
        "owner_exclusive_mutation_authority_attestation_required",
        "minimum_non_author_reviews",
        "maximum_review_age_days",
        "security_review_required",
        "product_review_required",
        "security_review_marker",
        "product_review_marker",
        "reviewer_independence_attestation",
        "exact_commit_required",
        "resolved_threads_required",
        "no_pending_review_requests",
        "authoritative_audit",
    }
    if set(requirements) != expected:
        raise AuditError("proposal review requirements are malformed")
    if requirements.get("repository") != "yzm1/boundver":
        raise AuditError("proposal review repository is not authoritative")
    if (
        requirements.get("repository_id") != 1226008327
        or type(requirements.get("repository_id")) is not int
    ):
        raise AuditError("proposal review repository ID is not authoritative")
    if (
        requirements.get("repository_owner_id") != 22440724
        or type(requirements.get("repository_owner_id")) is not int
    ):
        raise AuditError("proposal review repository owner ID is not authoritative")
    if requirements.get("base_branch") != "main":
        raise AuditError("proposal review base branch is not authoritative")
    if requirements.get("reviewer_authority") != REVIEW_AUTHORITY_SOURCE:
        raise AuditError("proposal reviewer authority is not authoritative")
    expected_roster_identity = {
        "review_roster_gist_id": REVIEW_ROSTER_GIST_ID,
        "review_roster_gist_node_id": REVIEW_ROSTER_GIST_NODE_ID,
        "review_roster_gist_description": REVIEW_ROSTER_GIST_DESCRIPTION,
        "review_roster_gist_filename": REVIEW_ROSTER_GIST_FILENAME,
    }
    if any(
        requirements.get(field) != expected
        or type(requirements.get(field)) is not type(expected)
        for field, expected in expected_roster_identity.items()
    ):
        raise AuditError("proposal reviewer roster identity is not authoritative")
    if requirements.get("distinct_roster_reviewers_required") is not True:
        raise AuditError("proposal reviewer roster needs distinct humans")
    if (
        requirements.get("owner_exclusive_repository_collaborators_required")
        is not True
    ):
        raise AuditError("proposal release mutation authority must be owner-exclusive")
    if (
        requirements.get("owner_exclusive_mutation_authority_attestation_required")
        is not True
    ):
        raise AuditError("proposal release mutation attestation must remain required")
    minimum = requirements.get("minimum_non_author_reviews")
    if type(minimum) is not int or minimum < 2:
        raise AuditError("proposal requires fewer than two independent reviews")
    if requirements.get("maximum_review_age_days") != 90:
        raise AuditError("proposal review freshness window is not authoritative")
    for field in (
        "security_review_required",
        "product_review_required",
        "exact_commit_required",
        "resolved_threads_required",
        "no_pending_review_requests",
    ):
        if requirements.get(field) is not True:
            raise AuditError(f"proposal review requirement {field} is not fail-closed")
    expected_review_text = {
        "security_review_marker": PROPOSAL_SECURITY_REVIEW_MARKER,
        "product_review_marker": PROPOSAL_PRODUCT_REVIEW_MARKER,
        "reviewer_independence_attestation": REVIEWER_INDEPENDENCE_ATTESTATION,
    }
    if any(
        requirements.get(field) != expected
        for field, expected in expected_review_text.items()
    ):
        raise AuditError("proposal role-review markers are not authoritative")
    if (
        requirements.get("authoritative_audit")
        != "scripts/audit_semantic_provider_proposal.py"
    ):
        raise AuditError("proposal authoritative-audit path is not fixed")
    return requirements


def _load_release_requirements(manifest_path: Path) -> dict[str, Any]:
    raw = _read_regular(manifest_path, MAX_JSON_BYTES, "proposal manifest")
    value = _decode_json(raw, "proposal manifest")
    release_gates = value.get("release_gates") if type(value) is dict else None
    if type(release_gates) is not dict or set(release_gates) != {
        SEMANTIC_RELEASE_TAG
    }:
        raise AuditError("proposal manifest has malformed release gates")
    requirements = release_gates.get(SEMANTIC_RELEASE_TAG)
    expected_fields = {
        "repository",
        "repository_id",
        "repository_owner_id",
        "base_branch",
        "reviewer_authority",
        "review_roster_gist_id",
        "review_roster_gist_node_id",
        "review_roster_gist_description",
        "review_roster_gist_filename",
        "distinct_roster_reviewers_required",
        "owner_exclusive_repository_collaborators_required",
        "owner_exclusive_mutation_authority_attestation_required",
        "evidence_source",
        "candidate_identity",
        "minimum_non_author_reviews",
        "maximum_review_age_days",
        "security_review_required",
        "product_review_required",
        "security_review_marker",
        "product_review_marker",
        "reviewer_independence_attestation",
        "required_attestations",
        "exact_commit_required",
        "exact_tree_required",
        "resolved_threads_required",
        "no_pending_review_requests",
        "authoritative_audit",
    }
    if type(requirements) is not dict or set(requirements) != expected_fields:
        raise AuditError("semantic-provider release-review requirements are malformed")
    expected_values = {
        "repository": "yzm1/boundver",
        "repository_id": 1226008327,
        "repository_owner_id": 22440724,
        "base_branch": "main",
        "reviewer_authority": REVIEW_AUTHORITY_SOURCE,
        "review_roster_gist_id": REVIEW_ROSTER_GIST_ID,
        "review_roster_gist_node_id": REVIEW_ROSTER_GIST_NODE_ID,
        "review_roster_gist_description": REVIEW_ROSTER_GIST_DESCRIPTION,
        "review_roster_gist_filename": REVIEW_ROSTER_GIST_FILENAME,
        "distinct_roster_reviewers_required": True,
        "owner_exclusive_repository_collaborators_required": True,
        "owner_exclusive_mutation_authority_attestation_required": True,
        "evidence_source": "github-exact-tree-review/v1",
        "candidate_identity": "reviewed-head-tree-equals-release-tree",
        "minimum_non_author_reviews": 2,
        "maximum_review_age_days": 14,
        "security_review_required": True,
        "product_review_required": True,
        "security_review_marker": SEMANTIC_RELEASE_MARKER,
        "product_review_marker": SEMANTIC_PRODUCT_REVIEW_MARKER,
        "reviewer_independence_attestation": REVIEWER_INDEPENDENCE_ATTESTATION,
        "exact_commit_required": True,
        "exact_tree_required": True,
        "resolved_threads_required": True,
        "no_pending_review_requests": True,
        "authoritative_audit": "scripts/audit_semantic_provider_proposal.py",
    }
    for field, expected in expected_values.items():
        actual = requirements.get(field)
        if actual != expected or type(actual) is not type(expected):
            raise AuditError(
                f"semantic-provider release-review requirement {field} is not authoritative"
            )
    attestations = requirements.get("required_attestations")
    if (
        type(attestations) is not list
        or any(type(item) is not str for item in attestations)
        or tuple(attestations) != SEMANTIC_RELEASE_ATTESTATIONS
    ):
        raise AuditError(
            "semantic-provider release-review attestations are not authoritative"
        )
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
    raw_commit = (
        _git(
            repo,
            "log",
            "-1",
            "--first-parent",
            "--format=%H",
            "HEAD",
            "--",
            *GOVERNED_PATHS,
        )
        .decode("ascii", "strict")
        .strip()
    )
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


def _local_release_record(repo: Path, release_sha: str) -> tuple[str, str, str]:
    release_sha = _sha(release_sha, "release SHA")
    dirty = _git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=no",
    )
    if dirty:
        raise AuditError("release candidate has tracked uncommitted changes")
    head = _sha(
        _git(repo, "rev-parse", "--verify", "HEAD^{commit}")
        .decode("ascii", "strict")
        .strip(),
        "local HEAD",
    )
    if head != release_sha:
        raise AuditError("release SHA does not equal local HEAD")
    parent = _sha(
        _git(repo, "rev-parse", f"{release_sha}^1").decode("ascii", "strict").strip(),
        "release record parent",
    )
    local_tree = _sha(
        _git(repo, "rev-parse", f"{release_sha}^{{tree}}")
        .decode("ascii", "strict")
        .strip(),
        "local release tree",
    )
    return release_sha, parent, local_tree


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
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--gate",
        choices=("accepted", "semantic-provider-work", "semantic-provider-release"),
        default="accepted",
    )
    parser.add_argument("--release-sha")
    parser.add_argument("--release-tag")
    parser.add_argument(
        "--format",
        choices=("text", "json", "expiry"),
        default="text",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    repo = args.repo.resolve()
    try:
        release_gate = args.gate == "semantic-provider-release"
        if args.format == "expiry" and not release_gate:
            raise AuditError(
                "expiry output is valid only for the semantic-provider-release gate"
            )
        if release_gate:
            if args.release_tag != SEMANTIC_RELEASE_TAG:
                raise AuditError(
                    "semantic-provider release audit requires --release-tag "
                    f"{SEMANTIC_RELEASE_TAG}"
                )
            release_sha = _sha(args.release_sha, "release SHA")
            release_record = _local_release_record(repo, release_sha)
        else:
            if args.release_sha is not None or args.release_tag is not None:
                raise AuditError(
                    "release identity arguments are valid only for the "
                    "semantic-provider-release gate"
                )
            release_record = None
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
                requirements,
                record_commit,
                record_parent,
                local_tree,
            )
            second = collect_snapshot(
                client,
                requirements,
                record_commit,
                record_parent,
                local_tree,
            )
            if first != second:
                raise AuditError(
                    "GitHub proposal-review state changed during the audit"
                )
            release_first = None
            release_review_result = None
            if release_record is not None:
                release_requirements = _load_release_requirements(manifest_path)
                release_commit, release_parent, release_tree = release_record
                release_first = collect_snapshot(
                    client,
                    release_requirements,
                    release_commit,
                    release_parent,
                    release_tree,
                )
                release_second = collect_snapshot(
                    client,
                    release_requirements,
                    release_commit,
                    release_parent,
                    release_tree,
                )
                if release_first != release_second:
                    raise AuditError(
                        "GitHub release-review state changed during the audit"
                    )
            evaluated_at = datetime.now(timezone.utc)
            review_result = evaluate_snapshot(
                first,
                requirements,
                evaluated_at=evaluated_at,
            )
            if release_first is not None:
                release_review_result = evaluate_snapshot(
                    release_first,
                    release_requirements,
                    evaluated_at=evaluated_at,
                    attestations=SEMANTIC_RELEASE_ATTESTATIONS,
                )
                if (
                    release_review_result["pull_request"]
                    == review_result["pull_request"]
                ):
                    raise AuditError(
                        "proposal acceptance and semantic-provider release require "
                        "separate pull requests"
                    )
            authority_valid_until = review_result["valid_until"]
            if release_review_result is not None and _timestamp_datetime(
                release_review_result["valid_until"]
            ) < _timestamp_datetime(authority_valid_until):
                authority_valid_until = release_review_result["valid_until"]
            checker = _load_checker(validation_root)
            checker_arguments = {
                "authoritative_review_passed": True,
                "authoritative_release_passed": release_review_result is not None,
                "require_accepted": args.gate == "accepted",
                "require_semantic_provider_work": args.gate
                == "semantic-provider-work",
                "require_semantic_provider_release": args.gate
                == "semantic-provider-release",
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
            "release_review": release_review_result,
            "authority_valid_until": authority_valid_until,
            "snapshot_sha256": _stable_digest(
                {"proposal": first, "release": release_first}
                if release_first is not None
                else first
            ),
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
    elif args.format == "expiry":
        print(result["authority_valid_until"])
    else:
        release_summary = ""
        if result["release_review"] is not None:
            release_summary = (
                f" release-PR=#{result['release_review']['pull_request']}"
                f" release={result['release_review']['record_commit']}"
            )
        print(
            "Semantic-provider proposal audit passed: "
            f"gate={result['gate']} PR=#{result['pull_request']} "
            f"record={result['record_commit']} reviewers="
            f"{','.join(result['reviewers'])}{release_summary} "
            f"snapshot={result['snapshot_sha256']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
