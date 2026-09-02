from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
TAG = "v1.2.3"


def _load_script(name: str):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


release_changelog = _load_script("release_changelog")
release_readiness = _load_script("verify_release_readiness")


def _changelog(unreleased: str = "") -> str:
    return f"""# Changelog

## [Unreleased]

{unreleased}## [1.2.3] - 2026-08-18

### Upgrade contract

- Semantic config: `boundver-semantic-config/v2`
- Lock schema: `boundary-lock/v3`
- Fingerprint compatibility: `digest-neutral`
- Lock regeneration: `not-required`

### Fixed

- Exact release notes.

## [1.2.2] - 2026-08-01

### Fixed

- Previous release.

[Unreleased]: https://github.com/yzm1/boundver/compare/v1.2.3...HEAD
[1.2.3]: https://github.com/yzm1/boundver/compare/v1.2.2...v1.2.3
"""


def _write_minimal_project(repo: Path, changelog: str) -> None:
    config_schema_url = (
        "https://raw.githubusercontent.com/yzm1/boundver/"
        f"{TAG}/boundary.config.schema.json"
    )
    lock_schema_url = (
        "https://raw.githubusercontent.com/yzm1/boundver/"
        "v0.13.0/spec/boundary.lock.schema.json"
    )
    (repo / "pyproject.toml").write_text(
        """[project]
name = 'boundver'
version = '1.2.3'

[project.urls]
Homepage = 'https://github.com/yzm1/boundver'
Documentation = 'https://yzm1.github.io/boundver/'
Changelog = 'https://github.com/yzm1/boundver/blob/main/CHANGELOG.md'
Issues = 'https://github.com/yzm1/boundver/issues'
Repository = 'https://github.com/yzm1/boundver'
Community = 'https://github.com/yzm1/boundver/discussions/100'
'GitHub Action' = 'https://github.com/marketplace/actions/boundver'
Container = 'https://github.com/yzm1/boundver/pkgs/container/boundver'
Homebrew = 'https://github.com/yzm1/homebrew-boundver'
'GitLab CI/CD Catalog' = 'https://gitlab.com/boundver-project/boundver'
""",
        encoding="utf-8",
    )
    (repo / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    docs = repo / "docs"
    docs.mkdir()
    (repo / "README.md").write_text(
        f"""yzm1/boundver@{TAG}
https://pypi.org/project/boundver/
https://github.com/marketplace/actions/boundver
https://yzm1.github.io/boundver/
https://yzm1.github.io/boundver/assets/logo.png
ghcr.io/yzm1/boundver:1.2.3
brew install yzm1/boundver/boundver
https://yzm1.github.io/boundver/assets/verify-demo.svg
docs/RELEASING.md
{config_schema_url}
""",
        encoding="utf-8",
    )
    for name in (
        "index.md",
        "demo.md",
        "getting-started.md",
        "ci-cookbook.md",
        "comparison.md",
        "distribution.md",
        "WHY_BOUNDVER.md",
    ):
        (docs / name).write_text("Release guide.\n", encoding="utf-8")
    config_schema = json.dumps({"$id": config_schema_url}) + "\n"
    (repo / "boundary.config.schema.json").write_text(
        config_schema, encoding="utf-8"
    )
    package = repo / "src" / "boundver"
    package.mkdir(parents=True)
    (package / "boundary.config.schema.json").write_text(
        config_schema, encoding="utf-8"
    )
    (package / "core.py").write_text(config_schema_url + "\n", encoding="utf-8")
    (package / "_lockfile.py").write_text(
        lock_schema_url + "\n", encoding="utf-8"
    )
    spec = repo / "spec"
    spec.mkdir()
    (spec / "boundary.lock.schema.json").write_text(
        json.dumps(
            {
                "$id": lock_schema_url,
                "properties": {
                    "schema": {"const": "boundary-lock/v3"},
                    "config_contract": {
                        "const": "boundver-semantic-config/v2"
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    for schema_name in (
        "cli-output.migrate-lock.schema.json",
        "verify-baseline.schema.json",
    ):
        schema_url = (
            "https://raw.githubusercontent.com/yzm1/boundver/"
            f"{TAG}/spec/{schema_name}"
        )
        (spec / schema_name).write_text(
            json.dumps({"$id": schema_url}) + "\n", encoding="utf-8"
        )
    (repo / "boundary.lock.json").write_text(
        json.dumps(
            {
                "$schema": lock_schema_url,
                "schema": "boundary-lock/v3",
                "config_contract": "boundver-semantic-config/v2",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_pre_tag_mode_rejects_substantive_unreleased_notes() -> None:
    changelog = _changelog("### Added\n\n- Work after the release.\n\n")

    with pytest.raises(ValueError, match="Unreleased section must be empty"):
        release_changelog.extract_release_notes(changelog, TAG, mode="pre-tag")


def test_post_release_mode_retains_historical_note_extraction() -> None:
    changelog = _changelog("### Added\n\n- Work after the release.\n\n")

    assert release_changelog.extract_release_notes(changelog, TAG) == (
        "### Upgrade contract\n\n"
        "- Semantic config: `boundver-semantic-config/v2`\n"
        "- Lock schema: `boundary-lock/v3`\n"
        "- Fingerprint compatibility: `digest-neutral`\n"
        "- Lock regeneration: `not-required`\n\n"
        "### Fixed\n\n- Exact release notes.\n"
    )


def test_release_version_threshold_does_not_parse_untrusted_large_integers() -> None:
    assert release_changelog.version_at_least(("0", "14", "0"), (0, 14, 0))
    assert not release_changelog.version_at_least(("0", "13", "999"), (0, 14, 0))
    assert release_changelog.version_at_least(
        ("9" * 10_000, "0", "0"),
        (0, 14, 0),
    )


def test_upgrade_contract_is_required_and_matches_public_lock_contracts(
    tmp_path: Path,
) -> None:
    _write_minimal_project(tmp_path, _changelog())

    assert release_readiness.readiness_errors(tmp_path, TAG) == []

    missing = _changelog().replace(
        "### Upgrade contract\n\n"
        "- Semantic config: `boundver-semantic-config/v2`\n"
        "- Lock schema: `boundary-lock/v3`\n"
        "- Fingerprint compatibility: `digest-neutral`\n"
        "- Lock regeneration: `not-required`\n\n",
        "",
    )
    with pytest.raises(ValueError, match="Upgrade contract"):
        release_changelog.extract_release_notes(missing, TAG)

    mismatched = _changelog().replace(
        "- Semantic config: `boundver-semantic-config/v2`",
        "- Semantic config: `boundver-semantic-config/v9`",
    )
    (tmp_path / "CHANGELOG.md").write_text(mismatched, encoding="utf-8")
    errors = release_readiness.readiness_errors(tmp_path, TAG)
    assert any("semantic config must be" in error for error in errors), errors

    misplaced = _changelog().replace(
        "### Upgrade contract",
        "Introductory release prose.\n\n### Upgrade contract",
        1,
    )
    with pytest.raises(ValueError, match="must start"):
        release_changelog.extract_release_notes(misplaced, TAG)


def test_readiness_uses_pre_tag_changelog_mode(tmp_path: Path) -> None:
    _write_minimal_project(
        tmp_path,
        _changelog("### Added\n\n- Work that must move into the release.\n\n"),
    )

    errors = release_readiness.readiness_errors(tmp_path, TAG)

    assert any("Unreleased section must be empty" in error for error in errors)


def test_readiness_rejects_stale_lock_contracts(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path, _changelog())
    lock_schema_url = (
        "https://raw.githubusercontent.com/yzm1/boundver/"
        f"{TAG}/spec/boundary.lock.schema.json"
    )
    spec = tmp_path / "spec"
    spec.mkdir(exist_ok=True)
    (spec / "boundary.lock.schema.json").write_text(
        json.dumps(
            {
                "$id": lock_schema_url,
                "properties": {
                    "schema": {"const": "boundary-lock/v3"},
                    "config_contract": {
                        "const": "boundver-semantic-config/v2"
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "boundary.lock.json").write_text(
        json.dumps(
            {
                "$schema": lock_schema_url,
                "schema": "boundary-lock/v2",
                "config_contract": "boundver-semantic-config/v1",
            }
        ),
        encoding="utf-8",
    )

    errors = release_readiness.readiness_errors(tmp_path, TAG)

    assert any(
        "boundary.lock.json schema must be 'boundary-lock/v3'" in error
        for error in errors
    ), errors
    assert any(
        "boundary.lock.json config_contract must be "
        "'boundver-semantic-config/v2'" in error
        for error in errors
    ), errors


def test_readiness_rejects_stale_new_public_schema_ids(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path, _changelog())
    schema_path = tmp_path / "spec" / "verify-baseline.schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "$id": (
                    "https://raw.githubusercontent.com/yzm1/boundver/"
                    "v0.11.0/spec/verify-baseline.schema.json"
                )
            }
        ),
        encoding="utf-8",
    )

    errors = release_readiness.readiness_errors(tmp_path, TAG)

    assert any(
        "verify-baseline.schema.json $id must be" in error
        for error in errors
    ), errors
