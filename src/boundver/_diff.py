"""Diff operations between lockfiles for boundver."""

from typing import Dict

from ._lockfile import COMPONENT_METADATA_FIELDS


def diff_lockfiles(old: dict, new: dict) -> dict:
    """Produce a human-readable diff between two lockfiles."""
    result: Dict[str, dict] = {
        "components": {"added": [], "removed": [], "changed": [], "unchanged": []},
        "slices": {"added": [], "removed": [], "changed": [], "unchanged": []},
    }

    old_comps = old.get("components") or {}
    new_comps = new.get("components") or {}
    if not isinstance(old_comps, dict):
        old_comps = {}
    if not isinstance(new_comps, dict):
        new_comps = {}

    all_names = sorted(set(old_comps.keys()) | set(new_comps.keys()))
    for name in all_names:
        old_entry = old_comps.get(name) if isinstance(old_comps.get(name), dict) else {}
        new_entry = new_comps.get(name) if isinstance(new_comps.get(name), dict) else {}
        if name not in old_comps:
            result["components"]["added"].append({
                "name": name,
                "version": new_entry.get("version"),
            })
        elif name not in new_comps:
            result["components"]["removed"].append({
                "name": name,
                "version": old_entry.get("version"),
            })
        else:
            old_fp = old_entry.get("fingerprints", {})
            new_fp = new_entry.get("fingerprints", {})
            changes: Dict[str, dict] = {}
            for facet in ("exact", "behavior", "boundary", "compat"):
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
                    _summarize_change(changes)
                    if changes
                    else "component metadata changed"
                )
                result["components"]["changed"].append(entry)
            else:
                result["components"]["unchanged"].append(name)

    # Slice diffs
    old_slices = old.get("slices") or {}
    new_slices = new.get("slices") or {}
    if not isinstance(old_slices, dict):
        old_slices = {}
    if not isinstance(new_slices, dict):
        new_slices = {}
    for sname in sorted(set(old_slices.keys()) | set(new_slices.keys())):
        old_s = old_slices.get(sname) if isinstance(old_slices.get(sname), dict) else {}
        new_s = new_slices.get(sname) if isinstance(new_slices.get(sname), dict) else {}
        if sname not in old_slices:
            result["slices"]["added"].append({
                "name": sname,
                "fingerprint": new_s.get("fingerprint"),
            })
        elif sname not in new_slices:
            result["slices"]["removed"].append({
                "name": sname,
                "fingerprint": old_s.get("fingerprint"),
            })
        else:
            old_fp = old_s.get("fingerprint")
            new_fp = new_s.get("fingerprint")
            if old_fp != new_fp:
                result["slices"]["changed"].append({
                    "name": sname,
                    "old": old_fp,
                    "new": new_fp,
                })
            else:
                result["slices"]["unchanged"].append(sname)

    return result


def _summarize_change(changes: dict) -> str:
    facets = list(changes.keys())
    if facets == ["exact"]:
        return "implementation-only by declaration: exact content changed; declared behavior and boundary artifacts are unchanged"
    elif set(facets) == {"exact", "behavior"}:
        return "behavioral artifacts changed; declared boundary artifacts are unchanged"
    elif "boundary" in facets and "compat" not in facets:
        return "declared boundary changed; compatibility family is unchanged"
    elif "compat" in facets:
        return "BREAKING-policy signal: declared compatibility family changed"
    return "changed: " + ", ".join(facets)
