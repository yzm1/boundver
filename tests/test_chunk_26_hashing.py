"""Six promises about digest inputs and one declaration guard.

Everything in this file is a claim about an input transformation rather than
about a digest value, which is why so much of it was cheap to leave untested.
A suite can assert that two lockfiles agree, that a reformat did not move a
canonical digest, or that a permuted entry list hashed the same, and still say
nothing about *why*: sorting is stable, so a fixture that already arrives in
the right relative order hashes the same under a sort key reduced to its first
component; a metamorphic pair asserted only in the direction where nothing
moves cannot tell a stable digest from a constant one; and a digest recomputed
by calling the same function twice pins no wire format at all. Each test here
is written to fail under exactly those degenerate implementations.

Three of them therefore carry an oracle transcribed from ``spec/HASHING.md``
rather than borrowed from the code. The v3 frame — ``u64(len(magic)) || magic``,
domain, entry count, then four length-delimited fields per entry — is
rebuilt here from the document's own code block, so the entry-ordering test
compares a digest against an independently framed one and can additionally
frame the same multiset under three *wrong* sort keys and require that the real
digest match only the documented one. The tie-break multiset is the smallest
that distinguishes all four: one label, two modes, two object types and two
contents, listed in an input order that none of the candidate keys reproduces.
The line-ending oracle is a byte-by-byte scanner rather than a ``replace``
call, which is what lets it state the bare-CR clause as a property instead of
as a handful of chosen strings — and the whole-content NUL predicate is
exercised with the sentinel at the head, in the middle and as the final byte,
because a normalizer that scanned only a prefix is precisely the bug that would
make a Windows checkout and a Linux runner disagree.

The provider-ordering obligation needed a witness that does not exist in ASCII.
Both implementations of the rule — the streaming ``_ProviderEntryCollector.add``
and the post-hoc ``_resolved_boundary_error`` — must compare *encoded* label
bytes, and the only labels where that differs from comparing Python strings are
the ones carrying surrogateescaped Git bytes: ``U+DCFF`` sorts below ``U+FFFD``
as a code point but encodes to ``\\xff``, which sorts above ``\\xef\\xbf\\xbd``.
That pair, in both directions, is pinned as an always-run example beside a
property that drives both entry points over the same generated label lists and
requires all three verdicts — collector, validator, byte-order oracle — to be
the same string. The leaf obligation checks both declaration validation and the
provider boundary so non-empty paths cannot be accepted or silently ignored.

Covers OBL-HASHING-023, OBL-HASHING-024, OBL-HASHING-028, OBL-HASHING-029,
OBL-HASHING-031 and OBL-HASHING-035.
"""

from __future__ import annotations

import hashlib
import json
import struct
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest import mock

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from boundver import providers
from boundver._config import config_warnings, validate_config
from boundver._hashing import (
    HASH_DOMAIN_BOUNDARY,
    HASH_FRAME_VERSION,
    _hash_framed_entries,
    _normalize_hash_content,
    canonical_json,
    source_tree_digest,
)
from boundver._lockfile import (
    SEMANTIC_CONFIG_VERSION,
    _semantic_config,
    semantic_config_digest,
)
from boundver._utils import ConfigError, _available_component_facets
from boundver.providers import (
    ProviderContext,
    ResolvedBoundary,
    _ProviderEntryCollector,
    _resolved_boundary_error,
    compute_boundary,
    create_registry,
)

from tests._parity import run_cli
from tests._scenarios import Scenario

PROFILE = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)

#: The provider surface is resolved once per example, so this profile trades
#: examples for the thirteen registry members each one drives.
REGISTRY_PROFILE = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
)


# ---------------------------------------------------------------------------
# An oracle transcribed from spec/HASHING.md, holding none of boundver's code.
# ---------------------------------------------------------------------------

#: The magic the spec names in prose ("the ASCII value `boundver-hash/v3`"),
#: written out rather than imported so a changed constant fails here.
SPEC_MAGIC = b"boundver-hash/v3"


def _u64(value: int) -> bytes:
    return struct.pack(">Q", value)


def _encoded_entry(entry: tuple) -> Tuple[bytes, bytes, bytes, bytes]:
    label, mode, object_type, content = entry
    return (
        label.encode("utf-8", errors="surrogateescape"),
        mode.encode("ascii"),
        object_type.encode("ascii"),
        content,
    )


def _framed(prepared, domain: str) -> str:
    """SHA-256 over the v3 wire format's own code block in spec/HASHING.md."""
    prepared = list(prepared)
    domain_bytes = domain.encode("utf-8")
    digest = hashlib.sha256()
    digest.update(_u64(len(SPEC_MAGIC)))
    digest.update(SPEC_MAGIC)
    digest.update(_u64(len(domain_bytes)))
    digest.update(domain_bytes)
    digest.update(_u64(len(prepared)))
    for label, mode, object_type, content in prepared:
        digest.update(_u64(len(label)))
        digest.update(label)
        digest.update(_u64(len(mode)))
        digest.update(mode)
        digest.update(_u64(len(object_type)))
        digest.update(object_type)
        digest.update(_u64(len(content)))
        digest.update(content)
    return digest.hexdigest()


def _oracle_digest(entries, domain: str, key=None) -> str:
    prepared = [_encoded_entry(entry) for entry in entries]
    prepared.sort(key=key or (lambda item: (item[0], item[1], item[2], item[3])))
    return _framed(prepared, domain)


#: The documented key, and three ways of getting it wrong that no existing
#: fixture distinguishes. Every function orders the *encoded* four-tuple.
KEY_ORDERS = {
    "spec: label, mode, type, content": lambda item: (
        item[0], item[1], item[2], item[3],
    ),
    "wrong: type ahead of mode": lambda item: (
        item[0], item[2], item[1], item[3],
    ),
    "wrong: content ahead of mode": lambda item: (
        item[0], item[3], item[1], item[2],
    ),
    "wrong: label alone": lambda item: (item[0],),
}

#: One label, two modes, two object types and two contents. The smallest
#: multiset whose order is decided by all four fields at once; the listed input
#: order is reproduced by none of the four keys above, so a stable sort under a
#: truncated key cannot accidentally agree with the documented one.
TIE_BREAK_ENTRIES = [
    ("L", "100755", "blob", b"aaa"),
    ("L", "100644", "value", b"aaa"),
    ("L", "100644", "blob", b"bbb"),
    ("L", "100644", "blob", b"aaa"),
]

hash_entries = st.lists(
    st.tuples(
        st.sampled_from(("L", "M", "é", "\udcff")),
        st.sampled_from(("100644", "100755", "120000", "semantic")),
        st.sampled_from(("blob", "value")),
        st.sampled_from((b"", b"aaa", b"bbb")),
    ),
    min_size=1,
    max_size=5,
)


@st.composite
def entries_and_one_permutation(draw):
    entries = draw(hash_entries)
    return entries, draw(st.permutations(entries))


class FramedEntryOrderingTests(unittest.TestCase):
    """OBL-HASHING-023: one digest per multiset, under the documented key."""

    def test_the_spec_frame_distinguishes_the_orders_it_is_asked_about(self):
        """The premise: this oracle is order-sensitive and key-sensitive.

        Every assertion below is of the form "the real digest equals the
        oracle under one key and not under another". That is only evidence if
        the four orders are four different digests to begin with.
        """
        digests = {
            name: _oracle_digest(TIE_BREAK_ENTRIES, "probe", key)
            for name, key in KEY_ORDERS.items()
        }
        self.assertEqual(len(set(digests.values())), len(KEY_ORDERS), digests)
        orders = {
            name: [
                TIE_BREAK_ENTRIES.index(entry)
                for entry in sorted(
                    TIE_BREAK_ENTRIES, key=lambda e, k=key: k(_encoded_entry(e))
                )
            ]
            for name, key in KEY_ORDERS.items()
        }
        self.assertEqual(
            orders,
            {
                "spec: label, mode, type, content": [3, 2, 1, 0],
                "wrong: type ahead of mode": [3, 2, 0, 1],
                "wrong: content ahead of mode": [3, 1, 0, 2],
                "wrong: label alone": [0, 1, 2, 3],
            },
        )

    def test_the_frame_version_constant_still_names_the_documented_magic(self):
        self.assertEqual(HASH_FRAME_VERSION.encode("ascii"), SPEC_MAGIC)

    def test_the_tie_break_multiset_hashes_to_the_spec_key_and_no_other(self):
        actual = _hash_framed_entries(list(TIE_BREAK_ENTRIES), domain="probe")
        for name, key in KEY_ORDERS.items():
            expected = _oracle_digest(TIE_BREAK_ENTRIES, "probe", key)
            with self.subTest(key=name):
                if name.startswith("spec"):
                    self.assertEqual(actual, expected)
                else:
                    self.assertNotEqual(actual, expected)

    def test_each_key_field_alone_decides_an_order_the_previous_ones_tie(self):
        """Mode, then type, then content: each shown where all earlier tie."""
        cases = {
            "mode breaks a label tie": (
                ("L", "100644", "blob", b"x"),
                ("L", "100755", "blob", b"x"),
            ),
            "type breaks a label+mode tie": (
                ("L", "100644", "blob", b"x"),
                ("L", "100644", "value", b"x"),
            ),
            "content breaks a label+mode+type tie": (
                ("L", "100644", "blob", b"aaa"),
                ("L", "100644", "blob", b"bbb"),
            ),
        }
        for name, (lower, higher) in cases.items():
            with self.subTest(case=name):
                ascending = _hash_framed_entries([lower, higher], domain="probe")
                descending = _hash_framed_entries([higher, lower], domain="probe")
                self.assertEqual(ascending, descending)
                self.assertEqual(
                    ascending, _oracle_digest([lower, higher], "probe")
                )
                # Premise for the equality above: framing these two entries in
                # the two orders is not the same byte stream, so agreement is
                # the sort's doing and not the frame's indifference.
                self.assertNotEqual(
                    _framed([_encoded_entry(lower), _encoded_entry(higher)], "probe"),
                    _framed([_encoded_entry(higher), _encoded_entry(lower)], "probe"),
                )

    def test_a_label_orders_by_encoded_bytes_and_not_by_code_point(self):
        """U+DCFF encodes to \\xff, which sorts above U+FFFD's \\xef\\xbf\\xbd."""
        low_code_point = ("\udcff", "100644", "blob", b"x")
        high_code_point = ("\ufffd", "100644", "blob", b"x")
        self.assertLess(low_code_point[0], high_code_point[0])
        self.assertGreater(
            low_code_point[0].encode("utf-8", errors="surrogateescape"),
            high_code_point[0].encode("utf-8", errors="surrogateescape"),
        )
        digest = _hash_framed_entries(
            [low_code_point, high_code_point], domain="probe"
        )
        self.assertEqual(
            digest, _framed(
                [_encoded_entry(high_code_point), _encoded_entry(low_code_point)],
                "probe",
            )
        )
        self.assertNotEqual(
            digest, _framed(
                [_encoded_entry(low_code_point), _encoded_entry(high_code_point)],
                "probe",
            )
        )

    @PROFILE
    @given(entries_and_one_permutation())
    def test_any_permutation_of_one_multiset_hashes_to_the_spec_order(self, pair):
        entries, permuted = pair
        expected = _oracle_digest(entries, HASH_DOMAIN_BOUNDARY)
        self.assertEqual(
            _hash_framed_entries(list(entries), domain=HASH_DOMAIN_BOUNDARY),
            expected,
        )
        self.assertEqual(
            _hash_framed_entries(list(permuted), domain=HASH_DOMAIN_BOUNDARY),
            expected,
        )


# ---------------------------------------------------------------------------
# OBL-HASHING-024
# ---------------------------------------------------------------------------

def _canonical_line_endings(content: bytes) -> bytes:
    """The spec's rule as a scanner: a CR is rewritten only before an LF.

    Written as a two-byte lookahead walk rather than a ``replace`` call so the
    bare-CR clause is expressed by the oracle itself. A NUL anywhere in the
    whole buffer disables the conversion.
    """
    if b"\x00" in content:
        return content
    out = bytearray()
    index = 0
    while index < len(content):
        if content[index : index + 2] == b"\r\n":
            out.append(0x0A)
            index += 2
        else:
            out.append(content[index])
            index += 1
    return bytes(out)


#: Two byte spellings of one file and whether the rule says their digests must
#: agree. The keys are also the filenames the two fixture repositories carry.
LINE_ENDING_CASES = {
    "text_crlf_vs_lf": (b"alpha\r\nbeta\r\n", b"alpha\nbeta\n", True),
    "mixed_bare_cr_and_crlf": (b"a\rb\r\nc\r\n", b"a\rb\nc\n", True),
    "nul_at_head": (b"\x00alpha\r\nbeta\n", b"\x00alpha\nbeta\n", False),
    "nul_in_middle": (b"alpha\r\n\x00beta\n", b"alpha\n\x00beta\n", False),
    "nul_as_final_byte": (b"alpha\r\nbeta\n\x00", b"alpha\nbeta\n\x00", False),
    "bare_cr_is_not_a_terminator": (b"alpha\rbeta\r", b"alpha\nbeta\n", False),
    "cr_before_crlf_is_one_pass": (b"a\r\r\nb", b"a\r\nb", False),
    "premise_identical_bytes": (b"alpha\nbeta\n", b"alpha\nbeta\n", True),
    "premise_unrelated_text": (b"alpha\n", b"beta\n", False),
}


class ContentCanonicalizationTests(unittest.TestCase):
    """OBL-HASHING-024: CRLF folds to LF, but only in a buffer without NUL."""

    left: Scenario
    right: Scenario

    @classmethod
    def setUpClass(cls):
        """Two repositories carrying the same filenames with different bytes.

        The label is part of the digest, so the two spellings of a case have to
        live at the same repository path; that means two repositories rather
        than two files. Building them once keeps nine Git fixtures down to two.
        """
        cls.left = Scenario()
        cls.right = Scenario()
        for scene, index in ((cls.left, 0), (cls.right, 1)):
            scene.component("svc", path="svc", provider="leaf")
            for name, payloads in LINE_ENDING_CASES.items():
                target = scene.root / "svc" / f"{name}.bin"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payloads[index])
            scene.commit()

    @classmethod
    def tearDownClass(cls):
        cls.left.close()
        cls.right.close()

    def _digests(self, name: str) -> Tuple[str, str]:
        selector = f"svc/{name}.bin"
        left = source_tree_digest(self.left.root, selector, "head")
        right = source_tree_digest(self.right.root, selector, "head")
        self.assertIsNotNone(left, selector)
        self.assertIsNotNone(right, selector)
        return left, right

    def test_git_stored_each_fixture_byte_for_byte(self):
        """The premise: without this, every equality below is about LF vs LF.

        A developer machine with ``core.autocrlf=true`` stages LF for a file
        written with CRLF. ``Scenario`` pins the setting per repository, and
        this reads the committed blob back to prove the pin took.
        """
        for name, (left_bytes, right_bytes, _) in LINE_ENDING_CASES.items():
            with self.subTest(case=name):
                self.assertEqual(self.left.blob(f"svc/{name}.bin"), left_bytes)
                self.assertEqual(self.right.blob(f"svc/{name}.bin"), right_bytes)

    def test_the_comparison_produces_both_answers_across_the_table(self):
        """The premise for every inequality: the digest does respond to bytes."""
        observed = {
            name: self._digests(name)[0] == self._digests(name)[1]
            for name in LINE_ENDING_CASES
        }
        self.assertEqual(set(observed.values()), {True, False}, observed)
        self.assertTrue(observed["premise_identical_bytes"])
        self.assertFalse(observed["premise_unrelated_text"])

    def test_each_spelling_pair_agrees_exactly_when_the_rule_says_so(self):
        for name, (_, _, must_agree) in LINE_ENDING_CASES.items():
            with self.subTest(case=name):
                left, right = self._digests(name)
                if must_agree:
                    self.assertEqual(left, right)
                else:
                    self.assertNotEqual(left, right)

    def test_the_table_expectations_match_the_scanner_oracle(self):
        """The table's third column is derived, not asserted by hand."""
        for name, (left_bytes, right_bytes, must_agree) in (
            LINE_ENDING_CASES.items()
        ):
            with self.subTest(case=name):
                self.assertEqual(
                    _canonical_line_endings(left_bytes)
                    == _canonical_line_endings(right_bytes),
                    must_agree,
                )

    @PROFILE
    @given(st.binary(max_size=64))
    @example(b"a\rb")
    @example(b"a\r\nb")
    @example(b"\x00a\r\nb")
    @example(b"a\r\nb\x00")
    @example(b"a\r\r\nb")
    def test_normalization_is_the_documented_scan_for_any_byte_string(
        self, content: bytes
    ):
        self.assertEqual(_normalize_hash_content(content), _canonical_line_endings(content))

    @PROFILE
    @given(st.binary(max_size=64).filter(lambda value: b"\x00" not in value))
    def test_a_cr_not_followed_by_lf_survives_normalization(self, content: bytes):
        bare = sum(
            1
            for index, byte in enumerate(content)
            if byte == 0x0D and content[index + 1 : index + 2] != b"\n"
        )
        normalized = _normalize_hash_content(content)
        self.assertEqual(normalized.count(b"\r"), bare)

    @PROFILE
    @given(st.binary(min_size=1, max_size=64))
    def test_a_nul_anywhere_disables_the_conversion(self, content: bytes):
        for position in (0, len(content) // 2, len(content)):
            payload = content[:position] + b"\x00" + content[position:]
            self.assertEqual(
                _normalize_hash_content(payload), payload, f"NUL at {position}"
            )


def _single_file_context(payload: bytes) -> ProviderContext:
    """A component holding one file, whose bytes the test chooses."""
    store = {"svc/data.bin": payload}

    def read_file(repo_rel: str) -> bytes:
        return store[repo_rel]

    def list_files(prefix: str) -> List[str]:
        prefix = prefix.rstrip("/")
        return sorted(
            path
            for path in store
            if path == prefix or path.startswith(prefix + "/")
        )

    return ProviderContext(
        repo_root=Path("."),
        component_path="svc",
        boundary_cfg={"paths": ["data.bin"]},
        source="working-tree",
        read_file=read_file,
        list_files=list_files,
    )


class RawProviderCanonicalizationTests(unittest.TestCase):
    """OBL-HASHING-024, second implementation.

    ``PathHashProvider.resolve`` carries its own inline copy of the rule rather
    than calling ``_normalize_hash_content``, so a fault written into one copy
    and not the other is invisible to everything above. This drives that copy
    over the same oracle.
    """

    provider = providers.PathHashProvider()

    def _entry(self, payload: bytes) -> bytes:
        resolved = self.provider.resolve(_single_file_context(payload))
        self.assertEqual(resolved.status, "ok", resolved.errors)
        self.assertEqual([label for label, _ in resolved.entries], ["file:data.bin"])
        return resolved.entries[0][1]

    def test_the_providers_own_conversion_branch_is_reached_in_both_directions(self):
        """The premise: the provider does convert, and does decline to."""
        self.assertEqual(self._entry(b"a\r\nb"), b"a\nb")
        self.assertEqual(self._entry(b"a\r\nb\x00"), b"a\r\nb\x00")

    @PROFILE
    @given(st.binary(min_size=1, max_size=64))
    @example(b"a\rb")
    @example(b"a\r\nb")
    @example(b"\x00a\r\nb")
    @example(b"a\r\nb\x00")
    def test_the_raw_provider_publishes_the_same_canonical_bytes(
        self, payload: bytes
    ):
        self.assertEqual(self._entry(payload), _canonical_line_endings(payload))


# ---------------------------------------------------------------------------
# OBL-HASHING-028
# ---------------------------------------------------------------------------

#: The literal from spec/HASHING.md, written out rather than imported so that
#: changing the module constant fails here instead of silently redefining the
#: contract identifier the digest is supposed to make distinguishable.
SPEC_CONFIG_PREFIX = "boundver-semantic-config/v3"

#: Configurations the composition is pinned over. The non-ASCII one is the
#: case the obligation is really about: an ASCII-escaping reader agrees with
#: boundver on every other config in this file.
DIGEST_CONFIGS = {
    "minimal": {"project": "p", "components": {}},
    "non_ascii": {
        "project": "prøsjekt",
        "components": {
            "télémétrie": {
                "path": "svc",
                "boundary": {"provider": "leaf", "paths": []},
            }
        },
    },
    "populated": {
        "project": "p",
        "defaults": {"compat_mode": "minor", "verify_facets": ["exact", "boundary"]},
        "components": {
            "svc": {
                "path": "svc",
                "boundary": {"provider": "path-hash", "paths": ["api/*.json"]},
                "consumers": ["web"],
            },
            "web": {"path": "web", "boundary": {"provider": "leaf", "paths": []}},
        },
        "slices": {"public": {"mode": "exact", "components": ["svc", "web"]}},
    },
}


class SemanticConfigDigestCompositionTests(unittest.TestCase):
    """OBL-HASHING-028: prefix, one newline, canonical JSON, SHA-256."""

    def _payload(self, config: dict) -> str:
        return f"{SPEC_CONFIG_PREFIX}\n{canonical_json(_semantic_config(config))}"

    def test_the_module_constant_is_still_the_documented_identifier(self):
        self.assertEqual(SEMANTIC_CONFIG_VERSION, SPEC_CONFIG_PREFIX)

    def test_the_digest_is_sha256_of_the_documented_string(self):
        for name, config in DIGEST_CONFIGS.items():
            with self.subTest(config=name):
                expected = hashlib.sha256(
                    self._payload(config).encode("utf-8")
                ).hexdigest()
                self.assertEqual(semantic_config_digest(config), expected)

    #: Ways of composing the same two pieces that the spec does not describe.
    #: Each must produce a digest boundver does not.
    def test_no_other_spelling_of_the_prefix_reaches_the_same_digest(self):
        config = DIGEST_CONFIGS["non_ascii"]
        body = canonical_json(_semantic_config(config))
        variants = {
            "no separator": f"{SPEC_CONFIG_PREFIX}{body}",
            "carriage return separator": f"{SPEC_CONFIG_PREFIX}\r\n{body}",
            "two newlines": f"{SPEC_CONFIG_PREFIX}\n\n{body}",
            "v1 identifier": f"boundver-semantic-config/v1\n{body}",
            "prefix omitted": body,
            "prefix doubled": f"{SPEC_CONFIG_PREFIX}\n{SPEC_CONFIG_PREFIX}\n{body}",
            "trailing newline added": f"{SPEC_CONFIG_PREFIX}\n{body}\n",
            "ascii escaped body": (
                SPEC_CONFIG_PREFIX
                + "\n"
                + json.dumps(
                    _semantic_config(config),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
            ),
            "spaced separators": (
                SPEC_CONFIG_PREFIX
                + "\n"
                + json.dumps(
                    _semantic_config(config),
                    sort_keys=True,
                    separators=(", ", ": "),
                    ensure_ascii=False,
                )
            ),
            "unsorted keys": (
                SPEC_CONFIG_PREFIX
                + "\n"
                + json.dumps(
                    _semantic_config(config),
                    sort_keys=False,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
            ),
        }
        actual = semantic_config_digest(config)
        # Premise: the correct composition does reach it, so the inequalities
        # below are about the variant and not about an unreachable target.
        self.assertEqual(
            actual,
            hashlib.sha256(self._payload(config).encode("utf-8")).hexdigest(),
        )
        for name, payload in variants.items():
            with self.subTest(variant=name):
                self.assertNotEqual(
                    actual, hashlib.sha256(payload.encode("utf-8")).hexdigest()
                )

    def test_canonical_json_emits_non_ascii_literally_and_without_padding(self):
        body = canonical_json(_semantic_config(DIGEST_CONFIGS["non_ascii"]))
        self.assertIn("télémétrie", body)
        self.assertIn("prøsjekt", body)
        self.assertNotIn("\\u", body)
        self.assertNotIn(", ", body)
        self.assertNotIn(": ", body)
        self.assertNotIn("\n", body)
        self.assertTrue(body.startswith('{"components":{'), body[:40])

    def test_canonical_json_sorts_object_keys_by_code_point(self):
        body = canonical_json({"b": 1, "é": 2, "a": 3, "B": 4})
        self.assertEqual(body, '{"B":4,"a":3,"b":1,"é":2}')

    def test_an_unsorted_body_is_reachable_so_the_sorting_claim_bites(self):
        """The premise: this config's keys are not already in sorted order."""
        semantic = _semantic_config(DIGEST_CONFIGS["populated"])
        self.assertNotEqual(list(semantic), sorted(semantic))


# ---------------------------------------------------------------------------
# OBL-HASHING-029
# ---------------------------------------------------------------------------

DUPLICATE_LABELS = "entries must have unique labels"
UNSORTED_LABELS = "entries must be in deterministic sorted order"

#: The alphabet labels are built from. It is chosen so that some pairs sort one
#: way as code points and the other way as UTF-8 bytes: \udcff and \udc80 are
#: the surrogateescape spellings of the raw Git bytes \xff and \x80, which both
#: sort above the three-byte encoding of the higher code point U+FFFD.
LABEL_CHARACTERS = ("a", "b", "é", "\ufffd", "\udcff", "\udc80")

label_strategy = st.lists(
    st.sampled_from(LABEL_CHARACTERS), min_size=1, max_size=3
).map("".join)
label_lists = st.lists(label_strategy, min_size=1, max_size=4)


def _expected_ordering_error(labels: List[str]) -> Optional[str]:
    """Decide the verdict from the encoded bytes, whole-list first.

    Acceptance is stated as "the encoded labels are distinct and already in
    sorted order", which is a different formulation from the adjacent-pair walk
    both implementations use. The pair walk is consulted only to name which of
    the two messages a rejected list earns.
    """
    encoded = [label.encode("utf-8", errors="surrogateescape") for label in labels]
    if len(set(encoded)) == len(encoded) and encoded == sorted(encoded):
        return None
    for previous, current in zip(encoded, encoded[1:]):
        if current == previous:
            return DUPLICATE_LABELS
        if current < previous:
            return UNSORTED_LABELS
    raise AssertionError(f"no adjacent offence in a rejected list: {labels!r}")


def _collector_verdict(labels: List[str]) -> Optional[str]:
    collector = _ProviderEntryCollector()
    try:
        for label in labels:
            collector.add(label, b"x")
    except providers.ProviderError as exc:
        return str(exc)
    return None


def _validator_verdict(labels: List[str]) -> Optional[str]:
    return _resolved_boundary_error(
        ResolvedBoundary(
            entries=[(label, b"x") for label in labels], status="ok"
        )
    )


class _StubProvider:
    """A provider that returns whatever entries the test hands it."""

    name = "stub"
    version = "1"

    def __init__(self, entries: List[tuple]) -> None:
        self._entries = entries

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        return ResolvedBoundary(entries=list(self._entries), status="ok")


def _empty_context() -> ProviderContext:
    return ProviderContext(
        repo_root=Path("."),
        component_path="svc",
        boundary_cfg={"paths": []},
        source="working-tree",
        read_file=lambda repo_rel: b"",
        list_files=lambda prefix: [],
    )


class ProviderEntryOrderingTests(unittest.TestCase):
    """OBL-HASHING-029: two implementations of one rule, on encoded bytes."""

    #: Label pairs chosen so the two comparisons a reader might make disagree.
    DIFFERENTIAL_PAIRS = {
        "ascii ascending": (["a", "b"], None),
        "ascii descending": (["b", "a"], UNSORTED_LABELS),
        "repeated label": (["a", "a"], DUPLICATE_LABELS),
        "code point ascending, bytes descending": (
            ["\udcff", "\ufffd"],
            UNSORTED_LABELS,
        ),
        "code point descending, bytes ascending": (["\ufffd", "\udcff"], None),
    }

    def test_the_chosen_pairs_really_do_split_the_two_comparisons(self):
        """The premise: without this, the differential rows prove nothing."""
        agreements = {}
        for name, (labels, _) in self.DIFFERENTIAL_PAIRS.items():
            first, second = labels
            as_text = first < second
            as_bytes = first.encode("utf-8", errors="surrogateescape") < second.encode(
                "utf-8", errors="surrogateescape"
            )
            agreements[name] = as_text == as_bytes
        self.assertTrue(agreements["ascii ascending"])
        self.assertFalse(agreements["code point ascending, bytes descending"])
        self.assertFalse(agreements["code point descending, bytes ascending"])

    def test_both_entry_points_return_the_same_verdict_for_the_chosen_pairs(self):
        for name, (labels, expected) in self.DIFFERENTIAL_PAIRS.items():
            with self.subTest(pair=name):
                self.assertEqual(_collector_verdict(labels), expected)
                self.assertEqual(_validator_verdict(labels), expected)
                self.assertEqual(_expected_ordering_error(labels), expected)

    @PROFILE
    @given(label_lists)
    @example(["\udcff", "\ufffd"])
    @example(["\ufffd", "\udcff"])
    @example(["\udc80", "é"])
    @example(["a", "a"])
    def test_collector_and_validator_agree_with_the_byte_order_oracle(
        self, labels: List[str]
    ):
        expected = _expected_ordering_error(labels)
        self.assertEqual(_collector_verdict(labels), expected, labels)
        self.assertEqual(_validator_verdict(labels), expected, labels)

    def test_a_well_formed_result_does_reach_the_hashing_call(self):
        """The premise for the rejection test below."""
        provider = _StubProvider([("file:a", b"x"), ("file:b", b"y")])
        with mock.patch.object(
            providers, "_hash_framed_entries", wraps=providers._hash_framed_entries
        ) as spy:
            digest, status, errors = compute_boundary(provider, _empty_context())
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(status, "ok")
        self.assertEqual(errors, [])
        self.assertEqual(len(digest), 64)

    def test_an_out_of_order_or_repeated_label_never_reaches_the_hash(self):
        cases = {
            "descending ascii": [("file:b", b"x"), ("file:a", b"y")],
            "repeated label": [("file:a", b"x"), ("file:a", b"y")],
            "code point ascending, bytes descending": [
                ("\udcff", b"x"),
                ("\ufffd", b"y"),
            ],
        }
        for name, entries in cases.items():
            with self.subTest(case=name):
                provider = _StubProvider(entries)
                with mock.patch.object(
                    providers,
                    "_hash_framed_entries",
                    wraps=providers._hash_framed_entries,
                ) as spy:
                    digest, status, errors = compute_boundary(
                        provider, _empty_context()
                    )
                self.assertEqual(spy.call_count, 0)
                self.assertIsNone(digest)
                self.assertEqual(status, "error")
                self.assertEqual(len(errors), 1)
                self.assertTrue(
                    errors[0].startswith(
                        "Provider 'stub' returned an invalid result: "
                    ),
                    errors[0],
                )


# ---------------------------------------------------------------------------
# OBL-HASHING-031
# ---------------------------------------------------------------------------

#: One selector for every provider. The `.json` suffix keeps the canonical
#: providers on their strict-JSON branch, so nothing here needs PyYAML.
SELECTOR = "contract.json"

ENDPOINT_NAMES = ("ping", "health", "users", "orders", "items", "reports")
STATUS_CODES = ("200", "201", "202", "400", "404")
PREMISE_ENDPOINT = "/added-by-the-premise"


@st.composite
def contracts(draw):
    """A document that is valid JSON and a valid OpenAPI 3.1 document at once."""
    names = draw(
        st.lists(
            st.sampled_from(ENDPOINT_NAMES), min_size=1, max_size=3, unique=True
        )
    )
    paths = {}
    for name in names:
        code = draw(st.sampled_from(STATUS_CODES))
        paths["/" + name] = {
            "get": {"responses": {code: {"description": "ok"}}}
        }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": draw(st.sampled_from(("t", "sørvis", "サービス"))),
            "version": "1.0.0",
        },
        "paths": paths,
    }


def _spellings(document: dict) -> Dict[str, str]:
    """The same value, formatted five ways. No value differs between them."""
    return {
        "indent 2": json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        "indent 4": json.dumps(document, indent=4, ensure_ascii=False) + "\n",
        "compact": json.dumps(
            document, separators=(",", ":"), ensure_ascii=False
        ),
        "top level keys reversed": json.dumps(
            dict(reversed(list(document.items()))), indent=2, ensure_ascii=False
        )
        + "\n",
        "every key sorted": json.dumps(
            document, indent=2, sort_keys=True, ensure_ascii=False
        )
        + "\n",
    }


def _context(text: str) -> ProviderContext:
    store = {f"svc/{SELECTOR}": text.encode("utf-8")}

    def read_file(repo_rel: str) -> bytes:
        return store[repo_rel]

    def list_files(prefix: str) -> List[str]:
        prefix = prefix.rstrip("/")
        return sorted(
            path
            for path in store
            if path == prefix or path.startswith(prefix + "/")
        )

    return ProviderContext(
        repo_root=Path("."),
        component_path="svc",
        boundary_cfg={"paths": [SELECTOR]},
        source="working-tree",
        read_file=read_file,
        list_files=list_files,
    )


def _boundary_digest(provider, text: str) -> Optional[str]:
    digest, status, errors = compute_boundary(provider, _context(text))
    if status != "ok" or errors:
        raise AssertionError(f"{provider.name} did not resolve: {status} {errors}")
    return digest


def _classify(registry) -> Dict[str, str]:
    """Read raw/canonical off the entry label each provider emits.

    The label prefix is the observable difference the module documents:
    ``file:`` means the bytes were hashed, ``canonical:`` means a parsed value
    was. Deriving the two sets this way means a provider added tomorrow is
    placed by its behaviour rather than by a list in this file.
    """
    document = _spellings(
        {
            "openapi": "3.1.0",
            "info": {"title": "t", "version": "1.0.0"},
            "paths": {
                "/ping": {"get": {"responses": {"200": {"description": "ok"}}}}
            },
        }
    )["indent 2"]
    classification = {}
    for name, provider in registry.items():
        labels = [
            label for label, _ in provider.resolve(_context(document)).entries
        ]
        if not labels:
            classification[name] = "no entries"
        elif all(label.startswith("canonical:") for label in labels):
            classification[name] = "canonical"
        elif all(label.startswith("file:") for label in labels):
            classification[name] = "raw"
        else:
            classification[name] = f"unclassified: {labels}"
    return classification


class FormattingNoiseTests(unittest.TestCase):
    """OBL-HASHING-031: canonical holds still, raw moves, on the same file."""

    def setUp(self):
        self.registry = create_registry()
        self.classification = _classify(self.registry)
        self.raw = sorted(
            name
            for name, kind in self.classification.items()
            if kind == "raw"
        )
        self.canonical = sorted(
            name
            for name, kind in self.classification.items()
            if kind == "canonical"
        )

    def test_every_registered_provider_is_classified_by_what_it_emits(self):
        """The premise: neither side of the split is empty or guessed at."""
        self.assertEqual(set(self.classification), set(self.registry))
        self.assertEqual(
            set(self.classification.values()), {"raw", "canonical", "no entries"}
        )
        self.assertEqual(self.canonical, ["json-canonical", "openapi-canonical"])
        self.assertEqual(
            [
                name
                for name, kind in self.classification.items()
                if kind == "no entries"
            ],
            ["leaf"],
        )
        self.assertEqual(len(self.raw), 10)

    @REGISTRY_PROFILE
    @given(contracts())
    def test_reformatting_holds_canonical_still_and_moves_every_raw_provider(
        self, document
    ):
        spellings = _spellings(document)
        # Premise: the transforms the obligation names really do change bytes.
        self.assertNotEqual(spellings["indent 2"], spellings["indent 4"])
        self.assertNotEqual(spellings["indent 2"], spellings["top level keys reversed"])
        self.assertNotEqual(spellings["indent 2"], spellings["every key sorted"])

        with_endpoint = dict(document)
        with_endpoint["paths"] = dict(
            document["paths"],
            **{
                PREMISE_ENDPOINT: {
                    "get": {"responses": {"200": {"description": "ok"}}}
                }
            },
        )
        premise_text = _spellings(with_endpoint)["indent 2"]

        for name in self.canonical:
            provider = self.registry[name]
            digests = {
                spelling: _boundary_digest(provider, text)
                for spelling, text in spellings.items()
            }
            self.assertEqual(
                len(set(digests.values())), 1, f"{name} moved on a reformat: {digests}"
            )
            # Without this the equality above is satisfied by a provider that
            # returns one digest for every document.
            self.assertNotEqual(
                digests["indent 2"],
                _boundary_digest(provider, premise_text),
                f"{name} is a constant function",
            )

        for name in self.raw:
            provider = self.registry[name]
            digests = {
                spelling: _boundary_digest(provider, text)
                for spelling, text in spellings.items()
            }
            self.assertNotEqual(
                digests["indent 2"],
                digests["indent 4"],
                f"{name} ignored a reindent",
            )
            self.assertNotEqual(
                digests["indent 2"],
                digests["top level keys reversed"],
                f"{name} ignored a top-level key reordering",
            )
            self.assertNotEqual(
                digests["indent 2"],
                digests["every key sorted"],
                f"{name} ignored a nested key reordering",
            )
            for left in spellings:
                for right in spellings:
                    self.assertEqual(
                        digests[left] == digests[right],
                        spellings[left] == spellings[right],
                        f"{name}: {left} vs {right}",
                    )

    def test_a_value_change_moves_canonical_and_raw_alike(self):
        """Formatting is not the only thing canonical is allowed to ignore."""
        base = {
            "openapi": "3.1.0",
            "info": {"title": "t", "version": "1.0.0"},
            "paths": {
                "/ping": {"get": {"responses": {"200": {"description": "ok"}}}}
            },
        }
        changed = json.loads(json.dumps(base))
        changed["paths"]["/pong"] = changed["paths"].pop("/ping")
        before = _spellings(base)["indent 2"]
        after = _spellings(changed)["indent 2"]
        for name in self.canonical + self.raw:
            with self.subTest(provider=name):
                provider = self.registry[name]
                self.assertNotEqual(
                    _boundary_digest(provider, before),
                    _boundary_digest(provider, after),
                )


# ---------------------------------------------------------------------------
# OBL-HASHING-035
# ---------------------------------------------------------------------------

def _leaf_repository() -> Scenario:
    """A leaf component declaring boundary paths, beside a gated one.

    The second component is the premise carrier: it proves that this
    repository, this config and this lockfile can produce a boundary digest and
    a boundary facet, so the two absences asserted of the leaf are absences.
    """
    scene = Scenario()
    scene.component("svc", path="svc", provider="leaf", boundary=["api.txt"])
    scene.component("gate", path="gate", provider="path-hash", boundary=["api.txt"])
    scene.file("svc/api.txt", "surface\n")
    scene.file("gate/api.txt", "surface\n")
    scene.commit()
    return scene


class LeafWithDeclaredPathsTests(unittest.TestCase):
    """OBL-HASHING-035: a leaf cannot silently ignore declared paths."""

    def test_a_non_leaf_component_does_publish_a_boundary_digest_and_facet(self):
        """The premise for both absences below."""
        with Scenario() as scene:
            scene.component(
                "gate",
                path="gate",
                provider="path-hash",
                boundary=["api.txt"],
            )
            scene.file("gate/api.txt", "surface\n")
            scene.commit()
            lockfile = scene.generate()
            gate = lockfile["components"]["gate"]["fingerprints"]["boundary"]
            self.assertIsInstance(gate, str)
            self.assertEqual(len(gate), 64)
            self.assertIn(
                "boundary",
                _available_component_facets(scene.config["components"]["gate"]),
            )

    def test_a_leaf_with_paths_produces_a_controlled_generation_error(self):
        with _leaf_repository() as scene:
            with self.assertRaisesRegex(ConfigError, "cannot declare paths"):
                scene.generate(strict=False)

    def test_a_leaf_exposes_no_boundary_facet_even_with_paths_declared(self):
        with _leaf_repository() as scene:
            component = scene.config["components"]["svc"]
            self.assertEqual(component["boundary"]["paths"], ["api.txt"])
            self.assertEqual(
                sorted(_available_component_facets(component)), ["exact"]
            )

    def test_the_declaration_validator_does_report_comparable_mistakes(self):
        """The premise for the divergence: the mechanism exists and speaks."""
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="path-hash", boundary=[])
            scene.file("svc/api.txt", "surface\n")
            scene.commit()
            self.assertEqual(
                validate_config(scene.config, scene.root),
                [
                    "Component 'svc': No boundary paths declared for explicit "
                    "boundary provider"
                ],
            )
        with Scenario() as scene:
            scene.component(
                "svc",
                path="svc",
                provider="leaf",
                boundary=["api.txt"],
                verify_facets=["boundary"],
            )
            scene.file("svc/api.txt", "surface\n")
            scene.commit()
            errors = validate_config(scene.config, scene.root)
            self.assertTrue(
                any("explicitly gates 'boundary'" in error for error in errors),
                errors,
            )
            self.assertTrue(
                any("cannot declare paths" in error for error in errors),
                errors,
            )

    def test_declaring_paths_on_a_leaf_is_reported_as_a_declaration_error(self):
        with _leaf_repository() as scene:
            errors = validate_config(scene.config, scene.root)
            self.assertTrue(
                any(
                    "svc" in error and "cannot declare paths" in error
                    for error in errors
                ),
                f"validate_config accepted leaf with declared paths: {errors}",
            )

    def test_validate_config_refuses_the_misdeclaration(self):
        with _leaf_repository() as scene:
            self.assertEqual(config_warnings(scene.config, scene.root), [])
            result = run_cli(scene.root, "validate-config")
            self.assertEqual(result.returncode, 2)
            self.assertIn("cannot declare paths", result.stdout)
            self.assertNotIn("Config is valid", result.stdout)

    def test_explain_refuses_instead_of_presenting_ignored_paths(self):
        with _leaf_repository() as scene:
            result = run_cli(scene.root, "explain", "svc")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn("cannot declare paths", result.stderr)


if __name__ == "__main__":
    unittest.main()
