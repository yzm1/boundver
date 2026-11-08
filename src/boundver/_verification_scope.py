"""Scope attribution is separate from the authoritative aggregate verdict."""


VERIFICATION_SCOPE_SCHEMA = "boundver-verification-scope/v1"


def verification_scope_result(args, receipt: dict, verification: dict) -> dict:
    """Wrap the legacy result without weakening or reinterpreting its issues.

    Unattributed diagnostics stay repository-level. In particular, metadata,
    slice and integrity failures are never assigned to a selected component
    merely because that component was requested.
    """
    compared = sorted(receipt.get("compared_components", []))
    if args.changed_from:
        selected = verification["changed_components"]
    else:
        selected = verification["components_filter"] or compared
    selected_set = set(selected)
    local_messages = set(receipt.get("component_diagnostics", []))
    findings = receipt.get("component_findings", [])
    selected_findings = [row for row in findings if row["component"] in selected_set]
    traversal_complete = receipt.get("traversal_complete", False)
    selected_complete = traversal_complete and not selected_set.intersection(receipt.get("unavailable_components", []))
    return {
        "schema": VERIFICATION_SCOPE_SCHEMA,
        "verification": verification,
        "requested": {"components_argument": args.components, "changed_from": args.changed_from or None},
        "selection": {
            "components": selected,
            "compared": sorted(selected_set & set(compared)),
            "fingerprint_findings": selected_findings,
            "comparison_complete": selected_complete,
            "fingerprint_drift": bool(selected_findings) if selected_complete else None,
        },
        "analysis": {
            "traversal_complete": traversal_complete,
            "computed_components": receipt.get("computed_components", []),
            "compared_components": compared,
            "version_components": receipt.get("version_components", []),
            "compared_slices": sorted(receipt.get("compared_slices", [])),
            "unavailable_components": receipt.get("unavailable_components", []),
            "repository_integrity_required": True,
        },
        "other_component_findings": [row for row in findings if row["component"] not in selected_set],
        "repository_issues": [issue for issue in verification["issues"] if issue not in local_messages],
    }
