"""A boundary that moved, and a provider with nothing to say about it.

A structural explanation is evidence, and a complete report with an empty
document list is the worst possible answer: it claims the change was
explained and then explains nothing. The contract is that this degrades to
an unavailable report naming the provider, so a reader can tell "there is
nothing to show" from "we did not look".

Reaching it means a provider whose digest and whose diff disagree, which no
built-in provider does on its own, so the provider's structural_diff is
replaced for the duration of one review.

Covers OBL-HASHING-008 and OBL-HASHING-011.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from boundver._provider_diff import StructuralDiffResult
from boundver._review import analyze_review_range, review_text_lines
from boundver.providers import OpenApiCanonicalProvider

from tests._parity import run_cli
from tests._scenarios import Scenario

OPENAPI = (
    "openapi: 3.1.0\ninfo:\n  title: t\n  version: '1'\n"
    "paths:\n  /a:\n    get:\n      responses:\n        '200':\n"
    "          description: ok\n"
)

#: The message _complete_report raises when a changed digest explains nothing.
NO_CHANGES = "Changed boundary digest produced no structural changes"


class _MovedBoundary:
    """A range where one component's boundary digest really did change."""

    def __init__(self) -> None:
        scene = Scenario()
        scene.component(
            "svc", path="svc", provider="openapi-canonical",
            boundary=["api/v1.yaml"],
        )
        scene.file("svc/api/v1.yaml", OPENAPI)
        scene.commit()
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        scene.commit("lock")
        self.base = scene.head()
        scene.append_line("svc/api/v1.yaml", "x-added: true\n")
        scene.commit("edit")
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        scene.commit("relock")
        self.scene = scene
        self.target = scene.head()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def reviewed(self) -> dict:
        previous = os.getcwd()
        try:
            os.chdir(self.scene.root)
            return analyze_review_range(self.scene.root, self.base, self.target)
        finally:
            os.chdir(previous)

    def report(self, result: dict) -> dict:
        reports = result["structural_changes"]["reports"]
        assert len(reports) == 1, reports
        return reports[0]


def _empty(self, before_ctx, after_ctx, budget):
    """A structural_diff that returns a well-formed result with no documents."""
    return StructuralDiffResult(documents=())


class CompleteReportTests(unittest.TestCase):
    """The premise: this range is explained, and the digest really moved."""

    def test_the_boundary_digest_changed(self):
        with _MovedBoundary() as repo:
            changed = repo.reviewed()["components"]["changed"]
            self.assertEqual([entry["name"] for entry in changed], ["svc"])
            moved = {
                facet["facet"]: (facet["before"], facet["after"])
                for facet in changed[0]["facets"]
            }
            self.assertIn("boundary", moved)
            self.assertNotEqual(*moved["boundary"])

    def test_the_unpatched_review_explains_it(self):
        with _MovedBoundary() as repo:
            report = repo.report(repo.reviewed())
            self.assertTrue(report["complete"])
            self.assertEqual(report["status"], "complete")
            self.assertTrue(report["documents"])


class NoEvidenceDegradationTests(unittest.TestCase):
    """OBL-HASHING-008 and 011: no documents means unavailable, not complete."""

    def _reviewed_with_no_documents(self, repo) -> dict:
        with patch.object(OpenApiCanonicalProvider, "structural_diff", _empty):
            return repo.reviewed()

    def test_an_empty_result_becomes_an_unavailable_report(self):
        with _MovedBoundary() as repo:
            report = repo.report(self._reviewed_with_no_documents(repo))
            self.assertEqual(report["status"], "unavailable")
            self.assertFalse(report["complete"])
            self.assertEqual(report["reason"], "provider-unavailable")
            self.assertEqual(report["documents"], [])

    def test_the_detail_comes_from_the_provider_error(self):
        with _MovedBoundary() as repo:
            report = repo.report(self._reviewed_with_no_documents(repo))
            self.assertEqual(report["detail"], NO_CHANGES)

    def test_the_range_is_marked_incomplete(self):
        with _MovedBoundary() as repo:
            result = self._reviewed_with_no_documents(repo)
            self.assertFalse(result["structural_changes"]["complete"])
            self.assertFalse(result["structural_changes"]["truncated"])

    def test_the_text_view_says_so(self):
        with _MovedBoundary() as repo:
            result = self._reviewed_with_no_documents(repo)
            lines = review_text_lines(result)
            announcements = [
                line for line in lines
                if "Structural explanation:" in line
            ]
            self.assertEqual(
                announcements,
                ["    Structural explanation: unavailable [provider-unavailable]"],
            )
            self.assertIn(f"      {NO_CHANGES}", lines)

    def test_an_invalid_result_type_degrades_the_same_way(self):
        """The neighbouring guard, so the degradation is not one branch wide."""
        def not_a_result(self, before_ctx, after_ctx, budget):
            return {"documents": []}

        with _MovedBoundary() as repo:
            with patch.object(
                OpenApiCanonicalProvider, "structural_diff", not_a_result
            ):
                report = repo.report(repo.reviewed())
            self.assertEqual(report["status"], "unavailable")
            self.assertEqual(report["reason"], "provider-unavailable")
            self.assertIn("invalid result type", report["detail"])

    def test_a_raising_provider_degrades_the_same_way(self):
        def explodes(self, before_ctx, after_ctx, budget):
            raise ValueError("provider exploded")

        with _MovedBoundary() as repo:
            with patch.object(OpenApiCanonicalProvider, "structural_diff", explodes):
                report = repo.report(repo.reviewed())
            self.assertEqual(report["reason"], "provider-unavailable")
            self.assertEqual(report["detail"], "provider exploded")

    def test_an_exiting_provider_degrades_instead_of_stopping_review(self):
        def exits(self, before_ctx, after_ctx, budget):
            raise SystemExit(0)

        with _MovedBoundary() as repo:
            with patch.object(OpenApiCanonicalProvider, "structural_diff", exits):
                report = repo.report(repo.reviewed())
            self.assertEqual(report["status"], "unavailable")
            self.assertEqual(report["reason"], "provider-unavailable")
            self.assertEqual(report["detail"], "SystemExit: 0")


if __name__ == "__main__":
    unittest.main()
