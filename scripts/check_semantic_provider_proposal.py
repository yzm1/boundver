#!/usr/bin/env python3
"""Validate the semantic-provider proposal and its assurance traceability.

This checker deliberately uses only the standard library. It validates the
proposal record as untrusted input, cross-references every threat/control/test,
and keeps semantic-provider implementation and release gates fail-closed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional, Sequence


MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 1_000
MAX_TEXT_CHARS = 16_384
MAX_JSON_INTEGER = (1 << 63) - 1
MAX_JSON_TOKENS = 100_000
MAX_JSON_DEPTH = 128
PROPOSAL_ID = "boundver-semantic-provider-system/v1"
REVIEW_AUTHORITY_SOURCE = "github-account-owned-public-gist/v1"
REVIEW_ROSTER_GIST_ID = "0caedb798d168b974f9d9fb63c377f73"
REVIEW_ROSTER_GIST_NODE_ID = (
    "G_kwDOAVZrFNoAIDBjYWVkYjc5OGQxNjhiOTc0ZjlkOWZiNjNjMzc3Zjcz"
)
REVIEW_ROSTER_GIST_DESCRIPTION = (
    "boundver semantic-provider independent reviewer roster"
)
REVIEW_ROSTER_GIST_FILENAME = "semantic-provider-review-roster.txt"
REVIEWER_INDEPENDENCE_ATTESTATION = "Independent-reviewer: confirmed"
PROPOSAL_SECURITY_REVIEW_MARKER = "semantic-provider-security-review/v1"
PROPOSAL_PRODUCT_REVIEW_MARKER = "semantic-provider-product-review/v1"
SEMANTIC_RELEASE_TAG = "v0.16.0"
SEMANTIC_SECURITY_REVIEW_MARKER = "semantic-provider-v0.16-release-review/v1"
SEMANTIC_PRODUCT_REVIEW_MARKER = "semantic-provider-v0.16-product-review/v1"
THREAT_RE = re.compile(r"^SPT-[0-9]{3}$")
CONTROL_RE = re.compile(r"^SPC-[0-9]{3}$")
VERIFICATION_RE = re.compile(r"^SPV-[0-9]{3}$")
ROUND_RE = re.compile(r"^RTR-[0-9]{3}$")
FINDING_RE = re.compile(r"^RTF-[0-9]{3}$")
URL_RE = re.compile(r"^https://[^\s]+$")

SEMANTIC_RELEASE_ATTESTATIONS = (
    "Full-source-bug-scan: passed",
    "Full-issue-audit: passed",
    "Full-security-scan: passed",
    "All-blockers: closed",
    "Supported-platforms: passed",
    "Publication-gates: passed",
)

ROOT_FIELDS = {
    "$schema",
    "schema_version",
    "proposal",
    "status",
    "implementation_allowed",
    "semantic_provider_work_allowed",
    "documents",
    "threats",
    "controls",
    "verifications",
    "red_team",
    "review_requirements",
    "open_findings",
    "release_gates",
    "references",
}
PROPOSAL_STATUSES = {"draft", "review-ready", "accepted", "rejected", "superseded"}
SEVERITIES = {"critical", "high", "medium", "low"}
CONTROL_KINDS = {"preventive", "detective", "recovery"}
VERIFICATION_PHASES = {"proposal", "implementation", "provider", "release"}
VERIFICATION_STATUSES = {"planned", "passed", "failed", "not-applicable"}


class ProposalError(ValueError):
    """A deterministic proposal validation failure."""


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProposalError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProposalError(f"non-finite JSON number is not allowed: {value}")


def _parse_int(value: str) -> int:
    if len(value) > 20:
        raise ProposalError("JSON integer exceeds the signed 64-bit limit")
    result = int(value)
    if abs(result) > MAX_JSON_INTEGER:
        raise ProposalError("JSON integer exceeds the signed 64-bit limit")
    return result


def _reject_float(value: str) -> None:
    raise ProposalError(f"floating-point JSON number is not allowed: {value}")


def _json_shape_within_limits(raw: bytes) -> bool:
    """Reject provably wide or deep JSON before the decoder allocates it."""
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


def _read_regular(path: Path, limit: int) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ProposalError(f"cannot stat {path}: {exc}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ProposalError(f"path is not a regular file: {path}")
    if before.st_size > limit:
        raise ProposalError(f"file exceeds the {limit}-byte limit: {path}")
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ProposalError(f"opened path is not a regular file: {path}")
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ProposalError(f"file changed while opening: {path}")
            data = stream.read(limit + 1)
            after_read = os.fstat(stream.fileno())
    except OSError as exc:
        raise ProposalError(f"cannot read {path}: {exc}") from exc
    if len(data) > limit:
        raise ProposalError(f"file exceeds the {limit}-byte limit: {path}")
    try:
        current = path.lstat()
    except OSError as exc:
        raise ProposalError(f"cannot restat {path}: {exc}") from exc
    identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    if identity != (
        after_read.st_dev,
        after_read.st_ino,
        after_read.st_size,
        after_read.st_mtime_ns,
    ) or identity != (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ):
        raise ProposalError(f"file changed while reading: {path}")
    return data


def _load_json(path: Path) -> dict[str, Any]:
    raw = _read_regular(path, MAX_JSON_BYTES)
    if not _json_shape_within_limits(raw):
        raise ProposalError(f"JSON exceeds the structural limit: {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProposalError(f"JSON is not UTF-8: {path}") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_duplicate_safe_object,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
            parse_int=_parse_int,
        )
    except (json.JSONDecodeError, ProposalError, RecursionError) as exc:
        raise ProposalError(f"invalid JSON in {path}: {exc}") from exc
    if type(value) is not dict:
        raise ProposalError(f"JSON root must be an object: {path}")
    return value


def _bounded_text(value: Any, field: str) -> str:
    if type(value) is not str or not value:
        raise ProposalError(f"{field} must be a non-empty string")
    if len(value) > MAX_TEXT_CHARS:
        raise ProposalError(f"{field} exceeds the {MAX_TEXT_CHARS}-character limit")
    return value


def _exact_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ProposalError(f"{field} must be a boolean")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if type(value) is not list:
        raise ProposalError(f"{field} must be an array")
    if len(value) > MAX_ITEMS:
        raise ProposalError(f"{field} exceeds the {MAX_ITEMS}-item limit")
    return value


def _object(value: Any, field: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ProposalError(f"{field} must be an object")
    return value


def _unique_ids(
    items: list[Any], field: str, pattern: re.Pattern[str]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, raw in enumerate(items):
        item = _object(raw, f"{field}[{index}]")
        identifier = _bounded_text(item.get("id"), f"{field}[{index}].id")
        if pattern.fullmatch(identifier) is None:
            raise ProposalError(
                f"{field}[{index}].id has an invalid identifier: {identifier!r}"
            )
        if identifier in result:
            raise ProposalError(f"duplicate {field} identifier: {identifier}")
        result[identifier] = item
        order.append(identifier)
    if order != sorted(order):
        raise ProposalError(f"{field} must be sorted by identifier")
    return result


def _id_list(
    value: Any,
    field: str,
    pattern: re.Pattern[str],
    *,
    required: bool = False,
) -> list[str]:
    raw_items = _list(value, field)
    items: list[str] = []
    for index, raw in enumerate(raw_items):
        identifier = _bounded_text(raw, f"{field}[{index}]")
        if pattern.fullmatch(identifier) is None:
            raise ProposalError(
                f"{field}[{index}] has an invalid identifier: {identifier!r}"
            )
        items.append(identifier)
    if required and not items:
        raise ProposalError(f"{field} must be non-empty")
    if len(items) != len(set(items)):
        raise ProposalError(f"{field} contains duplicate identifiers")
    if items != sorted(items):
        raise ProposalError(f"{field} must be sorted")
    return items


def _safe_repo_path(repo: Path, raw: Any, field: str) -> Path:
    value = _bounded_text(raw, field)
    if "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ProposalError(f"{field} must be a repository-relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ProposalError(f"{field} contains an invalid path segment")
    target = repo.joinpath(*parts)
    try:
        resolved = target.resolve(strict=True)
        repo_resolved = repo.resolve(strict=True)
    except OSError as exc:
        raise ProposalError(f"cannot resolve {field}: {exc}") from exc
    try:
        resolved.relative_to(repo_resolved)
    except ValueError as exc:
        raise ProposalError(f"{field} escapes the repository") from exc
    if target.is_symlink():
        raise ProposalError(f"{field} must not be a symlink")
    return target


def _read_document(repo: Path, raw: Any, field: str) -> str:
    path = _safe_repo_path(repo, raw, field)
    content = _read_regular(path, MAX_DOCUMENT_BYTES)
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProposalError(f"{field} is not UTF-8") from exc


def _validate_evidence(value: Any, field: str, *, required: bool) -> list[str]:
    evidence = _list(value, field)
    result = [
        _bounded_text(item, f"{field}[{index}]") for index, item in enumerate(evidence)
    ]
    if required and not result:
        raise ProposalError(f"{field} must contain evidence")
    return result


def _validate_document_gate_markers(
    text: str,
    field: str,
    *,
    status: str,
    implementation_allowed: bool,
    semantic_provider_work_allowed: bool,
) -> None:
    expected = {
        "proposal-status": status,
        "implementation-allowed": str(implementation_allowed).lower(),
        "work-allowed": str(semantic_provider_work_allowed).lower(),
    }
    for name, value in expected.items():
        matches = re.findall(
            rf"^<!-- semantic-provider-{re.escape(name)}: ([a-z0-9.-]+) -->$",
            text,
            flags=re.MULTILINE,
        )
        if matches != [value]:
            raise ProposalError(
                f"{field} semantic-provider {name} marker must equal {value!r}"
            )


def validate_proposal(
    repo: Path,
    manifest_path: Path,
    *,
    require_accepted: bool = False,
    require_semantic_provider_work: bool = False,
    require_semantic_provider_release: bool = False,
    authoritative_review_passed: bool = False,
    authoritative_release_passed: bool = False,
) -> dict[str, Any]:
    for value, field in (
        (require_accepted, "require_accepted"),
        (require_semantic_provider_work, "require_semantic_provider_work"),
        (require_semantic_provider_release, "require_semantic_provider_release"),
        (authoritative_review_passed, "authoritative_review_passed"),
        (authoritative_release_passed, "authoritative_release_passed"),
    ):
        if type(value) is not bool:
            raise ProposalError(f"{field} must be a boolean")
    if authoritative_release_passed and not authoritative_review_passed:
        raise ProposalError(
            "authoritative release evidence cannot bypass proposal review"
        )
    manifest = _load_json(manifest_path)
    unknown = set(manifest) - ROOT_FIELDS
    missing = ROOT_FIELDS - set(manifest)
    if unknown:
        raise ProposalError(
            f"proposal has unknown fields: {', '.join(sorted(unknown))}"
        )
    if missing:
        raise ProposalError(f"proposal is missing fields: {', '.join(sorted(missing))}")
    if (
        manifest.get("schema_version") != 2
        or type(manifest.get("schema_version")) is not int
    ):
        raise ProposalError("schema_version must be integer 2")
    if manifest.get("proposal") != PROPOSAL_ID:
        raise ProposalError(f"proposal must be {PROPOSAL_ID!r}")
    status = manifest.get("status")
    if type(status) is not str or status not in PROPOSAL_STATUSES:
        raise ProposalError("status is invalid")
    implementation_allowed = _exact_bool(
        manifest.get("implementation_allowed"), "implementation_allowed"
    )
    semantic_provider_work_allowed = _exact_bool(
        manifest.get("semantic_provider_work_allowed"),
        "semantic_provider_work_allowed",
    )

    schema_path = _safe_repo_path(repo, manifest.get("$schema"), "$schema")
    _load_json(schema_path)
    documents = _object(manifest.get("documents"), "documents")
    if set(documents) != {"rfc", "threat_model"}:
        raise ProposalError("documents must contain exactly rfc and threat_model")
    rfc_text = _read_document(repo, documents.get("rfc"), "documents.rfc")
    threat_text = _read_document(
        repo, documents.get("threat_model"), "documents.threat_model"
    )
    for field, text in (("RFC", rfc_text), ("threat model", threat_text)):
        if PROPOSAL_ID not in text:
            raise ProposalError(f"{field} does not identify {PROPOSAL_ID}")
        _validate_document_gate_markers(
            text,
            field,
            status=status,
            implementation_allowed=implementation_allowed,
            semantic_provider_work_allowed=semantic_provider_work_allowed,
        )
    ci_text = _read_document(
        repo,
        ".github/workflows/ci.yml",
        "semantic-provider CI gate",
    )
    ci_gate = "run: python -I scripts/check_semantic_provider_proposal.py"
    if ci_text.count(ci_gate) != 1:
        raise ProposalError(
            "CI must invoke the semantic-provider proposal checker exactly once"
        )
    coverage_gate = (
        'python -I -m coverage report --include="scripts/audit_semantic_provider_'
        'proposal.py,scripts/check_semantic_provider_proposal.py" --fail-under=75'
    )
    if ci_text.count(coverage_gate) != 1:
        raise ProposalError("CI must enforce the semantic-provider gate coverage floor")
    create_tag_text = _read_document(
        repo,
        ".github/workflows/create-release-tag.yml",
        "semantic-provider release-tag gate",
    )
    publish_workflow_text = _read_document(
        repo,
        ".github/workflows/publish.yml",
        "semantic-provider publication gate",
    )
    publish_launcher_text = _read_document(
        repo,
        "scripts/publish_release.py",
        "semantic-provider local release gate",
    )
    for workflow_name, workflow_text in (
        ("release-tag", create_tag_text),
        ("publication", publish_workflow_text),
    ):
        gh_token_sources = re.findall(
            r"(?m)^\s+GH_TOKEN:\s*(\S.*?)\s*$",
            workflow_text,
        )
        if (
            "${{ secrets." in workflow_text
            or not gh_token_sources
            or any(source != "${{ github.token }}" for source in gh_token_sources)
        ):
            raise ProposalError(
                f"{workflow_name} workflow must not receive user-secret authority"
            )
    release_audit_script = (
        '"$GITHUB_WORKSPACE/scripts/audit_semantic_provider_proposal.py"'
    )
    release_audit_arguments = (
        "--gate semantic-provider-release",
        '--release-tag "$RELEASE_TAG"',
        '--release-sha "$RELEASE_SHA"',
    )
    release_condition = f'if [[ "$RELEASE_TAG" == {SEMANTIC_RELEASE_TAG} ]]; then'
    regular_review_command = (
        'bash scripts/audit_release_reviews.sh "$RELEASE_SHA" "$RELEASE_TAG"'
    )
    create_semantic_positions = [
        match.start()
        for match in re.finditer(re.escape(release_audit_script), create_tag_text)
    ]
    create_review_positions = [
        match.start()
        for match in re.finditer(re.escape(regular_review_command), create_tag_text)
    ]
    create_audit_invocations = [
        create_tag_text[position : position + 512]
        for position in create_semantic_positions
    ]
    tag_mutation_position = create_tag_text.find(
        'git tag "$RELEASE_TAG" "$RELEASE_SHA"'
    )
    public_gist_start = create_tag_text.find("    def public_gist_json(endpoint):")
    public_gist_end = create_tag_text.find("\n    def rest_records", public_gist_start)
    public_gist_program = (
        create_tag_text[public_gist_start:public_gist_end]
        if public_gist_start >= 0 and public_gist_end > public_gist_start
        else ""
    )
    if (
        len(create_semantic_positions) != 2
        or any(
            invocation.count(argument) != 1
            for invocation in create_audit_invocations
            for argument in release_audit_arguments
        )
        or create_tag_text.count(release_condition) != 3
        or create_tag_text.count("clean_python_cwd=$(mktemp -d)") < 2
        or create_tag_text.count("--format expiry") != 1
        or create_tag_text.count(
            "semantic-review-valid-until: "
            "${{ steps.mutable-state.outputs.semantic-review-valid-until }}"
        )
        != 1
        or "fullDatabaseId lastEditedAt" not in create_tag_text
        or '"repository_id": repository_id' not in create_tag_text
        or create_tag_text.count("SEMANTIC_REVIEW_ROSTER_GIST_ID") < 8
        or create_tag_text.count(REVIEW_ROSTER_GIST_ID) != 1
        or create_tag_text.count(REVIEW_ROSTER_GIST_NODE_ID) != 1
        or create_tag_text.count(REVIEW_ROSTER_GIST_DESCRIPTION) != 1
        or create_tag_text.count(REVIEW_ROSTER_GIST_FILENAME) != 1
        or create_tag_text.count(REVIEW_AUTHORITY_SOURCE) != 1
        or create_tag_text.count("public_gist_json(") != 3
        or "http.client.HTTPSConnection" not in public_gist_program
        or '"api.github.com"' not in public_gist_program
        or 'connection.request(\n                "GET"' not in public_gist_program
        or '"Authorization"' in public_gist_program
        or "GH_TOKEN" in public_gist_program
        or 'GITHUB_REST_API_VERSION = "2022-11-28"' not in create_tag_text
        or create_tag_text.count("X-GitHub-Api-Version") < 2
        or 'rest(f"gists/' in create_tag_text
        or "normalize_semantic_roster_gist" not in create_tag_text
        or 'record.get("public") is not True' not in create_tag_text
        or 'record.get("truncated") is not False' not in create_tag_text
        or "Semantic review roster revision is not owner-authored"
        not in create_tag_text
        or "Semantic review roster gist changed during collection"
        not in create_tag_text
        or "Independent-beneficial-owners-attested: true" not in create_tag_text
        or "Owner-exclusive-mutation-authority-attested: true"
        not in create_tag_text
        or "parse_semantic_review_roster" not in create_tag_text
        or 'permission_record.get("permission") != "read"' not in create_tag_text
        or "type(permission_flags[field]) is not bool" not in create_tag_text
        or "Semantic roster reviewer is not read-only" not in create_tag_text
        or "Repository mutation authority is not owner-exclusive"
        not in create_tag_text
        or 'f"repos/{repository}/collaborators?per_page=100"'
        not in create_tag_text
        or '"repository_mutation_authority": repository_mutation_authority'
        not in create_tag_text
        or '"owner_attested_exclusive_mutation_authority": True'
        not in create_tag_text
        or '"repository_permission": {' not in create_tag_text
        or '"semantic_review_authority": semantic_review_authority'
        not in create_tag_text
        or 'review["last_edited_at"] = review_edit_times[review["id"]]'
        not in create_tag_text
        or create_tag_text.count("require_semantic_review_fresh") < 4
        or create_tag_text.count("now_epoch + 300 >= expiry_epoch") != 1
        or create_tag_text.count("timeout --signal=KILL 60s") != 1
        or len(create_review_positions) != 2
        or any(
            semantic <= review
            for semantic, review in zip(
                create_semantic_positions, create_review_positions
            )
        )
        or tag_mutation_position < 0
        or create_semantic_positions[-1] >= tag_mutation_position
    ):
        raise ProposalError(
            "release-tag workflow must enforce the semantic-provider exact-tree audit twice before tagging"
        )
    publish_semantic_position = publish_workflow_text.find(release_audit_script)
    publish_review_position = publish_workflow_text.find(regular_review_command)
    publish_audit_invocation = publish_workflow_text[
        publish_semantic_position : publish_semantic_position + 512
    ]
    if (
        publish_workflow_text.count(release_audit_script) != 1
        or any(
            publish_audit_invocation.count(argument) != 1
            for argument in release_audit_arguments
        )
        or publish_workflow_text.count(release_condition) != 1
        or publish_workflow_text.count("clean_python_cwd=$(mktemp -d)") < 1
        or publish_workflow_text.count(
            "Require exact-tree semantic-provider release evidence"
        )
        != 1
        or publish_review_position < 0
        or publish_semantic_position <= publish_review_position
    ):
        raise ProposalError(
            "publish workflow must enforce the semantic-provider exact-tree audit before publication"
        )
    launcher_audit = '"scripts/audit_semantic_provider_proposal.py",'
    launcher_semantic_position = publish_launcher_text.find(launcher_audit)
    launcher_review_position = publish_launcher_text.find(
        '"scripts/audit_release_reviews.sh"'
    )
    launcher_tooling_position = publish_launcher_text.find("tooling = Path(temporary)")
    if (
        publish_launcher_text.count(f'if tag == "{SEMANTIC_RELEASE_TAG}":') != 1
        or publish_launcher_text.count(launcher_audit) != 1
        or launcher_review_position < 0
        or launcher_tooling_position < 0
        or launcher_semantic_position <= launcher_review_position
        or launcher_semantic_position >= launcher_tooling_position
    ):
        raise ProposalError(
            "local release launcher must enforce the semantic-provider exact-tree audit before candidate checks"
        )

    threats = _unique_ids(
        _list(manifest.get("threats"), "threats"), "threats", THREAT_RE
    )
    controls = _unique_ids(
        _list(manifest.get("controls"), "controls"), "controls", CONTROL_RE
    )
    verifications = _unique_ids(
        _list(manifest.get("verifications"), "verifications"),
        "verifications",
        VERIFICATION_RE,
    )
    if not threats or not controls or not verifications:
        raise ProposalError("threats, controls, and verifications must be non-empty")

    control_kinds: dict[str, frozenset[str]] = {}
    for identifier, item in controls.items():
        if set(item) != {"id", "title", "kinds", "threats", "verifications"}:
            raise ProposalError(f"{identifier} has unknown or missing fields")
        _bounded_text(item.get("title"), f"{identifier}.title")
        kinds = _list(item.get("kinds"), f"{identifier}.kinds")
        if not kinds or any(
            type(kind) is not str or kind not in CONTROL_KINDS for kind in kinds
        ):
            raise ProposalError(f"{identifier}.kinds is invalid")
        if len(kinds) != len(set(kinds)):
            raise ProposalError(f"{identifier}.kinds contains duplicates")
        control_kinds[identifier] = frozenset(kinds)

    for identifier, item in threats.items():
        if set(item) != {"id", "title", "severity", "controls", "verifications"}:
            raise ProposalError(f"{identifier} has unknown or missing fields")
        _bounded_text(item.get("title"), f"{identifier}.title")
        severity = item.get("severity")
        if type(severity) is not str or severity not in SEVERITIES:
            raise ProposalError(f"{identifier}.severity is invalid")
        control_ids = _id_list(
            item.get("controls"),
            f"{identifier}.controls",
            CONTROL_RE,
            required=True,
        )
        verification_ids = _id_list(
            item.get("verifications"),
            f"{identifier}.verifications",
            VERIFICATION_RE,
            required=True,
        )
        missing_controls = set(control_ids) - set(controls)
        missing_verifications = set(verification_ids) - set(verifications)
        if missing_controls:
            raise ProposalError(
                f"{identifier} references unknown controls: {sorted(missing_controls)}"
            )
        if missing_verifications:
            raise ProposalError(
                f"{identifier} references unknown verifications: {sorted(missing_verifications)}"
            )
        if severity in {"critical", "high"} and len(control_ids) < 2:
            raise ProposalError(
                f"{identifier} requires at least two defense-in-depth controls"
            )
        kinds = {
            kind
            for control_id in control_ids
            for kind in control_kinds[control_id]
        }
        if "preventive" not in kinds or not kinds.intersection(
            {"detective", "recovery"}
        ):
            raise ProposalError(
                f"{identifier} requires preventive and detective/recovery coverage"
            )
        if identifier not in threat_text:
            raise ProposalError(f"threat model does not mention {identifier}")

    for identifier, item in controls.items():
        threat_ids = _id_list(
            item.get("threats"),
            f"{identifier}.threats",
            THREAT_RE,
            required=True,
        )
        verification_ids = _id_list(
            item.get("verifications"),
            f"{identifier}.verifications",
            VERIFICATION_RE,
            required=True,
        )
        if set(threat_ids) - set(threats):
            raise ProposalError(f"{identifier} references unknown threats")
        if set(verification_ids) - set(verifications):
            raise ProposalError(f"{identifier} references unknown verifications")
        for threat_id in threat_ids:
            if identifier not in threats[threat_id]["controls"]:
                raise ProposalError(
                    f"{identifier}/{threat_id} mapping is not bidirectional"
                )
        for threat_id, threat in threats.items():
            if identifier in threat["controls"] and threat_id not in threat_ids:
                raise ProposalError(
                    f"{threat_id}/{identifier} mapping is not bidirectional"
                )
        if identifier not in rfc_text:
            raise ProposalError(f"RFC does not mention {identifier}")

    proposal_verifications: list[str] = []
    for identifier, item in verifications.items():
        if set(item) != {"id", "title", "phase", "status", "evidence"}:
            raise ProposalError(f"{identifier} has unknown or missing fields")
        _bounded_text(item.get("title"), f"{identifier}.title")
        phase = item.get("phase")
        verification_status = item.get("status")
        if type(phase) is not str or phase not in VERIFICATION_PHASES:
            raise ProposalError(f"{identifier}.phase is invalid")
        if (
            type(verification_status) is not str
            or verification_status not in VERIFICATION_STATUSES
        ):
            raise ProposalError(f"{identifier}.status is invalid")
        _validate_evidence(
            item.get("evidence"),
            f"{identifier}.evidence",
            required=verification_status == "passed",
        )
        if verification_status == "not-applicable" and not item.get("evidence"):
            raise ProposalError(
                f"{identifier} needs evidence for not-applicable status"
            )
        if phase == "proposal":
            proposal_verifications.append(identifier)
        if identifier not in threat_text:
            raise ProposalError(f"threat model does not mention {identifier}")

    referenced_verifications = {
        verification_id
        for threat in threats.values()
        for verification_id in threat["verifications"]
    } | {
        verification_id
        for control in controls.values()
        for verification_id in control["verifications"]
    }
    orphaned = set(verifications) - referenced_verifications
    if orphaned:
        raise ProposalError(f"unreferenced verifications: {sorted(orphaned)}")

    red_team = _object(manifest.get("red_team"), "red_team")
    if set(red_team) != {"status", "rounds", "residual_risks_accepted"}:
        raise ProposalError("red_team has unknown or missing fields")
    red_team_status = red_team.get("status")
    if red_team_status not in {"pending", "passed", "failed"}:
        raise ProposalError("red_team.status is invalid")
    residual_risks_accepted = _exact_bool(
        red_team.get("residual_risks_accepted"),
        "red_team.residual_risks_accepted",
    )
    rounds = _unique_ids(
        _list(red_team.get("rounds"), "red_team.rounds"),
        "red_team.rounds",
        ROUND_RE,
    )
    red_team_findings: list[str] = []
    for identifier, item in rounds.items():
        if set(item) != {"id", "scope", "status", "findings", "evidence"}:
            raise ProposalError(f"{identifier} has unknown or missing fields")
        _bounded_text(item.get("scope"), f"{identifier}.scope")
        round_status = item.get("status")
        if round_status not in {"planned", "passed", "failed"}:
            raise ProposalError(f"{identifier}.status is invalid")
        round_findings = _id_list(
            item.get("findings"), f"{identifier}.findings", FINDING_RE
        )
        red_team_findings.extend(round_findings)
        _validate_evidence(
            item.get("evidence"),
            f"{identifier}.evidence",
            required=round_status == "passed",
        )
    if red_team_status == "passed" and any(
        item.get("status") != "passed" for item in rounds.values()
    ):
        raise ProposalError("red_team cannot pass while a round is not passed")
    if len(red_team_findings) != len(set(red_team_findings)):
        raise ProposalError("red-team findings must belong to exactly one round")
    documented_finding_rows = re.findall(
        r"^### (RTF-[0-9]{3}): .+ - (Critical|High|Medium|Low)$",
        threat_text,
        flags=re.MULTILINE,
    )
    documented_finding_ids = [identifier for identifier, _ in documented_finding_rows]
    if len(documented_finding_ids) != len(set(documented_finding_ids)):
        raise ProposalError("threat model contains duplicate red-team finding headings")
    documented_findings = {
        identifier: severity.lower() for identifier, severity in documented_finding_rows
    }
    if set(documented_findings) != set(red_team_findings):
        missing = sorted(set(red_team_findings) - set(documented_findings))
        extra = sorted(set(documented_findings) - set(red_team_findings))
        raise ProposalError(
            f"red-team finding documentation mismatch: missing={missing}, extra={extra}"
        )

    review_requirements = _object(
        manifest.get("review_requirements"), "review_requirements"
    )
    expected_review_fields = {
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
    if set(review_requirements) != expected_review_fields:
        raise ProposalError("review_requirements has unknown or missing fields")
    if review_requirements.get("repository") != "yzm1/boundver":
        raise ProposalError("review_requirements.repository is invalid")
    if (
        review_requirements.get("repository_id") != 1226008327
        or type(review_requirements.get("repository_id")) is not int
    ):
        raise ProposalError("review_requirements.repository_id is invalid")
    if (
        review_requirements.get("repository_owner_id") != 22440724
        or type(review_requirements.get("repository_owner_id")) is not int
    ):
        raise ProposalError("review_requirements.repository_owner_id is invalid")
    if review_requirements.get("base_branch") != "main":
        raise ProposalError("review_requirements.base_branch is invalid")
    if review_requirements.get("reviewer_authority") != REVIEW_AUTHORITY_SOURCE:
        raise ProposalError("review_requirements.reviewer_authority is invalid")
    expected_roster_identity = {
        "review_roster_gist_id": REVIEW_ROSTER_GIST_ID,
        "review_roster_gist_node_id": REVIEW_ROSTER_GIST_NODE_ID,
        "review_roster_gist_description": REVIEW_ROSTER_GIST_DESCRIPTION,
        "review_roster_gist_filename": REVIEW_ROSTER_GIST_FILENAME,
    }
    if any(
        review_requirements.get(field) != expected
        or type(review_requirements.get(field)) is not type(expected)
        for field, expected in expected_roster_identity.items()
    ):
        raise ProposalError("review_requirements reviewer roster is invalid")
    if review_requirements.get("distinct_roster_reviewers_required") is not True:
        raise ProposalError(
            "review_requirements.distinct_roster_reviewers_required must be true"
        )
    if (
        review_requirements.get(
            "owner_exclusive_repository_collaborators_required"
        )
        is not True
    ):
        raise ProposalError(
            "review_requirements owner-exclusive mutation authority must remain true"
        )
    if (
        review_requirements.get(
            "owner_exclusive_mutation_authority_attestation_required"
        )
        is not True
    ):
        raise ProposalError(
            "review_requirements owner mutation attestation must remain required"
        )
    minimum_reviews = review_requirements.get("minimum_non_author_reviews")
    if type(minimum_reviews) is not int or minimum_reviews < 2:
        raise ProposalError(
            "review_requirements requires at least two non-author reviews"
        )
    if review_requirements.get("maximum_review_age_days") != 90:
        raise ProposalError("review_requirements.maximum_review_age_days must be 90")
    for field in (
        "security_review_required",
        "product_review_required",
        "exact_commit_required",
        "resolved_threads_required",
        "no_pending_review_requests",
    ):
        if not _exact_bool(
            review_requirements.get(field), f"review_requirements.{field}"
        ):
            raise ProposalError(f"review_requirements.{field} must remain true")
    expected_review_text = {
        "security_review_marker": PROPOSAL_SECURITY_REVIEW_MARKER,
        "product_review_marker": PROPOSAL_PRODUCT_REVIEW_MARKER,
        "reviewer_independence_attestation": REVIEWER_INDEPENDENCE_ATTESTATION,
    }
    for field, expected in expected_review_text.items():
        if review_requirements.get(field) != expected:
            raise ProposalError(f"review_requirements.{field} is invalid")
    if (
        review_requirements.get("authoritative_audit")
        != "scripts/audit_semantic_provider_proposal.py"
    ):
        raise ProposalError("review_requirements.authoritative_audit is invalid")
    _safe_repo_path(
        repo,
        review_requirements.get("authoritative_audit"),
        "review_requirements.authoritative_audit",
    )

    findings = _list(manifest.get("open_findings"), "open_findings")
    finding_ids: list[str] = []
    for index, raw in enumerate(findings):
        finding = _object(raw, f"open_findings[{index}]")
        required = {"id", "severity", "title", "owner", "disposition"}
        allowed = required | {
            "rationale",
            "expires",
            "compensating_controls",
            "evidence",
        }
        if not required.issubset(finding) or set(finding) - allowed:
            raise ProposalError(f"open_findings[{index}] has unknown or missing fields")
        finding_id = _bounded_text(finding.get("id"), f"open_findings[{index}].id")
        if FINDING_RE.fullmatch(finding_id) is None:
            raise ProposalError(f"open_findings[{index}].id is invalid")
        finding_ids.append(finding_id)
        if finding_id not in red_team_findings:
            raise ProposalError(
                f"open_findings[{index}].id is not present in a red-team round"
            )
        if finding.get("severity") not in SEVERITIES:
            raise ProposalError(f"open_findings[{index}].severity is invalid")
        if finding.get("severity") != documented_findings[finding_id]:
            raise ProposalError(
                f"open_findings[{index}].severity contradicts the threat model"
            )
        if finding.get("disposition") not in {"open", "accepted", "closed"}:
            raise ProposalError(f"open_findings[{index}].disposition is invalid")
        _bounded_text(finding.get("title"), f"open_findings[{index}].title")
        _bounded_text(finding.get("owner"), f"open_findings[{index}].owner")
        disposition = finding.get("disposition")
        if disposition in {"accepted", "closed"}:
            _bounded_text(finding.get("rationale"), f"open_findings[{index}].rationale")
            _validate_evidence(
                finding.get("evidence"),
                f"open_findings[{index}].evidence",
                required=True,
            )
        if disposition == "accepted":
            control_ids = _id_list(
                finding.get("compensating_controls"),
                f"open_findings[{index}].compensating_controls",
                CONTROL_RE,
            )
            if not control_ids or set(control_ids) - set(controls):
                raise ProposalError(
                    f"open_findings[{index}] needs known compensating controls"
                )
            expires = _bounded_text(
                finding.get("expires"), f"open_findings[{index}].expires"
            )
            try:
                expiration = date.fromisoformat(expires)
            except ValueError as exc:
                raise ProposalError(
                    f"open_findings[{index}].expires is not an ISO date"
                ) from exc
            if expiration <= date.today():
                raise ProposalError(f"open_findings[{index}] acceptance has expired")
    if finding_ids != sorted(finding_ids) or len(finding_ids) != len(set(finding_ids)):
        raise ProposalError("open_findings must be sorted and unique")

    references = _list(manifest.get("references"), "references")
    if not references:
        raise ProposalError("references must be non-empty")
    for index, raw in enumerate(references):
        url = _bounded_text(raw, f"references[{index}]")
        if URL_RE.fullmatch(url) is None:
            raise ProposalError(f"references[{index}] must be an HTTPS URL")
        if url not in rfc_text:
            raise ProposalError(f"RFC does not contain reference {url}")

    release_gates = _object(manifest.get("release_gates"), "release_gates")
    if set(release_gates) != {SEMANTIC_RELEASE_TAG}:
        raise ProposalError(
            f"release_gates must contain exactly {SEMANTIC_RELEASE_TAG}"
        )
    release_field = f"release_gates.{SEMANTIC_RELEASE_TAG}"
    release = _object(release_gates[SEMANTIC_RELEASE_TAG], release_field)
    release_fields = {
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
    if set(release) != release_fields:
        raise ProposalError(f"{release_field} has unknown or missing fields")
    expected_release_values = {
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
        "security_review_marker": SEMANTIC_SECURITY_REVIEW_MARKER,
        "product_review_marker": SEMANTIC_PRODUCT_REVIEW_MARKER,
        "reviewer_independence_attestation": REVIEWER_INDEPENDENCE_ATTESTATION,
        "exact_commit_required": True,
        "exact_tree_required": True,
        "resolved_threads_required": True,
        "no_pending_review_requests": True,
        "authoritative_audit": "scripts/audit_semantic_provider_proposal.py",
    }
    for field, expected in expected_release_values.items():
        if release.get(field) != expected or type(release.get(field)) is not type(
            expected
        ):
            raise ProposalError(f"{release_field}.{field} is not authoritative")
    attestations = _validate_evidence(
        release.get("required_attestations"),
        f"{release_field}.required_attestations",
        required=True,
    )
    if tuple(attestations) != SEMANTIC_RELEASE_ATTESTATIONS:
        raise ProposalError(
            f"{release_field}.required_attestations are not authoritative"
        )

    static_acceptance_blockers = []
    if status != "accepted":
        static_acceptance_blockers.append("proposal status is not accepted")
    if red_team_status != "passed":
        static_acceptance_blockers.append("red-team status is not passed")
    if not residual_risks_accepted:
        static_acceptance_blockers.append("residual risks are not explicitly accepted")
    open_findings = [
        finding for finding in findings if finding.get("disposition") == "open"
    ]
    if open_findings:
        static_acceptance_blockers.append("Open red-team findings remain unresolved")
    blocking_findings = [
        finding
        for finding in findings
        if finding.get("disposition") == "accepted"
        and finding.get("severity") in {"critical", "high", "medium"}
    ]
    if blocking_findings:
        static_acceptance_blockers.append(
            "Critical/High/Medium findings remain unresolved"
        )
    pending_proposal = [
        identifier
        for identifier in proposal_verifications
        if verifications[identifier].get("status") != "passed"
    ]
    if pending_proposal:
        static_acceptance_blockers.append(
            "proposal verifications are not passed: " + ", ".join(pending_proposal)
        )
    accepted_requirements = list(static_acceptance_blockers)
    if not authoritative_review_passed:
        accepted_requirements.append(
            "authoritative exact-commit GitHub review audit has not passed"
        )
    release_allowed = authoritative_release_passed and not accepted_requirements

    if status == "accepted" and static_acceptance_blockers:
        raise ProposalError(
            "accepted proposal violates static acceptance gates: "
            + "; ".join(static_acceptance_blockers)
        )
    if status == "accepted" and (
        not implementation_allowed or not semantic_provider_work_allowed
    ):
        raise ProposalError(
            "accepted proposal must explicitly allow semantic-provider implementation and work"
        )
    if status != "accepted" and (
        implementation_allowed or semantic_provider_work_allowed
    ):
        raise ProposalError(
            "unaccepted proposal cannot allow semantic-provider implementation or work"
        )
    if implementation_allowed != semantic_provider_work_allowed:
        raise ProposalError(
            "implementation_allowed and semantic_provider_work_allowed must advance together"
        )

    if require_accepted and accepted_requirements:
        raise ProposalError(
            "proposal acceptance gate is blocked: " + "; ".join(accepted_requirements)
        )
    if require_semantic_provider_work and (
        not semantic_provider_work_allowed or accepted_requirements
    ):
        raise ProposalError(
            "semantic-provider work is blocked until the proposal and authoritative review audit pass"
        )
    if require_semantic_provider_release and (
        not release_allowed or accepted_requirements
    ):
        raise ProposalError(
            f"{SEMANTIC_RELEASE_TAG} semantic-provider release is blocked until "
            "proposal reviews and full-source release evidence pass"
        )

    return {
        "ok": True,
        "proposal": PROPOSAL_ID,
        "status": status,
        "implementation_allowed": implementation_allowed,
        "semantic_provider_work_allowed": semantic_provider_work_allowed,
        "semantic_provider_release_allowed": release_allowed,
        "authoritative_review_passed": authoritative_review_passed,
        "authoritative_release_passed": authoritative_release_passed,
        "threats": len(threats),
        "controls": len(controls),
        "verifications": len(verifications),
        "acceptance_blockers": accepted_requirements,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("spec/semantic-provider-proposal.json"),
    )
    parser.add_argument("--require-accepted", action="store_true")
    parser.add_argument("--require-semantic-provider-work", action="store_true")
    parser.add_argument("--require-semantic-provider-release", action="store_true")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    repo = args.repo.resolve()
    manifest = args.manifest
    if not manifest.is_absolute():
        manifest = repo / manifest
    try:
        result = validate_proposal(
            repo,
            manifest,
            require_accepted=args.require_accepted,
            require_semantic_provider_work=args.require_semantic_provider_work,
            require_semantic_provider_release=args.require_semantic_provider_release,
        )
    except (OSError, ProposalError, RecursionError) as exc:
        if args.format == "json":
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"Semantic provider proposal check failed: {exc}", file=sys.stderr)
        return 1
    if args.format == "json":
        print(json.dumps(result, sort_keys=True))
    else:
        print(
            "Semantic provider proposal record is structurally complete: "
            f"{result['threats']} threats, {result['controls']} controls, "
            f"{result['verifications']} verifications."
        )
        print(
            f"Status: {result['status']}; "
            f"implementation={'allowed' if result['implementation_allowed'] else 'blocked'}; "
            "semantic-provider work="
            f"{'allowed' if result['semantic_provider_work_allowed'] else 'blocked'}; "
            f"{SEMANTIC_RELEASE_TAG} semantic-provider release="
            f"{'allowed' if result['semantic_provider_release_allowed'] else 'blocked'}."
        )
        for blocker in result["acceptance_blockers"]:
            print(f"Acceptance blocker: {blocker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
