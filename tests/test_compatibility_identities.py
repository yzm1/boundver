from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess

import pytest

from boundver._config import validate_config
from boundver._lockfile import (
    generate_lockfile,
    semantic_config_digest,
    verify_lockfile,
)
from boundver._utils import ConfigError
from tests._repo_fixtures import init_git_repo


ROOT = Path(__file__).resolve().parents[1]


def _config() -> dict:
    return {
        "project": "compat-identities",
        "components": {
            "package": {
                "path": "package",
                "version_source": {"file": "package.json", "field": "version"},
                "boundary": {"provider": "leaf", "paths": []},
            },
            "contract": {
                "path": "contract",
                "version_source": {"component": "package"},
                "boundary": {
                    "provider": "path-hash",
                    "paths": ["schema.sql"],
                },
            },
            "fixed": {
                "path": "fixed",
                "version_source": {"constant": "9.8.7"},
                "boundary": {"provider": "leaf", "paths": []},
            },
        },
        "slices": {},
    }


def _repository(root: Path) -> dict:
    init_git_repo(root, initial_branch="main")
    (root / "package").mkdir()
    (root / "package" / "package.json").write_text(
        '{"version":"1.2.3"}\n',
        encoding="utf-8",
    )
    (root / "contract").mkdir()
    (root / "contract" / "schema.sql").write_text(
        "create table example (id integer);\n",
        encoding="utf-8",
    )
    (root / "fixed").mkdir()
    (root / "fixed" / "README.md").write_text("fixed\n", encoding="utf-8")
    config = _config()
    (root / "boundary.config.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "--all"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "compat fixture"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return config


def test_constant_and_component_identities_generate_and_verify(tmp_path: Path) -> None:
    config = _repository(tmp_path)

    lock = generate_lockfile(config, tmp_path, source="head")

    assert lock["components"]["package"]["version"] == "1.2.3"
    assert lock["components"]["contract"]["version"] == "1.2.3"
    assert lock["components"]["fixed"]["version"] == "9.8.7"
    assert lock["components"]["package"]["semver"]["compat_family"] == "1"
    assert lock["components"]["contract"]["semver"]["compat_family"] == "1"
    assert lock["components"]["fixed"]["semver"]["exact_version"] == "9.8.7"
    assert verify_lockfile(config, lock, tmp_path, source="head") == []


def test_inherited_compatibility_participates_in_slices_and_consumer_impact(
    tmp_path: Path,
) -> None:
    config = _repository(tmp_path)
    config["components"]["contract"]["consumers"] = ["fixed"]
    config["slices"]["release-family"] = {
        "mode": "compat",
        "components": ["package", "contract", "fixed"],
    }
    lock = generate_lockfile(config, tmp_path, source="head")
    (tmp_path / "package" / "package.json").write_text(
        '{"version":"2.0.0"}\n',
        encoding="utf-8",
    )
    impact: list[dict] = []

    issues = verify_lockfile(
        config,
        lock,
        tmp_path,
        source="working-tree",
        consumer_impact=impact,
    )

    assert lock["slices"]["release-family"]["fingerprint"] is not None
    assert any("MISMATCH package.compat" in issue for issue in issues)
    assert any("MISMATCH contract.compat" in issue for issue in issues)
    assert any("SLICE MISMATCH release-family.compat" in issue for issue in issues)
    assert any(
        row["component"] == "contract" and row["components"] == ["fixed"]
        for row in impact
    )


def test_changing_a_constant_does_not_change_content_facets(tmp_path: Path) -> None:
    config = _repository(tmp_path)
    before = generate_lockfile(config, tmp_path, source="head")
    changed = copy.deepcopy(config)
    changed["components"]["fixed"]["version_source"] = {"constant": "10.0.0"}

    after = generate_lockfile(changed, tmp_path, source="head")

    before_fixed = before["components"]["fixed"]
    after_fixed = after["components"]["fixed"]
    assert before_fixed["fingerprints"]["exact"] == after_fixed["fingerprints"]["exact"]
    assert (
        before_fixed["fingerprints"]["boundary"]
        == after_fixed["fingerprints"]["boundary"]
    )
    assert before_fixed["fingerprints"]["compat"] != after_fixed["fingerprints"]["compat"]


def test_inheritance_is_transitive_and_uses_the_selected_source(tmp_path: Path) -> None:
    config = _repository(tmp_path)
    config["components"]["fixed"]["version_source"] = {"component": "contract"}
    (tmp_path / "boundary.config.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "boundary.config.json"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "inherit transitively"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    original = (tmp_path / "package" / "package.json").read_text(encoding="utf-8")
    (tmp_path / "package" / "package.json").write_text(
        '{"version":"2.0.0"}\n',
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "package/package.json"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "package" / "package.json").write_text(original, encoding="utf-8")

    head = generate_lockfile(config, tmp_path, source="head")
    index = generate_lockfile(config, tmp_path, source="index")
    working = generate_lockfile(config, tmp_path, source="working-tree")

    assert {entry["version"] for entry in head["components"].values()} == {"1.2.3"}
    assert {entry["version"] for entry in index["components"].values()} == {"2.0.0"}
    assert {entry["version"] for entry in working["components"].values()} == {
        "1.2.3"
    }


@pytest.mark.parametrize(
    ("sources", "expected"),
    (
        (
            {"a": {"component": "missing"}},
            "references unknown component",
        ),
        (
            {"a": {"component": "a"}},
            "must not reference itself",
        ),
        (
            {"a": {"component": "b"}, "b": {"component": "a"}},
            "inheritance cycle",
        ),
        (
            {"a": {"constant": "01.2.3"}},
            "is not valid SemVer",
        ),
        (
            {"a": {"constant": "1.2.3", "component": "b"}},
            "Unknown field in component 'a' version_source: constant",
        ),
    ),
)
def test_invalid_compatibility_identity_declarations_fail_closed(
    tmp_path: Path,
    sources: dict,
    expected: str,
) -> None:
    config = {
        "project": "invalid",
        "components": {
            name: {
                "path": name,
                "version_source": source,
                "boundary": {"provider": "leaf", "paths": []},
            }
            for name, source in sources.items()
        },
        "slices": {},
    }

    errors = validate_config(config, tmp_path, validate_provider_runtime=False)

    assert any(expected in error for error in errors), errors


def test_inheriting_an_unversioned_component_fails_strict_generation(
    tmp_path: Path,
) -> None:
    config = _repository(tmp_path)
    config["components"]["package"]["version_source"] = None

    with pytest.raises(ConfigError, match="did not produce a version"):
        generate_lockfile(config, tmp_path, source="head")


def test_compatibility_identity_declarations_are_semantic() -> None:
    config = _config()
    changed_target = copy.deepcopy(config)
    changed_target["components"]["contract"]["version_source"] = {
        "component": "fixed"
    }
    changed_constant = copy.deepcopy(config)
    changed_constant["components"]["fixed"]["version_source"] = {
        "constant": "9.9.0"
    }

    assert semantic_config_digest(config) != semantic_config_digest(changed_target)
    assert semantic_config_digest(config) != semantic_config_digest(changed_constant)


def test_new_version_source_forms_conform_to_the_published_config_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ROOT / "boundary.config.schema.json").read_text(encoding="utf-8")
    )

    jsonschema.validate(_config(), schema)
