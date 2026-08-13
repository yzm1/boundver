#!/usr/bin/env python3
"""Fail closed when versioned public release surfaces disagree.

This check covers repository content that PyPI, GitHub Releases, and GitHub
Marketplace snapshot from the release commit.  Remote publication state is
verified separately after each service accepts the immutable artifacts.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10 release tooling
    import tomli as tomllib

from release_changelog import extract_release_notes


TAG_RE = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
RAW_SCHEMA_RE = re.compile(
    r"https://raw\.githubusercontent\.com/yzm1/boundver/"
    r"(?P<ref>[^/\s\"'<>]+)/(?P<path>[^\s\"'<>]*schema\.json)"
)
TEXT_SUFFIXES = {".json", ".md", ".py", ".toml", ".yaml", ".yml"}
SKIP_PARTS = {
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    ".venv",
    "build",
    "dist",
    "__pycache__",
}
RELEASE_DOCS = (
    "README.md",
    "docs/getting-started.md",
    "docs/ci-cookbook.md",
    "docs/WHY_BOUNDVER.md",
    "docs/PROJECT_REVIEW.md",
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _release_files(repo: Path) -> Iterable[Path]:
    for path in sorted(repo.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        relative = path.relative_to(repo)
        if any(part in SKIP_PARTS or part.endswith(".egg-info") for part in relative.parts):
            continue
        if relative.parts and relative.parts[0] == "tests":
            continue
        yield path


def _json(path: Path, errors: list[str]) -> object | None:
    try:
        return json.loads(_read_text(path))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"{path}: cannot parse JSON: {exc}")
        return None


def readiness_errors(repo: Path, tag: str) -> list[str]:
    """Return every local release-surface disagreement for ``tag``."""
    repo = repo.resolve()
    errors: list[str] = []
    if TAG_RE.fullmatch(tag) is None:
        return [f"invalid release tag: {tag!r}"]
    version = tag.removeprefix("v")

    try:
        project = tomllib.loads(_read_text(repo / "pyproject.toml"))["project"]
    except (OSError, UnicodeError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        return [f"pyproject.toml cannot be read as PEP 621 metadata: {exc}"]
    if project.get("name") != "boundver":
        errors.append("pyproject.toml project.name must be 'boundver'")
    if project.get("version") != version:
        errors.append(
            f"pyproject.toml version {project.get('version')!r} does not match {tag}"
        )
    required_urls = {
        "Homepage": "https://github.com/yzm1/boundver",
        "Documentation": "https://github.com/yzm1/boundver/tree/main/docs",
        "Changelog": "https://github.com/yzm1/boundver/blob/main/CHANGELOG.md",
        "Issues": "https://github.com/yzm1/boundver/issues",
        "Repository": "https://github.com/yzm1/boundver",
        "GitHub Action": "https://github.com/marketplace/actions/boundver",
    }
    urls = project.get("urls")
    if not isinstance(urls, dict):
        errors.append("pyproject.toml project.urls must be a table")
    else:
        for label, expected in required_urls.items():
            if urls.get(label) != expected:
                errors.append(
                    f"pyproject.toml project.urls.{label} must be {expected!r}"
                )

    try:
        extract_release_notes(_read_text(repo / "CHANGELOG.md"), tag)
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(str(exc))

    for relative in RELEASE_DOCS:
        path = repo / relative
        try:
            text = _read_text(path)
        except (OSError, UnicodeError) as exc:
            errors.append(f"{relative}: cannot read release documentation: {exc}")
            continue
        if re.search(r"\bunreleased\b", text, flags=re.IGNORECASE):
            errors.append(f"{relative}: still describes the release as unreleased")
        if re.search(
            rf"(?:after|once|when)[^\n]{{0,80}}{re.escape(version)}[^\n]{{0,80}}published",
            text,
            flags=re.IGNORECASE,
        ):
            errors.append(f"{relative}: still contains pre-publication instructions")
        if "corrective work in progress, not a completed release" in text.lower():
            errors.append(f"{relative}: still marks the corrective release incomplete")

    try:
        readme = _read_text(repo / "README.md")
    except (OSError, UnicodeError):
        readme = ""
    for required in (
        f"yzm1/boundver@{tag}",
        "https://pypi.org/project/boundver/",
        "https://github.com/marketplace/actions/boundver",
        "docs/RELEASING.md",
    ):
        if required not in readme:
            errors.append(f"README.md must contain release-facing reference {required!r}")

    for path in _release_files(repo):
        relative = path.relative_to(repo)
        try:
            text = _read_text(path)
        except (OSError, UnicodeError) as exc:
            errors.append(f"{relative}: cannot read release surface: {exc}")
            continue
        for match in RAW_SCHEMA_RE.finditer(text):
            if match.group("ref") != tag:
                errors.append(
                    f"{relative}: schema URL uses {match.group('ref')!r}, expected {tag!r}"
                )

    expected_config_schema = (
        f"https://raw.githubusercontent.com/yzm1/boundver/{tag}/"
        "boundary.config.schema.json"
    )
    expected_lock_schema = (
        f"https://raw.githubusercontent.com/yzm1/boundver/{tag}/"
        "spec/boundary.lock.schema.json"
    )
    config_schema_path = repo / "boundary.config.schema.json"
    packaged_schema_path = repo / "src/boundver/boundary.config.schema.json"
    config_schema = _json(config_schema_path, errors)
    packaged_schema = _json(packaged_schema_path, errors)
    try:
        schemas_match = (
            config_schema_path.read_bytes() == packaged_schema_path.read_bytes()
        )
    except OSError as exc:
        errors.append(f"cannot compare root and packaged config schemas: {exc}")
    else:
        if not schemas_match:
            errors.append("root and packaged config schemas must be byte-identical")
    if isinstance(config_schema, dict) and config_schema.get("$id") != expected_config_schema:
        errors.append(
            f"boundary.config.schema.json $id must be {expected_config_schema!r}"
        )

    lock_schema = _json(repo / "spec/boundary.lock.schema.json", errors)
    if isinstance(lock_schema, dict) and lock_schema.get("$id") != expected_lock_schema:
        errors.append(
            f"spec/boundary.lock.schema.json $id must be {expected_lock_schema!r}"
        )

    for path in sorted((repo / "spec").glob("cli-output.*.schema.json")):
        value = _json(path, errors)
        expected = (
            f"https://raw.githubusercontent.com/yzm1/boundver/{tag}/spec/{path.name}"
        )
        if isinstance(value, dict) and value.get("$id") != expected:
            errors.append(f"{path.relative_to(repo)} $id must be {expected!r}")

    for path in sorted(repo.glob("examples/*/boundary.config.json")) + [
        repo / "boundary.config.json"
    ]:
        if not path.exists():
            continue
        value = _json(path, errors)
        if isinstance(value, dict) and value.get("$schema") != expected_config_schema:
            errors.append(
                f"{path.relative_to(repo)} $schema must be {expected_config_schema!r}"
            )
    for path in sorted(repo.glob("examples/*/expected.boundary.lock.json")) + [
        repo / "boundary.lock.json"
    ]:
        if not path.exists():
            continue
        value = _json(path, errors)
        if isinstance(value, dict) and value.get("$schema") != expected_lock_schema:
            errors.append(
                f"{path.relative_to(repo)} $schema must be {expected_lock_schema!r}"
            )

    required_source_urls = {
        "src/boundver/core.py": expected_config_schema,
        "src/boundver/_lockfile.py": expected_lock_schema,
    }
    for relative, expected in required_source_urls.items():
        try:
            text = _read_text(repo / relative)
        except (OSError, UnicodeError) as exc:
            errors.append(f"{relative}: cannot verify generated schema URL: {exc}")
            continue
        if expected not in text:
            errors.append(f"{relative}: must generate release-pinned schema URL {expected!r}")

    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate versioned documentation and package release surfaces."
    )
    parser.add_argument("--tag", required=True, help="Exact vMAJOR.MINOR.PATCH tag")
    parser.add_argument("--repo", type=Path, default=Path("."))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    errors = readiness_errors(args.repo, args.tag)
    if errors:
        print(
            f"Release surface validation failed ({len(errors)} issue(s)):",
            file=sys.stderr,
        )
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"Release surfaces are ready for {args.tag}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
