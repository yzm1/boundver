"""What reaches a terminal, and whether a bound survives the trip.

Repository-controlled text ends up in a reader's terminal and in a CI log.
Escaping it is not decoration: a name is chosen by whoever can commit, and a
reader has to be able to tell what they are looking at. Two things here do not
hold, and both are about a transformation applied after the check that was
supposed to make it safe.

Covers OBL-OUTPUT-002, OBL-OUTPUT-004, OBL-OUTPUT-006, OBL-OUTPUT-007 and
OBL-OUTPUT-010.
"""

from __future__ import annotations

import unittest

import boundver._review_plan as review_plan
from boundver._hashing import _u64
from boundver._output import _display_text
from boundver._review_plan import MAX_PLAN_SUMMARY_FIELD_BYTES

from tests._parity import run_cli
from tests._scenarios import Scenario

BACKSLASH = chr(92)
ESC = chr(27)

#: Formatting codepoints that reorder or hide text without being visible.
BIDI_AND_INVISIBLE = (
    ("LRE", 0x202A), ("RLE", 0x202B), ("PDF", 0x202C),
    ("LRO", 0x202D), ("RLO", 0x202E),
    ("LRI", 0x2066), ("RLI", 0x2067), ("FSI", 0x2068), ("PDI", 0x2069),
    ("ZWSP", 0x200B), ("ZWNJ", 0x200C), ("ZWJ", 0x200D),
    ("LRM", 0x200E), ("RLM", 0x200F), ("BOM", 0xFEFF),
)


class HashFrameLengthTests(unittest.TestCase):
    """OBL-OUTPUT-004: the wire format's length prefix."""

    def test_the_boundaries_encode_as_eight_big_endian_bytes(self):
        self.assertEqual(_u64(0), bytes(8))
        self.assertEqual(_u64(1), bytes(7) + bytes([1]))
        self.assertEqual(_u64(2**64 - 1), bytes([0xFF]) * 8)
        self.assertEqual(_u64(256), bytes(6) + bytes([1, 0]))

    def test_out_of_range_lengths_are_refused_by_value(self):
        for value in (-1, 2**64, 2**70):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    _u64(value)
                self.assertIn(str(value), str(caught.exception))


class DisplayTextEscapingTests(unittest.TestCase):
    """OBL-OUTPUT-002 and OBL-OUTPUT-007: what a reader can tell apart."""

    def test_the_escapes_it_does_apply(self):
        for label, character, expected in (
            ("newline", "\n", BACKSLASH + "n"),
            ("carriage return", "\r", BACKSLASH + "r"),
            ("tab", "\t", BACKSLASH + "t"),
            ("escape", ESC, BACKSLASH + "x1b"),
            ("delete", chr(0x7F), BACKSLASH + "x7f"),
            ("line separator", chr(0x2028), BACKSLASH + "u2028"),
            ("paragraph separator", chr(0x2029), BACKSLASH + "u2029"),
        ):
            with self.subTest(character=label):
                self.assertEqual(_display_text("a" + character + "b"),
                                 "a" + expected + "b")

    def test_a_leading_workflow_command_is_defanged(self):
        """A value at the start of a CI log line must not become a directive."""
        self.assertTrue(_display_text("::error::x").startswith(BACKSLASH + "x3a"))

    def test_the_rendering_is_injective(self):
        """Known divergence: a literal backslash is not escaped.

        A real escape character renders as the six characters `\\x1b`, and a
        name literally containing those six characters renders identically. A
        reader cannot tell a defanged control sequence from a name that merely
        looks like one, which is the property escaping exists to provide.
        """
        real = "a" + ESC + "[2Jb"
        literal = "a" + BACKSLASH + "x1b[2Jb"
        self.assertNotEqual(real, literal)
        self.assertNotEqual(_display_text(real), _display_text(literal))

    def test_a_literal_backslash_is_escaped(self):
        real = "a" + ESC + "[2Jb"
        literal = "a" + BACKSLASH + "x1b[2Jb"
        self.assertEqual(_display_text(real), "a\\x1b[2Jb")
        self.assertEqual(_display_text(literal), "a\\\\x1b[2Jb")
        self.assertEqual(_display_text(BACKSLASH), BACKSLASH * 2)

    def test_bidirectional_and_invisible_codepoints_are_escaped(self):
        """Known divergence: every one passes through unchanged.

        These reorder or hide neighbouring text without being visible
        themselves, so a component name can render as something it is not.
        The obligation allows documenting the pass-through instead; nothing
        documents it.
        """
        for label, codepoint in BIDI_AND_INVISIBLE:
            character = chr(codepoint)
            self.assertNotIn(character, _display_text("a" + character + "b"), label)

    def test_every_formatting_control_has_an_explicit_escape(self):
        for label, codepoint in BIDI_AND_INVISIBLE:
            with self.subTest(codepoint=label):
                character = chr(codepoint)
                self.assertEqual(
                    _display_text("a" + character + "b"),
                    f"a\\u{codepoint:04x}b",
                )


#: Characters spanning every UTF-8 width, with the bytes each one costs. The
#: escape-expansion tests below are calibrated for ASCII and keep their own
#: tuple; this table exists so the byte bound is asserted against widths where
#: a per-character charge and a per-byte charge give different answers.
FIELD_CHARACTERS = (
    ("a", 1),
    ("\u00e9", 2),
    ("\u4e00", 3),
    ("\U0001f600", 4),
)


class SummaryFieldBoundTests(unittest.TestCase):
    """OBL-OUTPUT-010: a bound must hold for the bytes actually emitted."""

    def _rendered_size(self, character: str) -> tuple:
        raw = character * (MAX_PLAN_SUMMARY_FIELD_BYTES + 50)
        bounded, _truncated = review_plan._bounded_text(
            raw, MAX_PLAN_SUMMARY_FIELD_BYTES
        )
        rendered, _flag = review_plan._code(raw)
        return len(bounded.encode("utf-8")), len(rendered.encode("utf-8"))

    def test_the_truncation_step_respects_the_bound(self):
        """The half that works: _bounded_text stops at the cap."""
        for character in ("a", "'", "<", "&", '"'):
            with self.subTest(character=character):
                bounded, _rendered = self._rendered_size(character)
                self.assertLessEqual(bounded, MAX_PLAN_SUMMARY_FIELD_BYTES)

    def test_the_emitted_field_respects_the_bound(self):
        """Known divergence: html escaping runs after the truncation.

        `_code` applies html.escape(quote=True) to already-bounded text, and
        an apostrophe becomes six bytes. A field declared bounded at 512
        emits 3070.
        """
        for character in ("a", "'", "<", "&", '"'):
            _bounded, rendered = self._rendered_size(character)
            self.assertLessEqual(rendered, MAX_PLAN_SUMMARY_FIELD_BYTES, character)

    def test_the_bound_is_counted_in_bytes_not_characters(self):
        """The declared bound is a byte budget, so a wide character costs more.

        Every character in the tuple above is one UTF-8 byte, so a truncation
        loop that charged one per character would satisfy that test exactly as
        the shipped one does. MUT-OUTPUT-304 makes that change and nothing
        noticed: a 512-byte cap silently became a 512-character cap, emitting
        1021, 1530 and 2039 bytes for two-, three- and four-byte characters.
        """
        for character, _cost in FIELD_CHARACTERS:
            with self.subTest(character=character):
                bounded, _rendered = self._rendered_size(character)
                self.assertLessEqual(bounded, MAX_PLAN_SUMMARY_FIELD_BYTES)

    def test_the_character_table_really_spans_the_utf8_widths(self):
        """The premise: without widths above one byte the test above is blind.

        It states what the table has to be for the byte bound to mean
        anything. Each character must cost the bytes claimed, must survive
        _display_text unescaped so its width reaches the truncation loop
        intact, and must overflow the bound before truncation begins.
        """
        self.assertEqual({cost for _character, cost in FIELD_CHARACTERS}, {1, 2, 3, 4})
        for character, cost in FIELD_CHARACTERS:
            with self.subTest(character=character):
                self.assertEqual(len(character.encode("utf-8")), cost)
                self.assertEqual(review_plan._display_text(character), character)
                raw = character * (MAX_PLAN_SUMMARY_FIELD_BYTES + 50)
                self.assertGreater(
                    len(raw.encode("utf-8")), MAX_PLAN_SUMMARY_FIELD_BYTES
                )

    def test_a_field_inside_the_bound_is_returned_whole(self):
        """The contrast: bounding is not truncating everything it is given."""
        for character, cost in FIELD_CHARACTERS:
            with self.subTest(character=character):
                fits = character * (MAX_PLAN_SUMMARY_FIELD_BYTES // (cost * 2))
                bounded, truncated = review_plan._bounded_text(
                    fits, MAX_PLAN_SUMMARY_FIELD_BYTES
                )
                self.assertEqual(bounded, fits)
                self.assertFalse(truncated)

    def test_escape_expansion_is_included_in_the_budget(self):
        for character in ("'", '"', "&", "<"):
            with self.subTest(character=character):
                bounded, rendered = self._rendered_size(character)
                self.assertLessEqual(rendered, MAX_PLAN_SUMMARY_FIELD_BYTES)
                self.assertLess(rendered, bounded * 6)


class NullVersionRenderingTests(unittest.TestCase):
    """OBL-OUTPUT-006: no human view may print the literal None."""

    def test_an_added_component_without_a_version_reads_as_prose(self):
        scene = Scenario()
        try:
            scene.component("svc", path="svc", boundary=["api"])
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.commit()
            run_cli(scene.root, "generate", "--source", "head")
            scene.commit("lock")
            base = scene.head()

            scene.component("added", path="added", provider="leaf")
            scene.file("added/x.py", "x = 1\n")
            scene.commit("add")
            run_cli(scene.root, "generate", "--source", "head")
            scene.commit("relock")

            for view in ("text", "plan"):
                with self.subTest(view=view):
                    result = run_cli(
                        scene.root, "review", "--base", base,
                        "--target", scene.head(), "--format", view,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("added", result.stdout)
                    self.assertNotIn("None", result.stdout)
        finally:
            scene.close()


if __name__ == "__main__":
    unittest.main()
