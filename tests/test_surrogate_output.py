"""What happens when an undecodable filename reaches the output layer.

Git tracks paths as bytes. Python reads them with surrogateescape, so a name
that is not valid UTF-8 arrives as a string carrying lone surrogates in
U+DC80-U+DCFF. Those are not characters; they are a way of remembering bytes
that had no character. Anything that has to produce real output has to deal
with them, and two places do not.

Both divergences are marked rather than fixed: see the expectedFailure tests.

Covers OBL-FACETS-004 and OBL-GRAPH-005.
"""

from __future__ import annotations

import json
import unittest

from boundver._output import _display_text
from boundver._utils import GuardrailError, _bounded_json_dumps

#: What surrogateescape produces for the byte 0x80.
LONE_LOW = chr(0xDC80)
LONE_HIGH = chr(0xD800)

#: Characters _display_text is already known to neutralize, for contrast.
NEUTRALIZED = (
    ("C0 control", chr(1)),
    ("line separator", " "),
    ("paragraph separator", " "),
    ("escape", chr(27)),
)


def _is_surrogate(character: str) -> bool:
    return 0xD800 <= ord(character) <= 0xDFFF


def _encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class DisplayTextTests(unittest.TestCase):
    """OBL-FACETS-004: neutralization must not depend on the destination.

    `_encoding_safe_text` is skipped when the stream has no `encoding`
    attribute, which an embedder's buffer often does not have, so
    `_display_text` has to handle this itself or the bytes never get written.
    """

    def test_the_characters_it_does_neutralize(self):
        """The contrast: it is not that the function does nothing."""
        for label, character in NEUTRALIZED:
            with self.subTest(character=label):
                rendered = _display_text("a" + character + "b")
                self.assertNotIn(character, rendered)
                self.assertTrue(_encodable(rendered))

    def test_ordinary_text_is_left_alone(self):
        self.assertEqual(_display_text("api/v1.yaml"), "api/v1.yaml")

    def test_a_lone_surrogate_is_neutralized(self):
        """Known divergence: it passes straight through.

        The result cannot be encoded to UTF-8, so whatever tries to write it
        raises rather than printing a diagnostic.
        """
        for surrogate in (LONE_LOW, LONE_HIGH):
            rendered = _display_text("a" + surrogate + "b")
            self.assertFalse(any(_is_surrogate(c) for c in rendered), repr(rendered))

    def test_the_surrogate_escape_is_utf8_encodable(self):
        for surrogate in (LONE_LOW, LONE_HIGH):
            with self.subTest(surrogate=hex(ord(surrogate))):
                rendered = _display_text("a" + surrogate + "b")
                self.assertNotIn(surrogate, rendered)
                self.assertIn(f"\\u{ord(surrogate):04x}", rendered)
                self.assertTrue(_encodable(rendered))


class JsonPortabilityTests(unittest.TestCase):
    """OBL-GRAPH-005: a JSON document must be readable outside Python.

    Python's own decoder accepts a bare `\\udcXX` escape and hands back the
    lone surrogate. A consumer that wants bytes cannot do anything with it,
    which is the whole point of emitting JSON rather than prose.
    """

    def _emit(self, value: dict) -> str:
        return _bounded_json_dumps(value, ensure_ascii=True, sort_keys=True)

    def test_ordinary_values_emit_portable_json(self):
        text = self._emit({"path": "api/v1.yaml", "unicode": "café"})
        self.assertTrue(_encodable(text))
        self.assertEqual(json.loads(text)["path"], "api/v1.yaml")

    def test_the_document_itself_is_ascii_and_encodable(self):
        """A non-portable value is refused before a JSON document is emitted."""
        with self.assertRaisesRegex(GuardrailError, "lone surrogate"):
            self._emit({"path": "a" + LONE_LOW + "b"})

    def test_a_decoded_value_can_be_encoded_by_a_consumer(self):
        """Known divergence: the escape decodes to something unencodable.

        A strict consumer reading this document gets a string it cannot turn
        back into UTF-8 bytes, so the path is unusable downstream.
        """
        with self.assertRaisesRegex(GuardrailError, "not portable"):
            self._emit({"path": "a" + LONE_LOW + "b"})

    def test_the_refusal_applies_without_an_output_byte_cap(self):
        with self.assertRaisesRegex(GuardrailError, "not portable"):
            self._emit({"path": "a" + LONE_HIGH + "b"})

    def test_a_paired_surrogate_is_not_affected(self):
        """A real astral character survives, so the gap is lone surrogates."""
        text = self._emit({"path": "a\U0001f600b"})
        self.assertTrue(_encodable(json.loads(text)["path"]))
        self.assertEqual(json.loads(text)["path"], "a\U0001f600b")


if __name__ == "__main__":
    unittest.main()
