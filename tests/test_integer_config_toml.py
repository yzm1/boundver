"""Cross-runtime integer contracts for TOML configuration."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from boundver._config import parse_config_text
from boundver._utils import ConfigError, MAX_TOML_INTEGER_DIGITS


class TomlConfigIntegerTests(unittest.TestCase):
    def _oversized_text(self) -> str:
        return (
            'project = "p"\n'
            f"sequence = {'9' * (MAX_TOML_INTEGER_DIGITS + 1)}\n"
            "[components]\n"
        )

    def test_oversized_unrelated_integer_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "cross-runtime safety limit"):
            parse_config_text(
                self._oversized_text(),
                Path("boundary.config.toml"),
            )

    def test_oversized_hexadecimal_integer_is_rejected(self):
        text = (
            'project = "p"\n'
            f"sequence = 0x{'F' * (MAX_TOML_INTEGER_DIGITS + 1)}\n"
            "[components]\n"
        )
        with self.assertRaisesRegex(ConfigError, "cross-runtime safety limit"):
            parse_config_text(text, Path("boundary.config.toml"))

    def test_long_digit_containing_bare_component_key_is_allowed(self):
        component_name = (
            "service-" + "9" * (MAX_TOML_INTEGER_DIGITS + 1)
        )
        text = (
            'project = "p"\n'
            f"[components.{component_name}]\n"
            'path = "svc"\n'
        )

        parsed = parse_config_text(text, Path("boundary.config.toml"))

        self.assertEqual(parsed["components"][component_name]["path"], "svc")

    def test_oversized_integer_inside_inline_table_is_rejected(self):
        text = (
            'project = "p"\n'
            "metadata = { sequence = "
            + "9" * (MAX_TOML_INTEGER_DIGITS + 1)
            + " }\n[components]\n"
        )

        with self.assertRaisesRegex(ConfigError, "cross-runtime safety limit"):
            parse_config_text(text, Path("boundary.config.toml"))

    @unittest.skipUnless(
        hasattr(sys, "set_int_max_str_digits"),
        "runtime has no configurable integer conversion limit",
    )
    def test_oversized_unrelated_integer_is_rejected_independently(self):
        previous = sys.get_int_max_str_digits()
        try:
            messages = []
            for setting in (640, 4300, 0):
                sys.set_int_max_str_digits(setting)
                with self.assertRaises(ConfigError) as raised:
                    parse_config_text(
                        self._oversized_text(),
                        Path("boundary.config.toml"),
                    )
                messages.append(str(raised.exception))
        finally:
            sys.set_int_max_str_digits(previous)

        self.assertEqual(messages, [messages[0]] * len(messages))
        self.assertIn("cross-runtime safety limit", messages[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
