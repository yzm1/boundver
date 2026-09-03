"""Config parsing and source-bound file loading."""

import os
import subprocess
from pathlib import Path
from typing import Any, List, Optional

from ._git import GitSourceSnapshot, _git_cat_blob
from ._structured_data import strict_json_loads
from ._utils import (
    MAX_JSON_NUMBER_CHARACTERS,
    ConfigError,
    GuardrailError,
    _bounded_diagnostic_repr,
    _bounded_exception_text,
    _bounded_json_value_issues,
    _bounded_yaml_compose_node,
    _bounded_yaml_int,
    _read_bounded_path_bytes,
    _toml_preparse_issues,
)

CONFIG_CANDIDATES = (
    "boundary.config.json",
    "boundary.config.yaml",
    "boundary.config.yml",
    "boundary.config.toml",
)


def snapshot_relative_path(repo_root: Path, path: Path) -> str:
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


def json_value_issues(value: Any, *, path: str = "config") -> List[str]:
    """Return reasons a parsed value is unsafe for deterministic JSON hashing."""
    return _bounded_json_value_issues(value, path=path)


def parse_config_text(text: str, path: Path) -> dict:
    """Parse config text according to *path* while rejecting lossy values."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            result = strict_json_loads(text)
        except (ValueError, RecursionError, OverflowError) as exc:
            raise ConfigError(
                f"JSON parse error in {path}: {_bounded_exception_text(exc)}"
            ) from exc
    elif suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore

            class StrictConfigLoader(yaml.SafeLoader):
                def compose_node(self, parent: Any, index: Any) -> Any:
                    if self.check_event(yaml.AliasEvent):
                        raise ConfigError(
                            "YAML aliases are not supported in boundver config"
                        )
                    return _bounded_yaml_compose_node(
                        self,
                        parent,
                        index,
                        super().compose_node,
                    )

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
                        raise ConfigError(
                            "duplicate YAML mapping key "
                            f"{_bounded_diagnostic_repr(key)}"
                        )
                    mapping[key] = loader.construct_object(value_node, deep=deep)
                return mapping

            def construct_integer(loader: Any, node: Any) -> int:
                try:
                    scalar = loader.construct_scalar(node)
                    return _bounded_yaml_int(scalar)
                except (TypeError, ValueError) as exc:
                    raise ConfigError(
                        f"invalid YAML integer: {_bounded_exception_text(exc)}"
                    ) from exc

            def construct_float(loader: Any, node: Any) -> float:
                scalar = loader.construct_scalar(node)
                if len(scalar) > MAX_JSON_NUMBER_CHARACTERS:
                    raise ConfigError(
                        "YAML number exceeds the "
                        f"{MAX_JSON_NUMBER_CHARACTERS}-character limit"
                    )
                return yaml.SafeLoader.construct_yaml_float(loader, node)

            StrictConfigLoader.add_constructor(
                yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                construct_mapping,
            )
            StrictConfigLoader.add_constructor(
                "tag:yaml.org,2002:int",
                construct_integer,
            )
            StrictConfigLoader.add_constructor(
                "tag:yaml.org,2002:float",
                construct_float,
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
        except yaml.MarkedYAMLError as exc:
            # PyYAML's string form includes the offending source line. Config
            # files can accidentally contain credentials, so retain only the
            # exception kind and coordinates in CLI and Action diagnostics.
            mark = exc.context_mark or exc.problem_mark
            location = ""
            if mark is not None:
                location = f" at line {mark.line + 1}, column {mark.column + 1}"
            raise ConfigError(
                f"YAML parse error in {path}: "
                f"{exc.__class__.__name__}{location}"
            ) from exc
        except Exception as exc:
            raise ConfigError(
                f"YAML parse error in {path}: {_bounded_exception_text(exc)}"
            ) from exc
    elif suffix == ".toml":
        oversized_numeric, oversized_structure = _toml_preparse_issues(text)
        if oversized_numeric:
            raise ConfigError(
                f"TOML config contains a numeric token exceeding the "
                f"cross-runtime safety limit in {path}"
            )
        if oversized_structure:
            raise ConfigError(
                f"TOML config exceeds the pre-parse structural limit in {path}"
            )
        try:
            import tomllib  # type: ignore
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore
            except ImportError:
                raise ConfigError(
                    f"Cannot parse {path}: neither tomllib (Python 3.11+) nor "
                    "tomli is available. Install tomli: pip install tomli"
                )
        try:
            result = tomllib.loads(text)
        except MemoryError:
            raise
        except RecursionError as exc:
            raise ConfigError(f"TOML config is nested too deeply in {path}") from exc
        except Exception as exc:
            raise ConfigError(
                f"TOML parse error in {path}: {_bounded_exception_text(exc)}"
            ) from exc
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
        value_issues = json_value_issues(result)
    except RecursionError as exc:
        raise ConfigError(f"Config is nested too deeply in {path}") from exc
    if value_issues:
        raise ConfigError(
            f"Config file {path} contains values that cannot be represented "
            "as deterministic JSON:\n" + "\n".join(value_issues)
        )
    return result


def parse_config_bytes(data: bytes, path: Path, *, max_bytes: int) -> dict:
    """Decode and parse bounded UTF-8 config bytes."""
    if len(data) > max_bytes:
        raise ConfigError(f"Config file too large ({len(data)} bytes): {path}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"Config file is not valid UTF-8: {path}: "
            f"{_bounded_exception_text(exc)}"
        ) from exc
    return parse_config_text(text, path)


def find_config_file(
    repo_root: Path,
    hint: str = "boundary.config.json",
    *,
    snapshot: Optional[GitSourceSnapshot] = None,
) -> Path:
    """Return the explicit or first source-visible config path."""
    explicit = Path(hint)
    if explicit.is_absolute():
        return explicit
    candidate = repo_root / hint
    if snapshot is not None:
        if snapshot_relative_path(repo_root, candidate) in snapshot.entries:
            return candidate
    elif candidate.exists():
        return candidate
    if hint == "boundary.config.json":
        for name in CONFIG_CANDIDATES[1:]:
            alternative = repo_root / name
            if snapshot is not None:
                if snapshot_relative_path(repo_root, alternative) in snapshot.entries:
                    return alternative
            elif alternative.exists():
                return alternative
    return candidate


def load_config_file(
    path: Path,
    *,
    max_bytes: int,
    repo_root: Optional[Path] = None,
    snapshot: Optional[GitSourceSnapshot] = None,
) -> dict:
    """Load a config from disk or one captured immutable Git source."""
    if snapshot is not None:
        if repo_root is None:
            raise ConfigError("repo_root is required for source-backed config reads")
        label = snapshot_relative_path(repo_root, path)
        entry = snapshot.entries.get(label)
        if entry is None:
            raise FileNotFoundError(
                f"Config file not found in captured {snapshot.source} source: {label}"
            )
        if entry.object_type != "blob" or entry.mode not in {"100644", "100755"}:
            raise ConfigError(
                f"Config path must be a regular file in captured {snapshot.source} "
                f"source: {label} (mode={entry.mode}, type={entry.object_type})"
            )
        try:
            data = _git_cat_blob(repo_root, entry.oid, max_bytes=max_bytes)
        except GuardrailError as exc:
            raise ConfigError(
                f"Cannot read config from captured {snapshot.source} source: "
                f"{label}: file too large or transport limit exceeded"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise ConfigError(
                f"Cannot read config from captured {snapshot.source} source: {label}"
            ) from exc
        return parse_config_bytes(data, path, max_bytes=max_bytes)

    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    try:
        data = _read_bounded_path_bytes(path, str(path), max_bytes=max_bytes)
    except GuardrailError as exc:
        raise ConfigError(
            f"Config file exceeds the {max_bytes}-byte limit at {path}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise ConfigError(
            f"Cannot read config file {path}: {_bounded_exception_text(exc)}"
        ) from exc
    return parse_config_bytes(data, path, max_bytes=max_bytes)
