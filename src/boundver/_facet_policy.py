"""Stable machine representation of effective component and slice policies."""

from typing import List, Optional

from ._consumer_graph import resolve_slice_components
from ._utils import _effective_component_facets


def facet_policy_payload(
    config: dict,
    explicit_facets: Optional[List[str]],
) -> dict:
    """Describe effective component and slice gating without hiding overrides."""
    raw_defaults = config.get("defaults", {})
    configured_defaults = (
        raw_defaults.get("verify_facets")
        if isinstance(raw_defaults, dict) and "verify_facets" in raw_defaults
        else None
    )
    default_facets = (
        sorted(set(configured_defaults))
        if isinstance(configured_defaults, list)
        else None
    )
    components = config.get("components", {})
    effective_components = {
        name: sorted(_effective_component_facets(config, name, explicit_facets))
        for name, component in sorted(components.items())
        if isinstance(component, dict)
    }
    effective_slices = {}
    for name, definition in sorted(config.get("slices", {}).items()):
        if not isinstance(definition, dict):
            continue
        mode = definition.get("mode", "exact")
        members = resolve_slice_components(definition, components)
        gated = (
            mode in explicit_facets
            if explicit_facets is not None
            else any(mode in effective_components.get(member, []) for member in members)
        )
        effective_slices[name] = {"mode": mode, "gated": gated}
    return {
        "explicit": explicit_facets,
        "defaults": default_facets,
        "components": effective_components,
        "slices": effective_slices,
    }
