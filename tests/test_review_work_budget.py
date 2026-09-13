"""The review work budget, at the reservation rather than at the ceiling.

The aggregate step ceiling has tests: a large enough graph is refused, and the
refusal names the limit. What had none is the arithmetic underneath it - how
much work each step reserves before doing it. A reservation that is too small
does not fail; it lets the traversal past a limit that was supposed to stop it,
and only at a scale no ordinary test builds.

Two mutants survived every test that drives a review. Accepting a negative
reservation lets a caller refund steps it never spent, so a long traversal can
run indefinitely under a ceiling that looks enforced. Reserving the smaller of
two mapping sizes under-reserves whenever the two sides differ, which is the
normal case for a diff. Both are invisible from the outside until the budget
actually binds, so both are asserted here directly.

Covers OBL-GLOBS-044.
"""

from __future__ import annotations

import unittest

from boundver._review import (
    MAX_REVIEW_RESULT_BYTES,
    MAX_REVIEW_WORK_STEPS,
    _ReviewWorkBudget,
    _sorted_mapping_key_union,
)
from boundver._utils import GuardrailError


class ReservationValidityTests(unittest.TestCase):
    """What ensure() will accept as an amount of work."""

    def test_a_negative_reservation_is_refused(self):
        budget = _ReviewWorkBudget()
        for amount in (-1, -1000, -MAX_REVIEW_WORK_STEPS):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError) as raised:
                    budget.ensure(amount)
                self.assertIn("non-negative integer", str(raised.exception))

    def test_a_non_integer_reservation_is_refused(self):
        budget = _ReviewWorkBudget()
        for amount in (1.0, "1", None, True):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    budget.ensure(amount)

    def test_zero_and_positive_reservations_are_accepted(self):
        """The contrast: ensure() is not simply refusing everything."""
        budget = _ReviewWorkBudget()
        budget.ensure(0)
        budget.ensure(1)
        budget.ensure(MAX_REVIEW_WORK_STEPS)

    def test_a_negative_spend_cannot_refund_the_budget(self):
        """The substance of refusing a negative: steps must not go backwards."""
        budget = _ReviewWorkBudget()
        budget.spend(100)
        with self.assertRaises(ValueError):
            budget.spend(-100)
        self.assertEqual(budget.steps, 100)

    def test_the_ceiling_is_reached_by_spending(self):
        """The premise: the ceiling these reservations protect is real."""
        budget = _ReviewWorkBudget()
        budget.spend(MAX_REVIEW_WORK_STEPS)
        with self.assertRaises(GuardrailError):
            budget.spend(1)


class RowReservationTests(unittest.TestCase):
    """A reserved row must be charged for what it retains.

    reserve_row charges a fixed overhead per row plus the encoded bytes of
    every string value the row keeps. Charging only the overhead makes a
    row of arbitrary strings cost a constant, so the byte ceiling stops
    bounding anything the rows actually hold, and no test noticed
    (MUT-FACETS-202).
    """

    OVERHEAD = 128

    def test_a_row_is_charged_for_its_string_values(self):
        budget = _ReviewWorkBudget()
        budget.reserve_row("svc", overhead=self.OVERHEAD)
        small = budget.result_bytes

        budget = _ReviewWorkBudget()
        budget.reserve_row("s" * 1000, overhead=self.OVERHEAD)
        large = budget.result_bytes

        self.assertEqual(large - small, 1000 - len("svc"))

    def test_a_row_is_charged_its_overhead_even_with_no_values(self):
        """The premise: the difference above is the values, not the row."""
        budget = _ReviewWorkBudget()
        budget.reserve_row(overhead=self.OVERHEAD)
        self.assertEqual(budget.result_bytes, self.OVERHEAD)
        self.assertEqual(budget.result_rows, 1)

    def test_non_string_values_are_not_charged_as_bytes(self):
        """Only retained strings carry a length worth charging."""
        budget = _ReviewWorkBudget()
        budget.reserve_row(1, None, ["x"], overhead=self.OVERHEAD)
        self.assertEqual(budget.result_bytes, self.OVERHEAD)

    def test_enough_large_rows_reach_the_byte_ceiling(self):
        """The substance: the charge is what makes the ceiling bind."""
        budget = _ReviewWorkBudget()
        payload = "s" * 100_000
        with self.assertRaises(GuardrailError):
            for _ in range(MAX_REVIEW_RESULT_BYTES // 100_000 + 2):
                budget.reserve_row(payload, overhead=self.OVERHEAD)


class KeyUnionReservationTests(unittest.TestCase):
    """A key union must reserve for the side that actually costs the most."""

    #: Deliberately lopsided: the union costs the larger side, so reserving the
    #: smaller one under-reserves by exactly the difference.
    BEFORE = {f"key-{index:03d}": index for index in range(40)}
    AFTER = {"key-000": 0, "key-999": 999}

    def _budget_with_room_for(self, amount: int) -> _ReviewWorkBudget:
        budget = _ReviewWorkBudget()
        budget.spend(MAX_REVIEW_WORK_STEPS - amount)
        return budget

    def test_the_larger_side_is_what_must_fit(self):
        budget = self._budget_with_room_for(len(self.AFTER))
        with self.assertRaises(GuardrailError):
            _sorted_mapping_key_union(self.BEFORE, self.AFTER, budget)

    def test_room_for_the_whole_union_is_enough(self):
        """The contrast: the refusal above is about the amount, not the call."""
        budget = self._budget_with_room_for(MAX_REVIEW_WORK_STEPS)
        names = _sorted_mapping_key_union(self.BEFORE, self.AFTER, budget)
        self.assertEqual(names, sorted(set(self.BEFORE) | set(self.AFTER)))

    def test_the_two_sides_really_do_differ_in_size(self):
        """Without this the comparison above would be measuring nothing."""
        self.assertGreater(len(self.BEFORE), len(self.AFTER))
        self.assertTrue(set(self.AFTER) - set(self.BEFORE))

    def test_the_union_is_sorted_and_deduplicated(self):
        budget = self._budget_with_room_for(MAX_REVIEW_WORK_STEPS)
        names = _sorted_mapping_key_union(
            {"b": 1, "a": 2}, {"a": 3, "c": 4}, budget
        )
        self.assertEqual(names, ["a", "b", "c"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
