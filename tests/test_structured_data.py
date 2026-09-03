from __future__ import annotations

import unittest
from unittest.mock import patch

import boundver._structured_data as structured_data
from boundver._structured_data import StrictJSONError, strict_json_loads


class StrictJSONTests(unittest.TestCase):
    def test_accepts_regular_json(self):
        self.assertEqual(strict_json_loads('{"a": [1, true, null]}'), {"a": [1, True, None]})

    def test_rejects_duplicate_keys_and_nonfinite_numbers(self):
        for payload in ('{"a": 1, "a": 2}', '{"a": NaN}'):
            with self.subTest(payload=payload), self.assertRaises(StrictJSONError):
                strict_json_loads(payload)

    def test_rejects_cross_version_oversized_integers(self):
        with self.assertRaisesRegex(ValueError, "decimal-digit limit"):
            strict_json_loads('{"n": ' + "1" * 4301 + "}")

    def test_rejects_oversized_or_nonfinite_float_tokens(self):
        oversized = "1." + ("0" * 4_400)
        for payload, message in (
            ('{"n": ' + oversized + "}", "character limit"),
            ('{"n": 1e9999}', "non-finite"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                ValueError, message
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
