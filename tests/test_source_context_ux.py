"""Regression tests for adjacent source scope and consumer presentation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from tests._repo_fixtures import commit_all, init_git_repo


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


def _repository(root: Path) -> None:
    init_git_repo(root, initial_branch="main")
    for name in ("api", "client", "frontend"):
        (root / name).mkdir()
        (root / name / "impl.py").write_text(
            f'COMPONENT = "{name}"\n', encoding="utf-8"
        )
    (root / "api" / "contract.json").write_text(
        '{"version": 1}\n', encoding="utf-8"
    )
    config = {
        "project": "source-context",
        "components": {
            "api": {
                "path": "api",
                "verify_facets": ["boundary"],
                "boundary": {
                    "provider": "path-hash",
                    "paths": ["contract.json"],
                },
                "consumers": ["client"],
                "external_consumers": ["partner-sdk"],
            },
            "client": {
                "path": "client",
                "boundary": {"provider": "leaf", "paths": []},
                "consumers": ["frontend"],
            },
            "frontend": {
                "path": "frontend",
                "boundary": {"provider": "leaf", "paths": []},
            },
        },
        "slices": {},
    }
    (root / "boundary.config.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    commit_all(root, "source fixture")
    generated = _run(root, "generate", "--source", "head")
    assert generated.returncode == 0, generated.stderr
    assert "Source: head" in generated.stdout
    commit_all(root, "record lock")


def test_explain_source_is_per_invocation_across_divergent_views(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    contract = tmp_path / "api" / "contract.json"
    contract.write_text('{"version": 2}\n', encoding="utf-8")

    working_verify = _run(
        tmp_path,
        "verify",
        "--source",
        "working-tree",
        "--facets",
        "boundary",
        "--components",
        "api",
    )
    assert working_verify.returncode == 4, working_verify.stderr
    assert "Source: working-tree | Inputs:" in working_verify.stdout

    default_explain = _run(tmp_path, "explain", "api")
    assert default_explain.returncode == 0, default_explain.stderr
    assert "Source: head" in default_explain.stdout
    assert "No tracked file changes" in default_explain.stdout
    assert "`--source` applies only to this invocation" in default_explain.stdout
    assert "boundver explain COMPONENT --source index" in default_explain.stdout
    assert (
        "boundver explain COMPONENT --source working-tree"
        in default_explain.stdout
    )

    working_explain = _run(
        tmp_path, "explain", "api", "--source", "working-tree"
    )
    assert working_explain.returncode == 0, working_explain.stderr
    assert "Source: working-tree" in working_explain.stdout
    assert "api/contract.json" in working_explain.stdout

    subprocess.run(
        ["git", "add", "api/contract.json"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "api" / "impl.py").write_text(
        'COMPONENT = "api-local"\n', encoding="utf-8"
    )
    index_explain = _run(tmp_path, "explain", "api", "--source", "index")
    assert index_explain.returncode == 0, index_explain.stderr
    assert "Source: index" in index_explain.stdout
    assert "api/contract.json" in index_explain.stdout
    assert "api/impl.py" not in index_explain.stdout

    working_explain = _run(
        tmp_path, "explain", "api", "--source", "working-tree"
    )
    assert working_explain.returncode == 0, working_explain.stderr
    assert "api/contract.json" in working_explain.stdout
    assert "api/impl.py" in working_explain.stdout


def test_status_separates_source_and_declared_consumer_edges(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)

    status = _run(tmp_path, "status", "--source", "working-tree")

    assert status.returncode == 0, status.stderr
    assert "Source: working-tree" in status.stdout
    assert "Declared consumer edges (recorded in lock):" in status.stdout
    assert "api -> components: client" in status.stdout
    assert "api -> external consumers: partner-sdk" in status.stdout
    assert "client -> components: frontend" in status.stdout


def test_text_consumer_impact_matches_typed_json_groups(tmp_path: Path) -> None:
    _repository(tmp_path)
    (tmp_path / "api" / "contract.json").write_text(
        '{"version": 2}\n', encoding="utf-8"
    )

    text_result = _run(
        tmp_path,
        "verify",
        "--source",
        "working-tree",
        "--facets",
        "boundary",
        "--components",
        "api",
        "--transitive",
    )
    assert text_result.returncode == 4, text_result.stderr
    assert "Consumer impact:" in text_result.stdout
    assert "api [boundary; transitive]" in text_result.stdout
    assert "Components: client, frontend" in text_result.stdout
    assert "External consumers: partner-sdk" in text_result.stdout

    json_result = _run(
        tmp_path,
        "verify",
        "--source",
        "working-tree",
        "--facets",
        "boundary",
        "--components",
        "api",
        "--transitive",
        "--format",
        "json",
    )
    assert json_result.returncode == 4, json_result.stderr
    impact = json.loads(json_result.stdout)["consumer_impact"]
    assert impact == [
        {
            "component": "api",
            "facets": ["boundary"],
            "components": ["client", "frontend"],
            "external_consumers": ["partner-sdk"],
            "transitive": True,
        }
    ]

    why = _run(
        tmp_path,
        "why",
        "api",
        "--source",
        "working-tree",
        "--transitive",
    )
    assert why.returncode == 1, why.stderr
    assert "Source:     working-tree" in why.stdout
    assert "Consumer impact:" in why.stdout
    assert "api [boundary; transitive]" in why.stdout
    assert "Components: client, frontend" in why.stdout
    assert "External consumers: partner-sdk" in why.stdout
