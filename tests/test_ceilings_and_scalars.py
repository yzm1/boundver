"""Table tests for declared ceilings and for scalars with no JSON equivalent.

Two shapes of obligation need nothing but a table: a documented limit, which
must be asserted at the boundary rather than somewhere past it, and a value the
parser must refuse rather than coerce.

A limit asserted only well past its boundary is close to untested. Every case
here pins the accepted side too, so an off-by-one in either direction fails.
Where a limit is stated in bytes the table uses a multi-byte character, since
counting characters would pass a length test built from ASCII.

Every limit here is asserted relative to its constant, which is correct
for a boundary and says nothing about the constant's value. The values
themselves are pinned in tests/test_declared_ceilings.py.

Covers OBL-GIT-SOURCE-023, OBL-GIT-SOURCE-102 and the rejection half of
OBL-HASHING-050.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from boundver._config import parse_config_text
from boundver._git import (
    MAX_GITIGNORE_MATCH_STEPS,
    MAX_GITIGNORE_PATTERN_BYTES,
    MAX_GITIGNORE_RULES,
    _GitignoreRules,
)
from boundver._utils import ConfigError, GuardrailError

#: A three-byte character, so a byte limit and a character limit disagree.
WIDE = "中"

TOML_TAIL = (
    'project = "p"\n'
    "[components.svc]\n"
    'path = "svc"\n'
    "[components.svc.boundary]\n"
    'provider = "leaf"\n'
    "paths = []\n"
)

YAML_TAIL = (
    "project: p\n"
    "components:\n"
    "  svc:\n"
    "    path: svc\n"
    "    boundary:\n"
    "      provider: leaf\n"
    "      paths: []\n"
)


def _json_document(value: str) -> str:
    return (
        '{"x": ' + value + ', "project": "p", "components": {"svc": '
        '{"path": "svc", "boundary": {"provider": "leaf", "paths": []}}}}'
    )


class GitignoreCeilingTests(unittest.TestCase):
    """OBL-GIT-SOURCE-102: the fallback matcher must fail closed at each cap."""

    def test_the_rule_count_ceiling_holds_at_its_boundary(self):
        accepted = _GitignoreRules()
        for index in range(MAX_GITIGNORE_RULES):
            accepted.add(f"p{index}")

        rejected = _GitignoreRules()
        with self.assertRaises(GuardrailError) as caught:
            for index in range(MAX_GITIGNORE_RULES + 1):
                rejected.add(f"p{index}")
        self.assertIn(str(MAX_GITIGNORE_RULES), str(caught.exception))

    def test_the_pattern_length_ceiling_counts_utf8_bytes(self):
        """A character count would let a multi-byte pattern past the limit."""
        within = MAX_GITIGNORE_PATTERN_BYTES // 3
        _GitignoreRules().add(WIDE * within)
        self.assertLessEqual(len((WIDE * within).encode("utf-8")),
                             MAX_GITIGNORE_PATTERN_BYTES)

        over = within + 1
        self.assertGreater(len((WIDE * over).encode("utf-8")),
                           MAX_GITIGNORE_PATTERN_BYTES)
        with self.assertRaises(GuardrailError) as caught:
            _GitignoreRules().add(WIDE * over)
        self.assertIn("UTF-8 bytes", str(caught.exception))

    def test_an_ascii_pattern_holds_at_the_same_boundary(self):
        _GitignoreRules().add("a" * MAX_GITIGNORE_PATTERN_BYTES)
        with self.assertRaises(GuardrailError):
            _GitignoreRules().add("a" * (MAX_GITIGNORE_PATTERN_BYTES + 1))

    def test_the_aggregate_match_budget_fails_closed(self):
        """Steps accumulate across calls, so a long run must stop, not slow."""
        rules = _GitignoreRules()
        for index in range(500):
            rules.add(f"**/x{index}/**/y")
        with self.assertRaises(GuardrailError) as caught:
            for _ in range(200_000):
                rules.is_ignored("a/b/c/d/e/f/g/h/i/j/k.txt")
        self.assertIn(str(MAX_GITIGNORE_MATCH_STEPS), str(caught.exception))

    def test_a_short_rule_set_never_reaches_the_budget(self):
        """The ceiling must not fire on ordinary input."""
        rules = _GitignoreRules()
        for pattern in ("build", "*.log", "!keep.log", "/root", "a/**"):
            rules.add(pattern)
        for _ in range(1000):
            rules.is_ignored("a/b/c.txt")


class TomlNumericGuardrailTests(unittest.TestCase):
    """OBL-GIT-SOURCE-023: TOML gets a tighter pre-parse digit run limit."""

    LIMIT = 640

    def _parse(self, value: str):
        return parse_config_text(f"x = {value}\n" + TOML_TAIL, Path("c.toml"))

    def test_each_base_holds_at_the_same_boundary(self):
        for label, prefix, digit in (
            ("decimal", "", "9"),
            ("hex", "0x", "a"),
            ("octal", "0o", "7"),
            ("binary", "0b", "1"),
        ):
            with self.subTest(base=label):
                self._parse(prefix + digit * self.LIMIT)
                with self.assertRaises(ConfigError) as caught:
                    self._parse(prefix + digit * (self.LIMIT + 1))
                self.assertIn("numeric token", str(caught.exception))

    def test_digits_inside_a_quoted_string_are_not_a_value_run(self):
        """The limit is about numbers the parser must build, not about text."""
        parsed = self._parse('"' + "9" * 5000 + '"')
        self.assertEqual(parsed["x"], "9" * 5000)

    def test_underscore_separators_do_not_count_toward_the_run(self):
        """TOML lets 1_000 mean 1000, so the separators are not digits.

        Counting them would reject a legal number well under the cap, and
        skipping the digits around them would let an oversized one through.
        Both directions need the boundary, so this tables 640 and 641 with the
        separators in place.
        """
        grouped = "_".join("9" * 3 for _ in range(213))
        within = grouped + "_9"
        self.assertEqual(sum(c.isdigit() for c in within), self.LIMIT)
        self._parse(within)

        over = grouped + "_99"
        self.assertEqual(sum(c.isdigit() for c in over), self.LIMIT + 1)
        with self.assertRaises(ConfigError):
            self._parse(over)

    def test_a_long_run_in_a_non_numeric_context_does_not_trip(self):
        """The scanner tracks string and comment state, and each state counts.

        Literal strings are their own states in the hand-written scanner, and
        were the two the suite never reached.
        """
        run = "9" * (self.LIMIT + 1)
        for label, line in (
            ("basic string", f'x = "{run}"'),
            ("literal string", f"x = '{run}'"),
            ("multiline basic", f'x = """{run}"""'),
            ("multiline literal", f"x = '''{run}'''"),
            ("comment", f"x = 1  # {run}"),
            ("bare key", f"k{run} = 1"),
        ):
            with self.subTest(context=label):
                parse_config_text(line + "\n" + TOML_TAIL, Path("c.toml"))

    def test_the_limit_applies_even_when_every_named_field_is_valid(self):
        """A long run anywhere rejects the document, not just a bad field."""
        document = (
            "junk = " + "9" * (self.LIMIT + 1) + "\n"
            'version = "1.2.3"\n' + TOML_TAIL
        )
        with self.assertRaises(ConfigError):
            parse_config_text(document, Path("c.toml"))


class NonJsonScalarTests(unittest.TestCase):
    """OBL-HASHING-050: refuse what JSON cannot represent, never coerce it."""

    def _reject(self, text: str, name: str):
        with self.assertRaises(ConfigError) as caught:
            parse_config_text(text, Path(name))
        return str(caught.exception)

    def test_toml_dates_and_times_are_refused(self):
        for label, value in (
            ("date", "2020-01-01"),
            ("time", "12:30:00"),
            ("datetime", "2020-01-01T00:00:00Z"),
            ("local datetime", "2020-01-01T00:00:00"),
        ):
            with self.subTest(scalar=label):
                message = self._reject(f"x = {value}\n" + TOML_TAIL, "c.toml")
                self.assertIn("cannot be represented", message)

    def test_toml_non_finite_floats_are_refused(self):
        for value in ("inf", "-inf", "nan"):
            with self.subTest(scalar=value):
                message = self._reject(f"x = {value}\n" + TOML_TAIL, "c.toml")
                self.assertIn("cannot be represented", message)

    def test_yaml_dates_and_timestamps_are_refused(self):
        for label, value in (
            ("date", "2020-01-01"),
            ("timestamp", "2020-01-01T00:00:00Z"),
        ):
            with self.subTest(scalar=label):
                message = self._reject(f"x: {value}\n" + YAML_TAIL, "c.yaml")
                self.assertIn("cannot be represented", message)

    def test_yaml_non_finite_floats_are_refused(self):
        for value in (".inf", "-.inf", ".nan"):
            with self.subTest(scalar=value):
                message = self._reject(f"x: {value}\n" + YAML_TAIL, "c.yaml")
                self.assertIn("cannot be represented", message)

    def test_a_yaml_sexagesimal_is_refused_rather_than_multiplied(self):
        """YAML 1.1 reads 1:30 as ninety. JSON has no such reading."""
        message = self._reject("x: 1:30\n" + YAML_TAIL, "c.yaml")
        self.assertIn("YAML integer", message)

    def test_the_integer_digit_limit_holds_for_json_and_yaml(self):
        limit = 4300
        for name, build in (
            ("c.json", lambda run: _json_document(run)),
            ("c.yaml", lambda run: f"x: {run}\n" + YAML_TAIL),
        ):
            with self.subTest(format=name):
                parse_config_text(build("9" * limit), Path(name))
                with self.assertRaises(ConfigError):
                    parse_config_text(build("9" * (limit + 1)), Path(name))

    def test_toml_is_stricter_than_json_on_purpose(self):
        """The two limits differ, and the difference is the point.

        A run of a thousand digits is a legal JSON and YAML integer here and a
        rejected TOML one. Asserting it stops a later change from quietly
        levelling the two, which would loosen the TOML guardrail.
        """
        run = "9" * 1000
        parse_config_text(_json_document(run), Path("c.json"))
        parse_config_text(f"x: {run}\n" + YAML_TAIL, Path("c.yaml"))
        with self.assertRaises(ConfigError):
            parse_config_text(f"x = {run}\n" + TOML_TAIL, Path("c.toml"))


if __name__ == "__main__":
    unittest.main()
