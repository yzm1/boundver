"""Two implementations of one contract, and a metadata graph that shares.

A provider result is checked twice: once as it is built, by the collector the
built-in providers append to, and once as a finished object, by the validator
that inspects whatever `resolve()` returned. Two implementations of the same
five ceilings is exactly the arrangement that drifts, so the interesting
question is not whether either is right but whether they agree.

The metadata check has a different hazard. A graph where one subtree is
referenced twice is not a cycle, and a walk that cannot tell the difference
either refuses valid metadata or spends exponential time proving it is fine.

A third question meets the first one from the other side. The label a
path reduces to is not unique - a file at exactly the component path and
a same-named file inside it both reduce to that name - so what stops a
collision is the uniqueness rule those two implementations share.

Covers OBL-PROVIDERS-006, OBL-PROVIDERS-032 and OBL-PROVIDERS-036.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import boundver.providers as providers
from boundver.providers import (
    MAX_PROVIDER_METADATA_DEPTH,
    MAX_PROVIDER_METADATA_NODES,
    ProviderError,
    ResolvedBoundary,
    _component_relative_path,
    _metadata_error,
    _ProviderEntryCollector,
    _resolved_boundary_error,
)

#: The five ceilings, lowered so a corpus can sit exactly on each of them.
#: Two of the real values are 50 MB and 256 MB, which no test should allocate.
SMALL = {
    "MAX_PROVIDER_ENTRIES": 4,
    "MAX_PROVIDER_ENTRY_BYTES": 32,
    "MAX_PROVIDER_TOTAL_BYTES": 64,
    "MAX_PROVIDER_LABEL_BYTES": 8,
    "MAX_PROVIDER_TOTAL_LABEL_BYTES": 20,
}


def _corpus():
    """One entry list per ceiling, sitting exactly on it and one past it."""
    entries = SMALL["MAX_PROVIDER_ENTRIES"]
    entry_bytes = SMALL["MAX_PROVIDER_ENTRY_BYTES"]
    label_bytes = SMALL["MAX_PROVIDER_LABEL_BYTES"]
    yield "entry count at", [(f"l{index}", b"x") for index in range(entries)]
    yield "entry count over", [(f"l{index}", b"x") for index in range(entries + 1)]
    yield "entry bytes at", [("a", b"x" * entry_bytes)]
    yield "entry bytes over", [("a", b"x" * (entry_bytes + 1))]
    yield "total bytes at", [("a", b"x" * 32), ("b", b"x" * 32)]
    yield "total bytes over", [("a", b"x" * 32), ("b", b"x" * 32), ("c", b"x")]
    yield "label bytes at", [("a" * label_bytes, b"x")]
    yield "label bytes over", [("a" * (label_bytes + 1), b"x")]
    yield "total label at", [("a" * 8, b"x"), ("b" * 8, b"x"), ("c" * 4, b"x")]
    yield "total label over", [("a" * 8, b"x"), ("b" * 8, b"x"), ("c" * 5, b"x")]


def _collector_verdict(entries):
    """What the incremental implementation says, as a message or None."""
    collector = _ProviderEntryCollector()
    try:
        for label, content in entries:
            collector.add(label, content)
    except ProviderError as exc:
        return str(exc)
    return None


def _validator_verdict(entries):
    """What the whole-result implementation says, as a message or None."""
    return _resolved_boundary_error(ResolvedBoundary(entries=list(entries)))


class CeilingParityTests(unittest.TestCase):
    """OBL-PROVIDERS-032: same entry list, same verdict, same named number."""

    def test_the_two_implementations_agree_over_the_corpus(self):
        with patch.multiple(providers, **SMALL):
            for label, entries in _corpus():
                with self.subTest(case=label):
                    self.assertEqual(
                        _collector_verdict(entries), _validator_verdict(entries)
                    )

    def test_each_ceiling_is_actually_exercised(self):
        """The premise: the corpus straddles every boundary it claims to."""
        with patch.multiple(providers, **SMALL):
            verdicts = {label: _collector_verdict(entries) for label, entries in _corpus()}
        for label, verdict in verdicts.items():
            with self.subTest(case=label):
                if label.endswith(" at"):
                    self.assertIsNone(verdict)
                else:
                    self.assertIsNotNone(verdict)

    def test_the_refusals_name_the_lowered_number(self):
        """So the agreement is on the message and not only on the verdict."""
        with patch.multiple(providers, **SMALL):
            verdicts = dict(
                (label, _collector_verdict(entries))
                for label, entries in _corpus()
                if label.endswith(" over")
            )
        self.assertIn("4-item limit", verdicts["entry count over"])
        self.assertIn("32-byte limit", verdicts["entry bytes over"])
        self.assertIn("64-byte aggregate limit", verdicts["total bytes over"])
        self.assertIn("8-byte limit", verdicts["label bytes over"])
        self.assertIn("20-byte aggregate limit", verdicts["total label over"])

    def test_they_agree_on_the_shared_rules_too(self):
        """Ordering and uniqueness are checked twice as well."""
        for label, entries in (
            ("duplicate labels", [("a", b"x"), ("a", b"y")]),
            ("unsorted labels", [("b", b"x"), ("a", b"y")]),
            ("empty label", [("", b"x")]),
        ):
            with self.subTest(case=label):
                self.assertEqual(
                    _collector_verdict(entries), _validator_verdict(entries)
                )


class MetadataGraphTests(unittest.TestCase):
    """OBL-PROVIDERS-036: sharing is not a cycle, and neither is expensive."""

    def _doubling(self, levels: int):
        """`x = [x, x]`, *levels* times: 2**levels nodes if sharing is ignored."""
        node = ["leaf"]
        for _ in range(levels):
            node = [node, node]
        return {"root": node}

    def test_a_shared_subtree_is_accepted(self):
        shared = {"k": [1, 2, 3]}
        self.assertIsNone(_metadata_error({"a": shared, "b": shared}))
        self.assertIsNone(
            _metadata_error({f"k{index}": shared for index in range(8)})
        )

    def test_a_genuine_cycle_is_named_as_one(self):
        cycle: dict = {}
        cycle["self"] = cycle
        self.assertEqual(_metadata_error(cycle), "metadata contains a reference cycle")

    def test_a_doubling_graph_is_refused_by_node_count_in_bounded_time(self):
        started = time.monotonic()
        message = _metadata_error(self._doubling(62))
        elapsed = time.monotonic() - started
        self.assertEqual(
            message, f"metadata exceeds the {MAX_PROVIDER_METADATA_NODES}-value limit"
        )
        self.assertLess(elapsed, 5.0, elapsed)

    def test_the_time_does_not_grow_with_the_levels(self):
        """2**20 against 2**62 nodes: the walk counts visits, not paths."""
        timings = {}
        for levels in (20, 62):
            started = time.monotonic()
            _metadata_error(self._doubling(levels))
            timings[levels] = time.monotonic() - started
        self.assertLess(timings[62], 20 * timings[20] + 1.0, timings)

    def test_a_small_doubling_graph_is_accepted(self):
        """The premise: the refusal is the size, not the shape."""
        self.assertIsNone(_metadata_error(self._doubling(10)))

    def test_the_depth_ceiling_answers_first_when_it_applies(self):
        """Both ceilings exist and the nearer one speaks."""
        message = _metadata_error(self._doubling(MAX_PROVIDER_METADATA_DEPTH))
        self.assertEqual(
            message,
            f"metadata exceeds the {MAX_PROVIDER_METADATA_DEPTH}-level nesting limit",
        )


class LabelCollisionTests(unittest.TestCase):
    """OBL-PROVIDERS-006: two paths, one label, and no silent winner."""

    def test_distinct_paths_normally_give_distinct_labels(self):
        """The premise: the label is the path relative to the component."""
        self.assertEqual(_component_relative_path("a/b", "a/b/c.py"), "c.py")
        self.assertEqual(_component_relative_path("a/b", "a/b/d/c.py"), "d/c.py")

    def test_the_label_function_is_not_injective(self):
        """A file at the component path falls back to its own basename."""
        self.assertEqual(_component_relative_path("a/b", "a/b"), "b")
        self.assertEqual(_component_relative_path("a/b", "a/b/b"), "b")

    def test_a_path_outside_the_component_is_refused(self):
        """The contrast: the function is not simply permissive."""
        with self.assertRaises(ProviderError):
            _component_relative_path("a/b", "a/c/x.py")

    def test_a_collision_is_refused_by_both_implementations(self):
        """Fail closed: neither entry is dropped and neither overwrites."""
        labels = [
            _component_relative_path("a/b", path) for path in ("a/b", "a/b/b")
        ]
        self.assertEqual(labels, ["b", "b"])
        entries = [(f"file:{labels[0]}", b"x"), (f"file:{labels[1]}", b"y")]
        self.assertEqual(
            _collector_verdict(entries), "entries must have unique labels"
        )
        self.assertEqual(
            _validator_verdict(entries), "entries must have unique labels"
        )


if __name__ == "__main__":
    unittest.main()
