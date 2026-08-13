#!/usr/bin/env python3
"""Validate repository hygiene without changing the worktree or index."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence


GENERATED_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".hypothesis",
    ".cache",
    ".eggs",
    "build",
    "dist",
}
GENERATED_NAMES = {".coverage", "coverage.xml", "MANIFEST"}
GENERATED_SUFFIXES = {".pyc", ".pyo"}
TEXT_SUFFIXES = {
    ".json",
    ".md",
    ".py",
    ".pyi",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
TEXT_NAMES = {
    ".dockerignore",
    ".gitattributes",
    ".gitignore",
    ".pre-commit-hooks.yaml",
    "Dockerfile",
    "LICENSE",
    "MANIFEST.in",
}


def _git(repo: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo,
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError("git is required") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout).decode(
            "utf-8", "replace"
        ).strip()
        raise RuntimeError(detail or "git command failed") from error
    return result.stdout


def _tracked_entries(repo: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for record in _git(repo, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        fields = metadata.split()
        if len(fields) != 3 or fields[2] != b"0":
            raise RuntimeError("the index contains unresolved or malformed entries")
        path = raw_path.decode("utf-8", "surrogateescape")
        entries.append((fields[0].decode("ascii"), path))
    return entries


def _is_generated(path: Path) -> bool:
    return (
        any(part in GENERATED_PARTS or part.endswith(".egg-info") for part in path.parts)
        or path.name in GENERATED_NAMES
        or path.suffix in GENERATED_SUFFIXES
    )


def _is_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_NAMES


def hygiene_errors(repo: Path) -> list[str]:
    """Return deterministic hygiene violations in the tracked repository tree."""
    repo = repo.resolve()
    top = Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if top != repo:
        return ["--repo must be the repository root"]

    errors: list[str] = []
    entries = _tracked_entries(repo)
    folded: dict[str, str] = {}
    for mode, name in entries:
        relative = Path(name)
        if _is_generated(relative):
            errors.append(f"generated artifact is tracked: {name}")

        key = name.casefold()
        previous = folded.setdefault(key, name)
        if previous != name:
            errors.append(
                f"case-colliding tracked paths are not portable: {previous!r} and {name!r}"
            )

        if mode == "100755" and not (
            len(relative.parts) == 2
            and relative.parts[0] == "scripts"
            and relative.suffix == ".sh"
        ):
            errors.append(f"unexpected executable bit on tracked file: {name}")

        if mode != "120000" and _is_text(relative):
            path = repo / relative
            try:
                content = path.read_bytes()
            except OSError as error:
                errors.append(f"cannot read tracked text file {name}: {error}")
                continue
            if content.startswith(b"\xef\xbb\xbf"):
                errors.append(f"UTF-8 BOM is not allowed: {name}")
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(f"tracked text file is not UTF-8: {name}")
                continue
            if "\r" in text:
                errors.append(f"tracked text file contains CR characters: {name}")
            if content and not content.endswith(b"\n"):
                errors.append(f"tracked text file lacks a final newline: {name}")
            if any(line.endswith((" ", "\t")) for line in text.splitlines()):
                errors.append(f"tracked text file has trailing whitespace: {name}")
            if any(
                line.startswith(("<<<<<<< ", ">>>>>>> "))
                for line in text.splitlines()
            ):
                errors.append(f"tracked text file contains conflict markers: {name}")

    return sorted(set(errors))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check tracked files for generated debris and portability hazards."
    )
    parser.add_argument("--repo", type=Path, default=Path("."))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        errors = hygiene_errors(args.repo)
    except (OSError, RuntimeError, UnicodeError, ValueError) as error:
        print(f"Repository hygiene check failed: {error}", file=sys.stderr)
        return 1
    if errors:
        print(f"Repository hygiene check failed ({len(errors)} issue(s)):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Repository hygiene check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
