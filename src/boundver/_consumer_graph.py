"""Consumer-graph helpers shared by validation, slices, and impact output.

``components[A]["consumers"]`` names components directly downstream of A, so
graph traversal follows the direction in which a boundary change propagates.
"""

from collections import deque
from typing import Any, Dict, Iterable, List, Mapping, Optional


def empty_explicit_slice_error(
    slice_name: object,
    slice_definition: object,
) -> Optional[str]:
    """Return the actionable error for an explicit slice with no members."""
    if not isinstance(slice_definition, dict):
        return None
    members = slice_definition.get("components")
    if "components" not in slice_definition or not isinstance(members, list):
        return None
    if members:
        return None
    return (
        f"Slice '{slice_name}' field 'components' must contain at least one "
        "configured component; add a component name or remove the empty slice"
    )


def consumer_closure(
    components: Mapping[str, Any],
    seeds: Iterable[str],
    *,
    include_seeds: bool = False,
) -> List[str]:
    """Return the deterministic, cycle-safe downstream closure of *seeds*.

    Validation is responsible for rejecting malformed or unknown consumer
    names.  This helper still ignores malformed edges defensively so reporting
    a diagnostic can never recurse forever or fail with a type error.
    """
    seed_set = {seed for seed in seeds if isinstance(seed, str)}
    seen = set(seed_set)
    pending = deque(sorted(seed_set))

    while pending:
        component_name = pending.popleft()
        component = components.get(component_name, {})
        if not isinstance(component, dict):
            continue
        raw_consumers = component.get("consumers", [])
        if not isinstance(raw_consumers, list):
            continue
        for consumer in sorted(
            {
                candidate
                for candidate in raw_consumers
                if isinstance(candidate, str) and candidate in components
            }
        ):
            if consumer in seen:
                continue
            seen.add(consumer)
            pending.append(consumer)

    if not include_seeds:
        seen.difference_update(seed_set)
    return sorted(seen)


def affected_consumers(
    components: Mapping[str, Any],
    component_name: str,
    *,
    transitive: bool = False,
) -> List[str]:
    """Return internal consumers plus typed external terminal labels.

    Direct mode reports the source component's immediate internal edges and
    its external terminals.  Transitive mode walks internal edges and also
    collects external terminals declared by every reached component.
    """
    groups = affected_consumer_groups(
        components,
        component_name,
        transitive=transitive,
    )
    return sorted(
        set(groups["components"]) | set(groups["external_consumers"])
    )


def affected_consumer_groups(
    components: Mapping[str, Any],
    component_name: str,
    *,
    transitive: bool = False,
) -> Dict[str, List[str]]:
    """Return typed downstream impact for stable machine consumption."""
    if transitive:
        internal_consumers = consumer_closure(components, [component_name])
        external_scope = [component_name, *internal_consumers]
    else:
        internal_consumers = []
        external_scope = [component_name]
        component = components.get(component_name, {})
        if isinstance(component, dict):
            raw_consumers = component.get("consumers", [])
            if isinstance(raw_consumers, list):
                internal_consumers = [
                    consumer
                    for consumer in raw_consumers
                    if isinstance(consumer, str) and consumer in components
                ]

    external_consumers = set()
    for source_name in external_scope:
        source_component = components.get(source_name, {})
        if not isinstance(source_component, dict):
            continue
        raw_external = source_component.get("external_consumers", [])
        if not isinstance(raw_external, list):
            continue
        external_consumers.update(
            terminal for terminal in raw_external if isinstance(terminal, str)
        )
    return {
        "components": sorted(set(internal_consumers)),
        "external_consumers": sorted(external_consumers),
    }


def resolve_slice_components(
    slice_definition: Dict[str, Any],
    components: Mapping[str, Any],
) -> List[str]:
    """Resolve an explicit slice or a downstream consumer closure.

    A ``closure_of`` slice contains its seed and every component reachable by
    following ``consumers`` edges.  Config validation enforces the declaration
    shape and seed identity; this function remains deterministic for direct API
    callers as well.
    """
    if "closure_of" in slice_definition:
        seed = slice_definition.get("closure_of")
        if not isinstance(seed, str) or seed not in components:
            return []
        return consumer_closure(components, [seed], include_seeds=True)

    raw_components = slice_definition.get("components", [])
    if not isinstance(raw_components, list):
        return []
    return sorted(
        {
            component
            for component in raw_components
            if isinstance(component, str)
        }
    )
