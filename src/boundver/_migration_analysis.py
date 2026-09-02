"""Explain selector changes introduced after Boundver 0.10.

Version 0.10 delegated raw-provider and behavior glob declarations to
``fnmatch.fnmatchcase`` over the entire component-relative path, so ``*`` could
cross ``/``.  Current releases use deterministic segment-aware matching where
only a complete ``**`` segment crosses directories.  Canonical providers
rejected globs in 0.10, leaf providers ignored boundary paths, and custom
providers had provider-specific selection rules.  This module records those
cases explicitly instead of inventing a legacy match set.
"""

import posixpath
from bisect import bisect_left
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from ._git import GitSourceSnapshot, _list_files_for_source
from ._utils import (
    ConfigError,
    GuardrailError,
    _is_glob,
    _match_path_glob,
    _match_text_glob,
    _normalize_declared_path,
)
from .providers import MAX_PROVIDER_DECLARATIONS


MIGRATION_ANALYSIS_SCHEMA = "boundver-migration-analysis/v1"
MIGRATION_ANALYSIS_SCHEMA_URL = (
    "https://raw.githubusercontent.com/yzm1/boundver/v0.14.1/"
    "spec/cli-output.migrate-lock.schema.json"
)
MAX_ANALYZED_DECLARATIONS = min(MAX_PROVIDER_DECLARATIONS, 2_000)
MAX_SELECTOR_MATCH_EVALUATIONS = 5_000_000
MAX_SELECTOR_CHANGE_EXAMPLES = 5
MAX_SELECTOR_EXAMPLE_CHARS = 1_024
MAX_ANALYSIS_LABEL_CHARS = 4_096
MAX_ANALYSIS_SELECTOR_CHARS = 16_384

_V010_RAW_BOUNDARY_PROVIDERS = {
    "openapi",
    "openapi-raw",
    "json-file",
    "json-file-raw",
    "python-exports",
    "python-exports-raw",
    "typescript-exports",
    "typescript-exports-raw",
}
_V010_CANONICAL_BOUNDARY_PROVIDERS = {
    "json-canonical",
    "openapi-canonical",
}


def _preview_path(path: str) -> str:
    if len(path) <= MAX_SELECTOR_EXAMPLE_CHARS:
        return path
    return path[: MAX_SELECTOR_EXAMPLE_CHARS - 3] + "..."


def _bounded_label(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    if len(value) <= MAX_ANALYSIS_LABEL_CHARS:
        return value
    return value[: MAX_ANALYSIS_LABEL_CHARS - 3] + "..."


def _bounded_selector(value: str) -> str:
    if len(value) <= MAX_ANALYSIS_SELECTOR_CHARS:
        return value
    return value[: MAX_ANALYSIS_SELECTOR_CHARS - 3] + "..."


def _component_files(
    repo_root: Path,
    component_path: str,
    source: str,
    snapshot: Optional[GitSourceSnapshot],
    *,
    path_index: Optional[Sequence[str]] = None,
    step_consumer: Optional[Callable[[int], None]] = None,
) -> List[str]:
    if path_index is None and snapshot is not None:
        snapshot_paths = (
            snapshot.tracked_paths
            if snapshot.source == "working-tree"
            else snapshot.entries
        )
        path_index = tuple(sorted(snapshot_paths))
    if path_index is not None:
        if component_path in {"", "."}:
            if step_consumer is not None:
                step_consumer(len(path_index))
            return list(path_index)
        prefix = component_path.rstrip("/") + "/"
        # Every candidate beginning with ``prefix`` sorts before the same
        # component spelling followed by the character after '/'. Two binary
        # searches replace a full captured-tree scan for every component.
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        if step_consumer is not None:
            search_steps = 2 * max(1, len(path_index).bit_length())
            step_consumer(search_steps)
        start = bisect_left(path_index, prefix)
        stop = bisect_left(path_index, upper)
        selected = path_index[start:stop]
        if step_consumer is not None:
            step_consumer(len(selected))
        return [path[len(prefix) :] for path in selected]

    if component_path in {"", "."}:
        repo_files = _list_files_for_source(repo_root, ".", source)
        if step_consumer is not None:
            step_consumer(len(repo_files))
        return sorted(repo_files)
    repo_files = _list_files_for_source(repo_root, component_path, source)
    if step_consumer is not None:
        step_consumer(len(repo_files))
    prefix = component_path.rstrip("/") + "/"
    return sorted(path[len(prefix) :] for path in repo_files if path.startswith(prefix))


def _literal_matches(
    files: List[str],
    selector: str,
    *,
    step_consumer=None,
) -> List[str]:
    prefix = selector.rstrip("/")
    if selector.endswith("/"):
        # v0.10 passed the trailing slash through to Git's literal pathspec.
        # It selected descendants of a directory, but not an identically
        # named regular file. Git file listings never contain directory rows.
        matches = []
        for candidate in files:
            if step_consumer is not None:
                step_consumer(1)
            if candidate.startswith(prefix + "/"):
                matches.append(candidate)
        return matches
    directory = False
    for candidate in files:
        if step_consumer is not None:
            step_consumer(1)
        if candidate.startswith(prefix + "/"):
            directory = True
            break
    if directory:
        matches = []
        for candidate in files:
            if step_consumer is not None:
                step_consumer(1)
            if candidate == prefix or candidate.startswith(prefix + "/"):
                matches.append(candidate)
        return matches
    matches = []
    for candidate in files:
        if step_consumer is not None:
            step_consumer(1)
        if candidate == prefix:
            matches.append(candidate)
    return matches


def _boundary_analysis_status(provider: str, is_glob: bool) -> tuple:
    """Return the exact v0.10 comparability classification and detail."""
    if provider in _V010_RAW_BOUNDARY_PROVIDERS or provider == "implicit":
        return "compared", None
    if provider == "path-hash":
        return (
            "legacy-rejected",
            "Boundver 0.10 did not register path-hash as a public boundary "
            "provider",
        )
    if provider in _V010_CANONICAL_BOUNDARY_PROVIDERS:
        if not is_glob:
            return "compared", None
        return (
            "legacy-rejected",
            f"Boundver 0.10 rejected glob selectors for the {provider} "
            "boundary provider",
        )
    if provider == "leaf":
        return (
            "not-applicable",
            "The leaf boundary provider ignored boundary.paths in Boundver 0.10",
        )
    detail = (
        f"Selector semantics for boundary provider {provider!r} cannot be "
        "inferred from the Boundver 0.10 built-ins"
    )
    return "provider-specific", _bounded_label(detail)


def _uncompared_declaration(
    *,
    component: str,
    facet: str,
    provider: Optional[str],
    selector: str,
    is_glob: bool,
    analysis_status: str,
    detail: str,
) -> dict:
    return {
        "component": component,
        "facet": facet,
        "provider": provider,
        "selector": selector,
        "selector_kind": "glob" if is_glob else "literal",
        "analysis_status": analysis_status,
        "detail": detail,
        "impact": "not-comparable",
        "legacy_match_count": None,
        "current_match_count": None,
        "legacy_only_count": None,
        "current_only_count": None,
        "legacy_only_examples": [],
        "current_only_examples": [],
        "legacy_only_omitted": 0,
        "current_only_omitted": 0,
    }


def _prepare_component_declarations(
    component_name: object,
    component: object,
) -> tuple:
    """Validate and normalize one component without enumerating source files."""
    if (
        not isinstance(component_name, str)
        or not component_name
        or len(component_name) > MAX_ANALYSIS_LABEL_CHARS
    ):
        raise ConfigError(
            "Component names must be non-empty strings within the "
            f"{MAX_ANALYSIS_LABEL_CHARS}-character analysis limit"
        )
    if not isinstance(component, dict):
        raise ConfigError(f"Component '{component_name}' must be an object")
    raw_component_path = component.get("path")
    if not isinstance(raw_component_path, str) or not raw_component_path.strip():
        raise ConfigError(
            f"Component '{component_name}' must define a non-empty string path"
        )
    # The v0.10 generator stripped and normpath-normalized component roots
    # before any provider saw them. Use POSIX spelling here because the v0.10
    # schema required '/' separators and Git paths are POSIX on every host.
    legacy_component_path = posixpath.normpath(raw_component_path.strip())
    if legacy_component_path == ".":
        component_path = "."
    else:
        try:
            component_path = _normalize_declared_path(legacy_component_path)
        except ValueError as exc:
            raise ConfigError(
                f"Component '{component_name}' legacy-normalized path {exc}"
            ) from exc

    boundary = component.get("boundary")
    if not isinstance(boundary, dict):
        raise ConfigError(f"Component '{component_name}' boundary must be an object")
    provider = boundary.get("provider")
    if (
        not isinstance(provider, str)
        or not provider
        or len(provider) > MAX_ANALYSIS_LABEL_CHARS
    ):
        raise ConfigError(
            f"Component '{component_name}' boundary.provider must be a "
            f"non-empty string within the {MAX_ANALYSIS_LABEL_CHARS}-character "
            "analysis limit"
        )
    declaration_groups = [("boundary", provider, boundary.get("paths", []))]
    behavior = component.get("behavior")
    if behavior is not None:
        if not isinstance(behavior, dict):
            raise ConfigError(
                f"Component '{component_name}' behavior must be an object"
            )
        declaration_groups.append(("behavior", None, behavior.get("paths", [])))

    normalized_declarations = []
    for facet, facet_provider, declarations in declaration_groups:
        if not isinstance(declarations, list) or any(
            not isinstance(item, str) for item in declarations
        ):
            raise ConfigError(
                f"Component '{component_name}' {facet}.paths must be a string array"
            )
        for declaration in sorted(declarations):
            if not declaration:
                raise ConfigError(
                    f"Component '{component_name}' {facet} selector must not be empty"
                )
            # Boundver 0.10 stripped declarations immediately before both glob
            # detection and matching. Preserve that exact legacy input even
            # when the declaration is not valid under the current contract.
            legacy_selector = declaration.strip()
            is_glob = _is_glob(legacy_selector)
            if facet == "boundary":
                legacy_status, legacy_detail = _boundary_analysis_status(
                    provider, is_glob
                )
            else:
                legacy_status, legacy_detail = "compared", None
            try:
                current_selector = _normalize_declared_path(declaration)
            except ValueError as exc:
                current_error = exc
                current_selector = None
            else:
                current_error = None
            if legacy_status != "compared":
                analysis_status, detail = legacy_status, legacy_detail
            elif current_error is not None:
                legacy_preview = _preview_path(legacy_selector)
                detail = _bounded_label(
                    "Current Boundver rejects this declaration "
                    f"({current_error}); Boundver 0.10 trimmed and evaluated "
                    f"{legacy_preview!r}"
                )
                analysis_status = "current-rejected"
            else:
                analysis_status, detail = "compared", None
            normalized_declarations.append(
                (
                    facet,
                    facet_provider,
                    _bounded_selector(declaration),
                    legacy_selector,
                    current_selector,
                    is_glob,
                    analysis_status,
                    detail,
                )
            )
    return component_name, component_path, normalized_declarations


def analyze_selector_migration(
    config: dict,
    repo_root: Path,
    *,
    source: str,
    snapshot: Optional[GitSourceSnapshot],
    lock_path: str,
    lock_schema: object,
    migration_action: str,
    migration_reason: Optional[str],
) -> dict:
    components = config.get("components")
    if not isinstance(components, dict):
        raise ConfigError("Config field 'components' must be an object")

    declaration_count = 0
    evaluations = 0

    def spend_evaluations(amount: int) -> None:
        """Charge deterministic matching work across the complete analysis.

        One unit is a captured-source index range/selected-path operation,
        legacy candidate test, literal-path candidate test, or current glob
        NFA transition. Charging both source selection and matcher transitions
        closes the multiplicative gaps around this operation-wide limit.
        """
        nonlocal evaluations
        if amount < 0:
            raise GuardrailError("Migration selector analysis work is invalid")
        if amount > MAX_SELECTOR_MATCH_EVALUATIONS - evaluations:
            raise GuardrailError(
                "Migration selector analysis exceeds the "
                f"{MAX_SELECTOR_MATCH_EVALUATIONS}-step aggregate matching-work "
                "limit"
            )
        evaluations += amount

    results = []
    changed_count = 0
    compared_count = 0
    uncompared_count = 0
    legacy_only_total = 0
    current_only_total = 0

    prepared_components = []
    for component_name, component in sorted(components.items()):
        prepared = _prepare_component_declarations(component_name, component)
        declaration_count += len(prepared[2])
        if declaration_count > MAX_ANALYZED_DECLARATIONS:
            raise GuardrailError(
                "Migration selector analysis exceeds the "
                f"{MAX_ANALYZED_DECLARATIONS}-declaration limit"
            )
        prepared_components.append(prepared)

    needs_source_paths = any(
        analysis_status == "compared"
        for _name, _path, declarations in prepared_components
        for _, _, _, _, _, _, analysis_status, _ in declarations
    )
    captured_path_index = (
        tuple(
            sorted(
                snapshot.tracked_paths
                if snapshot.source == "working-tree"
                else snapshot.entries
            )
        )
        if snapshot is not None and needs_source_paths
        else None
    )

    # Validate and globally count every declaration before the first source
    # path listing, so an over-limit later component cannot consume matching
    # work for earlier components.
    for component_name, component_path, normalized_declarations in prepared_components:
        # A component with no boundary/behavior selectors contributes no
        # analysis work. Enforce and validate declaration limits before the
        # potentially large Git path listing.
        if not normalized_declarations:
            continue
        needs_files = any(
            analysis_status == "compared"
            for _, _, _, _, _, _, analysis_status, _ in normalized_declarations
        )
        files = (
            _component_files(
                repo_root,
                component_path,
                source,
                snapshot,
                path_index=captured_path_index,
                step_consumer=spend_evaluations,
            )
            if needs_files
            else []
        )

        for (
            facet,
            facet_provider,
            selector_label,
            legacy_selector,
            current_selector,
            is_glob,
            analysis_status,
            detail,
        ) in normalized_declarations:
            if analysis_status != "compared":
                uncompared_count += 1
                results.append(
                    _uncompared_declaration(
                        component=component_name,
                        facet=facet,
                        provider=facet_provider,
                        selector=selector_label,
                        is_glob=is_glob,
                        analysis_status=analysis_status,
                        detail=detail or "The declaration cannot be compared",
                    )
                )
                continue
            compared_count += 1
            assert current_selector is not None
            if is_glob:
                legacy = []
                current = []
                for candidate in files:
                    if _match_text_glob(
                        candidate,
                        legacy_selector,
                        _step_consumer=spend_evaluations,
                    ):
                        legacy.append(candidate)
                for candidate in files:
                    if _match_path_glob(
                        candidate,
                        current_selector,
                        _step_consumer=spend_evaluations,
                    ):
                        current.append(candidate)
            else:
                # Charge every actual directory-inference and selection scan
                # for both legacy and current literal semantics.
                legacy = _literal_matches(
                    files,
                    legacy_selector,
                    step_consumer=spend_evaluations,
                )
                current = _literal_matches(
                    files,
                    current_selector,
                    step_consumer=spend_evaluations,
                )
            legacy_set = set(legacy)
            current_set = set(current)
            legacy_only = sorted(legacy_set - current_set)
            current_only = sorted(current_set - legacy_set)
            if legacy_only and current_only:
                impact = "changed"
            elif legacy_only:
                impact = "narrowed"
            elif current_only:
                impact = "broadened"
            else:
                impact = "unchanged"
            changed = impact != "unchanged"
            changed_count += int(changed)
            legacy_only_total += len(legacy_only)
            current_only_total += len(current_only)
            results.append(
                {
                    "component": component_name,
                    "facet": facet,
                    "provider": facet_provider,
                    "selector": selector_label,
                    "selector_kind": "glob" if is_glob else "literal",
                    "analysis_status": "compared",
                    "detail": None,
                    "impact": impact,
                    "legacy_match_count": len(legacy_set),
                    "current_match_count": len(current_set),
                    "legacy_only_count": len(legacy_only),
                    "current_only_count": len(current_only),
                    "legacy_only_examples": [
                        _preview_path(path)
                        for path in legacy_only[:MAX_SELECTOR_CHANGE_EXAMPLES]
                    ],
                    "current_only_examples": [
                        _preview_path(path)
                        for path in current_only[:MAX_SELECTOR_CHANGE_EXAMPLES]
                    ],
                    "legacy_only_omitted": max(
                        0, len(legacy_only) - MAX_SELECTOR_CHANGE_EXAMPLES
                    ),
                    "current_only_omitted": max(
                        0, len(current_only) - MAX_SELECTOR_CHANGE_EXAMPLES
                    ),
                }
            )

    tree_oid = (
        snapshot.tree_oid
        if snapshot is not None and source in {"head", "index"}
        else None
    )
    return {
        "$schema": MIGRATION_ANALYSIS_SCHEMA_URL,
        "schema": MIGRATION_ANALYSIS_SCHEMA,
        "lock": {
            "path": lock_path,
            "source_schema": _bounded_label(lock_schema),
            "action": migration_action,
            "reason": migration_reason,
        },
        "source": {"mode": source, "tree_oid": tree_oid},
        "summary": {
            "declaration_count": declaration_count,
            "compared_declaration_count": compared_count,
            "uncompared_declaration_count": uncompared_count,
            "changed_declaration_count": changed_count,
            "legacy_only_match_count": legacy_only_total,
            "current_only_match_count": current_only_total,
            "match_evaluations": evaluations,
        },
        "declarations": results,
    }
