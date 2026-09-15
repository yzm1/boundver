"""Read-only declaration coverage analysis."""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ._config import _expand_component_paths
from ._git import GitSourceSnapshot, _list_files_for_source
from ._utils import (
    _available_component_facets,
    _is_glob,
    _normalize_declared_path,
    _PathGlobOperation,
    _select_literal_path_prefix,
    GuardrailError,
)


COVERAGE_SCHEMA = "boundver-declaration-coverage/v1"
MAX_COVERAGE_LISTED_PATHS = 1_000
MAX_COVERAGE_PATH_CHARS = 32_768


def _json_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _matches_selector(
    path: str,
    normalized: str,
    operation: _PathGlobOperation,
) -> bool:
    if _is_glob(normalized):
        return operation.matches(path, normalized)
    operation.spend()
    prefix = normalized.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def _selected_paths(
    paths: Sequence[str],
    selectors: Sequence[str],
    operation: _PathGlobOperation,
) -> Set[str]:
    normalized = tuple(_normalize_declared_path(selector) for selector in selectors)
    selected: Set[str] = set()
    for selector in normalized:
        if _is_glob(selector):
            selected.update(
                path for path in paths if operation.matches(path, selector)
            )
        else:
            selected.update(_select_literal_path_prefix(paths, selector, operation))
    return selected


def _component_paths(
    all_files: Sequence[str],
    component_path: str,
    operation: _PathGlobOperation,
) -> List[str]:
    prefix = _normalize_declared_path(component_path).rstrip("/")
    return _select_literal_path_prefix(all_files, prefix, operation)


def _belongs_to_ownership_root(
    path: str,
    ownership_roots: Sequence[str],
    operation: _PathGlobOperation,
) -> bool:
    for root in ownership_roots:
        operation.spend()
        if path == root or path.startswith(root + "/"):
            return True
    return False


def _repo_path(component_path: str, relative_path: str) -> str:
    return f"{component_path.rstrip('/')}/{relative_path}" if relative_path else component_path


_NormalizedCoverageExclusion = Tuple[int, object, Tuple[str, ...], object]


def _normalized_coverage_exclusions(
    config: Mapping[str, object],
) -> List[_NormalizedCoverageExclusion]:
    """Validate selector shape once before matching omitted paths."""
    normalized: List[_NormalizedCoverageExclusion] = []
    coverage = config.get("coverage", {})
    if not isinstance(coverage, dict):
        return normalized
    exclusions = coverage.get("exclusions", [])
    if not isinstance(exclusions, list):
        return normalized
    for index, exclusion in enumerate(exclusions):
        if not isinstance(exclusion, dict):
            continue
        selectors = exclusion.get("paths", [])
        if not isinstance(selectors, list) or not all(
            isinstance(selector, str) for selector in selectors
        ):
            continue
        normalized.append(
            (
                index,
                exclusion.get("facets", []),
                tuple(_normalize_declared_path(selector) for selector in selectors),
                exclusion.get("reason", ""),
            )
        )
    return normalized


def _coverage_exclusion(
    exclusions: Sequence[_NormalizedCoverageExclusion],
    path: str,
    facet: str,
    operation: _PathGlobOperation,
) -> Optional[dict]:
    for index, facets, selectors, reason in exclusions:
        if facet not in facets:
            continue
        if any(_matches_selector(path, selector, operation) for selector in selectors):
            return {
                "path": path,
                "reason": reason,
                "declaration": f"/coverage/exclusions/{index}",
            }
    return None


def _report_files(
    snapshot: Optional[GitSourceSnapshot],
    repo_root: Path,
    source: str,
) -> List[str]:
    if snapshot is not None:
        paths = sorted(snapshot.entries)
    else:
        paths = sorted(_list_files_for_source(repo_root, ".", source))
    oversized = next(
        (path for path in paths if len(path) > MAX_COVERAGE_PATH_CHARS),
        None,
    )
    if oversized is not None:
        raise GuardrailError(
            "Declaration coverage path exceeds the "
            f"{MAX_COVERAGE_PATH_CHARS}-character machine-output limit"
        )
    return paths


def declaration_coverage(
    config: dict,
    repo_root: Path,
    *,
    source: str,
    snapshot: Optional[GitSourceSnapshot],
    inputs: dict,
) -> dict:
    """Return deterministic coverage evidence without changing lock semantics."""
    all_files = _report_files(snapshot, repo_root, source)
    operation = _PathGlobOperation("Declaration coverage")
    exclusions = _normalized_coverage_exclusions(config)
    components = config.get("components", {})
    component_reports: List[dict] = []
    complete_components: List[str] = []
    listed_paths = 0
    uncovered_count = 0
    excluded_count = 0

    ownership_roots: Set[str] = set()
    for component_name in sorted(components):
        component = components[component_name]
        component_path = _normalize_declared_path(component["path"]).rstrip("/")
        ownership_roots.add(component_path)
        for vendored_path in component.get("vendored_copies", []):
            ownership_roots.add(_normalize_declared_path(vendored_path).rstrip("/"))

        tracked = _component_paths(all_files, component_path, operation)
        relative = [
            path[len(component_path) + 1 :] if path != component_path else ""
            for path in tracked
        ]
        facet_reports: List[dict] = []
        available = sorted(_available_component_facets(component))
        for facet in available:
            if facet == "exact":
                selected_relative = set(relative)
                declaration = f"/components/{_json_pointer_token(component_name)}/path"
            elif facet == "behavior":
                selectors = component["behavior"].get("paths", [])
                selected_relative = _expand_component_paths(
                    repo_root,
                    component_path,
                    selectors,
                    source=source,
                    snapshot=snapshot,
                    _glob_operation=operation,
                )
                declaration = (
                    f"/components/{_json_pointer_token(component_name)}/behavior/paths"
                )
            elif facet == "boundary":
                selectors = component["boundary"].get("paths", [])
                selected_relative = _expand_component_paths(
                    repo_root,
                    component_path,
                    selectors,
                    source=source,
                    snapshot=snapshot,
                    _glob_operation=operation,
                )
                declaration = (
                    f"/components/{_json_pointer_token(component_name)}/boundary/paths"
                )
            else:
                # Compatibility identity is not a path-coverage selector.
                continue

            omitted = [
                _repo_path(component_path, path)
                for path in relative
                if path not in selected_relative
            ]
            uncovered: List[str] = []
            excluded: List[dict] = []
            for path in omitted:
                exclusion = _coverage_exclusion(exclusions, path, facet, operation)
                if exclusion is None:
                    uncovered.append(path)
                    uncovered_count += 1
                else:
                    excluded.append(exclusion)
                    excluded_count += 1
                listed_paths += 1
                if listed_paths > MAX_COVERAGE_LISTED_PATHS:
                    raise GuardrailError(
                        "Declaration coverage report exceeds the "
                        f"{MAX_COVERAGE_LISTED_PATHS}-path output limit; narrow "
                        "the component or coverage selectors"
                    )
            if uncovered or excluded:
                facet_reports.append(
                    {
                        "facet": facet,
                        "declaration": declaration,
                        "selected_files": len(selected_relative),
                        "uncovered_files": uncovered,
                        "excluded": excluded,
                    }
                )

        if facet_reports:
            component_reports.append(
                {
                    "component": component_name,
                    "path": component_path,
                    "tracked_files": len(tracked),
                    "available_facets": available,
                    "facets": facet_reports,
                }
            )
        else:
            complete_components.append(component_name)

    coverage = config.get("coverage", {})
    indicators = (
        coverage.get("source_indicators", [])
        if isinstance(coverage, dict)
        else []
    )
    source_files = sorted(
        _selected_paths(all_files, indicators, operation) if indicators else set()
    )
    sorted_ownership_roots = sorted(ownership_roots)
    unowned_by_directory: Dict[str, dict] = {}
    for path in source_files:
        if _belongs_to_ownership_root(path, sorted_ownership_roots, operation):
            continue
        directory = posixpath.dirname(path) or "."
        group = unowned_by_directory.setdefault(
            directory,
            {
                "directory": directory,
                "declaration": "/coverage/source_indicators",
                "uncovered_files": [],
                "excluded": [],
            },
        )
        exclusion = _coverage_exclusion(exclusions, path, "ownership", operation)
        if exclusion is None:
            group["uncovered_files"].append(path)
            uncovered_count += 1
        else:
            group["excluded"].append(exclusion)
            excluded_count += 1
        listed_paths += 1
        if listed_paths > MAX_COVERAGE_LISTED_PATHS:
            raise GuardrailError(
                "Declaration coverage report exceeds the "
                f"{MAX_COVERAGE_LISTED_PATHS}-path output limit; narrow "
                "coverage.source_indicators"
            )

    unowned_directories = [unowned_by_directory[key] for key in sorted(unowned_by_directory)]
    return {
        "schema": COVERAGE_SCHEMA,
        "complete": True,
        "ok": uncovered_count == 0,
        "issues": [],
        "inputs": inputs,
        "policy": {
            "source_indicators_declaration": "/coverage/source_indicators",
            "source_indicators": list(indicators),
        },
        "components": component_reports,
        "complete_components": complete_components,
        "unowned_source_directories": unowned_directories,
        "summary": {
            "tracked_files": len(all_files),
            "source_indicator_files": len(source_files),
            "component_facet_groups": sum(
                len(component["facets"]) for component in component_reports
            ),
            "unowned_source_directories": len(unowned_directories),
            "uncovered_files": uncovered_count,
            "excluded_files": excluded_count,
        },
    }
