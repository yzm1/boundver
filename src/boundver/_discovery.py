"""Deterministic comparison between discovery results and configured roots."""

from typing import Dict

from ._utils import ConfigError, _normalize_declared_path


MAX_DISCOVERY_DIFF_COMPONENTS = 10_000
MAX_DISCOVERY_DIFF_TEXT = 16_384


def compare_discovery_to_config(discovered: dict, config: dict) -> dict:
    components = config.get("components")
    if not isinstance(components, dict):
        raise ConfigError("Config field 'components' must be an object")
    if len(components) > MAX_DISCOVERY_DIFF_COMPONENTS:
        raise ConfigError(
            "Discovery config comparison exceeds the "
            f"{MAX_DISCOVERY_DIFF_COMPONENTS}-component limit"
        )

    configured_by_path: Dict[str, list] = {}
    configured_rows = []
    for name, component in sorted(components.items(), key=lambda item: str(item[0])):
        if (
            not isinstance(name, str)
            or not name
            or len(name) > MAX_DISCOVERY_DIFF_TEXT
        ):
            raise ConfigError(
                "Configured component names must be non-empty strings within "
                f"the {MAX_DISCOVERY_DIFF_TEXT}-character limit"
            )
        if not isinstance(component, dict) or not isinstance(component.get("path"), str):
            raise ConfigError(f"Configured component '{name}' must define a string path")
        try:
            path = _normalize_declared_path(component["path"])
        except ValueError as exc:
            raise ConfigError(f"Configured component '{name}' path {exc}") from exc
        configured_by_path.setdefault(path, []).append(name)
        configured_rows.append({"name": name, "path": path})

    registered = []
    unregistered = []
    discovered_paths = set()
    for name, component in sorted(discovered.items()):
        if not isinstance(name, str) or len(name) > MAX_DISCOVERY_DIFF_TEXT:
            raise ConfigError("Discovered component names exceed the text limit")
        if not isinstance(component, dict) or not isinstance(component.get("path"), str):
            raise ConfigError(f"Discovered component '{name}' has no string path")
        try:
            path = _normalize_declared_path(component["path"])
        except ValueError as exc:
            raise ConfigError(f"Discovered component '{name}' path {exc}") from exc
        discovered_paths.add(path)
        configured_names = sorted(configured_by_path.get(path, []))
        if configured_names:
            registered.append(
                {
                    "discovered_name": name,
                    "path": path,
                    "configured_names": configured_names,
                }
            )
        else:
            unregistered.append({"name": name, "path": path})

    not_discovered = [
        row for row in configured_rows if row["path"] not in discovered_paths
    ]
    return {
        "registered_count": len(registered),
        "unregistered_count": len(unregistered),
        "not_discovered_count": len(not_discovered),
        "registered": registered,
        "unregistered": unregistered,
        "not_discovered": not_discovered,
    }
