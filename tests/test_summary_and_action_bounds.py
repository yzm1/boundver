"""A summary that must fit, and an output that must say when it did not.

Both of these are about the same distinction: content that was omitted has to
be distinguishable from content that was never there. A CI summary says so in
a sentence with a count in it, and an Action output says so by naming itself
in truncated-outputs and carrying a marker. An output that is simply empty
must say neither.

The size contract has a floor as well as a ceiling: below a certain allowance
the fixed header cannot fit at all, and there is nothing honest left to render.

Covers OBL-GRAPH-004 and OBL-GRAPH-012.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path

from boundver._review_plan import (
    MAX_PLAN_SUMMARY_BYTES,
    render_review_plan_markdown,
)
from boundver._utils import GuardrailError

from tests._parity import run_cli
from tests._scenarios import Scenario

#: The smallest allowance the renderer accepts at all.
MINIMUM_ALLOWANCE = 2048

COMPLETE = "Presentation complete:"
TRUNCATED = "Presentation truncated:"


def _export_module():
    """The Action exporter, which lives in scripts/ rather than the package."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "export_action_outputs.py"
    spec = importlib.util.spec_from_file_location("export_action_outputs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXPORT = _export_module()


def _plan() -> dict:
    with Scenario() as scene:
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/main.py", "x\n")
        scene.commit()
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        scene.commit("lock")
        base = scene.head()
        scene.append_line("svc/main.py", "y\n")
        scene.commit("edit")
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        scene.commit("relock")
        result = run_cli(
            scene.root, "review", "--base", base, "--target", scene.head(),
            "--format", "plan",
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)


def _presentation(rendered: str) -> str:
    lines = [line for line in rendered.splitlines() if line.startswith("Presentation")]
    assert len(lines) == 1, lines
    return lines[0]


class SummarySizeTests(unittest.TestCase):
    """OBL-GRAPH-004: never over the allowance, and honest about why."""

    def setUp(self):
        self.plan = _plan()

    def test_the_complete_sentence_carries_the_total(self):
        """Not 'shown' alone: the count is what makes it checkable."""
        sentence = _presentation(render_review_plan_markdown(self.plan))
        self.assertTrue(sentence.startswith(COMPLETE), sentence)
        self.assertIn("all 1 routing rows shown", sentence)

    def test_the_truncated_sentence_carries_both_numbers(self):
        sentence = _presentation(render_review_plan_markdown(self.plan, max_rows=0))
        self.assertTrue(sentence.startswith(TRUNCATED), sentence)
        self.assertIn("0 of 1 routing rows shown", sentence)

    def test_the_rendering_never_exceeds_its_allowance(self):
        for allowance in (MINIMUM_ALLOWANCE, 4096, MAX_PLAN_SUMMARY_BYTES):
            with self.subTest(max_bytes=allowance):
                rendered = render_review_plan_markdown(
                    self.plan, max_bytes=allowance
                )
                self.assertLessEqual(len(rendered.encode("utf-8")), allowance)

    def test_a_negative_row_limit_is_a_usage_error(self):
        with self.assertRaises(ValueError):
            render_review_plan_markdown(self.plan, max_rows=-1)

    def test_an_allowance_below_the_floor_is_a_usage_error(self):
        with self.assertRaises(ValueError):
            render_review_plan_markdown(self.plan, max_bytes=MINIMUM_ALLOWANCE - 1)

    def test_a_header_that_cannot_fit_raises_rather_than_truncating(self):
        """The clause the gap named: nothing honest is left to render."""
        inflated = copy.deepcopy(self.plan)
        inflated["policy"]["impact"] = "i" * 4000
        with self.assertRaises(GuardrailError) as raised:
            render_review_plan_markdown(inflated, max_bytes=MINIMUM_ALLOWANCE)
        self.assertIn("cannot fit its bounded presentation", str(raised.exception))

    def test_the_same_plan_renders_when_the_header_does_fit(self):
        """The contrast: a smaller impact string is truncated, not refused."""
        inflated = copy.deepcopy(self.plan)
        inflated["policy"]["impact"] = "i" * 1500
        rendered = render_review_plan_markdown(inflated, max_bytes=MINIMUM_ALLOWANCE)
        self.assertLessEqual(len(rendered.encode("utf-8")), MINIMUM_ALLOWANCE)
        self.assertTrue(_presentation(rendered).startswith(TRUNCATED))


class ActionOutputBoundTests(unittest.TestCase):
    """OBL-GRAPH-012: bounded and empty must not look alike."""

    LIMIT = 1000

    def test_a_complete_list_is_joined_and_not_marked(self):
        """The premise: an untruncated list carries no marker."""
        text, truncated = EXPORT._bounded_lines(["a", "b"], self.LIMIT)
        self.assertEqual(text, "a\nb")
        self.assertFalse(truncated)

    def test_an_empty_list_is_empty_and_not_marked(self):
        text, truncated = EXPORT._bounded_lines([], self.LIMIT)
        self.assertEqual(text, "")
        self.assertFalse(truncated)

    def test_a_non_string_item_marks_the_output(self):
        """The early return the gap said nothing reached."""
        text, truncated = EXPORT._bounded_lines(["a", 3, "b"], self.LIMIT)
        self.assertTrue(truncated)
        self.assertEqual(text, "a\n" + EXPORT.TRUNCATION_MARKER)

    def test_a_list_of_only_a_non_string_is_marked_and_not_empty(self):
        """Which is the case an empty output could otherwise be mistaken for."""
        text, truncated = EXPORT._bounded_lines([3], self.LIMIT)
        self.assertTrue(truncated)
        self.assertEqual(text, EXPORT.TRUNCATION_MARKER)
        self.assertNotEqual(text, "")

    def test_a_value_that_is_not_a_list_is_empty_and_not_marked(self):
        text, truncated = EXPORT._bounded_lines("abc", self.LIMIT)
        self.assertEqual(text, "")
        self.assertFalse(truncated)

    def test_an_over_long_list_is_marked_too(self):
        """The other route to the same marker, so it is not one branch wide."""
        text, truncated = EXPORT._bounded_lines(["x" * 400] * 5, self.LIMIT)
        self.assertTrue(truncated)
        self.assertIn(EXPORT.TRUNCATION_MARKER, text)


if __name__ == "__main__":
    unittest.main()
