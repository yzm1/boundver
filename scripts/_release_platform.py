"""Platform-specific executable resolution for maintainer release tools."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path, PureWindowsPath
from typing import Mapping, Optional


_GIT_AMBIENT_OVERRIDE_NAMES = frozenset({"SSH_ASKPASS"})
_GIT_AMBIENT_OVERRIDE_PREFIXES = ("GIT_",)

_SHELL_AMBIENT_OVERRIDE_NAMES = frozenset(
    {
        "BASHOPTS",
        "BASH_ENV",
        "CDPATH",
        "ENV",
        "GLOBIGNORE",
        "SHELLOPTS",
    }
)

_GITHUB_AMBIENT_OVERRIDE_NAMES = frozenset(
    {
        "ALL_PROXY",
        "BROWSER",
        "CLICOLOR_FORCE",
        "CURL_CA_BUNDLE",
        "CURL_HOME",
        "DEBUG",
        "EDITOR",
        "GH_ACCESSIBLE_COLORS",
        "GH_ACCESSIBLE_PROMPTER",
        "GH_BROWSER",
        "GH_DEBUG",
        "GH_EDITOR",
        "GH_ENTERPRISE_TOKEN",
        "GH_FORCE_TTY",
        "GH_HOST",
        "GH_HTTP_UNIX_SOCKET",
        "GH_MDWIDTH",
        "GH_PAGER",
        "GH_PROMPT_DISABLED",
        "GH_REPO",
        "GH_SPINNER_DISABLED",
        "GHES_TOKEN",
        "GITHUB_API_URL",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_GRAPHQL_URL",
        "GITHUB_SERVER_URL",
        "GIT_EDITOR",
        "GLAMOUR_STYLE",
        "GODEBUG",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NODE_EXTRA_CA_CERTS",
        "NO_PROXY",
        "PAGER",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SSLKEYLOGFILE",
        "VISUAL",
    }
)


def _is_windows_reparse_point(identity: os.stat_result) -> bool:
    attributes = getattr(identity, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _directory_identity(identity: os.stat_result) -> tuple[int, int, int, bool]:
    """Return the stable fields used to detect a replaced output parent."""
    return (
        identity.st_dev,
        identity.st_ino,
        identity.st_mode,
        _is_windows_reparse_point(identity),
    )


def _canonicalize_trusted_output_prefix(path: Path, label: str) -> Path:
    """Resolve only the runtime-selected prefix of an output path.

    macOS exposes its system temporary directory below the stable ``/var``
    alias for ``/private/var``. Rejecting that alias makes ordinary temporary
    outputs impossible, while resolving an arbitrary output parent would let a
    repository-owned symlink redirect publication. Canonicalize only an
    existing root already selected by the process (the working directory or
    Python's temporary directory), then validate every output-specific
    component without following links.
    """
    candidates: list[tuple[int, Path, Path]] = []
    for selected in (Path.cwd(), Path(tempfile.gettempdir())):
        lexical_root = Path(os.path.abspath(selected))
        try:
            relative = path.relative_to(lexical_root)
        except ValueError:
            continue
        candidates.append((len(lexical_root.parts), lexical_root, relative))
    if not candidates:
        return path

    _depth, lexical_root, relative = max(candidates, key=lambda item: item[0])
    try:
        resolved_root = lexical_root.resolve(strict=True)
        identity = resolved_root.lstat()
    except OSError as error:
        raise ValueError(
            f"{label} runtime-selected root is unavailable: {lexical_root}"
        ) from error
    if not stat.S_ISDIR(identity.st_mode) or _is_windows_reparse_point(identity):
        raise ValueError(
            f"{label} runtime-selected root is not a plain directory: "
            f"{lexical_root}"
        )
    return resolved_root.joinpath(*relative.parts)


def prepare_plain_output_file(
    path: Path,
    label: str,
) -> tuple[Path, tuple[tuple[Path, tuple[int, int, int, bool]], ...]]:
    """Prepare a lexical output path without following repository symlinks.

    The process-selected working or temporary-directory prefix is resolved once
    to tolerate stable platform aliases. Every output-specific ancestor must be
    a real directory rather than a symlink, junction, or other Windows reparse
    point. Missing ancestors are created one at a time and checked immediately.
    An existing leaf must be a regular file. The caller must revalidate the
    returned parent identity immediately before replacing the leaf.
    """
    absolute = Path(os.path.abspath(path))
    if not absolute.anchor or not absolute.name:
        raise ValueError(f"{label} must name a file: {path}")
    absolute = _canonicalize_trusted_output_prefix(absolute, label)

    current = Path(absolute.anchor)
    ancestors = [current]
    for part in absolute.parts[1:-1]:
        current = current / part
        ancestors.append(current)
        try:
            identity = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir()
            except FileExistsError:
                pass
            identity = current.lstat()
        if not stat.S_ISDIR(identity.st_mode) or _is_windows_reparse_point(identity):
            raise ValueError(
                f"{label} parent must not traverse a symlink, junction, "
                f"or reparse point: {current}"
            )

    ancestor_identities = []
    for ancestor in ancestors:
        identity = ancestor.lstat()
        if not stat.S_ISDIR(identity.st_mode) or _is_windows_reparse_point(identity):
            raise ValueError(
                f"{label} parent must not traverse a symlink, junction, "
                f"or reparse point: {ancestor}"
            )
        ancestor_identities.append((ancestor, _directory_identity(identity)))

    try:
        leaf_identity = absolute.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(leaf_identity.st_mode) or _is_windows_reparse_point(
            leaf_identity
        ):
            raise ValueError(
                f"{label} must be a regular file, not a symlink or reparse point: "
                f"{absolute}"
            )
    return absolute, tuple(ancestor_identities)


def revalidate_plain_output_file(
    path: Path,
    ancestor_identities: tuple[
        tuple[Path, tuple[int, int, int, bool]], ...
    ],
    label: str,
) -> None:
    """Fail if a prepared output ancestor was replaced before publication."""
    for ancestor, expected in ancestor_identities:
        try:
            current = ancestor.lstat()
        except OSError as error:
            raise ValueError(
                f"{label} parent changed before publication: {ancestor}"
            ) from error
        if (
            not stat.S_ISDIR(current.st_mode)
            or _is_windows_reparse_point(current)
            or _directory_identity(current) != expected
        ):
            raise ValueError(
                f"{label} parent changed before publication: {ancestor}"
            )


def prepare_plain_output_directory(
    path: Path,
    label: str,
) -> tuple[Path, tuple[tuple[Path, tuple[int, int, int, bool]], ...]]:
    """Create and validate an output directory without following symlinks."""
    lexical = Path(os.path.abspath(path))
    if not lexical.anchor or not lexical.name:
        raise ValueError(f"{label} must name a directory: {path}")
    prepared_probe, identities = prepare_plain_output_file(
        lexical / ".boundver-output-probe", label
    )
    return prepared_probe.parent, identities


def revalidate_plain_output_directory(
    path: Path,
    ancestor_identities: tuple[
        tuple[Path, tuple[int, int, int, bool]], ...
    ],
    label: str,
) -> None:
    """Fail if a prepared output directory or ancestor was replaced."""
    revalidate_plain_output_file(
        path / ".boundver-output-probe", ancestor_identities, label
    )


def sanitize_git_environment(
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Remove ambient variables that can redirect Git's repo, objects, or code."""
    result = dict(os.environ if environment is None else environment)
    for name in tuple(result):
        canonical = name.upper()
        if canonical in _GIT_AMBIENT_OVERRIDE_NAMES or canonical.startswith(
            _GIT_AMBIENT_OVERRIDE_PREFIXES
        ):
            result.pop(name, None)
    return result


def sanitize_shell_environment(
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Remove ambient shell startup files and option/path overrides."""
    result = dict(os.environ if environment is None else environment)
    for name in tuple(result):
        if name.upper() in _SHELL_AMBIENT_OVERRIDE_NAMES:
            result.pop(name, None)
    return result


def sanitize_github_environment(
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Pin authenticated GitHub CLI requests to the public HTTPS service.

    ``GH_TOKEN`` and ``GITHUB_TOKEN`` are deliberately preserved. Everything
    capable of selecting another host, transport, trust root, repository, or
    interactive/debug output mode is removed before deterministic values are
    installed.
    """
    result = dict(os.environ if environment is None else environment)
    for name in tuple(result):
        if name.upper() in _GITHUB_AMBIENT_OVERRIDE_NAMES:
            result.pop(name, None)
    result.update(
        {
            "GH_HOST": "github.com",
            "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_PAGER": "cat",
            "GH_PROMPT_DISABLED": "1",
            "NO_COLOR": "1",
            "NO_PROXY": "github.com,api.github.com",
            "PAGER": "cat",
            "TERM": "dumb",
        }
    )
    return result


def _trusted_external_file(
    candidate: Optional[str], forbidden_root: Optional[Path]
) -> Optional[str]:
    """Resolve *candidate* and reject files selected from a guarded tree."""
    if candidate is None:
        return None
    if forbidden_root is None:
        return candidate if os.path.isfile(candidate) else None
    try:
        root = forbidden_root.resolve(strict=True)
        raw = Path(os.path.abspath(candidate))
        resolved = Path(candidate).resolve(strict=True)
        identity = resolved.stat()
    except OSError:
        return None
    if not stat.S_ISREG(identity.st_mode):
        return None
    if (
        raw == root
        or root in raw.parents
        or resolved == root
        or root in resolved.parents
    ):
        return None
    return str(resolved)


def resolve_bash(
    search_path: Optional[str],
    *,
    platform_name: str = os.name,
    forbidden_root: Optional[Path] = None,
) -> Optional[str]:
    """Return a Bash that shares filesystem paths with the invoking Python."""
    if platform_name == "nt":
        git = _trusted_external_file(
            shutil.which("git", path=search_path), forbidden_root
        )
        if git is not None:
            git_path = PureWindowsPath(git)
            parent = git_path.parent
            roots: list[PureWindowsPath] = []
            if parent.name.casefold() in {"bin", "cmd"}:
                roots.append(parent.parent)
                container = parent.parent.name.casefold()
                if container == "usr" or container.startswith("mingw"):
                    roots.insert(0, parent.parent.parent)

            seen: set[str] = set()
            for root in roots:
                for relative in (("bin", "bash.exe"), ("usr", "bin", "bash.exe")):
                    candidate = str(root.joinpath(*relative))
                    key = candidate.casefold()
                    if key not in seen:
                        trusted = _trusted_external_file(candidate, forbidden_root)
                        if trusted is not None:
                            return trusted
                    seen.add(key)

        # A generic ``bash.exe`` on Windows may be the WSL launcher. It does
        # not share Windows paths with this Python process, so fail closed.
        return None

    return _trusted_external_file(
        shutil.which("bash", path=search_path), forbidden_root
    )
