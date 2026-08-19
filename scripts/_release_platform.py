"""Platform-specific executable resolution for maintainer release tools."""

from __future__ import annotations

import os
import shutil
from pathlib import PureWindowsPath
from typing import Optional


def resolve_bash(
    search_path: Optional[str],
    *,
    platform_name: str = os.name,
) -> Optional[str]:
    """Return a Bash that shares filesystem paths with the invoking Python."""
    if platform_name == "nt":
        git = shutil.which("git", path=search_path)
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
                    if key not in seen and os.path.isfile(candidate):
                        return candidate
                    seen.add(key)

        # A generic ``bash.exe`` on Windows may be the WSL launcher. It does
        # not share Windows paths with this Python process, so fail closed.
        return None

    return shutil.which("bash", path=search_path)
