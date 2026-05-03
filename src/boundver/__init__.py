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
    from pathlib import Path
    from ._config import load_config_file, find_config_file
    from ._git import git_root

    repo_root = git_root()
    path = find_config_file(repo_root, config_path)
    return load_config_file(path)


def generate(
    config_path: str = "boundary.config.json",
    out_path: str = "boundary.lock.json",
    source: str = "head",
    allow_custom_providers: bool = False,
) -> dict:
    """Generate a lockfile and return it as a dict.

    Also writes to *out_path* (relative to repo root). Pass ``out_path=None``
    to skip writing.
    """
    import json
    from pathlib import Path
    from ._config import load_config_file, find_config_file
    from ._lockfile import generate_lockfile
    from ._git import git_root

    repo_root = git_root()
    config = load_config_file(find_config_file(repo_root, config_path))
    lockfile = generate_lockfile(
        config, repo_root, source=source, strict=True,
        allow_custom_providers=allow_custom_providers,
    )
    if out_path is not None:
        dest = repo_root / out_path
        dest.write_text(json.dumps(lockfile, indent=2) + "\n", encoding="utf-8")
    return lockfile


def verify(
    config_path: str = "boundary.config.json",
    lock_path: str = "boundary.lock.json",
    source: str = "head",
    components: Optional[List[str]] = None,
    allow_custom_providers: bool = False,
) -> List[str]:
    """Verify lockfile matches current repo state.

    Returns a list of mismatch strings. Empty list means up to date.
    """
    import json
    from pathlib import Path
    from ._config import load_config_file, find_config_file
    from ._lockfile import verify_lockfile
    from ._git import git_root

    repo_root = git_root()
    config = load_config_file(find_config_file(repo_root, config_path))
    lf = json.loads((repo_root / lock_path).read_text(encoding="utf-8"))
    return verify_lockfile(
        config, lf, repo_root, source=source,
        components_filter=components,
        allow_custom_providers=allow_custom_providers,
    )


def diff(old_path: str, new_path: str) -> dict:
    """Diff two lockfiles and return structured result."""
    import json
    from pathlib import Path
    from ._diff import diff_lockfiles

    old = json.loads(Path(old_path).read_text(encoding="utf-8"))
    new = json.loads(Path(new_path).read_text(encoding="utf-8"))
    return diff_lockfiles(old, new)
