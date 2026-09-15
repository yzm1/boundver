"""Property-based tests for the bounded glob matcher.

The register lists 43 generative obligations against the glob layer, and the
module they cover has produced five escaped semantic bugs. Example tests keep
missing them for the obvious reason: the interesting inputs are the ones nobody
thinks to write down, such as ``[!]]`` or ``[a-]`` or a pattern whose bracket
expression never closes.

Two oracles do most of the work here. ``_compile_text_glob`` states that it
follows fnmatch's documented class grammar, which makes ``fnmatch.fnmatchcase``
a real differential oracle for a single segment rather than a reimplementation
of the code under test. And boundver ships two matchers, one segment-aware and
one not, which must agree whenever no separator is present.

Covers OBL-GLOBS-003, OBL-GLOBS-007, OBL-GLOBS-016, OBL-GLOBS-020 and
OBL-GLOBS-021.
"""

from __future__ import annotations

import fnmatch
import time
import unittest
from typing import List, Optional

from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st

from boundver._utils import (
    MAX_GLOB_METACHARACTERS_PER_SEGMENT,
    MAX_GLOB_PATH_BYTES,
    GuardrailError,
    _is_glob,
    _match_path_glob,
    _match_text_glob,
)

#: A deliberately small alphabet. Two ordinary characters are enough to
#: distinguish matches, and the rest are the metacharacters where the grammar
#: is subtle, so a short random string is dense in interesting cases.
SEGMENT_ALPHABET = "ab.-_*?[]!^"

PROFILE = settings(
    max_examples=400,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)


def segments(alphabet: str = SEGMENT_ALPHABET, max_size: int = 8) -> st.SearchStrategy:
    """One path segment: never empty, never containing a separator."""
    return st.text(alphabet=alphabet, min_size=1, max_size=max_size).filter(
        lambda value: value not in {".", ".."}
    )


def literal_segments() -> st.SearchStrategy:
    return st.text(alphabet="ab.-_", min_size=1, max_size=6).filter(
        lambda value: value not in {".", ".."}
    )


#: Any character a path segment may hold. The small alphabet above makes
#: matches common, but it also hides anything conditioned on a character
#: outside it, so the grammar tests need a source with no such blind spot.
WIDE_CHARACTERS = st.characters(
    blacklist_characters="/", blacklist_categories=("Cs", "Cc")
)


def wide_segments(max_size: int = 5) -> st.SearchStrategy:
    return st.text(alphabet=WIDE_CHARACTERS, min_size=1, max_size=max_size)


def paths(max_segments: int = 3) -> st.SearchStrategy:
    return st.lists(literal_segments(), min_size=1, max_size=max_segments).map(
        "/".join
    )


def patterns(max_segments: int = 3) -> st.SearchStrategy:
    return st.lists(segments(), min_size=1, max_size=max_segments).map("/".join)


def _match_or_none(candidate: str, pattern: str) -> Optional[bool]:
    """The verdict, or None when a guardrail refused the input."""
    try:
        return _match_path_glob(candidate, pattern)
    except GuardrailError:
        return None


def _steps(candidate: str, pattern: str) -> int:
    """Primitive matcher steps charged for one match, or -1 if refused."""
    charged = [0]

    def consumer(amount: int) -> None:
        charged[0] += amount

    try:
        _match_path_glob(candidate, pattern, _step_consumer=consumer)
    except GuardrailError:
        return -1
    return charged[0]


@st.composite
def derived_segment_pairs(draw) -> tuple:
    """A segment and a pattern built from it, so a match is the common case.

    Drawing a pattern and a candidate independently gives a match about two
    per cent of the time, which leaves the interesting direction — a pattern
    that should match and does not — barely sampled. Replacing characters of a
    known string with wildcards that cover them keeps every pattern a match by
    construction.
    """
    candidate = draw(literal_segments())
    pieces = []
    for character in candidate:
        choice = draw(st.integers(min_value=0, max_value=4))
        if choice == 0:
            pieces.append("?")
        elif choice == 1:
            pieces.append("*")
        elif choice == 2:
            pieces.append("[" + character + "]")
        elif choice == 3:
            pieces.append("[" + character + "a-c]")
        else:
            pieces.append(character)
    return "".join(pieces), candidate


@st.composite
def derived_path_pairs(draw) -> tuple:
    """A path and a segment-aware pattern that must select it."""
    path_segments = draw(st.lists(literal_segments(), min_size=1, max_size=4))
    pattern_segments = []
    for segment in path_segments:
        choice = draw(st.integers(min_value=0, max_value=3))
        if choice == 0:
            pattern_segments.append("*")
        elif choice == 1:
            pattern_segments.append("?" * len(segment))
        else:
            pattern_segments.append(segment)
    if draw(st.booleans()):
        pattern_segments.insert(
            draw(st.integers(min_value=0, max_value=len(pattern_segments))), "**"
        )
    return "/".join(pattern_segments), "/".join(path_segments)


class SingleSegmentGrammarTests(unittest.TestCase):
    """OBL-GLOBS-016: bracket expressions follow fnmatch's grammar."""

    @PROFILE
    @given(pattern=segments(), candidate=segments())
    @example(pattern="[!]]", candidate="a")
    @example(pattern="[a-]", candidate="-")
    @example(pattern="[]]", candidate="]")
    @example(pattern="[", candidate="[")
    # An unclosed [ is a literal, so it must reject anything else. Asserting
    # only that it matches "[" cannot distinguish a literal from a wildcard.
    @example(pattern="[", candidate="a")
    @example(pattern="a[b", candidate="axb")
    @example(pattern="[^a]", candidate="^")
    @example(pattern="a**b", candidate="ab")
    @example(pattern="A", candidate="a")
    def test_a_single_segment_pattern_agrees_with_fnmatch(self, pattern, candidate):
        # A segment of exactly ** is boundver's recursive wildcard, which
        # fnmatch has no notion of, so it is not a fair comparison.
        assume(pattern != "**")
        verdict = _match_or_none(candidate, pattern)
        assume(verdict is not None)
        self.assertEqual(
            verdict,
            fnmatch.fnmatchcase(candidate, pattern),
            f"pattern {pattern!r} against {candidate!r}",
        )

    @PROFILE
    @given(pair=derived_segment_pairs())
    def test_a_pattern_built_from_a_segment_selects_that_segment(self, pair):
        """Every wildcard here covers the character it replaced."""
        pattern, candidate = pair
        self.assertIs(_match_or_none(candidate, pattern), True,
                      f"pattern {pattern!r} failed to select {candidate!r}")
        self.assertTrue(fnmatch.fnmatchcase(candidate, pattern))

    @PROFILE
    @given(pair=derived_segment_pairs(), candidate=literal_segments())
    def test_a_derived_pattern_still_agrees_with_fnmatch_on_other_input(
        self, pair, candidate
    ):
        """The same patterns, judged against segments they may well reject."""
        pattern, _source = pair
        verdict = _match_or_none(candidate, pattern)
        assume(verdict is not None)
        self.assertEqual(
            verdict,
            fnmatch.fnmatchcase(candidate, pattern),
            f"pattern {pattern!r} against {candidate!r}",
        )

    @PROFILE
    @given(pattern=segments(), candidate=segments())
    def test_both_matchers_agree_when_no_separator_is_present(self, pattern, candidate):
        """OBL-GLOBS: the two engines must not diverge on separator-free input."""
        assume(pattern != "**")
        path_verdict = _match_or_none(candidate, pattern)
        assume(path_verdict is not None)
        try:
            text_verdict = _match_text_glob(candidate, pattern)
        except GuardrailError:
            return
        self.assertEqual(path_verdict, text_verdict)

    @PROFILE
    @given(pattern=literal_segments(), candidate=literal_segments())
    def test_a_literal_segment_matches_itself_and_nothing_else(self, pattern, candidate):
        assume(not _is_glob(pattern))
        self.assertEqual(_match_or_none(candidate, pattern), candidate == pattern)

    @PROFILE
    @given(pattern=wide_segments(), candidate=wide_segments())
    def test_the_text_matcher_agrees_with_fnmatch(self, pattern, candidate):
        """OBL-GLOBS-016 names ``_match_text_glob`` and fnmatch by name."""
        try:
            verdict = _match_text_glob(candidate, pattern)
        except GuardrailError:
            return
        self.assertEqual(
            verdict,
            fnmatch.fnmatchcase(candidate, pattern),
            f"pattern {pattern!r} against {candidate!r}",
        )

    @PROFILE
    @given(
        pattern=st.text(alphabet=SEGMENT_ALPHABET + "/", min_size=1, max_size=10),
        candidate=st.text(alphabet="ab/.", min_size=1, max_size=10),
    )
    def test_the_text_matcher_agrees_with_fnmatch_across_separators(
        self, pattern, candidate
    ):
        """The text matcher has no segment rule, so ``*`` may cross ``/`` here."""
        try:
            verdict = _match_text_glob(candidate, pattern)
        except GuardrailError:
            return
        self.assertEqual(
            verdict,
            fnmatch.fnmatchcase(candidate, pattern),
            f"pattern {pattern!r} against {candidate!r}",
        )

    @PROFILE
    @given(pattern=wide_segments(), candidate=wide_segments())
    def test_arbitrary_characters_agree_with_fnmatch(self, pattern, candidate):
        """The small alphabet cannot see a bug that turns on some other character."""
        assume(pattern != "**")
        verdict = _match_or_none(candidate, pattern)
        assume(verdict is not None)
        self.assertEqual(
            verdict,
            fnmatch.fnmatchcase(candidate, pattern),
            f"pattern {pattern!r} against {candidate!r}",
        )

    @PROFILE
    @given(member=WIDE_CHARACTERS, candidate=WIDE_CHARACTERS)
    def test_a_one_character_class_agrees_with_fnmatch(self, member, candidate):
        pattern = "[" + member + "]"
        verdict = _match_or_none(candidate, pattern)
        assume(verdict is not None)
        self.assertEqual(verdict, fnmatch.fnmatchcase(candidate, pattern),
                         f"class {pattern!r} against {candidate!r}")

    @PROFILE
    @given(character=WIDE_CHARACTERS)
    def test_a_question_mark_matches_exactly_one_character(self, character):
        self.assertIs(_match_or_none(character, "?"), True, repr(character))

    @PROFILE
    @given(character=WIDE_CHARACTERS)
    def test_matching_is_case_sensitive(self, character):
        """`_match_path_glob` documents case-sensitive matching.

        A pattern of one plain character is compiled as a whole-segment
        literal and never reaches the token matcher, so the wildcard forms
        below are the ones that exercise per-character comparison.
        """
        upper = character.upper()
        assume(len(upper) == 1 and upper != character)
        self.assertIs(_match_or_none(character, character), True)
        self.assertIs(_match_or_none(character, upper), False, repr(character))
        self.assertIs(_match_or_none(upper, character), False, repr(character))
        for pattern, candidate in (
            ("*" + upper, character),
            (upper + "*", character),
            ("?" + upper, "x" + character),
        ):
            self.assertIs(_match_or_none(candidate, pattern), False,
                          f"{pattern!r} matched {candidate!r}")
        self.assertIs(_match_or_none(character, "*" + character), True)

    @PROFILE
    @given(character=WIDE_CHARACTERS)
    def test_a_class_is_case_sensitive_too(self, character):
        upper = character.upper()
        assume(len(upper) == 1 and upper != character)
        self.assertIs(_match_or_none(character, "[" + upper + "]"), False)

    @PROFILE
    @given(candidate=wide_segments(max_size=4))
    def test_a_question_mark_run_matches_only_that_many_characters(self, candidate):
        for width in range(1, 6):
            self.assertEqual(
                _match_or_none(candidate, "?" * width),
                len(candidate) == width,
                f"{'?' * width} against {candidate!r}",
            )


class SeparatorContainmentTests(unittest.TestCase):
    """OBL-GLOBS-007: no ordinary wildcard may cross a separator."""

    @PROFILE
    @given(pattern=segments(), candidate=st.lists(literal_segments(), min_size=2, max_size=4))
    def test_a_one_segment_pattern_never_matches_a_multi_segment_path(
        self, pattern, candidate
    ):
        assume(pattern != "**")
        self.assertIs(_match_or_none(candidate="/".join(candidate), pattern=pattern), False)

    @PROFILE
    @given(pattern=patterns(), candidate=paths())
    def test_a_match_needs_as_many_segments_as_the_pattern_has(self, pattern, candidate):
        """Without a ** the segment counts must be equal."""
        assume("**" not in pattern.split("/"))
        verdict = _match_or_none(candidate, pattern)
        assume(verdict is not None)
        if verdict:
            self.assertEqual(len(candidate.split("/")), len(pattern.split("/")))

    @PROFILE
    @given(candidate=paths(max_segments=5))
    def test_a_lone_recursive_wildcard_matches_every_valid_path(self, candidate):
        self.assertIs(_match_or_none(candidate, "**"), True)

    @PROFILE
    @given(name=wide_segments())
    def test_a_wildcard_matches_a_leading_dot(self, name):
        """Dotfiles are declared contract files, so no implicit exclusion.

        POSIX glob and shell expansion both hide names beginning with a dot.
        A matcher that picked up that habit would silently drop `.env` and
        `.gitattributes` from every boundary selection.
        """
        assume(not name.startswith("."))
        hidden = "." + name
        self.assertIs(_match_or_none(hidden, "*"), True, repr(hidden))
        self.assertIs(_match_or_none(hidden, "**"), True, repr(hidden))
        self.assertIs(_match_or_none("dir/" + hidden, "dir/*"), True, repr(hidden))
        self.assertIs(_match_or_none(hidden, "." + "?" * len(name)), True, repr(hidden))

    @PROFILE
    @given(pair=derived_path_pairs())
    def test_a_pattern_built_from_a_path_selects_that_path(self, pair):
        pattern, candidate = pair
        self.assertIs(_match_or_none(candidate, pattern), True,
                      f"pattern {pattern!r} failed to select {candidate!r}")

    @PROFILE
    @given(pair=derived_path_pairs())
    def test_prefixing_a_recursive_wildcard_can_only_widen_a_pattern(self, pair):
        """** matches zero segments, so ``**/p`` selects everything ``p`` does."""
        pattern, candidate = pair
        narrow = _match_or_none(candidate, pattern)
        assume(narrow is not None)
        if narrow:
            self.assertIs(_match_or_none(candidate, "**/" + pattern), True)


class RecursiveWildcardCollapseTests(unittest.TestCase):
    """OBL-GLOBS-003: adjacent ** segments are equivalent to one."""

    @PROFILE
    @given(
        prefix=st.lists(segments(), max_size=2),
        suffix=st.lists(segments(), max_size=2),
        repeats=st.integers(min_value=2, max_value=4),
        candidate=paths(max_segments=5),
    )
    def test_repeating_a_recursive_wildcard_preserves_every_verdict(
        self, prefix, suffix, repeats, candidate
    ):
        one = "/".join([*prefix, "**", *suffix])
        many = "/".join([*prefix, *["**"] * repeats, *suffix])
        self.assertEqual(
            _match_or_none(candidate, one),
            _match_or_none(candidate, many),
            f"{one!r} and {many!r} disagreed on {candidate!r}",
        )

    @PROFILE
    @given(candidate=paths(max_segments=5), repeats=st.integers(min_value=1, max_value=5))
    def test_a_run_of_recursive_wildcards_matches_everything(self, candidate, repeats):
        self.assertIs(_match_or_none(candidate, "/".join(["**"] * repeats)), True)

    def test_repeating_a_recursive_wildcard_does_not_multiply_the_work(self):
        """Collapsing is a work guarantee, not only a semantic one.

        Every verdict is identical whether or not adjacent ``**`` segments
        collapse, because ``**`` already matches zero or more segments. What
        collapsing buys is a state space that stays flat, so the cost has to be
        asserted separately or the guarantee is untested.
        """
        candidate = "a/b/c/d/e/f"
        baseline = _steps(candidate, "**")
        for repeats in (2, 4, 8, 16, 32):
            with self.subTest(repeats=repeats):
                cost = _steps(candidate, "/".join(["**"] * repeats))
                self.assertLessEqual(cost, baseline + 4 * repeats)

    def test_repeating_a_star_does_not_multiply_the_work(self):
        candidate = "xabcdefghy"
        baseline = _steps(candidate, "x*y")
        for repeats in (2, 4, 8, 16, 32):
            with self.subTest(repeats=repeats):
                cost = _steps(candidate, "x" + "*" * repeats + "y")
                self.assertLessEqual(cost, baseline + 4 * repeats)


class BoundednessTests(unittest.TestCase):
    """OBL-GLOBS-020 and OBL-GLOBS-021: the work budget may only ever error."""

    @PROFILE
    @given(pattern=patterns(), candidate=paths(max_segments=4))
    def test_matching_returns_a_boolean_or_refuses(self, pattern, candidate):
        """No other exception type may escape, and nothing may hang."""
        try:
            self.assertIsInstance(_match_path_glob(candidate, pattern), bool)
        except GuardrailError:
            pass

    @PROFILE
    @given(
        pattern=patterns(),
        candidate=paths(max_segments=4),
        allowance=st.integers(min_value=1, max_value=60),
    )
    def test_a_tighter_budget_never_turns_a_match_into_a_miss(
        self, pattern, candidate, allowance
    ):
        """Exhaustion must convert a verdict into an error, never into False."""
        unbudgeted = _match_or_none(candidate, pattern)
        assume(unbudgeted is not None)

        spent: List[int] = []

        def consumer(amount: int) -> None:
            spent.append(amount)
            if sum(spent) > allowance:
                raise GuardrailError("test budget exhausted")

        try:
            budgeted = _match_path_glob(candidate, pattern, _step_consumer=consumer)
        except GuardrailError:
            return
        self.assertEqual(budgeted, unbudgeted)

    def test_the_documented_caps_are_not_jointly_sufficient_and_fail_closed(self):
        """A pattern and candidate both inside every cap can still exceed the budget.

        MAX_GLOB_METACHARACTERS_PER_SEGMENT allows 256 wildcards in one segment
        and MAX_GLOB_PATH_BYTES allows a 64 KiB candidate. Running the NFA over
        that pair needs millions of steps against a MAX_GLOB_MATCH_STEPS of
        100,000, so the two caps do not bound the budget between them. What
        matters is which way it fails: it must be the documented guardrail
        error, promptly, and never a wrong boolean.
        """
        segment = "*?" * (MAX_GLOB_METACHARACTERS_PER_SEGMENT // 2)
        self.assertEqual(
            sum(segment.count(character) for character in "*?["),
            MAX_GLOB_METACHARACTERS_PER_SEGMENT,
        )
        candidate = "a" * (MAX_GLOB_PATH_BYTES - 1)
        for matcher in (_match_path_glob, _match_text_glob):
            with self.subTest(matcher=matcher.__name__):
                started = time.monotonic()
                with self.assertRaises(GuardrailError):
                    matcher(candidate, segment)
                self.assertLess(time.monotonic() - started, 2.0)

    def test_a_cap_legal_pair_returns_a_correct_boolean_or_refuses(self):
        """The same disjunction on inputs the budget does cover."""
        candidate = "a" * (MAX_GLOB_PATH_BYTES - 1)
        for pattern, expected in (
            ("[ab]" * 128, False),
            ("a" * (MAX_GLOB_PATH_BYTES - 1), True),
            ("*", True),
            ("*b", False),
        ):
            with self.subTest(pattern=pattern[:16]):
                try:
                    self.assertIs(_match_path_glob(candidate, pattern), expected)
                except GuardrailError:
                    pass

    def test_a_pattern_that_would_backtrack_exponentially_still_terminates(self):
        """The shape that kills a naive matcher: many stars, no match at the end."""
        for stars in (6, 10, 16, 24):
            with self.subTest(stars=stars):
                pattern = "a" + "*a" * stars + "b"
                candidate = "a" * 200
                started = time.monotonic()
                try:
                    self.assertIs(_match_path_glob(candidate, pattern), False)
                except GuardrailError:
                    pass
                self.assertLess(time.monotonic() - started, 2.0)

    @PROFILE
    @given(pattern=patterns(), candidate=paths(max_segments=4))
    def test_the_step_consumer_sees_every_charged_step(self, pattern, candidate):
        charged: List[int] = []
        try:
            _match_path_glob(
                candidate, pattern, _step_consumer=lambda amount: charged.append(amount)
            )
        except GuardrailError:
            return
        self.assertTrue(all(amount == 1 for amount in charged), charged)


class AbsoluteAndEmptyInputTests(unittest.TestCase):
    """Invalid candidates do not match; invalid declarations fail closed."""

    @PROFILE
    @given(candidate=paths(), pattern=patterns())
    def test_a_leading_separator_never_matches(self, candidate, pattern):
        self.assertIs(_match_or_none("/" + candidate, pattern), False)
        self.assertIsNone(_match_or_none(candidate, "/" + pattern))

    @PROFILE
    @given(candidate=paths(), pattern=patterns())
    def test_an_empty_segment_never_matches(self, candidate, pattern):
        self.assertIs(_match_or_none(candidate + "//x", pattern), False)
        self.assertIsNone(_match_or_none(candidate, pattern + "//x"))

    def test_dot_segments_in_a_pattern_fail_closed(self):
        for pattern in (".", "..", "a/./b", "a/../b"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(_match_or_none("a/b", pattern))

    def test_a_wildcard_never_stands_in_for_an_empty_segment(self):
        """A pattern that would happily match "" must still reject a//x.

        The rejection has to happen when the candidate is validated. A test
        that only tries patterns which fail anyway cannot tell the two apart.
        """
        for pattern in ("a/*/x", "a/**/x", "a/?*/x", "a/[ab]*/x", "*/*/*"):
            with self.subTest(pattern=pattern):
                self.assertIs(_match_or_none("a//x", pattern), False)

    @PROFILE
    @given(pattern=patterns())
    def test_a_star_segment_matches_no_empty_segment_anywhere(self, pattern):
        assume("/" in pattern)
        self.assertIs(_match_or_none("//", pattern), False)
        self.assertIs(_match_or_none("a//b", pattern), False)


if __name__ == "__main__":
    unittest.main()
