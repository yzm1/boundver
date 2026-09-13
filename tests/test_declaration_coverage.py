"""Declaration coverage reports omissions without changing lock semantics."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from boundver._config import validate_config
from boundver._lockfile import generate_lockfile, semantic_config_digest
from tests._repo_fixtures import init_git_repo


ROOT = Path(__file__).resolve().parents[1]


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "boundver", *arguments],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
    )


def _base_config() -> dict:
    return {
        "project": "coverage-fixture",
        "components": {
            "api": {
                "path": "services/api",
                "boundary": {
                    "provider": "path-hash",
                    "paths": ["contract.json", "generated/openapi.yaml"],
                },
                "behavior": {
                    "paths": [
                        "contract.json",
                        "generated/openapi.yaml",
                        "behavior.yaml",
                    ]
                },
                "vendored_copies": ["vendor/api"],
            },
            "client": {
                "path": "services/api/client",
                "boundary": {"provider": "leaf", "paths": []},
            },
        },
        "slices": {},
    }


def _coverage_policy() -> dict:
    return {
        "source_indicators": ["**/*.py", "**/*.ts"],
        "exclusions": [
            {
                "paths": ["services/api/internal.py"],
                "facets": ["boundary"],
                "reason": "implementation is not part of the API",
            },
            {
                "paths": ["services/api/client/**"],
                "facets": ["behavior", "boundary"],
                "reason": "owned by the nested client component",
            },
            {
                "paths": ["tools/generated.py"],
                "facets": ["ownership"],
                "reason": "generated build helper",
            },
        ],
    }


def _repository(root: Path) -> dict:
    init_git_repo(root, initial_branch="main")
    for directory in (
        "services/api/client",
        "services/api/generated",
        "vendor/api",
        "tools",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
    files = {
        "services/api/contract.json": "{}\n",
        "services/api/behavior.yaml": "enabled: true\n",
        "services/api/internal.py": "VALUE = 1\n",
        "services/api/generated/openapi.yaml": "openapi: 3.0.0\n",
        "services/api/client/sdk.ts": "export const v = 1;\n",
        "vendor/api/copy.py": "VALUE = 1\n",
        "tools/check.py": "print('check')\n",
        "tools/generated.py": "print('generated')\n",
        "ignored.py": "print('untracked')\n",
        "tracked-ignored.py": "print('tracked')\n",
    }
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    (root / ".gitignore").write_text(
        "ignored.py\ntracked-ignored.py\n",
        encoding="utf-8",
    )
    config = _base_config()
    config["coverage"] = _coverage_policy()
    (root / "boundary.config.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "-f", "tracked-ignored.py"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "coverage fixture"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return config


def _facet(report: dict, component: str, facet: str) -> dict:
    component_row = next(
        row for row in report["components"] if row["component"] == component
    )
    return next(row for row in component_row["facets"] if row["facet"] == facet)


def test_coverage_groups_selector_and_ownership_omissions_with_exclusions(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)

    result = _run(tmp_path, "coverage", "--source", "head", "--format", "json")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["schema"] == "boundver-declaration-coverage/v1"
    assert report["complete"] is True
    assert report["ok"] is False
    assert report["inputs"]["source"] == "head"
    assert report["inputs"]["config"].startswith("HEAD@")

    behavior = _facet(report, "api", "behavior")
    assert "services/api/internal.py" in behavior["uncovered_files"]
    assert behavior["declaration"] == "/components/api/behavior/paths"
    assert any(
        item["path"] == "services/api/client/sdk.ts"
        and item["reason"] == "owned by the nested client component"
        for item in behavior["excluded"]
    )

    boundary = _facet(report, "api", "boundary")
    assert "services/api/behavior.yaml" in boundary["uncovered_files"]
    assert any(
        item["path"] == "services/api/internal.py"
        and item["declaration"] == "/coverage/exclusions/0"
        for item in boundary["excluded"]
    )
    assert "services/api/generated/openapi.yaml" not in boundary["uncovered_files"]

    assert "client" in report["complete_components"]
    ownership = {
        row["directory"]: row for row in report["unowned_source_directories"]
    }
    assert ownership["tools"]["uncovered_files"] == ["tools/check.py"]
    assert ownership["tools"]["excluded"] == [
        {
            "path": "tools/generated.py",
            "reason": "generated build helper",
            "declaration": "/coverage/exclusions/2",
        }
    ]
    all_reported = result.stdout
    assert "vendor/api/copy.py" not in all_reported
    assert ownership["."]["uncovered_files"] == ["tracked-ignored.py"]
    assert "ignored.py" not in {
        path
        for row in report["unowned_source_directories"]
        for path in row["uncovered_files"]
    }

    strict = _run(tmp_path, "coverage", "--source", "head", "--strict")
    assert strict.returncode == 1
    assert "UNCOVERED services/api/internal.py" in strict.stdout
    assert "EXCLUDED services/api/internal.py" in strict.stdout
    assert "Declaration coverage:" in strict.stdout
    assert "\\nDeclaration coverage:" not in strict.stdout


def test_coverage_policy_is_digest_neutral_and_command_is_read_only(
    tmp_path: Path,
) -> None:
    config = _repository(tmp_path)
    without_policy = copy.deepcopy(config)
    without_policy.pop("coverage")
    assert semantic_config_digest(config) == semantic_config_digest(without_policy)
    generation_config = copy.deepcopy(config)
    generation_config["components"]["api"].pop("vendored_copies")
    generation_without_policy = copy.deepcopy(generation_config)
    generation_without_policy.pop("coverage")
    assert generate_lockfile(
        generation_config, tmp_path, source="head"
    ) == generate_lockfile(
        generation_without_policy,
        tmp_path,
        source="head",
    )
    before = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    result = _run(tmp_path, "coverage", "--source", "head")

    assert result.returncode == 0, result.stderr
    after = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert after == before


def test_coverage_uses_each_selected_source_path_set(tmp_path: Path) -> None:
    config = _repository(tmp_path)
    config["coverage"]["exclusions"].append(
        {
            "paths": ["services/api/index-only.py"],
            "facets": ["behavior", "boundary"],
            "reason": "source-view fixture",
        }
    )
    (tmp_path / "boundary.config.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "services/api/index-only.py").write_text(
        "INDEX = True\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "add", "boundary.config.json", "services/api/index-only.py"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "services/api/index-only.py").unlink()

    head = json.loads(
        _run(tmp_path, "coverage", "--source", "head", "--format", "json").stdout
    )
    index = json.loads(
        _run(tmp_path, "coverage", "--source", "index", "--format", "json").stdout
    )
    working = json.loads(
        _run(
            tmp_path,
            "coverage",
            "--source",
            "working-tree",
            "--format",
            "json",
        ).stdout
    )

    assert head["summary"]["tracked_files"] + 1 == index["summary"]["tracked_files"]
    assert working["summary"]["tracked_files"] == head["summary"]["tracked_files"]
    assert head["inputs"]["source"] == "head"
    assert index["inputs"]["source"] == "index"
    assert working["inputs"] == {
        "source": "working-tree",
        "tree": None,
        "commit": None,
        "config": "WORKING-TREE:boundary.config.json",
    }


def test_tracked_git_symlink_is_covered_as_a_path_without_following_it(
    tmp_path: Path,
) -> None:
    config = _repository(tmp_path)
    target = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=tmp_path,
        input=b"../../outside.py",
        check=True,
        capture_output=True,
    ).stdout.decode("ascii").strip()
    subprocess.run(
        [
            "git",
            "update-index",
            "--add",
            "--cacheinfo",
            f"120000,{target},services/api/link.py",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    config["coverage"]["exclusions"].append(
        {
            "paths": ["services/api/link.py"],
            "facets": ["behavior", "boundary"],
            "reason": "opaque link fixture",
        }
    )
    (tmp_path / "boundary.config.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "add", "boundary.config.json"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add link identity"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    result = _run(tmp_path, "coverage", "--source", "head", "--format", "json")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    behavior = _facet(report, "api", "behavior")
    assert any(item["path"] == "services/api/link.py" for item in behavior["excluded"])
    assert "outside.py" not in result.stdout


@pytest.mark.parametrize(
    ("mutate", "expected"),
    (
        (
            lambda policy: policy["exclusions"][0].update({"reason": " "}),
            "reason must be a non-empty string",
        ),
        (
            lambda policy: policy["exclusions"][0].update({"facets": ["exact"]}),
            "contains unknown coverage facets: exact",
        ),
        (
            lambda policy: policy.update({"source_indicators": ["../escape.py"]}),
            "is not a safe repository-relative selector",
        ),
    ),
)
def test_coverage_policy_validation_fails_closed(
    tmp_path: Path,
    mutate,
    expected: str,
) -> None:
    _repository(tmp_path)
    config = _base_config()
    policy = _coverage_policy()
    mutate(policy)
    config["coverage"] = policy

    errors = validate_config(config, tmp_path, source="working-tree")

    assert any(expected in error for error in errors), errors


def test_coverage_json_conforms_to_its_versioned_schema(tmp_path: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    _repository(tmp_path)
    result = _run(tmp_path, "coverage", "--source", "head", "--format", "json")
    assert result.returncode == 0, result.stderr
    schema = json.loads(
        (ROOT / "spec" / "cli-output.coverage.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.validate(json.loads(result.stdout), schema)
