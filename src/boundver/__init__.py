"""boundver package."""

from typing import List, Optional

__all__ = [
    "main",
    "generate",
    "verify",
    "diff",
    "load_config",
    "BoundverError",
    "ConfigError",
    "LockfileError",
    "ProviderError",
    "GuardrailError",
    "SourceMode",
    "create_registry",
    "analyze_component_drift",
    "analyze_explain_changes",
]

from importlib.metadata import PackageNotFoundError, version

from ._utils import BoundverError, ConfigError, GuardrailError, LockfileError, ProviderError, SourceMode
from .providers import create_registry
from ._output import analyze_component_drift, analyze_explain_changes

from .cli import main

try:
    __version__ = version("boundver")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+local"


def load_config(config_path: str = "boundary.config.json") -> dict:
    """Load and return the parsed config dict from a boundary.config.json file.

    Raises ValueError if the config is invalid or the file is not found.
    """
    from ._config import load_config_file, find_config_file
    from ._git import git_root

    repo_root = git_root()
    path = find_config_file(repo_root, config_path)
    return load_config_file(path)


def generate(
    config_path: str = "boundary.config.json",
    out_path: Optional[str] = "boundary.lock.json",
    source: str = "head",
    allow_custom_providers: bool = False,
) -> dict:
    """Generate a lockfile and return it as a dict.

    Also writes to *out_path* (relative to repo root). Pass ``out_path=None``
    to skip writing.
    """
    from ._config import load_config_file, find_config_file, validate_config
    from ._lockfile import generate_lockfile
    from ._git import git_root
    from .core import (
        _capture_operation_snapshot,
        _ensure_lock_outside_components,
        _write_lockfile_atomic,
    )

    repo_root = git_root()
    snapshot = _capture_operation_snapshot(repo_root, source)
    resolved_config_path = find_config_file(
        repo_root, config_path, snapshot=snapshot
    )
    config = load_config_file(
        resolved_config_path, repo_root=repo_root, snapshot=snapshot
    )
    config_errors = validate_config(
        config,
        repo_root,
        allow_custom_providers=allow_custom_providers,
        source=source,
        snapshot=snapshot,
        require_slice_facets=True,
    )
    if config_errors:
        raise ConfigError("Config is invalid:\n" + "\n".join(config_errors))
    if out_path is not None:
        dest = repo_root / out_path
        _ensure_lock_outside_components(
            repo_root,
            dest,
            config,
            config_path=resolved_config_path,
        )
    lockfile = generate_lockfile(
        config, repo_root, source=source, strict=True,
        allow_custom_providers=allow_custom_providers,
        snapshot=snapshot,
    )
    if out_path is not None:
        _write_lockfile_atomic(dest, lockfile)
    return lockfile


def verify(
    config_path: str = "boundary.config.json",
    lock_path: str = "boundary.lock.json",
    source: str = "head",
    components: Optional[List[str]] = None,
    allow_custom_providers: bool = False,
    facets: Optional[List[str]] = None,
    observations: Optional[List[str]] = None,
    fail_fast: bool = False,
    transitive_consumers: bool = False,
) -> List[str]:
    """Verify lockfile matches current repo state.

    Returns gated mismatch strings. Empty means the selected gate is current.
    When *facets* is omitted, ``defaults.verify_facets`` is honored just like
    the CLI. Pass a list as *observations* to collect drift outside that gate.
    """
    from ._config import load_config_file, find_config_file, validate_config
    from ._lockfile import verify_lockfile
    from ._git import git_root
    from .core import (
        _capture_operation_snapshot,
        _ensure_lock_outside_components,
        _load_lockfile,
        _verify_lock_preflight_issues,
    )

    repo_root = git_root()
    snapshot = _capture_operation_snapshot(repo_root, source)
    resolved_config_path = find_config_file(
        repo_root, config_path, snapshot=snapshot
    )
    config = load_config_file(
        resolved_config_path, repo_root=repo_root, snapshot=snapshot
    )
    config_errors = validate_config(
        config,
        repo_root,
        allow_custom_providers=allow_custom_providers,
        source=source,
        snapshot=snapshot,
    )
    if config_errors:
        raise ConfigError("Config is invalid:\n" + "\n".join(config_errors))
    resolved_lock_path = repo_root / lock_path
    _ensure_lock_outside_components(
        repo_root,
        resolved_lock_path,
        config,
        config_path=resolved_config_path,
    )
    lf = _load_lockfile(
        resolved_lock_path, repo_root=repo_root, snapshot=snapshot
    )
    preflight_issues = _verify_lock_preflight_issues(config, lf)
    if preflight_issues:
        return preflight_issues
    return verify_lockfile(
        config, lf, repo_root, source=source,
        components_filter=components,
        allow_custom_providers=allow_custom_providers,
        facets=facets,
        observations=observations,
        fail_fast=fail_fast,
        snapshot=snapshot,
        transitive_consumers=transitive_consumers,
    )


def diff(old_path: str, new_path: str) -> dict:
    """Diff two lockfiles and return structured result."""
    from pathlib import Path
    from ._diff import diff_lockfiles, require_compatible_lockfile_schemas
    from .core import _load_lockfile, _require_diffable_lockfile

    old = _load_lockfile(Path(old_path))
    new = _load_lockfile(Path(new_path))
    require_compatible_lockfile_schemas(old, new)
    _require_diffable_lockfile(old)
    _require_diffable_lockfile(new)
    return diff_lockfiles(old, new)
