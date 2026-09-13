"""Serialising, shortening, reading and printing, each against its own bound.

Four small contracts that only meet at the edges. A bounded serialiser must
agree with the unbounded one byte for byte until the bound is reached and then
refuse rather than truncate. A shortener must say it shortened only when it
did. A bounded reader must accept exactly its limit and distinguish a file
that was already too large from one that grew while being read. And a printer
must survive a stream that cannot represent what it was given, without letting
the escaping it does become a CI workflow command.

Covers OBL-OUTPUT-012, OBL-OUTPUT-013, OBL-OUTPUT-018, OBL-OUTPUT-023,
OBL-OUTPUT-026 and OBL-OUTPUT-029.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import boundver._output as output
from boundver._bounded_io import FileSizeLimitError, read_bounded_file
from boundver._utils import GuardrailError, _bounded_json_dumps, _short

PROFILE = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

#: Keyword combinations the two serialisers must agree under.
KEYWORDS = (
    {},
    {"sort_keys": True},
    {"ensure_ascii": False},
    {"indent": 2},
    {"indent": "\t"},
    {"separators": (",", ":")},
    {"sort_keys": True, "ensure_ascii": False, "indent": 4},
    {"allow_nan": True},
)

#: Values covering every JSON type, including ones the encoders render by
#: hand: large integers, floats, and text needing escapes.
VALUES = (
    None,
    True,
    0,
    -1,
    10 ** 30,
    1.5,
    "",
    "é一\U0001f600",
    'quote " backslash \\ newline \n tab \t',
    [],
    {},
    [1, [2, [3, {"a": None}]]],
    {"b": 1, "a": [1, 2, {"c": None}]},
)

#: DELETE is the one ASCII character ``json.dumps`` spells out under
#: ensure_ascii. The reference encoder escapes everything outside the printable
#: run from space through tilde, not merely everything below space, so the
#: capped escaper has to draw its own boundary in the same place.
DELETE = "\x7f"

#: A stream that reports a legacy encoding without being able to hold one.
LEGACY_ENCODINGS = ("cp437", "cp1252", "ascii", "not-a-real-codec")


def positions(character: str) -> tuple:
    """Return the four places one character can sit in a JSON document.

    The capped escaper runs over object keys as well as string values, and a
    character in the middle of a longer run is emitted through a different
    branch from one that stands alone, so a character worth checking is worth
    checking in each of these positions.
    """
    return (
        character,
        f"before{character}after",
        {character: 1},
        [1, {"key": [character]}],
    )


class _EncodedStream(io.StringIO):
    """A text stream that claims one encoding and accepts any string."""

    def __init__(self, encoding: str) -> None:
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self) -> str:
        return self._encoding


class BoundedJsonAgreementTests(unittest.TestCase):
    """OBL-OUTPUT-013 and 018: the same bytes, bounded or not."""

    def test_the_two_serialisers_agree_over_the_table(self):
        for index, value in enumerate(VALUES):
            for keywords in KEYWORDS:
                with self.subTest(value=index, keywords=str(keywords)):
                    plain = json.dumps(value, **keywords)
                    self.assertEqual(_bounded_json_dumps(value, **keywords), plain)

    def test_a_cap_at_the_exact_size_changes_nothing(self):
        for index, value in enumerate(VALUES):
            for keywords in KEYWORDS:
                with self.subTest(value=index, keywords=str(keywords)):
                    plain = json.dumps(value, **keywords)
                    self.assertEqual(
                        _bounded_json_dumps(
                            value, max_bytes=len(plain.encode("utf-8")), **keywords
                        ),
                        plain,
                    )

    def test_one_byte_under_refuses_rather_than_truncating(self):
        for index, value in enumerate(VALUES):
            for keywords in KEYWORDS:
                plain = json.dumps(value, **keywords)
                size = len(plain.encode("utf-8"))
                if size == 0:
                    continue
                with self.subTest(value=index, keywords=str(keywords)):
                    with self.assertRaises(GuardrailError):
                        _bounded_json_dumps(
                            value, max_bytes=size - 1, **keywords
                        )

    def test_delete_is_a_character_the_reference_encoder_spells_out(self):
        """The premise: escaping DELETE is not a no-op, so agreement means something.

        The assertions below compare the capped escaper against
        ``json.dumps``, and every one of them would hold vacuously if the
        reference encoder passed DELETE through untouched, because an escaper
        that also passed it through would then agree by accident. It does not
        pass it through. Under ensure_ascii the one input character becomes
        six, and it survives as itself only once ensure_ascii is turned off.
        """
        self.assertEqual(ord(DELETE), 0x7F)
        self.assertEqual(json.dumps(DELETE), '"\\u007f"')
        self.assertEqual(json.dumps(DELETE, ensure_ascii=False), '"' + DELETE + '"')
        self.assertGreater(
            len(json.dumps(DELETE)),
            len(json.dumps(DELETE, ensure_ascii=False)),
        )

    def test_the_capped_escaper_spells_out_delete_as_well(self):
        """MUT-OUTPUT-483: the capped escaper escapes DELETE, not only what is above it.

        The table of values above carries control characters below space and
        text far above ASCII, but nothing containing DELETE, and under a byte
        cap the escaper decides for itself which codepoints ensure_ascii has
        to spell out. Writing that decision as ``codepoint > 0x7F`` rather
        than ``codepoint >= 0x7F`` left DELETE raw in capped output while the
        uncapped path went on delegating to ``json.dumps`` and writing
        ``\\u007f``. One value then serialised two ways depending on nothing
        but whether a cap happened to be in force, which is the divergence
        OBL-OUTPUT-018 exists to forbid.
        """
        for index, value in enumerate(positions(DELETE)):
            for keywords in ({}, {"ensure_ascii": False}, {"sort_keys": True}):
                with self.subTest(position=index, keywords=str(keywords)):
                    plain = json.dumps(value, **keywords)
                    self.assertEqual(
                        _bounded_json_dumps(
                            value,
                            max_bytes=len(plain.encode("utf-8")),
                            **keywords,
                        ),
                        plain,
                    )

    def test_a_cap_one_byte_under_the_escaped_delete_still_refuses(self):
        """MUT-OUTPUT-483 from the byte accounting rather than from the text.

        An escaper that leaves DELETE raw does not only write the wrong
        characters, it writes five bytes fewer than the document truly needs.
        A cap set one byte under the real size therefore stops refusing and
        hands back a short document instead, so this pins the refusing half of
        the obligation for the same character.
        """
        plain = json.dumps(DELETE)
        size = len(plain.encode("utf-8"))
        self.assertEqual(size, 8)
        with self.assertRaises(GuardrailError):
            _bounded_json_dumps(DELETE, max_bytes=size - 1)

    def test_every_codepoint_across_the_ascii_boundary_agrees(self):
        """The neighbourhood around DELETE, so the boundary is pinned from both sides.

        DELETE sits between the last printable ASCII character and the first
        character above ASCII, and those three characters are handled by three
        different branches of the capped escaper. Sweeping the whole range
        from space to U+00FF asserts that the branches meet without leaving a
        gap between them, rather than asserting that one chosen character
        happens to come out right.
        """
        for codepoint in range(0x20, 0x100):
            character = chr(codepoint)
            for keywords in ({}, {"ensure_ascii": False}):
                with self.subTest(
                    codepoint=hex(codepoint), keywords=str(keywords)
                ):
                    plain = json.dumps(character, **keywords)
                    self.assertEqual(
                        _bounded_json_dumps(
                            character,
                            max_bytes=len(plain.encode("utf-8")),
                            **keywords,
                        ),
                        plain,
                    )

    def test_the_printable_neighbour_is_still_emitted_raw(self):
        """The contrast: escaping DELETE does not mean escaping everything.

        An escaper that spelled out every character it saw would satisfy the
        three assertions above and be useless, so this asserts that the
        ordinary case still passes through. Tilde is the character immediately
        below DELETE and has to survive as itself, and DELETE has to survive
        as itself too once ensure_ascii is turned off, which is where the
        reference encoder draws the same line.
        """
        self.assertEqual(_bounded_json_dumps("~", max_bytes=3), '"~"')
        self.assertEqual(
            _bounded_json_dumps(DELETE, ensure_ascii=False, max_bytes=3),
            '"' + DELETE + '"',
        )

    @PROFILE
    @given(
        value=st.recursive(
            st.one_of(
                st.none(),
                st.booleans(),
                st.integers(),
                st.floats(allow_nan=False, allow_infinity=False),
                st.text(max_size=8),
            ),
            lambda children: st.one_of(
                st.lists(children, max_size=4),
                st.dictionaries(st.text(max_size=4), children, max_size=4),
            ),
            max_leaves=12,
        ),
        keywords=st.sampled_from(KEYWORDS),
    )
    def test_they_agree_for_a_generated_value(self, value, keywords):
        plain = json.dumps(value, **keywords)
        self.assertEqual(_bounded_json_dumps(value, **keywords), plain)
        self.assertEqual(
            _bounded_json_dumps(
                value, max_bytes=len(plain.encode("utf-8")), **keywords
            ),
            plain,
        )

    def test_a_negative_cap_is_a_usage_error(self):
        """The contrast: a bad bound is refused before anything is rendered."""
        with self.assertRaises(ValueError):
            _bounded_json_dumps({}, max_bytes=-1)


class ShortenerHonestyTests(unittest.TestCase):
    """OBL-OUTPUT-026: a marker means something was left out."""

    def test_a_long_value_is_shortened_and_marked(self):
        """The premise: the marker is right where it belongs."""
        digest = "a" * 64
        self.assertEqual(_short(digest), "a" * 12 + "...")
        self.assertLess(len(_short(digest)), len(digest))

    def test_a_value_that_fits_is_not_marked(self):
        """Known divergence: the marker is appended unconditionally."""
        for value in ("", "a", "a" * 11, "a" * 12):
            with self.subTest(value=len(value)):
                self.assertEqual(_short(value), value)

    def test_every_value_at_or_under_twelve_is_unchanged(self):
        for value in ("", "a", "a" * 12):
            with self.subTest(value=len(value)):
                self.assertEqual(_short(value), value)

    def test_none_is_rendered_as_a_word_rather_than_shortened(self):
        self.assertEqual(_short(None), "none")

    def test_a_non_string_raises(self):
        """Recorded rather than asserted as desirable: it is a TypeError."""
        with self.assertRaises(TypeError):
            _short(123)


class _GrowingPath:
    """A path whose file gains bytes between its stat and its first read."""

    def __init__(self, real: Path, extra: int) -> None:
        self._real = real
        self._extra = extra

    def __str__(self) -> str:
        return str(self._real)

    def lstat(self):
        return self._real.lstat()

    def open(self, mode="rb"):
        stream = self._real.open(mode)
        grower = self

        class _Stream:
            def __init__(self) -> None:
                self._grown = False

            def fileno(self):
                return stream.fileno()

            def read(self, size=-1):
                if not self._grown:
                    self._grown = True
                    with grower._real.open("ab") as appending:
                        appending.write(b"x" * grower._extra)
                return stream.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                stream.close()
                return False

        return _Stream()


class BoundedReaderTests(unittest.TestCase):
    """OBL-OUTPUT-023: the limit, one past it, and one that moved."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())

    def _file(self, name: str, size: int) -> Path:
        target = self.directory / name
        target.write_bytes(b"x" * size)
        return target

    def test_an_empty_file_at_a_zero_limit_reads(self):
        self.assertEqual(read_bounded_file(self._file("empty", 0), 0), b"")

    def test_a_file_of_exactly_the_limit_reads(self):
        self.assertEqual(read_bounded_file(self._file("exact", 10), 10), b"x" * 10)

    def test_one_byte_over_is_refused_with_the_true_size(self):
        with self.assertRaises(FileSizeLimitError) as raised:
            read_bounded_file(self._file("over", 11), 10)
        self.assertEqual(raised.exception.size, 11)
        self.assertEqual(raised.exception.limit, 10)
        self.assertFalse(raised.exception.grew_during_read)

    def test_a_file_that_grows_is_distinguished_by_its_flag(self):
        target = self._file("growing", 10)
        with self.assertRaises(FileSizeLimitError) as raised:
            read_bounded_file(_GrowingPath(target, 5), 10)
        self.assertTrue(raised.exception.grew_during_read)
        self.assertGreater(raised.exception.size, 10)

    def test_the_two_refusals_are_told_apart_by_that_flag_alone(self):
        """Which is the point of carrying it: the message does not say."""
        first = FileSizeLimitError(Path("p"), 11, 10)
        second = FileSizeLimitError(Path("p"), 11, 10, grew_during_read=True)
        self.assertEqual(str(first), str(second))
        self.assertNotEqual(first.grew_during_read, second.grew_during_read)


class LegacyEncodingTests(unittest.TestCase):
    """OBL-OUTPUT-029: a stream that cannot hold what it was given."""

    HOSTILE = "café 一 \U0001f600 \x1b[31m \r\n‮"

    def _printed(self, encoding: str) -> str:
        stream = _EncodedStream(encoding)
        output.safe_print(self.HOSTILE, file=stream)
        return stream.getvalue()

    def test_no_encoding_makes_the_write_fail(self):
        for encoding in LEGACY_ENCODINGS:
            with self.subTest(encoding=encoding):
                self.assertTrue(self._printed(encoding).endswith("\n"))

    def test_no_control_character_survives(self):
        for encoding in LEGACY_ENCODINGS:
            with self.subTest(encoding=encoding):
                written = self._printed(encoding).rstrip("\n")
                for character in written:
                    self.assertGreaterEqual(ord(character), 32, repr(written))

    def test_nothing_written_begins_a_workflow_command(self):
        for encoding in LEGACY_ENCODINGS:
            with self.subTest(encoding=encoding):
                for line in self._printed(encoding).splitlines():
                    self.assertFalse(line.startswith("::"), line)

    def test_an_unknown_codec_falls_back_rather_than_raising(self):
        """The premise for the last row of the table."""
        written = self._printed("not-a-real-codec")
        self.assertIn("\\x1b", written)
        self.assertNotIn("\x1b", written)


class AnnotationGuardTests(unittest.TestCase):
    """OBL-OUTPUT-012: the guard belongs on the line, not on a value."""

    def _line(self, *values, sep=" ") -> str:
        stream = io.StringIO()
        output.safe_print(*values, sep=sep, file=stream)
        return stream.getvalue().rstrip("\n")

    def test_a_single_value_that_starts_one_is_neutralised(self):
        """The premise: the guard exists and works on one value."""
        self.assertEqual(self._line("::set-output name=x"), "\\x3a:set-output name=x")

    def test_an_empty_first_value_does_not_defeat_it(self):
        self.assertEqual(
            self._line("", "::set-output name=x", sep=""), "\\x3a:set-output name=x"
        )

    def test_no_assembled_line_begins_one(self):
        """Known divergence: two halves each carrying one colon get through."""
        self.assertFalse(self._line(":", ":set-output name=x", sep="").startswith("::"))

    def test_the_assembled_line_is_defanged(self):
        values = (":", ":set-output name=x")
        for value in values:
            self.assertFalse(value.startswith("::"))
        self.assertEqual(self._line(*values, sep=""), "\\x3a:set-output name=x")
        self.assertEqual(self._line(*values), ": :set-output name=x")


if __name__ == "__main__":
    unittest.main()
