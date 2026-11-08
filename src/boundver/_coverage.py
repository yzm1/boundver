"""Read-only declaration coverage analysis."""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ._config import _expand_component_paths
from ._git import GitSourceSnapshot, _list_files_for_source
from ._utils import (
    _available_component_facets,
    _bounded_json_dumps,
    _is_glob,
    _normalize_declared_path,
    _PathGlobOperation,
    _select_literal_path_prefix,
    GuardrailError,
)


COVERAGE_SCHEMA = "boundver-declaration-coverage/v2"
MAX_COVERAGE_LISTED_PATHS = 1_000
MAX_COVERAGE_PATH_CHARS = 32_768
MAX_COVERAGE_DETAIL_BYTES = 512 * 1024
MAX_COVERAGE_RESULT_BYTES = 4 * 1024 * 1024
MAX_COVERAGE_LIMIT = 10_000
MAX_COVERAGE_GROUPS = 10_000
MAX_COVERAGE_FILTERS = 256
MAX_COVERAGE_ANALYZED_PATHS = 1_000_000


class _CoverageDetails:
    """Bound presentation independently of analysis and strict-gate totals."""

    def __init__(self, limit: int):
        self.limit = limit
        self.shown = 0
        self.total = 0
        self.bytes = 0

    def record(self, group: dict, path: str, exclusion: Optional[dict]) -> None:
        key = "excluded" if exclusion is not None else "uncovered_files"
        count = "excluded_count" if exclusion is not None else "uncovered_count"
        group[count] += 1
        self.total += 1
        if self.total > MAX_COVERAGE_ANALYZED_PATHS:
            raise GuardrailError("Declaration coverage exceeds its analyzed-path work budget")
        if self.shown >= self.limit:
            return
        value = exclusion if exclusion is not None else path
        # Include conservative pretty-print/list overhead. Final serialization
        # independently enforces the complete-document byte bound.
        size = len(_bounded_json_dumps(value, ensure_ascii=True).encode("utf-8")) + 256
        if self.bytes + size > MAX_COVERAGE_DETAIL_BYTES:
            return
        group[key].append(value)
        self.bytes += size
        self.shown += 1


def _coverage_group(**fields) -> dict:
    return {**fields, "uncovered_files": [], "excluded": [],
            "uncovered_count": 0, "excluded_count": 0}


def _coverage_prefixes(prefixes: Sequence[str]) -> Tuple[str, ...]:
    if len(prefixes) > MAX_COVERAGE_FILTERS:
        raise ValueError(f"Coverage accepts at most {MAX_COVERAGE_FILTERS} path prefixes")
    normalized = []
    for prefix in prefixes:
        value = _normalize_declared_path(prefix)
        if _is_glob(value):
            raise ValueError("Coverage path prefixes must be literal, not globs")
        normalized.append(value)
    return tuple(sorted(set(normalized)))


def _in_coverage_scope(path: str, prefixes: Sequence[str], operation: _PathGlobOperation) -> bool:
    if not prefixes:
        return True
    return any(_matches_selector(path, prefix, operation) for prefix in prefixes)


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
    component_names: Optional[Sequence[str]] = None,
    path_prefixes: Sequence[str] = (),
    limit: int = MAX_COVERAGE_LISTED_PATHS,
    summary_only: bool = False,
) -> dict:
    """Analyze the selected scope fully; bound path presentation separately.

    Component selection excludes the repository-wide ownership audit. Literal
    path prefixes narrow both component path coverage and (when enabled)
    ownership. Config validation remains the caller's repository-wide gate.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= MAX_COVERAGE_LIMIT:
        raise ValueError(f"Coverage limit must be between 0 and {MAX_COVERAGE_LIMIT}")
    prefixes = _coverage_prefixes(path_prefixes)
    components = config.get("components", {})
    if component_names is not None:
        if not component_names or len(component_names) > MAX_COVERAGE_FILTERS:
            raise ValueError(f"Coverage requires 1..{MAX_COVERAGE_FILTERS} component filters")
        unknown = set(component_names) - set(components)
        if unknown:
            raise ValueError("Unknown coverage component(s): " + ", ".join(sorted(unknown)))
        selected_names = sorted(set(component_names))
    else:
        selected_names = sorted(components)
    all_files = _report_files(snapshot, repo_root, source)
    operation = _PathGlobOperation("Declaration coverage")
    exclusions = _normalized_coverage_exclusions(config)
    component_reports: List[dict] = []
    complete_components: List[str] = []
    details = _CoverageDetails(0 if summary_only else limit)
    uncovered_count = 0
    excluded_count = 0
    group_count = 0

    ownership_roots: Set[str] = set()
    for component in components.values():
        component_path = _normalize_declared_path(component["path"]).rstrip("/")
        ownership_roots.add(component_path)
        for vendored_path in component.get("vendored_copies", []):
            ownership_roots.add(_normalize_declared_path(vendored_path).rstrip("/"))

    for component_name in selected_names:
        component = components[component_name]
        component_path = _normalize_declared_path(component["path"]).rstrip("/")
        tracked = [path for path in _component_paths(all_files, component_path, operation)
                   if _in_coverage_scope(path, prefixes, operation)]
        relative = [
            path[len(component_path) + 1 :] if path != component_path else ""
            for path in tracked
        ]
        facet_reports: List[dict] = []
        available = sorted(_available_component_facets(component))
        for facet in available:
            if facet == "exact":
                # Exact already includes every in-scope tracked path.
                continue
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

            group_count += 1
            if group_count > MAX_COVERAGE_GROUPS:
                raise GuardrailError("Declaration coverage exceeds its analysis group budget")
            selected_relative = set(relative) & selected_relative
            omitted = [
                _repo_path(component_path, path)
                for path in relative
                if path not in selected_relative
            ]
            group = _coverage_group(facet=facet, declaration=declaration,
                                    selected_files=len(selected_relative))
            for path in omitted:
                exclusion = _coverage_exclusion(exclusions, path, facet, operation)
                if exclusion is None:
                    uncovered_count += 1
                else:
                    excluded_count += 1
                details.record(group, path, exclusion)
            facet_reports.append(group)

        component_reports.append(
            {
                "component": component_name,
                "path": component_path,
                "tracked_files": len(tracked),
                "available_facets": available,
                "facets": facet_reports,
            }
        )
        if not any(group["uncovered_count"] or group["excluded_count"] for group in facet_reports):
            complete_components.append(component_name)

    coverage = config.get("coverage", {})
    indicators = (
        coverage.get("source_indicators", [])
        if isinstance(coverage, dict)
        else []
    )
    ownership_analyzed = component_names is None
    source_files = sorted(path for path in (
        _selected_paths(all_files, indicators, operation)
        if indicators and ownership_analyzed else set()
    ) if _in_coverage_scope(path, prefixes, operation))
    sorted_ownership_roots = sorted(ownership_roots)
    unowned_by_directory: Dict[str, dict] = {}
    for path in source_files:
        if _belongs_to_ownership_root(path, sorted_ownership_roots, operation):
            continue
        directory = posixpath.dirname(path) or "."
        if directory not in unowned_by_directory:
            group_count += 1
            if group_count > MAX_COVERAGE_GROUPS:
                raise GuardrailError("Declaration coverage exceeds its analysis group budget")
            unowned_by_directory[directory] = _coverage_group(
                directory=directory, declaration="/coverage/source_indicators",
            )
        group = unowned_by_directory[directory]
        exclusion = _coverage_exclusion(exclusions, path, "ownership", operation)
        if exclusion is None:
            uncovered_count += 1
        else:
            excluded_count += 1
        details.record(group, path, exclusion)

    unowned_directories = [unowned_by_directory[key] for key in sorted(unowned_by_directory)]
    report = {
        "schema": COVERAGE_SCHEMA,
        "complete": True,
        "ok": uncovered_count == 0,
        "issues": [],
        "inputs": inputs,
        "scope": {
            "filtered": component_names is not None or bool(prefixes),
            "components": selected_names,
            "path_prefixes": list(prefixes),
            "ownership_analyzed": ownership_analyzed,
        },
        "presentation": {
            "summary_only": summary_only,
            "path_limit": details.limit,
            "detail_byte_limit": MAX_COVERAGE_DETAIL_BYTES,
            "total_paths": details.total,
            "shown_paths": details.shown,
            "omitted_paths": details.total - details.shown,
            "truncated": details.total > details.shown,
        },
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
    # Refuse pathological metadata before any output. This is an independent
    # work/result guardrail, not the old 1,000-path presentation abort.
    _bounded_json_dumps(report, indent=2, sort_keys=True,
                        max_bytes=MAX_COVERAGE_RESULT_BYTES - 1)
    return report
