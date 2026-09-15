"""Contract tests for the explicit core/exhaustive test partition."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "test_tiers.py"
SPEC = importlib.util.spec_from_file_location("boundver_test_tiers", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
test_tiers = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = test_tiers
SPEC.loader.exec_module(test_tiers)


def _write_catalog(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_committed_tiers_partition_every_test_file() -> None:
    tiers = test_tiers.load_tiers()

    assert set(tiers) == {"core", "exhaustive", "all"}
    assert set(tiers["core"]).isdisjoint(tiers["exhaustive"])
    assert set(tiers["core"]) | set(tiers["exhaustive"]) == set(tiers["all"])
    assert "tests/test_test_tiers.py" in tiers["core"]
    assert "tests/test_chunk_01_git_source.py" in tiers["exhaustive"]


def test_public_assurance_counts_are_complete_and_cross_checked() -> None:
    counts = test_tiers.load_assurance_counts()

    assert counts == {
        "total": 620,
        "asserted": 480,
        "examples_only": 133,
        "uncovered": 7,
        "unassessed": 0,
        "survey_expected_failures": 178,
        "remaining_expected_failures": 0,
        "release_mutants": 12,
    }


def test_assurance_counts_must_partition_the_survey(tmp_path: Path) -> None:
    summary = json.loads(
        (ROOT / "spec" / "testing-obligations-summary.json").read_text(
            encoding="utf-8"
        )
    )
    summary["source"]["survey_uncovered"] = 8
    summary_path = tmp_path / "summary.json"
    _write_catalog(summary_path, summary)

    with pytest.raises(test_tiers.TestTierError, match="do not equal"):
        test_tiers.load_assurance_counts(
            summary_path,
            ROOT / "spec" / "release-mutations.json",
        )


def test_new_test_files_default_to_the_required_core_tier(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_existing.py").write_text("", encoding="utf-8")
    (tests / "test_new_regression.py").write_text("", encoding="utf-8")
    catalog = tmp_path / "tiers.json"
    _write_catalog(
        catalog,
        {
            "record": test_tiers.RECORD,
            "exhaustive_files": ["tests/test_existing.py"],
        },
    )

    tiers = test_tiers.load_tiers(catalog, root=tmp_path)

    assert tiers["exhaustive"] == ("tests/test_existing.py",)
    assert tiers["core"] == ("tests/test_new_regression.py",)


@pytest.mark.parametrize(
    "exhaustive",
    [
        ["tests/test_missing.py"],
        ["tests\\test_existing.py"],
        ["../tests/test_existing.py"],
        ["tests/test_existing.py", "tests/test_existing.py"],
    ],
)
def test_invalid_or_stale_exhaustive_entries_fail_closed(
    tmp_path: Path,
    exhaustive: list[str],
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_existing.py").write_text("", encoding="utf-8")
    (tests / "test_core.py").write_text("", encoding="utf-8")
    catalog = tmp_path / "tiers.json"
    _write_catalog(
        catalog,
        {"record": test_tiers.RECORD, "exhaustive_files": exhaustive},
    )

    with pytest.raises(test_tiers.TestTierError):
        test_tiers.load_tiers(catalog, root=tmp_path)


def test_duplicate_catalog_keys_are_rejected(tmp_path: Path) -> None:
    catalog = tmp_path / "tiers.json"
    catalog.write_text(
        '{"record":"boundver-test-tiers/v1",'
        '"record":"boundver-test-tiers/v1","exhaustive_files":[]}',
        encoding="utf-8",
    )

    with pytest.raises(test_tiers.TestTierError, match="duplicate JSON key"):
        test_tiers.load_tiers(catalog, root=tmp_path)


def test_pytest_command_puts_options_before_the_catalog_paths(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text("", encoding="utf-8")
    (tests / "test_survey.py").write_text("", encoding="utf-8")
    catalog = tmp_path / "tiers.json"
    _write_catalog(
        catalog,
        {
            "record": test_tiers.RECORD,
            "exhaustive_files": ["tests/test_survey.py"],
        },
    )

    command = test_tiers.pytest_command(
        "core",
        ["--", "-q", "--tb=short"],
        path=catalog,
        root=tmp_path,
    )

    assert command[:4] == (sys.executable, "-I", "-m", "pytest")
    assert command[4:6] == ("-q", "--tb=short")
    assert command[6:] == ("--", "tests/test_core.py")


def test_ci_and_release_use_the_declared_tiers() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    dispositions = (
        ROOT / "docs" / "design" / "testing-obligation-dispositions.md"
    ).read_text(encoding="utf-8")
    release = (ROOT / "scripts" / "verify_release_candidate.py").read_text(
        encoding="utf-8"
    )

    assert "scripts/test_tiers.py run core --" in workflow
    assert "scripts/test_tiers.py run exhaustive --" in workflow
    assert "github.event_name != 'pull_request'" in workflow
    assert "timeout-minutes: 45" in workflow
    assert "45-minute per-job CI limit" in dispositions
    assert '"scripts/test_tiers.py", "run", "all"' in release
