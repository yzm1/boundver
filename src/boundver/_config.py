"""Config validation and component discovery for boundver."""

import fnmatch
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._git import _to_posix
from ._hashing import _is_within, boundary_provider_name

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
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON parse error in {path}: {exc}") from exc
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
            result = yaml.safe_load(text)
        except ImportError:
            raise ValueError(
                f"Cannot parse {path}: PyYAML is not installed. "
                "Install it with: pip install PyYAML"
            )
        except Exception as exc:
            raise ValueError(f"YAML parse error in {path}: {exc}") from exc
        if not isinstance(result, dict):
            raise ValueError(f"Config file {path} must contain a YAML mapping, got {type(result).__name__}")
        return result
    if suffix == ".toml":
        try:
            import tomllib  # type: ignore  # Python 3.11+
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore  # pip install tomli
            except ImportError:
                raise ValueError(
                    f"Cannot parse {path}: neither tomllib (Python 3.11+) nor tomli is available. "
                    "Install tomli: pip install tomli"
                )
        try:
            return tomllib.loads(text)
        except Exception as exc:
            raise ValueError(f"TOML parse error in {path}: {exc}") from exc
    raise ValueError(
        f"Unsupported config file extension '{suffix}' for {path}. "
        "Supported formats: .json, .yaml, .yml, .toml"
    )


def _is_glob(pattern: str) -> bool:
    return any(c in pattern for c in ("*", "?", "["))


def _load_config_schema(repo_root: Path) -> Optional[dict]:
    schema_path = repo_root / "boundary.config.schema.json"
    if not schema_path.exists():
        return None
    try:
        return json.loads(schema_path.read_text())
    except json.JSONDecodeError:
        return None


def _schema_required_fields(schema: Optional[dict]) -> List[str]:
    if not schema:
        return ["project", "components", "slices"]
    required = schema.get("required", [])
    return [k for k in required if isinstance(k, str)] or ["project", "components", "slices"]


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


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

    supported_modes = {"exact", "boundary", "compat"}
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
                # Track declared custom provider names for cross-reference
                cls_name = entry.get("class", "")
                if isinstance(cls_name, str) and cls_name.strip():
                    declared_custom_names.add(f"custom.{cls_name.strip()}")

    for name, comp in components.items():
        if not isinstance(comp, dict):
            errors.append(f"Component '{name}' must be an object")
            continue
        if "path" not in comp:
            errors.append(f"Component '{name}' missing required field: path")
            continue
        if not isinstance(comp["path"], str) or not comp["path"].strip():
            errors.append(f"Component '{name}' field 'path' must be a non-empty string")
        else:
            normalized_path = comp["path"].rstrip("/")
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
        for rel in paths:
            # Glob patterns (*, ?, [) are expanded at runtime against actual files.
            if _is_glob(rel):
                if ".." in rel:
                    errors.append(
                        f"Component '{name}' boundary glob pattern must not contain '..': {rel}"
                    )
                continue
            full = repo_root / comp["path"] / rel
            component_root = repo_root / comp["path"]
            if not _is_within(component_root, full):
                errors.append(f"Component '{name}' boundary path escapes component root: {rel}")
                continue
            if not _is_within(repo_root, full):
                errors.append(f"Component '{name}' boundary path escapes repository root: {rel}")
                continue
            if not full.exists():
                errors.append(
                    f"Component '{name}' boundary path not found: {comp['path']}/{rel}"
                    f" — ensure the file exists before running generate"
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
                    vs_full = repo_root / comp["path"] / vs_file
                    if not vs_full.exists():
                        errors.append(
                            f"Component '{name}' version_source.file not found: '{vs_file}'"
                            f" (looked for {comp['path']}/{vs_file})"
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


def discover_components(repo_root: Path) -> Dict[str, dict]:
    """Best-effort component discovery from common manifest files."""
    manifests = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod")
    _ignored_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
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
