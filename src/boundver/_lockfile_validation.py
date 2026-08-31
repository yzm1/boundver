"""Structural validation for persisted boundary lockfiles."""

from typing import List, Optional, Sequence, Set

from ._utils import (
    BoundedDiagnosticList,
    _bounded_diagnostic_repr,
    _bounded_diagnostic_text,
)


def is_sha256_digest(value: object) -> bool:
    """Return whether *value* is one canonical lowercase SHA-256 digest."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def lockfile_schema_issues(lockfile: dict, expected_schema: str) -> List[str]:
    """Validate the root schema identity before deeper inspection."""
    if not isinstance(lockfile, dict):
        return ["LOCKFILE malformed: root must be an object"]
    schema = lockfile.get("schema")
    if schema is None:
        return [f"LOCKFILE schema missing (expected {expected_schema})"]
    if schema != expected_schema:
        return [
            "LOCKFILE schema unsupported: "
            f"{_bounded_diagnostic_text(schema)} (expected {expected_schema})"
        ]
    return []


def append_unknown_field_issues(
    issues: List[str],
    value: object,
    allowed: Set[str],
    context: str,
) -> None:
    """Mirror JSON Schema ``additionalProperties: false`` without jsonschema."""
    if not isinstance(value, dict):
        return
    for field in value:
        if isinstance(issues, BoundedDiagnosticList) and issues.truncated:
            break
        if not isinstance(field, str):
            issues.append(f"LOCKFILE malformed: {context} field names must be strings")
        elif field not in allowed:
            issues.append(
                f"LOCKFILE malformed: unknown field in "
                f"{_bounded_diagnostic_text(context)}: "
                f"{_bounded_diagnostic_text(field)}"
            )


def lockfile_structure_issues(
    lockfile: dict,
    *,
    semantic_config_version: str,
    facets: Sequence[str],
    component_metadata_fields: Sequence[str],
    expected_schema: str,
    allowed_config_contracts: Optional[Set[str]] = None,
    running_version: Optional[str] = None,
) -> List[str]:
    """Validate the complete non-schema-engine lockfile structure."""
    issues = BoundedDiagnosticList()
    if not isinstance(lockfile, dict):
        return ["LOCKFILE malformed: root must be an object"]
    facet_set = frozenset(facets)
    accepted_contracts = (
        {semantic_config_version}
        if allowed_config_contracts is None
        else set(allowed_config_contracts)
    )
    append_unknown_field_issues(
        issues,
        lockfile,
        {
            "$schema",
            "schema",
            "config_contract",
            "config_digest",
            "project",
            "components",
            "slices",
        },
        "lockfile",
    )
    if "$schema" in lockfile and not isinstance(lockfile["$schema"], str):
        issues.append("LOCKFILE malformed: $schema must be a string")
    if not isinstance(lockfile.get("project"), str) or not lockfile.get("project"):
        issues.append("LOCKFILE malformed: project must be a non-empty string")
    config_contract = lockfile.get("config_contract")
    if not isinstance(config_contract, str) or config_contract not in accepted_contracts:
        contract_prefix = "boundver-semantic-config/v"
        read_only_historical = accepted_contracts != {semantic_config_version}
        if read_only_historical:
            supported = ", ".join(
                repr(contract) for contract in sorted(accepted_contracts)
            )
            if (
                isinstance(config_contract, str)
                and config_contract.startswith(contract_prefix)
                and config_contract[len(contract_prefix) :].isdigit()
            ):
                return [
                    "LOCKFILE semantic configuration contract unsupported for "
                    "this read-only comparison: "
                    f"{_bounded_diagnostic_repr(config_contract)}; supported "
                    f"contracts are {supported}"
                ]
            issues.append(
                "LOCKFILE malformed: config_contract must be one of "
                f"{supported} for this read-only comparison"
            )
        elif (
            isinstance(config_contract, str)
            and config_contract.startswith(contract_prefix)
            and config_contract[len(contract_prefix) :].isdigit()
        ):
            release = (
                f"boundver {running_version}"
                if isinstance(running_version, str) and running_version
                else "this boundver release"
            )
            schema = lockfile.get("schema", expected_schema)
            locked_contract_digits = config_contract[len(contract_prefix) :]
            expected_contract_digits = (
                semantic_config_version[len(contract_prefix) :]
                if semantic_config_version.startswith(contract_prefix)
                and semantic_config_version[len(contract_prefix) :].isdigit()
                else None
            )
            contract_order = 0
            if expected_contract_digits is not None:
                normalized_locked = locked_contract_digits.lstrip("0") or "0"
                normalized_expected = expected_contract_digits.lstrip("0") or "0"
                if len(normalized_locked) != len(normalized_expected):
                    contract_order = (
                        1
                        if len(normalized_locked) > len(normalized_expected)
                        else -1
                    )
                elif normalized_locked != normalized_expected:
                    contract_order = 1 if normalized_locked > normalized_expected else -1
            if expected_contract_digits is not None and contract_order > 0:
                direction = (
                    "This lock uses a newer semantic contract than the running "
                    f"{release}; upgrade the installation to the repository-pinned "
                    "exact boundver version. Do not overwrite the newer lock with "
                    "this older writer."
                )
            elif expected_contract_digits is not None and contract_order < 0:
                direction = (
                    "This lock uses an older semantic contract than the running "
                    f"{release}; install the repository-pinned boundver version, "
                    "upgrade writers and verifiers together, then regenerate with "
                    "`boundver generate` against the reviewed repository snapshot."
                )
            else:
                direction = (
                    "Install the repository's exact boundver pin before deciding "
                    "whether regeneration is required."
                )
            return [
                "LOCKFILE semantic configuration contract mismatch: "
                f"{_bounded_diagnostic_repr(schema)} uses "
                f"{_bounded_diagnostic_repr(config_contract)}, but {release} "
                f"requires {semantic_config_version!r}. Semantic digests cannot "
                "be relabelled or migrated without repository content. "
                f"{direction}"
            ]
        elif not read_only_historical:
            issues.append(
                "LOCKFILE malformed: config_contract must be "
                f"{semantic_config_version!r}"
            )
    if not is_sha256_digest(lockfile.get("config_digest")):
        issues.append(
            "LOCKFILE malformed: config_digest must be a lowercase SHA-256 digest"
        )
    if not isinstance(lockfile.get("components"), dict):
        issues.append("LOCKFILE malformed: components must be an object")
        return list(issues)
    if not isinstance(lockfile.get("slices"), dict):
        issues.append("LOCKFILE malformed: slices must be an object")
    for name, component in lockfile.get("components", {}).items():
        if issues.truncated:
            break
        if not isinstance(name, str) or not name:
            issues.append("LOCKFILE malformed: component names must be non-empty strings")
        name = _bounded_diagnostic_text(name)
        if not isinstance(component, dict):
            issues.append(f"LOCKFILE malformed: component '{name}' must be an object")
            continue
        append_unknown_field_issues(
            issues,
            component,
            set(component_metadata_fields) | {"fingerprints"},
            f"component '{name}'",
        )
        for field in ("version", "boundary_provider_version"):
            value = component.get(field)
            if field not in component or (
                value is not None and not isinstance(value, str)
            ):
                issues.append(
                    f"LOCKFILE malformed: component '{name}' {field} "
                    "must be a string or null"
                )
        for field in ("path", "boundary_provider"):
            if not isinstance(component.get(field), str) or not component.get(field):
                issues.append(
                    f"LOCKFILE malformed: component '{name}' {field} "
                    "must be a non-empty string"
                )
        if component.get("boundary_status") not in {"ok", "partial", "error"}:
            issues.append(
                f"LOCKFILE malformed: component '{name}' boundary_status must be "
                "one of ok, partial, or error"
            )
        for consumer_field in ("consumers", "external_consumers"):
            consumers = component.get(consumer_field)
            if (
                not isinstance(consumers, list)
                or not all(isinstance(item, str) for item in consumers)
                or len(consumers) != len(set(consumers))
            ):
                issues.append(
                    f"LOCKFILE malformed: component '{name}' {consumer_field} "
                    "must be an array of unique strings"
                )
        fingerprints = component.get("fingerprints")
        if not isinstance(fingerprints, dict):
            issues.append(
                f"LOCKFILE malformed: component '{name}' missing fingerprints object"
            )
        else:
            append_unknown_field_issues(
                issues,
                fingerprints,
                set(facets),
                f"component '{name}' fingerprints",
            )
            for required in facets:
                if issues.truncated:
                    break
                if required not in fingerprints:
                    issues.append(
                        f"LOCKFILE malformed: component '{name}' "
                        f"missing fingerprints.{required}"
                    )
                elif fingerprints[required] is not None and not is_sha256_digest(
                    fingerprints[required]
                ):
                    issues.append(
                        f"LOCKFILE malformed: component '{name}' "
                        f"fingerprints.{required} must be a lowercase SHA-256 "
                        "digest or null"
                    )
        semver = component.get("semver")
        if not isinstance(semver, dict):
            issues.append(
                f"LOCKFILE malformed: component '{name}' semver must be an object"
            )
        else:
            append_unknown_field_issues(
                issues,
                semver,
                {"compat_family", "api_surface", "exact_version"},
                f"component '{name}' semver",
            )
            for field in ("compat_family", "api_surface", "exact_version"):
                value = semver.get(field)
                if field not in semver or (
                    value is not None and not isinstance(value, str)
                ):
                    issues.append(
                        f"LOCKFILE malformed: component '{name}' semver.{field} "
                        "must be a string or null"
                    )
        for field in (
            "version_errors",
            "exact_errors",
            "behavior_errors",
            "boundary_errors",
            "warnings",
            "vendored_copies",
            "vendored_errors",
        ):
            value = component.get(field)
            if value is not None and (
                not isinstance(value, list)
                or not all(isinstance(item, str) for item in value)
            ):
                issues.append(
                    f"LOCKFILE malformed: component '{name}' {field} "
                    "must be an array of strings"
                )
        metadata = component.get("boundary_metadata")
        if metadata is not None and not isinstance(metadata, dict):
            issues.append(
                f"LOCKFILE malformed: component '{name}' boundary_metadata "
                "must be an object or null"
            )
        vendored_digests = component.get("vendored_digests")
        if vendored_digests is not None and (
            not isinstance(vendored_digests, dict)
            or not all(
                isinstance(key, str) and is_sha256_digest(value)
                for key, value in vendored_digests.items()
            )
        ):
            issues.append(
                f"LOCKFILE malformed: component '{name}' vendored_digests must "
                "be an object with lowercase SHA-256 digest values"
            )
    slices = lockfile.get("slices")
    if isinstance(slices, dict):
        for name, slice_entry in slices.items():
            if issues.truncated:
                break
            if not isinstance(name, str) or not name:
                issues.append("LOCKFILE malformed: slice names must be non-empty strings")
            name = _bounded_diagnostic_text(name)
            if not isinstance(slice_entry, dict):
                issues.append(f"LOCKFILE malformed: slice '{name}' must be an object")
                continue
            append_unknown_field_issues(
                issues,
                slice_entry,
                {
                    "description",
                    "mode",
                    "components",
                    "fingerprint",
                    "component_digests",
                },
                f"slice '{name}'",
            )
            if not is_sha256_digest(slice_entry.get("fingerprint")):
                issues.append(
                    f"LOCKFILE malformed: slice '{name}' fingerprint must be a "
                    "lowercase SHA-256 digest"
                )
            if not isinstance(slice_entry.get("description"), str):
                issues.append(
                    f"LOCKFILE malformed: slice '{name}' description must be a string"
                )
            if slice_entry.get("mode") not in facet_set:
                issues.append(
                    f"LOCKFILE malformed: slice '{name}' mode must be one of "
                    "exact, behavior, boundary, or compat"
                )
            components = slice_entry.get("components")
            if not isinstance(components, list) or not all(
                isinstance(item, str) for item in components
            ):
                issues.append(
                    f"LOCKFILE malformed: slice '{name}' components must be "
                    "an array of strings"
                )
            component_digests = slice_entry.get("component_digests")
            if (
                not isinstance(component_digests, dict)
                or not all(
                    isinstance(key, str)
                    and (value is None or is_sha256_digest(value))
                    for key, value in component_digests.items()
                )
            ):
                issues.append(
                    f"LOCKFILE malformed: slice '{name}' component_digests must "
                    "be an object with lowercase SHA-256 digest or null values"
                )
    return list(issues)
