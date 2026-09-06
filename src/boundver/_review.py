"""Historical, read-only facet and downstream-impact range analysis."""

from __future__ import annotations

from collections import deque
import os
from pathlib import Path
import subprocess
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from ._config import find_config_file, load_config_file, validate_config
from ._consumer_graph import resolve_slice_components
from ._diff import require_compatible_lockfile_schemas
from ._facet_policy import facet_policy_payload
from ._git import (
    GitSourceSnapshot,
    _capture_git_ref_snapshot,
    _git_merge_base,
    _git_repository_is_shallow,
    _git_run,
    _resolve_git_commit,
    _validated_git_object_id,
)
from ._lockfile import (
    COMPONENT_METADATA_FIELDS,
    LOCKFILE_SCHEMA,
    SEMANTIC_CONFIG_VERSION,
    _generation_errors,
    _lockfile_schema_issues,
    _lockfile_structure_issues,
    load_lockfile_file,
    semantic_config_digest,
    verify_lockfile,
)
from ._structural_review import structural_boundary_changes
from ._utils import (
    DIAGNOSTIC_TRUNCATION_SENTINEL,
    FACETS,
    FACET_SET,
    BoundverError,
    ConfigError,
    GuardrailError,
    LockfileError,
    _bounded_json_dumps,
)


REVIEW_SCHEMA = "boundver-review/v1"
MAX_REVIEW_WORK_STEPS = 250_000
MAX_REVIEW_RESULT_ROWS = 100_000
MAX_REVIEW_RESULT_BYTES = 64 * 1024 * 1024
MAX_REVIEW_RECONCILIATION_CANDIDATES = 8


class _ReviewWorkBudget:
    """One aggregate work ceiling shared by every graph/slice traversal."""

    def __init__(self) -> None:
        self.steps = 0
        self.result_rows = 0
        self.result_bytes = 0

    def spend(self, amount: int = 1) -> None:
        self.ensure(amount)
        self.steps += amount

    def ensure(self, amount: int) -> None:
        """Reject known work before allocating traversal-wide state."""
        if type(amount) is not int or amount < 0:
            raise ValueError("Range review work must be a non-negative integer")
        if amount > MAX_REVIEW_WORK_STEPS - self.steps:
            raise GuardrailError(
                "Range review graph and slice analysis exceeds the "
                f"{MAX_REVIEW_WORK_STEPS}-step aggregate limit. Reduce graph "
                "fan-out or slice membership, or use direct impact instead of "
                "--transitive. No partial review result was emitted."
            )

    def reserve_row(self, *values: object, overhead: int = 128) -> None:
        """Reserve one repeated result row before retaining its values."""
        self.result_rows += 1
        self.result_bytes += overhead
        for value in values:
            if isinstance(value, str):
                self.result_bytes += len(
                    value.encode("utf-8", errors="backslashreplace")
                )
        if (
            self.result_rows > MAX_REVIEW_RESULT_ROWS
            or self.result_bytes > MAX_REVIEW_RESULT_BYTES
        ):
            raise GuardrailError(
                "Range review result exceeds the aggregate "
                f"{MAX_REVIEW_RESULT_ROWS}-row or "
                f"{MAX_REVIEW_RESULT_BYTES}-byte construction limit. Reduce "
                "graph fan-out or use direct impact instead of --transitive. "
                "No partial review result was emitted."
            )


def parse_review_endpoints(
    range_expression: Optional[str],
    base_ref: Optional[str],
    target_ref: Optional[str],
) -> Tuple[str, str]:
    """Normalize positional or explicit review endpoints without guessing."""
    if range_expression:
        if base_ref is not None or target_ref is not None:
            raise ConfigError(
                "Use either BASE..TARGET or --base BASE --target TARGET, not both"
            )
        if "..." in range_expression or range_expression.count("..") != 1:
            raise ConfigError(
                "Review range must use exactly BASE..TARGET; add --merge-base "
                "for merge-base semantics"
            )
        base, target = range_expression.split("..", 1)
        if not base or not target:
            raise ConfigError("Review range must name both BASE and TARGET")
        return base, target
    if base_ref is None or target_ref is None:
        raise ConfigError(
            "Review requires BASE..TARGET or both --base BASE and --target TARGET"
        )
    return base_ref, target_ref


def parse_review_facets(raw: str) -> Optional[List[str]]:
    """Return a canonical explicit facet override, or ``None`` for config policy."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    requested = {value.strip() for value in raw.split(",") if value.strip()}
    unknown = requested - FACET_SET
    if unknown:
        raise ConfigError(
            "Unknown review facet(s): " + ", ".join(sorted(unknown))
        )
    if not requested:
        raise ConfigError("--facets must name at least one review facet")
    return [facet for facet in FACETS if facet in requested]


def _source_label(sources: Iterable[str]) -> str:
    values = set(sources)
    if values == {"base", "target"}:
        return "both"
    if values == {"base"}:
        return "base"
    if values == {"target"}:
        return "target"
    raise ConfigError("Internal review provenance is incomplete")


def _sorted_mapping_key_union(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    budget: _ReviewWorkBudget,
) -> List[str]:
    """Return one key union only when every resulting row can be visited."""
    budget.ensure(max(len(before), len(after)))
    union_size = len(before) + sum(1 for key in after if key not in before)
    budget.ensure(union_size)
    names = list(before)
    names.extend(key for key in after if key not in before)
    names.sort()
    return names


def _endpoint_path(repo_root: Path, path: Path) -> str:
    root = Path(os.path.abspath(repo_root))
    candidate = path if path.is_absolute() else root / path
    candidate = Path(os.path.abspath(candidate))
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError as exc:
        raise ConfigError(
            f"Review input path must stay within the repository: {path}"
        ) from exc


def _config_lock_consistency_issues(config: dict, lockfile: dict) -> List[str]:
    """Return endpoint inconsistencies that make historical reconstruction unsafe."""
    issues: List[str] = []
    if lockfile.get("project") != config.get("project", "unknown"):
        issues.append("project differs between config and lock")
    if lockfile.get("config_digest") != semantic_config_digest(config):
        issues.append("config_digest does not describe the endpoint config")

    configured_components = config.get("components", {})
    locked_components = lockfile.get("components", {})
    if set(configured_components) != set(locked_components):
        issues.append("component set differs between config and lock")
    for name in sorted(set(configured_components) & set(locked_components)):
        configured = configured_components[name]
        locked = locked_components[name]
        if not isinstance(configured, dict) or not isinstance(locked, dict):
            continue
        for field in ("consumers", "external_consumers"):
            configured_value = sorted(configured.get(field, []))
            locked_value = sorted(locked.get(field, []))
            if configured_value != locked_value:
                issues.append(
                    f"component {name!r} {field} differs between config and lock"
                )

    configured_slices = config.get("slices", {})
    locked_slices = lockfile.get("slices", {})
    if set(configured_slices) != set(locked_slices):
        issues.append("slice set differs between config and lock")
    for name in sorted(set(configured_slices) & set(locked_slices)):
        definition = configured_slices[name]
        locked = locked_slices[name]
        if not isinstance(definition, dict) or not isinstance(locked, dict):
            continue
        expected_members = resolve_slice_components(
            definition,
            configured_components,
        )
        if locked.get("components") != expected_members:
            issues.append(
                f"slice {name!r} membership differs between config and lock"
            )
        if locked.get("mode") != definition.get("mode", "exact"):
            issues.append(f"slice {name!r} mode differs between config and lock")
    return issues


def _first_parent_ancestors(repo_root: Path, commit: str) -> List[str]:
    """Return enough nearest-first ancestors to detect a truncated search."""
    result = _git_run(
        repo_root,
        [
            "rev-list",
            "--first-parent",
            f"--max-count={MAX_REVIEW_RECONCILIATION_CANDIDATES + 2}",
            commit,
            "--",
        ],
    )
    commits = [
        _validated_git_object_id(line, "git rev-list")
        for line in result.stdout.splitlines()
        if line
    ]
    if not commits or commits[0] != commit:
        raise ValueError("Git first-parent history did not begin at the endpoint")
    return commits[1:]


def _reconciliation_candidates(
    ancestors: List[str],
) -> Tuple[List[Tuple[str, int]], bool]:
    """Select the nearest ancestors without guessing which files reconciled them."""
    selected = [
        (candidate, distance)
        for distance, candidate in enumerate(
            ancestors[:MAX_REVIEW_RECONCILIATION_CANDIDATES],
            1,
        )
    ]
    return selected, len(ancestors) > MAX_REVIEW_RECONCILIATION_CANDIDATES


def _reconciled_checkpoint_hint(
    repo_root: Path,
    commit: str,
    *,
    config_hint: str,
    lock_hint: str,
    has_custom_providers: bool,
) -> str:
    """Return bounded recovery guidance without weakening endpoint validation."""
    if has_custom_providers:
        return (
            "Checkpoint search was skipped because the endpoint declares "
            "custom providers; choose a known reconciled checkpoint explicitly."
        )
    try:
        ancestors = _first_parent_ancestors(repo_root, commit)
        candidates, candidates_truncated = _reconciliation_candidates(
            ancestors,
        )
    except (OSError, subprocess.CalledProcessError, BoundverError, ValueError):
        return "Checkpoint search could not inspect first-parent history."

    for candidate, distance in candidates:
        try:
            candidate_snapshot = _capture_git_ref_snapshot(
                repo_root,
                candidate,
                label="reconciliation ancestor",
            )
            _load_review_endpoint(
                repo_root,
                candidate_snapshot,
                label="ancestor",
                config_hint=config_hint,
                lock_hint=lock_hint,
                allow_custom_providers=False,
                include_reconciliation_hint=False,
            )
        except GuardrailError:
            return (
                "Checkpoint search stopped after a candidate exceeded a safety "
                "guardrail; the endpoint remains unreconciled."
            )
        except (BoundverError, ValueError, OSError):
            continue
        return (
            f"Reconciled checkpoint found by bounded first-parent search: "
            f"{candidate} "
            f"({distance} {'commit' if distance == 1 else 'commits'} back)."
        )

    candidate_count = len(candidates)
    if candidates_truncated:
        candidate_unit = "commit" if candidate_count == 1 else "commits"
        candidate_scope = (
            f"the nearest {candidate_count} first-parent {candidate_unit}"
        )
    else:
        candidate_unit = "commit" if candidate_count == 1 else "commits"
        candidate_scope = f"{candidate_count} available first-parent {candidate_unit}"
    shallow = False
    try:
        shallow = _git_repository_is_shallow(repo_root)
    except (OSError, subprocess.CalledProcessError, BoundverError, ValueError):
        pass
    remediation = (
        " Fetch complete history first (GitHub Actions: fetch-depth: 0; "
        "GitLab: GIT_DEPTH: 0)."
        if shallow
        else ""
    )
    return (
        f"No reconciled checkpoint was found among {candidate_scope}."
        f"{remediation}"
    )


def _load_review_endpoint(
    repo_root: Path,
    snapshot: GitSourceSnapshot,
    *,
    label: str,
    config_hint: str,
    lock_hint: str,
    allow_custom_providers: bool,
    include_reconciliation_hint: bool = True,
) -> Tuple[dict, dict, Path, Path]:
    config_path = find_config_file(repo_root, config_hint, snapshot=snapshot)
    lock_path = repo_root / lock_hint
    try:
        config = load_config_file(
            config_path,
            repo_root=repo_root,
            snapshot=snapshot,
        )
        lockfile = load_lockfile_file(
            lock_path,
            repo_root=repo_root,
            snapshot=snapshot,
        )
    except FileNotFoundError as exc:
        raise ConfigError(f"{label} endpoint is incomplete: {exc}") from exc

    config_issues = validate_config(
        config,
        repo_root,
        source="head",
        snapshot=snapshot,
        require_slice_facets=False,
        validate_provider_runtime=False,
    )
    if config_issues:
        raise ConfigError(
            f"{label} endpoint config is invalid ({len(config_issues)} issues):\n"
            + "\n".join(config_issues)
        )

    lock_issues = [
        *_lockfile_schema_issues(lockfile),
        *_lockfile_structure_issues(lockfile),
    ]
    if lock_issues:
        raise LockfileError(
            f"{label} endpoint lock is invalid ({len(lock_issues)} issues):\n"
            + "\n".join(lock_issues)
        )
    if lockfile.get("schema") != LOCKFILE_SCHEMA:
        raise LockfileError(
            f"{label} endpoint uses {lockfile.get('schema')!r}; range review "
            f"requires {LOCKFILE_SCHEMA!r}"
        )
    if lockfile.get("config_contract") != SEMANTIC_CONFIG_VERSION:
        raise LockfileError(
            f"{label} endpoint uses {lockfile.get('config_contract')!r}; "
            f"reliable policy reconstruction requires {SEMANTIC_CONFIG_VERSION!r}"
        )
    generation_errors = _generation_errors(lockfile)
    if generation_errors:
        raise LockfileError(
            f"{label} endpoint lock contains incomplete digests:\n"
            + "\n".join(generation_errors)
        )
    consistency_issues = _config_lock_consistency_issues(config, lockfile)
    if consistency_issues:
        raise LockfileError(
            f"{label} endpoint config and lock are not reconciled:\n"
            + "\n".join(consistency_issues)
        )

    observations: List[str] = []
    drifted_components: Set[str] = set()
    integrity_issues = verify_lockfile(
        config,
        lockfile,
        repo_root,
        source="head",
        allow_custom_providers=allow_custom_providers,
        facets=[],
        observations=observations,
        snapshot=snapshot,
        drifted_components=drifted_components,
    )
    endpoint_drift = [*integrity_issues, *observations]
    if endpoint_drift:
        commit = snapshot.head_oid
        if commit is None:
            raise ValueError("Review snapshot is missing its exact commit identity")
        component_count = len(drifted_components)
        diagnostics_truncated = DIAGNOSTIC_TRUNCATION_SENTINEL in endpoint_drift
        component_summary = (
            f" in {'at least ' if diagnostics_truncated else ''}{component_count} "
            f"{'component' if component_count == 1 else 'components'}"
            if component_count
            else ""
        )
        header = (
            "Range review compares reconciled endpoint commits; "
            f"{label} commit {commit} has unreconciled drift"
            f"{component_summary}."
        )
        guidance = (
            "Reconcile and commit that endpoint's lock before review, or choose "
            "two existing reconciled commits."
        )
        hint = ""
        if include_reconciliation_hint:
            try:
                hint = _reconciled_checkpoint_hint(
                    repo_root,
                    commit,
                    config_hint=config_hint,
                    lock_hint=lock_hint,
                    has_custom_providers=bool(config.get("providers")),
                )
            except Exception:
                # Checkpoint discovery is recovery guidance. An unexpected
                # auxiliary failure must not replace the original fail-closed
                # endpoint verdict.
                hint = (
                    "Checkpoint search was unavailable; the endpoint remains "
                    "unreconciled."
                )
        preview_limit = 20
        preview = endpoint_drift[:preview_limit]
        if len(endpoint_drift) > preview_limit:
            preview.append(
                f"+{len(endpoint_drift) - preview_limit} additional endpoint issues"
            )
        raise LockfileError(
            "\n".join(
                [
                    header,
                    *([hint] if hint else []),
                    guidance,
                    "Observed endpoint drift:",
                    *preview,
                ]
            )
        )
    return config, lockfile, config_path, lock_path


def _walk_consumer_graph(
    components: Mapping[str, Any],
    seed: str,
    *,
    transitive: bool,
    budget: _ReviewWorkBudget,
) -> Tuple[Set[str], Set[str], Set[Tuple[str, str, str]]]:
    internal: Set[str] = set()
    external: Set[str] = set()
    edges: Set[Tuple[str, str, str]] = set()
    seen = {seed}
    pending = deque([seed])
    while pending:
        budget.spend()
        source = pending.popleft()
        component = components.get(source, {})
        if not isinstance(component, dict):
            continue
        consumers = component.get("consumers", [])
        if isinstance(consumers, list):
            budget.ensure(len(consumers))
            previous: object = object()
            for consumer in sorted(consumers):
                if consumer == previous:
                    continue
                previous = consumer
                budget.spend()
                if not isinstance(consumer, str) or consumer not in components:
                    continue
                edges.add((source, consumer, "component"))
                if consumer != seed:
                    internal.add(consumer)
                if transitive and consumer not in seen:
                    seen.add(consumer)
                    pending.append(consumer)
        terminals = component.get("external_consumers", [])
        if isinstance(terminals, list):
            budget.ensure(len(terminals))
            previous = object()
            for terminal in sorted(terminals):
                if terminal == previous:
                    continue
                previous = terminal
                budget.spend()
                if not isinstance(terminal, str):
                    continue
                edges.add((source, terminal, "external"))
                external.add(terminal)
        if not transitive:
            break
    return internal, external, edges


def _consumer_impact(
    base_components: Mapping[str, Any],
    target_components: Mapping[str, Any],
    component_name: str,
    *,
    trigger_facets: List[str],
    graph_changed: bool,
    transitive: bool,
    budget: _ReviewWorkBudget,
) -> dict:
    component_sources: Dict[str, Set[str]] = {}
    external_sources: Dict[str, Set[str]] = {}
    edge_sources: Dict[Tuple[str, str, str], Set[str]] = {}
    for source, components in (
        ("base", base_components),
        ("target", target_components),
    ):
        internal, external, edges = _walk_consumer_graph(
            components,
            component_name,
            transitive=transitive,
            budget=budget,
        )
        for name in internal:
            component_sources.setdefault(name, set()).add(source)
        for name in external:
            external_sources.setdefault(name, set()).add(source)
        for edge in edges:
            edge_sources.setdefault(edge, set()).add(source)
    budget.ensure(
        len(component_sources) + len(external_sources) + len(edge_sources)
    )
    component_rows = []
    for name, sources in sorted(component_sources.items()):
        budget.spend()
        budget.reserve_row(name)
        component_rows.append({"name": name, "source": _source_label(sources)})
    external_rows = []
    for name, sources in sorted(external_sources.items()):
        budget.spend()
        budget.reserve_row(name)
        external_rows.append({"name": name, "source": _source_label(sources)})
    edge_rows = []
    for edge, sources in sorted(edge_sources.items()):
        budget.spend()
        budget.reserve_row(*edge)
        edge_rows.append(
            {
                "from": edge[0],
                "to": edge[1],
                "kind": edge[2],
                "source": _source_label(sources),
            }
        )
    return {
        "component": component_name,
        "trigger_facets": trigger_facets,
        "graph_changed": graph_changed,
        "transitive": transitive,
        "components": component_rows,
        "external_consumers": external_rows,
        "edges": edge_rows,
    }


def _component_transitions(
    base_lock: dict,
    target_lock: dict,
    base_policy: dict,
    target_policy: dict,
    base_config: dict,
    target_config: dict,
    *,
    transitive: bool,
    budget: _ReviewWorkBudget,
) -> Tuple[List[dict], List[str], List[dict]]:
    base_components = base_lock["components"]
    target_components = target_lock["components"]
    changed: List[dict] = []
    unchanged: List[str] = []
    impacts: List[dict] = []
    for name in _sorted_mapping_key_union(
        base_components,
        target_components,
        budget,
    ):
        budget.spend()
        before = base_components.get(name)
        after = target_components.get(name)
        status = (
            "added" if before is None else "removed" if after is None else "changed"
        )
        before_entry = before if isinstance(before, dict) else {}
        after_entry = after if isinstance(after, dict) else {}
        before_fingerprints = before_entry.get("fingerprints", {})
        after_fingerprints = after_entry.get("fingerprints", {})
        base_selected = set(base_policy["components"].get(name, []))
        target_selected = set(target_policy["components"].get(name, []))
        facet_changes = []
        for facet in FACETS:
            old = before_fingerprints.get(facet)
            new = after_fingerprints.get(facet)
            if old == new:
                continue
            facet_changes.append(
                {
                    "facet": facet,
                    "before": old,
                    "after": new,
                    "selected": {
                        "base": facet in base_selected,
                        "target": facet in target_selected,
                        "effective": facet in base_selected or facet in target_selected,
                    },
                }
            )
        metadata_changes = {}
        if before is not None and after is not None:
            for field in COMPONENT_METADATA_FIELDS:
                old = before_entry.get(field)
                new = after_entry.get(field)
                if old != new:
                    metadata_changes[field] = {"before": old, "after": new}
        if before is not None and after is not None and not facet_changes and not metadata_changes:
            unchanged.append(name)
            continue

        trigger_facets = [
            change["facet"]
            for change in facet_changes
            if change["facet"] in {"boundary", "compat"}
        ]
        graph_changed = (
            status != "changed"
            and bool(
                before_entry.get("consumers", [])
                or before_entry.get("external_consumers", [])
                or after_entry.get("consumers", [])
                or after_entry.get("external_consumers", [])
            )
        ) or bool(
            {"consumers", "external_consumers"} & set(metadata_changes)
        )
        record = {
            "name": name,
            "status": status,
            "before_version": before_entry.get("version"),
            "after_version": after_entry.get("version"),
            "facets": facet_changes,
            "metadata": metadata_changes,
        }
        budget.reserve_row(name, status)
        changed.append(record)
        if trigger_facets or graph_changed:
            impacts.append(
                _consumer_impact(
                    base_config["components"],
                    target_config["components"],
                    name,
                    trigger_facets=trigger_facets,
                    graph_changed=graph_changed,
                    transitive=transitive,
                    budget=budget,
                )
            )
    return changed, unchanged, impacts


def _slice_transitions(
    base_lock: dict,
    target_lock: dict,
    *,
    budget: _ReviewWorkBudget,
) -> Tuple[List[dict], List[str]]:
    base_slices = base_lock["slices"]
    target_slices = target_lock["slices"]
    changed: List[dict] = []
    unchanged: List[str] = []
    metadata_fields = ("description", "mode", "components", "component_digests")
    for name in _sorted_mapping_key_union(base_slices, target_slices, budget):
        budget.spend()
        before = base_slices.get(name)
        after = target_slices.get(name)
        status = (
            "added" if before is None else "removed" if after is None else "changed"
        )
        before_entry = before if isinstance(before, dict) else {}
        after_entry = after if isinstance(after, dict) else {}
        metadata = {}
        if before is not None and after is not None:
            for field in metadata_fields:
                old = before_entry.get(field)
                new = after_entry.get(field)
                if old != new:
                    metadata[field] = {"before": old, "after": new}
        if (
            before is not None
            and after is not None
            and before_entry.get("fingerprint") == after_entry.get("fingerprint")
            and not metadata
        ):
            unchanged.append(name)
            continue
        changed.append(
            {
                "name": name,
                "status": status,
                "before": before_entry.get("fingerprint"),
                "after": after_entry.get("fingerprint"),
                "metadata": metadata,
            }
        )
        budget.reserve_row(name, status)
    return changed, unchanged


def _slice_impact(
    base_lock: dict,
    target_lock: dict,
    changed_components: List[dict],
    consumer_impacts: List[dict],
    changed_slices: List[dict],
    *,
    budget: _ReviewWorkBudget,
) -> List[dict]:
    roles: Dict[str, Set[str]] = {}
    budget.ensure(len(changed_components))
    for component in changed_components:
        budget.spend()
        roles[component["name"]] = {"changed"}
    for impact in consumer_impacts:
        budget.spend()
        for component in impact["components"]:
            budget.spend()
            roles.setdefault(component["name"], set()).add("consumer")

    membership: Dict[str, Dict[str, Set[str]]] = {}
    slice_sources: Dict[str, Set[str]] = {}
    for source, lockfile in (("base", base_lock), ("target", target_lock)):
        for slice_name, definition in lockfile["slices"].items():
            budget.spend()
            members = definition.get("components", [])
            if not isinstance(members, list):
                continue
            for component in members:
                budget.spend()
                if component not in roles:
                    continue
                slice_sources.setdefault(slice_name, set()).add(source)
                membership.setdefault(slice_name, {}).setdefault(
                    component,
                    set(),
                ).add(source)

    changed_by_name = {}
    budget.ensure(len(changed_slices))
    for item in changed_slices:
        budget.spend()
        changed_by_name[item["name"]] = item
    for name, transition in changed_by_name.items():
        if name in slice_sources:
            continue
        sources = set()
        if transition["status"] != "added":
            sources.add("base")
        if transition["status"] != "removed":
            sources.add("target")
        slice_sources[name] = sources

    budget.ensure(len(slice_sources))
    result = []
    for name, sources in sorted(slice_sources.items()):
        budget.spend()
        budget.reserve_row(name)
        components = []
        budget.ensure(len(membership.get(name, {})))
        for component, component_sources in sorted(
            membership.get(name, {}).items()
        ):
            budget.spend()
            budget.reserve_row(name, component)
            components.append(
                {
                    "name": component,
                    "roles": sorted(roles[component]),
                    "source": _source_label(component_sources),
                }
            )
        result.append(
            {
                "name": name,
                "source": _source_label(sources),
                "changed": name in changed_by_name,
                "components": components,
            }
        )
    return result


def _endpoint_payload(
    repo_root: Path,
    snapshot: GitSourceSnapshot,
    *,
    requested_ref: str,
    requested_commit: str,
    config_path: Path,
    lock_path: Path,
) -> dict:
    commit = snapshot.head_oid
    if commit is None:
        raise ValueError("Review snapshot is missing its exact commit identity")
    result = {
        "requested_ref": requested_ref,
        "requested_commit": requested_commit,
        "commit": commit,
        "tree": snapshot.tree_oid,
        "source": "git-commit",
        "config": f"{commit}:{_endpoint_path(repo_root, config_path)}",
        "lock": f"{commit}:{_endpoint_path(repo_root, lock_path)}",
    }
    return result


def analyze_review_range(
    repo_root: Path,
    base_ref: str,
    target_ref: str,
    *,
    use_merge_base: bool = False,
    transitive: bool = False,
    explicit_facets: Optional[List[str]] = None,
    config_hint: str = "boundary.config.json",
    lock_hint: str = "boundary.lock.json",
    allow_custom_providers: bool = False,
) -> dict:
    """Compare two reconciled immutable lock/config endpoint pairs."""
    if explicit_facets is not None:
        if (
            not isinstance(explicit_facets, list)
            or not explicit_facets
            or not all(isinstance(item, str) for item in explicit_facets)
            or set(explicit_facets) - FACET_SET
        ):
            raise ConfigError(
                "explicit review facets must be a non-empty list containing "
                "only exact, behavior, boundary, and compat"
            )
        explicit_facets = [
            facet for facet in FACETS if facet in set(explicit_facets)
        ]
    try:
        shallow = _git_repository_is_shallow(repo_root)
        requested_base_commit = _resolve_git_commit(
            repo_root,
            base_ref,
            label="base",
        )
        target_commit = _resolve_git_commit(
            repo_root,
            target_ref,
            label="target",
        )
        base_commit = (
            _git_merge_base(repo_root, requested_base_commit, target_commit)
            if use_merge_base
            else requested_base_commit
        )
        base_snapshot = _capture_git_ref_snapshot(
            repo_root,
            base_commit,
            label="effective base",
        )
        target_snapshot = _capture_git_ref_snapshot(
            repo_root,
            target_commit,
            label="target",
        )
    except ValueError as exc:
        remediation = (
            " Repository is shallow; fetch complete history first "
            "(GitHub Actions: fetch-depth: 0; GitLab: GIT_DEPTH: 0)."
            if locals().get("shallow") is True
            else ""
        )
        raise ConfigError(f"Cannot capture review endpoints: {exc}.{remediation}") from exc

    base_config, base_lock, base_config_path, base_lock_path = _load_review_endpoint(
        repo_root,
        base_snapshot,
        label="base",
        config_hint=config_hint,
        lock_hint=lock_hint,
        allow_custom_providers=allow_custom_providers,
    )
    target_config, target_lock, target_config_path, target_lock_path = (
        _load_review_endpoint(
            repo_root,
            target_snapshot,
            label="target",
            config_hint=config_hint,
            lock_hint=lock_hint,
            allow_custom_providers=allow_custom_providers,
        )
    )
    require_compatible_lockfile_schemas(base_lock, target_lock)
    if base_lock.get("config_contract") != target_lock.get("config_contract"):
        raise LockfileError(
            "Review endpoints use incompatible semantic config contracts: "
            f"base={base_lock.get('config_contract')!r}, "
            f"target={target_lock.get('config_contract')!r}"
        )

    base_policy = facet_policy_payload(base_config, explicit_facets)
    target_policy = facet_policy_payload(target_config, explicit_facets)
    budget = _ReviewWorkBudget()
    changed_components, unchanged_components, consumer_impacts = (
        _component_transitions(
            base_lock,
            target_lock,
            base_policy,
            target_policy,
            base_config,
            target_config,
            transitive=transitive,
            budget=budget,
        )
    )
    structural_changes = structural_boundary_changes(
        repo_root,
        base_snapshot,
        target_snapshot,
        base_config,
        target_config,
        base_lock,
        target_lock,
        changed_components,
        base_ref=base_ref,
        target_ref=target_ref,
        requested_base_commit=requested_base_commit,
        requested_target_commit=target_commit,
        allow_custom_providers=allow_custom_providers,
        review_budget=budget,
    )
    changed_slices, unchanged_slices = _slice_transitions(
        base_lock,
        target_lock,
        budget=budget,
    )
    slice_impacts = _slice_impact(
        base_lock,
        target_lock,
        changed_components,
        consumer_impacts,
        changed_slices,
        budget=budget,
    )

    result = {
        "schema": REVIEW_SCHEMA,
        "complete": True,
        "request": {
            "base": base_ref,
            "target": target_ref,
            "merge_base": use_merge_base,
        },
        "history": {
            "repository_shallow": shallow,
            "requirement": (
                "a unique common ancestor and both immutable endpoint trees"
                if use_merge_base
                else "both immutable endpoint commits and trees; intervening history is not traversed"
            ),
        },
        "endpoints": {
            "base": _endpoint_payload(
                repo_root,
                base_snapshot,
                requested_ref=base_ref,
                requested_commit=requested_base_commit,
                config_path=base_config_path,
                lock_path=base_lock_path,
            ),
            "target": _endpoint_payload(
                repo_root,
                target_snapshot,
                requested_ref=target_ref,
                requested_commit=target_commit,
                config_path=target_config_path,
                lock_path=target_lock_path,
            ),
        },
        "policy": {
            "compared_facets": list(FACETS),
            "explicit_facets": explicit_facets,
            "impact": "transitive" if transitive else "direct",
            "base": base_policy,
            "target": target_policy,
        },
        "metadata": {
            field: {"before": base_lock.get(field), "after": target_lock.get(field)}
            for field in ("project", "config_contract", "config_digest")
            if base_lock.get(field) != target_lock.get(field)
        },
        "components": {
            "changed": changed_components,
            "unchanged": unchanged_components,
        },
        "structural_changes": structural_changes,
        "consumer_impact": consumer_impacts,
        "slices": {
            "changed": changed_slices,
            "unchanged": unchanged_slices,
        },
        "slice_impact": slice_impacts,
        "summary": {
            "changed_components": len(changed_components),
            "consumer_impacts": len(consumer_impacts),
            "changed_slices": len(changed_slices),
            "impacted_slices": len(slice_impacts),
        },
    }
    try:
        _bounded_json_dumps(
            result,
            sort_keys=True,
            max_bytes=MAX_REVIEW_RESULT_BYTES,
        )
    except GuardrailError as exc:
        raise GuardrailError(
            "Range review result exceeds the "
            f"{MAX_REVIEW_RESULT_BYTES}-byte complete JSON limit. Reduce graph "
            "fan-out or use direct impact instead of --transitive. No partial "
            "review result was emitted."
        ) from exc
    return result


def _short_identity(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return value[:12]
    return "invalid"


def review_text_lines(result: dict) -> List[str]:
    """Render a concise complete human view of the versioned review result."""
    request = result["request"]
    endpoints = result["endpoints"]
    history = result["history"]
    policy = result["policy"]
    changed = result["components"]["changed"]
    impacts = {item["component"]: item for item in result["consumer_impact"]}
    structural_reports = {
        item["component"]: item
        for item in result["structural_changes"]["reports"]
    }
    lines = [
        "BOUNDVER RANGE REVIEW",
        f"Range: {request['base']}..{request['target']}",
        (
            "Requested base: "
            f"{endpoints['base']['requested_ref']} -> "
            f"{endpoints['base']['requested_commit']}"
        ),
        (
            "Effective base: "
            f"{endpoints['base']['commit']}"
            + (" (merge base)" if request["merge_base"] else "")
        ),
        f"Target: {endpoints['target']['commit']}",
        "Source: immutable Git commit trees",
        f"Base config: {endpoints['base']['config']}",
        f"Base lock: {endpoints['base']['lock']}",
        f"Target config: {endpoints['target']['config']}",
        f"Target lock: {endpoints['target']['lock']}",
        (
            "History: "
            + ("shallow; " if history["repository_shallow"] else "complete; ")
            + history["requirement"]
        ),
        "Compared facets: " + ", ".join(policy["compared_facets"]),
        "Impact mode: " + policy["impact"],
        "",
        f"CHANGED COMPONENTS ({len(changed)})",
    ]
    if not changed:
        lines.append("  none")
    for component in changed:
        lines.append(f"  {component['name']} [{component['status']}]")
        if not component["facets"]:
            lines.append("    Facets: none (metadata-only change)")
        if component["metadata"]:
            lines.append(
                "    Metadata: " + ", ".join(sorted(component["metadata"]))
            )
        for transition in component["facets"]:
            selected = (
                "selected" if transition["selected"]["effective"] else "observed"
            )
            lines.append(
                f"    {transition['facet']}: "
                f"{_short_identity(transition['before'])} -> "
                f"{_short_identity(transition['after'])} [{selected}]"
            )
        structural = structural_reports.get(component["name"])
        if structural is not None:
            provider = structural["inputs"]["target"]["provider"]
            provider_version = structural["inputs"]["target"]["provider_version"]
            if structural["complete"]:
                lines.append(
                    "    Structural explanation: complete "
                    f"({provider} v{provider_version}; not a compatibility verdict)"
                )
                for document in structural["documents"]:
                    lines.append(
                        f"      {document['label']} [{document['status']}]"
                    )
                    for change in document["changes"]:
                        pointer = change["path"] or "<document>"
                        lines.append(
                            f"        {change['kind']} {pointer}: "
                            f"{change['before_type']} -> {change['after_type']}"
                        )
            else:
                suffix = "; no partial rows emitted" if structural["truncated"] else ""
                lines.append(
                    "    Structural explanation: unavailable "
                    f"[{structural['reason']}]{suffix}"
                )
                lines.append(f"      {structural['detail']}")
        impact = impacts.get(component["name"])
        if impact is not None:
            internal = ", ".join(
                f"{item['name']} ({item['source']})"
                for item in impact["components"]
            ) or "none"
            external = ", ".join(
                f"{item['name']} ({item['source']})"
                for item in impact["external_consumers"]
            ) or "none"
            lines.append(f"    Affected components: {internal}")
            lines.append(f"    External consumers: {external}")
            if impact["edges"]:
                lines.append("    Consumer edges:")
                for edge in impact["edges"]:
                    lines.append(
                        f"      {edge['from']} -> {edge['to']} "
                        f"[{edge['kind']}; {edge['source']}]"
                    )
    lines.extend(
        [
            "",
            f"CHANGED SLICES ({len(result['slices']['changed'])})",
        ]
    )
    if not result["slices"]["changed"]:
        lines.append("  none")
    else:
        for item in result["slices"]["changed"]:
            lines.append(f"  {item['name']} [{item['status']}]")
    lines.extend(
        [
            "",
            f"IMPACTED SLICES ({len(result['slice_impact'])})",
        ]
    )
    if not result["slice_impact"]:
        lines.append("  none")
    else:
        for item in result["slice_impact"]:
            lines.append(f"  {item['name']} ({item['source']})")
    lines.extend(
        [
            "",
            "Review complete. Changes do not alter the exit status; run verify as the integrity gate.",
        ]
    )
    rendered_bytes = sum(
        len(line.encode("utf-8", errors="backslashreplace")) + 1
        for line in lines
    )
    if rendered_bytes > MAX_REVIEW_RESULT_BYTES:
        raise GuardrailError(
            "Range review text exceeds the "
            f"{MAX_REVIEW_RESULT_BYTES}-byte complete-output limit. Use "
            "--format json for the complete machine contract or reduce graph "
            "fan-out. No partial review result was emitted."
        )
    return lines
