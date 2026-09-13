"""A structural explanation has endpoint-order-independent input accounting.

Canonical providers charge both bounded source bytes and canonical entries to
one aggregate. The same endpoint pair therefore has the same threshold in
either direction, even when one source contains much more whitespace.

Covers OBL-GLOBS-002.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from boundver._provider_diff import StructuralDiffBudget
from boundver._utils import GuardrailError
from boundver.providers import OpenApiCanonicalProvider, ProviderContext

LIMITED = "limited"
COMPLETE = "complete"


def _document(version: str) -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": version},
        "paths": {"/a": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }


#: The same structure twice, written two ways. Canonicalisation removes the
#: difference, which is the point: these two documents cost nearly the same
#: once reserved and very differently while being read.
COMPACT = json.dumps(_document("1"), separators=(",", ":")).encode("utf-8")
PADDED = json.dumps(_document("2"), indent=48).encode("utf-8")


def _context(raw: bytes) -> ProviderContext:
    files = {"svc/api.openapi": raw}

    def read_file(repo_relative: str) -> bytes:
        return files[repo_relative]

    def read_file_limited(repo_relative: str, max_bytes: int) -> bytes:
        value = files[repo_relative]
        if len(value) > max_bytes:
            raise GuardrailError("fixture exceeds requested read limit")
        return value

    def list_files(prefix: str):
        return sorted(name for name in files if name.startswith(prefix))

    return ProviderContext(
        repo_root=Path("."),
        component_path="svc",
        boundary_cfg={"paths": ["api.openapi"]},
        source="head",
        read_file=read_file,
        read_file_limited=read_file_limited,
        list_files=list_files,
    )


def _verdict(base: bytes, target: bytes, limit: int) -> str:
    """Complete or limited, for one ordering at one allowance."""
    try:
        OpenApiCanonicalProvider().structural_diff(
            _context(base),
            _context(target),
            StructuralDiffBudget(max_input_bytes=limit),
        )
    except GuardrailError as exc:
        if "aggregate input limit" in str(exc):
            return LIMITED
        raise
    return COMPLETE


def _threshold(base: bytes, target: bytes) -> int:
    """The smallest allowance at which this ordering completes."""
    low, high = 1, 1 << 16
    while low < high:
        middle = (low + high) // 2
        if _verdict(base, target, middle) == COMPLETE:
            high = middle
        else:
            low = middle + 1
    return low


class BudgetPremiseTests(unittest.TestCase):
    """Both orderings agree away from the boundary."""

    def test_a_generous_allowance_completes_either_way(self):
        for base, target in ((COMPACT, PADDED), (PADDED, COMPACT)):
            with self.subTest(base=len(base)):
                self.assertEqual(_verdict(base, target, 1 << 16), COMPLETE)

    def test_a_tight_allowance_is_refused_either_way(self):
        for base, target in ((COMPACT, PADDED), (PADDED, COMPACT)):
            with self.subTest(base=len(base)):
                self.assertEqual(_verdict(base, target, 64), LIMITED)


class BudgetSymmetryTests(unittest.TestCase):
    """OBL-GLOBS-002: one verdict per pair, whichever end is called the base."""

    def test_the_verdict_does_not_depend_on_which_endpoint_is_the_base(self):
        limit = len(PADDED)
        self.assertEqual(
            _verdict(COMPACT, PADDED, limit), _verdict(PADDED, COMPACT, limit)
        )

    def test_the_two_orderings_have_the_same_threshold(self):
        self.assertEqual(_threshold(COMPACT, PADDED), _threshold(PADDED, COMPACT))

    def test_the_shared_threshold_accounts_for_both_endpoint_inputs(self):
        with_padded_base = _threshold(PADDED, COMPACT)
        with_padded_target = _threshold(COMPACT, PADDED)
        self.assertEqual(with_padded_base, with_padded_target)
        self.assertGreater(with_padded_base, len(PADDED))
        for base, target in ((COMPACT, PADDED), (PADDED, COMPACT)):
            self.assertEqual(_verdict(base, target, with_padded_base), COMPLETE)
            self.assertEqual(_verdict(base, target, with_padded_base - 1), LIMITED)

    def test_more_source_padding_raises_the_shared_threshold(self):
        thresholds = {}
        for indent in (16, 48, 96):
            padded = json.dumps(_document("2"), indent=indent).encode("utf-8")
            forward = _threshold(COMPACT, padded)
            reverse = _threshold(padded, COMPACT)
            self.assertEqual(forward, reverse)
            thresholds[indent] = forward
        self.assertEqual(
            list(thresholds.values()),
            sorted(thresholds.values()),
            thresholds,
        )
        self.assertEqual(len(set(thresholds.values())), len(thresholds))

    def test_the_allowance_is_an_aggregate_over_both_endpoints(self):
        """Neither document alone exceeds it; together they do.

        Without this the accumulation itself is untested: a budget that
        replaced its running total with each reservation instead of adding to
        it would still refuse everything these other tests refuse, because
        they never depend on two reservations summing.
        """
        first = StructuralDiffBudget(max_input_bytes=1 << 16)
        OpenApiCanonicalProvider().structural_diff(
            _context(COMPACT), _context(COMPACT), first
        )
        one_pair = first.input_bytes
        self.assertGreater(one_pair, 0)

        both = StructuralDiffBudget(max_input_bytes=1 << 16)
        OpenApiCanonicalProvider().structural_diff(
            _context(COMPACT), _context(PADDED), both
        )
        self.assertGreater(both.input_bytes, one_pair // 2)

        # An allowance that fits either document but not their sum.
        allowance = both.input_bytes - 1
        self.assertGreater(allowance, len(COMPACT))
        self.assertEqual(_verdict(COMPACT, PADDED, max(allowance, len(PADDED) + 1)), LIMITED)

    def test_source_bytes_and_canonical_entries_are_both_charged(self):
        self.assertGreater(len(PADDED), 10 * len(COMPACT))
        budget = StructuralDiffBudget(max_input_bytes=1 << 16)
        OpenApiCanonicalProvider().structural_diff(
            _context(PADDED), _context(COMPACT), budget
        )
        self.assertGreater(budget.input_bytes, len(PADDED) + len(COMPACT))


if __name__ == "__main__":
    unittest.main()
