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
        f"{TAG}/spec/boundary.lock.schema.json"
    )
    (repo / "pyproject.toml").write_text(
        """[project]
name = 'boundver'
version = '1.2.3'

[project.urls]
Homepage = 'https://github.com/yzm1/boundver'
Documentation = 'https://github.com/yzm1/boundver/tree/main/docs'
Changelog = 'https://github.com/yzm1/boundver/blob/main/CHANGELOG.md'
Issues = 'https://github.com/yzm1/boundver/issues'
Repository = 'https://github.com/yzm1/boundver'
'GitHub Action' = 'https://github.com/marketplace/actions/boundver'
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
docs/RELEASING.md
{config_schema_url}
""",
        encoding="utf-8",
    )
    for name in ("getting-started.md", "ci-cookbook.md", "WHY_BOUNDVER.md"):
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
        "### Fixed\n\n- Exact release notes.\n"
    )


def test_readiness_uses_pre_tag_changelog_mode(tmp_path: Path) -> None:
    _write_minimal_project(
        tmp_path,
        _changelog("### Added\n\n- Work that must move into the release.\n\n"),
    )

    errors = release_readiness.readiness_errors(tmp_path, TAG)

    assert any("Unreleased section must be empty" in error for error in errors)


def test_project_review_is_never_read_as_a_release_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_minimal_project(tmp_path, _changelog())
    review = tmp_path / "docs" / "PROJECT_REVIEW.md"
    review.parent.mkdir(exist_ok=True)
    review.write_bytes(b"\xffnot UTF-8")
    original_read_text = release_readiness._read_text

    def guarded_read_text(path: Path) -> str:
        if path.resolve() == review.resolve():
            pytest.fail("docs/PROJECT_REVIEW.md must not be read by readiness")
        return original_read_text(path)

    monkeypatch.setattr(release_readiness, "_read_text", guarded_read_text)

    release_readiness.readiness_errors(tmp_path, TAG)

    assert "docs/PROJECT_REVIEW.md" not in release_readiness.RELEASE_DOCS
    assert review not in set(release_readiness._release_files(tmp_path))


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
