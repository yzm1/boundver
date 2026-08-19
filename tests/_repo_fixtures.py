"""Shared helpers for tests that need a minimal local Git repository."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


def init_git_repo(
    root: Path,
    *,
    initial_branch: Optional[str] = None,
    user_email: str = "test@example.invalid",
    user_name: str = "Boundver Test",
) -> None:
    """Initialize *root* with deterministic local branch and commit identity."""
    init_command = ["git", "init"]
    if initial_branch is not None:
        init_command.extend(["-b", initial_branch])
    subprocess.run(
        init_command,
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", user_email],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", user_name],
        cwd=root,
        check=True,
        capture_output=True,
    )


def commit_all(root: Path, message: str = "test fixture") -> None:
    """Stage the complete fixture state and create one quiet local commit."""
    subprocess.run(
        ["git", "add", "--all"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=root,
        check=True,
        capture_output=True,
    )
