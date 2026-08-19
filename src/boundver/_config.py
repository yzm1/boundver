"""Config validation and component discovery for boundver."""

import json
import os
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ._git import (
    GitSourceSnapshot,
    _capture_git_source_snapshot,
    _git_cat_blob,
    _git_run,
    _iter_bounded_git_paths,
    _is_git_repository,
    _list_files_for_source,
    _snapshot_files,
    _to_posix,
)
from ._hashing import _read_bounded_path_bytes
from ._utils import (
    FACET_SET,
    SOURCE_MODE_SET,
    _bounded_diagnostic_repr,
    _bounded_diagnostic_text,
    _bounded_json_value_issues,
    _bounded_json_int,
    _bounded_sorted_paths,
    _bounded_yaml_int,
    _is_glob,
    _is_within,
    _iter_bounded_filesystem_paths,
    _iter_bounded_json_values,
    _match_path_glob,
    _normalize_declared_path,
    _toml_has_oversized_numeric_token,
    boundary_provider_name,
    ConfigError,
    GuardrailError,
)
from .providers import (
    MAX_CUSTOM_PROVIDERS,
    MAX_PROVIDER_DECLARATIONS,
    create_registry,
    get_provider,
    load_custom_providers,
    validate_provider_config,
)
from ._consumer_graph import resolve_slice_components

# Ordered preference when auto-discovering config (first match wins)
_CONFIG_CANDIDATES = [
    "boundary.config.json",
    "boundary.config.yaml",
    "boundary.config.yml",
    "boundary.config.toml",
]

MAX_CONFIG_BYTES = 10 * 1024 * 1024
MAX_COMPONENT_EXPANSION_FILES = 50_000
MAX_DISCOVERY_MANIFESTS = 50_000
MAX_DISCOVERED_COMPONENTS = 1_000
MAX_PROVIDER_DETECTION_ENTRIES = 50_000
MAX_FILESYSTEM_TRAVERSAL_ENTRIES = 200_000


def _snapshot_relative_path(repo_root: Path, path: Path) -> str:
    """Return a safe repository-relative label for immutable source reads."""
    root = Path(os.path.abspath(repo_root))
    candidate = path if path.is_absolute() else root / path
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ConfigError(
            f"Source-backed path must stay within the repository: {path}"
        ) from exc
    label = relative.as_posix()
    if not label or label == ".":
        raise ConfigError(f"Source-backed path must name a file: {path}")
    return label


def _json_object_without_duplicates(pairs: List[tuple]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(
                "duplicate JSON object key " f"{_bounded_diagnostic_repr(key)}"
            )
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> Any:
    raise ConfigError(
        "non-finite JSON number "
        f"{_bounded_diagnostic_repr(value)} is not supported"
    )


def _json_value_issues(value: Any, *, path: str = "config") -> List[str]:
    """Return reasons a parsed value is unsafe for deterministic JSON hashing."""
    return _bounded_json_value_issues(value, path=path)


def parse_config_text(text: str, path: Path) -> dict:
    """Parse config text according to *path* while rejecting lossy values."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            result = json.loads(
                text,
                object_pairs_hook=_json_object_without_duplicates,
                parse_constant=_reject_nonfinite_json_constant,
                parse_int=_bounded_json_int,
            )
        except (ValueError, RecursionError, OverflowError) as exc:
            raise ConfigError(f"JSON parse error in {path}: {exc}") from exc
    elif suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore

            class StrictConfigLoader(yaml.SafeLoader):
                def compose_node(self, parent: Any, index: Any) -> Any:
                    if self.check_event(yaml.AliasEvent):
                        raise ConfigError(
                            "YAML aliases are not supported in boundver config"
                        )
                    return super().compose_node(parent, index)

            def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict:
                if not isinstance(node, yaml.MappingNode):
                    raise ConfigError("expected a YAML mapping")
                mapping: dict = {}
                for key_node, value_node in node.value:
                    key = loader.construct_object(key_node, deep=deep)
                    if type(key) is not str:
                        raise ConfigError(
                            "YAML mapping keys must be strings, got "
                            f"{_bounded_diagnostic_repr(key)}"
                        )
                    if key in mapping:
                        raise ConfigError(f"duplicate YAML mapping key {key!r}")
                    mapping[key] = loader.construct_object(value_node, deep=deep)
                return mapping

            def construct_integer(loader: Any, node: Any) -> int:
                try:
                    scalar = loader.construct_scalar(node)
                    return _bounded_yaml_int(scalar)
                except (TypeError, ValueError) as exc:
                    raise ConfigError(f"invalid YAML integer: {exc}") from exc

            StrictConfigLoader.add_constructor(
                yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                construct_mapping,
            )
            StrictConfigLoader.add_constructor(
                "tag:yaml.org,2002:int",
                construct_integer,
            )
            result = yaml.load(text, Loader=StrictConfigLoader)
        except ImportError:
            raise ConfigError(
                f"Cannot parse {path}: PyYAML is not installed. "
                "Install it with: pip install PyYAML"
            )
        except MemoryError:
            raise
        except RecursionError as exc:
            raise ConfigError(f"YAML config is nested too deeply in {path}") from exc
        except Exception as exc:
            raise ConfigError(f"YAML parse error in {path}: {exc}") from exc
    elif suffix == ".toml":
        try:
            import tomllib  # type: ignore  # Python 3.11+
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore
            except ImportError:
                raise ConfigError(
                    f"Cannot parse {path}: neither tomllib (Python 3.11+) nor tomli is available. "
                    "Install tomli: pip install tomli"
                )
        if _toml_has_oversized_numeric_token(text):
            raise ConfigError(
                f"TOML config contains a numeric token exceeding the "
                f"cross-runtime safety limit in {path}"
            )
        try:
            result = tomllib.loads(text)
        except MemoryError:
            raise
        except RecursionError as exc:
            raise ConfigError(f"TOML config is nested too deeply in {path}") from exc
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
    try:
        value_issues = _json_value_issues(result)
    except RecursionError as exc:
        raise ConfigError(f"Config is nested too deeply in {path}") from exc
    if value_issues:
        raise ConfigError(
            f"Config file {path} contains values that cannot be represented "
            "as deterministic JSON:\n" + "\n".join(value_issues)
        )
    return result


def parse_config_bytes(data: bytes, path: Path) -> dict:
    """Decode and parse bounded UTF-8 config bytes."""
    if len(data) > MAX_CONFIG_BYTES:
        raise ConfigError(f"Config file too large ({len(data)} bytes): {path}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"Config file is not valid UTF-8: {path}: {exc}") from exc
    return parse_config_text(text, path)


def find_config_file(
    repo_root: Path,
    hint: str = "boundary.config.json",
    *,
    snapshot: Optional[GitSourceSnapshot] = None,
) -> Path:
    """Return the config file path to use.

    If *hint* is the default ``boundary.config.json`` and that file does not
    exist, probe for YAML/TOML alternatives in order.  If *hint* is an explicit
    user-supplied path, return it as-is (the caller handles missing-file errors).
    """
    explicit = Path(hint)
    if explicit.is_absolute():
        return explicit
    candidate = repo_root / hint
    if snapshot is not None:
        candidate_label = _snapshot_relative_path(repo_root, candidate)
        if candidate_label in snapshot.entries:
            return candidate
    elif candidate.exists():
        return candidate
    # Only auto-probe when the hint is the default JSON name
    if hint == "boundary.config.json":
        for name in _CONFIG_CANDIDATES[1:]:
            alt = repo_root / name
            if snapshot is not None:
                alt_label = _snapshot_relative_path(repo_root, alt)
                if alt_label in snapshot.entries:
                    return alt
            elif alt.exists():
                return alt
    return candidate  # caller will report missing


def load_config_file(
    path: Path,
    *,
    repo_root: Optional[Path] = None,
    snapshot: Optional[GitSourceSnapshot] = None,
) -> dict:
    """Parse a boundver config file.  Supports JSON, YAML, and TOML.

    Raises ``ValueError`` with a human-readable message on parse failure.
    Raises ``FileNotFoundError`` if *path* does not exist.
    """
    if snapshot is not None:
        if repo_root is None:
            raise ConfigError("repo_root is required for source-backed config reads")
        label = _snapshot_relative_path(repo_root, path)
        entry = snapshot.entries.get(label)
        if entry is None:
            raise FileNotFoundError(
                f"Config file not found in captured {snapshot.source} source: {label}"
            )
        if entry.object_type != "blob" or entry.mode not in {
            "100644", "100755",
        }:
            raise ConfigError(
                f"Config path must be a regular file in captured {snapshot.source} "
                f"source: {label} (mode={entry.mode}, type={entry.object_type})"
            )
        try:
            data = _git_cat_blob(
                repo_root,
                entry.oid,
                max_bytes=MAX_CONFIG_BYTES,
            )
        except GuardrailError as exc:
            raise ConfigError(
                f"Cannot read config from captured {snapshot.source} source: "
                f"{label}: file too large or transport limit exceeded"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise ConfigError(
                f"Cannot read config from captured {snapshot.source} source: "
                f"{label}"
            ) from exc
        return parse_config_bytes(data, path)

    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    try:
        data = _read_bounded_path_bytes(
            path,
            str(path),
            max_bytes=MAX_CONFIG_BYTES,
        )
    except GuardrailError as exc:
        raise ConfigError(
            f"Config file exceeds the {MAX_CONFIG_BYTES}-byte limit at {path}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc
    return parse_config_bytes(data, path)



def _load_config_schema(repo_root: Path) -> Optional[dict]:
    """Load boundver's trusted packaged validation schema.

    A repository file is editor metadata, not executable validation policy: an
    untrusted checkout must not be able to replace the installed contract with
    a permissive schema.  The checkout copy is used only when running directly
    from an incomplete source tree that lacks packaged data.
    """
    try:
        bundled = resources.read_text(
            "boundver", "boundary.config.schema.json", encoding="utf-8"
        )
        return json.loads(bundled)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        schema_path = repo_root / "boundary.config.schema.json"
        if schema_path.exists():
            try:
                schema_data = _read_bounded_path_bytes(
                    schema_path,
                    str(schema_path),
                    max_bytes=MAX_CONFIG_BYTES,
                )
                return json.loads(
                    schema_data.decode("utf-8"),
                    parse_int=_bounded_json_int,
                    parse_constant=_reject_nonfinite_json_constant,
                )
            except (
                GuardrailError,
                OSError,
                UnicodeDecodeError,
                ValueError,
                json.JSONDecodeError,
            ):
                pass
        return None


def _schema_required_fields(schema: Optional[dict]) -> List[str]:
    if not schema:
        return ["project", "components"]
    required = schema.get("required", [])
    return [k for k in required if isinstance(k, str)] or ["project", "components"]


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _reject_unknown_fields(
    errors: List[str],
    value: Any,
    allowed: Set[str],
    context: str,
) -> None:
    """Enforce schema ``additionalProperties: false`` without jsonschema.

    The standalone zipapp deliberately has no third-party dependencies, so
    misspelled policy fields must still fail in the hand validator.
    """
    if not isinstance(value, dict):
        return
    for key in value:
        if not isinstance(key, str):
            errors.append(f"{context} field names must be strings")
        elif key not in allowed:
            errors.append(
                f"Unknown field in {context}: {_bounded_diagnostic_text(key)}"
            )


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
        try:
            normalized = _normalize_declared_path(rel)
        except ValueError as exc:
            errors.append(
                f"Component '{component_name}' {field_name} path {rel!r} {exc}"
            )
            continue
        if _is_glob(normalized):
            continue

        full = component_root / normalized
        if not _is_within(component_root, full):
            errors.append(f"Component '{component_name}' {field_name} path escapes component root: {normalized}")
            continue
        if not _is_within(repo_root, full):
            errors.append(f"Component '{component_name}' {field_name} path escapes repository root: {normalized}")
            continue
        if check_exists and not full.exists():
            errors.append(
                f"Component '{component_name}' {field_name} path not found: {component_path}/{normalized}"
                f" — ensure the file exists before running generate"
            )


def _expand_component_paths(
    repo_root: Path,
    component_path: Optional[str],
    paths: List[str],
    source: Optional[str] = None,
    snapshot: Optional[GitSourceSnapshot] = None,
) -> Set[str]:
    if component_path is None:
        return set()
    if len(paths) > MAX_PROVIDER_DECLARATIONS:
        raise GuardrailError(
            "Component path expansion guardrail exceeded: "
            f">{MAX_PROVIDER_DECLARATIONS} declarations"
        )

    component_root = repo_root / component_path
    # Validation must inspect the same tracked source as hashing. Otherwise an
    # untracked file can make a valid working-tree config fail containment, or
    # a locally deleted file can hide a HEAD/index mistake.
    if source is not None:
        try:
            repo_files = (
                _snapshot_files(snapshot, component_path)
                if snapshot is not None
                else _list_files_for_source(repo_root, component_path, source)
            )
        except GuardrailError:
            raise
        except (OSError, subprocess.CalledProcessError, ValueError):
            return set()
        prefix = component_path.rstrip("/") + "/"
        all_files = sorted(
            repo_file[len(prefix):]
            for repo_file in repo_files
            if repo_file.startswith(prefix)
        )
    else:
        if not component_root.exists() or not component_root.is_dir():
            return set()
        # Filter lazily, enforce the contract, and only then sort.  Sorting the
        # raw ``rglob`` result first would allocate for every directory entry
        # before the safety limit could run.
        filesystem_files = _bounded_sorted_paths(
            (
                path
                for path in _iter_bounded_filesystem_paths(
                    component_root,
                    recursive=True,
                    max_entries=MAX_FILESYSTEM_TRAVERSAL_ENTRIES,
                    exceeded_message=(
                        "Component path expansion guardrail exceeded: "
                        "filesystem traversal exceeds "
                        f"{MAX_FILESYSTEM_TRAVERSAL_ENTRIES} entries"
                    ),
                )
                if path.is_file()
            ),
            max_paths=MAX_COMPONENT_EXPANSION_FILES,
            exceeded_message=(
                "Component path expansion guardrail exceeded: "
                f">{MAX_COMPONENT_EXPANSION_FILES} files"
            ),
        )
        all_files = [
            _to_posix(str(path.relative_to(component_root)))
            for path in filesystem_files
        ]
    matched: Set[str] = set()

    for rel in paths:
        try:
            rel_norm = _normalize_declared_path(rel)
        except ValueError:
            continue
        is_dir_like = rel.endswith("/")
        if _is_glob(rel_norm):
            for file_rel in all_files:
                if _match_path_glob(file_rel, rel_norm):
                    matched.add(file_rel)
            continue

        prefix = rel_norm.rstrip("/")
        is_selected_directory = any(
            file_rel.startswith(prefix + "/") for file_rel in all_files
        )
        if is_dir_like or is_selected_directory:
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

    class DiagnosticInt(int):
        """An integer whose JSON Schema error representation is always safe."""

        def __repr__(self) -> str:
            return _bounded_diagnostic_repr(int.__new__(int, self))

        __str__ = __repr__

    # JSON Schema error construction calls repr() on invalid instances.
    # Values above CPython's smallest supported process-wide conversion limit
    # can otherwise make validation itself raise. Preserve numeric semantics
    # with an int subclass and change only its diagnostic representation.
    diagnostic_int_limit = 10 ** 640
    needs_diagnostic_ints = False
    walker = _iter_bounded_json_values(config, path="config")
    try:
        for item, _item_path in walker:
            if type(item) is int and (
                item >= diagnostic_int_limit or item <= -diagnostic_int_limit
            ):
                needs_diagnostic_ints = True
                break
    except (GuardrailError, RuntimeError, ValueError) as exc:
        return [
            "Config cannot be traversed safely for schema validation: "
            f"{_bounded_diagnostic_text(str(exc))}"
        ]
    finally:
        walker.close()

    schema_instance: Any = config
    if needs_diagnostic_ints:
        schema_instance = {}
        stack = [(config, schema_instance)]
        while stack:
            source_value, target_value = stack.pop()
            if type(source_value) is dict:
                children = source_value.items()
            else:
                children = enumerate(source_value)
            for key, child in children:
                if type(child) is dict:
                    transformed_child: Any = {}
                    stack.append((child, transformed_child))
                elif type(child) is list:
                    transformed_child = []
                    stack.append((child, transformed_child))
                elif type(child) is int and (
                    child >= diagnostic_int_limit
                    or child <= -diagnostic_int_limit
                ):
                    transformed_child = DiagnosticInt(child)
                else:
                    transformed_child = child
                if type(target_value) is dict:
                    target_value[key] = transformed_child
                else:
                    target_value.append(transformed_child)

    try:
        validator = jsonschema.Draft202012Validator(schema)
    except Exception as exc:  # pragma: no cover - defensive path
        try:
            detail = str(exc)
        except Exception:
            detail = exc.__class__.__name__
        return [
            "Schema validator initialization failed: "
            f"{_bounded_diagnostic_text(detail)}"
        ]

    errors: List[str] = []
    try:
        for err in validator.iter_errors(schema_instance):
            path = ".".join(
                _bounded_diagnostic_text(part, max_chars=128)
                for part in err.path
            ) or "<root>"
            path = _bounded_diagnostic_text(path)
            message = _bounded_diagnostic_text(err.message)
            errors.append(f"Schema validation error at {path}: {message}")
    except MemoryError:
        raise
    except RecursionError:
        return ["Config is nested too deeply for schema validation"]
    except Exception as exc:  # pragma: no cover - defensive library boundary
        try:
            detail = str(exc)
        except Exception:
            detail = exc.__class__.__name__
        return [
            "Schema validation failed safely: "
            f"{_bounded_diagnostic_text(detail)}"
        ]
    return sorted(errors)


def validate_config(
    config: dict,
    repo_root: Path,
    allow_custom_providers: bool = False,
    source: str = "working-tree",
    snapshot: Optional[GitSourceSnapshot] = None,
) -> List[str]:
    errors: List[str] = []
    if not isinstance(config, dict):
        return ["Config root must be a JSON object"]
    if source not in SOURCE_MODE_SET:
        return [f"Unknown source mode: {_bounded_diagnostic_repr(source)}"]
    if snapshot is not None and snapshot.source != source:
        return [
            f"Captured source mismatch: snapshot={snapshot.source!r}, source={source!r}"
        ]
    try:
        json_issues = _json_value_issues(config)
    except RecursionError:
        return ["Config is nested too deeply to validate safely"]
    if json_issues:
        return [
            "Config contains values that cannot be represented as deterministic JSON: "
            + issue
            for issue in json_issues
        ]

    # Capture the index membership policy once. An established repository (or
    # an unborn repository with staged entries) has authoritative tracked
    # state; a non-Git directory or truly empty unborn index retains the
    # documented first-run filesystem fallback.
    tracking_snapshot: Optional[GitSourceSnapshot] = None
    tracking_snapshot_error: Optional[str] = None
    if source == "working-tree":
        try:
            candidate_tracking = _capture_git_source_snapshot(repo_root, "index")
        except (OSError, ValueError) as exc:
            if _is_git_repository(repo_root):
                tracking_snapshot_error = str(exc)
        else:
            if candidate_tracking.entries or candidate_tracking.head_oid is not None:
                tracking_snapshot = candidate_tracking

    schema = _load_config_schema(repo_root)
    errors.extend(_schema_engine_errors(config, schema))
    _reject_unknown_fields(
        errors,
        config,
        {"$schema", "project", "providers", "defaults", "components", "slices"},
        "config",
    )
    for required_key in _schema_required_fields(schema):
        if required_key not in config:
            errors.append(f"Missing required top-level field: {required_key}")

    project = config.get("project")
    if not isinstance(project, str) or not project.strip():
        errors.append("Field 'project' must be a non-empty string")
    elif project != project.strip():
        errors.append("Field 'project' must not have surrounding whitespace")
    if "$schema" in config and not isinstance(config["$schema"], str):
        errors.append("Field '$schema' must be a string")

    supported_modes = FACET_SET
    defaults = config.get("defaults", {})
    if not isinstance(defaults, dict):
        errors.append("Field 'defaults' must be an object")
        defaults = {}
    else:
        _reject_unknown_fields(
            errors, defaults, {"compat_mode", "verify_facets"}, "defaults"
        )
    compat_mode = defaults.get("compat_mode", "major")
    if not isinstance(compat_mode, str) or compat_mode not in {
        "major", "semver_major", "semver_major_minor"
    }:
        errors.append(
            "Unsupported defaults.compat_mode: "
            f"{_bounded_diagnostic_text(compat_mode)}"
        )
    verify_facets = defaults.get("verify_facets")
    if verify_facets is not None:
        if not _is_str_list(verify_facets) or not verify_facets:
            errors.append("defaults.verify_facets must be a non-empty array of strings")
        else:
            if len(verify_facets) != len(set(verify_facets)):
                errors.append("defaults.verify_facets contains duplicates")
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
    # Derive validation names from the same registry used for resolution so a
    # newly registered built-in or alias cannot be accepted by one layer and
    # rejected by the other.
    registry = create_registry()
    known_providers = set(registry)

    # Validate top-level providers list (Phase 2)
    providers_list = config.get("providers")
    declared_custom_names: set = set()
    if providers_list is not None:
        if not isinstance(providers_list, list):
            errors.append("Top-level 'providers' must be an array")
        elif len(providers_list) > MAX_CUSTOM_PROVIDERS:
            errors.append(
                "Top-level 'providers' exceeds the "
                f"{MAX_CUSTOM_PROVIDERS}-provider limit"
            )
        else:
            for i, entry in enumerate(providers_list):
                if not isinstance(entry, dict):
                    errors.append(f"providers[{i}] must be an object")
                    continue
                _reject_unknown_fields(
                    errors, entry, {"module", "class", "name"}, f"providers[{i}]"
                )
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
                raw_declared_name = entry.get("name")
                declared_name = (
                    raw_declared_name.strip()
                    if isinstance(raw_declared_name, str)
                    else ""
                )
                if raw_declared_name is not None:
                    if not isinstance(raw_declared_name, str) or not declared_name:
                        errors.append(
                            f"providers[{i}] field 'name' must be a non-empty string"
                        )
                    elif raw_declared_name != declared_name:
                        errors.append(
                            f"providers[{i}] field 'name' must not have surrounding whitespace"
                        )
                    elif not declared_name.startswith("custom.") or declared_name == "custom.":
                        errors.append(
                            f"providers[{i}] field 'name' must start with 'custom.' "
                            "and include a name after the prefix "
                            f"to avoid collisions with built-in providers "
                            f"(got '{declared_name}', try 'custom.{declared_name}')"
                        )
                    else:
                        declared_custom_names.add(declared_name)
                # Without an explicit name, the provider instance determines
                # its registered custom.* identifier after trusted loading.

    for name, comp in components.items():
        if not isinstance(name, str) or not name.strip():
            errors.append("Component names must be non-empty strings")
            continue
        if not isinstance(comp, dict):
            errors.append(f"Component '{name}' must be an object")
            continue
        _reject_unknown_fields(
            errors,
            comp,
            {
                "path", "ecosystem", "version_source", "boundary", "behavior",
                "vendored_copies", "consumers", "external_consumers",
                "verify_facets",
            },
            f"component '{name}'",
        )
        if "ecosystem" in comp and not isinstance(comp["ecosystem"], str):
            errors.append(f"Component '{name}' field 'ecosystem' must be a string")
        if "path" not in comp:
            errors.append(f"Component '{name}' missing required field: path")
            continue
        raw_component_path = comp.get("path")
        component_path: Optional[str] = None
        if not isinstance(raw_component_path, str) or not raw_component_path.strip():
            errors.append(f"Component '{name}' field 'path' must be a non-empty string")
        else:
            normalized_path: Optional[str] = None
            if raw_component_path.strip().rstrip("/") == ".":
                errors.append(
                    f"Component '{name}' cannot use the repository root as its path: "
                    "the generated lockfile would become part of its own exact fingerprint. "
                    "Place the component in a subdirectory."
                )
            else:
                try:
                    normalized_path = _normalize_declared_path(raw_component_path)
                except ValueError as exc:
                    errors.append(f"Component '{name}' path {raw_component_path!r} {exc}")
                    normalized_path = None
            if normalized_path is not None and _is_glob(normalized_path):
                errors.append(
                    f"Component '{name}' path must be a literal directory, not a glob: "
                    f"{normalized_path}"
                )
                normalized_path = None
            if normalized_path is not None:
                component_path = normalized_path
            if component_path is not None and source == "working-tree" and (repo_root / component_path).is_symlink():
                errors.append(
                    f"Component '{name}' path must not be a symlink: {component_path}"
                )
            elif component_path is not None and source == "working-tree" and not _is_within(repo_root, repo_root / component_path):
                errors.append(
                    f"Component '{name}' path escapes the repository: {component_path}"
                )
            elif component_path is not None and source == "working-tree" and not (repo_root / component_path).is_dir():
                errors.append(
                    f"Component '{name}' path not found or not a directory: "
                    f"{component_path}"
                )
            elif component_path is not None and source in {"head", "index"}:
                try:
                    selected_files = (
                        _snapshot_files(snapshot, component_path)
                        if snapshot is not None
                        else _list_files_for_source(
                            repo_root, component_path, source
                        )
                    )
                except (OSError, subprocess.CalledProcessError, ValueError) as exc:
                    errors.append(
                        f"Component '{name}' path cannot be read at {source}: {exc}"
                    )
                else:
                    directory_prefix = component_path.rstrip("/") + "/"
                    if not any(
                        selected.startswith(directory_prefix)
                        for selected in selected_files
                    ):
                        errors.append(
                            f"Component '{name}' path is not a tracked directory "
                            f"at {source}: {component_path}"
                        )
            if component_path is not None and component_path in seen_component_paths:
                other = seen_component_paths[component_path]
                errors.append(
                    f"Duplicate component path '{component_path}' used by '{other}' and '{name}'"
                )
            elif component_path is not None:
                seen_component_paths[component_path] = name
        boundary = comp.get("boundary", {})
        if not isinstance(boundary, dict):
            errors.append(f"Component '{name}' boundary must be an object")
            continue
        _reject_unknown_fields(
            errors,
            boundary,
            {"provider", "paths", "options", "note"},
            f"component '{name}' boundary",
        )
        if "provider" not in boundary:
            errors.append(f"Component '{name}' missing required field: boundary.provider")
        elif not isinstance(boundary["provider"], str) or not boundary["provider"].strip():
            errors.append(f"Component '{name}' field 'boundary.provider' must be a non-empty string")
        elif boundary["provider"] != boundary["provider"].strip():
            errors.append(f"Component '{name}' boundary.provider must not have surrounding whitespace")
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
        if "options" in boundary and not isinstance(boundary["options"], dict):
            errors.append(f"Component '{name}' field 'boundary.options' must be an object")
        if "note" in boundary and not isinstance(boundary["note"], str):
            errors.append(f"Component '{name}' field 'boundary.note' must be a string")
        paths = boundary.get("paths", [])
        if "paths" in boundary and not _is_str_list(paths):
            errors.append(f"Component '{name}' field 'boundary.paths' must be an array of strings")
            paths = []
        elif len(paths) > MAX_PROVIDER_DECLARATIONS:
            errors.append(
                f"Component '{name}' field 'boundary.paths' exceeds the "
                f"{MAX_PROVIDER_DECLARATIONS}-declaration limit"
            )
            paths = []
        elif len(paths) != len(set(paths)):
            errors.append(f"Component '{name}' field 'boundary.paths' contains duplicates")
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
                _reject_unknown_fields(
                    errors,
                    behavior,
                    {"paths"},
                    f"component '{name}' behavior",
                )
                behavior_paths = behavior.get("paths", [])
                if "paths" in behavior and not _is_str_list(behavior_paths):
                    errors.append(f"Component '{name}' field 'behavior.paths' must be an array of strings")
                    behavior_paths = []
                elif len(behavior_paths) > MAX_PROVIDER_DECLARATIONS:
                    errors.append(
                        f"Component '{name}' field 'behavior.paths' exceeds the "
                        f"{MAX_PROVIDER_DECLARATIONS}-declaration limit"
                    )
                    behavior_paths = []
                elif len(behavior_paths) != len(set(behavior_paths)):
                    errors.append(f"Component '{name}' field 'behavior.paths' contains duplicates")
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
            if len(vendored) != len(set(vendored)):
                errors.append(f"Component '{name}' field 'vendored_copies' contains duplicates")
            for vendored_path in vendored:
                try:
                    normalized_vendored = _normalize_declared_path(vendored_path)
                except ValueError:
                    normalized_vendored = None
                if normalized_vendored is None or _is_glob(normalized_vendored):
                    errors.append(
                        f"Component '{name}' vendored copy must be a safe repo-relative "
                        f"path using '/' separators: {vendored_path!r}"
                    )
                elif component_path is not None and (
                    normalized_vendored == component_path
                    or normalized_vendored.startswith(component_path + "/")
                    or component_path.startswith(normalized_vendored + "/")
                ):
                    errors.append(
                        f"Component '{name}' vendored copy overlaps its source component: "
                        f"{normalized_vendored}"
                    )
                elif not _is_within(repo_root, repo_root / normalized_vendored):
                    errors.append(
                        f"Component '{name}' vendored copy must be a safe repo-relative "
                        f"path within the repository: {normalized_vendored}"
                    )
                elif source == "working-tree" and not (repo_root / normalized_vendored).exists():
                    errors.append(
                        f"Component '{name}' vendored copy not found: {normalized_vendored}"
                    )
        consumers = comp.get("consumers")
        if consumers is not None:
            if not _is_str_list(consumers):
                errors.append(f"Component '{name}' field 'consumers' must be an array of strings")
            else:
                if len(consumers) != len(set(consumers)):
                    errors.append(f"Component '{name}' field 'consumers' contains duplicates")
                for consumer in consumers:
                    if not consumer.strip() or consumer != consumer.strip():
                        errors.append(
                            f"Component '{name}' consumer identifiers must be non-empty "
                            f"and have no surrounding whitespace: {consumer!r}"
                        )
                        continue
                    if consumer == name:
                        errors.append(f"Component '{name}' cannot consume its own boundary")
                    elif consumer not in components:
                        errors.append(
                            f"Component '{name}' references unknown consumer: {consumer}"
                        )
        external_consumers = comp.get("external_consumers")
        if external_consumers is not None:
            if not _is_str_list(external_consumers):
                errors.append(
                    f"Component '{name}' field 'external_consumers' must be an "
                    "array of strings"
                )
            else:
                if len(external_consumers) != len(set(external_consumers)):
                    errors.append(
                        f"Component '{name}' field 'external_consumers' contains duplicates"
                    )
                for consumer in external_consumers:
                    if not consumer.strip() or consumer != consumer.strip():
                        errors.append(
                            f"Component '{name}' external consumer identifiers must "
                            "be non-empty and have no surrounding whitespace: "
                            f"{consumer!r}"
                        )
                    elif consumer in components:
                        errors.append(
                            f"Component '{name}' external consumer '{consumer}' is a "
                            "configured component; declare it in 'consumers' instead"
                        )
        component_verify_facets = comp.get("verify_facets")
        if component_verify_facets is not None:
            if (
                not _is_str_list(component_verify_facets)
                or not component_verify_facets
            ):
                errors.append(
                    f"Component '{name}' field 'verify_facets' must be a "
                    "non-empty array of strings"
                )
            else:
                if len(component_verify_facets) != len(set(component_verify_facets)):
                    errors.append(
                        f"Component '{name}' field 'verify_facets' contains duplicates"
                    )
                invalid_facets = sorted(
                    set(component_verify_facets) - supported_modes
                )
                if invalid_facets:
                    errors.append(
                        f"Component '{name}' field 'verify_facets' contains unknown "
                        f"facets: {', '.join(invalid_facets)}"
                    )

        # Reject explicit policies that can never produce their selected
        # facets. An omitted policy remains gradual-adoption shorthand for
        # "all facets that are available"; only an explicitly declared
        # component/default gate creates this static obligation.
        explicit_effective_facets: Optional[List[str]] = None
        if _is_str_list(component_verify_facets) and component_verify_facets:
            explicit_effective_facets = component_verify_facets
        elif "verify_facets" in defaults and _is_str_list(verify_facets) and verify_facets:
            explicit_effective_facets = verify_facets
        if explicit_effective_facets is not None:
            selected_facets = set(explicit_effective_facets) & supported_modes
            boundary_cfg = comp.get("boundary")
            boundary_kind = (
                boundary_provider_name(boundary_cfg)
                if isinstance(boundary_cfg, dict)
                else "implicit"
            )
            boundary_paths = (
                boundary_cfg.get("paths", [])
                if isinstance(boundary_cfg, dict)
                else []
            )
            behavior_cfg = comp.get("behavior")
            behavior_paths = (
                behavior_cfg.get("paths", [])
                if isinstance(behavior_cfg, dict)
                else []
            )
            if "compat" in selected_facets and not isinstance(
                comp.get("version_source"), dict
            ):
                errors.append(
                    f"Component '{name}' explicitly gates 'compat' but has no "
                    "version_source"
                )
            if "behavior" in selected_facets and not (
                _is_str_list(behavior_paths) and behavior_paths
            ):
                errors.append(
                    f"Component '{name}' explicitly gates 'behavior' but has no "
                    "behavior.paths"
                )
            if "boundary" in selected_facets and (
                boundary_kind == "leaf"
                or (
                    boundary_kind == "implicit"
                    and not (_is_str_list(boundary_paths) and boundary_paths)
                )
            ):
                errors.append(
                    f"Component '{name}' explicitly gates 'boundary' but provider "
                    f"'{boundary_kind}' has no boundary paths"
                )

        # Validate version_source — check file exists and has a supported extension.
        version_source = comp.get("version_source")
        if version_source is not None:
            if not isinstance(version_source, dict):
                errors.append(f"Component '{name}' field 'version_source' must be an object")
            elif "git_tag_prefix" in version_source:
                _reject_unknown_fields(
                    errors,
                    version_source,
                    {"git_tag_prefix"},
                    f"component '{name}' version_source",
                )
                if not isinstance(version_source["git_tag_prefix"], str) or not version_source["git_tag_prefix"].strip():
                    errors.append(f"Component '{name}' version_source.git_tag_prefix must be a non-empty string")
            elif "file" in version_source:
                _reject_unknown_fields(
                    errors,
                    version_source,
                    {"file", "field"},
                    f"component '{name}' version_source",
                )
                vs_file = version_source["file"]
                if not isinstance(vs_file, str) or not vs_file.strip():
                    errors.append(f"Component '{name}' version_source.file must be a non-empty string")
                else:
                    try:
                        normalized_vs_file = _normalize_declared_path(vs_file)
                    except ValueError:
                        normalized_vs_file = None
                    safe_vs_file = (
                        normalized_vs_file is not None
                        and not _is_glob(normalized_vs_file)
                    )
                    if not safe_vs_file:
                        errors.append(
                            f"Component '{name}' version_source.file must be a safe "
                            f"component-relative path using '/' separators: {vs_file!r}"
                        )
                        normalized_vs_file = vs_file
                    _supported_vs_exts = {".json", ".toml", ".yaml", ".yml"}
                    if Path(normalized_vs_file).suffix not in _supported_vs_exts:
                        errors.append(
                            f"Component '{name}' version_source.file has unsupported extension '{Path(normalized_vs_file).suffix}'"
                            f" — supported: {', '.join(sorted(_supported_vs_exts))}"
                        )
                    vs_full = (
                        repo_root / component_path / normalized_vs_file
                        if component_path is not None and safe_vs_file
                        else None
                    )
                    version_repo_path = (
                        f"{component_path.rstrip('/')}/{normalized_vs_file}"
                        if component_path is not None and safe_vs_file
                        else None
                    )
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
                    elif source == "working-tree" and vs_full is not None:
                        if tracking_snapshot_error is not None:
                            errors.append(
                                f"Component '{name}' version_source.file tracking "
                                f"state cannot be read: '{vs_file}': "
                                f"{tracking_snapshot_error}"
                            )
                        elif (
                            tracking_snapshot is not None
                            and version_repo_path not in tracking_snapshot.entries
                        ):
                            errors.append(
                                f"Component '{name}' version_source.file must be "
                                f"tracked in Git: '{vs_file}'"
                            )
                    elif (
                        source in {"head", "index"}
                        and version_repo_path is not None
                    ):
                        try:
                            if snapshot is not None:
                                selected_entry = snapshot.entries.get(version_repo_path)
                            else:
                                selected_files = _list_files_for_source(
                                    repo_root, version_repo_path, source
                                )
                                selected_entry = (
                                    True if version_repo_path in selected_files else None
                                )
                        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
                            errors.append(
                                f"Component '{name}' version_source.file cannot be "
                                f"read at {source}: '{vs_file}': {exc}"
                            )
                        else:
                            if selected_entry is None:
                                errors.append(
                                    f"Component '{name}' version_source.file not found "
                                    f"in captured {source} source: '{vs_file}'"
                                )
                            elif snapshot is not None and (
                                selected_entry.object_type != "blob"
                                or selected_entry.mode not in {"100644", "100755"}
                            ):
                                errors.append(
                                    f"Component '{name}' version_source.file must be "
                                    f"a regular file in captured {source} source: "
                                    f"'{vs_file}'"
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
                    elif version_source["field"] != version_source["field"].strip():
                        errors.append(
                            f"Component '{name}' version_source.field must not have surrounding whitespace"
                        )
            else:
                _reject_unknown_fields(
                    errors,
                    version_source,
                    {"file", "field", "git_tag_prefix"},
                    f"component '{name}' version_source",
                )
                errors.append(
                    f"Component '{name}' version_source must have either 'file' or 'git_tag_prefix'"
                )

    # The facet model requires exact ⊇ behavior ⊇ boundary. Validate the
    # selected tracked file sets against the same source view used by hashing;
    # the behavior envelope independently makes boundary changes observable.
    for name, comp in components.items():
        if not isinstance(comp, dict):
            continue
        boundary = comp.get("boundary")
        behavior = comp.get("behavior")
        component_path = comp.get("path")
        if (
            not isinstance(boundary, dict)
            or not isinstance(behavior, dict)
            or not isinstance(component_path, str)
        ):
            continue
        boundary_paths = boundary.get("paths", [])
        behavior_paths = behavior.get("paths", [])
        if not _is_str_list(boundary_paths) or not _is_str_list(behavior_paths):
            continue
        try:
            boundary_files = _expand_component_paths(
                repo_root,
                component_path,
                boundary_paths,
                source=source,
                snapshot=snapshot,
            )
            behavior_files = _expand_component_paths(
                repo_root,
                component_path,
                behavior_paths,
                source=source,
                snapshot=snapshot,
            )
        except GuardrailError as exc:
            errors.append(
                f"Component '{name}' path expansion could not be validated: {exc}"
            )
            continue
        uncovered = sorted(boundary_files - behavior_files)
        if uncovered:
            preview = ", ".join(uncovered[:3])
            if len(uncovered) > 3:
                preview += f", +{len(uncovered) - 3} more"
            errors.append(
                f"Component '{name}' behavior.paths must cover every boundary "
                f"artifact; uncovered: {preview}"
            )

    for sname, sdef in slices.items():
        if not isinstance(sname, str) or not sname.strip():
            errors.append("Slice names must be non-empty strings")
            continue
        if not isinstance(sdef, dict):
            errors.append(f"Slice '{sname}' must be an object")
            continue
        _reject_unknown_fields(
            errors,
            sdef,
            {"description", "mode", "components", "closure_of"},
            f"slice '{sname}'",
        )
        mode = sdef.get("mode", "exact")
        if not isinstance(mode, str) or mode not in supported_modes:
            errors.append(
                f"Slice '{sname}' has unknown mode: "
                f"{_bounded_diagnostic_text(mode)}"
            )
        has_components = "components" in sdef
        has_closure = "closure_of" in sdef
        if has_components == has_closure:
            errors.append(
                f"Slice '{sname}' must define exactly one of 'components' or 'closure_of'"
            )
            slice_components: List[str] = []
        elif has_components:
            raw_slice_components = sdef.get("components")
            if not _is_str_list(raw_slice_components):
                errors.append(
                    f"Slice '{sname}' field 'components' must be an array of strings"
                )
                slice_components = []
            else:
                slice_components = raw_slice_components
                if len(slice_components) != len(set(slice_components)):
                    errors.append(
                        f"Slice '{sname}' field 'components' contains duplicates"
                    )
        else:
            closure_seed = sdef.get("closure_of")
            if (
                not isinstance(closure_seed, str)
                or not closure_seed.strip()
                or closure_seed != closure_seed.strip()
            ):
                errors.append(
                    f"Slice '{sname}' field 'closure_of' must be a non-empty "
                    "component name with no surrounding whitespace"
                )
                slice_components = []
            elif closure_seed not in components:
                errors.append(
                    f"Slice '{sname}' closure_of references unknown component: "
                    f"{closure_seed}"
                )
                slice_components = []
            else:
                slice_components = resolve_slice_components(sdef, components)
        if "description" in sdef and not isinstance(sdef["description"], str):
            errors.append(f"Slice '{sname}' field 'description' must be a string")
        for cname in slice_components:
            if cname not in components:
                errors.append(f"Slice '{sname}' references unknown component: {cname}")

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
        if source == "working-tree":
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

        try:
            boundary_files = _expand_component_paths(
                repo_root, component_path, boundary_paths, source="working-tree"
            )
        except GuardrailError as exc:
            warnings.append(
                f"Component '{name}' behavior coverage could not be inspected: {exc}"
            )
            continue
        if not boundary_files:
            continue
        try:
            behavior_files = _expand_component_paths(
                repo_root, component_path, behavior_paths, source="working-tree"
            )
        except GuardrailError as exc:
            warnings.append(
                f"Component '{name}' behavior coverage could not be inspected: {exc}"
            )
            continue
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
                    max_entries=MAX_FILESYSTEM_TRAVERSAL_ENTRIES,
                    exceeded_message=(
                        "Component discovery guardrail exceeded: "
                        "filesystem traversal exceeds "
                        f"{MAX_FILESYSTEM_TRAVERSAL_ENTRIES} entries"
                    ),
                    should_descend=(
                        lambda directory: directory.name not in _ignored_dirs
                    ),
                )
                if path.name in manifest_names
            ),
            max_paths=MAX_DISCOVERY_MANIFESTS,
            exceeded_message=(
                "Component discovery guardrail exceeded: "
                f">{MAX_DISCOVERY_MANIFESTS} manifests"
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
                component_dir, available_paths=available_paths
            )
            version_source = (
                {"file": mf.name, "field": version_field}
                if version_field is not None and str(rel_dir) != "."
                else None
            )
            if len(found) >= MAX_DISCOVERED_COMPONENTS:
                raise GuardrailError(
                    "Component discovery guardrail exceeded: "
                    f">{MAX_DISCOVERED_COMPONENTS} components"
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
) -> tuple:
    """Detect the best boundary provider and paths for a component directory.

    Returns (provider_name, paths_list).
    """
    if available_paths is not None:
        # Discovery is index-backed whenever Git supplied the manifest list.
        # Restrict provider evidence to that same immutable name set so an
        # untracked, deleted, or concurrently-created working-tree artifact
        # cannot change the generated declaration.
        if len(available_paths) > MAX_PROVIDER_DETECTION_ENTRIES:
            raise GuardrailError(
                "Provider detection guardrail exceeded: "
                f">{MAX_PROVIDER_DETECTION_ENTRIES} available paths"
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
                max_entries=MAX_PROVIDER_DETECTION_ENTRIES,
                exceeded_message=(
                    "Provider detection guardrail exceeded: "
                    f">{MAX_PROVIDER_DETECTION_ENTRIES} directory entries"
                ),
            )
            if component_dir.exists()
            else ()
        ),
        max_paths=MAX_PROVIDER_DETECTION_ENTRIES,
        exceeded_message=(
            "Provider detection guardrail exceeded: "
            f">{MAX_PROVIDER_DETECTION_ENTRIES} directory entries"
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
