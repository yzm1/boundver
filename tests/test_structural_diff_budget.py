"""The structural-diff budget, ceiling by ceiling.

Five of the eight MAX_PROVIDER_DIFF_* constants were named by no test when the
register was assessed. They bound the work `why` does explaining a canonical
provider's drift, which is work driven entirely by two untrusted documents, so
a missing bound is a way for a repository to make boundver expensive.

Every ceiling is asserted at its boundary rather than somewhere past it, and
every refusal is asserted to name its limit and to promise no partial result.
A message that said only "too big" would leave a user with no idea which
document to shrink.

Covers OBL-GLOBS-060 and OBL-GLOBS-063.
"""

from __future__ import annotations

import unittest

from boundver._provider_diff import (
    MAX_PROVIDER_DIFF_DEPTH,
    MAX_PROVIDER_DIFF_INPUT_BYTES,
    MAX_PROVIDER_DIFF_PATH_BYTES,
    MAX_PROVIDER_DIFF_RESULT_BYTES,
    MAX_PROVIDER_DIFF_ROWS,
    MAX_PROVIDER_DIFF_WORK_STEPS,
    StructuralDiffBudget,
)
from boundver._utils import GuardrailError

#: Every refusal carries this, so a caller knows nothing half-built escaped.
NO_PARTIAL = "No partial structural result was emitted"


def _budget(**overrides) -> StructuralDiffBudget:
    """A budget with every ceiling small enough to reach deliberately."""
    limits = dict(
        max_input_bytes=100,
        max_work_steps=10,
        max_rows=3,
        max_result_bytes=400,
        max_depth=4,
        max_path_bytes=20,
    )
    limits.update(overrides)
    return StructuralDiffBudget(**limits)


class LimitValidationTests(unittest.TestCase):
    """A budget built from nonsense must refuse before any work runs."""

    def test_a_negative_or_non_integer_limit_is_refused(self):
        for name in (
            "max_input_bytes", "max_work_steps", "max_rows",
            "max_result_bytes", "max_depth", "max_path_bytes",
        ):
            for value in (-1, "3", 1.5, True):
                with self.subTest(limit=name, value=value):
                    with self.assertRaises(ValueError):
                        StructuralDiffBudget(**{name: value})

    def test_the_shipped_defaults_are_the_declared_constants(self):
        budget = StructuralDiffBudget()
        self.assertEqual(budget.max_input_bytes, MAX_PROVIDER_DIFF_INPUT_BYTES)
        self.assertEqual(budget.max_work_steps, MAX_PROVIDER_DIFF_WORK_STEPS)
        self.assertEqual(budget.max_rows, MAX_PROVIDER_DIFF_ROWS)
        self.assertEqual(budget.max_result_bytes, MAX_PROVIDER_DIFF_RESULT_BYTES)
        self.assertEqual(budget.max_depth, MAX_PROVIDER_DIFF_DEPTH)
        self.assertEqual(budget.max_path_bytes, MAX_PROVIDER_DIFF_PATH_BYTES)


class ReservationValidityTests(unittest.TestCase):
    """What ensure_work will accept as an amount, not as a limit.

    LimitValidationTests covers the ceilings a budget is built with.
    Nothing covered the amounts charged against them, so dropping the
    negative half of the reservation check went unnoticed
    (MUT-GLOBS-214). A negative charge refunds work never done, which
    leaves a traversal running under a ceiling that still looks enforced.
    The same guard, worded almost identically, was untested in
    _review._ReviewWorkBudget as well.
    """

    def test_a_negative_reservation_is_refused(self):
        for amount in (-1, -10, -MAX_PROVIDER_DIFF_WORK_STEPS):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError) as raised:
                    _budget().ensure_work(amount, depth=0)
                self.assertIn("non-negative integer", str(raised.exception))

    def test_a_non_integer_reservation_is_refused(self):
        for amount in (1.0, "1", None, True):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    _budget().ensure_work(amount, depth=0)

    def test_zero_and_positive_reservations_are_accepted(self):
        """The contrast: ensure_work is not refusing everything."""
        budget = _budget()
        budget.ensure_work(0, depth=0)
        budget.ensure_work(1, depth=0)

    def test_a_negative_charge_cannot_refund_spent_work(self):
        """The substance: work_steps must not run backwards."""
        budget = _budget()
        budget.spend(depth=0)
        spent = budget.work_steps
        with self.assertRaises(ValueError):
            budget.ensure_work(-spent, depth=0)
        self.assertEqual(budget.work_steps, spent)


class CeilingBoundaryTests(unittest.TestCase):
    """OBL-GLOBS-060: every way of breaching a ceiling is classified alike."""

    def assertRefused(self, budget, action, limit_text):
        with self.assertRaises(GuardrailError) as caught:
            action(budget)
        message = str(caught.exception)
        self.assertIn(limit_text, message)
        self.assertIn(NO_PARTIAL, message)
        self.assertTrue(budget.exhausted)
        return message

    def test_the_input_ceiling_holds_at_its_boundary(self):
        accepted = _budget()
        accepted.reserve_input("l", b"x" * 99)
        self.assertEqual(accepted.input_bytes, 100)
        self.assertFalse(accepted.exhausted)

        self.assertRefused(
            _budget(), lambda b: b.reserve_input("l", b"x" * 100),
            "100-byte aggregate input limit",
        )

    def test_the_input_charge_includes_the_label(self):
        """A caller could otherwise slip unbounded bytes past it as labels."""
        budget = _budget(max_input_bytes=1000)
        budget.reserve_input("twelve_chars", b"x" * 10)
        self.assertEqual(budget.input_bytes, len("twelve_chars") + 10)

    def test_the_work_ceiling_holds_at_its_boundary(self):
        accepted = _budget()
        for _ in range(10):
            accepted.spend(depth=1)
        self.assertEqual(accepted.work_steps, 10)

        def overspend(budget):
            for _ in range(11):
                budget.spend(depth=1)

        self.assertRefused(_budget(), overspend, "10-step aggregate work limit")

    def test_the_depth_ceiling_holds_at_its_boundary(self):
        accepted = _budget()
        accepted.spend(depth=4)
        self.assertRefused(
            _budget(), lambda b: b.spend(depth=5), "4-level nesting limit"
        )

    def test_the_pointer_ceiling_holds_at_its_boundary(self):
        """A child is parent + '/' + segment, so 18 + 1 + 1 is exactly 20."""
        accepted = _budget()
        child = accepted.pointer_child("a" * 18, "b")
        self.assertEqual(len(child), 20)
        self.assertRefused(
            _budget(), lambda b: b.pointer_child("a" * 19, "b"),
            "20-byte JSON-pointer limit",
        )

    def test_a_pointer_segment_is_escaped_before_it_is_measured(self):
        """RFC 6901 escaping grows a segment, and the limit sees the growth."""
        budget = _budget(max_path_bytes=1000)
        self.assertEqual(budget.pointer_child("/x", "a/b"), "/x/a~1b")
        self.assertEqual(budget.pointer_child("/x", "a~b"), "/x/a~0b")

    def test_the_row_ceiling_holds_at_its_boundary(self):
        accepted = _budget()
        for index in range(3):
            accepted.change(
                kind="added", path=f"/p{index}",
                before_type=None, after_type="string",
            )
        self.assertEqual(accepted.result_rows, 3)

        def overflow(budget):
            for index in range(4):
                budget.change(
                    kind="added", path=f"/p{index}",
                    before_type=None, after_type="string",
                )

        self.assertRefused(_budget(), overflow, "3-row aggregate output limit")

    def test_the_result_byte_ceiling_holds_at_its_boundary(self):
        def overflow(budget):
            for index in range(50):
                budget.change(
                    kind="added", path=f"/{'p' * 10}{index}",
                    before_type=None, after_type="string",
                )

        self.assertRefused(
            _budget(max_rows=1000), overflow, "400-byte aggregate output limit"
        )

    def test_an_unsupported_change_is_refused_before_it_is_counted(self):
        budget = _budget()
        for kind, before, after in (
            ("renamed", None, "string"),
            ("added", "widget", "string"),
            ("added", None, "widget"),
        ):
            with self.subTest(kind=kind, before=before, after=after):
                with self.assertRaises(Exception) as caught:
                    budget.change(
                        kind=kind, path="/p",
                        before_type=before, after_type=after,
                    )
                self.assertNotIsInstance(caught.exception, GuardrailError)


class ExhaustionTests(unittest.TestCase):
    """OBL-GLOBS-063: once a budget is spent it stays spent.

    A budget that forgot it had been exhausted would let a later endpoint
    start again on an allowance the earlier one already used.
    """

    def test_exhaustion_is_set_before_the_error_escapes(self):
        budget = _budget()
        self.assertFalse(budget.exhausted)
        with self.assertRaises(GuardrailError):
            budget.reserve_input("l", b"x" * 200)
        self.assertTrue(budget.exhausted)

    def test_exhaustion_survives_a_later_successful_call(self):
        budget = _budget()
        with self.assertRaises(GuardrailError):
            budget.spend(depth=99)
        self.assertTrue(budget.exhausted)
        budget.spend(depth=1)
        self.assertTrue(budget.exhausted)

    def test_spent_input_is_not_returned_by_a_refusal(self):
        """The refused amount stays charged, so a retry cannot reuse it."""
        budget = _budget(max_input_bytes=50)
        budget.reserve_input("a", b"x" * 20)
        charged = budget.input_bytes
        with self.assertRaises(GuardrailError):
            budget.reserve_input("b", b"x" * 100)
        self.assertGreater(budget.input_bytes, charged)


if __name__ == "__main__":
    unittest.main()
