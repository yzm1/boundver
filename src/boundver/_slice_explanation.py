"""Read-only slice explanations; lock contents do not attest their source."""

from pathlib import Path
import subprocess

from ._consumer_graph import resolve_slice_components
from ._git import _capture_git_source_snapshot
from ._lockfile import (
    _generation_errors,
    _lockfile_schema_issues,
    _lockfile_structure_issues,
    _recompute_slice_entry,
    generate_lockfile,
)
from ._review import _config_lock_consistency_issues
from ._utils import (
    BoundedDiagnosticList,
    BoundverError,
    ConfigError,
    FACETS,
    SOURCE_MODE_SET,
    _bounded_exception_text,
    _bounded_json_dumps,
    _effective_component_facets,
)
from ._version_changes import resolved_version_change


SLICE_EXPLANATION_SCHEMA = "boundver-slice-explanation/v1"
MAX_SLICE_EXPLANATION_ROWS = 10_000
MAX_SLICE_EXPLANATION_BYTES = 4 * 1024 * 1024
MAX_SLICE_EXPLANATION_NAME_CHARS = 16_384


def analyze_slice_drift(
    config: dict,
    lock: dict,
    repo_root: Path,
    name: str,
    *,
    source="head",
    snapshot=None,
    limit=1000,
    allow_custom_providers=False,
) -> dict:
    """Explain an internally consistent current-contract lock, never repair it."""
    if type(limit) is not int or not 0 <= limit <= MAX_SLICE_EXPLANATION_ROWS:
        raise ConfigError("Slice explanation limit must be 0..10000")
    if (
        not isinstance(name, str)
        or not 0 < len(name) <= MAX_SLICE_EXPLANATION_NAME_CHARS
    ):
        raise ConfigError("Slice explanation requires a bounded nonempty slice name")
    if not isinstance(source, str) or source not in SOURCE_MODE_SET:
        raise ConfigError("Unknown slice comparison source")
    issues = BoundedDiagnosticList()
    result = {
        "schema": SLICE_EXPLANATION_SCHEMA,
        "slice": name,
        "mode": None,
        "complete": False,
        "comparable": False,
        "drifted": None,
        "slice_changed": None,
        "gating": None,
        "exit_code": 2,
        "previous": None,
        "current": None,
        "counts": None,
        "members": [],
        "source": {"mode": source, "commit": None, "tree": None},
        "locked_source": {"status": "unavailable", "commit": None},
        "presentation": {"limit": limit, "shown": 0, "omitted": 0, "truncated": False},
        "issues": issues,
    }
    try:
        if name not in config.get("slices", {}):
            raise ConfigError("Unknown slice; choose a name declared in config.slices")
        problems = [*_lockfile_schema_issues(lock), *_lockfile_structure_issues(lock)]
        if not problems:
            problems = [
                *_config_lock_consistency_issues(config, lock),
                *_generation_errors(lock),
            ]
        if problems:
            issues.extend(problems)
        else:
            definition = config["slices"][name]
            mode = definition.get("mode", "exact")
            result["mode"] = mode
            names = sorted(resolve_slice_components(definition, config["components"]))
            previous = lock["slices"][name]
            reconstructed = _recompute_slice_entry(
                name,
                definition,
                lock["components"],
                strict=False,
                graph_components=config["components"],
            )
            if any(
                previous.get(field) != reconstructed[field]
                for field in ("mode", "components", "component_digests", "fingerprint")
            ):
                raise ConfigError(
                    "Locked slice is inconsistent with its stored members; no reliable comparison"
                )
            if source != "working-tree" and snapshot is None:
                snapshot = _capture_git_source_snapshot(repo_root, source)
            if snapshot is not None:
                if source == "working-tree" or snapshot.source != source:
                    raise ConfigError(
                        "Captured source does not match the slice comparison source"
                    )
                result["source"].update(
                    commit=snapshot.head_oid, tree=snapshot.tree_oid
                )
            current_lock = generate_lockfile(
                config,
                repo_root,
                source=source,
                snapshot=snapshot,
                strict=False,
                allow_custom_providers=allow_custom_providers,
            )
            errors = _generation_errors(current_lock)
            if errors:
                issues.extend(errors)
            else:
                current = current_lock["slices"][name]
                counts = dict(
                    members=len(names),
                    changed_members=0,
                    contributing_members=0,
                    version_changes=0,
                    unavailable_versions=0,
                )
                for member in names:
                    old = lock["components"][member]
                    new = current_lock["components"][member]
                    for field in (
                        "path",
                        "boundary_provider",
                        "boundary_provider_version",
                        "consumers",
                        "external_consumers",
                    ):
                        if old.get(field) != new.get(field):
                            raise ConfigError(
                                "Member declaration/provider contract changed; fingerprints are incomparable"
                            )
                    if (
                        old["fingerprints"].get(mode) is None
                        or new["fingerprints"].get(mode) is None
                    ):
                        raise ConfigError(
                            "A slice member lacks its required facet; comparison is unavailable"
                        )
                    changes = {
                        facet: {
                            "previous": old["fingerprints"].get(facet),
                            "current": new["fingerprints"].get(facet),
                        }
                        for facet in FACETS
                        if old["fingerprints"].get(facet)
                        != new["fingerprints"].get(facet)
                    }
                    version = resolved_version_change(old, new)
                    contributes = mode in changes
                    counts["changed_members"] += bool(changes)
                    counts["contributing_members"] += contributes
                    counts["version_changes"] += version["status"] == "changed"
                    counts["unavailable_versions"] += version["status"] == "unavailable"
                    if len(result["members"]) < limit:
                        result["members"].append(
                            {
                                "component": member,
                                "contributes": contributes,
                                "changes": changes,
                                "version": version,
                            }
                        )
                changed = previous["fingerprint"] != current["fingerprint"]
                gating = any(
                    mode in _effective_component_facets(config, member)
                    for member in names
                )
                result.update(
                    complete=True,
                    comparable=True,
                    slice_changed=changed,
                    drifted=changed and gating,
                    gating=gating,
                    exit_code=int(changed and gating),
                    previous=previous["fingerprint"],
                    current=current["fingerprint"],
                    counts=counts,
                )
                shown = len(result["members"])
                result["presentation"].update(
                    shown=shown,
                    omitted=len(names) - shown,
                    truncated=shown < len(names),
                )
    except (BoundverError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        issues.append(_bounded_exception_text(exc))
    if not result["complete"]:
        result["members"] = []
    try:
        _bounded_json_dumps(
            result, indent=2, sort_keys=True, max_bytes=MAX_SLICE_EXPLANATION_BYTES - 1
        )
    except ValueError:
        result.update(
            complete=False,
            comparable=False,
            drifted=None,
            slice_changed=None,
            exit_code=2,
            counts=None,
            members=[],
        )
        result["presentation"].update(shown=0, omitted=0, truncated=False)
        issues.append(
            "Slice explanation exceeds its output budget; use --summary or a lower --limit"
        )
    return result
