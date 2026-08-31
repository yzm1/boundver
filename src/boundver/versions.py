"""Version extraction and parsing utilities."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from ._config_contract import git_tag_prefix_error
from ._utils import (
    GuardrailError,
    MAX_TOML_INTEGER_DIGITS as MAX_TOML_INTEGER_DIGITS,
    _bounded_int_to_decimal,
    _bounded_json_int,
    _bounded_yaml_int,
    _normalize_declared_path,
    _read_bounded_path_bytes,
    _toml_has_oversized_numeric_token,
)

MAX_VERSION_FILE_BYTES = 10 * 1024 * 1024

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None

try:
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    yaml = None


def _load_yaml_with_bounded_integers(text: str) -> Any:
    """Load YAML without aliases, duplicate keys, or runtime-sized integers."""
    if yaml is None:  # pragma: no cover - callers guard the optional dependency
        raise RuntimeError("PyYAML is not available")

    class BoundedVersionLoader(yaml.SafeLoader):
        def compose_node(self, parent: Any, index: Any) -> Any:
            if self.check_event(yaml.AliasEvent):
                raise ValueError("YAML aliases are not supported in version sources")
            return super().compose_node(parent, index)

    def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict:
        if not isinstance(node, yaml.MappingNode):
            raise ValueError("expected a YAML mapping")
        mapping: dict = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if type(key) is not str:
                raise ValueError("YAML version-source mapping keys must be strings")
            if key in mapping:
                raise ValueError(f"duplicate YAML version-source key {key!r}")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    def construct_integer(loader: Any, node: Any) -> int:
        scalar = loader.construct_scalar(node)
        return _bounded_yaml_int(scalar)

    BoundedVersionLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_mapping,
    )
    BoundedVersionLoader.add_constructor(
        "tag:yaml.org,2002:int",
        construct_integer,
    )
    return yaml.load(text, Loader=BoundedVersionLoader)


def _version_value_to_string(value: Any) -> Optional[str]:
    """Render a parsed version field without Python's mutable integer limit."""
    if type(value) is str:
        return value
    if type(value) is int:
        return _bounded_int_to_decimal(value)
    if type(value) is float:
        return str(value) if math.isfinite(value) else None
    # Booleans, nulls, containers, timestamps, and extension objects are not
    # textual or numeric version identifiers. Never stringify a container:
    # nested large integers would reintroduce Python's mutable digit limit.
    return None


def _unique_version_json_object(pairs: list[tuple]) -> dict:
    """Build one version-source object without last-key-wins data loss."""
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON version-source key {key!r}")
        value[key] = item
    return value


def _reject_version_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON version-source number {value!r}")


def _toml_version_value_to_string(value: Any) -> Optional[str]:
    """Return a TOML version only when its source value was textual.

    TOML parsers expose no integer hook analogous to ``json.loads``. Parsing a
    numeric value can therefore accept or reject the document based on the
    interpreter-wide decimal conversion limit. Version identifiers are textual
    data, so requiring a TOML string gives every supported runtime the same
    result and preserves the source spelling exactly.
    """
    return value if type(value) is str else None


def extract_version(
    repo_root: Path,
    component_path: str,
    version_source: Optional[dict],
    git_latest_tag_fn: Optional[Callable[[Path, str], Optional[str]]] = None,
    read_file_fn: Optional[Callable[[str], bytes]] = None,
) -> Optional[str]:
    """Extract version from a version_source config.

    If *read_file_fn* is provided, it is used to read file content (supporting
    head/index/working-tree sources). Otherwise falls back to disk reads.
    """
    if version_source is None:
        return None
    if type(version_source) is not dict or type(component_path) is not str:
        return None
    if "git_tag_prefix" in version_source:
        prefix = version_source.get("git_tag_prefix")
        if git_tag_prefix_error(prefix) is not None:
            return None
        if git_latest_tag_fn is None:
            return None
        return git_latest_tag_fn(repo_root, prefix)
    file_rel = version_source.get("file")
    field_path = version_source.get("field")
    if (
        type(file_rel) is not str
        or type(field_path) is not str
        or not file_rel
        or not field_path
    ):
        return None
    try:
        file_rel = _normalize_declared_path(file_rel)
    except ValueError:
        return None
    component_prefix = component_path.strip().strip("/")
    repo_rel = (
        file_rel.lstrip("/")
        if component_prefix in {"", "."}
        else f"{component_prefix}/{file_rel.lstrip('/')}"
    )
    if read_file_fn is not None:
        try:
            raw = read_file_fn(repo_rel)
        except (OSError, subprocess.CalledProcessError, GuardrailError):
            return None
        return _extract_field_from_bytes(raw, file_rel, field_path)
    # Fallback: read from disk
    full_path = repo_root / component_path / file_rel
    # Verify resolved path stays within repository
    try:
        full_path.resolve().relative_to(repo_root.resolve())
    except (OSError, ValueError):
        return None
    if not full_path.exists():
        return None
    raw = _read_version_file_bytes(full_path, repo_rel)
    if raw is None:
        return None
    return _extract_field_from_bytes(raw, file_rel, field_path)


def _extract_field_from_bytes(raw: bytes, file_rel: str, field_path: str) -> Optional[str]:
    """Extract a field from raw file bytes based on file extension."""
    if not isinstance(raw, bytes) or len(raw) > MAX_VERSION_FILE_BYTES:
        return None
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if file_rel.endswith('.json'):
        return _extract_json_from_text(text, field_path)
    if file_rel.endswith('.toml'):
        return _extract_toml_from_text(text, field_path)
    if file_rel.endswith('.yaml') or file_rel.endswith('.yml'):
        return _extract_yaml_from_text(text, field_path)
    return None


def _read_version_file_bytes(path: Path, path_label: str) -> Optional[bytes]:
    """Read a disk-backed version source without a stat/read allocation race."""
    try:
        return _read_bounded_path_bytes(
            path,
            path_label,
            max_bytes=MAX_VERSION_FILE_BYTES,
        )
    except (OSError, ValueError, GuardrailError):
        return None


def _extract_json_from_text(text: str, field_path: str) -> Optional[str]:
    try:
        data = json.loads(
            text,
            object_pairs_hook=_unique_version_json_object,
            parse_constant=_reject_version_json_constant,
            parse_int=_bounded_json_int,
        )
        for key in field_path.split('.'):
            data = data[key]
        return _version_value_to_string(data)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _extract_toml_from_text(text: str, field_path: str) -> Optional[str]:
    keys = field_path.split('.')
    if _toml_has_oversized_numeric_token(text):
        return None
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except (MemoryError, RecursionError):
            raise
        except Exception:
            return None
        current: Any = data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
        return _toml_version_value_to_string(current)
    # tomli is required on Python 3.9-3.10 and tomllib is built in thereafter.
    # A missing parser is a broken installation; do not guess at TOML syntax
    # and risk blessing a different version identity.
    return None


def _extract_yaml_from_text(text: str, field_path: str) -> Optional[str]:
    keys = field_path.split('.')
    if yaml is not None:
        try:
            data = _load_yaml_with_bounded_integers(text)
        except (MemoryError, RecursionError):
            raise
        except Exception:
            return None  # Real parser failed — don't fall back to regex; be authoritative
        current: Any = data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return None  # Field not found — authoritative answer from real parser
            current = current[key]
        return _version_value_to_string(current)
    # YAML support is an explicit optional dependency. Fail closed when its
    # authoritative parser is absent rather than applying a partial grammar.
    return None


def _extract_json_field(path: Path, field_path: str) -> Optional[str]:
    raw = _read_version_file_bytes(path, str(path))
    if raw is None:
        return None
    return _extract_field_from_bytes(raw, ".json", field_path)


def _extract_toml_field(path: Path, field_path: str) -> Optional[str]:
    raw = _read_version_file_bytes(path, str(path))
    if raw is None:
        return None
    return _extract_field_from_bytes(raw, ".toml", field_path)


def _extract_yaml_field(path: Path, field_path: str) -> Optional[str]:
    raw = _read_version_file_bytes(path, str(path))
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return _extract_yaml_from_text(text, field_path)


def _is_ascii_digit(character: str) -> bool:
    return "0" <= character <= "9"


def _is_semver_identifier_character(character: str) -> bool:
    return (
        _is_ascii_digit(character)
        or "A" <= character <= "Z"
        or "a" <= character <= "z"
        or character == "-"
    )


def _consume_core_identifier(value: str, offset: int) -> Optional[tuple[int, int]]:
    """Return one SemVer numeric identifier span, or ``None`` when invalid."""
    length = len(value)
    if offset >= length or not _is_ascii_digit(value[offset]):
        return None
    start = offset
    while offset < length and _is_ascii_digit(value[offset]):
        offset += 1
    if offset - start > 1 and value[start] == "0":
        return None
    return start, offset


def _consume_semver_identifiers(
    value: str,
    offset: int,
    *,
    stop_at_build: bool,
    reject_numeric_leading_zeroes: bool,
) -> Optional[int]:
    """Consume a dot-delimited prerelease or build identifier sequence."""
    length = len(value)
    while True:
        start = offset
        numeric = True
        while offset < length:
            character = value[offset]
            if character == "." or (stop_at_build and character == "+"):
                break
            if not _is_semver_identifier_character(character):
                return None
            if not _is_ascii_digit(character):
                numeric = False
            offset += 1
        if offset == start:
            return None
        if (
            reject_numeric_leading_zeroes
            and numeric
            and offset - start > 1
            and value[start] == "0"
        ):
            return None
        if offset >= length or (stop_at_build and value[offset] == "+"):
            return offset
        # A period terminates the current identifier and requires another.
        offset += 1


def parse_semver(version: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if not version:
        return (None, None, None)
    if type(version) is not str:
        return (None, None, None)

    length = len(version)
    offset = 1 if version.startswith("v") else 0
    major_span = _consume_core_identifier(version, offset)
    if major_span is None:
        return (None, None, version)
    major_start, offset = major_span
    if offset >= length or version[offset] != ".":
        return (None, None, version)
    offset += 1

    minor_span = _consume_core_identifier(version, offset)
    if minor_span is None:
        return (None, None, version)
    minor_start, offset = minor_span
    patch_span: Optional[tuple[int, int]] = None
    if offset < length and version[offset] == ".":
        offset += 1
        patch_span = _consume_core_identifier(version, offset)
        if patch_span is None:
            return (None, None, version)
        _, offset = patch_span

    if offset < length and version[offset] == "-":
        offset = _consume_semver_identifiers(
            version,
            offset + 1,
            stop_at_build=True,
            reject_numeric_leading_zeroes=True,
        )
        if offset is None:
            return (None, None, version)
    if offset < length and version[offset] == "+":
        offset = _consume_semver_identifiers(
            version,
            offset + 1,
            stop_at_build=False,
            reject_numeric_leading_zeroes=False,
        )
        if offset is None:
            return (None, None, version)
    if offset != length:
        return (None, None, version)

    major = version[major_start:major_span[1]]
    minor = version[minor_start:minor_span[1]]
    patch = version[patch_span[0]:patch_span[1]] if patch_span is not None else "0"
    return (major, f"{major}.{minor}", f"{major}.{minor}.{patch}")
