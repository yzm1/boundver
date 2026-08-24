from __future__ import annotations

import unittest

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
