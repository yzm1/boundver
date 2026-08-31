"""Config validation and component discovery for boundver."""

import subprocess
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ._git import (
    GitSourceSnapshot,
    _capture_git_source_snapshot,
    _is_git_repository,
    _list_files_for_source,
    _snapshot_files,
    _to_posix,
)
from ._hashing import _read_bounded_path_bytes
from ._utils import (
    BoundedDiagnosticList,
    DIAGNOSTIC_TRUNCATION_SENTINEL,
    FACET_SET,
    SOURCE_MODE_SET,
    _available_component_facets,
    _bounded_diagnostic_list_preview,
    _bounded_diagnostic_repr,
    _bounded_diagnostic_text,
    _bounded_json_dumps,
    _bounded_sorted_paths,
    _is_glob,
    _is_within,
    _iter_bounded_filesystem_paths,
    _iter_bounded_json_values,
    _normalize_declared_path,
    _PathGlobOperation,
    boundary_provider_name,
    ConfigError as ConfigError,
    GuardrailError,
)
from .providers import (
    MAX_CUSTOM_PROVIDERS,
    MAX_PROVIDER_DECLARATIONS,
    create_registry,
    get_provider,
    load_custom_providers,
    validate_provider_config,
    validate_provider_environment,
)
from ._consumer_graph import empty_explicit_slice_error, resolve_slice_components
from ._structured_data import strict_json_loads
from ._config_contract import (
    BEHAVIOR_FIELDS,
    BOUNDARY_FIELDS,
    COMPONENT_FIELDS,
    DEFAULT_FIELDS,
    git_tag_prefix_error,
    MAX_CONSUMER_GRAPH_ITEMS,
    MAX_CONSUMER_IDENTIFIER_CHARS,
    PROVIDER_FIELDS,
    ROOT_FIELDS,
    SLICE_FIELDS,
    VERSION_FILE_FIELDS,
    VERSION_SOURCE_FIELDS,
    VERSION_TAG_FIELDS,
    component_identifier_problem,
)
from ._config_io import (
    find_config_file as _find_config_file_impl,
    json_value_issues as _json_value_issues_impl,
    load_config_file as _load_config_file_impl,
    parse_config_bytes as _parse_config_bytes_impl,
    parse_config_text as _parse_config_text_impl,
    snapshot_relative_path as _snapshot_relative_path_impl,
)
from ._discovery import (
    _detect_provider as _detect_provider_impl,
    discover_components as _discover_components_impl,
)

MAX_CONFIG_BYTES = 10 * 1024 * 1024
MAX_COMPONENT_EXPANSION_FILES = 50_000
MAX_DISCOVERY_MANIFESTS = 50_000
MAX_DISCOVERED_COMPONENTS = 1_000
MAX_PROVIDER_DETECTION_ENTRIES = 50_000
MAX_FILESYSTEM_TRAVERSAL_ENTRIES = 200_000


def _snapshot_relative_path(repo_root: Path, path: Path) -> str:
    """Compatibility wrapper for source-backed path normalization."""
    return _snapshot_relative_path_impl(repo_root, path)


def _json_value_issues(value: Any, *, path: str = "config") -> List[str]:
    """Compatibility wrapper for deterministic JSON-value validation."""
    return _json_value_issues_impl(value, path=path)


def parse_config_text(text: str, path: Path) -> dict:
    """Compatibility wrapper for the config parser subsystem."""
    return _parse_config_text_impl(text, path)


def parse_config_bytes(data: bytes, path: Path) -> dict:
    """Compatibility wrapper honoring the patchable public size limit."""
    return _parse_config_bytes_impl(data, path, max_bytes=MAX_CONFIG_BYTES)


def find_config_file(
    repo_root: Path,
    hint: str = "boundary.config.json",
    *,
    snapshot: Optional[GitSourceSnapshot] = None,
) -> Path:
    """Compatibility wrapper for source-aware config discovery."""
    return _find_config_file_impl(repo_root, hint, snapshot=snapshot)


def load_config_file(
    path: Path,
    *,
    repo_root: Optional[Path] = None,
    snapshot: Optional[GitSourceSnapshot] = None,
) -> dict:
    """Compatibility wrapper honoring the patchable public size limit."""
    return _load_config_file_impl(
        path,
        max_bytes=MAX_CONFIG_BYTES,
        repo_root=repo_root,
        snapshot=snapshot,
    )


def dump_config(value: dict) -> str:
    """Render one config under the same UTF-8 limit accepted by its loader."""
    if MAX_CONFIG_BYTES < 1:  # pragma: no cover - production invariant
        raise ConfigError("Config storage limit must leave room for a newline")
    try:
        body = _bounded_json_dumps(
            value,
            indent=2,
            max_bytes=MAX_CONFIG_BYTES - 1,
        )
    except GuardrailError as exc:
        raise ConfigError(
            "Config output exceeds the "
            f"{MAX_CONFIG_BYTES}-byte storage limit; no file was written. "
            "Reduce component, slice, or provider declarations before retrying."
        ) from exc
    return body + "\n"



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
        return strict_json_loads(bundled)
    except (FileNotFoundError, OSError, ValueError):
        schema_path = repo_root / "boundary.config.schema.json"
        if schema_path.exists():
            try:
                schema_data = _read_bounded_path_bytes(
                    schema_path,
                    str(schema_path),
                    max_bytes=MAX_CONFIG_BYTES,
                )
                return strict_json_loads(schema_data.decode("utf-8"))
            except (
                GuardrailError,
                OSError,
                UnicodeDecodeError,
                ValueError,
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
        if isinstance(errors, BoundedDiagnosticList) and errors.truncated:
            break
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
    component_name_display = _bounded_diagnostic_text(component_name)
    component_path_display = _bounded_diagnostic_text(component_path)
    for rel in paths:
        if isinstance(errors, BoundedDiagnosticList) and errors.truncated:
            break
        rel_display = _bounded_diagnostic_repr(rel)
        try:
            normalized = _normalize_declared_path(rel)
        except ValueError as exc:
            errors.append(
                f"Component '{component_name_display}' {field_name} path "
                f"{rel_display} {_bounded_diagnostic_text(str(exc))}"
            )
            continue
        if _is_glob(normalized):
            continue

        full = component_root / normalized
        normalized_display = _bounded_diagnostic_text(normalized)
        if not _is_within(component_root, full):
            errors.append(
                f"Component '{component_name_display}' {field_name} path escapes "
                f"component root: {normalized_display}"
            )
            continue
        if not _is_within(repo_root, full):
            errors.append(
                f"Component '{component_name_display}' {field_name} path escapes "
                f"repository root: {normalized_display}"
            )
            continue
        if check_exists and not full.exists():
            errors.append(
                f"Component '{component_name_display}' {field_name} path not "
                f"found: {component_path_display}/{normalized_display}"
                f" — ensure the file exists before running generate"
            )


def _expand_component_paths(
    repo_root: Path,
    component_path: Optional[str],
    paths: List[str],
    source: Optional[str] = None,
    snapshot: Optional[GitSourceSnapshot] = None,
    _glob_operation: Optional[_PathGlobOperation] = None,
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
    glob_operation = _glob_operation or _PathGlobOperation(
        "Component path expansion"
    )

    for rel in paths:
        try:
            rel_norm = _normalize_declared_path(rel)
        except ValueError:
            continue
        is_dir_like = rel.endswith("/")
        if _is_glob(rel_norm):
            glob_operation.prepare(rel_norm)
            for file_rel in all_files:
                if glob_operation.matches(file_rel, rel_norm):
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
        from referencing import Registry  # type: ignore
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
        # An explicit empty registry resolves same-document fragments but has
        # no retrieval callback.  Validation must never turn a schema $ref
        # into an implicit network request.
        validator = jsonschema.Draft202012Validator(schema, registry=Registry())
    except Exception as exc:  # pragma: no cover - defensive path
        try:
            detail = str(exc)
        except Exception:
            detail = exc.__class__.__name__
        return [
            "Schema validator initialization failed: "
            f"{_bounded_diagnostic_text(detail)}"
        ]

    errors = BoundedDiagnosticList()
    try:
        for err in validator.iter_errors(schema_instance):
            path = ".".join(
                _bounded_diagnostic_text(part, max_chars=128)
                for part in err.path
            ) or "<root>"
            path = _bounded_diagnostic_text(path)
            message = _bounded_diagnostic_text(err.message)
            errors.append(f"Schema validation error at {path}: {message}")
            if errors.truncated:
                break
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
    if errors.truncated:
        return sorted(errors[:-1]) + [DIAGNOSTIC_TRUNCATION_SENTINEL]
    return sorted(errors)


def validate_config(
    config: dict,
    repo_root: Path,
    allow_custom_providers: bool = False,
    source: str = "working-tree",
    snapshot: Optional[GitSourceSnapshot] = None,
    require_slice_facets: bool = False,
) -> List[str]:
    """Validate a configuration without performing digest computation.

    ``require_slice_facets`` applies strict-generation slice availability
    rules.  Its backward-compatible default permits intentional null slice
    inputs for callers that will generate with ``strict=False``.
    """
    errors = BoundedDiagnosticList()
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
        bounded_json_issues = BoundedDiagnosticList(
            "Config contains values that cannot be represented as deterministic JSON: "
            + _bounded_diagnostic_text(issue)
            for issue in json_issues
        )
        return list(bounded_json_issues)

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
    if tracking_snapshot_error is not None:
        errors.append(
            "Working-tree Git tracking state cannot be read: "
            f"{_bounded_diagnostic_text(tracking_snapshot_error)}"
        )

    schema = _load_config_schema(repo_root)
    errors.extend(_schema_engine_errors(config, schema))
    if errors.truncated:
        return list(errors)
    _reject_unknown_fields(
        errors,
        config,
        ROOT_FIELDS,
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
        _reject_unknown_fields(errors, defaults, DEFAULT_FIELDS, "defaults")
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
                    + _bounded_diagnostic_list_preview(invalid)
                )

    components = config.get("components", {})
    slices = config.get("slices", {})
    if not isinstance(components, dict):
        errors.append("Field 'components' must be an object")
        components = {}
    elif not components:
        errors.append("Field 'components' must define at least one component")
    elif len(components) > MAX_CONSUMER_GRAPH_ITEMS:
        errors.append(
            "Field 'components' exceeds the "
            f"{MAX_CONSUMER_GRAPH_ITEMS}-component consumer-graph limit"
        )
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
    declared_custom_names_complete = False
    if providers_list is not None:
        if not isinstance(providers_list, list):
            errors.append("Top-level 'providers' must be an array")
        elif len(providers_list) > MAX_CUSTOM_PROVIDERS:
            errors.append(
                "Top-level 'providers' exceeds the "
                f"{MAX_CUSTOM_PROVIDERS}-provider limit"
            )
        else:
            # An explicit configured name constrains the provider's runtime
            # name. The collected set is exhaustive only if every declaration
            # supplies a valid explicit name; otherwise trusted loading must
            # resolve anonymous declarations before references can be checked.
            declared_custom_names_complete = True
            for i, entry in enumerate(providers_list):
                if errors.truncated:
                    break
                if not isinstance(entry, dict):
                    errors.append(f"providers[{i}] must be an object")
                    declared_custom_names_complete = False
                    continue
                _reject_unknown_fields(errors, entry, PROVIDER_FIELDS, f"providers[{i}]")
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
                        f"providers[{i}] class name "
                        f"'{_bounded_diagnostic_text(cls_val.strip())}' is not a "
                        "valid Python identifier"
                    )
                # Track explicit expected names for static cross-reference.
                # Without one, the provider instance determines its registered
                # custom.* identifier after trusted loading.
                raw_declared_name = entry.get("name")
                declared_name = (
                    raw_declared_name.strip()
                    if isinstance(raw_declared_name, str)
                    else ""
                )
                if raw_declared_name is None:
                    declared_custom_names_complete = False
                else:
                    if not isinstance(raw_declared_name, str) or not declared_name:
                        declared_custom_names_complete = False
                        errors.append(
                            f"providers[{i}] field 'name' must be a non-empty string"
                        )
                    elif raw_declared_name != declared_name:
                        declared_custom_names_complete = False
                        errors.append(
                            f"providers[{i}] field 'name' must not have surrounding whitespace"
                        )
                    elif not declared_name.startswith("custom.") or declared_name == "custom.":
                        declared_custom_names_complete = False
                        errors.append(
                            f"providers[{i}] field 'name' must start with 'custom.' "
                            "and include a name after the prefix "
                            f"to avoid collisions with built-in providers "
                            f"(got '{_bounded_diagnostic_text(declared_name)}', try "
                            f"'custom.{_bounded_diagnostic_text(declared_name)}')"
                        )
                    else:
                        declared_custom_names.add(declared_name)

    if errors.truncated:
        return list(errors)

    all_external_consumers: Set[str] = set()
    for name, comp in components.items():
        if errors.truncated:
            break
        name_problem = component_identifier_problem(
            name,
            max_chars=MAX_CONSUMER_IDENTIFIER_CHARS,
        )
        if not isinstance(name, str) or not name.strip():
            errors.append("Component names must be non-empty strings")
            continue
        if len(name) > MAX_CONSUMER_IDENTIFIER_CHARS:
            errors.append(
                "Component name exceeds the "
                f"{MAX_CONSUMER_IDENTIFIER_CHARS}-character consumer-graph "
                f"limit: {_bounded_diagnostic_repr(name)}"
            )
            continue
        if name_problem is not None:
            errors.append(
                f"Component name {_bounded_diagnostic_repr(name)} is not "
                f"addressable: {name_problem}. Rename it and update every "
                "consumer edge, slice reference, and lockfile entry."
            )
            continue
        raw_component_name = name
        name = _bounded_diagnostic_text(name)
        if not isinstance(comp, dict):
            errors.append(f"Component '{name}' must be an object")
            continue
        _reject_unknown_fields(
            errors,
            comp,
            COMPONENT_FIELDS,
            f"component '{name}'",
        )
        if "ecosystem" in comp and not isinstance(comp["ecosystem"], str):
            errors.append(f"Component '{name}' field 'ecosystem' must be a string")
        if "note" in comp and not isinstance(comp["note"], str):
            errors.append(f"Component '{name}' field 'note' must be a string")
        if "path" not in comp:
            errors.append(f"Component '{name}' missing required field: path")
            continue
        raw_component_path = comp.get("path")
        raw_component_path_display = _bounded_diagnostic_repr(raw_component_path)
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
                    errors.append(
                        f"Component '{name}' path {raw_component_path_display} "
                        f"{_bounded_diagnostic_text(str(exc))}"
                    )
                    normalized_path = None
            if normalized_path is not None and _is_glob(normalized_path):
                errors.append(
                    f"Component '{name}' path must be a literal directory, not a glob: "
                    f"{_bounded_diagnostic_text(normalized_path)}"
                )
                normalized_path = None
            if normalized_path is not None:
                component_path = normalized_path
            if component_path is not None and source == "working-tree" and (repo_root / component_path).is_symlink():
                errors.append(
                    f"Component '{name}' path must not be a symlink: "
                    f"{_bounded_diagnostic_text(component_path)}"
                )
            elif component_path is not None and source == "working-tree" and not _is_within(repo_root, repo_root / component_path):
                errors.append(
                    f"Component '{name}' path escapes the repository: "
                    f"{_bounded_diagnostic_text(component_path)}"
                )
            elif component_path is not None and source == "working-tree" and not (repo_root / component_path).is_dir():
                errors.append(
                    f"Component '{name}' path not found or not a directory: "
                    f"{_bounded_diagnostic_text(component_path)}"
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
                        f"Component '{name}' path cannot be read at {source}: "
                        f"{_bounded_diagnostic_text(str(exc))}"
                    )
                else:
                    directory_prefix = component_path.rstrip("/") + "/"
                    if not any(
                        selected.startswith(directory_prefix)
                        for selected in selected_files
                    ):
                        errors.append(
                            f"Component '{name}' path is not a tracked directory "
                            f"at {source}: {_bounded_diagnostic_text(component_path)}"
                        )
            if component_path is not None and component_path in seen_component_paths:
                other = seen_component_paths[component_path]
                errors.append(
                    "Duplicate component path "
                    f"'{_bounded_diagnostic_text(component_path)}' used by "
                    f"'{_bounded_diagnostic_text(other)}' and '{name}'"
                )
            elif component_path is not None:
                seen_component_paths[component_path] = raw_component_name
        boundary = comp.get("boundary", {})
        if not isinstance(boundary, dict):
            errors.append(f"Component '{name}' boundary must be an object")
            continue
        _reject_unknown_fields(
            errors,
            boundary,
            BOUNDARY_FIELDS,
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
                f"Component '{name}' has unsupported boundary.provider "
                f"'{_bounded_diagnostic_text(boundary['provider'])}' "
                "(use a known provider or custom.* namespace)"
            )
        elif boundary["provider"] == "custom.":
            errors.append(
                f"Component '{name}' boundary.provider must include a name after "
                "the 'custom.' prefix"
            )
        elif boundary["provider"].startswith("custom."):
            # Verify the custom provider is declared in the top-level providers list.
            provider_name = boundary["provider"]
            if providers_list is None:
                errors.append(
                    f"Component '{name}' uses "
                    f"'{_bounded_diagnostic_text(provider_name)}' but no 'providers' list is "
                    "declared in the config — add a top-level 'providers' array"
                )
            elif (
                declared_custom_names_complete
                and provider_name not in declared_custom_names
            ):
                errors.append(
                    f"Component '{name}' uses "
                    f"'{_bounded_diagnostic_text(provider_name)}' which is not declared in the "
                    "'providers' list — add an entry with \"name\": "
                    f"\"{_bounded_diagnostic_text(provider_name)}\""
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
        if errors.truncated:
            break

        behavior = comp.get("behavior")
        if behavior is not None:
            if not isinstance(behavior, dict):
                errors.append(f"Component '{name}' behavior must be an object")
            else:
                _reject_unknown_fields(
                    errors,
                    behavior,
                    BEHAVIOR_FIELDS,
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
                if errors.truncated:
                    break
        if errors.truncated:
            break
        vendored = comp.get("vendored_copies")
        if vendored is not None and not _is_str_list(vendored):
            errors.append(f"Component '{name}' field 'vendored_copies' must be an array of strings")
        elif vendored is not None:
            if len(vendored) != len(set(vendored)):
                errors.append(f"Component '{name}' field 'vendored_copies' contains duplicates")
            for vendored_path in vendored:
                if errors.truncated:
                    break
                try:
                    normalized_vendored = _normalize_declared_path(vendored_path)
                except ValueError:
                    normalized_vendored = None
                if normalized_vendored is None or _is_glob(normalized_vendored):
                    errors.append(
                        f"Component '{name}' vendored copy must be a safe repo-relative "
                        "path using '/' separators: "
                        f"{_bounded_diagnostic_repr(vendored_path)}"
                    )
                elif component_path is not None and (
                    normalized_vendored == component_path
                    or normalized_vendored.startswith(component_path + "/")
                    or component_path.startswith(normalized_vendored + "/")
                ):
                    errors.append(
                        f"Component '{name}' vendored copy overlaps its source component: "
                        f"{_bounded_diagnostic_text(normalized_vendored)}"
                    )
                elif not _is_within(repo_root, repo_root / normalized_vendored):
                    errors.append(
                        f"Component '{name}' vendored copy must be a safe repo-relative "
                        "path within the repository: "
                        f"{_bounded_diagnostic_text(normalized_vendored)}"
                    )
                elif source == "working-tree" and not (repo_root / normalized_vendored).exists():
                    errors.append(
                        f"Component '{name}' vendored copy not found: "
                        f"{_bounded_diagnostic_text(normalized_vendored)}"
                    )
        if errors.truncated:
            break
        consumers = comp.get("consumers")
        if consumers is not None:
            if not _is_str_list(consumers):
                errors.append(f"Component '{name}' field 'consumers' must be an array of strings")
            else:
                if len(consumers) > MAX_CONSUMER_GRAPH_ITEMS:
                    errors.append(
                        f"Component '{name}' field 'consumers' exceeds the "
                        f"{MAX_CONSUMER_GRAPH_ITEMS}-entry consumer-graph limit"
                    )
                if len(consumers) != len(set(consumers)):
                    errors.append(f"Component '{name}' field 'consumers' contains duplicates")
                for consumer in consumers:
                    if errors.truncated:
                        break
                    consumer_problem = component_identifier_problem(
                        consumer,
                        max_chars=MAX_CONSUMER_IDENTIFIER_CHARS,
                    )
                    if len(consumer) > MAX_CONSUMER_IDENTIFIER_CHARS:
                        errors.append(
                            f"Component '{name}' consumer identifier exceeds the "
                            f"{MAX_CONSUMER_IDENTIFIER_CHARS}-character limit: "
                            f"{_bounded_diagnostic_repr(consumer)}"
                        )
                        continue
                    if not consumer.strip() or consumer != consumer.strip():
                        errors.append(
                            f"Component '{name}' consumer identifiers must be non-empty "
                            "and have no surrounding whitespace: "
                            f"{_bounded_diagnostic_repr(consumer)}"
                        )
                        continue
                    if consumer_problem is not None:
                        errors.append(
                            f"Component '{name}' consumer identifier "
                            f"{_bounded_diagnostic_repr(consumer)} is not "
                            f"addressable: {consumer_problem}. Rename the "
                            "referenced component and update this edge."
                        )
                        continue
                    if consumer == raw_component_name:
                        errors.append(f"Component '{name}' cannot consume its own boundary")
                    elif consumer not in components:
                        errors.append(
                            f"Component '{name}' references unknown consumer: "
                            f"{_bounded_diagnostic_text(consumer)}"
                        )
        if errors.truncated:
            break
        external_consumers = comp.get("external_consumers")
        if external_consumers is not None:
            if not _is_str_list(external_consumers):
                errors.append(
                    f"Component '{name}' field 'external_consumers' must be an "
                    "array of strings"
                )
            else:
                if len(external_consumers) > MAX_CONSUMER_GRAPH_ITEMS:
                    errors.append(
                        f"Component '{name}' field 'external_consumers' exceeds "
                        f"the {MAX_CONSUMER_GRAPH_ITEMS}-entry consumer-graph limit"
                    )
                if len(external_consumers) != len(set(external_consumers)):
                    errors.append(
                        f"Component '{name}' field 'external_consumers' contains duplicates"
                    )
                for consumer in external_consumers:
                    if errors.truncated:
                        break
                    if len(consumer) > MAX_CONSUMER_IDENTIFIER_CHARS:
                        errors.append(
                            f"Component '{name}' external consumer identifier "
                            f"exceeds the {MAX_CONSUMER_IDENTIFIER_CHARS}-character "
                            f"limit: {_bounded_diagnostic_repr(consumer)}"
                        )
                        continue
                    if not consumer.strip() or consumer != consumer.strip():
                        errors.append(
                            f"Component '{name}' external consumer identifiers must "
                            "be non-empty and have no surrounding whitespace: "
                            f"{_bounded_diagnostic_repr(consumer)}"
                        )
                    elif consumer in components:
                        errors.append(
                            f"Component '{name}' external consumer "
                            f"'{_bounded_diagnostic_text(consumer)}' is a "
                            "configured component; declare it in 'consumers' instead"
                        )
                    else:
                        all_external_consumers.add(consumer)
        if errors.truncated:
            break
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
                        "facets: "
                        + _bounded_diagnostic_list_preview(invalid_facets)
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
                    f"'{_bounded_diagnostic_text(boundary_kind)}' has no boundary paths"
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
                    VERSION_TAG_FIELDS,
                    f"component '{name}' version_source",
                )
                prefix_error = git_tag_prefix_error(
                    version_source["git_tag_prefix"]
                )
                if prefix_error is not None:
                    errors.append(
                        f"Component '{name}' version_source.git_tag_prefix "
                        f"{prefix_error}"
                    )
            elif "file" in version_source:
                _reject_unknown_fields(
                    errors,
                    version_source,
                    VERSION_FILE_FIELDS,
                    f"component '{name}' version_source",
                )
                vs_file = version_source["file"]
                vs_file_display = _bounded_diagnostic_repr(vs_file)
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
                            "component-relative path using '/' separators: "
                            f"{vs_file_display}"
                        )
                        normalized_vs_file = vs_file
                    _supported_vs_exts = {".json", ".toml", ".yaml", ".yml"}
                    if Path(normalized_vs_file).suffix not in _supported_vs_exts:
                        errors.append(
                            f"Component '{name}' version_source.file has unsupported "
                            "extension "
                            f"'{_bounded_diagnostic_text(Path(normalized_vs_file).suffix)}'"
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
                            f"{vs_file_display}"
                        )
                    elif source == "working-tree" and vs_full is not None and not _is_within(repo_root, vs_full):
                        errors.append(
                            f"Component '{name}' version_source.file escapes the repository: "
                            f"{vs_file_display}"
                        )
                    if source == "working-tree" and vs_full is not None and not vs_full.exists():
                        errors.append(
                            f"Component '{name}' version_source.file not found: "
                            f"{vs_file_display} (looked for "
                            f"{_bounded_diagnostic_text(component_path)}/"
                            f"{_bounded_diagnostic_text(vs_file)})"
                        )
                    elif source == "working-tree" and vs_full is not None:
                        if tracking_snapshot_error is not None:
                            # The one top-level tracking diagnostic above
                            # already makes validation fail closed.
                            pass
                        elif (
                            tracking_snapshot is not None
                            and version_repo_path not in tracking_snapshot.entries
                        ):
                            errors.append(
                                f"Component '{name}' version_source.file must be "
                                f"tracked in Git: {vs_file_display}"
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
                                f"read at {source}: {vs_file_display}: "
                                f"{_bounded_diagnostic_text(str(exc))}"
                            )
                        else:
                            if selected_entry is None:
                                errors.append(
                                    f"Component '{name}' version_source.file not found "
                                    f"in captured {source} source: {vs_file_display}"
                                )
                            elif snapshot is not None and (
                                selected_entry.object_type != "blob"
                                or selected_entry.mode not in {"100644", "100755"}
                            ):
                                errors.append(
                                    f"Component '{name}' version_source.file must be "
                                    f"a regular file in captured {source} source: "
                                    f"{vs_file_display}"
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
                    VERSION_SOURCE_FIELDS,
                    f"component '{name}' version_source",
                )
                errors.append(
                    f"Component '{name}' version_source must have either 'file' or 'git_tag_prefix'"
                )

    if errors.truncated:
        return list(errors)

    if len(all_external_consumers) > MAX_CONSUMER_GRAPH_ITEMS:
        errors.append(
            "Config declares more than "
            f"{MAX_CONSUMER_GRAPH_ITEMS} distinct external consumer labels; "
            "the repository-wide consumer graph must fit the machine-output "
            "contract"
        )

    # The facet model requires exact ⊇ behavior ⊇ boundary. Validate the
    # selected tracked file sets against the same source view used by hashing;
    # the behavior envelope independently makes boundary changes observable.
    for name, comp in components.items():
        if errors.truncated:
            break
        if not isinstance(comp, dict):
            continue
        name = _bounded_diagnostic_text(name)
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
        glob_operation = _PathGlobOperation("Component path expansion")
        try:
            boundary_files = _expand_component_paths(
                repo_root,
                component_path,
                boundary_paths,
                source=source,
                snapshot=snapshot,
                _glob_operation=glob_operation,
            )
            behavior_files = _expand_component_paths(
                repo_root,
                component_path,
                behavior_paths,
                source=source,
                snapshot=snapshot,
                _glob_operation=glob_operation,
            )
        except GuardrailError as exc:
            errors.append(
                f"Component '{name}' path expansion could not be validated: "
                f"{_bounded_diagnostic_text(str(exc))}"
            )
            continue
        uncovered = sorted(boundary_files - behavior_files)
        if uncovered:
            preview = ", ".join(
                _bounded_diagnostic_text(path) for path in uncovered[:3]
            )
            if len(uncovered) > 3:
                preview += f", +{len(uncovered) - 3} more"
            errors.append(
                f"Component '{name}' behavior.paths must cover every boundary "
                f"artifact; uncovered: {preview}"
            )

    for sname, sdef in slices.items():
        if errors.truncated:
            break
        if not isinstance(sname, str) or not sname.strip():
            errors.append("Slice names must be non-empty strings")
            continue
        sname = _bounded_diagnostic_text(sname)
        if not isinstance(sdef, dict):
            errors.append(f"Slice '{sname}' must be an object")
            continue
        empty_slice_error = empty_explicit_slice_error(sname, sdef)
        if empty_slice_error is not None:
            errors.append(empty_slice_error)
        _reject_unknown_fields(
            errors,
            sdef,
            SLICE_FIELDS,
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
                slice_components = []
                if len(raw_slice_components) != len(set(raw_slice_components)):
                    errors.append(
                        f"Slice '{sname}' field 'components' contains duplicates"
                    )
                for cname in raw_slice_components:
                    cname_problem = component_identifier_problem(
                        cname,
                        max_chars=MAX_CONSUMER_IDENTIFIER_CHARS,
                    )
                    if cname_problem is not None:
                        errors.append(
                            f"Slice '{sname}' component identifier "
                            f"{_bounded_diagnostic_repr(cname)} is not "
                            f"addressable: {cname_problem}. Rename the "
                            "component and update this slice."
                        )
                        continue
                    slice_components.append(cname)
        else:
            closure_seed = sdef.get("closure_of")
            closure_problem = component_identifier_problem(
                closure_seed,
                max_chars=MAX_CONSUMER_IDENTIFIER_CHARS,
            )
            if closure_problem is not None:
                errors.append(
                    f"Slice '{sname}' field 'closure_of' is not an addressable "
                    f"component identifier: {closure_problem}. Rename the "
                    "component and update this slice."
                )
                slice_components = []
            elif closure_seed not in components:
                errors.append(
                    f"Slice '{sname}' closure_of references unknown component: "
                    f"{_bounded_diagnostic_text(closure_seed)}"
                )
                slice_components = []
            else:
                slice_components = resolve_slice_components(sdef, components)
        if "description" in sdef and not isinstance(sdef["description"], str):
            errors.append(f"Slice '{sname}' field 'description' must be a string")
        for cname in slice_components:
            if errors.truncated:
                break
            if cname not in components:
                errors.append(
                    f"Slice '{sname}' references unknown component: "
                    f"{_bounded_diagnostic_text(cname)}"
                )
            elif (
                require_slice_facets
                and isinstance(mode, str)
                and mode in supported_modes
                and mode != "exact"
            ):
                component = components[cname]
                if (
                    isinstance(component, dict)
                    and mode not in _available_component_facets(component)
                ):
                    if mode == "boundary":
                        boundary = component.get("boundary")
                        provider = (
                            boundary_provider_name(boundary)
                            if isinstance(boundary, dict)
                            else "unknown"
                        )
                        detail = (
                            "provider "
                            f"'{_bounded_diagnostic_text(provider)}' does not "
                            "produce a boundary "
                            "digest from this declaration"
                        )
                    elif mode == "behavior":
                        detail = "the component has no non-empty behavior.paths"
                    else:
                        detail = "the component has no version_source"
                    errors.append(
                        f"Slice '{sname}' mode '{mode}' requires {mode} digest "
                        f"from component '{_bounded_diagnostic_text(cname)}' to "
                        f"supply that facet, but {_bounded_diagnostic_text(detail)}"
                    )

    if errors.truncated:
        return list(errors)

    provider_load_errors = load_custom_providers(
        config.get("providers", []),
        allow_custom=allow_custom_providers,
        registry=registry,
    )
    if allow_custom_providers:
        errors.extend(provider_load_errors)
    if errors.truncated:
        return list(errors)
    for name, comp in components.items():
        if errors.truncated:
            break
        if component_identifier_problem(
            name,
            max_chars=MAX_CONSUMER_IDENTIFIER_CHARS,
        ) is not None:
            continue
        name = _bounded_diagnostic_text(name)
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
                    f"Component '{name}' provider "
                    f"'{_bounded_diagnostic_text(provider_name)}' was not registered "
                    "by the configured provider module"
                )
            continue
        provider_paths = boundary.get("paths", [])
        if (
            provider_name in known_providers
            and provider_name not in {"implicit", "leaf"}
            and _is_str_list(provider_paths)
            and not provider_paths
        ):
            errors.append(
                f"Component '{name}': No boundary paths declared for "
                "explicit boundary provider"
            )
            continue
        for provider_error in validate_provider_environment(provider, boundary):
            errors.append(
                f"Component '{name}': {_bounded_diagnostic_text(provider_error)}"
            )
            if errors.truncated:
                break
        if source == "working-tree":
            for provider_error in validate_provider_config(
                provider, boundary, component_path, repo_root
            ):
                errors.append(
                    f"Component '{name}': {_bounded_diagnostic_text(provider_error)}"
                )
                if errors.truncated:
                    break

    return list(errors)


def config_warnings(config: dict, repo_root: Path) -> List[str]:
    warnings = BoundedDiagnosticList()
    if not isinstance(config, dict):
        return list(warnings)

    components = config.get("components", {})
    if not isinstance(components, dict):
        return list(warnings)

    for name, comp in components.items():
        if warnings.truncated:
            break
        if not isinstance(comp, dict):
            continue
        name = _bounded_diagnostic_text(name)
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

        glob_operation = _PathGlobOperation("Component path expansion")
        try:
            boundary_files = _expand_component_paths(
                repo_root,
                component_path,
                boundary_paths,
                source="working-tree",
                _glob_operation=glob_operation,
            )
        except GuardrailError as exc:
            warnings.append(
                f"Component '{name}' behavior coverage could not be inspected: "
                f"{_bounded_diagnostic_text(str(exc))}"
            )
            continue
        if not boundary_files:
            continue
        try:
            behavior_files = _expand_component_paths(
                repo_root,
                component_path,
                behavior_paths,
                source="working-tree",
                _glob_operation=glob_operation,
            )
        except GuardrailError as exc:
            warnings.append(
                f"Component '{name}' behavior coverage could not be inspected: "
                f"{_bounded_diagnostic_text(str(exc))}"
            )
            continue
        uncovered = sorted(boundary_files - behavior_files)
        if uncovered:
            preview = ", ".join(
                _bounded_diagnostic_text(path) for path in uncovered[:3]
            )
            if len(uncovered) > 3:
                preview += f", +{len(uncovered) - 3} more"
            warnings.append(
                f"Component '{name}' behavior.paths does not currently cover boundary files: {preview}"
                " — behavior should usually be a superset of boundary"
            )

    return list(warnings)


def discover_components(
    repo_root: Path,
    *,
    excluded_paths: Optional[List[str]] = None,
) -> Dict[str, dict]:
    """Compatibility wrapper honoring patchable discovery guardrails."""
    return _discover_components_impl(
        repo_root,
        excluded_paths=excluded_paths,
        max_discovery_manifests=MAX_DISCOVERY_MANIFESTS,
        max_discovered_components=MAX_DISCOVERED_COMPONENTS,
        max_provider_detection_entries=MAX_PROVIDER_DETECTION_ENTRIES,
        max_filesystem_traversal_entries=MAX_FILESYSTEM_TRAVERSAL_ENTRIES,
    )


def _detect_provider(
    component_dir: Path,
    *,
    available_paths: Optional[Set[str]] = None,
) -> tuple:
    """Compatibility wrapper honoring the patchable provider-entry limit."""
    return _detect_provider_impl(
        component_dir,
        available_paths=available_paths,
        max_entries=MAX_PROVIDER_DETECTION_ENTRIES,
    )
