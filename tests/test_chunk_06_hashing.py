"""What the v3 framing binds, and whether the facet gate agrees with itself.

Six obligations sit in two halves that turn out to share one shape: a rule
stated once in prose and then implemented twice. The hashing half is the wire
format. `spec/HASHING.md` prints the byte layout of a v3 digest, and
`_hash_framed_entries` builds it. The suite pins one single-entry digest as a
hex literal, which does constrain the format, but a literal cannot say which
field's length prefix moved, and every other framing test asserts only that two
digests differ. So this file rebuilds the layout from the document - magic,
domain, entry count, then a length-prefixed label, mode, object type and
content per entry - and uses that reconstruction as the oracle for eight entry
shapes, including the two-tuple form whose mode and type are read off a
`_ModeAwareBytes` and the plain-bytes form that falls back to semantic/value.
Injectivity then stops being an article of faith: one fixed byte string is
re-split into (label, mode, object_type, content) at every legal position, and
all fifty-six splits must hash apart. That corpus reaches the mode/type
boundary the old tests never touched, and the premise beside it shows the
search working, because the same corpus run through a framing that drops the
two inner length prefixes collides thirty-five times.

The ordering obligation needed a fixture the existing test could not supply.
`test_input_order_does_not_change_digest` reverses a two-element list whose
entries differ in their label, so only the first element of the sort key is
ever compared and a key truncated to `item[0]` would pass it. The multiset here
ties five entries on one label and splits them on mode, then object type, then
content, which is the whole key; all one hundred and twenty permutations must
produce one digest. The same multiset run through a label-only sort key
produces one hundred and twenty distinct digests, which is what makes the
invariance claim worth stating.

The duplicate rule was recorded as untested and is not. `test_hash_wire_format`
already hashes a repeated tuple and finds the digests differ, so what is added
here is the half a difference cannot show: that two copies of one entry frame
an entry count of two, checked against the header the oracle builds, and that
the framing deliberately keeps what the provider contract refuses, since the
tree path reaches the framing with no provider validation in front of it.

The providers half asks whether three implementations of "which facets does
this component gate" agree. `_config.py` refuses an explicit policy that
selects an impossible facet; `_utils.py::_available_component_facets` decides
the same question at verify time; and generation decides it a third way, by
writing a digest into a facet slot or leaving it null. The static gate and the
availability function are compared across all fifteen non-empty facet
selections on each of one hundred and fifty-six declaration shapes, and the
lockfile the third one writes is compared against the second for every
registered provider at once: the digest a facet slot holds must be non-null
exactly when the rule promises that facet.

How a provider added tomorrow is placed took a measurement to settle, and the
answer is not the one the first draft of this file claimed. Registering a new
provider and re-running proved that the partition over `{"leaf", "implicit"}`
places nothing: both `_available_component_facets` and the expectation beside it
test the same two names, so a new provider that publishes nothing agrees with
itself. What actually stops it is generation. `providers.compute_boundary`
refuses a resolve that published no entries unless the provider is literally
`LeafProvider` or `ImplicitProvider` - by type, not by name - and every one of
the three shapes such a provider can take (`ok` with no entries, `partial` with
no errors, `partial` with errors) raised a `ConfigError` out of lockfile
generation rather than quietly recording a null digest. That is the right
refusal in the wrong place: it arrived from a class fixture, which reads as
broken infrastructure. So `setUpClass` catches it and `setUp` reports it as a
named failure that says the provider's name and what it did. The recorded-facet
differential stays, not as the thing that places a new provider, but as a second
witness spanning all seventeen components and four facets at once: dropping the
non-empty check from the behavior clause of `_available_component_facets` makes
it name `empty-behavior` and both facet sets.

Absence claims here are premised by making the gate fire first. A leaf
repository whose only source file drifts makes `verify --source head --facets
exact` exit 1 with a pinned MISMATCH line, so the clean exit zero beside it
means the exact gate ran and found nothing rather than having been dropped from
the selection; the same drift under `--facets behavior` prints that MISMATCH
under NON-GATING DRIFT instead, which is the other half of the claim. Every
positive row of the availability table is driven twice on the provider
repository: once against a current lock, where selecting the facet must report
nothing, and once against a lock the input has drifted past, where selecting the
same facet must report a mismatch naming that component and that facet.

The slice sweep found the two slice implementations agreeing across all one
hundred and ninety-two combinations of real components, and disagreeing on
exactly the case the register predicted: a slice naming a component that is not
in `config.components`. `_facet_policy` looks that member up in a dict built
only from configured components and contributes nothing; verify hands the same
missing member to `_available_component_facets({})`, which returns `{"exact"}`.
The payload then says the slice is ungated while verify fails on it. The
divergence is pinned rather than fixed, and `validate_config` refuses such a
config today, so no CLI path reaches it.

Covers OBL-HASHING-037, OBL-HASHING-038, OBL-HASHING-091, OBL-PROVIDERS-014,
OBL-PROVIDERS-019 and OBL-PROVIDERS-029.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import struct
import unittest
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver._config import validate_config
from boundver._facet_policy import facet_policy_payload
from boundver._git import _capture_git_source_snapshot
from boundver._hashing import (
    HASH_DOMAIN_BOUNDARY,
    HASH_DOMAIN_EXACT,
    HASH_FRAME_VERSION,
    _hash_framed_entries,
    _ModeAwareBytes,
)
from boundver._lockfile import generate_lockfile, verify_lockfile
from boundver._utils import FACET_SET, FACETS, _available_component_facets
from boundver.providers import (
    ProviderError,
    ResolvedBoundary,
    _ProviderEntryCollector,
    _resolved_boundary_error,
    create_registry,
)

from tests._parity import run_cli
from tests._scenarios import Scenario

PROFILE = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)

#: The magic spec/HASHING.md names, rebuilt here rather than imported, so the
#: oracle below is a reading of the document and not a copy of the module.
SPEC_MAGIC = b"boundver-hash/v3"

#: An alphabet dense in the bytes a framing bug would confuse: a NUL that a
#: delimiter-based encoding would treat as a separator, a slash and a dot from
#: real paths, and digits from real Git modes.
SPLIT_ALPHABET = "ab01/.\x00"


def _u64(value: int) -> bytes:
    return struct.pack(">Q", value)


def _spec_preimage(entries: Sequence[tuple], domain: str) -> bytes:
    """Rebuild the byte string spec/HASHING.md prints, field by field.

    This is the independent oracle. It repeats the document rather than the
    module: sort by encoded label, mode, type then content, bind the magic, the
    domain and the entry count, then length-prefix every one of the four fields
    of every entry.
    """
    prepared: List[Tuple[bytes, bytes, bytes, bytes]] = []
    for entry in entries:
        if len(entry) == 2:
            label, content = entry
            mode = getattr(content, "git_mode", "semantic")
            object_type = getattr(content, "git_object_type", "value")
        else:
            label, mode, object_type, content = entry
        prepared.append(
            (
                label.encode("utf-8", errors="surrogateescape"),
                mode.encode("ascii"),
                object_type.encode("ascii"),
                bytes(content),
            )
        )
    prepared.sort()
    stream = bytearray()
    stream += _u64(len(SPEC_MAGIC)) + SPEC_MAGIC
    domain_bytes = domain.encode("utf-8")
    stream += _u64(len(domain_bytes)) + domain_bytes
    stream += _u64(len(prepared))
    for label_bytes, mode_bytes, type_bytes, content in prepared:
        stream += _u64(len(label_bytes)) + label_bytes
        stream += _u64(len(mode_bytes)) + mode_bytes
        stream += _u64(len(type_bytes)) + type_bytes
        stream += _u64(len(content)) + content
    return bytes(stream)


def _spec_digest(entries: Sequence[tuple], domain: str) -> str:
    return hashlib.sha256(_spec_preimage(entries, domain)).hexdigest()


def _unframed_inner_digest(entries: Sequence[tuple], domain: str) -> str:
    """The mutant: mode and object type emitted with no length of their own.

    Used only as a premise. A framing that concatenates the two inner fields is
    exactly the regression the obligation names, and running the same corpus
    through it shows that the collision search can find one.
    """
    prepared = sorted(
        (
            label.encode("utf-8", errors="surrogateescape"),
            mode.encode("ascii"),
            object_type.encode("ascii"),
            bytes(content),
        )
        for label, mode, object_type, content in entries
    )
    stream = bytearray()
    stream += _u64(len(SPEC_MAGIC)) + SPEC_MAGIC
    domain_bytes = domain.encode("utf-8")
    stream += _u64(len(domain_bytes)) + domain_bytes
    stream += _u64(len(prepared))
    for label_bytes, mode_bytes, type_bytes, content in prepared:
        stream += _u64(len(label_bytes)) + label_bytes
        stream += mode_bytes
        stream += type_bytes
        stream += _u64(len(content)) + content
    return hashlib.sha256(bytes(stream)).hexdigest()


def _label_only_sort_digest(entries: Sequence[tuple], domain: str) -> str:
    """The mutant: `prepared.sort(key=lambda item: item[0])`.

    Python's sort is stable, so under this key entries that tie on their label
    keep the order they arrived in and the digest follows provider iteration
    order. Used only as a premise for the permutation invariance claim.
    """
    prepared = [
        (
            label.encode("utf-8", errors="surrogateescape"),
            mode.encode("ascii"),
            object_type.encode("ascii"),
            bytes(content),
        )
        for label, mode, object_type, content in entries
    ]
    prepared.sort(key=lambda item: item[0])
    stream = bytearray()
    stream += _u64(len(SPEC_MAGIC)) + SPEC_MAGIC
    domain_bytes = domain.encode("utf-8")
    stream += _u64(len(domain_bytes)) + domain_bytes
    stream += _u64(len(prepared))
    for label_bytes, mode_bytes, type_bytes, content in prepared:
        stream += _u64(len(label_bytes)) + label_bytes
        stream += _u64(len(mode_bytes)) + mode_bytes
        stream += _u64(len(type_bytes)) + type_bytes
        stream += _u64(len(content)) + content
    return hashlib.sha256(bytes(stream)).hexdigest()


def _four_field_splits(source: bytes) -> List[tuple]:
    """Every way to cut *source* into label, mode, object type and content.

    Mode and object type must be non-empty, which is what the module enforces;
    a label and a content may be empty. Every cut concatenates back to the same
    bytes, so any two of them that hashed alike would be a framing collision.
    """
    splits = []
    for label_end in range(0, len(source) - 1):
        for mode_end in range(label_end + 1, len(source)):
            for type_end in range(mode_end + 1, len(source) + 1):
                splits.append(
                    (
                        source[:label_end].decode("ascii"),
                        source[label_end:mode_end].decode("ascii"),
                        source[mode_end:type_end].decode("ascii"),
                        source[type_end:],
                    )
                )
    return splits


class FramedEncodingOracleTests(unittest.TestCase):
    """OBL-HASHING-037: the digest is the one the specification describes."""

    #: Entry lists whose digest the oracle must reproduce. Each row exercises a
    #: different way the module fills the four fields: the explicit 4-tuple, the
    #: 2-tuple whose mode and type come from a `_ModeAwareBytes`, and the
    #: 2-tuple of plain bytes that falls back to semantic/value.
    SHAPES = {
        "empty list": [],
        "one explicit entry": [("a", "100644", "blob", b"one")],
        "unsorted explicit entries": [
            ("b", "100755", "blob", b"two"),
            ("a", "100644", "blob", b"one"),
            ("a", "120000", "blob", b"link"),
        ],
        "plain bytes fall back to semantic/value": [("a", b"bc"), ("z", b"q")],
        "mode-aware bytes carry their own mode": [
            ("a", _ModeAwareBytes(b"one", "100755", "blob")),
            ("b", _ModeAwareBytes(b"two", "120000", "blob")),
        ],
        "a gitlink entry": [("sub", "160000", "commit", b"")],
        "labels needing utf-8": [("é", "100644", "blob", b"\xff\x00\xfe")],
        "duplicate tuples": [("a", "100644", "blob", b"x")] * 3,
    }

    def test_the_magic_the_oracle_rebuilds_is_the_one_the_module_publishes(self):
        """The premise: the oracle is reading the current contract version."""
        self.assertEqual(HASH_FRAME_VERSION, "boundver-hash/v3")
        self.assertEqual(HASH_FRAME_VERSION.encode("ascii"), SPEC_MAGIC)

    def test_every_shape_hashes_to_the_wire_format_the_spec_prints(self):
        for label, entries in self.SHAPES.items():
            for domain in (HASH_DOMAIN_BOUNDARY, HASH_DOMAIN_EXACT):
                with self.subTest(shape=label, domain=domain):
                    self.assertEqual(
                        _hash_framed_entries(entries, domain=domain),
                        _spec_digest(entries, domain),
                    )

    def test_the_oracle_is_not_a_constant_it_moves_with_its_input(self):
        """The premise for the agreement above: distinct inputs, distinct oracle."""
        digests = {
            label: _spec_digest(entries, HASH_DOMAIN_BOUNDARY)
            for label, entries in self.SHAPES.items()
        }
        self.assertEqual(len(set(digests.values())), len(digests), digests)

    def test_the_oracle_notices_a_dropped_inner_length_prefix(self):
        """The premise that makes the agreement test load-bearing.

        If the oracle happened to be insensitive to the two inner prefixes it
        would agree with a broken module. It is not.
        """
        entries = [("x", "100644", "blob", b"")]
        self.assertNotEqual(
            _spec_digest(entries, HASH_DOMAIN_BOUNDARY),
            _unframed_inner_digest(entries, HASH_DOMAIN_BOUNDARY),
        )


class FieldBoundaryInjectivityTests(unittest.TestCase):
    """OBL-HASHING-037: moving a field boundary must move the digest."""

    #: The two pairs the obligation names by hand. Each pair concatenates to
    #: the same bytes and differs only in where one boundary falls: the first
    #: between label and content, the second between mode and object type.
    #: Each row is (left, right, moved) where *moved* names the fields whose
    #: boundary shifted: their bytes, concatenated across every entry in field
    #: order, are identical on both sides. 0 is the label, 1 the mode, 2 the
    #: object type and 3 the content.
    NAMED_PAIRS = {
        "label against content": ([("a", b"bc")], [("ab", b"c")], (0, 3)),
        "mode against object type": (
            [("x", "100644", "blob", b"")],
            [("x", "100", "644blob", b"")],
            (0, 1, 2, 3),
        ),
        "entry against entry": (
            [("a", "100644", "blob", b""), ("b", "100644", "blob", b"")],
            [("ab", "100644", "blob", b"")],
            (0,),
        ),
        "content against the next label": (
            [("a", "100644", "blob", b"xy"), ("b", "100644", "blob", b"")],
            [("a", "100644", "blob", b"x"), ("yb", "100644", "blob", b"")],
            (0, 3),
        ),
    }

    @staticmethod
    def _moved_bytes(entries, moved) -> bytes:
        """The bytes of the shifted fields, concatenated in field order."""
        joined = bytearray()
        for entry in entries:
            if len(entry) == 2:
                label, content = entry
                fields = (label, "semantic", "value", content)
            else:
                fields = entry
            for index in moved:
                value = fields[index]
                joined += (
                    value.encode("utf-8")
                    if isinstance(value, str)
                    else bytes(value)
                )
        return bytes(joined)

    def test_each_named_pair_hashes_apart(self):
        for label, (left, right, _moved) in self.NAMED_PAIRS.items():
            with self.subTest(pair=label):
                self.assertNotEqual(
                    _hash_framed_entries(left, domain=HASH_DOMAIN_BOUNDARY),
                    _hash_framed_entries(right, domain=HASH_DOMAIN_BOUNDARY),
                )

    def test_each_named_pair_really_shares_its_concatenated_bytes(self):
        """The premise: a pair that differed in its bytes would prove nothing."""
        for label, (left, right, moved) in self.NAMED_PAIRS.items():
            with self.subTest(pair=label):
                self.assertEqual(
                    self._moved_bytes(left, moved),
                    self._moved_bytes(right, moved),
                )

    def test_the_fields_that_did_not_move_are_identical_across_each_pair(self):
        """The other half of the premise: only the named boundary shifted."""
        held = {
            "label against content": ((1, 2), ("semantic", "value")),
            "mode against object type": ((0, 3), ("x", b"")),
            "entry against entry": ((1, 2, 3), ("100644", "blob", b"")),
            "content against the next label": ((1, 2), ("100644", "blob")),
        }
        for label, (left, right, _moved) in self.NAMED_PAIRS.items():
            indices, expected = held[label]
            with self.subTest(pair=label):
                for side in (left, right):
                    for entry in side:
                        fields = (
                            (entry[0], "semantic", "value", entry[1])
                            if len(entry) == 2
                            else entry
                        )
                        self.assertEqual(
                            tuple(fields[index] for index in indices), expected
                        )

    def test_no_two_field_splits_of_one_byte_string_collide(self):
        """The fixed corpus, stated as an example before the property runs."""
        source = b"abcdefg"
        splits = _four_field_splits(source)
        self.assertEqual(len(splits), 56)
        digests = {
            _hash_framed_entries([split], domain=HASH_DOMAIN_BOUNDARY)
            for split in splits
        }
        self.assertEqual(len(digests), len(splits))

    def test_a_framing_without_inner_lengths_collides_on_that_same_corpus(self):
        """The premise: the search finds a collision when one is there."""
        splits = _four_field_splits(b"abcdefg")
        digests = {
            _unframed_inner_digest([split], HASH_DOMAIN_BOUNDARY)
            for split in splits
        }
        self.assertLess(len(digests), len(splits))

    @PROFILE
    @given(
        source=st.text(alphabet=SPLIT_ALPHABET, min_size=3, max_size=8),
        domain=st.sampled_from((HASH_DOMAIN_BOUNDARY, HASH_DOMAIN_EXACT)),
    )
    def test_every_re_split_of_any_byte_string_gets_its_own_digest(
        self, source: str, domain: str
    ):
        splits = _four_field_splits(source.encode("ascii"))
        seen: Dict[str, tuple] = {}
        for split in splits:
            digest = _hash_framed_entries([split], domain=domain)
            self.assertNotIn(
                digest,
                seen,
                f"{split!r} and {seen.get(digest)!r} share a digest",
            )
            seen[digest] = split


class SortKeyPermutationTests(unittest.TestCase):
    """OBL-HASHING-038: the digest is over a multiset, not over an order."""

    #: Five entries sharing one label. They split on mode, then on object type,
    #: then on content, so the sort key is compared to its last element. A key
    #: truncated at any point before that leaves at least two of these tied and
    #: ordered by arrival.
    TIED = (
        ("a", "100644", "blob", b"x"),
        ("a", "100755", "blob", b"x"),
        ("a", "120000", "blob", b"x"),
        ("a", "100644", "blob", b"y"),
        ("a", "160000", "commit", b"x"),
    )

    def test_the_fixture_ties_on_every_prefix_of_the_sort_key(self):
        """The premise: without ties this file would test the label alone."""
        self.assertEqual(len({entry[0] for entry in self.TIED}), 1)
        self.assertLess(
            len({entry[:2] for entry in self.TIED}), len(self.TIED)
        )
        self.assertLess(
            len({entry[:3] for entry in self.TIED}), len(self.TIED)
        )
        self.assertEqual(len(set(self.TIED)), len(self.TIED))

    def test_every_permutation_of_the_tied_multiset_hashes_alike(self):
        digests = {
            _hash_framed_entries(list(order), domain=HASH_DOMAIN_BOUNDARY)
            for order in itertools.permutations(self.TIED)
        }
        self.assertEqual(len(digests), 1, sorted(digests))

    def test_a_label_only_sort_key_makes_those_permutations_disagree(self):
        """The premise: the sweep above would have seen a truncated key."""
        digests = {
            _label_only_sort_digest(list(order), HASH_DOMAIN_BOUNDARY)
            for order in itertools.permutations(self.TIED)
        }
        self.assertEqual(len(digests), 120)

    @PROFILE
    @given(
        pair=st.lists(
            st.tuples(
                st.sampled_from(("a", "b")),
                st.sampled_from(("100644", "100755", "120000", "160000")),
                st.sampled_from(("blob", "commit")),
                st.sampled_from((b"", b"x", b"y")),
            ),
            min_size=2,
            max_size=6,
        ).flatmap(
            lambda entries: st.tuples(st.just(entries), st.permutations(entries))
        ),
        domain=st.sampled_from((HASH_DOMAIN_BOUNDARY, HASH_DOMAIN_EXACT)),
    )
    def test_no_permutation_of_any_tie_dense_multiset_changes_the_digest(
        self, pair, domain: str
    ):
        entries, permuted = pair
        self.assertCountEqual(entries, permuted)
        self.assertEqual(
            _hash_framed_entries(list(entries), domain=domain),
            _hash_framed_entries(list(permuted), domain=domain),
        )

    @PROFILE
    @given(
        entries=st.lists(
            st.tuples(
                st.sampled_from(("a", "b")),
                st.sampled_from(("100644", "100755", "120000")),
                st.sampled_from(("blob", "commit")),
                st.sampled_from((b"", b"x", b"y")),
            ),
            min_size=1,
            max_size=6,
        )
    )
    def test_the_module_sorts_the_way_the_specification_says_it_does(
        self, entries
    ):
        """Ordering stated positively: the oracle sorts on the whole key."""
        self.assertEqual(
            _hash_framed_entries(list(entries), domain=HASH_DOMAIN_BOUNDARY),
            _spec_digest(list(entries), HASH_DOMAIN_BOUNDARY),
        )


class DuplicateEntryContractTests(unittest.TestCase):
    """OBL-HASHING-091: framing keeps duplicates, providers refuse them."""

    ENTRY = ("a", b"x")

    def test_one_entry_and_two_identical_entries_differ(self):
        self.assertNotEqual(
            _hash_framed_entries([self.ENTRY], domain=HASH_DOMAIN_BOUNDARY),
            _hash_framed_entries([self.ENTRY] * 2, domain=HASH_DOMAIN_BOUNDARY),
        )

    def test_two_identical_entries_match_a_hand_built_entry_count_of_two(self):
        """The count the second digest binds is 2, not the distinct count 1."""
        self.assertEqual(
            _hash_framed_entries([self.ENTRY] * 2, domain=HASH_DOMAIN_BOUNDARY),
            _spec_digest([self.ENTRY] * 2, HASH_DOMAIN_BOUNDARY),
        )
        preimage = _spec_preimage([self.ENTRY] * 2, HASH_DOMAIN_BOUNDARY)
        header = (
            _u64(len(SPEC_MAGIC))
            + SPEC_MAGIC
            + _u64(len(HASH_DOMAIN_BOUNDARY))
            + HASH_DOMAIN_BOUNDARY.encode("utf-8")
            + _u64(2)
        )
        self.assertTrue(preimage.startswith(header), preimage[:80])

    def test_the_count_climbs_with_every_repeat(self):
        digests = {
            count: _hash_framed_entries(
                [self.ENTRY] * count, domain=HASH_DOMAIN_BOUNDARY
            )
            for count in range(1, 5)
        }
        self.assertEqual(len(set(digests.values())), 4, digests)
        for count, digest in digests.items():
            with self.subTest(copies=count):
                self.assertEqual(
                    digest,
                    _spec_digest([self.ENTRY] * count, HASH_DOMAIN_BOUNDARY),
                )

    def test_a_provider_result_repeating_a_label_is_refused_before_hashing(self):
        self.assertEqual(
            _resolved_boundary_error(
                ResolvedBoundary(entries=[("a", b"x"), ("a", b"x")])
            ),
            "entries must have unique labels",
        )

    def test_a_provider_result_out_of_label_order_is_refused_before_hashing(self):
        self.assertEqual(
            _resolved_boundary_error(
                ResolvedBoundary(entries=[("b", b"x"), ("a", b"x")])
            ),
            "entries must be in deterministic sorted order",
        )

    def test_a_strictly_increasing_result_is_accepted(self):
        """The premise: the validator says nothing when the rule is kept."""
        self.assertIsNone(
            _resolved_boundary_error(
                ResolvedBoundary(entries=[("a", b"x"), ("b", b"x")])
            )
        )

    def test_the_incremental_collector_refuses_the_same_two_results(self):
        for label, entries, message in (
            ("duplicate", [("a", b"x"), ("a", b"x")], "entries must have unique labels"),
            (
                "descending",
                [("b", b"x"), ("a", b"x")],
                "entries must be in deterministic sorted order",
            ),
        ):
            with self.subTest(case=label):
                collector = _ProviderEntryCollector()
                with self.assertRaises(ProviderError) as raised:
                    for entry_label, content in entries:
                        collector.add(entry_label, content)
                self.assertEqual(str(raised.exception), message)

    def test_the_framing_keeps_exactly_what_the_provider_contract_forbids(self):
        """The two rules are meant to differ; this pins the contradiction.

        The framing layer is reached by the tree path with no provider
        validation in front of it, so its duplicate handling is a contract of
        its own rather than dead code behind a stricter check.
        """
        repeated = [("a", b"x"), ("a", b"x")]
        self.assertEqual(
            _resolved_boundary_error(ResolvedBoundary(entries=repeated)),
            "entries must have unique labels",
        )
        self.assertNotEqual(
            _hash_framed_entries(repeated, domain=HASH_DOMAIN_BOUNDARY),
            _hash_framed_entries([("a", b"x")], domain=HASH_DOMAIN_BOUNDARY),
        )


def _declaration(
    provider: str,
    boundary_paths: Sequence[str],
    behavior_paths: Optional[Sequence[str]],
    version_source: Optional[dict],
    path: str = "svc",
) -> dict:
    """One component declaration, built from the four inputs the table ranges over."""
    entry: dict = {
        "path": path,
        "boundary": {"provider": provider, "paths": list(boundary_paths)},
    }
    if behavior_paths is not None:
        entry["behavior"] = {"paths": list(behavior_paths)}
    if version_source is not None:
        entry["version_source"] = dict(version_source)
    return entry


def _expected_availability(
    provider: str,
    boundary_paths: Sequence[str],
    behavior_paths: Optional[Sequence[str]],
    version_source: Optional[dict],
) -> Set[str]:
    """The reference table from docs/reference.md, written out as a rule.

    Deliberately four-facet. `FACETS` is imported from `_utils` rather than
    re-spelled here, so a fifth facet would reach every sweep in this file and
    disagree with this function, which is the signal that the reference table
    has grown a row nobody wrote down.
    """
    available = {"exact"}
    if provider != "leaf" and not (provider == "implicit" and not boundary_paths):
        available.add("boundary")
    if behavior_paths:
        available.add("behavior")
    if version_source is not None:
        available.add("compat")
    return available


#: The version source used wherever a declaration needs compat to be available.
VERSION_SOURCE = {"file": "version.json", "field": "version"}

#: The behavior path lists that straddle the availability boundary: no
#: `behavior` key at all, the key with an empty list, and one non-empty entry.
BEHAVIOR_SHAPES = (None, (), ("api/v1.json",))

#: Every non-empty facet selection a policy can spell - fifteen of them, since
#: `FACETS` has four members. The differential runs all fifteen against every
#: declaration shape, so most rows compare a non-empty gate set against a
#: non-empty expectation rather than two empty sets.
SELECTIONS = tuple(
    tuple(subset)
    for size in range(1, len(FACETS) + 1)
    for subset in itertools.combinations(FACETS, size)
)


def _declaration_shapes() -> Iterator[Tuple[tuple, dict, Set[str]]]:
    """Every declaration the availability table ranges over.

    The registry is enumerated rather than listed, so the shape count follows
    the number of registered providers: thirteen names times two boundary path
    lists times three behavior shapes times two version sources.
    """
    for provider in sorted(create_registry()):
        for boundary_paths in ((), ("api/v1.json",)):
            for behavior_paths in BEHAVIOR_SHAPES:
                for version_source in (None, dict(VERSION_SOURCE)):
                    yield (
                        (provider, boundary_paths, behavior_paths,
                         version_source is not None),
                        _declaration(
                            provider,
                            boundary_paths,
                            behavior_paths,
                            version_source,
                        ),
                        _expected_availability(
                            provider,
                            boundary_paths,
                            behavior_paths,
                            version_source,
                        ),
                    )


class AvailabilityTableTests(unittest.TestCase):
    """OBL-PROVIDERS-019: the availability table, over the live registry."""

    def test_the_facet_vocabulary_this_file_ranges_over_is_the_modules_own(self):
        """The premise for every sweep below: the vocabulary is not re-spelled.

        `_utils.FACETS` is the canonical tuple, and `_config.py` gates on the
        frozenset built from it. Importing it means a fifth facet reaches the
        differential; pinning it here means the fifth facet is announced by
        name rather than quietly widening a sweep whose oracle still describes
        four.
        """
        self.assertEqual(FACETS, ("exact", "behavior", "boundary", "compat"))
        self.assertEqual(FACET_SET, frozenset(FACETS))

    def test_boundary_availability_follows_the_rule_for_every_registered_provider(self):
        registry = sorted(create_registry())
        self.assertIn("leaf", registry)
        self.assertIn("implicit", registry)
        self.assertGreater(len(registry), 2, registry)
        for provider in registry:
            for boundary_paths in ((), ("api/v1.json",)):
                with self.subTest(provider=provider, paths=bool(boundary_paths)):
                    declaration = _declaration(provider, boundary_paths, None, None)
                    self.assertEqual(
                        _available_component_facets(declaration),
                        _expected_availability(provider, boundary_paths, None, None),
                    )

    def test_leaf_and_pathless_implicit_are_the_only_providers_without_boundary(self):
        """The two names `_utils` special-cases, pinned as a partition.

        A pin on a name set, not a rule that places a provider added tomorrow.
        `_available_component_facets` tests membership of `{"leaf",
        "implicit"}` and so does the expectation beside it, so a new provider
        that publishes nothing agrees with itself here; registering one and
        re-running left this test green. Worth keeping anyway, because
        collapsing that membership test to `{"leaf"}` is a real single-line
        fault and this is where it reads most directly. The refusal a new
        provider actually meets is
        `UnavailableFacetSelectionTests::test_every_registered_provider_produces_a_lockable_component`.
        """
        without = {
            (provider, bool(paths))
            for provider in create_registry()
            for paths in ((), ("api/v1.json",))
            if "boundary"
            not in _available_component_facets(_declaration(provider, paths, None, None))
        }
        self.assertEqual(
            without, {("leaf", False), ("leaf", True), ("implicit", False)}
        )

    def test_behavior_becomes_available_exactly_at_a_non_empty_path_list(self):
        observed = {
            shape: "behavior"
            in _available_component_facets(
                _declaration("path-hash", ("api/v1.json",), shape, None)
            )
            for shape in BEHAVIOR_SHAPES
        }
        self.assertEqual(observed, {None: False, (): False, ("api/v1.json",): True})

    def test_compat_is_available_exactly_when_version_source_is_an_object(self):
        for label, value, expected in (
            ("absent", None, False),
            ("object", dict(VERSION_SOURCE), True),
        ):
            with self.subTest(version_source=label):
                declaration = _declaration("path-hash", ("api/v1.json",), None, value)
                self.assertEqual(
                    "compat" in _available_component_facets(declaration), expected
                )
        for label, value in (("list", []), ("string", "1.0.0"), ("null", None)):
            with self.subTest(non_object=label):
                declaration = _declaration("path-hash", ("api/v1.json",), None, None)
                declaration["version_source"] = value
                self.assertNotIn("compat", _available_component_facets(declaration))

    def test_the_availability_function_withholds_three_of_the_four_facets(self):
        """The premise: 'exact is unconditional' needs a function that says no.

        Over the whole shape table exactly behavior, boundary and compat are
        ever withheld, which is what makes the next test a statement rather
        than a restatement of `available = {"exact"}`.
        """
        self.assertEqual(
            _available_component_facets(_declaration("leaf", (), None, None)),
            {"exact"},
        )
        self.assertEqual(
            _available_component_facets(
                _declaration(
                    "path-hash",
                    ("api/v1.json",),
                    ("api/v1.json",),
                    dict(VERSION_SOURCE),
                )
            ),
            set(FACETS),
        )
        withheld = {
            facet
            for _key, declaration, _expected in _declaration_shapes()
            for facet in FACETS
            if facet not in _available_component_facets(declaration)
        }
        self.assertEqual(withheld, {"behavior", "boundary", "compat"})

    def test_exact_is_available_for_every_declaration_shape_including_an_empty_one(self):
        self.assertEqual(_available_component_facets({}), {"exact"})
        missing = [
            key
            for key, declaration, _expected in _declaration_shapes()
            if "exact" not in _available_component_facets(declaration)
        ]
        self.assertEqual(missing, [], missing[:8])


#: The OpenAPI document every component in the provider repository declares,
#: and the semantically different one the drift fixture replaces it with. The
#: canonical providers ignore formatting, so the drift adds a path and changes
#: the declared version rather than reindenting.
DOCUMENT = {"openapi": "3.1.0", "info": {"title": "t", "version": "1.0.0"}, "paths": {}}
DRIFTED_DOCUMENT = {
    "openapi": "3.1.0",
    "info": {"title": "t", "version": "2.0.0"},
    "paths": {"/new": {"get": {"responses": {"200": {"description": "ok"}}}}},
}

#: One (component, facet) row per way a declaration can make a facet available,
#: with the component whose declaration supplies it. Each row is driven twice:
#: clean, where selecting the facet must report nothing, and drifted, where
#: selecting the same facet must report a mismatch.
POSITIVE_ROWS = (
    ("path-hash", "boundary"),
    ("implicit-with-paths", "boundary"),
    ("openapi-canonical", "boundary"),
    ("with-behavior", "behavior"),
    ("with-version", "compat"),
    ("leaf", "exact"),
)

#: The components whose boundary or behavior input the drift fixture rewrites.
#: `with-version` drifts through its version file instead.
DRIFTING_COMPONENTS = (
    "path-hash",
    "implicit-with-paths",
    "openapi-canonical",
    "with-behavior",
    "leaf",
)


def _build_provider_repository(scene: Scenario) -> Scenario:
    """One component per registry key, plus the shapes that straddle the table."""
    scene.config["components"] = {}
    for provider in sorted(create_registry()):
        paths = [] if provider in {"leaf", "implicit"} else ["api/v1.json"]
        scene.component(provider, path=provider, provider=provider, boundary=paths)
        scene.json_file(f"{provider}/api/v1.json", DOCUMENT)
    scene.component(
        "implicit-with-paths",
        path="implicit-with-paths",
        provider="implicit",
        boundary=["api/v1.json"],
    )
    scene.json_file("implicit-with-paths/api/v1.json", DOCUMENT)
    scene.component(
        "with-behavior",
        path="with-behavior",
        boundary=["api/v1.json"],
        behavior=["api/v1.json"],
    )
    scene.json_file("with-behavior/api/v1.json", DOCUMENT)
    scene.component(
        "empty-behavior",
        path="empty-behavior",
        boundary=["api/v1.json"],
        behavior=[],
    )
    scene.json_file("empty-behavior/api/v1.json", DOCUMENT)
    scene.component(
        "with-version",
        path="with-version",
        boundary=["api/v1.json"],
        version_source=dict(VERSION_SOURCE),
    )
    scene.json_file("with-version/api/v1.json", DOCUMENT)
    scene.json_file("with-version/version.json", {"version": "1.2.3"})
    return scene


class UnavailableFacetSelectionTests(unittest.TestCase):
    """OBL-PROVIDERS-019: selecting an unavailable facet is a usage error.

    One repository holds a component per registry key, plus the three shapes
    that straddle a boundary in the table: an `implicit` with paths, a
    `behavior` with a non-empty list, and a declared version source. Verifying
    it component by component, facet by facet, turns the table above into a
    statement about what the verify loop does rather than about what a helper
    returns. A second copy of the same repository is locked and then drifted,
    so every positive row is shown gating before it is shown clean.

    Locking that repository is itself a check, and its failure has to arrive by
    name. A provider added to the registry that resolves without publishing
    entries makes `compute_boundary` refuse, and generation then raises a
    `ConfigError` from a class fixture, which reads as infrastructure rather
    than as a finding. `setUpClass` catches it so `setUp` can say what happened.
    """

    scene: Scenario
    drifted: Scenario
    lockfile: dict
    snapshot: object
    declarations: Dict[str, dict]
    generation_error: Optional[str]

    @classmethod
    def setUpClass(cls):
        scene = _build_provider_repository(Scenario())
        scene.commit()
        cls.scene = scene
        cls.declarations = dict(scene.config["components"])
        cls.generation_error = None
        try:
            cls.lockfile = generate_lockfile(scene.config, scene.root, source="head")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            cls.lockfile = {}
            cls.generation_error = f"{type(exc).__name__}: {exc}"
        scene.commit("lock")
        cls.snapshot = _capture_git_source_snapshot(scene.root, "head")

        drifted = _build_provider_repository(Scenario())
        drifted.commit()
        cls.drifted = drifted
        try:
            cls.drifted_lockfile = generate_lockfile(
                drifted.config, drifted.root, source="head"
            )
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            cls.drifted_lockfile = {}
            if cls.generation_error is None:
                cls.generation_error = f"{type(exc).__name__}: {exc}"
        drifted.commit("lock")
        for component in DRIFTING_COMPONENTS:
            drifted.json_file(f"{component}/api/v1.json", DRIFTED_DOCUMENT)
        drifted.json_file("with-version/version.json", {"version": "9.9.9"})
        drifted.commit("drift")
        cls.drifted_snapshot = _capture_git_source_snapshot(drifted.root, "head")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.scene.close()
        finally:
            cls.drifted.close()

    def setUp(self):
        if self.generation_error is not None:
            self.fail(
                "the fixture could not lock one component per registered "
                "provider, so no facet claim in this class was measured: "
                + self.generation_error
            )

    def _verify(self, component: str, facet: str) -> List[str]:
        return verify_lockfile(
            self.scene.config,
            self.lockfile,
            self.scene.root,
            source="head",
            components_filter=[component],
            facets=[facet],
            snapshot=self.snapshot,
        )

    def _verify_drifted(self, component: str, facet: str) -> List[str]:
        return verify_lockfile(
            self.drifted.config,
            self.drifted_lockfile,
            self.drifted.root,
            source="head",
            components_filter=[component],
            facets=[facet],
            snapshot=self.drifted_snapshot,
        )

    def test_every_registered_provider_produces_a_lockable_component(self):
        """The premise for the whole class, and the check a new provider meets.

        `compute_boundary` refuses a resolve that publishes nothing unless the
        provider is literally `LeafProvider` or `ImplicitProvider`, and
        generation turns that refusal into a `ConfigError`. Three stub
        providers were registered to confirm it - `ok` with no entries,
        `partial` with no errors, `partial` with errors - and all three landed
        here, each naming itself: "acme-stub: Provider 'acme-stub' returned an
        invalid result", "acme-excused: Provider 'acme-excused' returned an
        empty partial result; only the built-in implicit provider may omit a
        boundary". A well-behaved addition, a `PathHashProvider` subclass,
        passed the whole class unchanged.
        """
        self.assertIsNone(self.generation_error)
        self.assertEqual(
            sorted(self.lockfile["components"]), sorted(self.declarations)
        )

    def test_the_repository_declares_every_registered_provider(self):
        """The premise: the sweep below really covers the whole registry."""
        for provider in create_registry():
            self.assertIn(provider, self.declarations)

    def test_the_lockfile_records_exactly_the_facets_the_rule_promises(self):
        """What generation actually wrote, against what the rule promises.

        `compute_boundary` records a boundary digest only when the provider's
        resolve published entries, and generation fills the other three slots
        from the declaration; `_available_component_facets` answers the same
        question without computing anything. Seventeen components times four
        facets in one comparison, thirty-four slots filled and thirty-four left
        null on the current registry. Dropping the non-empty check from the
        behavior clause of `_available_component_facets` makes this name
        `empty-behavior` and both sets, which is how it was checked.
        """
        disagreements = []
        present = absent = 0
        for name in sorted(self.declarations):
            fingerprints = self.lockfile["components"][name]["fingerprints"]
            recorded = {
                facet for facet in FACETS if fingerprints.get(facet) is not None
            }
            promised = _available_component_facets(self.declarations[name])
            present += len(recorded)
            absent += len(set(FACETS) - recorded)
            if recorded != promised:
                disagreements.append((name, sorted(recorded), sorted(promised)))
        self.assertEqual(disagreements, [], disagreements)
        self.assertGreater(present, 0)
        self.assertGreater(absent, 0)

    def test_the_lockfile_is_current_before_any_facet_is_selected(self):
        """The premise: every issue found below is a facet issue, not drift."""
        self.assertEqual(
            verify_lockfile(
                self.scene.config,
                self.lockfile,
                self.scene.root,
                source="head",
                snapshot=self.snapshot,
            ),
            [],
        )

    def test_selecting_a_facet_reports_unavailable_exactly_when_it_is_unavailable(self):
        disagreements = []
        refusals = 0
        for component in sorted(self.declarations):
            available = _available_component_facets(self.declarations[component])
            for facet in FACETS:
                issues = self._verify(component, facet)
                unavailable = [
                    issue for issue in issues if issue.startswith("UNAVAILABLE FACET ")
                ]
                if unavailable:
                    refusals += 1
                if bool(unavailable) != (facet not in available):
                    disagreements.append((component, facet, sorted(available), issues))
        self.assertEqual(disagreements, [], disagreements)
        self.assertGreater(refusals, 0)

    def test_the_refusal_names_the_component_the_facet_and_the_reason(self):
        self.assertEqual(
            self._verify("leaf", "boundary"),
            [
                "UNAVAILABLE FACET leaf.boundary: selected gate requires both "
                "locked and current digests"
            ],
        )

    def test_each_positive_row_gates_when_the_input_it_covers_drifts(self):
        """The premise for the clean rows: each selection is a live gate.

        Without this, `assertEqual(self._verify(component, facet), [])` is
        equally consistent with the facet having been compared and with it
        having been dropped from the explicit selection. On the drifted copy of
        the same repository each row must name its own component and facet.
        """
        for component, facet in POSITIVE_ROWS:
            with self.subTest(component=component, facet=facet):
                issues = self._verify_drifted(component, facet)
                self.assertTrue(
                    any(
                        issue.startswith(f"MISMATCH {component}.{facet}: lockfile=")
                        for issue in issues
                    ),
                    issues,
                )

    def test_the_positive_rows_of_the_table_verify_clean(self):
        """Every facet a declaration can produce passes when it is selected.

        Live, because the same six rows all report a mismatch on the drifted
        copy in the test above.
        """
        for component, facet in POSITIVE_ROWS:
            with self.subTest(component=component, facet=facet):
                self.assertEqual(self._verify(component, facet), [])

    def test_the_empty_behavior_list_is_the_one_that_is_refused(self):
        """The transition, read through verify rather than through the helper."""
        self.assertEqual(self._verify("with-behavior", "behavior"), [])
        self.assertEqual(
            self._verify("empty-behavior", "behavior"),
            [
                "UNAVAILABLE FACET empty-behavior.behavior: selected gate requires "
                "both locked and current digests"
            ],
        )


class UnavailableFacetExitCodeTests(unittest.TestCase):
    """OBL-PROVIDERS-019: the three routes to UNAVAILABLE FACET and exit 2.

    A declaration-level impossibility never reaches this diagnostic through a
    configured policy, because `validate_config` refuses the config first with
    "explicitly gates". The route that does reach it is a lock generated before
    the facet existed: the declaration is capable, so the static gate passes,
    and the locked digest is null, so verify has nothing to compare.
    """

    @classmethod
    def setUpClass(cls):
        scene = Scenario()
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/main.py", "x = 1\n")
        scene.commit()
        generated = run_cli(scene.root, "generate", "--source", "head")
        assert generated.returncode == 0, generated.stderr
        scene.commit("lock")
        cls.leafy = scene

    @classmethod
    def tearDownClass(cls):
        cls.leafy.close()

    def test_a_clean_verify_of_the_leaf_repository_exits_zero(self):
        """The premise: the exit 2 below is the facet selection, not drift."""
        result = run_cli(self.leafy.root, "verify", "--source", "head")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("UNAVAILABLE FACET", result.stdout + result.stderr)

    def test_the_facets_flag_exits_two_for_each_facet_the_leaf_cannot_supply(self):
        for facet in ("behavior", "boundary", "compat"):
            with self.subTest(facet=facet):
                result = run_cli(
                    self.leafy.root, "verify", "--source", "head", "--facets", facet
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(
                    f"UNAVAILABLE FACET svc.{facet}: selected gate requires both "
                    "locked and current digests",
                    result.stdout + result.stderr,
                )

    def test_selecting_exact_gates_on_a_repository_that_has_drifted(self):
        """The premise for the clean exit below: `--facets exact` can fail.

        A separate leaf repository of the same shape, whose one source file
        changes after the lock is written. Selecting exact exits 1 and prints
        the mismatch as a gating issue; selecting behavior instead exits 2 for
        the unavailable facet and demotes the very same mismatch to NON-GATING
        DRIFT. The digests are stable because the component tree is two fixed
        files, so both lines are pinned as observed.
        """
        mismatch = (
            "MISMATCH svc.exact: lockfile=7d41fc15f419... current=b6d99848f468..."
        )
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/main.py", "x = 1\n")
            scene.commit()
            generated = run_cli(scene.root, "generate", "--source", "head")
            self.assertEqual(generated.returncode, 0, generated.stderr)
            scene.commit("lock")
            scene.file("svc/main.py", "x = 2\n")
            scene.commit("drift")

            gating = run_cli(
                scene.root, "verify", "--source", "head", "--facets", "exact"
            )
            output = gating.stdout + gating.stderr
            self.assertEqual(gating.returncode, 1, output)
            self.assertIn("LOCKFILE OUT OF DATE (1 issues):", output)
            self.assertIn(mismatch, output)
            self.assertNotIn("NON-GATING DRIFT", output)

            demoted = run_cli(
                scene.root, "verify", "--source", "head", "--facets", "behavior"
            )
            output = demoted.stdout + demoted.stderr
            self.assertEqual(demoted.returncode, 2, output)
            self.assertIn(
                "UNAVAILABLE FACET svc.behavior: selected gate requires both "
                "locked and current digests",
                output,
            )
            self.assertIn("NON-GATING DRIFT:\n  " + mismatch, output)

    def test_the_one_facet_the_leaf_can_supply_exits_zero(self):
        """Clean, and the gate was live: the drift test above makes it exit 1."""
        result = run_cli(
            self.leafy.root, "verify", "--source", "head", "--facets", "exact"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("MISMATCH", result.stdout + result.stderr)

    def _stale_compat_repository(self, route: str) -> Scenario:
        """Lock without a version source, then declare one and gate on compat."""
        scene = Scenario()
        scene.component("svc", path="svc", boundary=["api/v1.json"])
        scene.json_file("svc/api/v1.json", {"a": 1})
        scene.json_file("svc/version.json", {"version": "1.2.3"})
        scene.commit()
        generated = run_cli(scene.root, "generate", "--source", "head")
        assert generated.returncode == 0, generated.stderr
        scene.commit("lock")
        scene.config["components"]["svc"]["version_source"] = dict(VERSION_SOURCE)
        if route == "component":
            scene.config["components"]["svc"]["verify_facets"] = ["compat"]
        else:
            scene.config.setdefault("defaults", {})["verify_facets"] = ["compat"]
        scene.write_config()
        scene.git("add", "--all")
        scene.git("commit", "-m", "declare a version source")
        return scene

    def test_a_configured_compat_gate_reaches_unavailable_facet_and_exit_two(self):
        for route in ("component", "defaults"):
            with self.subTest(route=route):
                with self._stale_compat_repository(route) as scene:
                    result = run_cli(scene.root, "verify", "--source", "head")
                    output = result.stdout + result.stderr
                    self.assertEqual(result.returncode, 2, output)
                    self.assertIn(
                        "UNAVAILABLE FACET svc.compat: selected gate requires both "
                        "locked and current digests",
                        output,
                    )

    def test_a_declaration_level_impossibility_is_refused_before_verify_runs(self):
        """The contrast that explains why the fixture above is shaped that way."""
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="leaf",
                            verify_facets=["boundary"])
            scene.file("svc/main.py", "x = 1\n")
            scene.commit()
            result = run_cli(scene.root, "verify", "--source", "head")
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 2, output)
            self.assertIn(
                "Component 'svc' explicitly gates 'boundary' but provider 'leaf' "
                "has no boundary paths",
                output,
            )
            self.assertNotIn("UNAVAILABLE FACET", output)


class ExplicitGateAgreementTests(unittest.TestCase):
    """OBL-PROVIDERS-014: the static gate decides what verify would decide.

    `_config.validate_config` refuses an explicitly declared policy that selects
    a facet the declaration cannot produce; `_utils._available_component_facets`
    answers the same question for the verify loop. They are separate
    implementations of one rule, so the test is a differential: for every
    declaration and every one of the fifteen non-empty selections, the set of
    facets the gate names must be exactly the selected facets the availability
    function withholds.
    """

    @classmethod
    def setUpClass(cls):
        scene = Scenario()
        scene.config["components"] = {}
        scene.json_file("svc/api/v1.json", {"a": 1})
        scene.file("svc/main.py", "x = 1\n")
        scene.json_file("svc/version.json", {"version": "1.2.3"})
        scene.commit()
        cls.scene = scene
        cls.snapshot = _capture_git_source_snapshot(scene.root, "head")

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def _gate_errors(self, declaration: dict, policy: dict) -> Set[str]:
        config = {"project": "p", "components": {"svc": copy.deepcopy(declaration)}}
        config["components"]["svc"].update(policy.get("component", {}))
        if "defaults" in policy:
            config["defaults"] = dict(policy["defaults"])
        errors = validate_config(
            config, self.scene.root, source="head", snapshot=self.snapshot
        )
        return {
            facet
            for facet in FACETS
            if any(f"explicitly gates '{facet}'" in error for error in errors)
        }

    def test_the_selection_table_is_every_non_empty_subset_of_the_vocabulary(self):
        """The premise: the differential is not two selections wearing fifteen names."""
        self.assertEqual(len(SELECTIONS), 15)
        self.assertEqual(len(set(SELECTIONS)), 15)
        self.assertEqual(
            {len(selection) for selection in SELECTIONS}, {1, 2, 3, 4}
        )
        for selection in SELECTIONS:
            self.assertLessEqual(set(selection), FACET_SET)

    def test_the_availability_rule_this_file_writes_out_matches_the_module(self):
        """The premise: the differential's oracle is the module's own answer."""
        for key, declaration, expected in _declaration_shapes():
            with self.subTest(shape=key):
                self.assertEqual(_available_component_facets(declaration), expected)

    def test_a_component_policy_names_exactly_the_facets_availability_withholds(self):
        disagreements = []
        demanding = permissive = 0
        for key, declaration, available in _declaration_shapes():
            for selection in SELECTIONS:
                expected = set(selection) - available
                if expected:
                    demanding += 1
                else:
                    permissive += 1
                named = self._gate_errors(
                    declaration, {"component": {"verify_facets": list(selection)}}
                )
                if named != expected:
                    disagreements.append(
                        (key, selection, sorted(named), sorted(expected))
                    )
        self.assertEqual(disagreements, [], disagreements[:8])
        self.assertGreater(demanding, permissive)

    def test_an_explicitly_present_defaults_policy_decides_the_same_way(self):
        disagreements = []
        refusals = 0
        for key, declaration, available in _declaration_shapes():
            expected = set(FACETS) - available
            if expected:
                refusals += 1
            named = self._gate_errors(
                declaration, {"defaults": {"verify_facets": list(FACETS)}}
            )
            if named != expected:
                disagreements.append((key, sorted(named), sorted(expected)))
        self.assertEqual(disagreements, [], disagreements[:8])
        self.assertGreater(refusals, 0)

    def test_a_component_override_replaces_the_defaults_policy_entirely(self):
        """An override of a facet the declaration has is accepted under an
        impossible default, and the reverse is refused."""
        leafy = _declaration("leaf", (), None, None)
        self.assertEqual(
            self._gate_errors(
                leafy,
                {
                    "defaults": {"verify_facets": ["boundary"]},
                    "component": {"verify_facets": ["exact"]},
                },
            ),
            set(),
        )
        self.assertEqual(
            self._gate_errors(
                leafy,
                {
                    "defaults": {"verify_facets": ["exact"]},
                    "component": {"verify_facets": ["boundary"]},
                },
            ),
            {"boundary"},
        )

    def test_an_omitted_policy_never_produces_a_gate_error(self):
        offenders = []
        for key, declaration, _available in _declaration_shapes():
            if self._gate_errors(declaration, {}):
                offenders.append(key)
        self.assertEqual(offenders, [], offenders[:8])

    def test_an_omitted_policy_is_the_absence_of_the_key_not_an_empty_dict(self):
        """The premise: the sweep above really left the gate un-entered."""
        leafy = _declaration("leaf", (), None, None)
        self.assertEqual(self._gate_errors(leafy, {"defaults": {}}), set())
        self.assertEqual(
            self._gate_errors(leafy, {"defaults": {"verify_facets": ["boundary"]}}),
            {"boundary"},
        )

    def test_each_of_the_four_refusals_is_reachable_and_names_its_reason(self):
        """The premise for the differential: the gate really does refuse."""
        observed = {}
        for label, declaration in (
            ("leaf boundary", _declaration("leaf", (), None, None)),
            ("implicit boundary", _declaration("implicit", (), None, None)),
            ("behavior", _declaration("path-hash", ("api/v1.json",), (), None)),
            ("compat", _declaration("path-hash", ("api/v1.json",), None, None)),
        ):
            config = {"project": "p", "components": {"svc": dict(declaration)}}
            config["components"]["svc"]["verify_facets"] = list(FACETS)
            observed[label] = [
                error
                for error in validate_config(
                    config, self.scene.root, source="head", snapshot=self.snapshot
                )
                if "explicitly gates" in error
            ]
        self.assertIn(
            "Component 'svc' explicitly gates 'boundary' but provider 'leaf' "
            "has no boundary paths",
            observed["leaf boundary"],
        )
        self.assertIn(
            "Component 'svc' explicitly gates 'boundary' but provider 'implicit' "
            "has no boundary paths",
            observed["implicit boundary"],
        )
        self.assertIn(
            "Component 'svc' explicitly gates 'behavior' but has no behavior.paths",
            observed["behavior"],
        )
        self.assertIn(
            "Component 'svc' explicitly gates 'compat' but has no version_source",
            observed["compat"],
        )

    @PROFILE
    @given(
        provider=st.sampled_from(sorted(create_registry())),
        boundary_paths=st.sampled_from(((), ("api/v1.json",))),
        behavior_paths=st.sampled_from(BEHAVIOR_SHAPES),
        has_version_source=st.booleans(),
        selection=st.lists(
            st.sampled_from(FACETS), min_size=1, max_size=4, unique=True
        ),
        route=st.sampled_from(("component", "defaults")),
    )
    def test_the_gate_and_availability_agree_on_any_declaration_and_selection(
        self,
        provider: str,
        boundary_paths,
        behavior_paths,
        has_version_source: bool,
        selection,
        route: str,
    ):
        version_source = dict(VERSION_SOURCE) if has_version_source else None
        declaration = _declaration(
            provider, boundary_paths, behavior_paths, version_source
        )
        policy = (
            {"component": {"verify_facets": list(selection)}}
            if route == "component"
            else {"defaults": {"verify_facets": list(selection)}}
        )
        self.assertEqual(
            self._gate_errors(declaration, policy),
            set(selection) - _available_component_facets(declaration),
        )


#: The policy routes a slice gate can arrive through, as (component override,
#: defaults, explicit --facets). `None` in a slot means the route is not taken.
SLICE_OVERRIDES = (None, ["exact"], ["boundary"], ["exact", "boundary"])
SLICE_DEFAULTS = (None, ["exact"], ["boundary"])
SLICE_EXPLICIT = (None, ["exact"], ["boundary"], ["exact", "boundary"])


class SliceGatePayloadTests(unittest.TestCase):
    """OBL-PROVIDERS-029: the JSON gated flag against where the drift lands.

    `_facet_policy.facet_policy_payload` and the slice loop in
    `_lockfile.verify_lockfile` each decide whether a slice gates, from the same
    config, and `core.py` prints the first while acting on the second. The sweep
    runs once in `setUpClass`, over every combination of member override,
    configured default and explicit `--facets`, and records what each row
    produced. Two tests then read those rows: one requires the flag to be true
    exactly when the mismatch is an issue rather than an observation, the other
    requires the recorded rows to be the whole product rather than a subset of
    it, which is a claim about the sweep that ran and not about `itertools`.
    """

    @classmethod
    def setUpClass(cls):
        scene = Scenario()
        scene.component("alpha", path="alpha", boundary=["api/v1.json"])
        scene.component("beta", path="beta", boundary=["api/v1.json"])
        scene.json_file("alpha/api/v1.json", {"a": 1})
        scene.json_file("beta/api/v1.json", {"b": 1})
        scene.slice("wire", mode="boundary", components=["alpha", "beta"])
        scene.commit()
        cls.scene = scene
        cls.lockfile = generate_lockfile(scene.config, scene.root, source="head")
        scene.json_file("alpha/api/v1.json", {"a": 2})
        scene.commit("drift")
        cls.snapshot = _capture_git_source_snapshot(scene.root, "head")
        cls.rows = cls._sweep()

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    @classmethod
    def _sweep(cls) -> List[dict]:
        """Drive both implementations once per policy spelling, and record it."""
        rows: List[dict] = []
        for alpha, beta, defaults, explicit in itertools.product(
            SLICE_OVERRIDES, SLICE_OVERRIDES, SLICE_DEFAULTS, SLICE_EXPLICIT
        ):
            config = copy.deepcopy(cls.scene.config)
            if alpha is not None:
                config["components"]["alpha"]["verify_facets"] = list(alpha)
            if beta is not None:
                config["components"]["beta"]["verify_facets"] = list(beta)
            if defaults is not None:
                config.setdefault("defaults", {})["verify_facets"] = list(defaults)
            observations: List[str] = []
            issues = verify_lockfile(
                config,
                cls.lockfile,
                cls.scene.root,
                source="head",
                observations=observations,
                facets=list(explicit) if explicit is not None else None,
                snapshot=cls.snapshot,
            )
            payload = facet_policy_payload(
                config, list(explicit) if explicit is not None else None
            )
            rows.append(
                {
                    "spelling": repr((alpha, beta, defaults, explicit)),
                    "gated": payload["slices"]["wire"]["gated"],
                    "in_issues": any(
                        "SLICE MISMATCH wire.boundary" in message
                        for message in issues
                    ),
                    "in_observations": any(
                        "SLICE MISMATCH wire.boundary" in message
                        for message in observations
                    ),
                }
            )
        return rows

    def test_the_drift_moves_the_slice_fingerprint(self):
        """The premise: without a mismatch the sweep would assert nothing."""
        observations: List[str] = []
        issues = verify_lockfile(
            self.scene.config,
            self.lockfile,
            self.scene.root,
            source="head",
            observations=observations,
            snapshot=self.snapshot,
        )
        self.assertTrue(
            any("SLICE MISMATCH wire.boundary" in message for message in issues),
            (issues, observations),
        )

    def test_the_gated_flag_matches_where_the_slice_mismatch_lands(self):
        vacuous = [
            row
            for row in self.rows
            if not row["in_issues"] and not row["in_observations"]
        ]
        disagreements = [
            row
            for row in self.rows
            if (row["in_issues"] or row["in_observations"])
            and row["gated"] != row["in_issues"]
        ]
        gated_seen = {
            row["gated"]
            for row in self.rows
            if row["in_issues"] or row["in_observations"]
        }
        self.assertEqual(vacuous, [], vacuous[:8])
        self.assertEqual(disagreements, [], disagreements[:8])
        self.assertEqual(gated_seen, {True, False})

    def test_the_sweep_visits_every_combination_it_claims_to(self):
        """What the sweep recorded, against 4 x 4 x 3 x 4 policy spellings.

        The rows come from `setUpClass`, so this fails if the sweep skipped a
        spelling or visited one twice. It is not a property of
        `itertools.product`: delete the sweep and there is nothing to compare.
        """
        expected = {
            repr(combination)
            for combination in itertools.product(
                SLICE_OVERRIDES, SLICE_OVERRIDES, SLICE_DEFAULTS, SLICE_EXPLICIT
            )
        }
        self.assertEqual(len(expected), 192)
        visited = [row["spelling"] for row in self.rows]
        self.assertEqual(len(visited), 192)
        self.assertEqual(set(visited), expected)


class SliceGateGhostMemberTests(unittest.TestCase):
    """OBL-PROVIDERS-029: the member the two implementations look up differently.

    `_facet_policy.py` reads `effective_components.get(member, [])`, a dict built
    only from `config.components`, so a slice member that is not a configured
    component contributes no facets and the slice reads as ungated. The verify
    loop reads `all_components.get(cname, {})` and hands the empty mapping to
    `_available_component_facets`, which returns `{"exact"}`, so the same member
    contributes the exact facet and the slice gates. `validate_config` refuses
    such a config, so only a direct API caller can reach the disagreement, but
    both implementations process it without complaint.
    """

    @classmethod
    def setUpClass(cls):
        scene = Scenario()
        scene.component("alpha", path="alpha", boundary=["api/v1.json"])
        scene.json_file("alpha/api/v1.json", {"a": 1})
        scene.slice("wire", mode="exact", components=["alpha", "ghost"])
        scene.commit()
        cls.scene = scene
        cls.lockfile = generate_lockfile(
            scene.config, scene.root, source="head", strict=False
        )
        scene.file("alpha/notes.md", "hello\n")
        scene.commit("drift")
        cls.snapshot = _capture_git_source_snapshot(scene.root, "head")

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def _observe(self, override, defaults, explicit):
        config = copy.deepcopy(self.scene.config)
        if override is not None:
            config["components"]["alpha"]["verify_facets"] = list(override)
        if defaults is not None:
            config["defaults"] = {"verify_facets": list(defaults)}
        observations: List[str] = []
        issues = verify_lockfile(
            config,
            self.lockfile,
            self.scene.root,
            source="head",
            observations=observations,
            facets=list(explicit) if explicit is not None else None,
            snapshot=self.snapshot,
        )
        payload = facet_policy_payload(
            config, list(explicit) if explicit is not None else None
        )
        return (
            payload["slices"]["wire"]["gated"],
            [message for message in issues if "wire." in message],
            [message for message in observations if "wire." in message],
        )

    def test_the_configuration_is_one_validate_config_refuses(self):
        """The premise for calling the two implementations directly."""
        self.assertEqual(
            validate_config(
                self.scene.config,
                self.scene.root,
                source="head",
                snapshot=self.snapshot,
            ),
            ["Slice 'wire' references unknown component: ghost"],
        )

    def test_the_lock_records_the_ghost_member_with_a_null_digest(self):
        """The premise: the ghost is a member of the locked slice, not dropped."""
        locked = self.lockfile["slices"]["wire"]
        self.assertEqual(locked["components"], ["alpha", "ghost"])
        self.assertIsNone(locked["component_digests"]["ghost"])
        self.assertIsNotNone(locked["component_digests"]["alpha"])

    def test_the_payload_reports_no_facets_at_all_for_the_ghost(self):
        payload = facet_policy_payload(self.scene.config, None)
        self.assertEqual(sorted(payload["components"]), ["alpha"])
        self.assertNotIn("ghost", payload["components"])

    def test_the_current_behaviour_of_every_route_over_a_ghost_member(self):
        """The pin. A partial fix that moves any row must fail here."""
        mismatch = "SLICE MISMATCH wire.exact"
        unavailable = (
            "UNAVAILABLE FACET wire.exact: slice members lack locked or current "
            "digests: ghost"
        )
        expected = {
            "omitted": (True, [mismatch], []),
            "component override": (True, [unavailable], []),
            "defaults": (False, [], [mismatch]),
            "explicit exact": (True, [unavailable], []),
            "explicit boundary": (False, [], [mismatch]),
        }
        routes = {
            "omitted": (None, None, None),
            "component override": (["boundary"], None, None),
            "defaults": (None, ["boundary"], None),
            "explicit exact": (None, None, ["exact"]),
            "explicit boundary": (None, None, ["boundary"]),
        }
        for label, (override, defaults, explicit) in routes.items():
            with self.subTest(route=label):
                gated, issues, observations = self._observe(
                    override, defaults, explicit
                )
                want_gated, want_issues, want_observations = expected[label]
                self.assertEqual(gated, want_gated)
                self.assertEqual(len(issues), len(want_issues), issues)
                for message, prefix in zip(issues, want_issues):
                    self.assertTrue(message.startswith(prefix), message)
                self.assertEqual(len(observations), len(want_observations), observations)
                for message, prefix in zip(observations, want_observations):
                    self.assertTrue(message.startswith(prefix), message)

    def test_a_real_second_member_makes_the_same_routes_agree(self):
        """The control: the ghost is what breaks the agreement, not the shape."""
        with Scenario() as scene:
            scene.component("alpha", path="alpha", boundary=["api/v1.json"])
            scene.component("beta", path="beta", boundary=["api/v1.json"])
            scene.json_file("alpha/api/v1.json", {"a": 1})
            scene.json_file("beta/api/v1.json", {"b": 1})
            scene.slice("wire", mode="exact", components=["alpha", "beta"])
            scene.commit()
            lockfile = generate_lockfile(scene.config, scene.root, source="head")
            scene.file("alpha/notes.md", "hello\n")
            scene.commit("drift")
            snapshot = _capture_git_source_snapshot(scene.root, "head")
            for label, override in (("omitted", None), ("override", ["boundary"])):
                with self.subTest(route=label):
                    config = copy.deepcopy(scene.config)
                    if override is not None:
                        config["components"]["alpha"]["verify_facets"] = list(override)
                    observations: List[str] = []
                    issues = verify_lockfile(
                        config,
                        lockfile,
                        scene.root,
                        source="head",
                        observations=observations,
                        snapshot=snapshot,
                    )
                    payload = facet_policy_payload(config, None)
                    gated = payload["slices"]["wire"]["gated"]
                    in_issues = any(
                        "SLICE MISMATCH wire.exact" in message for message in issues
                    )
                    in_observations = any(
                        "SLICE MISMATCH wire.exact" in message
                        for message in observations
                    )
                    self.assertTrue(in_issues or in_observations, (issues, observations))
                    self.assertEqual(gated, in_issues)

    def test_the_payload_never_calls_a_slice_ungated_while_verify_gates_it(self):
        """The policy payload and verifier agree even on malformed direct input."""
        gated, issues, _observations = self._observe(["boundary"], None, None)
        gating = [
            message
            for message in issues
            if message.startswith(("SLICE MISMATCH wire.", "UNAVAILABLE FACET wire."))
        ]
        self.assertEqual(gated, bool(gating), (gated, gating))


if __name__ == "__main__":
    unittest.main()
