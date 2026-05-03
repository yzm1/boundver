"""Config validation and component discovery for boundver."""

import fnmatch
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ._git import _to_posix
from ._hashing import _is_within
from ._utils import _is_glob, boundary_provider_name, ConfigError

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
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"JSON parse error in {path}: {exc}") from exc
    if suffix in (".yaml", ".yml"):
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
        if not isinstance(result, dict):
            raise ConfigError(f"Config file {path} must contain a YAML mapping, got {type(result).__name__}")
        return result
    if suffix == ".toml":
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
            return tomllib.loads(text)
        except (MemoryError, RecursionError):
            raise
        except Exception as exc:
            raise ConfigError(f"TOML parse error in {path}: {exc}") from exc
    raise ConfigError(
        f"Unsupported config file extension '{suffix}' for {path}. "
        "Supported formats: .json, .yaml, .yml, .toml"
    )



def _load_config_schema(repo_root: Path) -> Optional[dict]:
    schema_path = repo_root / "boundary.config.schema.json"
    if not schema_path.exists():
        return None
    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
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
) -> None:
    """Validate a component-relative path list for boundary/behavior config."""
    if component_path is None:
        return

    component_root = repo_root / component_path
    for rel in paths:
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
        if not full.exists():
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


def validate_config(config: dict, repo_root: Path) -> List[str]:
    errors: List[str] = []
    if not isinstance(config, dict):
        return ["Config root must be a JSON object"]

    schema = _load_config_schema(repo_root)
    errors.extend(_schema_engine_errors(config, schema))
    for required_key in _schema_required_fields(schema):
        if required_key not in config:
            errors.append(f"Missing required top-level field: {required_key}")

    supported_modes = {"exact", "behavior", "boundary", "compat"}
    compat_mode = config.get("defaults", {}).get("compat_mode", "major")
    if compat_mode not in {"major", "semver_major", "semver_major_minor"}:
        errors.append(f"Unsupported defaults.compat_mode: {compat_mode}")

    components = config.get("components", {})
    slices = config.get("slices", {})
    if not isinstance(components, dict):
        errors.append("Field 'components' must be an object")
        components = {}
    if not isinstance(slices, dict):
        errors.append("Field 'slices' must be an object")
        slices = {}

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
                elif isinstance(cls_name, str) and cls_name.strip():
                    declared_custom_names.add(f"custom.{cls_name.strip()}")

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
            normalized_path = component_path.rstrip("/")
            if ".." in normalized_path.replace("\\", "/").split("/"):
                errors.append(
                    f"Component '{name}' path must not contain '..': {normalized_path}"
                )
            elif Path(normalized_path).is_absolute():
                errors.append(
                    f"Component '{name}' path must be relative: {normalized_path}"
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
            elif provider_name not in declared_custom_names:
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
                )
        vendored = comp.get("vendored_copies")
        if vendored is not None and not _is_str_list(vendored):
            errors.append(f"Component '{name}' field 'vendored_copies' must be an array of strings")

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
                    _supported_vs_exts = {".json", ".toml", ".yaml", ".yml"}
                    if Path(vs_file).suffix not in _supported_vs_exts:
                        errors.append(
                            f"Component '{name}' version_source.file has unsupported extension '{Path(vs_file).suffix}'"
                            f" — supported: {', '.join(sorted(_supported_vs_exts))}"
                        )
                    vs_full = (repo_root / component_path / vs_file) if component_path is not None else None
                    if vs_full is not None and not vs_full.exists():
                        errors.append(
                            f"Component '{name}' version_source.file not found: '{vs_file}'"
                            f" (looked for {component_path}/{vs_file})"
                        )
                    if "field" not in version_source:
                        errors.append(
                            f"Component '{name}' version_source has 'file' but no 'field' — "
                            "specify which field to read, e.g. \"field\": \"version\""
                        )
            else:
                errors.append(
                    f"Component '{name}' version_source must have either 'file' or 'git_tag_prefix'"
                )

    for sname, sdef in slices.items():
        if not isinstance(sdef, dict):
            errors.append(f"Slice '{sname}' must be an object")
            continue
        mode = sdef.get("mode", "exact")
        if mode not in supported_modes:
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
    """Best-effort component discovery from common manifest files."""
    manifests = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod")
    _ignored_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    _MAX_DISCOVER = 1000  # Cap discovered components to prevent runaway on huge monorepos
    found: Dict[str, dict] = {}
    for manifest in manifests:
        for mf in sorted(repo_root.rglob(manifest)):
            if _ignored_dirs & set(mf.relative_to(repo_root).parts):
                continue
            rel_dir = mf.parent.relative_to(repo_root)
            if str(rel_dir) == ".":
                continue
            comp_name = rel_dir.name
            base_name = comp_name
            idx = 2
            while comp_name in found:
                comp_name = f"{base_name}-{idx}"
                idx += 1
            provider, paths = _detect_provider(mf.parent)
            found[comp_name] = {
                "path": _to_posix(str(rel_dir)),
                "version_source": {"file": mf.name, "field": "version"},
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
