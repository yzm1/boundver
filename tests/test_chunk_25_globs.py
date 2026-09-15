"""Six contracts that only hold if something outside boundver can check them.

Five of the six obligations here are about agreement between two things that
were written separately. The published lock schema and the hand-written
validator in `_lockfile_validation` classify the same document; the canonical
JSON encoder and `json.dumps` produce the same bytes; the v3 hash framing and
any second implementation of `spec/HASHING.md` produce the same digest; a
declared path means the same thing to the config validator and to the hasher;
and `init --out` decides containment next to a sibling resolver that decides it
differently. Agreement of that kind cannot be tested by asking one side what it
thinks, which is roughly what the existing coverage does: the schema is checked
in one direction at hand-picked spots, the encoder's entire differential
against `json.dumps` is one value in `test_residual_memory_bounds`, and no
tracked test spells `boundver-hash/v3` at all, so nothing re-encodes the frame
without boundver's help. Every test below therefore carries a second opinion
that never consults the code it is judging - a `jsonschema` validator reading
the published file, `json.dumps`, and a `struct.pack('>Q', ...)` encoder
written from the fenced block in `spec/HASHING.md`.

The schema differential is the part that needed a fixture rather than a list.
Naming positions by hand is how the two files drifted in the first place, so
the witness document is synthesized from the schema itself - every declared
property gets a value derived from its own `const`, `enum`, `pattern` or type -
and the mutation walk enumerates positions from that same traversal. A property
added to the schema tomorrow is either given a value by one of the synthesis
rules and mutated automatically, or it hits the `no synthesis rule` assertion
and the check fails naming it. The premise that makes the walk mean anything is
asserted first: the synthesized witness is accepted by both readers, so every
later rejection is caused by the mutation and not by the scaffolding.

Four of the six diverge, and every divergence is pinned rather than softened.
The lock readers disagree at eight positions and crash at two more: an optional
array spelled `null` is accepted by boundver and refused by the published
contract, and `boundary_status` or a slice `mode` carrying a list or an object
reaches a `value not in {...}` membership test and raises `TypeError`, which
`boundver verify` prints as a traceback. `register_provider` reads `name`
twice, and a provider whose second read differs is filed under the second read
rather than refused, so a hostile extension replaces the built-in `path-hash`
entry. `_normalize_declared_path` strips a whole run of trailing slashes, so a
directory has three accepted spellings and not the one the obligation allows.
And `init --out` refuses `../outside.json` while writing an absolute path
outside the repository and reporting success. Every absence asserted in this
file - a registry that did not change, a directory that was not created, a
digest that did not move - is preceded by a test proving the same mechanism
reports the opposite when the opposite is true.

Covers OBL-GLOBS-027, OBL-GLOBS-037, OBL-GLOBS-043, OBL-GLOBS-064,
OBL-GLOBS-065 and OBL-HASHING-022.
"""

from __future__ import annotations

import copy
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple
from unittest import mock

import jsonschema
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver import _canonical_providers, _hashing, providers
from boundver._baseline import BaselineError
from boundver._canonical_providers import (
    CanonicalJsonLimitError,
    _canonical_json_bytes,
)
from boundver._config import validate_config
from boundver._hashing import (
    _ModeAwareBytes,
    _hash_framed_entries,
    _hash_prepared_entries,
)
from boundver._lockfile import (
    _lockfile_schema_issues,
    _lockfile_structure_issues,
)
from boundver._utils import (
    MAX_DECLARED_PATH_BYTES,
    MAX_GLOB_SEGMENTS,
    ConfigError,
    ProviderError,
    _normalize_declared_path,
)
from boundver.core import _resolve_baseline_path
from boundver.providers import (
    ResolvedBoundary,
    create_registry,
    get_provider,
    register_provider,
)

from tests._parity import run_cli
from tests._scenarios import Scenario

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

PROFILE = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)


# ---------------------------------------------------------------------------
# OBL-GLOBS-027: the published lock schema against the hand-written validator
# ---------------------------------------------------------------------------

LOCK_SCHEMA = json.loads(
    (REPOSITORY_ROOT / "spec" / "boundary.lock.schema.json").read_text(
        encoding="utf-8"
    )
)
LOCK_SCHEMA_VALIDATOR = jsonschema.Draft202012Validator(LOCK_SCHEMA)

#: The single map key the witness uses for both `components` and `slices`, and
#: for the two digest maps nested inside them. Every position label below is
#: spelled with it.
SAMPLE_KEY = "svc"

DIGEST = "a" * 64

#: One probe value per JSON shape a lock field can carry. These sixteen values
#: are the only thing in the differential written by hand: the positions they
#: are written to are enumerated from the published schema at run time, so a
#: field added to the schema is probed with all sixteen without being named
#: here.
LOCK_FIELD_PROBES: Dict[str, Any] = {
    "null": None,
    "true": True,
    "int": 7,
    "float": 1.5,
    "empty string": "",
    "plain string": "x",
    "uppercase digest": "A" * 64,
    "short digest": "a" * 63,
    "digest": DIGEST,
    "empty array": [],
    "string array": ["a"],
    "int array": [1],
    "duplicate array": ["a", "a"],
    "empty object": {},
    "digest map": {"k": DIGEST},
    "null map": {"k": None},
}

#: Container probes used to verify enum positions are diagnosed without
#: depending on value hashability.
UNHASHABLE_PROBES = frozenset(
    {
        "empty array",
        "string array",
        "int array",
        "duplicate array",
        "empty object",
        "digest map",
        "null map",
    }
)

#: The two enum positions exercised with every unhashable probe.
UNHASHABLE_CRASH_POSITIONS = frozenset(
    {
        f"components.{SAMPLE_KEY}.boundary_status",
        f"slices.{SAMPLE_KEY}.mode",
    }
)

#: Root shapes that are not objects at all. The walk below only mutates fields
#: of an object witness, so these are stated separately.
NON_OBJECT_ROOTS: Dict[str, Any] = {
    "null root": None,
    "list root": ["schema"],
    "string root": "boundary-lock/v3",
    "int root": 3,
}


def _resolve_schema_reference(node: dict) -> dict:
    """Return *node* with a local `$defs` reference substituted in place."""
    reference = node.get("$ref")
    if reference is None:
        return node
    if not reference.startswith("#/$defs/"):  # pragma: no cover - guard
        raise AssertionError(f"unsupported schema reference: {reference}")
    resolved = dict(LOCK_SCHEMA["$defs"][reference.rsplit("/", 1)[-1]])
    resolved.update({k: v for k, v in node.items() if k != "$ref"})
    return resolved


def _synthesize(node: dict, where: Tuple[str, ...]) -> Any:
    """Build a value the published schema declares valid at *where*.

    The rules read the schema rather than the lock format, so a property added
    to the schema either matches one of them or raises, which fails the check
    naming the position instead of silently leaving it unprobed.
    """
    node = _resolve_schema_reference(node)
    if "const" in node:
        return node["const"]
    if "enum" in node:
        return node["enum"][0]
    if "oneOf" in node:
        return _synthesize(node["oneOf"][0], where)
    declared = node.get("type")
    types = [declared] if isinstance(declared, str) else list(declared or [])
    if "string" in types:
        if node.get("pattern") == "^[0-9a-f]{64}$":
            return DIGEST
        return "x"
    if "array" in types:
        item_type = node.get("items", {}).get("type")
        return ["a"] if item_type == "string" else []
    if "object" in types:
        extra = node.get("additionalProperties")
        if isinstance(extra, dict):
            return {SAMPLE_KEY: _synthesize(extra, where + (SAMPLE_KEY,))}
        return {
            name: _synthesize(child, where + (name,))
            for name, child in node.get("properties", {}).items()
        }
    raise AssertionError(
        f"no synthesis rule for schema position {'.'.join(where) or '<root>'}: "
        f"{node}"
    )


WITNESS_LOCK = _synthesize(LOCK_SCHEMA, ())


def _walk_schema(
    node: dict, where: Tuple[str, ...]
) -> Iterator[Tuple[Tuple[str, ...], dict]]:
    """Yield every position of the witness together with its subschema."""
    node = _resolve_schema_reference(node)
    yield where, node
    declared = node.get("type")
    types = [declared] if isinstance(declared, str) else list(declared or [])
    if "object" not in types:
        return
    extra = node.get("additionalProperties")
    if isinstance(extra, dict):
        yield from _walk_schema(extra, where + (SAMPLE_KEY,))
    for name, child in node.get("properties", {}).items():
        yield from _walk_schema(child, where + (name,))


SCHEMA_POSITIONS = list(_walk_schema(LOCK_SCHEMA, ()))


def _container_at(document: Any, path: Tuple[str, ...]) -> Any:
    for step in path:
        document = document[step]
    return document


def _with_value(path: Tuple[str, ...], value: Any) -> Any:
    document = copy.deepcopy(WITNESS_LOCK)
    _container_at(document, path[:-1])[path[-1]] = value
    return document


def _without_key(path: Tuple[str, ...]) -> Any:
    document = copy.deepcopy(WITNESS_LOCK)
    del _container_at(document, path[:-1])[path[-1]]
    return document


def _with_unknown_key(path: Tuple[str, ...]) -> Any:
    document = copy.deepcopy(WITNESS_LOCK)
    _container_at(document, path)["boundver_audit_unknown"] = 1
    return document


def _boundver_issues(document: Any) -> List[str]:
    """Everything boundver says about a persisted lock's structure."""
    return list(_lockfile_schema_issues(document)) + list(
        _lockfile_structure_issues(document)
    )


def _schema_rejects(document: Any) -> bool:
    return not LOCK_SCHEMA_VALIDATOR.is_valid(document)


def _lock_mutations() -> Iterator[Tuple[str, Any]]:
    """Every mutation the walk derives from the published schema."""
    for path, node in SCHEMA_POSITIONS:
        label = ".".join(path)
        if path:
            for probe, value in LOCK_FIELD_PROBES.items():
                yield f"{label} = {probe}", _with_value(path, value)
        for required in node.get("required", []):
            dropped = path + (required,)
            yield f"{'.'.join(dropped)} dropped", _without_key(dropped)
        if node.get("additionalProperties") is False:
            yield f"{label or '<root>'} + unknown field", _with_unknown_key(path)


def _classify_lock_mutations() -> Tuple[List[str], List[str]]:
    """Return (positions where the readers disagree, positions that crash)."""
    disagreements: List[str] = []
    crashes: List[str] = []
    for label, document in _lock_mutations():
        try:
            boundver_rejects = bool(_boundver_issues(document))
        except TypeError:
            crashes.append(label)
            continue
        if boundver_rejects != _schema_rejects(document):
            disagreements.append(label)
    return disagreements, crashes


class LockContractParityTests(unittest.TestCase):
    """OBL-GLOBS-027: one document, two readers, one verdict."""

    def test_the_synthesized_witness_is_accepted_by_both_readers(self):
        """The premise for every mutation below: the scaffolding is valid."""
        self.assertEqual(_boundver_issues(WITNESS_LOCK), [])
        self.assertFalse(_schema_rejects(WITNESS_LOCK))

    def test_the_witness_carries_every_property_the_schema_declares(self):
        """A field the synthesis skipped would never be mutated."""
        declared: set = set()

        def collect(node: Any) -> None:
            if isinstance(node, dict):
                for name, child in node.get("properties", {}).items():
                    declared.add(name)
                    collect(child)
                for key, child in node.items():
                    if key != "properties":
                        collect(child)
            elif isinstance(node, list):
                for child in node:
                    collect(child)

        collect(LOCK_SCHEMA)
        self.assertGreater(len(declared), 25, "the recount collected nothing")
        reached = {path[-1] for path, _ in SCHEMA_POSITIONS if path}
        self.assertEqual(
            declared - reached,
            set(),
            "schema properties the mutation walk never reaches",
        )

    def test_the_walk_covers_every_container_the_schema_seals(self):
        """`additionalProperties: false` is the rule the validator mirrors."""
        sealed = {
            ".".join(path) or "<root>"
            for path, node in SCHEMA_POSITIONS
            if node.get("additionalProperties") is False
        }
        self.assertEqual(
            sealed,
            {
                "<root>",
                f"components.{SAMPLE_KEY}",
                f"components.{SAMPLE_KEY}.fingerprints",
                f"components.{SAMPLE_KEY}.semver",
                f"slices.{SAMPLE_KEY}",
            },
        )

    def test_both_readers_refuse_a_root_that_is_not_an_object(self):
        for name, document in NON_OBJECT_ROOTS.items():
            with self.subTest(root=name):
                self.assertTrue(_boundver_issues(document))
                self.assertTrue(_schema_rejects(document))

    def test_every_schema_position_agrees(self):
        """The hand-written validator and published schema classify alike."""
        disagreements, crashes = _classify_lock_mutations()
        self.assertEqual(disagreements, [])
        self.assertEqual(crashes, [])

    def test_the_differential_examines_more_than_six_hundred_documents(self):
        """A walk that silently stopped enumerating would pass everything."""
        mutations = list(_lock_mutations())
        self.assertGreater(len(mutations), 600)
        self.assertEqual(len(mutations), len({label for label, _ in mutations}))

    def test_the_validator_and_the_published_schema_classify_alike(self):
        disagreements, _ = _classify_lock_mutations()
        self.assertEqual(disagreements, [])

    def test_the_validator_reports_an_unhashable_enum_instead_of_raising(self):
        _, crashes = _classify_lock_mutations()
        self.assertEqual(crashes, [])

    def test_a_hashable_wrong_enum_value_is_reported_not_raised(self):
        """The premise for the crash pin: the enum check normally diagnoses."""
        for position, expected in (
            (
                ("components", SAMPLE_KEY, "boundary_status"),
                f"LOCKFILE malformed: component '{SAMPLE_KEY}' boundary_status "
                "must be one of ok, partial, or error",
            ),
            (
                ("slices", SAMPLE_KEY, "mode"),
                f"LOCKFILE malformed: slice '{SAMPLE_KEY}' mode must be one of "
                "exact, behavior, boundary, or compat",
            ),
        ):
            with self.subTest(position=".".join(position)):
                document = _with_value(position, "not-a-member")
                self.assertIn(expected, _boundver_issues(document))
                self.assertTrue(_schema_rejects(document))

    def test_each_unhashable_enum_value_is_reported_where_the_schema_refuses(self):
        positions = {
            f"components.{SAMPLE_KEY}.boundary_status": (
                "components",
                SAMPLE_KEY,
                "boundary_status",
            ),
            f"slices.{SAMPLE_KEY}.mode": ("slices", SAMPLE_KEY, "mode"),
        }
        self.assertEqual(set(positions), set(UNHASHABLE_CRASH_POSITIONS))
        for label, path in positions.items():
            for probe in sorted(UNHASHABLE_PROBES):
                with self.subTest(position=label, probe=probe):
                    document = _with_value(path, LOCK_FIELD_PROBES[probe])
                    self.assertTrue(_boundver_issues(document))
                    self.assertTrue(_schema_rejects(document))

    def test_verify_diagnoses_an_unhashable_boundary_status(self):
        with _lock_repository() as scene:
            lock = scene.generate(source="working-tree")
            lock["components"]["svc"]["boundary_status"] = []
            (scene.root / "boundary.lock.json").write_text(
                json.dumps(lock, indent=2) + "\n", encoding="utf-8"
            )
            result = run_cli(scene.root, "verify", "--source", "working-tree")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback (most recent call last)", result.stderr)
        self.assertIn("boundary_status must be one of", result.stderr)

    def test_verify_reports_a_well_formed_lock_without_a_traceback(self):
        """The premise for the traceback assertion above."""
        with _lock_repository() as scene:
            lock = scene.generate(source="working-tree")
            (scene.root / "boundary.lock.json").write_text(
                json.dumps(lock, indent=2) + "\n", encoding="utf-8"
            )
            result = run_cli(scene.root, "verify", "--source", "working-tree")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback (most recent call last)", result.stderr)

    def test_a_generated_lock_is_accepted_by_the_published_schema(self):
        """The witness is synthetic; a real lock has to clear both too."""
        with _lock_repository() as scene:
            lock = scene.generate(source="working-tree")
        self.assertEqual(_boundver_issues(lock), [])
        self.assertFalse(_schema_rejects(lock))


def _lock_repository() -> Scenario:
    scene = Scenario()
    scene.component("svc", path="svc", provider="path-hash", boundary=["api.json"])
    scene.slice("release", mode="exact", components=["svc"])
    scene.file("svc/api.json", '{"a": 1}\n')
    scene.commit()
    return scene


# ---------------------------------------------------------------------------
# OBL-GLOBS-037: the canonical JSON encoder against json.dumps
# ---------------------------------------------------------------------------

#: The string classes the size predictor treats specially, plus the ones it
#: has no branch for and therefore must fall through to `len(encode('utf-8'))`.
#: Each name says which arm of `quoted_utf8_size` the value exercises.
CANONICAL_STRINGS = {
    "empty": "",
    "all seven short escapes": '"\\\b\f\n\r\t',
    "C0 NUL": "\x00",
    "C0 unit separator": "\x1f",
    "DEL": "\x7f",
    "C1 control": "\x80",
    "two-byte BMP": "\u00e9",
    "three-byte BMP": "\u4e2d",
    "astral pair": "\U0001f600",
    "mixed": 'a"b\nc\x00d\U0001f600e',
}

#: Whole trees, to pin key ordering and the separators as well as the strings.
CANONICAL_TREES = {
    "empty object": {},
    "empty array": [],
    "unsorted keys": {"b": 1, "a": 2, "A": 3, "": 4},
    "nested": {"b": [1, 2.5, None, True, False], "a": {"z": "x", "0": ""}},
    "scalars": [0, -1, 1.5, -0.0, True, False, None, ""],
    "deep": {"a": {"b": {"c": [{"d": "\u00e9"}]}}},
}


def _stdlib_canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


_json_leaves = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**18), max_value=10**18),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.text(st.characters(blacklist_categories=("Cs",)), max_size=12),
    st.sampled_from(sorted(CANONICAL_STRINGS.values())),
)

_json_trees = st.recursive(
    _json_leaves,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(
            st.text(st.characters(blacklist_categories=("Cs",)), max_size=8),
            children,
            max_size=4,
        ),
    ),
    max_leaves=12,
)

_json_strings = st.one_of(
    st.text(st.characters(blacklist_categories=("Cs",)), max_size=40),
    st.sampled_from(sorted(CANONICAL_STRINGS.values())),
)


class CanonicalJsonEncoderTests(unittest.TestCase):
    """OBL-GLOBS-037: the same bytes as the stdlib, and a predictor to match."""

    @PROFILE
    @given(_json_trees)
    def test_every_generated_tree_encodes_exactly_like_the_stdlib(self, value):
        self.assertEqual(
            _canonical_json_bytes(value, "generated"), _stdlib_canonical(value)
        )

    def test_each_named_string_class_encodes_exactly_like_the_stdlib(self):
        for name, text in CANONICAL_STRINGS.items():
            with self.subTest(string=name):
                self.assertEqual(
                    _canonical_json_bytes(text, "named"),
                    _stdlib_canonical(text),
                )

    def test_each_named_tree_encodes_exactly_like_the_stdlib(self):
        for name, value in CANONICAL_TREES.items():
            with self.subTest(tree=name):
                self.assertEqual(
                    _canonical_json_bytes(value, "named"),
                    _stdlib_canonical(value),
                )

    @PROFILE
    @given(_json_strings)
    def test_a_budget_of_exactly_the_encoded_length_is_enough(self, text):
        """The predictor is read through the budget it is used to enforce.

        `quoted_utf8_size` is internal, but it decides the one byte between
        accepted and refused. A budget equal to the true encoded length must
        succeed and one byte less must not, which is false for any predictor
        that is off in either direction.
        """
        exact = len(_stdlib_canonical(text))
        self.assertEqual(
            _canonical_json_bytes(text, "budget", max_bytes=exact),
            _stdlib_canonical(text),
        )
        with self.assertRaises(CanonicalJsonLimitError):
            _canonical_json_bytes(text, "budget", max_bytes=exact - 1)

    def test_the_budget_refusal_names_the_remaining_limit(self):
        with self.assertRaises(CanonicalJsonLimitError) as caught:
            _canonical_json_bytes("abc", "probe", max_bytes=4)
        self.assertEqual(
            str(caught.exception),
            "Canonical JSON for probe exceeds the 4-byte remaining provider limit",
        )

    def test_a_size_mismatch_is_a_hard_provider_error(self):
        """The premise for every silent-agreement assertion in this class.

        Nothing above can distinguish a correct predictor from a dead check
        unless the check is shown to fire. Lending the encoder a `json` module
        whose `dumps` returns one extra character makes it fire.
        """

        class _LongerDumps:
            JSONDecodeError = json.JSONDecodeError

            @staticmethod
            def dumps(value, **kwargs):
                return json.dumps(value, **kwargs) + " "

        with mock.patch.object(_canonical_providers, "_json_mod", _LongerDumps):
            with self.assertRaises(ProviderError) as caught:
                _canonical_json_bytes("abc", "probe")
        self.assertEqual(
            str(caught.exception),
            "JSON string encoding produced an unexpected size",
        )
        self.assertNotIsInstance(caught.exception, CanonicalJsonLimitError)
        self.assertEqual(_canonical_json_bytes("abc", "probe"), b'"abc"')

    def test_a_lone_surrogate_is_refused_rather_than_mis_sized(self):
        """The one input where `len(encode('utf-8'))` cannot be computed."""
        with self.assertRaises(ProviderError) as caught:
            _canonical_json_bytes("\ud800", "probe")
        message = str(caught.exception)
        self.assertTrue(
            message.startswith("Canonical JSON serialization failed for probe: "),
            message,
        )
        self.assertIn("surrogates not allowed", message)
        with self.assertRaises(UnicodeEncodeError):
            _stdlib_canonical("\ud800")

    def test_non_json_values_are_refused_where_the_stdlib_would_coerce(self):
        """A deliberate strictness gap, stated so it cannot drift unnoticed."""
        for name, value, expected in (
            (
                "bytes",
                b"bytes",
                "Object of type bytes is not JSON serializable",
            ),
            (
                "tuple",
                (1, 2),
                "Object of type tuple is not JSON serializable",
            ),
            ("integer key", {1: "a"}, "JSON object keys must be strings"),
        ):
            with self.subTest(value=name):
                with self.assertRaises(ProviderError) as caught:
                    _canonical_json_bytes(value, "probe")
                self.assertEqual(
                    str(caught.exception),
                    f"Canonical JSON serialization failed for probe: {expected}",
                )
        self.assertEqual(json.dumps({1: "a"}, sort_keys=True), '{"1": "a"}')


# ---------------------------------------------------------------------------
# OBL-HASHING-022: the v3 wire format re-derived from spec/HASHING.md
# ---------------------------------------------------------------------------

HASH_MAGIC = b"boundver-hash/v3"

#: Every domain the hashing module defines, read from the module so a fifth
#: one is framed by the table below without being added to it.
HASH_DOMAINS = tuple(
    sorted(
        value
        for name, value in vars(_hashing).items()
        if name.startswith("HASH_DOMAIN_") and isinstance(value, str)
    )
)

#: Entry lists chosen for the fields a single-entry golden vector cannot pin:
#: the entry count for n other than one, a length that does not fit in a byte,
#: and content that is empty or non-UTF-8.
FRAMING_TABLE: Dict[str, List[Tuple[bytes, bytes, bytes, bytes]]] = {
    "no entries": [],
    "one entry": [(b"file:a", b"100644", b"blob", b"x")],
    "three entries": [
        (b"file:a", b"100644", b"blob", b"x"),
        (b"file:b", b"100755", b"blob", b""),
        (b"file:c", b"120000", b"blob", b"target"),
    ],
    "duplicate tuples": [(b"d", b"semantic", b"value", b"z")] * 2,
    "label over 255 bytes": [(b"L" * 300, b"semantic", b"value", b"")],
    "multi-byte label": [
        ("file:\u00e9\U0001f600".encode("utf-8"), b"100644", b"blob", b"\x00\xff")
    ],
    "empty label and content": [(b"", b"semantic", b"value", b"")],
}


def _reference_digest(
    prepared: List[Tuple[bytes, bytes, bytes, bytes]],
    domain: str,
    *,
    length_format: str = ">Q",
    frame_entry_count: bool = True,
) -> str:
    """Re-encode the v3 frame from spec/HASHING.md without reading boundver.

    The keyword arguments exist only for the negative controls: they let a
    test build a deliberately wrong encoder and show that this one would have
    caught the difference.
    """

    def length(value: int) -> bytes:
        return struct.pack(length_format, value)

    body = bytearray()
    body += length(len(HASH_MAGIC)) + HASH_MAGIC
    encoded_domain = domain.encode("utf-8")
    body += length(len(encoded_domain)) + encoded_domain
    if frame_entry_count:
        body += length(len(prepared))
    for label, mode, object_type, content in prepared:
        body += length(len(label)) + label
        body += length(len(mode)) + mode
        body += length(len(object_type)) + object_type
        body += length(len(content)) + content
    return hashlib.sha256(bytes(body)).hexdigest()


_frame_bytes = st.binary(max_size=24)
_frame_token = st.text(
    st.characters(min_codepoint=33, max_codepoint=126), min_size=1, max_size=10
).map(lambda text: text.encode("ascii"))
_frame_entries = st.lists(
    st.tuples(_frame_bytes, _frame_token, _frame_token, _frame_bytes),
    max_size=6,
)
_frame_domains = st.one_of(
    st.sampled_from(HASH_DOMAINS),
    st.text(st.characters(blacklist_categories=("Cs",)), max_size=12),
)


class HashFramingWireFormatTests(unittest.TestCase):
    """OBL-HASHING-022: a second implementation, and proof it can disagree."""

    def test_the_module_defines_four_distinct_non_empty_domains(self):
        self.assertGreaterEqual(len(HASH_DOMAINS), 4)
        self.assertEqual(len(set(HASH_DOMAINS)), len(HASH_DOMAINS))
        for domain in HASH_DOMAINS:
            with self.subTest(domain=domain):
                self.assertTrue(domain)
                self.assertEqual(domain, domain.encode("ascii").decode("ascii"))

    def test_every_table_entry_matches_the_independent_encoder(self):
        for name, prepared in FRAMING_TABLE.items():
            for domain in HASH_DOMAINS:
                with self.subTest(entries=name, domain=domain):
                    self.assertEqual(
                        _hash_prepared_entries(list(prepared), domain=domain),
                        _reference_digest(list(prepared), domain),
                    )

    @PROFILE
    @given(_frame_entries, _frame_domains)
    def test_every_generated_entry_list_matches_the_independent_encoder(
        self, entries, domain
    ):
        self.assertEqual(
            _hash_prepared_entries(list(entries), domain=domain),
            _reference_digest(list(entries), domain),
        )

    def test_a_little_endian_length_field_would_have_been_caught(self):
        """Premise: the reference encoder is sensitive to byte order."""
        prepared = FRAMING_TABLE["three entries"]
        self.assertNotEqual(
            _hash_prepared_entries(list(prepared), domain="boundary"),
            _reference_digest(list(prepared), "boundary", length_format="<Q"),
        )

    def test_a_thirty_two_bit_length_field_would_have_been_caught(self):
        """Premise: the reference encoder is sensitive to length width."""
        prepared = FRAMING_TABLE["label over 255 bytes"]
        self.assertNotEqual(
            _hash_prepared_entries(list(prepared), domain="boundary"),
            _reference_digest(list(prepared), "boundary", length_format=">I"),
        )

    def test_an_omitted_entry_count_would_have_been_caught(self):
        """Premise: the count is framed, for n = 0, 1 and 3 alike."""
        for name in ("no entries", "one entry", "three entries"):
            with self.subTest(entries=name):
                prepared = FRAMING_TABLE[name]
                self.assertNotEqual(
                    _hash_prepared_entries(list(prepared), domain="boundary"),
                    _reference_digest(
                        list(prepared), "boundary", frame_entry_count=False
                    ),
                )

    def test_two_domains_one_a_prefix_of_the_other_are_distinguished(self):
        """The domain length prefix, which a bare concatenation would lose."""
        prepared = [(b"x", b"semantic", b"value", b"c")]
        short = _hash_prepared_entries(list(prepared), domain="bound")
        long = _hash_prepared_entries(list(prepared), domain="boundary")
        self.assertNotEqual(short, long)
        self.assertEqual(short, _reference_digest(list(prepared), "bound"))
        self.assertEqual(long, _reference_digest(list(prepared), "boundary"))

    def test_a_label_whose_prefix_is_another_label_is_distinguished(self):
        """The same argument one field down."""
        first = [(b"file:a", b"semantic", b"value", b"bc")]
        second = [(b"file:ab", b"semantic", b"value", b"c")]
        self.assertNotEqual(
            _hash_prepared_entries(first, domain="boundary"),
            _hash_prepared_entries(second, domain="boundary"),
        )

    def test_framed_entries_are_sorted_before_they_are_encoded(self):
        entries = [
            ("b", _ModeAwareBytes(b"1", "100644", "blob")),
            ("a", _ModeAwareBytes(b"2", "100644", "blob")),
        ]
        self.assertEqual(
            _hash_framed_entries(entries, domain="boundary"),
            _reference_digest(
                sorted(
                    [
                        (b"b", b"100644", b"blob", b"1"),
                        (b"a", b"100644", b"blob", b"2"),
                    ]
                ),
                "boundary",
            ),
        )

    def test_declared_order_would_have_been_caught(self):
        """Premise for the sort: the two orders really do differ."""
        unsorted = [
            (b"b", b"100644", b"blob", b"1"),
            (b"a", b"100644", b"blob", b"2"),
        ]
        self.assertNotEqual(
            _reference_digest(list(unsorted), "boundary"),
            _reference_digest(sorted(unsorted), "boundary"),
        )

    def test_plain_bytes_are_framed_with_the_semantic_value_identity(self):
        self.assertEqual(
            _hash_framed_entries([("lab", b"payload")], domain="boundary"),
            _reference_digest(
                [(b"lab", b"semantic", b"value", b"payload")], "boundary"
            ),
        )

    def test_a_mode_aware_entry_is_framed_with_its_git_mode_and_type(self):
        content = _ModeAwareBytes(b"target", "120000", "blob")
        self.assertEqual(
            _hash_framed_entries([("lab", content)], domain="exact-tree"),
            _reference_digest(
                [(b"lab", b"120000", b"blob", b"target")], "exact-tree"
            ),
        )

    def test_an_undecodable_filename_byte_round_trips_through_the_label(self):
        label = "file:\udcff"
        self.assertEqual(
            _hash_framed_entries(
                [(label, _ModeAwareBytes(b"x", "100644", "blob"))],
                domain="exact-tree",
            ),
            _reference_digest(
                [(b"file:\xff", b"100644", b"blob", b"x")], "exact-tree"
            ),
        )


# ---------------------------------------------------------------------------
# OBL-GLOBS-043: the declared path grammar, at both call sites
# ---------------------------------------------------------------------------

#: Every rejection class the obligation names, with the exact message the
#: normalizer raises. The declaration is component-relative and is written into
#: a real component's boundary list by the identity test below.
DECLARED_PATH_REJECTIONS: Dict[str, Tuple[str, str]] = {
    "empty": ("", "must not be empty or whitespace"),
    "whitespace only": ("   ", "must not be empty or whitespace"),
    "leading whitespace": (
        " api.json",
        "must not have leading or trailing whitespace",
    ),
    "trailing whitespace": (
        "api.json ",
        "must not have leading or trailing whitespace",
    ),
    "backslash": ("sub\\api.json", "must use '/' separators"),
    "absolute": ("/etc/passwd", "must be relative"),
    "drive prefixed": ("C:/x", "must be relative"),
    "empty segment": ("sub//api.json", "must not contain empty path segments"),
    "leading dot segment": ("./api.json", "must not contain '.' path segments"),
    "inner dot segment": ("sub/./api.json", "must not contain '.' path segments"),
    "parent segment": (
        "../outside.json",
        "must not contain '..' path segments; the path escapes its declared root",
    ),
    "over the byte cap": (
        "a" * (MAX_DECLARED_PATH_BYTES + 1),
        f"must not exceed {MAX_DECLARED_PATH_BYTES} UTF-8 bytes",
    ),
    "over the segment cap": (
        "/".join(["a"] * (MAX_GLOB_SEGMENTS + 1)),
        f"must not exceed {MAX_GLOB_SEGMENTS} path segments",
    ),
}

#: The two classes whose declaration is long enough that the bounded diagnostic
#: truncates the reason out of the generation error. The verdict still matches;
#: only the explanation is elided, and that is pinned rather than excused.
TRUNCATED_REASON_CLASSES = frozenset({"over the byte cap", "over the segment cap"})

#: What the normalizer accepts, and what it turns each spelling into.
DECLARED_PATH_ACCEPTED = {
    "plain file": ("sub/api.json", "sub/api.json"),
    "directory literal": ("sub", "sub"),
    "trailing slash": ("sub/", "sub"),
    "dot file": (".hidden", ".hidden"),
    "dot inside a name": ("a/.b/c", "a/.b/c"),
    "at the segment cap": (
        "/".join(["a"] * MAX_GLOB_SEGMENTS),
        "/".join(["a"] * MAX_GLOB_SEGMENTS),
    ),
}


def _declaration_repository() -> Scenario:
    scene = Scenario()
    scene.component("svc", path="svc", provider="path-hash", boundary=["api.json"])
    scene.file("svc/api.json", '{"a": 1}\n')
    scene.file("svc/sub/api.json", '{"a": 2}\n')
    scene.commit()
    return scene


class DeclaredPathGrammarTests(unittest.TestCase):
    """OBL-GLOBS-043: one grammar, enforced the same way in both places."""

    def test_every_rejection_class_raises_its_stated_message(self):
        for name, (declared, message) in DECLARED_PATH_REJECTIONS.items():
            with self.subTest(rejection=name):
                with self.assertRaises(ValueError) as caught:
                    _normalize_declared_path(declared)
                self.assertEqual(str(caught.exception), message)

    def test_a_non_string_declaration_is_rejected_before_anything_else(self):
        for value in (None, 5, ["a"], b"a"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError) as caught:
                    _normalize_declared_path(value)
                self.assertEqual(str(caught.exception), "must be a string")

    def test_every_accepted_spelling_normalizes_to_its_stated_form(self):
        for name, (declared, expected) in DECLARED_PATH_ACCEPTED.items():
            with self.subTest(accepted=name):
                self.assertEqual(_normalize_declared_path(declared), expected)

    def test_a_valid_declaration_passes_both_call_sites(self):
        """The premise for the identity table: both sites accept good input."""
        with _declaration_repository() as scene:
            scene.config["components"]["svc"]["boundary"]["paths"] = [
                "sub/api.json"
            ]
            self.assertEqual(validate_config(scene.config, scene.root), [])
            lock = scene.generate(source="working-tree")
        component = lock["components"]["svc"]
        self.assertEqual(component["boundary_status"], "ok")
        self.assertIsNotNone(component["fingerprints"]["boundary"])

    def test_every_rejection_class_is_refused_at_both_call_sites(self):
        """The identity claim: a declaration cannot mean two things.

        `validate_config` reaches `_config._validate_component_path_entries`
        and `generate_lockfile` reaches `providers._select_declared_paths`.
        Both funnel through the same normalizer, and this asserts they agree
        on the verdict, and on the reason wherever the reason survives the
        bounded diagnostic.
        """
        with _declaration_repository() as scene:
            for name, (declared, message) in DECLARED_PATH_REJECTIONS.items():
                with self.subTest(rejection=name):
                    scene.config["components"]["svc"]["boundary"]["paths"] = [
                        declared
                    ]
                    errors = validate_config(scene.config, scene.root)
                    self.assertTrue(errors, "config validation accepted it")
                    with self.assertRaises(ConfigError) as caught:
                        scene.generate(source="working-tree")
                    generation = str(caught.exception)
                    self.assertIn("Invalid declared boundary path", generation)
                    self.assertTrue(
                        any(message in error for error in errors),
                        f"validate_config never said {message!r}: {errors}",
                    )
                    truncated = name in TRUNCATED_REASON_CLASSES
                    self.assertEqual(
                        message not in generation,
                        truncated,
                        f"reason visibility changed for {name}",
                    )

    def test_a_doubled_trailing_slash_is_rejected_like_any_empty_segment(self):
        with self.assertRaises(ValueError):
            _normalize_declared_path("sub//")

    def test_exactly_one_trailing_slash_normalizes_to_the_directory(self):
        self.assertEqual(_normalize_declared_path("sub/"), "sub")
        for declared in ("sub//", "sub///"):
            with self.subTest(declared=declared), self.assertRaises(ValueError):
                _normalize_declared_path(declared)

    def test_the_config_schema_and_hasher_both_refuse_the_spelling(self):
        with _declaration_repository() as scene:
            scene.config["components"]["svc"]["boundary"]["paths"] = ["sub"]
            plain = scene.generate(source="working-tree")
            self.assertEqual(validate_config(scene.config, scene.root), [])

            scene.config["components"]["svc"]["boundary"]["paths"] = ["sub//"]
            errors = validate_config(scene.config, scene.root)
            with self.assertRaises(ConfigError):
                scene.generate(source="working-tree")

            scene.config["components"]["svc"]["boundary"]["paths"] = ["api.json"]
            other = scene.generate(source="working-tree")

        self.assertTrue(
            any("components.svc.boundary.paths.0" in error for error in errors),
            f"config validation accepted 'sub//': {errors}",
        )
        self.assertNotEqual(plain["config_digest"], other["config_digest"])
        self.assertNotEqual(
            plain["components"]["svc"]["fingerprints"]["boundary"],
            other["components"]["svc"]["fingerprints"]["boundary"],
        )


# ---------------------------------------------------------------------------
# OBL-GLOBS-064: register_provider reads `name` twice
# ---------------------------------------------------------------------------


class _NameSequenceProvider:
    """A provider whose `name` answers a different string on each read."""

    version = "1.0.0"

    def __init__(self, *names: object) -> None:
        self._names = list(names)
        self.reads = 0

    @property
    def name(self) -> object:
        value = self._names[min(self.reads, len(self._names) - 1)]
        self.reads += 1
        return value

    def resolve(self, boundary_cfg, ctx):  # pragma: no cover - never resolved
        return ResolvedBoundary(entries=[])


class _RaisingNameProvider:
    """A provider whose `name` cannot be read at all."""

    version = "1.0.0"

    @property
    def name(self) -> str:
        raise RuntimeError("boom")

    def resolve(self, boundary_cfg, ctx):  # pragma: no cover - never resolved
        return ResolvedBoundary(entries=[])


class _StableProvider:
    """An ordinary well-formed extension."""

    name = "custom.stable"
    version = "1.0.0"

    def resolve(self, boundary_cfg, ctx):  # pragma: no cover - never resolved
        return ResolvedBoundary(entries=[])


class ProviderRegistryIdentityTests(unittest.TestCase):
    """OBL-GLOBS-064: the key written must be the string that was checked."""

    def setUp(self):
        self.global_registry = dict(providers._REGISTRY)
        self.registry = create_registry()
        self.before = dict(self.registry)

    def tearDown(self):
        providers._REGISTRY.clear()
        providers._REGISTRY.update(self.global_registry)

    def test_a_well_formed_provider_is_added_under_its_own_name(self):
        """The premise for every unchanged-registry assertion below."""
        register_provider(_StableProvider(), registry=self.registry)
        self.assertEqual(
            set(self.registry) - set(self.before), {"custom.stable"}
        )
        self.assertNotEqual(self.registry, self.before)
        self.assertIsInstance(
            get_provider("custom.stable", self.registry), _StableProvider
        )

    def test_registering_into_a_registry_leaves_the_process_global_alone(self):
        register_provider(_StableProvider(), registry=self.registry)
        self.assertNotIn("custom.stable", providers._REGISTRY)
        self.assertEqual(dict(providers._REGISTRY), self.global_registry)

    def test_a_non_string_name_is_refused(self):
        hostile = _NameSequenceProvider(5)
        with self.assertRaises(ProviderError) as caught:
            register_provider(hostile, registry=self.registry)
        self.assertEqual(
            str(caught.exception),
            "Cannot register boundary provider: Provider name must be a "
            "non-empty string",
        )
        self.assertEqual(self.registry, self.before)

    def test_a_name_that_raises_on_access_is_refused(self):
        with self.assertRaises(ProviderError) as caught:
            register_provider(_RaisingNameProvider(), registry=self.registry)
        self.assertEqual(
            str(caught.exception),
            "Cannot register boundary provider: Provider attribute 'name' "
            "could not be read: boom",
        )
        self.assertEqual(self.registry, self.before)

    def test_a_blank_or_padded_name_is_refused(self):
        for name, value, expected in (
            ("empty", "", "Provider name must be a non-empty string"),
            ("blank", "   ", "Provider name must be a non-empty string"),
            (
                "padded",
                " custom.x ",
                "Provider name must not have leading or trailing whitespace",
            ),
            (
                "oversized",
                "custom." + "x" * 300,
                "Provider name exceeds the 256-byte limit",
            ),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ProviderError) as caught:
                    register_provider(
                        _NameSequenceProvider(value), registry=self.registry
                    )
                self.assertEqual(
                    str(caught.exception),
                    f"Cannot register boundary provider: {expected}",
                )
                self.assertEqual(self.registry, self.before)

    def test_the_name_attribute_is_captured_once(self):
        hostile = _NameSequenceProvider("custom.ok", "custom.ok")
        register_provider(hostile, registry=self.registry)
        self.assertEqual(hostile.reads, 1)

    def test_a_later_name_value_cannot_change_the_registration_key(self):
        hostile = _NameSequenceProvider("custom.ok", "path-hash")
        register_provider(hostile, registry=self.registry)
        self.assertIs(self.registry["custom.ok"], hostile)
        self.assertIs(self.registry["path-hash"], self.before["path-hash"])

    def test_the_builtin_registry_entry_is_unchanged_after_a_mutating_name(self):
        register_provider(
            _NameSequenceProvider("custom.ok", "path-hash"),
            registry=self.registry,
        )
        self.assertIs(self.registry["path-hash"], self.before["path-hash"])

    def test_a_mutating_name_is_filed_under_the_captured_read(self):
        hostile = _NameSequenceProvider("custom.ok", "path-hash")
        register_provider(hostile, registry=self.registry)
        self.assertIs(self.registry["custom.ok"], hostile)
        self.assertIs(get_provider("custom.ok", self.registry), hostile)
        self.assertIs(self.registry["path-hash"], self.before["path-hash"])
        self.assertEqual(set(self.registry), set(self.before) | {"custom.ok"})

    def test_a_mutating_name_reaches_the_process_global_registry(self):
        """The pin for the no-registry call, restored in tearDown."""
        hostile = _NameSequenceProvider("custom.ok", "path-hash")
        register_provider(hostile)
        self.assertIs(providers._REGISTRY["custom.ok"], hostile)
        self.assertIs(
            providers._REGISTRY["path-hash"], self.global_registry["path-hash"]
        )

    def test_the_global_registry_is_restored_between_tests(self):
        """Isolation, asserted rather than assumed."""
        self.assertEqual(
            providers._REGISTRY["path-hash"].name, "path-hash"
        )
        self.assertNotIsInstance(
            providers._REGISTRY["path-hash"], _NameSequenceProvider
        )


# ---------------------------------------------------------------------------
# OBL-GLOBS-065: what `init --out` refuses, writes and creates
# ---------------------------------------------------------------------------

#: The three outcomes `--out` produces today, one shape each. "refused" names
#: the exact stderr line; "written" and "created" say what appeared on disk.
INIT_OUT_SHAPES: Dict[str, Tuple[str, str]] = {
    "sibling filename": ("custom.config.json", "written"),
    "nested under missing directories": ("a/b/boundary.config.json", "created"),
    "parent traversal": ("../outside.json", "refused"),
    "non-json suffix": ("boundary.config.yaml", "refused"),
    "nested non-json suffix": ("c/d/boundary.config.yaml", "refused"),
}

INIT_REFUSAL_MESSAGES = {
    "../outside.json": "Output path must not contain parent-directory traversal",
    "boundary.config.yaml": (
        "`boundver init` only writes JSON configs. Use boundary.config.json "
        "or edit your YAML/TOML config directly."
    ),
    "c/d/boundary.config.yaml": (
        "`boundver init` only writes JSON configs. Use boundary.config.json "
        "or edit your YAML/TOML config directly."
    ),
}


def _init_repository() -> Scenario:
    """A committed repository with a `src/` tree and no config of its own."""
    scene = Scenario()
    scene.file("src/main.py", "x\n")
    scene.git("add", "--all")
    scene.git("commit", "-m", "init")
    return scene


def _undiscoverable_repository() -> Scenario:
    """A committed repository `--discover` can find no component in."""
    scene = Scenario()
    scene.file("notes.txt", "x\n")
    scene.git("add", "--all")
    scene.git("commit", "-m", "init")
    return scene


class InitOutContainmentTests(unittest.TestCase):
    """OBL-GLOBS-065: refuse, write or create, stated per shape."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="boundver-outside-")
        self.addCleanup(directory.cleanup)
        self.outside = Path(directory.name)

    def test_each_out_shape_produces_its_stated_outcome(self):
        for name, (out, outcome) in INIT_OUT_SHAPES.items():
            with self.subTest(shape=name):
                with _init_repository() as scene:
                    before = (scene.root / out).exists()
                    result = run_cli(scene.root, "init", "--out", out)
                    after = (scene.root / out).exists()
                    created = (scene.root / "a" / "b").is_dir()
                if outcome == "refused":
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(INIT_REFUSAL_MESSAGES[out], result.stderr)
                    self.assertEqual(after, before, "a refusal wrote something")
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("component(s).", result.stdout)
                    self.assertTrue(after)
                self.assertEqual(created, outcome == "created")

    def test_a_refused_out_creates_no_intermediate_directory(self):
        """The premise for the mkdir pin: refusal happens before creation."""
        with _init_repository() as scene:
            result = run_cli(
                scene.root, "init", "--out", "c/d/boundary.config.yaml"
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse((scene.root / "c").exists())

    def test_missing_intermediate_directories_are_created_silently(self):
        """The pin: `--out` is a documented file path, and also a mkdir -p."""
        with _init_repository() as scene:
            self.assertFalse((scene.root / "a").exists())
            result = run_cli(
                scene.root, "init", "--out", "a/b/boundary.config.json"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((scene.root / "a" / "b").is_dir())
            self.assertTrue(
                (scene.root / "a" / "b" / "boundary.config.json").is_file()
            )
        # "Silently" is the whole point: nothing on either stream mentions a
        # directory, so the two lines below are the entire user-visible report.
        self.assertEqual(result.stderr, "")
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2, result.stdout)
        self.assertTrue(lines[0].startswith("Created "), lines[0])
        self.assertEqual(
            lines[1],
            "Next: review the config, then run `boundver validate-config` "
            "and `boundver generate`.",
        )

    def test_the_json_only_guard_runs_before_discovery(self):
        """`_ensure_json_mutation_path` is the first thing `init` decides.

        The premise is the second half: with a `.json` suffix the same
        repository gets as far as discovery and fails there instead, so the
        first refusal really did pre-empt a reachable later one.
        """
        with _undiscoverable_repository() as scene:
            refused = run_cli(scene.root, "init", "--discover", "--out", "x.yaml")
            reached = run_cli(scene.root, "init", "--discover", "--out", "x.json")
            self.assertFalse((scene.root / "x.json").exists())
        self.assertEqual(refused.returncode, 2)
        self.assertIn(
            "`boundver init` only writes JSON configs.", refused.stderr
        )
        self.assertEqual(reached.returncode, 2)
        self.assertIn(
            "No tracked component could be discovered.", reached.stderr
        )

    def test_a_non_directory_ancestor_is_refused(self):
        with _init_repository() as scene:
            scene.file("a", "not a directory\n")
            result = run_cli(scene.root, "init", "--out", "a/b/x.json")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(
                "Output path must not traverse or create through a symlink, "
                "junction, reparse point, or non-directory ancestor",
                result.stderr,
            )

    def test_an_absolute_out_outside_the_repository_is_refused(self):
        """The CLI enforces the documented repository-relative output path."""
        target = str(self.outside / "abs.json").replace("\\", "/")
        with _init_repository() as scene:
            result = run_cli(scene.root, "init", "--out", target)
        self.assertEqual(result.returncode, 2, result.stdout)

    def test_an_absolute_out_leaves_no_file_outside_the_repository(self):
        target = str(self.outside / "abs.json").replace("\\", "/")
        with _init_repository() as scene:
            result = run_cli(scene.root, "init", "--out", target)
            self.assertFalse((scene.root / "boundary.config.json").exists())
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("must be relative to the repository root", result.stderr)
        self.assertFalse((self.outside / "abs.json").exists())

    def test_a_refused_outside_config_leaves_the_repository_unconfigured(self):
        target = str(self.outside / "invisible.json").replace("\\", "/")
        with _init_repository() as scene:
            self.assertEqual(run_cli(scene.root, "init", "--out", target).returncode, 2)
            follow_up = run_cli(scene.root, "validate-config")
        self.assertFalse((self.outside / "invisible.json").exists())
        self.assertEqual(follow_up.returncode, 2)
        self.assertIn("Config file not found", follow_up.stderr)
        self.assertIn("boundary.config.json", follow_up.stderr)

    def test_a_config_written_inside_is_found_by_the_next_command(self):
        """The premise for the invisibility assertion above."""
        with _init_repository() as scene:
            self.assertEqual(
                run_cli(scene.root, "init", "--out", "boundary.config.json").returncode,
                0,
            )
            follow_up = run_cli(scene.root, "validate-config")
        self.assertEqual(follow_up.returncode, 0, follow_up.stderr)
        self.assertIn("Config is valid.", follow_up.stdout)

    def test_the_baseline_sibling_refuses_what_init_accepts(self):
        """The contrast the obligation asks for, in one place."""
        target = str(self.outside / "abs.json").replace("\\", "/")
        with _init_repository() as scene:
            inside = _resolve_baseline_path(scene.root, "sub/base.json")
            self.assertEqual(inside.name, "base.json")
            for raw in (target, "../outside.json"):
                with self.subTest(path=raw[:40]):
                    with self.assertRaises(BaselineError) as caught:
                        _resolve_baseline_path(scene.root, raw)
                    self.assertEqual(
                        str(caught.exception),
                        "verification baseline paths must stay within the "
                        "repository",
                    )
            with self.assertRaises(BaselineError) as caught:
                _resolve_baseline_path(scene.root, "sub/base.yaml")
            self.assertEqual(
                str(caught.exception),
                "verification baseline path must end in .json",
            )


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
