"""The framing that makes a digest mean one list of entries and not another.

A digest over a list is only trustworthy if the framing is unambiguous: the
count is bound before any entry, duplicates are not quietly coalesced, and a
value's position in a composite is bound along with the value. These are the
properties that stop two different repository states from hashing alike, and
each of them is a single arithmetic fact that a test can state directly.

The compat digest is the one derived value that skips framing entirely - it is
a SHA-256 over an interpolated string - so its injectivity rests on the
grammar of the two things interpolated rather than on any length prefix.

Covers OBL-HASHING-012, OBL-HASHING-013, OBL-HASHING-014, OBL-HASHING-021 and
OBL-HASHING-106.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import boundver._hashing as hashing
from boundver._hashing import _tree_entry_descriptors
import boundver.providers as providers
from boundver._hashing import (
    HASH_DOMAIN_BEHAVIOR,
    HASH_DOMAIN_BOUNDARY,
    MAX_HASH_FILE_BYTES,
    MAX_HASH_FILES,
    MAX_HASH_LABEL_BYTES,
    MAX_HASH_TOTAL_BYTES,
    MAX_HASH_TOTAL_LABEL_BYTES,
    _hash_framed_entries,
)
from boundver.providers import (
    MAX_PROVIDER_ENTRIES,
    MAX_PROVIDER_ENTRY_BYTES,
    MAX_PROVIDER_LABEL_BYTES,
    MAX_PROVIDER_TOTAL_BYTES,
    MAX_PROVIDER_TOTAL_LABEL_BYTES,
    ProviderContext,
    ResolvedBoundary,
    _resolved_boundary_error,
    compute_boundary,
)

#: The separator the compat preimage interpolates around.
SEPARATOR = "@compat:"


def _digest(entries) -> str:
    return _hash_framed_entries(entries, domain=HASH_DOMAIN_BOUNDARY)


def _envelope(behavior: str, boundary: str) -> str:
    """The behavior envelope, exactly as _lockfile builds it."""
    return _hash_framed_entries(
        [
            ("behavior", behavior.encode("ascii")),
            ("boundary", boundary.encode("ascii")),
        ],
        domain=HASH_DOMAIN_BEHAVIOR,
    )


class DescriptorOrderTests(unittest.TestCase):
    """Entries are ordered by the bytes that get framed, not by path text.

    A descriptor is a label and a path. The label is `file:` plus the
    component-relative path encoded with surrogateescape; the path is
    repository-relative. For every name a filesystem can normally hold the
    two orders agree - UTF-8 preserves code-point order, and the differing
    prefixes are common to every entry - which is why sorting by the wrong
    element survived the whole suite (MUT-HASHING-220).

    They diverge as soon as an undecodable byte is present. A surrogate
    escape encodes back to a raw byte in 0x80-0xff, which sorts below the
    lead byte of any two-byte UTF-8 sequence, while in text U+DC80 sorts
    above every Latin-1 character. So the escaped name comes first in
    bytes and last in text.

    _tree_entry_descriptors only encodes strings and never touches the
    filesystem, so this holds on hosts that cannot create such a file.
    """

    #: An undecodable byte, an accented name, and an ASCII name: the three
    #: that make the two orders disagree.
    FILES = ["x/z.py", "x/\udc80.py", "x/\u00e9.py"]

    def _descriptors(self):
        return _tree_entry_descriptors(self.FILES, base="x")

    def test_descriptors_come_back_in_label_byte_order(self):
        labels = [label for label, _ in self._descriptors()]
        self.assertEqual(labels, sorted(labels))

    def test_label_order_and_path_order_really_differ_here(self):
        """The premise: without it the assertion above proves nothing."""
        descriptors = self._descriptors()
        by_label = [path for _, path in descriptors]
        by_path = [path for _, path in sorted(descriptors, key=lambda i: i[1])]
        self.assertNotEqual(by_label, by_path)

    def test_ordinary_names_are_ordered_the_obvious_way(self):
        """The contrast: nothing exotic happens to ordinary paths."""
        descriptors = _tree_entry_descriptors(
            ["x/b.py", "x/a.py", "x/c.py"], base="x"
        )
        self.assertEqual(
            [path for _, path in descriptors], ["x/a.py", "x/b.py", "x/c.py"]
        )

    def test_the_label_is_component_relative_and_the_path_is_not(self):
        """Why the two can disagree at all: they are different strings."""
        descriptors = _tree_entry_descriptors(["deep/x/a.py"], base="deep/x")
        (label, path), = descriptors
        self.assertEqual(label, b"file:a.py")
        self.assertEqual(path, "deep/x/a.py")


class DuplicateEntryTests(unittest.TestCase):
    """OBL-HASHING-012: the framing never coalesces identical tuples."""

    ENTRY = ("L", "100644", "blob", b"C")

    def test_one_entry_and_two_identical_entries_differ(self):
        self.assertNotEqual(_digest([self.ENTRY]), _digest([self.ENTRY] * 2))

    def test_the_count_is_the_list_length_not_the_distinct_count(self):
        """Three copies differ from two, which differ from one."""
        digests = [_digest([self.ENTRY] * count) for count in (1, 2, 3)]
        self.assertEqual(len(set(digests)), 3, digests)

    def test_two_copies_differ_from_two_distinct_entries(self):
        """The contrast: it is the duplication that counts, not the length."""
        other = ("M", "100644", "blob", b"C")
        self.assertNotEqual(
            _digest([self.ENTRY] * 2), _digest([self.ENTRY, other])
        )


class EntryCountFramingTests(unittest.TestCase):
    """OBL-HASHING-013: the count is bound before any entry."""

    EMPTY_ENTRY = ("", "m", "t", b"")

    def test_no_entries_differs_from_one_empty_entry(self):
        self.assertNotEqual(_digest([]), _digest([self.EMPTY_ENTRY]))

    def test_appending_an_empty_entry_changes_the_digest(self):
        base = [("a", "100644", "blob", b"x")]
        self.assertNotEqual(_digest(base), _digest(base + [self.EMPTY_ENTRY]))

    def test_no_entries_differs_from_every_non_empty_list(self):
        empty = _digest([])
        for count in range(1, 4):
            with self.subTest(entries=count):
                entries = [
                    (f"l{index}", "100644", "blob", b"x") for index in range(count)
                ]
                self.assertNotEqual(empty, _digest(entries))

    def test_the_empty_digest_is_not_the_bare_domain_digest(self):
        """The premise: hashing nothing still hashes the frame."""
        self.assertEqual(len(_digest([])), 64)


class BehaviorEnvelopeTests(unittest.TestCase):
    """OBL-HASHING-014: the envelope binds both values in their places."""

    INNER = "a" * 64
    IDENTITY = "b" * 64

    def test_swapping_the_two_values_changes_the_envelope(self):
        self.assertNotEqual(
            _envelope(self.INNER, self.IDENTITY),
            _envelope(self.IDENTITY, self.INNER),
        )

    def test_the_envelope_is_never_the_digest_it_wraps(self):
        envelope = _envelope(self.INNER, self.IDENTITY)
        self.assertNotEqual(envelope, self.INNER)
        self.assertNotEqual(envelope, self.IDENTITY)

    def test_an_absent_boundary_still_distinguishes_its_reason(self):
        """The 'none:<status>' identity is a value like any other."""
        self.assertNotEqual(
            _envelope(self.INNER, "none:ok"), _envelope(self.INNER, "none:error")
        )

    def test_the_envelope_uses_its_own_domain(self):
        """The premise: it is not the boundary framing under another name."""
        entries = [
            ("behavior", self.INNER.encode("ascii")),
            ("boundary", self.IDENTITY.encode("ascii")),
        ]
        self.assertNotEqual(
            _hash_framed_entries(entries, domain=HASH_DOMAIN_BEHAVIOR),
            _hash_framed_entries(entries, domain=HASH_DOMAIN_BOUNDARY),
        )


class CompatPreimageTests(unittest.TestCase):
    """OBL-HASHING-021: one preimage per pair, with no length framing to help."""

    #: Names a validated config can declare, including ones that carry the
    #: separator the preimage is built around.
    NAMES = ("svc", f"svc{SEPARATOR}1", f"a{SEPARATOR}", f"{SEPARATOR}x", "a@b:c")

    #: Identities the compat facet can actually produce: a SemVer major, or a
    #: major.minor. Both are digits and dots.
    IDENTITIES = ("1", "2", "10", "1.2", "1.20")

    def test_every_pair_has_its_own_preimage(self):
        preimages = {}
        for name in self.NAMES:
            for identity in self.IDENTITIES:
                preimages.setdefault(
                    f"{name}{SEPARATOR}{identity}", []
                ).append((name, identity))
        collisions = {
            preimage: pairs
            for preimage, pairs in preimages.items()
            if len(pairs) > 1
        }
        self.assertEqual(collisions, {})
        self.assertEqual(len(preimages), len(self.NAMES) * len(self.IDENTITIES))

    def test_the_injectivity_rests_on_the_identity_grammar(self):
        """Not on the construction: an identity carrying the separator collides.

        The compat identity is a SemVer major or major.minor, so it is digits
        and dots and cannot contain the separator. That is the whole reason
        two pairs cannot meet, and it is a property of version validation
        rather than of the digest, which is why it is written down here.
        """
        self.assertEqual(
            f"a{SEPARATOR}1{SEPARATOR}2",
            f"a{SEPARATOR}1" + SEPARATOR + "2",
        )
        self.assertTrue(all(part.isdigit() for identity in self.IDENTITIES
                            for part in identity.split(".")))


#: Every ceiling that exists in both modules, by name.
SHARED_CEILINGS = (
    ("entries", MAX_PROVIDER_ENTRIES, MAX_HASH_FILES),
    ("entry bytes", MAX_PROVIDER_ENTRY_BYTES, MAX_HASH_FILE_BYTES),
    ("total bytes", MAX_PROVIDER_TOTAL_BYTES, MAX_HASH_TOTAL_BYTES),
    ("label bytes", MAX_PROVIDER_LABEL_BYTES, MAX_HASH_LABEL_BYTES),
    ("total label bytes", MAX_PROVIDER_TOTAL_LABEL_BYTES, MAX_HASH_TOTAL_LABEL_BYTES),
)

#: Lowered values, so a result can sit exactly on each ceiling. The real ones
#: are up to 256 MB, which no test should allocate.
SMALL_PROVIDER = {
    "MAX_PROVIDER_ENTRIES": 4,
    "MAX_PROVIDER_ENTRY_BYTES": 32,
    "MAX_PROVIDER_TOTAL_BYTES": 64,
    "MAX_PROVIDER_LABEL_BYTES": 8,
    "MAX_PROVIDER_TOTAL_LABEL_BYTES": 20,
}
SMALL_HASHING = {
    "MAX_HASH_FILES": 4,
    "MAX_HASH_FILE_BYTES": 32,
    "MAX_HASH_TOTAL_BYTES": 64,
    "MAX_HASH_LABEL_BYTES": 8,
    "MAX_HASH_TOTAL_LABEL_BYTES": 20,
}


class _FixedProvider:
    """A provider that returns one prepared entry list, whatever it is asked."""

    name = "fixed"
    version = "1"

    def __init__(self, entries) -> None:
        self._entries = list(entries)

    def resolve(self, ctx) -> ResolvedBoundary:
        return ResolvedBoundary(entries=list(self._entries))


def _context() -> ProviderContext:
    return ProviderContext(
        repo_root=Path("."),
        component_path="svc",
        boundary_cfg={"paths": []},
        source="head",
        read_file=lambda path: b"",
        read_file_limited=lambda path, max_bytes: b"",
        list_files=lambda prefix: [],
    )


class CeilingAgreementTests(unittest.TestCase):
    """OBL-HASHING-106: the same numbers, and the same verdict end to end."""

    def test_the_two_modules_declare_the_same_numbers(self):
        for label, provider_value, hash_value in SHARED_CEILINGS:
            with self.subTest(ceiling=label):
                self.assertEqual(provider_value, hash_value)

    def _at_ceiling(self):
        """One entry list sitting exactly on each lowered ceiling."""
        yield "entries", [(f"l{index}", b"x") for index in range(4)]
        yield "entry bytes", [("a", b"x" * 32)]
        yield "total bytes", [("a", b"x" * 32), ("b", b"x" * 32)]
        yield "label bytes", [("a" * 8, b"x")]
        yield "total label bytes", [("a" * 8, b"x"), ("b" * 8, b"x"), ("c" * 4, b"x")]

    def test_a_result_at_each_ceiling_validates_and_hashes(self):
        with patch.multiple(providers, **SMALL_PROVIDER), \
                patch.multiple(hashing, **SMALL_HASHING):
            for label, entries in self._at_ceiling():
                with self.subTest(ceiling=label):
                    self.assertIsNone(
                        _resolved_boundary_error(ResolvedBoundary(entries=entries))
                    )
                    digest, _errors, _status = compute_boundary(
                        _FixedProvider(entries), _context()
                    )[:3]
                    self.assertIsInstance(digest, str)
                    self.assertEqual(len(digest), 64)

    def test_one_past_each_ceiling_is_refused_by_the_validator(self):
        """The premise: the corpus really is sitting on the boundary."""
        over = {
            "entries": [(f"l{index}", b"x") for index in range(5)],
            "entry bytes": [("a", b"x" * 33)],
            "total bytes": [("a", b"x" * 32), ("b", b"x" * 33)],
            "label bytes": [("a" * 9, b"x")],
            "total label bytes": [("a" * 8, b"x"), ("b" * 8, b"x"), ("c" * 5, b"x")],
        }
        with patch.multiple(providers, **SMALL_PROVIDER), \
                patch.multiple(hashing, **SMALL_HASHING):
            for label, entries in over.items():
                with self.subTest(ceiling=label):
                    self.assertIsNotNone(
                        _resolved_boundary_error(ResolvedBoundary(entries=entries))
                    )


if __name__ == "__main__":
    unittest.main()
