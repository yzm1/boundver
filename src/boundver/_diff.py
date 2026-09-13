"""Diff operations between lockfiles for boundver."""

from typing import Dict

from ._lockfile import (
    COMPONENT_METADATA_FIELDS,
    DIFFABLE_LOCK_CONTRACTS,
)
from ._utils import FACETS, LockfileError, _bounded_diagnostic_repr


LOCKFILE_METADATA_FIELDS = ("project", "config_contract", "config_digest")


def _diff_mapping(lockfile: dict, field: str) -> dict:
    """Return one required diff mapping without coercing malformed input."""
    value = lockfile.get(field, {})
    if not isinstance(value, dict):
        raise LockfileError(f"lockfile {field} must be an object")
    for name, entry in value.items():
        if not isinstance(name, str) or not name:
            raise LockfileError(
                f"lockfile {field} names must be non-empty strings; got "
                f"{_bounded_diagnostic_repr(name)}"
            )
        if not isinstance(entry, dict):
            raise LockfileError(
                f"lockfile {field} entry {_bounded_diagnostic_repr(name)} "
                "must be an object"
            )
        if field == "components" and not isinstance(entry.get("fingerprints"), dict):
            raise LockfileError(
                f"lockfile component {_bounded_diagnostic_repr(name)} "
                "must contain a fingerprints object"
            )
    return value


def require_compatible_lockfile_schemas(old: dict, new: dict) -> None:
    """Reject cross-schema or unsupported-schema diffs with one clear error."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise LockfileError("lockfiles must each contain a JSON object")
    old_schema = old.get("schema")
    new_schema = new.get("schema")
    if old_schema != new_schema:
        raise LockfileError(
            "lockfiles use incompatible schemas "
            f"(old={_bounded_diagnostic_repr(old_schema)}, "
            f"new={_bounded_diagnostic_repr(new_schema)}); regenerate both "
            "lockfiles with the same Boundver version before diffing"
        )
    if (
        not isinstance(old_schema, str)
        or old_schema not in DIFFABLE_LOCK_CONTRACTS
    ):
        supported = ", ".join(repr(item) for item in DIFFABLE_LOCK_CONTRACTS)
        raise LockfileError(
            "lockfiles use unsupported schema "
            f"{_bounded_diagnostic_repr(old_schema)} (supported schemas: "
            f"{supported}); regenerate both lockfiles with a supported "
            "Boundver version before diffing"
        )


def diff_lockfiles(old: dict, new: dict) -> dict:
    """Produce a human-readable diff between two lockfiles."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise LockfileError("lockfiles must each contain a JSON object")
    result: Dict[str, dict] = {
        "changed_metadata": {},
        "components": {"added": [], "removed": [], "changed": [], "unchanged": []},
        "slices": {"added": [], "removed": [], "changed": [], "unchanged": []},
    }

    for field in LOCKFILE_METADATA_FIELDS:
        old_value = old.get(field)
        new_value = new.get(field)
        if old_value != new_value:
            result["changed_metadata"][field] = {
                "old": old_value,
                "new": new_value,
            }

    old_comps = _diff_mapping(old, "components")
    new_comps = _diff_mapping(new, "components")

    all_names = sorted(set(old_comps.keys()) | set(new_comps.keys()))
    for name in all_names:
        old_entry = old_comps.get(name, {})
        new_entry = new_comps.get(name, {})
        if name not in old_comps:
            result["components"]["added"].append(
                {
                    "name": name,
                    "version": new_entry.get("version"),
                }
            )
        elif name not in new_comps:
            result["components"]["removed"].append(
                {
                    "name": name,
                    "version": old_entry.get("version"),
                }
            )
        else:
            old_fp = old_entry.get("fingerprints", {})
            new_fp = new_entry.get("fingerprints", {})
            changes: Dict[str, dict] = {}
            for facet in FACETS:
                ov = old_fp.get(facet)
                nv = new_fp.get(facet)
                if ov != nv:
                    changes[facet] = {"old": ov, "new": nv}
            metadata_changes: Dict[str, dict] = {}
            for field in COMPONENT_METADATA_FIELDS:
                old_value = old_entry.get(field)
                new_value = new_entry.get(field)
                if old_value != new_value:
                    metadata_changes[field] = {"old": old_value, "new": new_value}
            if changes or metadata_changes:
                entry = {
                    "name": name,
                    "old_version": old_entry.get("version"),
                    "new_version": new_entry.get("version"),
                    "changed_facets": changes,
                    "changed_metadata": metadata_changes,
                }
                entry["summary"] = (
                    _summarize_change(changes, old_fp, new_fp)
                    if changes
                    else "component metadata changed"
                )
                result["components"]["changed"].append(entry)
            else:
                result["components"]["unchanged"].append(name)

    # Slice diffs
    old_slices = _diff_mapping(old, "slices")
    new_slices = _diff_mapping(new, "slices")
    for sname in sorted(set(old_slices.keys()) | set(new_slices.keys())):
        old_s = old_slices.get(sname, {})
        new_s = new_slices.get(sname, {})
        if sname not in old_slices:
            result["slices"]["added"].append(
                {
                    "name": sname,
                    "fingerprint": new_s.get("fingerprint"),
                }
            )
        elif sname not in new_slices:
            result["slices"]["removed"].append(
                {
                    "name": sname,
                    "fingerprint": old_s.get("fingerprint"),
                }
            )
        else:
            old_fp = old_s.get("fingerprint")
            new_fp = new_s.get("fingerprint")
            metadata_changes: Dict[str, dict] = {}
            metadata_fields = sorted((set(old_s) | set(new_s)) - {"fingerprint"})
            for field in metadata_fields:
                old_value = old_s.get(field)
                new_value = new_s.get(field)
                if old_value != new_value:
                    metadata_changes[field] = {
                        "old": old_value,
                        "new": new_value,
                    }
            if old_fp != new_fp or metadata_changes:
                result["slices"]["changed"].append(
                    {
                        "name": sname,
                        "old": old_fp,
                        "new": new_fp,
                        "changed_metadata": metadata_changes,
                    }
                )
            else:
                result["slices"]["unchanged"].append(sname)

    return result


def _summarize_change(
    changes: dict,
    old_fingerprints: dict | None = None,
    new_fingerprints: dict | None = None,
) -> str:
    available = {
        facet
        for facet in FACETS
        if any(
            isinstance(fingerprints, dict)
            and fingerprints.get(facet) is not None
            for fingerprints in (old_fingerprints, new_fingerprints)
        )
    }
    facets = list(changes.keys())
    if facets == ["exact"]:
        unchanged = []
        absent = []
        for facet in ("behavior", "boundary"):
            if facet in available:
                unchanged.append(facet)
            else:
                absent.append(facet)
        if unchanged and not absent:
            detail = "declared behavior and boundary artifacts are unchanged"
        elif unchanged:
            detail = (
                f"declared {unchanged[0]} artifact is unchanged; "
                f"no {absent[0]} artifact is declared"
            )
        else:
            detail = "no behavior or boundary artifact is declared"
        return f"implementation-only by declaration: exact content changed; {detail}"
    elif set(facets) == {"exact", "behavior"}:
        boundary = (
            "declared boundary artifact is unchanged"
            if "boundary" in available
            else "no boundary artifact is declared"
        )
        return f"behavioral artifacts changed; {boundary}"
    elif "boundary" in facets and "compat" not in facets:
        compat = (
            "compatibility family is unchanged"
            if "compat" in available
            else "no compatibility identity is declared"
        )
        return f"declared boundary changed; {compat}"
    elif "compat" in facets:
        return "BREAKING-policy signal: declared compatibility family changed"
    return "changed: " + ", ".join(facets)
