"""The shape of a review report, and what a baseline guard counts as one file.

A report a person reads has to be complete in the boring cases too: an empty
range still needs its provenance, and a component with no consumers still needs
its consumer fields, or a reader cannot tell "none" from "not computed".

The path guards face the opposite problem. Two spellings that name one file
must be refused alike, or a guard is bypassed by changing the case.

Covers OBL-LOCKFILE-006, OBL-LOCKFILE-007 and OBL-LOCKFILE-009.
"""

from __future__ import annotations

import re
import unittest

from tests._parity import run_cli
from tests._scenarios import Scenario

COULD_NOT_CHECK = 2

#: The provenance an empty range must still disclose.
PROVENANCE = (
    "Requested base:", "Effective base:", "Target:",
    "Base config:", "Base lock:", "Target config:", "Target lock:",
    "History:",
)

EMPTY_SECTIONS = ("CHANGED COMPONENTS", "CHANGED SLICES", "IMPACTED SLICES")


def _reviewed(consumers=None, external=None, change=True) -> str:
    """Review a range, optionally with a change and a consumer graph."""
    scene = Scenario()
    try:
        scene.component(
            "svc", path="svc", boundary=["api"],
            consumers=list(consumers) if consumers else None,
            external_consumers=list(external) if external else None,
        )
        if consumers:
            for name in consumers:
                scene.component(name, path=name, provider="leaf")
                scene.file(f"{name}/index.ts", "export const x = 1;\n")
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.commit()
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("lock")
        base = scene.head()
        if change:
            scene.append_line("svc/api/v1.yaml", "change\n")
            scene.commit("edit")
            run_cli(scene.root, "generate", "--source", "head")
            scene.commit("relock")
        result = run_cli(
            scene.root, "review", "--base", base, "--target", scene.head(),
            "--format", "text",
        )
        assert result.returncode == 0, result.stderr
        return result.stdout
    finally:
        scene.close()


class EmptyRangeShapeTests(unittest.TestCase):
    """OBL-LOCKFILE-006: an empty range is still a full report."""

    def test_the_provenance_block_is_complete(self):
        report = _reviewed(change=False)
        for label in PROVENANCE:
            with self.subTest(field=label):
                self.assertIn(label, report)

    def test_each_section_reports_a_count_of_zero_and_none(self):
        report = _reviewed(change=False)
        lines = report.splitlines()
        for section in EMPTY_SECTIONS:
            with self.subTest(section=section):
                index = next(
                    i for i, line in enumerate(lines) if line.startswith(section)
                )
                self.assertIn("(0)", lines[index])
                self.assertEqual(lines[index + 1], "  none")

    def test_the_range_names_the_same_commit_twice(self):
        """The premise: this really is an empty range."""
        report = _reviewed(change=False)
        match = re.search(r"^Range: (\w+)\.\.(\w+)$", report, re.MULTILINE)
        self.assertIsNotNone(match, report)
        self.assertEqual(match.group(1), match.group(2))


class ConsumerImpactShapeTests(unittest.TestCase):
    """OBL-LOCKFILE-007: both fields always, edges only when there are edges."""

    def test_both_fields_print_when_both_are_populated(self):
        report = _reviewed(consumers=["sdk"], external=["mobile"])
        self.assertIn("Affected components: sdk", report)
        self.assertIn("External consumers: mobile", report)

    def test_an_empty_external_list_prints_none_rather_than_vanishing(self):
        """A missing line reads as "not computed"; none reads as none."""
        report = _reviewed(consumers=["sdk"])
        self.assertIn("Affected components: sdk", report)
        self.assertIn("External consumers: none", report)

    def test_both_fields_print_none_when_there_are_no_consumers(self):
        report = _reviewed()
        self.assertIn("Affected components: none", report)
        self.assertIn("External consumers: none", report)

    def test_the_edges_block_appears_only_when_there_are_edges(self):
        self.assertIn("Consumer edges:", _reviewed(consumers=["sdk"]))
        self.assertNotIn("Consumer edges:", _reviewed())

    def test_an_external_only_component_affects_no_internal_component(self):
        """The premise: this fixture really does have an empty internal list.

        The edges assertion below only says something while the affected
        component list is empty, so assert that emptiness on its own first.
        """
        self.assertIn("Affected components: none", _reviewed(external=["mobile"]))

    def test_the_edges_block_survives_an_empty_affected_component_list(self):
        """MUT-LOCKFILE-403: the block is gated on edges, not on components.

        The two shapes the test above drives move both lists together. A
        component with an internal consumer has an affected component and an
        edge, and a component with no consumers has neither, so a guard
        rewritten to ask whether the affected component list is populated
        printed exactly the same two reports and survived. A component whose
        only consumer is external separates them: it affects no internal
        component and still has one consumer edge, so the block has to be
        printed on the strength of that edge alone. Together with the empty
        case above, which keeps the block out of a report that has no edge at
        all, this pins the block to the edge list.
        """
        report = _reviewed(external=["mobile"])
        self.assertIn("Affected components: none", report)
        self.assertIn("Consumer edges:", report)
        self.assertIn("svc -> mobile [external; both]", report)

    def test_each_edge_carries_its_provenance(self):
        report = _reviewed(consumers=["sdk"], external=["mobile"])
        edges = [
            line.strip() for line in report.splitlines()
            if line.strip().startswith("svc -> ")
        ]
        self.assertEqual(len(edges), 2, edges)
        for edge in edges:
            with self.subTest(edge=edge):
                self.assertRegex(edge, r"\[(component|external); \w+\]$")


class BaselinePathEquivalenceTests(unittest.TestCase):
    """OBL-LOCKFILE-009: one file, however it is spelled."""

    def _write_baseline(self, target: str):
        scene = Scenario()
        try:
            scene.component("svc", path="svc", boundary=["api"])
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.commit()
            run_cli(scene.root, "generate", "--source", "head")
            scene.commit("lock")
            folds_case = (scene.root / "SVC").is_dir()
            result = run_cli(
                scene.root, "verify", "--source", "head",
                "--write-baseline", target,
            )
            return result, folds_case
        finally:
            scene.close()

    def test_a_path_outside_any_component_is_accepted(self):
        """The premise: the guards are not refusing everything."""
        result, _folds_case = self._write_baseline("debt.json")
        self.assertEqual(result.returncode, 0)

    def test_component_aliases_follow_the_filesystem_case_identity(self):
        for spelling in ("svc/debt.json", "SVC/debt.json", "Svc/debt.json"):
            with self.subTest(spelling=spelling):
                result, folds_case = self._write_baseline(spelling)
                aliases_component = spelling == "svc/debt.json" or folds_case
                expected = COULD_NOT_CHECK if aliases_component else 0
                self.assertEqual(result.returncode, expected, result.stdout)
                if aliases_component:
                    self.assertIn("baseline", result.stderr)

    def test_lock_aliases_follow_the_filesystem_case_identity(self):
        for spelling in (
            "boundary.lock.json", "BOUNDARY.LOCK.JSON", "Boundary.Lock.Json",
        ):
            with self.subTest(spelling=spelling):
                result, folds_case = self._write_baseline(spelling)
                aliases_lock = spelling == "boundary.lock.json" or folds_case
                expected = COULD_NOT_CHECK if aliases_lock else 0
                self.assertEqual(result.returncode, expected, result.stdout)
                if aliases_lock:
                    self.assertIn("must not overwrite", result.stderr)


if __name__ == "__main__":
    unittest.main()
