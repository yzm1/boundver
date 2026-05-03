"""Diff operations between lockfiles for boundver."""

from typing import Dict, List


def diff_lockfiles(old: dict, new: dict) -> dict:
    """Produce a human-readable diff between two lockfiles."""
    result: Dict[str, dict] = {
        "components": {"added": [], "removed": [], "changed": [], "unchanged": []},
        "slices": {"changed": [], "unchanged": []},
    }

    old_comps = old.get("components", {})
    new_comps = new.get("components", {})

    all_names = sorted(set(old_comps.keys()) | set(new_comps.keys()))
    for name in all_names:
        if name not in old_comps:
            result["components"]["added"].append({
                "name": name,
                "version": new_comps[name].get("version"),
            })
        elif name not in new_comps:
            result["components"]["removed"].append({
                "name": name,
                "version": old_comps[name].get("version"),
            })
        else:
            old_fp = old_comps[name].get("fingerprints", {})
            new_fp = new_comps[name].get("fingerprints", {})
            changes: Dict[str, dict] = {}
            for facet in ("exact", "behavior", "boundary", "compat"):
                ov = old_fp.get(facet)
                nv = new_fp.get(facet)
                if ov != nv:
                    changes[facet] = {"old": ov, "new": nv}
            if changes:
                entry = {
                    "name": name,
                    "old_version": old_comps[name].get("version"),
                    "new_version": new_comps[name].get("version"),
                    "changed_facets": changes,
                }
                entry["summary"] = _summarize_change(changes)
                result["components"]["changed"].append(entry)
            else:
                result["components"]["unchanged"].append(name)

    # Slice diffs
    old_slices = old.get("slices", {})
    new_slices = new.get("slices", {})
    for sname in sorted(set(old_slices.keys()) | set(new_slices.keys())):
        old_fp = old_slices.get(sname, {}).get("fingerprint")
        new_fp = new_slices.get(sname, {}).get("fingerprint")
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
        return "implementation-only change (API stable)"
    elif set(facets) == {"exact", "behavior"}:
        return "behavioral contract changed (API shape stable)"
    elif "boundary" in facets and "compat" not in facets:
        return "declared boundary changed (compatibility unchanged)"
    elif "compat" in facets:
        return "BREAKING: compatibility family changed"
    return "changed: " + ", ".join(facets)
