"""Historical records can be inspected, never silently promoted to live locks."""

import copy
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from boundver._change_report import lock_change_report
from boundver._lockfile import (
    load_lockfile_file,
    migrate_lockfile,
    MigrationError,
    verify_lockfile,
)
from boundver._utils import ConfigError
from tests._parity import run_cli
from tests._schema_validation import assert_output_schema


FIXTURE = Path(__file__).parent / "fixtures" / "historical-locks" / "v0.15.2.json"


@pytest.mark.parametrize(
    "tag,digest",
    [
        ("v0.15.2", "f4e34885646fb16d120b2bed5c18ad0dd69cf1f8b69bda6de8887c7d296a54d7"),
        ("v0.16.0", "31316d746b682b079a2ae8251fa3538d4103bff73ea67c172c33a91ee64a9db4"),
    ],
)
def test_fixture_values_remain_frozen_during_future_release_preparation(tag, digest):
    record = json.loads(FIXTURE.with_name(tag + ".json").read_text())
    encoded = json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    assert hashlib.sha256(encoded).hexdigest() == digest


@pytest.fixture
def historical():
    return load_lockfile_file(FIXTURE)


def inspect(previous, current, limit=1000):
    result = lock_change_report(
        previous,
        current,
        source={
            "mode": "lockfiles",
            "commit": None,
            "tree": None,
            "lock": "new.json",
            "previous_lock": "old.json",
        },
        operation="lock-diff",
        historical=True,
        limit=limit,
    )
    assert_output_schema("cli-output.lock-changes.schema.json", result)
    return result


def test_authentic_v3_record_can_be_compared_with_itself(historical):
    result = inspect(historical, historical)
    assert result["complete"] and result["exit_code"] == 0, result["issues"]
    assert (
        result["comparison"]["historical"]
        and result["comparison"]["contract_compatible"]
    )
    assert result["counts"]["unchanged"] == 2
    assert result["counts"]["equal_component_values"] == 2
    assert all(row["values_equal"] for row in result["slices"])


def test_authentic_v3_and_v4_records_expose_differences_without_causal_claims(
    historical,
):
    current = load_lockfile_file(FIXTURE.with_name("v0.16.0.json"))
    result = inspect(historical, current)
    assert result["complete"] and not result["comparison"]["contract_compatible"]
    assert result["comparison"]["previous_contract"]["schema"] == "boundary-lock/v3"
    assert result["comparison"]["current_contract"]["schema"] == "boundary-lock/v4"
    assert (
        result["counts"]["incomparable"]
        == result["counts"]["different_component_values"]
        == 2
    )
    assert result["comparison"]["declarations_match"] is None
    assert all(row["status"] == "incomparable" for row in result["slices"])
    assert result["locked_source"]["commit"] is None


def test_synthetic_header_only_upgrade_is_not_a_migration(historical):
    # Explicitly synthetic: relabelling this copy is a negative test, not an
    # endorsed migration or an independently generated v4 historical fixture.
    changed = copy.deepcopy(historical)
    changed["schema"] = "boundary-lock/v4"
    changed["config_contract"] = "boundver-semantic-config/v3"
    result = inspect(historical, changed)
    assert result["complete"] and not result["comparison"]["contract_compatible"]
    assert result["comparison"]["declarations_match"] is None
    assert result["counts"]["equal_component_values"] == 2
    assert result["counts"]["incomparable"] == 2 and result["counts"]["changed"] == 0
    assert all(
        row["status"] == "incomparable" and row["values_equal"]
        for row in result["components"]
    )
    assert historical["schema"] == "boundary-lock/v3"


def test_same_contract_real_value_changes_are_reported_separately(historical):
    changed = copy.deepcopy(historical)
    changed["components"]["boundver"]["fingerprints"]["exact"] = "a" * 64
    changed["components"]["boundver"]["version"] = "1.0.0"
    changed["slices"]["all"]["fingerprint"] = "b" * 64
    result = inspect(historical, changed)
    assert result["counts"]["changed"] == 1
    assert result["counts"]["different_component_values"] == 1
    assert result["counts"]["changed_slices"] == 1
    row = next(row for row in result["components"] if row["component"] == "boundver")
    assert row["version"]["previous"] is None and row["version"]["current"] == "1.0.0"
    assert row["version"]["status"] == "unavailable"


def test_provider_change_is_explicit_and_propagates_to_slices(historical):
    changed = copy.deepcopy(historical)
    changed["components"]["boundver"]["boundary_provider_version"] = "another-runtime"
    result = inspect(historical, changed)
    row = next(row for row in result["components"] if row["component"] == "boundver")
    assert row["status"] == "incomparable" and "provider" in row["reason"]
    assert row["provider_contract"]["previous"]["boundary_provider_version"] == "3"
    assert (
        row["provider_contract"]["current"]["boundary_provider_version"]
        == "another-runtime"
    )
    assert (
        next(row for row in result["slices"] if row["slice"] == "cli-api")["status"]
        == "incomparable"
    )


def test_slice_metadata_is_visible_even_when_component_values_and_slice_hash_match(
    historical,
):
    changed = copy.deepcopy(historical)
    changed["slices"]["all"]["description"] = "new description"
    result = inspect(historical, changed)
    assert result["counts"]["equal_component_values"] == 2
    row = next(row for row in result["slices"] if row["slice"] == "all")
    assert row["status"] == "changed" and not row["values_equal"]
    assert row["previous"] == row["current"]
    assert row["metadata_fields"] == ["description"]


@pytest.mark.parametrize(
    "mutation",
    [
        "future-schema",
        "bad-contract",
        "wrong-known-pair",
        "bad-digest",
        "unknown-field",
        "mixed-deployments",
    ],
)
def test_untrusted_old_records_fail_closed(historical, mutation):
    bad = copy.deepcopy(historical)
    if mutation == "future-schema":
        bad["schema"] = "boundary-lock/v999"
    elif mutation == "bad-contract":
        bad["config_contract"] = "boundver-semantic-config/v999"
    elif mutation == "wrong-known-pair":
        bad["config_contract"] = "boundver-semantic-config/v3"
    elif mutation == "bad-digest":
        bad["components"]["boundver"]["fingerprints"]["exact"] = "not-a-digest"
    elif mutation == "unknown-field":
        bad["components"]["boundver"]["execute"] = "untrusted code"
    else:
        bad["deployments"] = {"boundver": "unproved-commit", "spec": "another-commit"}
    result = inspect(bad, historical)
    assert not result["complete"] and result["exit_code"] == 2
    assert result["counts"] is None and not result["components"]


def test_historical_authority_cannot_be_used_for_generation(historical):
    with pytest.raises(ConfigError, match="never generation"):
        lock_change_report(historical, historical, source={}, historical=True)


def test_v3_remains_refused_by_normal_verification_and_migration(historical, tmp_path):
    config = {
        "components": {
            name: {"path": entry["path"]}
            for name, entry in historical["components"].items()
        }
    }
    issues = verify_lockfile(config, historical, tmp_path, source="working-tree")
    assert any("schema unsupported" in issue for issue in issues)
    with pytest.raises(MigrationError):
        migrate_lockfile(historical)
    assert historical["schema"] == "boundary-lock/v3"


def test_cli_inspects_without_git_or_history_and_never_writes(
    historical, tmp_path, monkeypatch
):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    raw = json.dumps(historical)
    old.write_text(raw, encoding="utf-8")
    new.write_text(raw, encoding="utf-8")
    monkeypatch.setenv("PATH", "")
    assert shutil.which("git") is None
    result = run_cli(
        tmp_path,
        "diff",
        "old.json",
        "new.json",
        "--format",
        "upgrade-json",
        "--summary",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert_output_schema("cli-output.lock-changes.schema.json", payload)
    assert payload["counts"]["equal_component_values"] == 2
    assert payload["locked_source"]["status"] == "unavailable"
    assert not payload["components"] and not payload["slices"]
    assert old.read_text(encoding="utf-8") == new.read_text(encoding="utf-8") == raw


def test_duplicate_keys_are_rejected_before_inspection(historical, tmp_path):
    (tmp_path / "old.json").write_text(
        '{"schema":"boundary-lock/v3","schema":"boundary-lock/v4"}', encoding="utf-8"
    )
    (tmp_path / "new.json").write_text(json.dumps(historical), encoding="utf-8")
    result = run_cli(
        tmp_path, "diff", "old.json", "new.json", "--format", "upgrade-json"
    )
    assert result.returncode == 2 and "duplicate" in result.stderr.lower()


def test_boolean_and_integer_provider_metadata_are_not_equal_recorded_values(
    historical,
):
    previous = copy.deepcopy(historical)
    previous["components"]["boundver"]["boundary_metadata"] = {"flag": True}
    current = copy.deepcopy(previous)
    current["components"]["boundver"]["boundary_metadata"]["flag"] = 1
    result = inspect(previous, current)
    row = next(row for row in result["components"] if row["component"] == "boundver")
    assert row["status"] == "changed" and not row["values_equal"]
    assert row["metadata_fields"] == ["boundary_metadata"]


def test_incompatible_contracts_cannot_claim_matching_declarations_in_json(historical):
    jsonschema = pytest.importorskip("jsonschema")
    current = load_lockfile_file(FIXTURE.with_name("v0.16.0.json"))
    result = inspect(historical, current)
    result["comparison"]["declarations_match"] = True
    with pytest.raises(jsonschema.ValidationError):
        assert_output_schema("cli-output.lock-changes.schema.json", result)


def test_recorded_computation_errors_are_unavailable_not_ordinary_changes(historical):
    old = copy.deepcopy(historical)
    old["components"]["boundver"]["exact_errors"] = ["source unavailable"]
    result = inspect(old, historical)
    assert result["counts"]["unavailable"] == 1
    assert result["counts"]["unavailable_slices"] == 2
    assert result["counts"]["changed"] == result["counts"]["incomparable"] == 0
    row = next(row for row in result["components"] if row["component"] == "boundver")
    assert row["status"] == "unavailable" and not row["comparable"]


def test_cli_cross_contract_inspection_is_explicit_and_preserves_both_inputs(tmp_path):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    shutil.copyfile(FIXTURE, old)
    shutil.copyfile(FIXTURE.with_name("v0.16.0.json"), new)
    originals = (old.read_bytes(), new.read_bytes())
    ordinary = run_cli(tmp_path, "diff", "old.json", "new.json", "--format", "json")
    assert ordinary.returncode == 2
    assert "do not rewrite historical locks" in ordinary.stderr
    upgraded = run_cli(
        tmp_path,
        "diff",
        "old.json",
        "new.json",
        "--format",
        "upgrade-json",
        "--summary",
    )
    assert upgraded.returncode == 0, upgraded.stderr
    payload = json.loads(upgraded.stdout)
    assert_output_schema("cli-output.lock-changes.schema.json", payload)
    assert payload["counts"]["incomparable"] == 2
    assert payload["comparison"]["historical"]
    assert (old.read_bytes(), new.read_bytes()) == originals
