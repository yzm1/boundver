"""Config validation and component discovery for boundver."""

import fnmatch
import json
import os
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ._git import _decode_nul_paths, _git_run_bytes, _to_posix
from ._hashing import _is_within
from ._utils import _is_glob, boundary_provider_name, ConfigError
from .providers import (
    create_registry,
    get_provider,
    load_custom_providers,
    validate_provider_config,
)

# Ordered preference when auto-discovering config (first match wins)
_CONFIG_CANDIDATES = [
    "boundary.config.json",
    "boundary.config.yaml",
    "boundary.config.yml",
    "boundary.config.toml",
]


def find_config_file(repo_root: Path, hint: str = "boundary.config.json") -> Path:
    """Return the config file path to use.

    If *hint* is the default ``boundary.config.json`` and that file does not
    exist, probe for YAML/TOML alternatives in order.  If *hint* is an explicit
    user-supplied path, return it as-is (the caller handles missing-file errors).
    """
    explicit = Path(hint)
    if explicit.is_absolute():
        return explicit
    candidate = repo_root / hint
    if candidate.exists():
        return candidate
    # Only auto-probe when the hint is the default JSON name
    if hint == "boundary.config.json":
        for name in _CONFIG_CANDIDATES[1:]:
            alt = repo_root / name
            if alt.exists():
                return alt
    return candidate  # caller will report missing


def load_config_file(path: Path) -> dict:
    """Parse a boundver config file.  Supports JSON, YAML, and TOML.

    Raises ``ValueError`` with a human-readable message on parse failure.
    Raises ``FileNotFoundError`` if *path* does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    _MAX_CONFIG_BYTES = 10 * 1024 * 1024  # 10 MiB
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ConfigError(f"Cannot stat config file {path}: {exc}") from exc
    if size > _MAX_CONFIG_BYTES:
        raise ConfigError(f"Config file too large ({size} bytes): {path}")
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"JSON parse error in {path}: {exc}") from exc
    elif suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
            result = yaml.safe_load(text)
        except ImportError:
            raise ConfigError(
                f"Cannot parse {path}: PyYAML is not installed. "
                "Install it with: pip install PyYAML"
            )
        except (MemoryError, RecursionError):
            raise
        except Exception as exc:
            raise ConfigError(f"YAML parse error in {path}: {exc}") from exc
    elif suffix == ".toml":
        try:
            import tomllib  # type: ignore  # Python 3.11+
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore  # pip install tomli
            except ImportError:
                raise ConfigError(
                    f"Cannot parse {path}: neither tomllib (Python 3.11+) nor tomli is available. "
                    "Install tomli: pip install tomli"
                )
        try:
            result = tomllib.loads(text)
        except (MemoryError, RecursionError):
            raise
        except Exception as exc:
            raise ConfigError(f"TOML parse error in {path}: {exc}") from exc
    else:
        raise ConfigError(
            f"Unsupported config file extension '{suffix}' for {path}. "
            "Supported formats: .json, .yaml, .yml, .toml"
        )
    if not isinstance(result, dict):
        raise ConfigError(
            f"Config file {path} must contain an object/mapping, "
            f"got {type(result).__name__}"
        )
    return result



def _load_config_schema(repo_root: Path) -> Optional[dict]:
    """Load the packaged schema, falling back to a checkout-local override."""
    schema_path = repo_root / "boundary.config.schema.json"
    if schema_path.exists():
        try:
            return json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    try:
        bundled = resources.read_text(
            "boundver", "boundary.config.schema.json", encoding="utf-8"
        )
        return json.loads(bundled)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _schema_required_fields(schema: Optional[dict]) -> List[str]:
    if not schema:
        return ["project", "components"]
    required = schema.get("required", [])
    return [k for k in required if isinstance(k, str)] or ["project", "components"]


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _validate_component_path_entries(
    errors: List[str],
    repo_root: Path,
    component_name: str,
    component_path: Optional[str],
    field_name: str,
    paths: List[str],
    check_exists: bool = True,
) -> None:
    """Validate a component-relative path list for boundary/behavior config."""
    if component_path is None:
        return

    component_root = repo_root / component_path
    for rel in paths:
        if "\\" in rel:
            errors.append(
                f"Component '{component_name}' {field_name} path must use '/' "
                f"separators for portability: {rel}"
            )
            continue
        if _is_glob(rel):
            if ".." in rel:
                errors.append(
                    f"Component '{component_name}' {field_name} glob pattern must not contain '..': {rel}"
                )
            continue

        full = component_root / rel
        if not _is_within(component_root, full):
            errors.append(f"Component '{component_name}' {field_name} path escapes component root: {rel}")
            continue
        if not _is_within(repo_root, full):
            errors.append(f"Component '{component_name}' {field_name} path escapes repository root: {rel}")
            continue
        if check_exists and not full.exists():
            errors.append(
                f"Component '{component_name}' {field_name} path not found: {component_path}/{rel}"
                f" — ensure the file exists before running generate"
            )


def _expand_component_paths(
    repo_root: Path,
    component_path: Optional[str],
    paths: List[str],
) -> Set[str]:
    if component_path is None:
        return set()

    component_root = repo_root / component_path
    if not component_root.exists() or not component_root.is_dir():
        return set()

    # Bounded enumeration to prevent runaway memory on huge directories.
    _MAX_EXPAND_FILES = 50000
    all_files: List[str] = []
    for path in sorted(component_root.rglob("*")):
        if path.is_file():
            all_files.append(_to_posix(str(path.relative_to(component_root))))
            if len(all_files) >= _MAX_EXPAND_FILES:
                break
    matched: Set[str] = set()

    for rel in paths:
        is_dir_like = rel.endswith(("/", "\\"))
        rel_norm = _to_posix(rel).strip()
        if rel_norm.startswith("./"):
            rel_norm = rel_norm[2:]
        if not rel_norm:
            continue
        if _is_glob(rel_norm):
            for file_rel in all_files:
                if fnmatch.fnmatchcase(file_rel, rel_norm):
                    matched.add(file_rel)
            continue

        prefix = rel_norm.rstrip("/")
        target = component_root / prefix
        if is_dir_like or target.is_dir():
            for file_rel in all_files:
                if file_rel == prefix or file_rel.startswith(prefix + "/"):
                    matched.add(file_rel)
        else:
            for file_rel in all_files:
                if file_rel == prefix:
                    matched.add(file_rel)

    return matched


def _schema_engine_errors(config: dict, schema: Optional[dict]) -> List[str]:
    """Optional strict JSON Schema validation.

    If jsonschema is unavailable, return no engine errors and rely on
    hand-rolled checks.
    """
    if not schema:
        return []
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return []

    try:
        validator = jsonschema.Draft202012Validator(schema)
    except Exception as exc:  # pragma: no cover - defensive path
        return [f"Schema validator initialization failed: {exc}"]

    errors: List[str] = []
    for err in validator.iter_errors(config):
        path = ".".join(str(p) for p in err.path) or "<root>"
        errors.append(f"Schema validation error at {path}: {err.message}")
    return sorted(errors)


def validate_config(
    config: dict,
    repo_root: Path,
    allow_custom_providers: bool = False,
    source: str = "working-tree",
) -> List[str]:
    errors: List[str] = []
    if not isinstance(config, dict):
        return ["Config root must be a JSON object"]
    if source not in {"head", "index", "working-tree"}:
        return [f"Unknown source mode: {source!r}"]

    schema = _load_config_schema(repo_root)
    errors.extend(_schema_engine_errors(config, schema))
    for required_key in _schema_required_fields(schema):
        if required_key not in config:
            errors.append(f"Missing required top-level field: {required_key}")

    project = config.get("project")
    if not isinstance(project, str) or not project.strip():
        errors.append("Field 'project' must be a non-empty string")

    supported_modes = {"exact", "behavior", "boundary", "compat"}
    defaults = config.get("defaults", {})
    if not isinstance(defaults, dict):
        errors.append("Field 'defaults' must be an object")
        defaults = {}
    compat_mode = defaults.get("compat_mode", "major")
    if not isinstance(compat_mode, str) or compat_mode not in {
        "major", "semver_major", "semver_major_minor"
    }:
        errors.append(f"Unsupported defaults.compat_mode: {compat_mode}")
    verify_facets = defaults.get("verify_facets")
    if verify_facets is not None:
        if not _is_str_list(verify_facets) or not verify_facets:
            errors.append("defaults.verify_facets must be a non-empty array of strings")
        else:
            invalid = sorted(set(verify_facets) - supported_modes)
            if invalid:
                errors.append(
                    "defaults.verify_facets contains unknown facets: "
                    + ", ".join(invalid)
                )

    components = config.get("components", {})
    slices = config.get("slices", {})
    if not isinstance(components, dict):
        errors.append("Field 'components' must be an object")
        components = {}
    elif not components:
        errors.append("Field 'components' must define at least one component")
    if not isinstance(slices, dict):
        errors.append("Field 'slices' must be an object")
        slices = {}

    if "allow_custom_providers" in config:
        errors.append(
            "Top-level 'allow_custom_providers' is not supported: repository config "
            "cannot authorize Python imports; pass --allow-custom-providers or set "
            "BOUNDVER_ALLOW_CUSTOM_PROVIDERS=1 in trusted automation"
        )

    seen_component_paths: Dict[str, str] = {}
    known_providers = {
        "openapi", "python-exports", "typescript-exports", "json-file", "leaf", "implicit",
        "json-canonical", "openapi-canonical",
        # Explicit "-raw" aliases for clarity
        "openapi-raw", "json-file-raw", "python-exports-raw", "typescript-exports-raw",
    }

    # Validate top-level providers list (Phase 2)
    providers_list = config.get("providers")
    declared_custom_names: set = set()
    if providers_list is not None:
        if not isinstance(providers_list, list):
            errors.append("Top-level 'providers' must be an array")
        else:
            for i, entry in enumerate(providers_list):
                if not isinstance(entry, dict):
                    errors.append(f"providers[{i}] must be an object")
                    continue
                for field_name in ("module", "class"):
                    val = entry.get(field_name)
                    if not isinstance(val, str) or not val.strip():
                        errors.append(
                            f"providers[{i}] missing required string field '{field_name}'"
                        )
                # Validate class name is a legal Python identifier.
                cls_val = entry.get("class", "")
                if isinstance(cls_val, str) and cls_val.strip() and not cls_val.strip().isidentifier():
                    errors.append(
                        f"providers[{i}] class name '{cls_val.strip()}' is not a valid Python identifier"
                    )
                # Track declared custom provider names for cross-reference.
                # The registered provider name is: entry["name"] if provided,
                # otherwise defaults to "custom.{class_name}".
                declared_name = entry.get("name", "").strip() if isinstance(entry.get("name"), str) else ""
                cls_name = entry.get("class", "")
                if declared_name:
                    if not declared_name.startswith("custom."):
                        errors.append(
                            f"providers[{i}] field 'name' must start with 'custom.' "
                            f"to avoid collisions with built-in providers "
                            f"(got '{declared_name}', try 'custom.{declared_name}')"
                        )
                    else:
                        declared_custom_names.add(declared_name)
                # Without an explicit name, the provider instance determines
                # its registered custom.* identifier after trusted loading.

    for name, comp in components.items():
        if not isinstance(comp, dict):
            errors.append(f"Component '{name}' must be an object")
            continue
        if "path" not in comp:
            errors.append(f"Component '{name}' missing required field: path")
            continue
        component_path = comp.get("path") if isinstance(comp.get("path"), str) and comp["path"].strip() else None
        if component_path is None:
            errors.append(f"Component '{name}' field 'path' must be a non-empty string")
        else:
            if "\\" in component_path:
                errors.append(
                    f"Component '{name}' path must use '/' separators for portability: "
                    f"{component_path}"
                )
            normalized_path = _to_posix(os.path.normpath(component_path.strip()))
            if ".." in normalized_path.replace("\\", "/").split("/"):
                errors.append(
                    f"Component '{name}' path must not contain '..': {normalized_path}"
                )
            elif Path(normalized_path).is_absolute():
                errors.append(
                    f"Component '{name}' path must be relative: {normalized_path}"
                )
            elif normalized_path in {"", "."}:
                errors.append(
                    f"Component '{name}' cannot use the repository root as its path: "
                    "the generated lockfile would become part of its own exact fingerprint. "
                    "Place the component in a subdirectory."
                )
            elif source == "working-tree" and (repo_root / normalized_path).is_symlink():
                errors.append(
                    f"Component '{name}' path must not be a symlink: {normalized_path}"
                )
            elif source == "working-tree" and not _is_within(repo_root, repo_root / normalized_path):
                errors.append(
                    f"Component '{name}' path escapes the repository: {normalized_path}"
                )
            elif source == "working-tree" and not (repo_root / normalized_path).is_dir():
                errors.append(
                    f"Component '{name}' path not found or not a directory: "
                    f"{normalized_path}"
                )
            if normalized_path in seen_component_paths:
                other = seen_component_paths[normalized_path]
                errors.append(
                    f"Duplicate component path '{normalized_path}' used by '{other}' and '{name}'"
                )
            else:
                seen_component_paths[normalized_path] = name
        boundary = comp.get("boundary", {})
        if not isinstance(boundary, dict):
            errors.append(f"Component '{name}' boundary must be an object")
            continue
        if "provider" not in boundary:
            errors.append(f"Component '{name}' missing required field: boundary.provider")
        elif not isinstance(boundary["provider"], str) or not boundary["provider"].strip():
            errors.append(f"Component '{name}' field 'boundary.provider' must be a non-empty string")
        elif boundary["provider"] not in known_providers and not boundary["provider"].startswith("custom."):
            errors.append(
                f"Component '{name}' has unsupported boundary.provider '{boundary['provider']}' "
                "(use a known provider or custom.* namespace)"
            )
        elif boundary["provider"].startswith("custom."):
            # Verify the custom provider is declared in the top-level providers list.
            provider_name = boundary["provider"]
            if providers_list is None:
                errors.append(
                    f"Component '{name}' uses '{provider_name}' but no 'providers' list is "
                    "declared in the config — add a top-level 'providers' array"
                )
            elif declared_custom_names and provider_name not in declared_custom_names:
                errors.append(
                    f"Component '{name}' uses '{provider_name}' which is not declared in the "
                    f"'providers' list — add an entry with \"class\": \"{provider_name[7:]}\""
                )
        paths = boundary.get("paths", [])
        if "paths" in boundary and not _is_str_list(paths):
            errors.append(f"Component '{name}' field 'boundary.paths' must be an array of strings")
            paths = []
        _validate_component_path_entries(
            errors,
            repo_root,
            name,
            component_path,
            "boundary",
            paths,
            check_exists=(source == "working-tree"),
        )

        behavior = comp.get("behavior")
        if behavior is not None:
            if not isinstance(behavior, dict):
                errors.append(f"Component '{name}' behavior must be an object")
            else:
                behavior_paths = behavior.get("paths", [])
                if "paths" in behavior and not _is_str_list(behavior_paths):
                    errors.append(f"Component '{name}' field 'behavior.paths' must be an array of strings")
                    behavior_paths = []
                _validate_component_path_entries(
                    errors,
                    repo_root,
                    name,
                    component_path,
                    "behavior",
                    behavior_paths,
                    check_exists=(source == "working-tree"),
                )
        vendored = comp.get("vendored_copies")
        if vendored is not None and not _is_str_list(vendored):
            errors.append(f"Component '{name}' field 'vendored_copies' must be an array of strings")
        elif vendored is not None:
            for vendored_path in vendored:
                normalized_vendored = vendored_path.replace("\\", "/")
                if (
                    not vendored_path.strip()
                    or "\\" in vendored_path
                    or Path(vendored_path).is_absolute()
                    or ".." in normalized_vendored.split("/")
                    or not _is_within(repo_root, repo_root / vendored_path)
                ):
                    errors.append(
                        f"Component '{name}' vendored copy must be a safe repo-relative "
                        f"path using '/' separators: {vendored_path!r}"
                    )
                elif source == "working-tree" and not (repo_root / vendored_path).exists():
                    errors.append(
                        f"Component '{name}' vendored copy not found: {vendored_path}"
                    )
        consumers = comp.get("consumers")
        if consumers is not None:
            if not _is_str_list(consumers):
                errors.append(f"Component '{name}' field 'consumers' must be an array of strings")
            else:
                if len(consumers) != len(set(consumers)):
                    errors.append(f"Component '{name}' field 'consumers' contains duplicates")
                for consumer in consumers:
                    if consumer == name:
                        errors.append(f"Component '{name}' cannot consume its own boundary")
                    elif consumer not in components:
                        errors.append(
                            f"Component '{name}' references unknown consumer: {consumer}"
                        )

        # Validate version_source — check file exists and has a supported extension.
        version_source = comp.get("version_source")
        if version_source is not None:
            if not isinstance(version_source, dict):
                errors.append(f"Component '{name}' field 'version_source' must be an object")
            elif "git_tag_prefix" in version_source:
                if not isinstance(version_source["git_tag_prefix"], str) or not version_source["git_tag_prefix"].strip():
                    errors.append(f"Component '{name}' version_source.git_tag_prefix must be a non-empty string")
            elif "file" in version_source:
                vs_file = version_source["file"]
                if not isinstance(vs_file, str) or not vs_file.strip():
                    errors.append(f"Component '{name}' version_source.file must be a non-empty string")
                else:
                    if (
                        "\\" in vs_file
                        or Path(vs_file).is_absolute()
                        or ".." in vs_file.replace("\\", "/").split("/")
                    ):
                        errors.append(
                            f"Component '{name}' version_source.file must be a safe "
                            f"component-relative path using '/' separators: {vs_file!r}"
                        )
                    _supported_vs_exts = {".json", ".toml", ".yaml", ".yml"}
                    if Path(vs_file).suffix not in _supported_vs_exts:
                        errors.append(
                            f"Component '{name}' version_source.file has unsupported extension '{Path(vs_file).suffix}'"
                            f" — supported: {', '.join(sorted(_supported_vs_exts))}"
                        )
                    vs_full = (repo_root / component_path / vs_file) if component_path is not None else None
                    if source == "working-tree" and vs_full is not None and vs_full.is_symlink():
                        errors.append(
                            f"Component '{name}' version_source.file must not be a symlink: "
                            f"'{vs_file}'"
                        )
                    elif source == "working-tree" and vs_full is not None and not _is_within(repo_root, vs_full):
                        errors.append(
                            f"Component '{name}' version_source.file escapes the repository: "
                            f"'{vs_file}'"
                        )
                    if source == "working-tree" and vs_full is not None and not vs_full.exists():
                        errors.append(
                            f"Component '{name}' version_source.file not found: '{vs_file}'"
                            f" (looked for {component_path}/{vs_file})"
                        )
                    if "field" not in version_source:
                        errors.append(
                            f"Component '{name}' version_source has 'file' but no 'field' — "
                            "specify which field to read, e.g. \"field\": \"version\""
                        )
                    elif not isinstance(version_source.get("field"), str) or not version_source["field"].strip():
                        errors.append(
                            f"Component '{name}' version_source.field must be a non-empty string"
                        )
            else:
                errors.append(
                    f"Component '{name}' version_source must have either 'file' or 'git_tag_prefix'"
                )

    for sname, sdef in slices.items():
        if not isinstance(sname, str) or not sname.strip():
            errors.append("Slice names must be non-empty strings")
            continue
        if not isinstance(sdef, dict):
            errors.append(f"Slice '{sname}' must be an object")
            continue
        mode = sdef.get("mode", "exact")
        if not isinstance(mode, str) or mode not in supported_modes:
            errors.append(f"Slice '{sname}' has unknown mode: {mode}")
        slice_components = sdef.get("components", [])
        if not _is_str_list(slice_components):
            errors.append(f"Slice '{sname}' field 'components' must be an array of strings")
            continue
        for cname in slice_components:
            if cname not in components:
                errors.append(f"Slice '{sname}' references unknown component: {cname}")
                continue
            if mode == "boundary":
                kind = boundary_provider_name(components[cname].get("boundary", {}))
                paths = components[cname].get("boundary", {}).get("paths", [])
                if kind == "leaf":
                    errors.append(f"Slice '{sname}' in {mode} mode cannot include '{cname}' (leaf provider produces no boundary)")
                elif kind == "implicit" and not paths:
                    errors.append(f"Slice '{sname}' in {mode} mode cannot include '{cname}' (implicit with no paths produces no boundary)")
                elif kind not in {"leaf", "implicit"} and not paths:
                    errors.append(f"Slice '{sname}' in {mode} mode includes '{cname}' with no boundary paths")

    registry = create_registry()
    provider_load_errors = load_custom_providers(
        config.get("providers", []),
        allow_custom=allow_custom_providers,
        registry=registry,
    )
    if allow_custom_providers:
        errors.extend(provider_load_errors)
    for name, comp in components.items():
        if not isinstance(name, str) or not name.strip():
            errors.append("Component names must be non-empty strings")
            continue
        if not isinstance(comp, dict):
            continue
        boundary = comp.get("boundary")
        component_path = comp.get("path")
        if not isinstance(boundary, dict) or not isinstance(component_path, str):
            continue
        provider_name = boundary_provider_name(boundary)
        provider = get_provider(provider_name, registry=registry)
        if provider is None:
            if allow_custom_providers and provider_name.startswith("custom."):
                errors.append(
                    f"Component '{name}' provider '{provider_name}' was not registered "
                    "by the configured provider module"
                )
            continue
        if source == "working-tree" or provider_name.startswith("custom."):
            for provider_error in validate_provider_config(
                provider, boundary, component_path, repo_root
            ):
                errors.append(f"Component '{name}': {provider_error}")

    return errors


def config_warnings(config: dict, repo_root: Path) -> List[str]:
    warnings: List[str] = []
    if not isinstance(config, dict):
        return warnings

    components = config.get("components", {})
    if not isinstance(components, dict):
        return warnings

    for name, comp in components.items():
        if not isinstance(comp, dict):
            continue
        component_path = comp.get("path") if isinstance(comp.get("path"), str) and comp["path"].strip() else None
        if component_path is None:
            continue

        boundary = comp.get("boundary", {})
        behavior = comp.get("behavior")
        if not isinstance(boundary, dict) or not isinstance(behavior, dict):
            continue

        boundary_paths = boundary.get("paths", [])
        behavior_paths = behavior.get("paths", [])
        if not _is_str_list(boundary_paths) or not _is_str_list(behavior_paths):
            continue
        if not boundary_paths:
            continue

        boundary_files = _expand_component_paths(repo_root, component_path, boundary_paths)
        if not boundary_files:
            continue
        behavior_files = _expand_component_paths(repo_root, component_path, behavior_paths)
        uncovered = sorted(boundary_files - behavior_files)
        if uncovered:
            preview = ", ".join(uncovered[:3])
            if len(uncovered) > 3:
                preview += f", +{len(uncovered) - 3} more"
            warnings.append(
                f"Component '{name}' behavior.paths does not currently cover boundary files: {preview}"
                " — behavior should usually be a superset of boundary"
            )

    return warnings


def discover_components(repo_root: Path) -> Dict[str, dict]:
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
    _MAX_DISCOVER = 1000  # Cap discovered components to prevent runaway on huge monorepos
    found: Dict[str, dict] = {}
    seen_directories: Set[str] = set()
    try:
        tracked = _git_run_bytes(repo_root, ["ls-files", "-z", "--"])
        candidate_paths = [repo_root / p for p in _decode_nul_paths(tracked.stdout)]
        if not candidate_paths:
            try:
                _git_run_bytes(repo_root, ["rev-parse", "--verify", "HEAD"])
            except subprocess.CalledProcessError:
                for manifest, _field in manifest_specs:
                    candidate_paths.extend(repo_root.rglob(manifest))
    except (OSError, subprocess.CalledProcessError):
        candidate_paths = []
        for manifest, _field in manifest_specs:
            candidate_paths.extend(repo_root.rglob(manifest))

    for manifest, version_field in manifest_specs:
        for mf in sorted(p for p in candidate_paths if p.name == manifest):
            if _ignored_dirs & set(mf.relative_to(repo_root).parts):
                continue
            rel_dir = mf.parent.relative_to(repo_root)
            if str(rel_dir) == ".":
                continue
            rel_path = _to_posix(str(rel_dir))
            if rel_path in seen_directories:
                continue
            seen_directories.add(rel_path)
            comp_name = rel_dir.name
            base_name = comp_name
            idx = 2
            while comp_name in found:
                comp_name = f"{base_name}-{idx}"
                idx += 1
            provider, paths = _detect_provider(mf.parent)
            version_source = (
                {"file": mf.name, "field": version_field}
                if version_field is not None
                else None
            )
            found[comp_name] = {
                "path": rel_path,
                "version_source": version_source,
                "boundary": {"provider": provider, "paths": paths},
            }
            if len(found) >= _MAX_DISCOVER:
                return found
    return found


def _detect_provider(component_dir: Path) -> tuple:
    """Detect the best boundary provider and paths for a component directory.

    Returns (provider_name, paths_list).
    """
    # OpenAPI specs
    for pattern in ("openapi.yaml", "openapi.yml", "openapi.json", "swagger.yaml", "swagger.json"):
        candidates = sorted(component_dir.glob(pattern))
        if candidates:
            return ("openapi", [candidates[0].name])
    # Glob for any openapi-like files
    for f in sorted(component_dir.iterdir()) if component_dir.exists() else []:
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
