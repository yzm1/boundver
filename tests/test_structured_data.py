from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import boundver._structured_data as structured_data
from boundver._structured_data import (
    StrictJSONError,
    _reject_excessive_json_tokens,
    strict_json_loads,
)

# The two characters JSON spells a single backslash with. Writing them through
# chr() keeps the fixtures below readable, because a literal would need four
# backslashes in the Python source and would invite a miscount while reading.
_ESCAPED_BACKSLASH = chr(92) * 2


class StrictJSONTests(unittest.TestCase):
    def test_accepts_regular_json(self):
        self.assertEqual(strict_json_loads('{"a": [1, true, null]}'), {"a": [1, True, None]})

    def test_rejects_duplicate_keys_and_nonfinite_numbers(self):
        for payload in ('{"a": 1, "a": 2}', '{"a": NaN}'):
            with self.subTest(payload=payload), self.assertRaises(StrictJSONError):
                strict_json_loads(payload)

    def test_rejects_cross_version_oversized_integers(self):
        with self.assertRaisesRegex(StrictJSONError, "decimal-digit limit"):
            strict_json_loads('{"n": ' + "1" * 4301 + "}")

    def test_rejects_oversized_or_nonfinite_float_tokens(self):
        oversized = "1." + ("0" * 4_400)
        for payload, message in (
            ('{"n": ' + oversized + "}", "character limit"),
            ('{"n": 1e9999}', "non-finite"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                StrictJSONError, message
            ):
                strict_json_loads(payload)

    def test_rejects_provably_wide_tree_before_json_parser_allocation(self):
        with (
            patch.object(structured_data, "MAX_JSON_TREE_NODES", 2),
            patch.object(
                structured_data.json,
                "loads",
                side_effect=AssertionError("parser must not be called"),
            ) as loads,
            self.assertRaisesRegex(StrictJSONError, "pre-parse structural"),
        ):
            strict_json_loads("[0,0,0,0,0]")
        loads.assert_not_called()

    def test_rejects_deep_tree_before_json_parser_allocation(self):
        with (
            patch.object(structured_data, "MAX_JSON_TREE_DEPTH", 1),
            patch.object(
                structured_data.json,
                "loads",
                side_effect=AssertionError("parser must not be called"),
            ) as loads,
            self.assertRaisesRegex(StrictJSONError, "pre-parse structural depth"),
        ):
            strict_json_loads("[[[0]]]")
        loads.assert_not_called()

    def test_rejects_duplicate_key_when_both_copies_carry_the_same_value(self):
        """The loader rejects a repeated object key even when both copies agree.

        Every other duplicate-key payload in this repository pairs two
        different values, so nothing distinguished "reject duplicate keys"
        from the weaker "reject conflicting duplicate keys". Mutant
        MUT-PROVIDERS-309 makes exactly that weakening in
        ``_unique_json_object`` and the rest of the suite stays green, because
        the weakened guard quietly returns {"a": 1} for this payload instead
        of raising. A document that names one key twice is ambiguous no matter
        which copy a reader happens to keep, so the repetition itself is the
        error and the agreement between the values is beside the point.
        """
        with self.assertRaisesRegex(
            StrictJSONError, "duplicate JSON object key 'a'"
        ):
            strict_json_loads('{"a": 1, "a": 1}')

    def test_same_value_duplicate_payload_really_repeats_one_key(self):
        """Premise: that payload does repeat one key with two equal values.

        The rejection above only pins the weakened guard if its payload really
        carries the key twice with values a permissive parser would treat as
        identical. Reading the raw member list with the standard library shows
        both things at once: the document is otherwise ordinary JSON, and it
        hands the parser two ("a", 1) pairs rather than a conflicting pair.
        """
        pairs = json.loads('{"a": 1, "a": 1}', object_pairs_hook=list)
        self.assertEqual(pairs, [("a", 1), ("a", 1)])
        self.assertEqual(pairs[0][1], pairs[1][1])

    def test_accepts_distinct_keys_that_share_one_value(self):
        """Contrast: repeating a value under distinct keys is still accepted.

        A loader that refused every document mentioning the same value twice
        would satisfy the rejection test without being correct, so this pins
        the ordinary case that has to keep parsing.
        """
        self.assertEqual(
            strict_json_loads('{"a": 1, "b": 1}'), {"a": 1, "b": 1}
        )


class EscapedBackslashTokenScannerTests(unittest.TestCase):
    """Assert the token counts the pre-parse scanner produces for JSON strings
    that end in an escaped backslash.

    The mutant MUT-OUTPUT-302 leaves the scanner's escape flag raised when the
    escaped character is itself a backslash. The scanner then swallows the
    closing quote of such a string, and for the rest of the document it reads
    the regions that are inside a string as outside one and the regions that are
    outside as inside. Every other test in this file misses that, because they
    ask only whether the scanner refuses a document, and they ask it about
    documents whose token counts sit nowhere near the ceiling. Any scanner that
    keeps a ceiling somewhere satisfies them, however badly it has lost its
    place. These tests assert the counts themselves, and they assert that a
    large document the standard parser accepts still passes the real ceiling.
    """

    # Each document is paired with the number of tokens the shipped scanner
    # counts for it. Under the mutant the first two documents count 5 and 2,
    # because the swallowed quote hides the tokens that follow it, and the third
    # counts 9, because the swapped regions expose string bodies as bare atoms.
    # The fourth document counts 3 either way and is here to show that the
    # smallest shape of the fixture is counted correctly as well.
    _COUNTED_DOCUMENTS = (
        ('{"k": "a' + _ESCAPED_BACKSLASH + '", "v": [1,2,3]}', 8),
        ('["' + _ESCAPED_BACKSLASH + '", 1, 2, 3, 4, 5]', 7),
        ('{"a": "x' + _ESCAPED_BACKSLASH + '", "b": "y", "c": 1}', 7),
        ('{"k": "' + _ESCAPED_BACKSLASH + '"}', 3),
    )

    @staticmethod
    def _wide_document() -> str:
        """Build a large document in which every object carries an escaped backslash.

        The shipped scanner counts 154,001 tokens for it, which sits under the
        200,000 the real ceiling allows, so the document is accepted without any
        patching. The mutant counts 220,000 for the same text and refuses a
        document that ``json.loads`` parses without complaint.
        """
        unit = '{"a": "x' + _ESCAPED_BACKSLASH + '", "b": "y", "c": 1}'
        return "[" + ",".join([unit] * 22_000) + "]"

    @staticmethod
    def _escape_free_twin() -> str:
        """Build the same document without the escapes, which the scanner counts alike."""
        unit = '{"a": "x", "b": "y", "c": 1}'
        return "[" + ",".join([unit] * 22_000) + "]"

    def test_premise_every_fixture_ends_a_string_with_an_escaped_backslash(self):
        """Assert the fixtures really carry the escape the counts depend on.

        A document without an escaped backslash is counted the same by a correct
        scanner and by a scanner that mishandles the escape, so the count
        assertions below would hold vacuously on such a document. Assert instead
        that each fixture closes a string immediately after an escaped
        backslash, and that ``json.loads`` reads that escape as one backslash.
        """
        for document, _ in self._COUNTED_DOCUMENTS:
            with self.subTest(document=document):
                self.assertIn(_ESCAPED_BACKSLASH + '"', document)
                parsed = json.loads(document)
                values = parsed.values() if isinstance(parsed, dict) else parsed
                escaped = [
                    value
                    for value in values
                    if isinstance(value, str) and value.endswith(chr(92))
                ]
                self.assertEqual(len(escaped), 1)
                self.assertNotIn(_ESCAPED_BACKSLASH, escaped[0])

        wide = self._wide_document()
        self.assertEqual(len(json.loads(wide)), 22_000)
        self.assertEqual(wide.count(_ESCAPED_BACKSLASH + '"'), 22_000)

    def test_scanner_counts_the_tokens_a_document_with_an_escaped_backslash_has(self):
        """Assert the exact token count by bracketing it with two ceilings.

        The scanner refuses once the count passes twice ``MAX_JSON_TREE_NODES``,
        so a ceiling just under half the count has to refuse the document and a
        ceiling at half the count has to accept it. A scanner that miscounts a
        string ending in an escaped backslash fails one side of that bracket.
        """
        for document, count in self._COUNTED_DOCUMENTS:
            with self.subTest(document=document, count=count):
                with patch.object(
                    structured_data, "MAX_JSON_TREE_NODES", (count - 1) // 2
                ):
                    with self.assertRaisesRegex(
                        StrictJSONError, "pre-parse structural token"
                    ):
                        _reject_excessive_json_tokens(document)
                with patch.object(
                    structured_data, "MAX_JSON_TREE_NODES", (count + 1) // 2
                ):
                    _reject_excessive_json_tokens(document)

    def test_large_document_of_escaped_backslashes_survives_the_real_ceiling(self):
        """Assert the scanner does not refuse a parseable document at production settings.

        This is the behavioural half of the contract, taken through the public
        entry point with no ceiling patched. A scanner that loses its place
        after an escaped backslash counts this document over the limit and
        rejects 682 KB of perfectly ordinary JSON.
        """
        document = self._wide_document()
        self.assertEqual(strict_json_loads(document), json.loads(document))

    def test_contrast_the_ceiling_still_refuses_a_document_that_is_too_wide(self):
        """Assert the acceptance above comes from the count, not from a toothless guard.

        A scanner that refused nothing at all would also pass the test above, so
        assert that the same document is refused as soon as the ceiling drops
        below its token count. Assert as well that the escape-free twin of the
        document is accepted at the real ceiling, which is the ordinary case the
        escaped document has to match.
        """
        document = self._wide_document()
        with patch.object(structured_data, "MAX_JSON_TREE_NODES", 1_000):
            with self.assertRaisesRegex(
                StrictJSONError, "pre-parse structural token"
            ):
                strict_json_loads(document)

        twin = self._escape_free_twin()
        self.assertEqual(strict_json_loads(twin), json.loads(twin))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
