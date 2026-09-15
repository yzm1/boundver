"""How a review says it could not explain a boundary change.

When the structural provider cannot compare two endpoints, the review prints
one line naming the reason and one indented line giving the detail, and adds
"; no partial rows emitted" when the report was truncated. The suffix is the
part that matters most: it is the difference between "there was nothing to
say" and "there was more to say and you are not being shown it".

The reasons are a closed vocabulary, so the rendering is asserted over all of
it rather than over the one reason a repository happens to produce.

Covers OBL-PROVIDERS-001.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from boundver import _structural_review
from boundver._review import analyze_review_range, review_text_lines

from tests._parity import run_cli
from tests._scenarios import Scenario

OPENAPI = "openapi: 3.1.0\ninfo:\n  title: t\n  version: '1'\npaths: {}\n"

TRUNCATION_SUFFIX = "; no partial rows emitted"

def _reasons_in_source() -> set:
    """Every reason _structural_review can put in a report, read from itself.

    Copying the list here would make the completeness check circular. The
    point is that a reason added to the code and not to this file fails.
    """
    source = Path(_structural_review.__file__).read_text(encoding="utf-8")
    found = set()
    for match in re.finditer(r'reason=["\']([a-z-]+)["\']', source):
        found.add(match.group(1))
    for match in re.finditer(r'^\s+"([a-z]+(?:-[a-z]+)+)",$', source, re.MULTILINE):
        found.add(match.group(1))
    return found


#: The vocabulary this file asserts the rendering over.
REASONS = (
    "component-absent",
    "provider-changed",
    "provider-version-changed",
    "provider-unavailable",
    "provider-implementation-changed",
    "provider-unsupported",
    "provider-interface-unsupported",
    "limit-exceeded",
)


class _Reviewed:
    """A range whose target adds a component the base does not have."""

    def __init__(self) -> None:
        scene = Scenario()
        scene.component(
            "svc", path="svc", provider="openapi-canonical",
            boundary=["api/v1.yaml"],
        )
        scene.file("svc/api/v1.yaml", OPENAPI)
        scene.commit()
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("lock")
        base = scene.head()
        scene.component(
            "added", path="added", provider="openapi-canonical",
            boundary=["api/v1.yaml"],
        )
        scene.file("added/api/v1.yaml", OPENAPI)
        scene.append_line("svc/api/v1.yaml", "x-note: changed\n")
        scene.commit("add")
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("relock")
        self.scene = scene
        self.result = analyze_review_range(scene.root, base, scene.head())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def report(self, component: str) -> dict:
        for report in self.result["structural_changes"]["reports"]:
            if report["component"] == component:
                return report
        raise AssertionError(f"no report for {component}")

    def rendered(self) -> list:
        return review_text_lines(self.result)


def _explanation_lines(lines) -> list:
    """The (announcement, detail) pairs for every unavailable explanation."""
    pairs = []
    for index, line in enumerate(lines):
        if line.strip().startswith("Structural explanation: unavailable"):
            pairs.append((line, lines[index + 1]))
    return pairs


class ReasonVocabularyTests(unittest.TestCase):
    """The table is only a table if it covers what the code can produce."""

    def test_the_asserted_reasons_are_the_ones_the_source_can_emit(self):
        self.assertEqual(_reasons_in_source(), set(REASONS))


class UnavailableRenderingTests(unittest.TestCase):
    """OBL-PROVIDERS-001: one line naming the reason, one giving the detail."""

    def test_a_real_run_renders_an_unavailable_explanation(self):
        """The premise: this path is reachable without constructing a report."""
        with _Reviewed() as repo:
            report = repo.report("added")
            self.assertEqual(report["reason"], "component-absent")
            self.assertFalse(report["truncated"])
            pairs = _explanation_lines(repo.rendered())
            self.assertEqual(len(pairs), 1, repo.rendered())
            announcement, detail = pairs[0]
            self.assertEqual(
                announcement,
                "    Structural explanation: unavailable [component-absent]",
            )
            self.assertEqual(detail, f"      {report['detail']}")

    def test_every_reason_renders_the_same_shape(self):
        with _Reviewed() as repo:
            report = repo.report("added")
            for reason in REASONS:
                with self.subTest(reason=reason):
                    report["reason"] = reason
                    report["detail"] = f"detail for {reason}"
                    announcement, detail = _explanation_lines(repo.rendered())[0]
                    self.assertEqual(
                        announcement,
                        f"    Structural explanation: unavailable [{reason}]",
                    )
                    self.assertEqual(detail, f"      detail for {reason}")

    def test_the_suffix_appears_exactly_when_the_report_is_truncated(self):
        with _Reviewed() as repo:
            report = repo.report("added")
            for reason in REASONS:
                for truncated in (False, True):
                    with self.subTest(reason=reason, truncated=truncated):
                        report["reason"] = reason
                        report["truncated"] = truncated
                        announcement, _detail = _explanation_lines(repo.rendered())[0]
                        self.assertEqual(
                            announcement.endswith(TRUNCATION_SUFFIX), truncated
                        )

    def test_the_detail_is_indented_under_the_announcement(self):
        """Positional, so a detail printed somewhere else would fail."""
        with _Reviewed() as repo:
            report = repo.report("added")
            report["detail"] = "a distinctive detail"
            lines = repo.rendered()
            index = next(
                position for position, line in enumerate(lines)
                if "Structural explanation: unavailable" in line
            )
            self.assertEqual(lines[index + 1], "      a distinctive detail")
            self.assertTrue(lines[index].startswith("    Structural"))

    def test_a_complete_report_renders_the_other_way(self):
        """The contrast: 'unavailable' is not what every report says."""
        with _Reviewed() as repo:
            self.assertTrue(repo.report("svc")["complete"])
            complete = [
                line for line in repo.rendered()
                if "Structural explanation: complete" in line
            ]
            self.assertEqual(len(complete), 1, complete)
            self.assertIn("not a compatibility verdict", complete[0])


if __name__ == "__main__":
    unittest.main()
