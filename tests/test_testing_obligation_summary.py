"""Integrity checks for the public v0.16 survey disposition."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "spec" / "testing-obligations-summary.json"
EXPECTED_FAILURE = re.compile(r"@unittest\.expectedFailure\b")
PRIVATE_SURVEY_PATHS = (
    "docs/design/obligation-survey-method.md",
    "docs/design/obligation-survey-reply.md",
    "docs/design/obligation-survey-reply-2.md",
    "docs/design/testing-obligations.md",
    "spec/mutants.json",
    "spec/testing-obligations.json",
    "scripts/assess_obligations.py",
    "scripts/render_testing_obligations.py",
    "tests/test_render_testing_obligations.py",
)


def _load_summary() -> dict:
    return json.loads(SUMMARY.read_text(encoding="utf-8"))


def test_dispositions_account_for_every_imported_expected_failure() -> None:
    summary = _load_summary()
    source = summary["source"]
    dispositions = summary["dispositions"]
    accounted = (
        dispositions["remediated_in_code_or_regression_oracles"]
        + dispositions["contract_documented"]
        + dispositions["rejected_as_contradictory_or_stale"]
        + dispositions["deferred"]
    )
    assert accounted == source["survey_expected_failures"] == 178
    assert dispositions["deferred"] == 0


def test_the_summary_matches_the_expected_failure_corpus() -> None:
    summary = _load_summary()
    occurrences = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if EXPECTED_FAILURE.search(line):
                occurrences.append(f"{path.relative_to(ROOT)}:{line_number}")
    assert occurrences == []
    assert summary["dispositions"]["remaining_expected_failures"] == len(
        occurrences
    )


def test_rejected_obligations_are_unique_and_explained() -> None:
    summary = _load_summary()
    rejected = summary["rejected"]
    identifiers = [item["obligation"] for item in rejected]
    assert len(rejected) == summary["dispositions"][
        "rejected_as_contradictory_or_stale"
    ]
    assert len(identifiers) == len(set(identifiers))
    assert all(item["reason"].strip() for item in rejected)


def test_every_maintained_surface_exists() -> None:
    summary = _load_summary()
    for relative in summary["maintained_surfaces"]:
        assert (ROOT / relative).is_file(), relative


def test_private_survey_evidence_is_not_in_the_public_tree() -> None:
    present = [
        relative for relative in PRIVATE_SURVEY_PATHS if (ROOT / relative).exists()
    ]
    assert present == []
