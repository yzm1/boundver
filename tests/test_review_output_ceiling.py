"""Whether the review text ceiling counts the bytes that safe output writes.

`review_text_lines` measures the safely escaped rendering of every raw line.
The CLI's output wrapper performs that same escaping and may additionally use
backslash replacement for a legacy stream encoding. The ceiling must cover the
larger representation so accepted text cannot expand beyond the bound.

Covers OBL-FACETS-001.
"""

from __future__ import annotations

import inspect
import io
import unittest
from unittest.mock import patch

from boundver._output import safe_print
from boundver._review import MAX_REVIEW_RESULT_BYTES, review_text_lines
from boundver._utils import GuardrailError, _safe_display_text

from tests._parity import run_cli
from tests._scenarios import Scenario

LONE_SURROGATE = chr(0xDC80)

#: Every character class the obligation names that a UTF-8 stream can carry.
WRITABLE = (
    ("plain ascii", "a"),
    ("C0 control", chr(1)),
    ("escape", chr(27)),
    ("line separator", " "),
    ("paragraph separator", " "),
    ("non-ascii", "中"),
    ("astral", "\U0001f600"),
)


def _measured(line: str) -> int:
    """The arithmetic the ceiling uses, from _review.py."""
    display = _safe_display_text(line)
    return max(
        len(display.encode("utf-8")),
        len(display.encode("ascii", errors="backslashreplace")),
    ) + 1


def _written(line: str, encoding: str) -> int:
    """The bytes the safe output wrapper emits for one line."""
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding=encoding, newline="\n")
    safe_print(line, file=stream)
    stream.flush()
    return len(buffer.getvalue())


class MeasurementAccuracyTests(unittest.TestCase):
    """The ceiling must count what a write costs, not an approximation."""

    def test_the_text_view_measures_the_safe_display_form(self):
        """Bind the accounting tests to the implementation's render step."""
        source = inspect.getsource(review_text_lines)
        self.assertIn("display = _safe_display_text(line)", source)
        self.assertIn('display.encode("ascii", errors="backslashreplace")', source)

    def test_the_count_matches_the_largest_stream_rendering(self):
        for label, character in WRITABLE:
            with self.subTest(character=label):
                line = character * 100
                self.assertEqual(
                    _measured(line),
                    max(_written(line, "utf-8"), _written(line, "ascii")),
                )

    def test_a_dense_mixture_still_matches(self):
        line = "".join(character for _label, character in WRITABLE) * 50
        self.assertEqual(
            _measured(line),
            max(_written(line, "utf-8"), _written(line, "ascii")),
        )

    def test_a_lone_surrogate_is_escaped_and_counted(self):
        line = "a" + LONE_SURROGATE + "b"
        display = _safe_display_text(line)
        self.assertNotIn(LONE_SURROGATE, display)
        self.assertIn("\\udc80", display)
        self.assertEqual(
            _measured(line),
            max(_written(line, "utf-8"), _written(line, "ascii")),
        )


class CeilingEnforcementTests(unittest.TestCase):
    """The ceiling must refuse rather than emit a partial document."""

    def _result(self, scene: Scenario, base: str, target: str) -> dict:
        import json

        completed = run_cli(
            scene.root, "review", "--base", base, "--target", target,
            "--format", "json",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def _reviewable(self):
        scene = Scenario()
        scene.component("svc", path="svc", boundary=["api"], consumers=["sdk"])
        scene.component("sdk", path="sdk", provider="leaf")
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("sdk/index.ts", "export const x = 1;\n")
        scene.commit()
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("lock")
        base = scene.head()
        scene.append_line("svc/api/v1.yaml", "change: true")
        scene.commit("edit")
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("relock")
        return scene, base, scene.head()

    def test_an_ordinary_review_renders_within_the_ceiling(self):
        scene, base, target = self._reviewable()
        try:
            lines = review_text_lines(self._result(scene, base, target))
            self.assertLessEqual(
                sum(_measured(line) for line in lines), MAX_REVIEW_RESULT_BYTES
            )
        finally:
            scene.close()

    def test_the_shipped_count_is_the_arithmetic_measured_here(self):
        """The binding the accuracy tests above rest on.

        _measured is a copy of the expression in _review.py, and every
        test in MeasurementAccuracyTests compares that copy against what a
        stream emits. That proves the formula is right. It does not prove
        the product uses it: dropping the per-line newline from the real
        sum left all of them green (MUT-FACETS-201), because none of them
        calls the shipped code.

        Patching the ceiling to the measured size, and to one byte below
        it, pins the two together at the only place it matters - where the
        sum is compared against the limit.
        """
        scene, base, target = self._reviewable()
        try:
            result = self._result(scene, base, target)
            lines = review_text_lines(result)
            exact = sum(_measured(line) for line in lines)
            self.assertGreater(len(lines), 0)

            with patch("boundver._review.MAX_REVIEW_RESULT_BYTES", exact):
                self.assertEqual(review_text_lines(result), lines)

            with patch("boundver._review.MAX_REVIEW_RESULT_BYTES", exact - 1):
                with self.assertRaises(GuardrailError):
                    review_text_lines(result)
        finally:
            scene.close()

    def test_the_ceiling_refuses_rather_than_truncating(self):
        scene, base, target = self._reviewable()
        try:
            result = self._result(scene, base, target)
            with patch("boundver._review.MAX_REVIEW_RESULT_BYTES", 32):
                with self.assertRaises(GuardrailError) as caught:
                    review_text_lines(result)
            message = str(caught.exception)
            self.assertIn("complete-output limit", message)
            self.assertIn("No partial review result was emitted", message)
        finally:
            scene.close()

    def test_the_refusal_points_at_the_machine_contract(self):
        """A user told only 'too big' has nowhere to go."""
        scene, base, target = self._reviewable()
        try:
            result = self._result(scene, base, target)
            with patch("boundver._review.MAX_REVIEW_RESULT_BYTES", 32):
                with self.assertRaises(GuardrailError) as caught:
                    review_text_lines(result)
            self.assertIn("--format json", str(caught.exception))
        finally:
            scene.close()


if __name__ == "__main__":
    unittest.main()
