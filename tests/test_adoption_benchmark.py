"""The adoption profile must measure valid immutable source operations."""

import importlib.util
import hashlib
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
    assert signature(adoption[0].create_fixture).parameters["owned_files"].default == 0


@pytest.mark.parametrize("count,payload", [(-1, 0), (8001, 0), (1, -1), (1, 65537),
                                        (8000, 65536), (4096, 65505), (True, 1)])
def test_extra_payload_is_refused_before_fixture_creation(adoption, tmp_path, count, payload):
    root = tmp_path / "not-created"
    with pytest.raises(ValueError, match="Extra files"):
        adoption[0].create_fixture(root, 6, owned_files=count, owned_payload_bytes=payload)
    assert not root.exists()


@pytest.mark.parametrize("count,payload", [(0, 0), (0, 65536), (8000, 0), (4096, 65504)])
def test_payload_budget_includes_headers(adoption, count, payload):
    adoption[0].validate_payload_volume(count, payload)


def test_owned_payload_changes_exact_only_and_keeps_reference_verifiable(adoption, tmp_path):
    module = adoption[0]
    roots = [tmp_path / "plain", tmp_path / "owned"]
    for root in roots:
        root.mkdir()
    module.create_fixture(roots[0], 6)
    module.create_fixture(roots[1], 6, owned_files=13, owned_payload_bytes=128)
    files = list(roots[1].glob("components/*/benchmark-payload/*.dat"))
    assert len(files) == 13
    for path in files:
        index = int(path.stem.removeprefix("file-"))
        payload = hashlib.shake_256(f"boundver-owned-{index}".encode("ascii")).digest(128)
        assert len(set(payload)) > 32
        assert path.read_bytes() == f"implementation {index}\n".encode("ascii") + payload
    inventories = [module.fixture_inventory(root) for root in roots]
    assert inventories[1]["inside_component_files"] - inventories[0]["inside_component_files"] == 13
    assert inventories[1]["inside_component_bytes"] - inventories[0]["inside_component_bytes"] == sum(path.stat().st_size for path in files)
    locks = [json.loads((root / "boundary.lock.json").read_text()) for root in roots]
    assert locks[0]["config_digest"] == locks[1]["config_digest"]
    for name in locks[0]["components"]:
        before, after = [lock["components"][name]["fingerprints"] for lock in locks]
        assert before["exact"] != after["exact"]
        for facet in ("behavior", "boundary", "compat"):
            assert before[facet] == after[facet]
    for operation in ("validate-config", "verify", "generate"):
        assert module.measure(roots[1], operation)["ok"]


def test_cli_profile_reports_owned_volume_without_inspecting_working_tree(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_generation_cli.py"
    spec = importlib.util.spec_from_file_location("boundver_cli_scale_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.profile(tmp_path, 6, 4, 1, False, 256, owned_files=12, owned_payload_bytes=128)
    assert result["owned_extra_files"] == 12 and result["owned_payload_bytes_per_file"] == 128
    assert result["owned_payload_kind"] == "deterministic-shake256-bytes"
    assert result["outside_files"] == 4 and result["outside_payload_bytes_per_file"] == 256
    inventory = result["fixture_inventory"]
    assert inventory["tracked_files"] == inventory["inside_component_files"] + inventory["outside_component_files"]
    assert inventory["tracked_bytes"] == inventory["inside_component_bytes"] + inventory["outside_component_bytes"]
    assert inventory["inside_component_files"] >= 12 and inventory["outside_component_files"] >= 4
    hashes = set()
    for case in result["results"].values():
        for row in [case["first"], *case["repeated"]]:
            assert row["exit_code"] == 0
            assert row["working_tree_reads"] == row["advisory_calls"] == 0
            assert row["total"]["git_commands"].get("write-tree", 0) == 0
            assert row["total"]["git_processes"] <= 22
            hashes.add(row["lock_sha256"])
    assert len(hashes) == 1 and None not in hashes
