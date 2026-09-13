"""A pre-scan that must not have an opinion of its own.

`strict_json_loads` runs a character-level scanner before `json.loads` so that a
provably oversized document is refused before the parser allocates a tree for
it. That makes the scanner a second, cruder JSON implementation sitting in
front of the real one, and the risk with a second implementation is that it
disagrees with the first: a document Python parses happily must never be
refused by the scanner that guards it.

The scanner's own state machine is where a disagreement would come from. It
tracks whether it is inside a string literal and whether the last character was
a backslash, so brackets, braces and colons inside strings, an escaped quote,
and an escaped backslash immediately before a closing quote are exactly the
inputs that would break it. `json.loads` is the oracle: whatever it accepts
within the published limits, the scanner must pass through.

Covers OBL-OUTPUT-020.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver import _structured_data
from boundver._structured_data import StrictJSONError, strict_json_loads
from boundver._utils import MAX_JSON_TREE_DEPTH, MAX_JSON_TREE_NODES

PROFILE = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

#: The scanner counts one token per value plus one per object key, so its
#: ceiling is twice the public value-node limit.
TOKEN_LIMIT = 2 * MAX_JSON_TREE_NODES

#: String payloads that look like structure to a scanner that loses its place.
CONFUSING = [
    "[",
    "]",
    "{",
    "}",
    ":",
    ",",
    "[{:,}]",
    '\\"',
    "\\\\",
    '\\"[',
    "\\\\\\\\",
    "ends with a backslash \\\\",
    "\\u005b\\u007b",
    "\\n\\t\\r",
    "  spaced  ",
    "",
]


def _nested(depth: int) -> str:
    return "[" * depth + "1" + "]" * depth


def _nested_objects(depth: int) -> str:
    return '{"a":' * depth + "1" + "}" * depth


class ScannerAgreementTests(unittest.TestCase):
    """OBL-OUTPUT-020: never refuse what the parser would have accepted."""

    def test_structure_inside_a_string_is_not_structure(self):
        for payload in CONFUSING:
            with self.subTest(payload=payload):
                text = json.dumps({"key": json.loads(f'"{payload}"')})
                self.assertEqual(strict_json_loads(text), json.loads(text))

    def test_a_string_full_of_brackets_does_not_count_as_depth(self):
        """The scanner would otherwise see a hundred thousand open brackets."""
        text = json.dumps({"k": "[" * (TOKEN_LIMIT + 10)})
        self.assertEqual(strict_json_loads(text), json.loads(text))

    def test_an_escaped_backslash_ends_the_string_where_python_does(self):
        """A literal ending in a backslash: the next quote really closes it."""
        text = '{"a": "x\\\\", "b": [1, 2]}'
        self.assertEqual(json.loads(text), {"a": "x\\", "b": [1, 2]})
        self.assertEqual(strict_json_loads(text), json.loads(text))

    def test_an_escaped_quote_does_not_end_the_string(self):
        text = '{"a": "x\\"},[{", "b": 1}'
        self.assertEqual(json.loads(text), {"a": 'x"},[{', "b": 1})
        self.assertEqual(strict_json_loads(text), json.loads(text))

    def test_whitespace_variants_are_all_accepted(self):
        value = {"a": [1, {"b": "c"}], "d": None}
        for label, text in (
            ("compact", json.dumps(value, separators=(",", ":"))),
            ("indented", json.dumps(value, indent=4)),
            ("tabs", json.dumps(value, indent="\t")),
            ("crlf", json.dumps(value, indent=2).replace("\n", "\r\n")),
            ("padded", "  \t\r\n" + json.dumps(value) + "\n\n  "),
        ):
            with self.subTest(whitespace=label):
                self.assertEqual(strict_json_loads(text), value)

    def test_nesting_at_exactly_the_depth_limit_is_accepted(self):
        for build in (_nested, _nested_objects):
            with self.subTest(shape=build.__name__):
                text = build(MAX_JSON_TREE_DEPTH)
                self.assertEqual(strict_json_loads(text), json.loads(text))

    def test_a_wide_document_just_under_the_token_ceiling_is_accepted(self):
        """Half the limit in values plus half in keys is exactly the ceiling."""
        value = {f"k{index}": index for index in range(MAX_JSON_TREE_NODES - 1)}
        self.assertEqual(strict_json_loads(json.dumps(value)), value)

    def test_a_scalar_at_the_top_level_is_accepted(self):
        for text in ('"x"', "1", "true", "false", "null", "-1.5e3"):
            with self.subTest(document=text):
                self.assertEqual(strict_json_loads(text), json.loads(text))


class ScannerRefusalTests(unittest.TestCase):
    """OBL-OUTPUT-020: and refuse the provably oversized before allocating."""

    def test_depth_past_the_limit_is_refused(self):
        with self.assertRaises(StrictJSONError) as raised:
            strict_json_loads(_nested(MAX_JSON_TREE_DEPTH + 8))
        self.assertIn("depth limit", str(raised.exception))

    def test_a_token_count_past_the_ceiling_is_refused(self):
        with self.assertRaises(StrictJSONError) as raised:
            strict_json_loads("[" + ",".join(["1"] * (TOKEN_LIMIT + 2)) + "]")
        self.assertIn("token limit", str(raised.exception))

    def test_the_refusal_happens_before_the_parser_runs(self):
        """The point of a pre-scan: no allocation for a hopeless document."""
        for text in (
            _nested(MAX_JSON_TREE_DEPTH + 8),
            "[" + ",".join(["1"] * (TOKEN_LIMIT + 2)) + "]",
        ):
            with self.subTest(document=text[:16]):
                with mock.patch.object(
                    _structured_data.json, "loads", side_effect=AssertionError
                ) as parser:
                    with self.assertRaises(StrictJSONError):
                        strict_json_loads(text)
                parser.assert_not_called()

    def test_an_accepted_document_does_reach_the_parser(self):
        """The premise: the patch above would have fired had it been reached."""
        with mock.patch.object(
            _structured_data.json, "loads", side_effect=AssertionError
        ):
            with self.assertRaises(AssertionError):
                strict_json_loads('{"a": 1}')

    def test_the_strict_rules_still_apply_after_the_scan(self):
        with self.assertRaises(StrictJSONError):
            strict_json_loads('{"a": 1, "a": 2}')
        with self.assertRaises(StrictJSONError):
            strict_json_loads("[NaN]")


#: Small JSON trees, deliberately dense in the characters that make a
#: character-level scanner lose its place.
_TEXT = st.text(
    alphabet=st.sampled_from(list('ab {}[]:,"\\\n\t') + ["é", "\U0001f600"]),
    max_size=12,
)
_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**6), max_value=10**6),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    _TEXT,
)
_TREES = st.recursive(
    _SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(_TEXT, children, max_size=4),
    ),
    max_leaves=12,
)


class ScannerPropertyTests(unittest.TestCase):
    """The agreement stated as a property rather than as a list."""

    @PROFILE
    @given(value=_TREES, indent=st.sampled_from([None, 0, 2, "\t"]))
    def test_anything_json_can_dump_survives_the_scan(self, value, indent):
        text = json.dumps(value, indent=indent)
        self.assertEqual(strict_json_loads(text), json.loads(text))

    @PROFILE
    @given(value=_TREES)
    def test_the_ascii_and_unicode_encodings_both_survive(self, value):
        for ensure_ascii in (True, False):
            text = json.dumps(value, ensure_ascii=ensure_ascii)
            self.assertEqual(strict_json_loads(text), json.loads(text))

    @PROFILE
    @given(payload=_TEXT)
    def test_a_string_never_makes_a_one_value_document_oversized(self, payload):
        text = json.dumps(payload)
        self.assertEqual(strict_json_loads(text), payload)


if __name__ == "__main__":
    unittest.main()
