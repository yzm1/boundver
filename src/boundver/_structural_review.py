"""Provider-aware structural evidence for immutable range review endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional, Protocol, Tuple

from ._git import GitSourceSnapshot
from ._lockfile import _SourceAccessor
from ._provider_diff import (
    STRUCTURAL_DIFF_INTERFACE,
    StructuralDiffBudget,
    StructuralDiffResult,
    structural_diff_payload,
)
from ._utils import (
    GuardrailError,
    ProviderError,
    _bounded_diagnostic_text,
)
from .providers import (
    ProviderContext,
    _safe_provider_attribute,
    create_registry,
    get_provider,
    load_custom_providers,
)


_STRUCTURAL_CLAIM = "structural-explanation-only"


class _ResultBudget(Protocol):
    def reserve_row(self, *values: object, overhead: int = 128) -> None:
        ...  # pragma: no cover


def _structural_input(
    endpoint: str,
    snapshot: GitSourceSnapshot,
    entry: object,
    *,
    requested_ref: str,
    requested_commit: str,
) -> dict:
    component = entry if isinstance(entry, dict) else {}
    fingerprints = component.get("fingerprints", {})
    if not isinstance(fingerprints, dict):
        fingerprints = {}
    assert snapshot.head_oid is not None
    return {
        "endpoint": endpoint,
        "requested_ref": requested_ref,
        "requested_commit": requested_commit,
        "commit": snapshot.head_oid,
        "tree": snapshot.tree_oid,
        "source": "git-commit",
        "present": isinstance(entry, dict),
        "component_path": component.get("path"),
        "provider": component.get("boundary_provider"),
        "provider_version": component.get("boundary_provider_version"),
        "boundary_digest": fingerprints.get("boundary"),
    }


def _unavailable_report(
    component: str,
    base_input: dict,
    target_input: dict,
    *,
    reason: str,
    detail: str,
    truncated: bool = False,
) -> dict:
    return {
        "component": component,
        "status": "unavailable",
        "complete": False,
        "truncated": truncated,
        "interface": STRUCTURAL_DIFF_INTERFACE,
        "claim": _STRUCTURAL_CLAIM,
        "inputs": {"base": base_input, "target": target_input},
        "reason": reason,
        "detail": detail,
        "documents": [],
        "summary": {"added": 0, "removed": 0, "changed": 0},
    }


def _provider_context(
    repo_root: Path,
    config: dict,
    entry: dict,
    component: str,
    accessor: _SourceAccessor,
) -> ProviderContext:
    definition = config["components"][component]
    return ProviderContext(
        repo_root=repo_root,
        component_path=entry["path"],
        boundary_cfg=definition.get("boundary", {}),
        source="head",
        read_file=accessor.read_file,
        read_file_limited=accessor.read_file_limited,
        list_files=accessor.list_files,
    )


def _provider_method(
    provider_name: object,
    base_registry: dict,
    target_registry: dict,
) -> Tuple[Optional[Callable[..., object]], Optional[str], Optional[str]]:
    base_provider = get_provider(provider_name, registry=base_registry)
    target_provider = get_provider(provider_name, registry=target_registry)
    if base_provider is None or target_provider is None:
        return (
            None,
            "provider-unavailable",
            f"Provider {provider_name!r} is unavailable at one or both endpoints",
        )
    if type(base_provider) is not type(target_provider):
        return (
            None,
            "provider-implementation-changed",
            "Structural comparison requires the same provider implementation at both endpoints",
        )
    try:
        base_interface = _safe_provider_attribute(
            base_provider,
            "structural_diff_interface",
        )
        target_interface = _safe_provider_attribute(
            target_provider,
            "structural_diff_interface",
        )
        base_method = _safe_provider_attribute(base_provider, "structural_diff")
        target_method = _safe_provider_attribute(target_provider, "structural_diff")
    except ProviderError:
        return (
            None,
            "provider-unsupported",
            f"Provider {provider_name!r} does not expose bounded structural diff output",
        )
    if not callable(base_method) or not callable(target_method):
        return (
            None,
            "provider-unsupported",
            f"Provider {provider_name!r} does not expose bounded structural diff output",
        )
    if (
        base_interface != STRUCTURAL_DIFF_INTERFACE
        or target_interface != STRUCTURAL_DIFF_INTERFACE
    ):
        return (
            None,
            "provider-interface-unsupported",
            "Provider structural-diff interface is not supported by this host "
            f"(expected {STRUCTURAL_DIFF_INTERFACE})",
        )
    return target_method, None, None


def _resolved_registries(
    base_config: dict,
    target_config: dict,
    *,
    allow_custom_providers: bool,
) -> Tuple[dict, dict, Optional[str]]:
    base_registry = create_registry()
    target_registry = create_registry()
    if not allow_custom_providers:
        return base_registry, target_registry, None
    errors = [
        *load_custom_providers(
            base_config.get("providers", []),
            allow_custom=True,
            registry=base_registry,
        ),
        *load_custom_providers(
            target_config.get("providers", []),
            allow_custom=True,
            registry=target_registry,
        ),
    ]
    detail = (
        _bounded_diagnostic_text("; ".join(errors[:3]))
        if errors
        else None
    )
    return base_registry, target_registry, detail


def _complete_report(
    component: str,
    base_input: dict,
    target_input: dict,
    result: object,
) -> dict:
    if type(result) is not StructuralDiffResult:
        raise ProviderError("Structural provider returned an invalid result type")
    payload = structural_diff_payload(result)
    if not payload["documents"]:
        raise ProviderError("Changed boundary digest produced no structural changes")
    return {
        "component": component,
        "status": "complete",
        "complete": True,
        "truncated": False,
        "interface": STRUCTURAL_DIFF_INTERFACE,
        "claim": _STRUCTURAL_CLAIM,
        "inputs": {"base": base_input, "target": target_input},
        "reason": None,
        "detail": None,
        **payload,
    }


def structural_boundary_changes(
    repo_root: Path,
    base_snapshot: GitSourceSnapshot,
    target_snapshot: GitSourceSnapshot,
    base_config: dict,
    target_config: dict,
    base_lock: dict,
    target_lock: dict,
    changed_components: List[dict],
    *,
    base_ref: str,
    target_ref: str,
    requested_base_commit: str,
    requested_target_commit: str,
    allow_custom_providers: bool,
    review_budget: _ResultBudget,
) -> dict:
    """Explain changed boundary facets without weakening range completeness."""
    candidates = [
        transition
        for transition in changed_components
        if any(item["facet"] == "boundary" for item in transition["facets"])
    ]
    structural_budget = StructuralDiffBudget()
    base_accessor = _SourceAccessor(repo_root, "head", snapshot=base_snapshot)
    target_accessor = _SourceAccessor(repo_root, "head", snapshot=target_snapshot)
    base_registry, target_registry, registry_error = _resolved_registries(
        base_config,
        target_config,
        allow_custom_providers=allow_custom_providers,
    )

    reports = []
    for transition in candidates:
        name = transition["name"]
        before = base_lock["components"].get(name)
        after = target_lock["components"].get(name)
        base_input = _structural_input(
            "base",
            base_snapshot,
            before,
            requested_ref=base_ref,
            requested_commit=requested_base_commit,
        )
        target_input = _structural_input(
            "target",
            target_snapshot,
            after,
            requested_ref=target_ref,
            requested_commit=requested_target_commit,
        )
        report: Optional[dict] = None
        method: Optional[Callable[..., object]] = None
        if not isinstance(before, dict) or not isinstance(after, dict):
            report = _unavailable_report(
                name,
                base_input,
                target_input,
                reason="component-absent",
                detail="Structural comparison requires the component at both endpoints",
            )
        elif base_input["provider"] != target_input["provider"]:
            report = _unavailable_report(
                name,
                base_input,
                target_input,
                reason="provider-changed",
                detail="Structural comparison requires the same provider at both endpoints",
            )
        elif base_input["provider_version"] != target_input["provider_version"]:
            report = _unavailable_report(
                name,
                base_input,
                target_input,
                reason="provider-version-changed",
                detail=(
                    "Structural comparison requires the same provider version at both endpoints"
                ),
            )
        elif registry_error is not None:
            report = _unavailable_report(
                name,
                base_input,
                target_input,
                reason="provider-unavailable",
                detail=registry_error,
            )
        else:
            method, reason, detail = _provider_method(
                target_input["provider"],
                base_registry,
                target_registry,
            )
            if reason is not None:
                assert detail is not None
                report = _unavailable_report(
                    name,
                    base_input,
                    target_input,
                    reason=reason,
                    detail=detail,
                )

        if report is None and structural_budget.exhausted:
            report = _unavailable_report(
                name,
                base_input,
                target_input,
                reason="limit-exceeded",
                detail=(
                    "The aggregate structural-diff budget was already exhausted; "
                    "no partial rows were retained"
                ),
                truncated=True,
            )
        elif report is None:
            try:
                assert method is not None
                result = method(
                    _provider_context(
                        repo_root,
                        base_config,
                        before,
                        name,
                        base_accessor,
                    ),
                    _provider_context(
                        repo_root,
                        target_config,
                        after,
                        name,
                        target_accessor,
                    ),
                    structural_budget,
                )
                report = _complete_report(name, base_input, target_input, result)
            except GuardrailError as exc:
                report = _unavailable_report(
                    name,
                    base_input,
                    target_input,
                    reason="limit-exceeded",
                    detail=_bounded_diagnostic_text(str(exc)),
                    truncated=True,
                )
            except (ProviderError, OSError, RecursionError, TypeError, ValueError) as exc:
                report = _unavailable_report(
                    name,
                    base_input,
                    target_input,
                    reason="provider-unavailable",
                    detail=_bounded_diagnostic_text(str(exc) or type(exc).__name__),
                )

        assert report is not None
        review_budget.reserve_row(name, report["status"], report["reason"])
        for document in report["documents"]:
            review_budget.reserve_row(name, document["label"], document["status"])
            for change in document["changes"]:
                review_budget.reserve_row(name, document["label"], change["path"])
        reports.append(report)

    return {
        "complete": all(report["complete"] for report in reports),
        "truncated": any(report["truncated"] for report in reports),
        "interface": STRUCTURAL_DIFF_INTERFACE,
        "claim": _STRUCTURAL_CLAIM,
        "reports": reports,
    }
