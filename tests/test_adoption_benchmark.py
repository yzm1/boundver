"""The adoption profile must measure valid immutable source operations."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def adoption(tmp_path_factory):
    path = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_adoption.py"
    spec = importlib.util.spec_from_file_location("boundver_adoption_benchmark_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path_factory.mktemp("adoption58")
    module.create_fixture(root)
    return module, root


def test_adoption_fixture_has_real_tags_receipts_and_58_components(adoption):
    module, root = adoption
    config = json.loads((root / "boundary.config.json").read_text(encoding="utf-8"))
    assert len(config["components"]) == 58
    assert len(config["derivations"]) == 10
    versions = {
        key for entry in config["components"].values()
        for key in entry["version_source"] if key != "field"
    }
    assert versions == {"file", "component", "constant", "git_tag_prefix"}
    assert len(module.runtime._run(root, "tag", "--list").splitlines()) == 14
    result = module.measure(root, "verify")
    assert result["ok"] is True
    assert result["issue_count"] == result["observation_count"] == 0


@pytest.mark.parametrize("operation", ["validate-config", "generate", "verify"])
def test_each_profile_reports_separate_work_without_rewriting_lock(adoption, operation):
    module, root = adoption
    lock = (root / "boundary.lock.json").read_bytes()
    result = module.measure(root, operation)
    assert result["ok"] is True
    assert result["git_processes"] > 0
    assert result["wall_seconds"] >= 0
    assert result["phases"][0]["name"] == "capture_snapshot"
    assert (root / "boundary.lock.json").read_bytes() == lock
    if operation == "generate":
        phase = next(row for row in result["phases"] if row["name"] == "generate_fingerprints")
        config = json.loads((root / "boundary.config.json").read_text(encoding="utf-8"))
        # The instrumentation includes path-hash behavior extraction as well
        # as the boundary provider, including leaf's no-boundary result.
        expected = len(config["components"]) + sum(
            bool(entry.get("behavior")) for entry in config["components"].values()
        )
        assert phase["source_work"]["provider_extractions"]["calls"] == expected
    if operation == "validate-config":
        assert "validate_derivations" in {row["name"] for row in result["phases"]}


def test_fixture_size_parameter_does_not_change_legacy_default(adoption):
    from inspect import signature

    assert signature(adoption[0].runtime._write_fixture).parameters["component_count"].default == 20
