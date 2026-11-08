"""Read-only lock changes, with explicit limits on comparability and provenance."""

from ._lockfile import (
    DIFFABLE_LOCK_CONTRACTS,
    _generation_errors,
    _lockfile_schema_issues,
    _lockfile_structure_issues,
)
from ._utils import BoundedDiagnosticList, ConfigError, FACETS, _bounded_json_dumps
from ._version_changes import resolved_version_change
from ._hashing import canonical_json


CHANGE_REPORT_SCHEMA = "boundver-lock-changes/v1"
MAX_CHANGE_REPORT_ROWS = 10_000
MAX_CHANGE_REPORT_BYTES = 4 * 1024 * 1024


def lock_change_report(
    previous,
    current,
    *,
    source,
    operation="generate-preview",
    limit=1000,
    historical=False,
):
    """Observe changes; never accept old digests or infer their source commit."""
    if type(limit) is not int or not 0 <= limit <= MAX_CHANGE_REPORT_ROWS:
        raise ConfigError("Change report limit must be 0..10000")
    if operation not in {"generate-preview", "lock-diff"}:
        raise ConfigError("Unknown lock change report operation")
    if historical and operation != "lock-diff":
        raise ConfigError(
            "Historical inspection is read-only lock-diff only; never generation"
        )
    if historical and previous is None:
        raise ConfigError("Historical inspection requires two explicit lock records")
    issues = BoundedDiagnosticList()
    result = {
        "schema": CHANGE_REPORT_SCHEMA,
        "operation": operation,
        "publication": "not-written",
        "current_lock_verification": "not-performed",
        "complete": False,
        "exit_code": 2,
        "source": source,
        "locked_source": {"status": "unavailable", "commit": None},
        "comparison": {
            "previous_present": previous is not None,
            "contract_compatible": None,
            "declarations_match": None,
            "historical": historical,
            "previous_contract": None,
            "current_contract": None,
        },
        "changed_headers": [],
        "counts": None,
        "components": [],
        "slices": [],
        "presentation": {"limit": limit, "shown": 0, "omitted": 0, "truncated": False},
        "issues": issues,
    }
    for label, lock in (("current", current), ("previous", previous)):
        if lock is None and label == "previous":
            continue
        schema = lock.get("schema") if isinstance(lock, dict) else None
        if historical and isinstance(schema, str) and schema in DIFFABLE_LOCK_CONTRACTS:
            problems = [
                *_lockfile_schema_issues(lock, expected_schema=schema),
                *_lockfile_structure_issues(
                    lock,
                    expected_schema=schema,
                    allowed_config_contracts=set(DIFFABLE_LOCK_CONTRACTS[schema]),
                ),
            ]
        else:
            problems = [
                *_lockfile_schema_issues(lock),
                *_lockfile_structure_issues(lock),
            ]
        if problems:
            issues.extend(f"{label}: {message}" for message in problems)
    if issues:
        return result
    old = previous or {"components": {}, "slices": {}}
    contract_equal = previous is not None and all(
        old.get(field) == current.get(field) for field in ("schema", "config_contract")
    )
    declarations_equal = contract_equal and all(
        old.get(field) == current.get(field) for field in ("config_digest", "project")
    )
    result["comparison"].update(
        contract_compatible=contract_equal if previous is not None else None,
        declarations_match=declarations_equal if contract_equal else None,
        previous_contract={
            field: old.get(field)
            for field in ("schema", "config_contract", "config_digest", "project")
        }
        if previous is not None
        else None,
        current_contract={
            field: current.get(field)
            for field in ("schema", "config_contract", "config_digest", "project")
        },
    )
    result["changed_headers"] = [
        field
        for field in (
            "$schema",
            "schema",
            "config_contract",
            "config_digest",
            "project",
        )
        if old.get(field) != current.get(field)
    ]
    counts = {
        key: 0
        for key in (
            "components",
            "added",
            "removed",
            "changed",
            "unchanged",
            "incomparable",
            "unavailable",
            "observed_common_changes",
            "version_changes",
            "unavailable_versions",
            "slices",
            "changed_slices",
            "incomparable_slices",
            "unavailable_slices",
            "equal_component_values",
            "different_component_values",
        )
    }
    total_rows = 0
    incomparable_components = set()
    unavailable_components = set()
    for name in sorted(set(old["components"]) | set(current["components"])):
        before = old["components"].get(name)
        after = current["components"].get(name)
        counts["components"] += 1
        values_equal = (
            None
            if before is None or after is None
            else canonical_json(before) == canonical_json(after)
        )
        if values_equal is not None:
            counts[
                "equal_component_values"
                if values_equal
                else "different_component_values"
            ] += 1
        if before is None or after is None:
            status = "added" if before is None else "removed"
            comparable = False
            reason = "component has no counterpart"
            changes = {}
            metadata = []
        else:
            unavailable = False
            if not contract_equal:
                reason = "digest or semantic-config contracts differ"
            elif not declarations_equal:
                reason = "declared configuration or project differs"
            elif any(
                before.get(field) != after.get(field)
                for field in ("path", "boundary_provider", "boundary_provider_version")
            ):
                reason = "provider or path contracts differ"
            elif _generation_errors(
                {"components": {name: before}}
            ) or _generation_errors({"components": {name: after}}):
                reason = "computed values are unavailable"
                unavailable = True
            else:
                reason = None
            comparable = reason is None
            changes = {
                facet: {
                    "previous": before["fingerprints"].get(facet),
                    "current": after["fingerprints"].get(facet),
                }
                for facet in FACETS
                if before["fingerprints"].get(facet) != after["fingerprints"].get(facet)
            }
            metadata = [
                field
                for field in sorted((set(before) | set(after)) - {"fingerprints"})
                if (field in before) != (field in after)
                or canonical_json(before.get(field)) != canonical_json(after.get(field))
            ]
            observed = not values_equal
            counts["observed_common_changes"] += observed
            status = (
                ("changed" if observed else "unchanged")
                if comparable
                else "unavailable"
                if unavailable
                else "incomparable"
            )
        if status == "unavailable":
            unavailable_components.add(name)
        if not comparable:
            incomparable_components.add(name)
        version = resolved_version_change(before or {}, after or {})
        counts[status] += 1
        counts["version_changes"] += version["status"] == "changed"
        counts["unavailable_versions"] += version["status"] == "unavailable"
        if total_rows < limit:
            result["components"].append(
                {
                    "component": name,
                    "status": status,
                    "comparable": comparable,
                    "reason": reason,
                    "facets": changes,
                    "metadata_fields": metadata,
                    "version": version,
                    "values_equal": values_equal,
                    "provider_contract": {
                        "previous": {
                            field: before.get(field)
                            for field in (
                                "boundary_provider",
                                "boundary_provider_version",
                            )
                        }
                        if before
                        else None,
                        "current": {
                            field: after.get(field)
                            for field in (
                                "boundary_provider",
                                "boundary_provider_version",
                            )
                        }
                        if after
                        else None,
                    },
                }
            )
        total_rows += 1
    for name in sorted(set(old["slices"]) | set(current["slices"])):
        before, after = old["slices"].get(name), current["slices"].get(name)
        counts["slices"] += 1
        if before is None or after is None:
            status = "added" if before is None else "removed"
        elif not contract_equal or not declarations_equal:
            status = "incomparable"
        elif unavailable_components.intersection(
            set(before.get("components", [])) | set(after.get("components", []))
        ):
            status = "unavailable"
        elif incomparable_components.intersection(
            set(before.get("components", [])) | set(after.get("components", []))
        ):
            status = "incomparable"
        else:
            status = "changed" if before != after else "unchanged"
        counts["changed_slices"] += status in {"added", "removed", "changed"}
        counts["incomparable_slices"] += status == "incomparable"
        counts["unavailable_slices"] += status == "unavailable"
        if total_rows < limit:
            result["slices"].append(
                {
                    "slice": name,
                    "status": status,
                    "previous": before.get("fingerprint") if before else None,
                    "current": after.get("fingerprint") if after else None,
                    "values_equal": None
                    if before is None or after is None
                    else canonical_json(before) == canonical_json(after),
                    "previous_mode": before.get("mode") if before else None,
                    "current_mode": after.get("mode") if after else None,
                    "metadata_fields": [
                        field
                        for field in sorted(
                            (set(before or {}) | set(after or {})) - {"fingerprint"}
                        )
                        if (field in (before or {})) != (field in (after or {}))
                        or canonical_json((before or {}).get(field))
                        != canonical_json((after or {}).get(field))
                    ],
                }
            )
        total_rows += 1
    shown = len(result["components"]) + len(result["slices"])
    result.update(complete=True, exit_code=0, counts=counts)
    result["presentation"].update(
        shown=shown, omitted=total_rows - shown, truncated=shown < total_rows
    )
    try:
        _bounded_json_dumps(
            result, indent=2, sort_keys=True, max_bytes=MAX_CHANGE_REPORT_BYTES - 1
        )
    except ValueError:
        result.update(
            complete=False, exit_code=2, counts=None, components=[], slices=[]
        )
        result["presentation"].update(shown=0, omitted=0, truncated=False)
        issues.append(
            "Change report exceeds its output budget; use --summary or a lower --limit"
        )
    return result
