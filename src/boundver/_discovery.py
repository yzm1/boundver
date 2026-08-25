"""Tracked component discovery and deterministic config comparison."""

import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set

from ._git import _git_run, _iter_bounded_git_paths, _to_posix
from ._utils import (
    ConfigError,
    GuardrailError,
    _bounded_sorted_paths,
    _is_glob,
    _iter_bounded_filesystem_paths,
    _normalize_declared_path,
)


MAX_DISCOVERY_DIFF_COMPONENTS = 10_000
MAX_DISCOVERY_DIFF_TEXT = 16_384
MAX_DISCOVERY_EXCLUSIONS = 1_000
MAX_DISCOVERY_EXCLUSION_BYTES = 1024 * 1024


def normalize_discovery_exclusions(excluded_paths: Optional[List[str]]) -> List[str]:
    """Validate, normalize, deduplicate, and bound discovery path prefixes."""
    raw_paths = excluded_paths or []
    if len(raw_paths) > MAX_DISCOVERY_EXCLUSIONS:
        raise ConfigError(
            "Discovery exclusions exceed the "
            f"{MAX_DISCOVERY_EXCLUSIONS}-path limit"
        )
    normalized_exclusions: List[str] = []
    total_bytes = 0
    for raw_path in raw_paths:
        try:
            normalized = _normalize_declared_path(raw_path)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"Invalid discovery exclusion {raw_path!r}: {exc}"
            ) from exc
        if normalized in {"", "."}:
            raise ConfigError("Discovery exclusion cannot select the repository root")
        normalized = normalized.rstrip("/")
        if _is_glob(normalized):
            raise ConfigError(
                "Discovery exclusions are literal path prefixes; glob syntax is "
                f"not supported: {raw_path!r}"
            )
        total_bytes += len(normalized.encode("utf-8", errors="replace"))
        if total_bytes > MAX_DISCOVERY_EXCLUSION_BYTES:
            raise ConfigError(
                "Discovery exclusions exceed the "
                f"{MAX_DISCOVERY_EXCLUSION_BYTES}-byte aggregate limit"
            )
        normalized_exclusions.append(normalized)
    return sorted(set(normalized_exclusions))


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


def discover_components(
    repo_root: Path,
    *,
    excluded_paths: Optional[List[str]] = None,
    max_discovery_manifests: int = 50_000,
    max_discovered_components: int = 1_000,
    max_provider_detection_entries: int = 50_000,
    max_filesystem_traversal_entries: int = 200_000,
) -> Dict[str, dict]:
    """Discover components from tracked manifests, with a bounded non-Git fallback."""
    manifest_specs = (
        ("package.json", "version"),
        ("pyproject.toml", "project.version"),
        ("Cargo.toml", "package.version"),
        ("go.mod", None),
    )
    _ignored_dirs = {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        "dist", "build", "vendor",
    }
    normalized_exclusions = normalize_discovery_exclusions(excluded_paths)

    def is_excluded(path: Path) -> bool:
        try:
            relative = path.relative_to(repo_root).as_posix().strip("/")
        except ValueError:
            return True
        return any(
            relative == excluded or relative.startswith(excluded + "/")
            for excluded in normalized_exclusions
        )
    found: Dict[str, dict] = {}
    seen_directories: Set[str] = set()
    root_manifest_component: Optional[str] = None
    tracked_detection = False
    provider_candidate_paths: List[Path] = []

    def filesystem_manifest_candidates() -> List[Path]:
        manifest_names = {manifest for manifest, _field in manifest_specs}
        return _bounded_sorted_paths(
            (
                path
                for path in _iter_bounded_filesystem_paths(
                    repo_root,
                    recursive=True,
                    max_entries=max_filesystem_traversal_entries,
                    exceeded_message=(
                        "Component discovery guardrail exceeded: "
                        "filesystem traversal exceeds "
                        f"{max_filesystem_traversal_entries} entries"
                    ),
                    should_descend=(
                        lambda directory: (
                            directory.name not in _ignored_dirs
                            and not is_excluded(directory)
                        )
                    ),
                )
                if path.name in manifest_names and not is_excluded(path)
            ),
            max_paths=max_discovery_manifests,
            exceeded_message=(
                "Component discovery guardrail exceeded: "
                f">{max_discovery_manifests} manifests"
            ),
        )

    try:
        listed_paths = [
            repo_root / path
            for path in _iter_bounded_git_paths(
                repo_root,
                ["ls-files", "--cached", "-z", "--"],
            )
        ]
        listed_paths = [path for path in listed_paths if not is_excluded(path)]
        # Manifests are discovered from the index name set so an unstaged
        # deletion does not erase an otherwise configured component. Provider
        # evidence, however, must be readable from the working tree selected by
        # the generated declaration; filter that separate view below.
        candidate_paths = listed_paths
        provider_candidate_paths = [
            path
            for path in listed_paths
            if path.exists() or path.is_symlink()
        ]
        if listed_paths:
            tracked_detection = True
        else:
            try:
                _git_run(repo_root, ["rev-parse", "--verify", "HEAD"])
            except subprocess.CalledProcessError:
                candidate_paths = filesystem_manifest_candidates()
            else:
                tracked_detection = True
    except (OSError, subprocess.CalledProcessError):
        candidate_paths = filesystem_manifest_candidates()

    # A repository-root manifest is common for single-package projects, but a
    # root component would hash its own lockfile.  Map it to a conventional,
    # tracked source directory when one is unambiguous instead of silently
    # dropping the project or generating an invalid empty config.
    tracked_relative = []
    for candidate in candidate_paths:
        try:
            tracked_relative.append(candidate.relative_to(repo_root).as_posix())
        except ValueError:
            continue
    provider_relative = []
    for candidate in provider_candidate_paths:
        try:
            provider_relative.append(candidate.relative_to(repo_root).as_posix())
        except ValueError:
            continue
    python_package_dirs = sorted({
        path.rsplit("/", 1)[0]
        for path in tracked_relative
        if path.endswith("/__init__.py")
        and not path.startswith(".")
        and not (_ignored_dirs & set(path.split("/")))
    })
    top_python_packages = [
        candidate
        for candidate in python_package_dirs
        if not any(
            candidate.startswith(other + "/")
            for other in python_package_dirs
            if other != candidate
        )
    ]
    if len(top_python_packages) == 1:
        root_manifest_component = top_python_packages[0]

    for conventional in ("src", "lib", "app"):
        if root_manifest_component is not None:
            break
        if any(
            path == conventional or path.startswith(conventional + "/")
            for path in tracked_relative
        ):
            root_manifest_component = conventional
            break

    for manifest, version_field in manifest_specs:
        for mf in sorted(p for p in candidate_paths if p.name == manifest):
            if _ignored_dirs & set(mf.relative_to(repo_root).parts):
                continue
            rel_dir = mf.parent.relative_to(repo_root)
            if str(rel_dir) == ".":
                if root_manifest_component is None:
                    continue
                rel_path = root_manifest_component
            else:
                rel_path = _to_posix(str(rel_dir))
            if rel_path in seen_directories:
                continue
            seen_directories.add(rel_path)
            comp_name = repo_root.name if str(rel_dir) == "." else rel_dir.name
            base_name = comp_name
            idx = 2
            while comp_name in found:
                comp_name = f"{base_name}-{idx}"
                idx += 1
            component_dir = repo_root / rel_path
            component_prefix = rel_path.rstrip("/") + "/"
            available_paths = (
                {
                    path[len(component_prefix):]
                    for path in provider_relative
                    if path.startswith(component_prefix)
                }
                if tracked_detection
                else None
            )
            provider, paths = _detect_provider(
                component_dir,
                available_paths=available_paths,
                max_entries=max_provider_detection_entries,
            )
            version_source = (
                {"file": mf.name, "field": version_field}
                if version_field is not None and str(rel_dir) != "."
                else None
            )
            if len(found) >= max_discovered_components:
                raise GuardrailError(
                    "Component discovery guardrail exceeded: "
                    f">{max_discovered_components} components"
                )
            found[comp_name] = {
                "path": rel_path,
                "version_source": version_source,
                "boundary": {"provider": provider, "paths": paths},
            }
    return found


def _detect_provider(
    component_dir: Path,
    *,
    available_paths: Optional[Set[str]] = None,
    max_entries: int = 50_000,
) -> tuple:
    """Detect the best boundary provider and paths for a component directory.

    Returns (provider_name, paths_list).
    """
    if available_paths is not None:
        # Discovery is index-backed whenever Git supplied the manifest list.
        # Restrict provider evidence to that same immutable name set so an
        # untracked, deleted, or concurrently-created working-tree artifact
        # cannot change the generated declaration.
        if len(available_paths) > max_entries:
            raise GuardrailError(
                "Provider detection guardrail exceeded: "
                f">{max_entries} available paths"
            )
        names = {
            _to_posix(path)
            for path in available_paths
            if isinstance(path, str) and path
        }
        for name in (
            "openapi.yaml",
            "openapi.yml",
            "openapi.json",
            "swagger.yaml",
            "swagger.json",
        ):
            if name in names:
                return ("openapi", [name])
        openapi_names = sorted(
            name
            for name in names
            if "/" not in name and name.lower().startswith("openapi")
        )
        if openapi_names:
            return ("openapi", [openapi_names[0]])
        for name in ("boundary.json", "schema.json", "api.json"):
            if name in names:
                return ("json-file", [name])
        if "__init__.py" in names:
            return ("python-exports", ["__init__.py"])
        if "src/index.ts" in names:
            return ("typescript-exports", ["src/index.ts"])
        if "index.ts" in names:
            return ("typescript-exports", ["index.ts"])
        return ("implicit", [])

    # Non-Git and truly empty unborn repositories retain the documented
    # bounded filesystem fallback.
    # OpenAPI specs
    for name in (
        "openapi.yaml",
        "openapi.yml",
        "openapi.json",
        "swagger.yaml",
        "swagger.json",
    ):
        candidate = component_dir / name
        if candidate.exists() or candidate.is_symlink():
            return ("openapi", [name])
    # Glob for any openapi-like files
    directory_entries = _bounded_sorted_paths(
        (
            _iter_bounded_filesystem_paths(
                component_dir,
                recursive=False,
                max_entries=max_entries,
                exceeded_message=(
                    "Provider detection guardrail exceeded: "
                    f">{max_entries} directory entries"
                ),
            )
            if component_dir.exists()
            else ()
        ),
        max_paths=max_entries,
        exceeded_message=(
            "Provider detection guardrail exceeded: "
            f">{max_entries} directory entries"
        ),
    )
    for f in directory_entries:
        if f.is_file() and f.name.lower().startswith("openapi"):
            return ("openapi", [f.name])

    # JSON schema / config boundary files
    for name in ("boundary.json", "schema.json", "api.json"):
        if (component_dir / name).exists():
            return ("json-file", [name])

    # Python exports
    if (component_dir / "__init__.py").exists():
        return ("python-exports", ["__init__.py"])

    # TypeScript exports
    src_index = component_dir / "src" / "index.ts"
    if src_index.exists():
        return ("typescript-exports", ["src/index.ts"])
    if (component_dir / "index.ts").exists():
        return ("typescript-exports", ["index.ts"])

    return ("implicit", [])
