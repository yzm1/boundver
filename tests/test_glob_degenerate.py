"""The empty candidate, which nothing on disk can ever be.

A path with no characters is not a file, so no pattern should select it. The
matcher validates a candidate into a tuple of segments, and the empty string
validates to no segments at all; a pattern that consumes no segments then
finds itself in an accepting state with nothing left to match.

Covers OBL-GLOBS-004.
"""

from __future__ import annotations

import unittest

from boundver._utils import (
    GuardrailError,
    _match_path_glob,
    _validated_path_glob_candidate,
)

#: Patterns spanning the grammar: literal, wildcard, class, and the two that
#: can match without consuming a segment.
PATTERNS = ("", "a", "*", "**", "*.py", "a/b", "**/*.py", "[ab]", "**/**")


class EmptyCandidateTests(unittest.TestCase):
    """OBL-GLOBS-004: no pattern selects the empty candidate."""

    def test_no_pattern_selects_the_empty_candidate(self):
        for pattern in PATTERNS:
            with self.subTest(pattern=pattern):
                if pattern:
                    self.assertFalse(_match_path_glob("", pattern))
                else:
                    with self.assertRaises(GuardrailError):
                        _match_path_glob("", pattern)

    def test_no_valid_pattern_selects_the_empty_candidate(self):
        selecting = [
            pattern for pattern in PATTERNS if pattern and _match_path_glob("", pattern)
        ]
        self.assertEqual(selecting, [])

    def test_the_empty_candidate_validates_to_no_segments(self):
        """The mechanism: nothing is left for a pattern to consume."""
        self.assertIsNone(_validated_path_glob_candidate(""))

    def test_a_real_candidate_is_matched_the_way_it_should_be(self):
        """The contrast: the matcher is not simply saying yes to everything."""
        self.assertTrue(_match_path_glob("a.py", "*.py"))
        self.assertTrue(_match_path_glob("a/b.py", "**/*.py"))
        self.assertFalse(_match_path_glob("a.py", "*.ts"))
        with self.assertRaises(GuardrailError):
            _match_path_glob("a.py", "")


if __name__ == "__main__":
    unittest.main()
