"""Regressions from the 0.13.0 monorepo field report."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from boundver._config import validate_config
from boundver._discovery import normalize_discovery_exclusions
from boundver._lockfile import _lockfile_structure_issues
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


def _single_component_repo(root: Path) -> None:
    init_git_repo(root, initial_branch="main")
    component = root / "svc"
    component.mkdir()
    (component / "api.json").write_text('{"contract": 1}\n', encoding="utf-8")
    (component / "impl.py").write_text("VALUE = 1\n", encoding="utf-8")
    config = {
        "project": "field-report",
        "components": {
            "svc": {
                "path": "svc",
                "verify_facets": ["boundary"],
                "boundary": {"provider": "path-hash", "paths": ["api.json"]},
            }
        },
    }
    (root / "boundary.config.json").write_text(
        json.dumps(config) + "\n",
        encoding="utf-8",
    )
    commit_all(root, "initial source")
    generated = _run(root, "generate", "--source", "head")
    assert generated.returncode == 0, generated.stderr
    commit_all(root, "record boundary lock")


def test_head_verify_names_the_snapshot_backed_lock(tmp_path: Path) -> None:
    _single_component_repo(tmp_path)
    disk_lock = json.loads((tmp_path / "boundary.lock.json").read_text())
    disk_lock["components"]["svc"]["fingerprints"]["exact"] = "f" * 64
    (tmp_path / "boundary.lock.json").write_text(
        json.dumps(disk_lock) + "\n",
        encoding="utf-8",
    )

    verified = _run(
        tmp_path,
        "verify",
        "--source",
        "head",
        "--lock",
        "boundary.lock.json",
        "--format",
        "json",
    )

    assert verified.returncode == 0, verified.stderr
    payload = json.loads(verified.stdout)
    assert payload["inputs"]["source"] == "head"
    assert payload["inputs"]["lock"].startswith("HEAD@")
    assert payload["inputs"]["lock"].endswith(":boundary.lock.json")
    assert payload["inputs"]["tree"]
    assert payload["inputs"]["commit"]

    text_result = _run(
        tmp_path,
        "verify",
        "--source",
        "head",
        "--lock",
        "boundary.lock.json",
    )
    assert text_result.returncode == 0, text_result.stderr
    assert "Inputs: config=HEAD@" in text_result.stdout
    assert " lock=HEAD@" in text_result.stdout
    assert ":boundary.lock.json" in text_result.stdout


def test_explain_and_why_use_lock_history_and_effective_facets(
    tmp_path: Path,
) -> None:
    _single_component_repo(tmp_path)
    (tmp_path / "svc" / "impl.py").write_text("VALUE = 2\n", encoding="utf-8")
    commit_all(tmp_path, "implementation drift")
    # A later rewrite of the same lock entry models a partial update for an
    # unrelated component. Repository-wide lock mtime/history is now too new;
    # diagnostics must follow this component's unchanged entry farther back.
    lockfile = json.loads((tmp_path / "boundary.lock.json").read_text())
    (tmp_path / "boundary.lock.json").write_text(
        json.dumps(lockfile, indent=4) + "\n",
        encoding="utf-8",
    )
    commit_all(tmp_path, "rewrite lock without updating svc")
    (tmp_path / "unrelated.txt").write_text("later\n", encoding="utf-8")
    commit_all(tmp_path, "unrelated latest commit")

    explained = _run(tmp_path, "explain", "svc", "--source", "head")
    assert explained.returncode == 0, explained.stderr
    assert "svc/impl.py" in explained.stdout
    assert "commit that introduced the current lock entry for svc" in explained.stdout
    assert "Gated facets: boundary" in explained.stdout

    why_text = _run(tmp_path, "why", "svc", "--source", "head")
    assert why_text.returncode == 0, why_text.stderr
    assert "Status: UP TO DATE -- no gated drift detected" in why_text.stdout
    assert "Non-gating drift observed" in why_text.stdout
    assert "svc/impl.py" in why_text.stdout
    assert "Diagnostic base:" in why_text.stdout
    assert "Recommendation:" not in why_text.stdout

    why_json = _run(
        tmp_path,
        "why",
        "svc",
        "--source",
        "head",
        "--format",
        "json",
    )
    assert why_json.returncode == 0, why_json.stderr
    payload = json.loads(why_json.stdout)
    assert payload["observed_drift"] is True
    assert payload["drifted"] is False
    assert payload["gated_facets"] == ["boundary"]
    assert payload["gated_changes"] == []
    assert payload["non_gating_changes"] == ["exact"]
    assert payload["changed_files_status"] == "ok"
    assert payload["changed_files"] == [{"path": "svc/impl.py", "status": "M"}]
    assert "introduced the current lock entry for svc" in payload[
        "diagnostic_base_origin"
    ]

    status = _run(tmp_path, "status", "--source", "head", "--format", "json")
    assert status.returncode == 0, status.stderr
    status_payload = json.loads(status.stdout)
    assert status_payload["issues"] == []
    assert any("MISMATCH svc.exact" in row for row in status_payload["observations"])
    assert status_payload["facet_policy"]["components"]["svc"] == ["boundary"]

    status_text = _run(tmp_path, "status", "--source", "head", "--strict")
    assert status_text.returncode == 0, status_text.stderr
    assert "NON-GATING DRIFT" in status_text.stdout
    assert "MISMATCH svc.exact" in status_text.stdout

    invalid_ref = _run(
        tmp_path,
        "why",
        "svc",
        "--source",
        "head",
        "--base-ref=--output=unexpected.diff",
    )
    assert invalid_ref.returncode == 2
    assert "invalid diagnostic base ref" in invalid_ref.stderr
    assert not (tmp_path / "unexpected.diff").exists()


def test_lock_history_treats_malformed_historical_components_as_no_entry(
    tmp_path: Path,
) -> None:
    _single_component_repo(tmp_path)
    valid_lock = (tmp_path / "boundary.lock.json").read_text(encoding="utf-8")
    (tmp_path / "boundary.lock.json").write_text(
        '{"components":[]}\n',
        encoding="utf-8",
    )
    commit_all(tmp_path, "malformed historical lock")
    (tmp_path / "boundary.lock.json").write_text(valid_lock, encoding="utf-8")
    commit_all(tmp_path, "restore valid lock")
    (tmp_path / "svc" / "impl.py").write_text("VALUE = 2\n", encoding="utf-8")
    commit_all(tmp_path, "drift after restored lock")

    explained = _run(tmp_path, "explain", "svc", "--source", "head")

    assert explained.returncode == 0, explained.stderr
    assert "svc/impl.py" in explained.stdout
    assert "commit that introduced the current lock entry for svc" in explained.stdout


def test_verify_json_exposes_typed_transitive_consumer_impact(
    tmp_path: Path,
) -> None:
    init_git_repo(tmp_path, initial_branch="main")
    for name in ("api", "client", "frontend"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "contract.json").write_text(
            json.dumps({"component": name, "revision": 1}) + "\n",
            encoding="utf-8",
        )
    config = {
        "project": "impact",
        "components": {
            "api": {
                "path": "api",
                "verify_facets": ["boundary"],
                "boundary": {
                    "provider": "path-hash",
                    "paths": ["contract.json"],
                },
                "consumers": ["client"],
                "external_consumers": ["vendor-sdk"],
            },
            "client": {
                "path": "client",
                "verify_facets": ["exact"],
                "boundary": {"provider": "implicit", "paths": []},
                "consumers": ["frontend"],
                "external_consumers": ["partner-app"],
            },
            "frontend": {
                "path": "frontend",
                "verify_facets": ["exact"],
                "boundary": {"provider": "implicit", "paths": []},
            },
        },
    }
    (tmp_path / "boundary.config.json").write_text(
        json.dumps(config) + "\n",
        encoding="utf-8",
    )
    commit_all(tmp_path, "initial graph")
    generated = _run(tmp_path, "generate", "--source", "head")
    assert generated.returncode == 0, generated.stderr
    commit_all(tmp_path, "record lock")
    (tmp_path / "api" / "contract.json").write_text(
        '{"component":"api","revision":2}\n',
        encoding="utf-8",
    )
    commit_all(tmp_path, "change api boundary")

    verified = _run(
        tmp_path,
        "verify",
        "--source",
        "head",
        "--transitive",
        "--format",
        "json",
    )

    assert verified.returncode == 4, verified.stderr
    payload = json.loads(verified.stdout)
    assert payload["consumer_impact"] == [
        {
            "component": "api",
            "facets": ["boundary"],
            "components": ["client", "frontend"],
            "external_consumers": ["partner-app", "vendor-sdk"],
            "transitive": True,
        }
    ]

    fail_fast = _run(
        tmp_path,
        "verify",
        "--source",
        "head",
        "--transitive",
        "--fail-fast",
        "--format",
        "json",
    )
    assert fail_fast.returncode == 4, fail_fast.stderr
    assert json.loads(fail_fast.stdout)["consumer_impact"] == payload[
        "consumer_impact"
    ]


def test_discover_excludes_repeatable_path_prefixes(tmp_path: Path) -> None:
    init_git_repo(tmp_path, initial_branch="main")
    for path in ("apps/keep", "legacy/drop"):
        directory = tmp_path / path
        directory.mkdir(parents=True)
        (directory / "package.json").write_text(
            '{"name":"fixture","version":"1.0.0"}\n',
            encoding="utf-8",
        )
    commit_all(tmp_path, "tracked manifests")

    discovered = _run(
        tmp_path,
        "discover",
        "--exclude",
        "legacy/",
        "--format",
        "json",
    )

    assert discovered.returncode == 0, discovered.stderr
    payload = json.loads(discovered.stdout)
    assert payload["excluded"] == ["legacy"]
    assert {component["path"] for component in payload["components"].values()} == {
        "apps/keep"
    }

    invalid = _run(tmp_path, "discover", "--exclude", "../outside")
    assert invalid.returncode == 2
    assert "Invalid discovery exclusion" in invalid.stderr

    glob = _run(tmp_path, "discover", "--exclude", "legacy/*")
    assert glob.returncode == 2
    assert "glob syntax is not supported" in glob.stderr

    with pytest.raises(ValueError, match="1000-path limit"):
        normalize_discovery_exclusions(["legacy"] * 1_001)


def test_validate_config_preflights_only_providers_that_need_yaml(
    tmp_path: Path,
) -> None:
    component = tmp_path / "svc"
    component.mkdir()
    (component / "openapi.yaml").write_text(
        "openapi: 3.1.0\npaths: {}\n",
        encoding="utf-8",
    )
    (component / "openapi.json").write_text(
        '{"openapi":"3.1.0","paths":{}}\n',
        encoding="utf-8",
    )
    canonical = {
        "project": "dependencies",
        "components": {
            "svc": {
                "path": "svc",
                "boundary": {
                    "provider": "openapi-canonical",
                    "paths": ["openapi.yaml"],
                },
            }
        },
    }
    real_import = importlib.import_module

    def missing_yaml(name: str, package: str | None = None):
        if name == "yaml":
            raise ModuleNotFoundError("No module named 'yaml'")
        return real_import(name, package)

    with patch("boundver.providers.importlib.import_module", side_effect=missing_yaml):
        canonical_errors = validate_config(canonical, tmp_path)
        raw = json.loads(json.dumps(canonical))
        raw["components"]["svc"]["boundary"]["provider"] = "openapi"
        raw_errors = validate_config(raw, tmp_path)
        canonical_json = json.loads(json.dumps(canonical))
        canonical_json["components"]["svc"]["boundary"]["paths"] = [
            "openapi.json"
        ]
        canonical_json_errors = validate_config(canonical_json, tmp_path)

    assert any("install `boundver[yaml]`" in error for error in canonical_errors)
    assert not any("boundver[yaml]" in error for error in raw_errors)
    assert not any("boundver[yaml]" in error for error in canonical_json_errors)


def test_contract_mismatch_names_upgrade_direction() -> None:
    newer = _lockfile_structure_issues(
        {
            "schema": "boundary-lock/v3",
            "config_contract": "boundver-semantic-config/v3",
        },
        running_version="0.13.0",
    )
    older = _lockfile_structure_issues(
        {
            "schema": "boundary-lock/v3",
            "config_contract": "boundver-semantic-config/v1",
        },
        running_version="0.13.0",
    )

    assert len(newer) == 1
    assert "newer semantic contract" in newer[0]
    assert "upgrade the installation" in newer[0]
    assert len(older) == 1
    assert "older semantic contract" in older[0]
    assert "regenerate" in older[0]

    oversized_future = _lockfile_structure_issues(
        {
            "schema": "boundary-lock/v3",
            "config_contract": "boundver-semantic-config/v" + ("9" * 10_000),
        },
        running_version="0.13.0",
    )
    assert len(oversized_future) == 1
    assert "newer semantic contract" in oversized_future[0]


def test_versioned_leaf_supplies_compat_but_not_boundary(tmp_path: Path) -> None:
    init_git_repo(tmp_path, initial_branch="main")
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    package = frontend / "package.json"
    package.write_text(
        '{"name":"frontend","version":"1.2.3"}\n',
        encoding="utf-8",
    )
    config = {
        "project": "versioned-leaf",
        "components": {
            "frontend": {
                "path": "frontend",
                "version_source": {"file": "package.json", "field": "version"},
                "boundary": {"provider": "leaf", "paths": []},
                "verify_facets": ["compat"],
            }
        },
    }
    (tmp_path / "boundary.config.json").write_text(
        json.dumps(config) + "\n",
        encoding="utf-8",
    )
    commit_all(tmp_path, "add versioned leaf")

    validated = _run(tmp_path, "validate-config")
    assert validated.returncode == 0, validated.stderr
    generated = _run(tmp_path, "generate", "--source", "head")
    assert generated.returncode == 0, generated.stderr
    entry = json.loads((tmp_path / "boundary.lock.json").read_text())["components"][
        "frontend"
    ]
    assert entry["fingerprints"]["boundary"] is None
    assert entry["fingerprints"]["compat"] is not None
    commit_all(tmp_path, "record versioned leaf")

    package.write_text(
        '{"name":"frontend","version":"2.0.0"}\n',
        encoding="utf-8",
    )
    commit_all(tmp_path, "change leaf compatibility family")
    verified = _run(tmp_path, "verify", "--source", "head")

    assert verified.returncode == 5, verified.stderr
    assert "MISMATCH frontend.compat" in verified.stdout
    assert "MISMATCH frontend.boundary" not in verified.stdout
