"""The provider byte budgets, and what line-ending folding does to one.

These ceilings exist so untrusted provider input cannot grow without limit.
Eight of the eighteen MAX_PROVIDER_* constants were named by no test when the
register was assessed; this takes the aggregate one, which is the one a
component of ordinary files actually reaches.

The comparison that matters is between two components holding the same number
of bytes on disk and differing only in line endings. Comparing the same logical
content instead makes the CRLF component larger and hides the effect, which is
what an earlier probe of this did.

Covers OBL-GLOBS-061 and OBL-GLOBS-062.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from boundver._utils import ConfigError
from boundver.providers import (
    MAX_PROVIDER_TOTAL_BYTES,
    ProviderError,
    _ProviderEntryCollector,
)

from tests._scenarios import Scenario

#: Small enough to reach with a handful of files, large enough that a single
#: file still fits and the interesting case is the aggregate.
BUDGET = 4000

#: One file's size on disk, identical for both line endings.
PER_FILE = 1600


def _body(crlf: bool) -> str:
    """Content that occupies PER_FILE bytes on disk either way.

    The CRLF form is every fourth byte a line ending, so folding shrinks it by
    a quarter. The LF form has one line ending in total, so folding is a no-op.
    """
    if crlf:
        return "ab\n" * (PER_FILE // 4)
    return "a" * (PER_FILE - 1) + "\n"


class _Budgeted:
    """One component of *count* files, all with the same line endings."""

    def __init__(self, crlf: bool, count: int) -> None:
        self.scene = Scenario()
        self.scene.component("svc", path="svc", boundary=["api"])
        for index in range(count):
            self.scene.file(f"svc/api/f{index}.yaml", _body(crlf), crlf=crlf)
        self.scene.commit()
        self.count = count

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def bytes_on_disk(self) -> int:
        return sum(
            len(self.scene.blob(f"svc/api/f{i}.yaml")) for i in range(self.count)
        )

    def bytes_after_folding(self) -> int:
        return sum(
            len(self.scene.blob(f"svc/api/f{i}.yaml").replace(b"\r\n", b"\n"))
            for i in range(self.count)
        )

    def accepted(self) -> bool:
        with patch("boundver.providers.MAX_PROVIDER_TOTAL_BYTES", BUDGET):
            try:
                self.scene.digest("svc", "boundary")
                return True
            except ConfigError:
                return False


class CollectorBudgetSeparationTests(unittest.TestCase):
    """OBL-GLOBS-061: reading input must not spend the output allowance."""

    def test_source_and_output_are_charged_to_different_counters(self):
        collector = _ProviderEntryCollector()
        before_output = collector.remaining_output_bytes
        before_source = collector.remaining_source_bytes

        collector.add_source(b"x" * 1000)
        self.assertEqual(collector.remaining_output_bytes, before_output)
        self.assertLess(collector.remaining_source_bytes, before_source)

        after_source = collector.remaining_source_bytes
        collector.add("label", b"y" * 1000)
        self.assertEqual(collector.remaining_source_bytes, after_source)
        self.assertLess(collector.remaining_output_bytes, before_output)

    def test_a_full_source_allowance_still_leaves_a_full_output_allowance(self):
        """A canonical provider reads its input, then emits its own bytes."""
        collector = _ProviderEntryCollector(
            max_total_source_bytes=2048, max_total_bytes=2048
        )
        collector.add_source(b"x" * 2048)
        self.assertEqual(collector.remaining_source_bytes, 0)
        self.assertEqual(collector.remaining_output_bytes, 2048)
        collector.add("label", b"y" * 2048)
        self.assertEqual(collector.remaining_output_bytes, 0)

    def test_each_counter_refuses_on_its_own(self):
        collector = _ProviderEntryCollector(
            max_total_source_bytes=100, max_total_bytes=100
        )
        with self.assertRaises(ProviderError) as caught:
            collector.add_source(b"x" * 101)
        self.assertIn("source files exceed", str(caught.exception))

        collector = _ProviderEntryCollector(
            max_total_source_bytes=100, max_total_bytes=100
        )
        with self.assertRaises(ProviderError):
            collector.add("label", b"y" * 101)


class LineEndingBudgetTests(unittest.TestCase):
    """OBL-GLOBS-062: folding must not refund the aggregate allowance."""

    def test_the_ceiling_is_the_shipped_constant_unless_patched(self):
        self.assertGreater(MAX_PROVIDER_TOTAL_BYTES, BUDGET)

    def test_a_component_inside_the_budget_is_accepted_either_way(self):
        for crlf in (False, True):
            with self.subTest(crlf=crlf), _Budgeted(crlf, 2) as component:
                self.assertLess(component.bytes_on_disk(), BUDGET)
                self.assertTrue(component.accepted())

    def test_the_two_shapes_really_do_hold_the_same_bytes(self):
        """Without this the comparison below would be measuring file size."""
        with _Budgeted(False, 3) as lf, _Budgeted(True, 3) as crlf:
            self.assertEqual(lf.bytes_on_disk(), crlf.bytes_on_disk())
            self.assertEqual(lf.bytes_after_folding(), lf.bytes_on_disk())
            self.assertLess(crlf.bytes_after_folding(), crlf.bytes_on_disk())

    def test_a_single_file_over_the_budget_is_refused_either_way(self):
        """The read itself is bounded before folding, and that part is right."""
        for crlf in (False, True):
            with self.subTest(crlf=crlf):
                scene = Scenario()
                try:
                    scene.component("svc", path="svc", boundary=["api"])
                    oversized = BUDGET * 2
                    body = (
                        "ab\n" * (oversized // 4) if crlf
                        else "a" * (oversized - 1) + "\n"
                    )
                    scene.file("svc/api/one.yaml", body, crlf=crlf)
                    scene.commit()
                    with patch("boundver.providers.MAX_PROVIDER_TOTAL_BYTES", BUDGET):
                        with self.assertRaises(ConfigError):
                            scene.digest("svc", "boundary")
                finally:
                    scene.close()

    def test_the_same_bytes_on_disk_get_the_same_verdict(self):
        """CRLF folding does not refund source bytes already consumed.

        Three files of 1600 bytes each is 4800 bytes on disk whichever line
        ending they use. The LF component is refused against a 4000-byte
        budget. The CRLF component folds to 3600 and is accepted, so the
        aggregate ceiling let 4800 bytes through. The read of each file is
        bounded correctly; what is charged afterwards is the folded size, so
        the refund accumulates across files and a single file never shows it.
        """
        with _Budgeted(False, 3) as lf, _Budgeted(True, 3) as crlf:
            self.assertEqual(lf.bytes_on_disk(), crlf.bytes_on_disk())
            self.assertEqual(crlf.accepted(), lf.accepted())

    def test_both_oversized_source_shapes_are_refused(self):
        with _Budgeted(False, 3) as lf, _Budgeted(True, 3) as crlf:
            self.assertGreater(lf.bytes_on_disk(), BUDGET)
            self.assertLess(crlf.bytes_after_folding(), BUDGET)
            self.assertFalse(lf.accepted())
            self.assertFalse(crlf.accepted())


if __name__ == "__main__":
    unittest.main()
