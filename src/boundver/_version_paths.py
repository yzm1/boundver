"""Pure path resolution shared by version extraction and change selection."""

from typing import List, Optional

from ._utils import _is_glob, _normalize_declared_path


def version_file_repo_path(component_path: str, source: dict) -> Optional[str]:
    """Resolve one explicit file variant without opening or trusting a path."""
    if not isinstance(source, dict) or not isinstance(component_path, str):
        return None
    variants = {"file", "repo_file", "git_tag_prefix", "constant", "component"} & source.keys()
    if len(variants) != 1 or not variants & {"file", "repo_file"}:
        return None
    kind = next(iter(variants))
    try:
        relative = _normalize_declared_path(source[kind])
        prefix = (
            "" if component_path in {"", "."}
            else _normalize_declared_path(component_path)
        )
    except ValueError:
        return None
    if ":" in relative or ":" in prefix or _is_glob(relative) or _is_glob(prefix):
        return None
    return relative if kind == "repo_file" or not prefix else f"{prefix}/{relative}"


def component_version_input_paths(config: dict, name: str) -> List[str]:
    """Return declared file inputs through a bounded, non-recursive chain.

    The caller validates the graph. A visited set also keeps diagnostics safe
    when directly called with an invalid cycle; this is not cycle acceptance.
    """
    components = config.get("components", {})
    seen = set()
    while isinstance(name, str) and name not in seen:
        seen.add(name)
        component = components.get(name)
        if not isinstance(component, dict):
            break
        source = component.get("version_source")
        path = version_file_repo_path(component.get("path"), source)
        if path is not None:
            return [path]
        if not isinstance(source, dict):
            break
        name = source.get("component")
    return []
