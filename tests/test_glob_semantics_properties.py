"""Property-based tests for the glob semantics the first module cannot reach.

``tests/test_glob_properties.py`` already covers five obligations against this
subsystem, and its header names the two oracles the area has. This module
extends the same approach to six obligations it does not cover, and it takes a
harder line on which of those two oracles is worth anything where.

For everything that happens inside a bracket expression the only real oracle is
``fnmatch.fnmatchcase``. CPython compiles a shell class through
``fnmatch.translate`` into the ``re`` engine; boundver's
``_compile_glob_class`` never builds a regex, normalizing the class into chunks
and emitting code-point intervals for a hand-written NFA instead. Two
mechanisms, one published specification, so agreement is evidence. The second
oracle the older module advertises — that boundver's two matchers must agree
when no separator is present — is worthless here, and saying so is the point.
``_match_path_glob`` compiles each segment by calling the very
``_compile_text_glob`` that ``_match_text_glob`` calls, so both matchers share
``_compile_glob_class`` in its entirety and any class bug is present in both by
construction. That invariant returns True while both are wrong together.

Two obligations need a different authority. OBL-GLOBS-019 was described here
as a differential between Python and Git's C pathspec matcher, and that
description was false. A ``_SourceAccessor`` captures a Git snapshot when it
is constructed, so ``ctx.list_files`` resolves the declaration against that
snapshot through ``_snapshot_files`` — a Python whole-segment prefix rule,
the same rule ``_expand_component_paths`` applies — and never hands the
declaration to Git at all. Wrapping ``_list_files_for_source`` in a counter
records zero calls from ``list_files`` and three from
``_expand_component_paths``, one per source, each passing the *component*
path rather than the declaration. Two runs of one Python rule cannot
disagree, so a fault written into both sides at once used to pass the
equality that was supposed to be the differential; what caught such faults
was the hand-written expectation next to it, which was never described as
carrying the section. Git is now asked directly as well:
``_git_pathspec_selection`` runs ``git ls-tree`` or ``git ls-files`` with the
declaration as a ``--literal-pathspecs`` pathspec, so the third opinion is
the C matcher and holds none of boundver's code.

OBL-GLOBS-036 gets a weaker authority: a reference matcher written in this
file that splits on ``/`` and decides each segment with fnmatch. Its
per-segment verdict is fnmatch's, but the segment splitting is a restatement
of the rule under test, so it cannot catch a shared misunderstanding of what
a segment is — only a bug in the NFA wrapped around it.

Covers OBL-GLOBS-017, OBL-GLOBS-019, OBL-GLOBS-028, OBL-GLOBS-035,
OBL-GLOBS-036 and OBL-GLOBS-046.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st

from boundver import _git
from boundver._config import _expand_component_paths
from boundver._lockfile import _SourceAccessor
from boundver._utils import (
    MAX_DECLARED_PATH_BYTES,
    MAX_GLOB_METACHARACTERS_PER_SEGMENT,
    MAX_GLOB_PATH_BYTES,
    MAX_GLOB_SEGMENTS,
    GuardrailError,
    _compile_path_glob,
    _compile_text_glob,
    _is_glob,
    _match_path_glob,
    _match_text_glob,
    _normalize_declared_path,
)
from boundver.providers import _component_relative_path, _join_repo_path
from tests._repo_fixtures import commit_all, init_git_repo

PROFILE = settings(
    max_examples=400,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)

#: Every comparison against a real repository costs a Git subprocess, measured
#: at about 25 ms. The fixtures are built once per class; only the declaration
#: is drawn, and the example count is set so the whole class stays a few
#: seconds rather than ten minutes.
GIT_PROFILE = settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
)

#: A path segment may hold anything except a separator. Surrogates are excluded
#: because the matcher deliberately refuses them (see the OBL-GLOBS-028
#: carve-outs below) and control characters because they make failures
#: unreadable without buying any grammar coverage.
WIDE_CHARACTERS = st.characters(
    blacklist_characters="/", blacklist_categories=("Cs", "Cc")
)


def _text_or_none(candidate: object, pattern: object) -> Optional[bool]:
    """The text matcher's verdict, or None when a guardrail refused it."""
    try:
        return _match_text_glob(candidate, pattern)
    except GuardrailError:
        return None


def _path_or_none(candidate: object, pattern: object) -> Optional[bool]:
    """The segment-aware matcher's verdict, or None when refused."""
    try:
        return _match_path_glob(candidate, pattern)
    except GuardrailError:
        return None


# ---------------------------------------------------------------------------
# OBL-GLOBS-017 - the hyphen's role in a bracket expression
# ---------------------------------------------------------------------------

#: Endpoints for a constructed range. Every neighbouring pair differs by at
#: least two code points, so the gap between two ranges built from this pool is
#: never empty, and the pool stops below U+D800 so no gap can contain a
#: surrogate. It starts at '3' rather than '0' so the character just below the
#: lowest endpoint is never '/', which the segment-aware matcher refuses as a
#: candidate for reasons that have nothing to do with character classes.
CLASS_ENDPOINTS = "369ACFILORUXZ_acfiloruxzµÀÉÎÕàéîõĈΩЖ✓"

#: Where a candidate sits relative to the compiled intervals. Drawing the
#: candidate independently of the class reaches the gap about twice in four
#: hundred examples, because a randomly drawn range is enormous and the gap is
#: a rounding error. Deriving one representative per region from the same
#: endpoints reaches every region on every example instead.
CLASS_REGIONS = (
    "below-first",
    "first-low-endpoint",
    "inside-first",
    "first-high-endpoint",
    "gap",
    "second-low-endpoint",
    "inside-second",
    "second-high-endpoint",
    "above-last",
    "literal-hyphen",
)


def _region_candidates(bounds: Tuple[str, ...]) -> Dict[str, str]:
    """One representative candidate per region of a multi-range class."""
    points = [ord(character) for character in bounds]
    candidates = {
        "below-first": chr(points[0] - 1),
        "first-low-endpoint": chr(points[0]),
        "inside-first": chr((points[0] + points[1]) // 2),
        "first-high-endpoint": chr(points[1]),
        "second-low-endpoint": chr(points[2]),
        "inside-second": chr((points[2] + points[3]) // 2),
        "second-high-endpoint": chr(points[3]),
        "above-last": chr(points[-1] + 1),
        "literal-hyphen": "-",
    }
    # The gap between the first two ranges is the region the field report
    # describes going wrong: 'w' in `[a-cx-z]`, excluded by both ranges and
    # matched only by a compiler that widened them into one span.
    candidates["gap"] = chr((points[1] + points[2]) // 2)
    if len(points) > 4:
        candidates["second-gap"] = chr((points[3] + points[4]) // 2)
        candidates["inside-third"] = chr((points[4] + points[5]) // 2)
    return candidates


@st.composite
def disjoint_range_bounds(draw) -> Tuple[Tuple[str, ...], bool]:
    """Endpoints for two or three genuinely disjoint ranges, plus negation."""
    range_count = draw(st.integers(min_value=2, max_value=3))
    indices = draw(
        st.lists(
            st.integers(min_value=0, max_value=len(CLASS_ENDPOINTS) - 1),
            min_size=2 * range_count,
            max_size=2 * range_count,
            unique=True,
        )
    )
    indices.sort()
    bounds = tuple(CLASS_ENDPOINTS[index] for index in indices)
    return bounds, draw(st.booleans())


def _multi_range_pattern(bounds: Tuple[str, ...], negated: bool) -> str:
    body = "".join(
        f"{bounds[index]}-{bounds[index + 1]}"
        for index in range(0, len(bounds), 2)
    )
    return "[" + ("!" if negated else "") + body + "]"


#: Every hyphen position fnmatch assigns a role to, plus the two shapes that
#: are the only reachable way to put a normalized range separator at the front
#: of a class. `[c-a]` normalizes to an empty class and `[a--c]` to `[c]`.
HYPHEN_POSITION_SHAPES = (
    "[-a-c]",
    "[a-c-]",
    "[a-b-c]",
    "[a--c]",
    "[c-a]",
    "[a-cx-z]",
    "[!a-cx-z]",
    "[!-a-c]",
    "[a-!!-a]",
    "[a--!-a]",
    "[--ac-x]",
    "[---!--]",
    "[0-9a-fA-F]",
    "[a-c",
    "[]-a]",
    "[!]-a]",
    "[a-c]-]",
)

#: The characters the table is scanned against to build its candidate pool.
#: Printable ASCII covers every endpoint, gap and metacharacter the shapes
#: mention; the tail adds Latin, Greek, Cyrillic, symbol and astral code points
#: so a shape whose behaviour changed above U+007F would still be separated.
HYPHEN_SCAN_CHARACTERS = tuple(
    character
    for character in (
        [chr(point) for point in range(0x20, 0x7F)]
        + list("µÀÉàéîõĈΩЖ✓中\U0001f600")
    )
    if character != "/"
)


def _verdict_vector(candidate: str) -> Tuple[bool, ...]:
    """What the whole hyphen table says about one candidate."""
    return tuple(
        fnmatch.fnmatchcase(candidate, pattern)
        for pattern in HYPHEN_POSITION_SHAPES
    )


def _hyphen_shape_representatives() -> Tuple[str, ...]:
    """One candidate per answer the table is capable of giving.

    Measured before this pool existed: of three thousand candidates drawn from
    ``WIDE_CHARACTERS``, 2911 produced the verdict vector of a generic
    non-member and only 89 landed anywhere the seventeen shapes can tell two
    answers apart — three per cent, about twelve informative draws in a
    four-hundred-example run. The table itself only distinguishes eleven
    vectors over the scan above, so keeping one representative of each reaches
    every distinguishable answer instead, and the property runs all eleven on
    every example. Deriving the pool from the table rather than typing it out
    means a new row cannot leave its own discriminating characters
    unreachable.
    """
    seen: Dict[Tuple[bool, ...], str] = {}
    for candidate in HYPHEN_SCAN_CHARACTERS:
        seen.setdefault(_verdict_vector(candidate), candidate)
    return tuple(sorted(seen.values()))


HYPHEN_SHAPE_REPRESENTATIVES = _hyphen_shape_representatives()


class HyphenRoleTests(unittest.TestCase):
    """OBL-GLOBS-017: a hyphen takes exactly the role its position gives it."""

    @PROFILE
    @given(specification=disjoint_range_bounds())
    @example(specification=(("a", "c", "x", "z"), False))
    @example(specification=(("a", "c", "x", "z"), True))
    @example(specification=(("0", "9", "A", "F", "a", "f"), False))
    @example(specification=(("a", "Ĉ", "Ω", "✓"), False))
    @example(specification=(("µ", "õ", "Ĉ", "Ж"), True))
    def test_disjoint_ranges_accept_both_ends_and_reject_the_gap(
        self, specification
    ):
        """A class of two ranges must be two ranges, not one span or one range.

        Asserting only that ``[a-cx-z]`` selects 'a' cannot tell a correct
        compiler from one that honours the first range separator and drops the
        second, nor from one that widens the pair into a single ``a-z``. The
        first is caught by the second range's interior, the second by the gap
        between them, so both directions have to be asserted on every example.

        Covers OBL-GLOBS-017 through both shipped entry points.
        """
        bounds, negated = specification
        pattern = _multi_range_pattern(bounds, negated)
        candidates = _region_candidates(bounds)
        # Coverage is asserted, not hoped for: the gap between two ranges is
        # reached on every example rather than a couple of times in four
        # hundred, which is what an independently drawn candidate manages.
        self.assertLessEqual(set(CLASS_REGIONS), set(candidates))
        for region, candidate in candidates.items():
            expected = fnmatch.fnmatchcase(candidate, pattern)
            self.assertEqual(
                _text_or_none(candidate, pattern),
                expected,
                f"{region}: text matcher on {pattern!r} against {candidate!r}",
            )
            self.assertEqual(
                _path_or_none(candidate, pattern),
                expected,
                f"{region}: path matcher on {pattern!r} against {candidate!r}",
            )

    def test_the_candidate_pool_reaches_every_answer_the_table_can_give(self):
        """The reach claim under the property below, asserted rather than hoped.

        A pattern table with an inert candidate generator is a table test
        wearing a property's clothes. Three things are pinned here. The pool
        separates every verdict vector the whole scan separates, so nothing
        the table can distinguish is unreachable. Each member has a distinct
        vector, so no single answer accounts for the pool the way the generic
        non-member accounted for 97 per cent of the old draws. And each shape
        is matched and rejected by the pool wherever any character can do
        either, so no row is reduced to one half of its meaning.

        Covers OBL-GLOBS-017 (coverage of the property below).
        """
        scanned = {_verdict_vector(character)
                   for character in HYPHEN_SCAN_CHARACTERS}
        pooled = {_verdict_vector(character)
                  for character in HYPHEN_SHAPE_REPRESENTATIVES}
        self.assertEqual(pooled, scanned)
        self.assertEqual(len(HYPHEN_SHAPE_REPRESENTATIVES), len(scanned))
        self.assertGreaterEqual(len(scanned), 8)
        for pattern in HYPHEN_POSITION_SHAPES:
            with self.subTest(pattern=pattern):
                for verdict in (True, False):
                    reachable = any(
                        fnmatch.fnmatchcase(character, pattern) is verdict
                        for character in HYPHEN_SCAN_CHARACTERS
                    )
                    pooled_verdict = any(
                        fnmatch.fnmatchcase(character, pattern) is verdict
                        for character in HYPHEN_SHAPE_REPRESENTATIVES
                    )
                    self.assertEqual(
                        pooled_verdict,
                        reachable,
                        f"{pattern!r} can be {verdict} for some character but "
                        f"not for any representative",
                    )

    @PROFILE
    @given(candidate=WIDE_CHARACTERS)
    @example(candidate="-")
    @example(candidate="y")
    @example(candidate="w")
    @example(candidate="d")
    @example(candidate="v")
    @example(candidate="!")
    @example(candidate="]")
    @example(candidate="^")
    def test_every_hyphen_position_shape_agrees_with_fnmatch(self, candidate):
        """The four positions, both collapsing shapes, and the reject side.

        A leading hyphen and a trailing hyphen are literals, and the code that
        makes them so is the chunk normalizer rather than the range-separator
        guard downstream of it: for ``[-a-c]`` and ``[a-c-]`` the hyphen in
        question never becomes a separator unit at all. The guard is reached
        only by ``[a-!!-a]`` and ``[a--!-a]``, where removing a reversed range
        exposes a ``!`` at the front, so both families need rows.

        Every representative runs on every example, alongside the drawn
        character. Two cheaper arrangements were measured and rejected: a
        drawn character alone repeats the modal verdict vector 97.0 per cent
        of the time, and ``one_of(sampled_from(pool), WIDE_CHARACTERS)`` is
        worse than it looks — the pool holds eleven values, Hypothesis will
        not keep re-offering a branch it has exhausted, and only 0.7 per cent
        of three thousand draws came back from it. Running the pool outright
        costs about three and a half seconds a run and reaches every
        distinguishable answer on every example, which is what the
        disjoint-range property above already does with its regions. The drawn
        character is still judged, so the claim stays "for every character"
        rather than "for eleven of them".

        Covers OBL-GLOBS-017.
        """
        for value in (*HYPHEN_SHAPE_REPRESENTATIVES, candidate):
            for pattern in HYPHEN_POSITION_SHAPES:
                expected = fnmatch.fnmatchcase(value, pattern)
                self.assertEqual(
                    _text_or_none(value, pattern),
                    expected,
                    f"text matcher on {pattern!r} against {value!r}",
                )
                self.assertEqual(
                    _path_or_none(value, pattern),
                    expected,
                    f"path matcher on {pattern!r} against {value!r}",
                )

    @PROFILE
    @given(name=st.text(alphabet="abcdvwxyzy-", min_size=1, max_size=5))
    @example(name="y")
    @example(name="w")
    def test_a_two_range_class_reaches_the_matcher_through_a_declaration(
        self, name
    ):
        """The field-report shape, judged as a declared boundary path.

        ``api/[a-cx-z]*.yaml`` passes ``_normalize_declared_path`` and the
        schema's relativePath pattern, so this is a configuration a user can
        write. The oracle stays fnmatch, applied to the last segment, because
        the pattern's separator is literal and both sides split on it.

        Covers OBL-GLOBS-017 at the entry point declarations actually use.
        """
        leaf = name + ".yaml"
        expected = fnmatch.fnmatchcase(leaf, "[a-cx-z]*.yaml")
        self.assertIs(
            _path_or_none("api/" + leaf, "api/[a-cx-z]*.yaml"),
            expected,
            f"api/{leaf} against api/[a-cx-z]*.yaml",
        )
        self.assertIs(
            _path_or_none("api/other/" + leaf, "api/[a-cx-z]*.yaml"),
            False,
            "a two-range class must not let the pattern cross a separator",
        )


# ---------------------------------------------------------------------------
# OBL-GLOBS-028 and OBL-GLOBS-035 - the bracket grammar against fnmatch
# ---------------------------------------------------------------------------

#: Members drawn into a generated class body. Every one of them is a character
#: the grammar treats specially somewhere: ']' terminates unless it is first,
#: '!' negates only in first position, '^' is an ordinary member rather than a
#: negation marker, '-' is a range operator only between two members, and '['
#: is a plain literal inside a class.
CLASS_MEMBERS = "ab!]^[-0"

#: Candidates chosen so the bracket metacharacters appear on both sides. A
#: generator that pairs class patterns only with letters loses '-', which is
#: the single candidate that distinguishes `[a-b-c]` from a compiler that
#: misreads the second hyphen.
CLASS_CANDIDATES = tuple("ab!]^[-0cz*?A") + ("", "a]", "[]", "ab", "^]")


@st.composite
def class_patterns(draw) -> str:
    """One bracket expression, built as structure rather than as text.

    Uniform text over a metacharacter alphabet finds the shapes that matter
    only after thousands of draws it will not get, and never reaches a
    reversed range followed by surviving content, which is the region where
    the normalizer's splice is observable at all. Building the body from
    explicit literal, forward-range and reversed-range parts reaches it on
    every other example.

    ``^`` gets a slot of its own immediately after the negation marker and
    before the literal ``]``. It is an ordinary member of a shell class, but
    it is a negation marker in a regex, and the difference is observable only
    when a ``]`` follows it directly — see the ``[^]`` rows below.
    """
    parts: List[str] = []
    if draw(st.booleans()):
        parts.append("!")
    if draw(st.booleans()):
        parts.append("^")
    if draw(st.booleans()):
        parts.append("]")
    for _ in range(draw(st.integers(min_value=0, max_value=4))):
        kind = draw(st.integers(min_value=0, max_value=3))
        if kind == 0:
            parts.append(draw(st.sampled_from(CLASS_MEMBERS)))
        elif kind == 3:
            parts.append("-")
        else:
            low = draw(st.sampled_from(CLASS_MEMBERS))
            high = draw(st.sampled_from(CLASS_MEMBERS))
            if kind == 1:
                low, high = min(low, high), max(low, high)
            else:
                # A reversed range is deleted by splicing its neighbours
                # together, which is observable only when content survives it.
                low, high = max(low, high), min(low, high)
            parts.append(f"{low}-{high}")
    body = "".join(parts)
    if draw(st.booleans()):
        return "[" + body + "]"
    # An unclosed '[' is a literal, and where the scan decides a class ends has
    # to agree before any hyphen rule is reachable.
    return "[" + body


class BracketGrammarAgreementTests(unittest.TestCase):
    """OBL-GLOBS-028: the whole bracket grammar, against fnmatch."""

    @PROFILE
    @given(pattern=class_patterns(), candidate=st.sampled_from(CLASS_CANDIDATES))
    @example(pattern="[a--!]", candidate="a")
    @example(pattern="[a--!]", candidate="!")
    @example(pattern="[a-!!-a]", candidate="^")
    @example(pattern="[a-!!-a]", candidate="-")
    @example(pattern="[a-!!-a]", candidate="a")
    @example(pattern="[a--c]", candidate="b")
    @example(pattern="[a--c]", candidate="c")
    @example(pattern="[a-b-c]", candidate="-")
    @example(pattern="[a-cd-f]", candidate="e")
    @example(pattern="[--!!]", candidate="!")
    @example(pattern="[b-a!]", candidate="!")
    @example(pattern="[]]", candidate="]")
    @example(pattern="[]a]", candidate="a")
    @example(pattern="[!]]", candidate="]")
    @example(pattern="[!]]", candidate="a")
    @example(pattern="[!]", candidate="a")
    @example(pattern="[^]", candidate="a")
    # The three rows that separate '^' from '!' in the scanner. The rows above
    # and below cannot: skipping a '^' after '[' changes nothing unless the
    # very next character is ']', so `[^a]` and `[^]` against 'a' give the
    # same answer either way, and every '^' row the suite had was one of those.
    @example(pattern="[^]", candidate="^")
    @example(pattern="[^]]", candidate="^]")
    @example(pattern="[^]a]", candidate="^")
    @example(pattern="[^a]", candidate="a")
    @example(pattern="[abc]", candidate="b")
    @example(pattern="[!abc]", candidate="b")
    @example(pattern="[-a]", candidate="-")
    @example(pattern="[a-]", candidate="-")
    def test_a_bracket_expression_agrees_with_fnmatch(self, pattern, candidate):
        """The one oracle here is fnmatch; the two-matcher invariant is blind.

        ``_match_path_glob`` reaches ``_compile_glob_class`` by calling
        ``_compile_text_glob`` per segment, so a wrong interval is present in
        both matchers at once and their agreement proves nothing about the
        class grammar. Only the differential can fail.

        ``[a--!]`` and ``[a-!!-a]`` earn their rows: a reversed range is
        removed by splicing what survives it onto its neighbour, and those two
        are the shapes where the splice changes the answer.

        The ``[^]`` family earns its rows for a different reason. A scanner
        that treats ``^`` as a negation marker, which is what a regex would
        do, is invisible everywhere except where a ``]`` follows the ``^``
        immediately: the marker skip exists only so that a ``]`` in first
        class position stays a literal. That fault was injected and it
        survived this module, tests/test_glob_properties.py,
        tests/test_runtime_regressions.py, tests/test_declared_path_grammar.py,
        tests/test_path_layer_agreement.py and
        tests/test_config_digest_projection.py together. A search for a
        ``^``-first class across the repository turns up three rows in total —
        ``[^a]`` against ``^``, ``[^]`` against ``a``, ``[^a]`` against ``a``
        — and every one of them gives the same answer with the fault as
        without it.

        Covers OBL-GLOBS-028.
        """
        self.assertEqual(
            _text_or_none(candidate, pattern),
            fnmatch.fnmatchcase(candidate, pattern),
            f"pattern {pattern!r} against {candidate!r}",
        )

    @PROFILE
    @given(
        pattern=class_patterns(),
        candidate=st.sampled_from(CLASS_CANDIDATES),
    )
    def test_the_two_matchers_agree_on_a_class_because_they_are_one_matcher(
        self, pattern, candidate
    ):
        """A recorded blind spot, with the reason for it asserted.

        A mutation run over twenty-nine injected faults recorded no kills at
        all for the equality below, which is what the mechanism predicts: both
        matchers compile a class through ``_compile_glob_class``, so a wrong
        interval is wrong identically on both sides and the two verdicts move
        together whatever the intervals say.

        An assertion that cannot fail is not worth keeping, and the sentence
        that used to justify this one — that a refactor giving the segment
        matcher its own class compiler could not diverge unnoticed — was not
        true either, since a second compiler carrying the same misreading
        would still agree. So the structural fact is what gets asserted. For a
        single-segment class pattern the path matcher's whole compiled program
        is the text compiler's token tuple, wrapped in nothing but the one
        segment that holds it. That assertion can fail, and it fails exactly
        when the two matchers stop sharing a class compiler — the day this
        blind spot moves and the equality below starts meaning something.

        Covers OBL-GLOBS-028 (negative result, and the reason for it).
        """
        assume("/" not in candidate and candidate != "")
        text = _text_or_none(candidate, pattern)
        path = _path_or_none(candidate, pattern)
        self.assertEqual(text, path, f"{pattern!r} against {candidate!r}")

        compiled = _compile_path_glob(pattern)
        self.assertIsNotNone(compiled, f"{pattern!r} failed to compile")
        self.assertEqual(
            compiled.parts,
            (("glob", _compile_text_glob(pattern, lambda: None)),),
            f"the two matchers no longer share a class compiler for {pattern!r}",
        )

    def test_input_past_a_documented_cap_is_refused_rather_than_answered(self):
        """The first carve-out: a bounded matcher may error instead of answering.

        The caps are measured in UTF-8 bytes and refuse strictly above the
        documented boundary, not at it. The metacharacter cap is per
        ``/``-delimited segment, so three segments of 256 are accepted while
        one segment of 257 is not.

        Covers OBL-GLOBS-028 (guardrail carve-out).
        """
        rows = (
            ("candidate at the cap", "a" * MAX_GLOB_PATH_BYTES, "b", False),
            (
                "candidate one byte past",
                "a" * (MAX_GLOB_PATH_BYTES + 1),
                "b",
                GuardrailError,
            ),
            (
                "multi-byte candidate at the cap",
                "é" * (MAX_GLOB_PATH_BYTES // 2),
                "b",
                False,
            ),
            (
                "multi-byte candidate past the cap",
                "é" * (MAX_GLOB_PATH_BYTES // 2 + 1),
                "b",
                GuardrailError,
            ),
            ("pattern at the cap", "a", "a" * MAX_DECLARED_PATH_BYTES, False),
            (
                "pattern one byte past",
                "a",
                "a" * (MAX_DECLARED_PATH_BYTES + 1),
                GuardrailError,
            ),
            (
                "metacharacters at the cap",
                "a",
                "?" * MAX_GLOB_METACHARACTERS_PER_SEGMENT,
                False,
            ),
            (
                "metacharacters one past",
                "a",
                "?" * (MAX_GLOB_METACHARACTERS_PER_SEGMENT + 1),
                GuardrailError,
            ),
            (
                "the cap is per segment",
                "a/b/c",
                "/".join(["?" * MAX_GLOB_METACHARACTERS_PER_SEGMENT] * 3),
                False,
            ),
        )
        for label, candidate, pattern, expected in rows:
            with self.subTest(label=label):
                if expected is GuardrailError:
                    with self.assertRaises(GuardrailError):
                        _match_text_glob(candidate, pattern)
                    # fnmatch answers every one of these, which is what makes
                    # the refusal a carve-out rather than a disagreement.
                    self.assertIsInstance(
                        fnmatch.fnmatchcase(candidate, pattern), bool
                    )
                else:
                    self.assertIs(_match_text_glob(candidate, pattern), expected)

    @PROFILE
    @given(
        value=st.one_of(
            st.integers(),
            st.none(),
            st.binary(max_size=4),
            st.lists(st.text(max_size=2), max_size=2),
        )
    )
    def test_a_non_string_argument_is_false_rather_than_a_type_error(self, value):
        """The second carve-out: fnmatch raises TypeError here, boundver does not.

        Covers OBL-GLOBS-028 (type carve-out).
        """
        self.assertIs(_match_text_glob(value, "*"), False)
        self.assertIs(_match_text_glob("a", value), False)

    @PROFILE
    @given(codepoint=st.integers(min_value=0xDC80, max_value=0xDCFF))
    def test_a_git_escaped_candidate_byte_still_matches_a_wildcard(self, codepoint):
        """The candidate is encoded with surrogateescape, so this range works.

        Git hands boundver paths whose undecodable bytes surface exactly as
        U+DC80 through U+DCFF, and those candidates must keep matching. The
        pattern is encoded strictly, so the same character in a pattern does
        not — that asymmetry is the divergence recorded below.

        Covers OBL-GLOBS-028 (surrogate carve-out, the half that works).
        """
        character = chr(codepoint)
        self.assertIs(_match_text_glob(character, "*"), True)
        self.assertIs(_match_text_glob(character, "?"), True)
        self.assertIs(_match_text_glob("a" + character, "a*"), True)

    @PROFILE
    @given(codepoint=st.integers(min_value=0xD800, max_value=0xDFFF))
    def test_a_surrogate_in_the_pattern_fails_closed(self, codepoint):
        """Invalid pattern text must not quietly omit files from a digest."""
        character = chr(codepoint)
        pattern = "a" + character + "b"
        with self.assertRaises(GuardrailError):
            _match_text_glob(pattern, pattern)


# ---------------------------------------------------------------------------
# OBL-GLOBS-035 - the hand-rolled normalization, on this interpreter
# ---------------------------------------------------------------------------

#: Characters outside the BMP and outside any small ASCII alphabet, so a bug
#: conditioned on a wide code point is visible. Ranges built from these are
#: ordered by construction.
WIDE_CLASS_POOL = "µéĈЖ✓中\U0001f600\U0001f64f\U0001f680"


@st.composite
def single_segment_patterns(draw) -> str:
    """One pattern that never contains a separator, over a wide alphabet."""
    pieces: List[str] = []
    for _ in range(draw(st.integers(min_value=1, max_value=4))):
        kind = draw(st.integers(min_value=0, max_value=4))
        if kind == 0:
            pieces.append("*")
        elif kind == 1:
            pieces.append("?")
        elif kind == 2:
            pieces.append(draw(st.sampled_from(CLASS_MEMBERS + WIDE_CLASS_POOL)))
        elif kind == 3:
            members = draw(
                st.text(
                    alphabet=CLASS_MEMBERS + WIDE_CLASS_POOL,
                    min_size=1,
                    max_size=3,
                )
            )
            pieces.append("[" + ("!" if draw(st.booleans()) else "") + members + "]")
        else:
            low = draw(st.sampled_from(WIDE_CLASS_POOL))
            high = draw(st.sampled_from(WIDE_CLASS_POOL))
            pieces.append(f"[{low}-{high}]")
    return "".join(pieces)


class ClassNormalizationStabilityTests(unittest.TestCase):
    """OBL-GLOBS-035: the hand transliteration of CPython's normalization.

    The obligation asks for agreement on Python 3.10 through 3.14. A test run
    can only speak for the interpreter running it, so what these properties
    assert is agreement with *this* host's fnmatch. That is still the right
    assertion: the normalizer exists so boundver does not inherit an
    interpreter's behaviour, and if a future CPython changes its class
    normalization these properties are what turns that into a visible failure
    rather than a silent change in which files a boundary selects.
    """

    @PROFILE
    @given(
        pattern=single_segment_patterns(),
        candidate=st.text(alphabet=CLASS_MEMBERS + WIDE_CLASS_POOL, max_size=4),
    )
    @example(pattern="[!]a]", candidate="]")
    @example(pattern="[!]a]", candidate="b")
    @example(pattern="[!-a]", candidate="-")
    @example(pattern="[!-a]", candidate="b")
    @example(pattern="[[a]]", candidate="a]")
    @example(pattern="[[a]]", candidate="[]")
    @example(pattern="[a-c", candidate="[a-c")
    @example(pattern="[]-a]", candidate="^")
    @example(pattern="[a--!-a]", candidate="b")
    @example(pattern="[a-Ĉ]", candidate="Ĉ")
    @example(pattern="[\U0001f600-\U0001f64f]", candidate="\U0001f604")
    @example(pattern="[\U0001f600-\U0001f64f]", candidate="\U0001f680")
    def test_a_single_segment_pattern_agrees_with_fnmatch(self, pattern, candidate):
        """Wide code points, nested brackets and negation with ']' first.

        The shapes the older module can reach are ASCII, because its alphabet
        is eleven characters; a class whose endpoints are astral, or whose
        first member is ']' under a negation, is unreachable there. Both are
        ordinary inputs for a declared path holding non-Latin filenames.

        Covers OBL-GLOBS-035.
        """
        assume("/" not in pattern and "/" not in candidate)
        self.assertEqual(
            _text_or_none(candidate, pattern),
            fnmatch.fnmatchcase(candidate, pattern),
            f"pattern {pattern!r} against {candidate!r}",
        )

    @PROFILE
    @given(
        low=st.sampled_from(WIDE_CLASS_POOL),
        high=st.sampled_from(WIDE_CLASS_POOL),
        negated=st.booleans(),
    )
    def test_a_wide_range_holds_at_both_endpoints_and_just_outside_them(
        self, low, high, negated
    ):
        """Endpoints, interior and the two adjacent code points, in both orders.

        Drawing the two endpoints independently means half the classes are
        reversed, which is the shape the normalizer rewrites before the range
        logic runs. Checking the code point one below and one above the range
        is what separates a correct interval from one off by one.

        Covers OBL-GLOBS-035.
        """
        pattern = "[" + ("!" if negated else "") + low + "-" + high + "]"
        points = {ord(low), ord(high), ord(low) - 1, ord(high) + 1,
                  (ord(low) + ord(high)) // 2}
        for point in sorted(points):
            if 0xD800 <= point <= 0xDFFF or point == ord("/") or point < 0:
                continue
            candidate = chr(point)
            self.assertEqual(
                _text_or_none(candidate, pattern),
                fnmatch.fnmatchcase(candidate, pattern),
                f"pattern {pattern!r} against U+{point:04X}",
            )


# ---------------------------------------------------------------------------
# OBL-GLOBS-036 and OBL-GLOBS-046 - what a separator means to each matcher
# ---------------------------------------------------------------------------


def _reference_path_match(candidate: str, pattern: str) -> bool:
    """Split on ``/``, decide each segment with fnmatch, let ``**`` span.

    The per-segment verdict is CPython's; the segment splitting is this
    file's, so this model cannot catch a shared misunderstanding of what a
    segment is. What it can catch is a fault in the NFA wrapped around the
    per-segment decision — a wildcard that consumes a separator, a ``**`` that
    refuses to match zero segments, an off-by-one in the state closure.
    """
    if candidate.startswith("/") or pattern.startswith("/"):
        return False
    if not candidate or not pattern:
        return False
    path_parts = candidate.split("/")
    raw_pattern_parts = pattern.split("/")
    if any(part == "" for part in path_parts):
        return False
    if any(part == "" for part in raw_pattern_parts):
        return False
    pattern_parts: List[str] = []
    for part in raw_pattern_parts:
        if part == "**" and pattern_parts and pattern_parts[-1] == "**":
            continue
        pattern_parts.append(part)

    def walk(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        token = pattern_parts[pattern_index]
        if token == "**":
            return any(
                walk(skip, pattern_index + 1)
                for skip in range(path_index, len(path_parts) + 1)
            )
        if path_index == len(path_parts):
            return False
        if not fnmatch.fnmatchcase(path_parts[path_index], token):
            return False
        return walk(path_index + 1, pattern_index + 1)

    return walk(0, 0)


@st.composite
def path_glob_segments(draw) -> str:
    """One pattern segment, built as structure so classes actually appear.

    Uniform text over ``ab.-*?[]!^`` was measured across three thousand draws:
    it closed a bracket expression 2.4 per cent of the time and built a
    forward range inside one exactly never. The segment-model property below
    was therefore judging character classes almost entirely in their unclosed
    form, where ``[`` is a literal and no interval is compiled at all. Naming
    the parts fixes that without narrowing the property, because the raw-text
    branch is still here.
    """
    pieces: List[str] = []
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        kind = draw(st.integers(min_value=0, max_value=6))
        if kind == 0:
            pieces.append("*")
        elif kind == 1:
            pieces.append("?")
        elif kind == 2:
            pieces.append(draw(st.sampled_from("ab.-")))
        elif kind == 3:
            low, high = sorted(
                draw(
                    st.lists(
                        st.sampled_from("ab.-0z"), min_size=2, max_size=2
                    )
                )
            )
            negation = "!" if draw(st.booleans()) else ""
            pieces.append(f"[{negation}{low}-{high}]")
        elif kind == 4:
            members = draw(st.text(alphabet="ab.-]^", min_size=1, max_size=3))
            negation = "!" if draw(st.booleans()) else ""
            pieces.append(f"[{negation}{members}]")
        elif kind == 5:
            pieces.append("[" + draw(st.text(alphabet="ab.-!", max_size=3)))
        else:
            pieces.append(draw(st.text(alphabet="ab.-*?[]!^", min_size=1,
                                       max_size=4)))
    return "".join(pieces)


def path_patterns(max_segments: int = 3) -> st.SearchStrategy:
    return st.lists(
        st.one_of(
            path_glob_segments().filter(lambda value: value not in {".", ".."}),
            st.just("**"),
        ),
        min_size=1,
        max_size=max_segments,
    ).map("/".join)


#: Code points below U+20000 whose uppercase is one single other character —
#: the whole population the case-sensitivity property is about. Drawing from
#: ``WIDE_CHARACTERS`` and filtering to this condition accepted 4.4 per cent of
#: draws (measured: 67,931 draws for three thousand accepted examples), so a
#: four-hundred-example run spent about nine thousand draws reaching four
#: hundred. Enumerating costs about sixteen milliseconds once.
CASE_VARYING_CHARACTERS = tuple(
    chr(point)
    for point in range(0x20, 0x20000)
    if not 0xD800 <= point <= 0xDFFF
    and len(chr(point).upper()) == 1
    and chr(point).upper() != chr(point)
    and chr(point).upper() != "/"
)


def multi_segment_paths(max_segments: int = 3) -> st.SearchStrategy:
    return st.lists(
        st.text(alphabet="ab.-", min_size=1, max_size=3).filter(
            lambda value: value not in {".", ".."}
        ),
        min_size=2,
        max_size=max_segments,
    ).map("/".join)


@st.composite
def segment_model_pairs(draw) -> Tuple[str, str]:
    """A pattern and a path, half drawn apart and half built together.

    Drawing the two independently is what the reject side needs, and it is
    also why three shapes the obligation names were effectively absent. A
    class token was aligned against a real segment on 2.9 per cent of
    examples. A ``**`` was observed spanning zero segments on 0.4 per cent,
    about one and a half times in a four-hundred-example run, and that is the
    case the v0.10 grammar actually got wrong. A forward range inside a class
    was built exactly never, because uniform text has to produce ``[``, two
    ordered members around a ``-``, and a ``]`` before anything else closes
    it. The built branch aligns a range against the segment it must judge and
    inserts a ``**`` that spans nothing, and the segment generator names the
    class parts, so all three now arrive often: a closed bracket expression on
    48.6 per cent of examples, a forward range on 22.4, a zero-span ``**`` on
    16.8.
    """
    candidate = draw(multi_segment_paths())
    if draw(st.booleans()):
        return draw(path_patterns()), candidate

    pattern_parts: List[str] = []
    for segment in candidate.split("/"):
        choice = draw(st.integers(min_value=0, max_value=4))
        if choice == 0:
            pattern_parts.append("*")
        elif choice == 1:
            pattern_parts.append("?" * len(segment))
        elif choice == 2:
            # A forward range that contains the segment's first character, so
            # the interval test decides a segment rather than a stray one.
            span = draw(st.integers(min_value=0, max_value=8))
            low = chr(ord(segment[0]) - span)
            high = chr(ord(segment[0]) + span)
            if "/" in (low, high):
                # A separator inside the pattern text would split the segment
                # and make the pair mean something else entirely.
                pattern_parts.append("[" + segment[0] + "]" + segment[1:])
            else:
                pattern_parts.append(f"[{low}-{high}]" + segment[1:])
        elif choice == 3:
            excluded = draw(st.sampled_from("xyz"))
            pattern_parts.append(f"[!{excluded}]" + segment[1:])
        else:
            pattern_parts.append(segment)
    if draw(st.booleans()):
        position = draw(st.integers(min_value=0, max_value=len(pattern_parts)))
        pattern_parts.insert(position, "**")
    return "/".join(pattern_parts), candidate


@st.composite
def selecting_path_pairs(draw) -> Tuple[str, str]:
    """A multi-segment path and a segment-aware pattern that must select it."""
    path_segments = draw(
        st.lists(
            st.text(alphabet="ab.-", min_size=1, max_size=3).filter(
                lambda value: value not in {".", ".."}
            ),
            min_size=2,
            max_size=3,
        )
    )
    pattern_segments: List[str] = []
    for segment in path_segments:
        choice = draw(st.integers(min_value=0, max_value=3))
        if choice == 0:
            pattern_segments.append("*")
        elif choice == 1:
            pattern_segments.append("?" * len(segment))
        elif choice == 2:
            pattern_segments.append("[" + segment[0] + "x]" + segment[1:])
        else:
            pattern_segments.append(segment)
    return "/".join(pattern_segments), "/".join(path_segments)


class SegmentGrammarTests(unittest.TestCase):
    """OBL-GLOBS-036: the published grammar, per segment."""

    @PROFILE
    @given(pair=segment_model_pairs())
    @example(pair=("a?b", "a/b"))
    @example(pair=("a[!x]b", "a/b"))
    @example(pair=("**/**", "a/b"))
    @example(pair=("a/**/b", "a/b"))
    @example(pair=("[a-c]/[a-c]", "a/b"))
    @example(pair=("[a-c]/**/[a-c]", "a/b"))
    @example(pair=("a/[!x]", "a/b"))
    def test_path_matching_follows_the_segment_model(self, pair):
        """Every verdict, against a model that splits first and matches second.

        The oracle is only half independent, and the half that is not is the
        segment splitting. It is written out here so the claim is inspectable:
        no wildcard is offered a separator, and ``**`` is the only token that
        may consume more or fewer than one segment.

        Covers OBL-GLOBS-036.
        """
        pattern, candidate = pair
        verdict = _path_or_none(candidate, pattern)
        assume(verdict is not None)
        self.assertEqual(
            verdict,
            _reference_path_match(candidate, pattern),
            f"pattern {pattern!r} against {candidate!r}",
        )

    @PROFILE
    @given(
        left=st.text(alphabet="ab.", min_size=1, max_size=3),
        right=st.text(alphabet="ab.", min_size=1, max_size=3),
    )
    def test_no_single_character_wildcard_stands_in_for_a_separator(
        self, left, right
    ):
        """'?' and a bracket class must both refuse the separator itself.

        The suite's existing cases put ``?`` and a class against candidates
        where crossing would not have changed the answer — ``api/v?.yaml``
        against v1 and vA. A candidate whose separator sits exactly under the
        wildcard is what tells the two apart, and the class forms have to
        include a negated one and a range that spans '/' in code-point order,
        since those reach the interval test rather than a literal comparison.

        Covers OBL-GLOBS-036 and OBL-GLOBS-046 (the path-glob half).
        """
        crossing = left + "/" + right
        # 'q' is outside the segment alphabet, so every wildcard below accepts
        # it except the class whose only member is the separator itself.
        control = left + "q" + right
        wildcards = (
            ("?", True),
            ("[!x]", True),
            ("[.-z]", True),
            ("[!" + right[0] + "]", True),
            ("[/]", False),
        )
        for wildcard, control_verdict in wildcards:
            pattern = left + wildcard + right
            self.assertIs(
                _path_or_none(crossing, pattern),
                False,
                f"{pattern!r} crossed a separator to match {crossing!r}",
            )
            # Without the control the assertion above would pass for a pattern
            # that matches nothing at all.
            self.assertIs(
                _path_or_none(control, pattern),
                control_verdict,
                f"{pattern!r} against the separator-free {control!r}",
            )

    @PROFILE
    @given(
        depth=st.integers(min_value=0, max_value=4),
        middle=st.text(alphabet="ab", min_size=1, max_size=2),
    )
    def test_a_recursive_wildcard_spans_zero_or_more_whole_segments(
        self, depth, middle
    ):
        """``a/**/b`` must select a direct child as well as a descendant.

        Zero is the case the v0.10 grammar got wrong, and it is the one an
        example test writes last.

        Covers OBL-GLOBS-036.
        """
        segments = ["a", *[middle] * depth, "b"]
        candidate = "/".join(segments)
        self.assertIs(_path_or_none(candidate, "a/**/b"), True, candidate)
        self.assertIs(_path_or_none(candidate, "a/**/**/b"), True, candidate)
        self.assertIs(_path_or_none(candidate, "**"), True, candidate)
        self.assertIs(
            _path_or_none(candidate, "a/" + "*/" * depth + "b"), True, candidate
        )
        if depth:
            self.assertIs(_path_or_none(candidate, "a/b"), False, candidate)

    def test_the_case_pool_holds_every_character_the_property_claims(self):
        """The pool is the population, not a sample of it.

        ``CASE_VARYING_CHARACTERS`` stops at U+20000 so it can be built in
        milliseconds. That bound is a claim about Unicode, so it is checked
        once against the whole code space rather than trusted: the highest
        cased code point is U+1E943, and a future Unicode release that added a
        cased character above the bound would fail here rather than quietly
        leave it undrawn.

        Covers OBL-GLOBS-036 (coverage of the property below).
        """
        everything = {
            chr(point)
            for point in range(0x20, 0x110000)
            if not 0xD800 <= point <= 0xDFFF
            and len(chr(point).upper()) == 1
            and chr(point).upper() != chr(point)
            and chr(point).upper() != "/"
        }
        self.assertEqual(set(CASE_VARYING_CHARACTERS), everything)
        self.assertGreater(len(CASE_VARYING_CHARACTERS), 1000)
        self.assertTrue(
            any(ord(character) > 0xFFFF for character in CASE_VARYING_CHARACTERS),
            "an astral cased character must be drawable",
        )

    @PROFILE
    @given(
        character=st.sampled_from(CASE_VARYING_CHARACTERS),
        tail=st.text(alphabet="ab", max_size=2),
    )
    def test_selection_is_case_sensitive_for_every_character(self, character, tail):
        """A case-folding host must not fold the matcher.

        The existing suite pins case sensitivity for a bare segment. What is
        untested is a case variant inside a multi-segment path, which is where
        a host filesystem's own folding would show up first.

        Two comparison sites are reached, and they are different code. A
        pattern segment holding a wildcard is compiled and compared token by
        token; a segment holding no wildcard at all never reaches the token
        matcher, because ``_compile_path_glob_with_spender`` marks it
        ``literal`` and ``_bounded_text_equal`` decides it. Folding case in
        that second site survived every property in this file, because every
        pattern any of them offered either held a wildcard or was compared
        against itself. The sibling module catches it for a bare one-segment
        pattern, so it was never escaping the suite, but the multi-segment
        claim is this property's and the wildcard-free forms belong here.

        Covers OBL-GLOBS-036.
        """
        upper = character.upper()
        lower_path = "dir/" + character + tail
        upper_path = "dir/" + upper + tail
        self.assertIs(_path_or_none(lower_path, "dir/" + character + "*"), True)
        self.assertIs(_path_or_none(upper_path, "dir/" + character + "*"), False)
        self.assertIs(
            _path_or_none(lower_path, "dir/[" + upper + "]*"),
            False,
            f"a class of {upper!r} matched {character!r}",
        )
        self.assertIs(_path_or_none(lower_path, lower_path), True)
        self.assertIs(
            _path_or_none(upper_path, lower_path),
            False,
            f"a wildcard-free {lower_path!r} selected {upper_path!r}",
        )
        self.assertIs(
            _path_or_none(lower_path, upper_path),
            False,
            f"a wildcard-free {upper_path!r} selected {lower_path!r}",
        )
        self.assertIs(
            _path_or_none("DIR/" + character + tail, lower_path),
            False,
            "a wildcard-free leading segment folded case",
        )


@st.composite
def whole_path_text_pairs(draw, separator_in_pattern: bool = True) -> Tuple[str, str]:
    """A multi-segment path and a whole-path pattern built to match it.

    The obligation is that ``/`` is an ordinary byte to the text matcher, and
    the only examples that can show it are the ones where a wildcard actually
    stands over a separator. Drawing the pattern independently reached that on
    1.7 per cent of examples — about seven times in a four-hundred-example run
    — and produced a True verdict on 3.0 per cent; the other 97 per cent was
    False agreeing with False, which any matcher that rejects everything would
    also satisfy. Consuming the candidate left to right and replacing runs of
    it with wildcards makes the pattern match by construction, separators
    included.

    With ``separator_in_pattern`` false no literal ``/`` is ever emitted, so
    the result is a separator-free pattern that matches a multi-segment path
    as text — which is exactly the input the two matchers must disagree on.
    Two ``*`` pieces are never emitted in a row, so the pattern can never come
    out as ``**``, which the segment-aware matcher would read as its recursive
    wildcard rather than as two stars.
    """
    candidate = draw(multi_segment_paths())
    pieces: List[str] = []
    index = 0
    while index < len(candidate):
        character = candidate[index]
        choices = [1, 2, 3]
        if not pieces or pieces[-1] != "*":
            choices.append(0)
        if character == "/" and not separator_in_pattern:
            choices = [choice for choice in choices if choice != 3]
        choice = draw(st.sampled_from(choices))
        if choice == 0:
            run = draw(
                st.integers(min_value=1, max_value=len(candidate) - index)
            )
            pieces.append("*")
            index += run
            continue
        if choice == 1:
            pieces.append("?")
        elif choice == 2:
            span = draw(st.integers(min_value=0, max_value=6))
            low = chr(ord(character) - span)
            high = chr(ord(character) + span)
            if "/" in (low, high):
                # A literal separator inside the class text would end the
                # segment for the path matcher and change what is being asked.
                pieces.append("[!" + draw(st.sampled_from("xyzXYZ")) + "]")
            else:
                pieces.append(f"[{low}-{high}]")
        else:
            pieces.append(character)
        index += 1
    return "".join(pieces), candidate


def whole_path_pairs_or_free_draws() -> st.SearchStrategy:
    """Half built to match, half drawn apart so the reject side stays real."""
    return st.one_of(
        whole_path_text_pairs(),
        st.tuples(
            st.text(alphabet="ab.-/*?[]!^", min_size=1, max_size=8),
            multi_segment_paths(),
        ),
    )


class WholePathTextMatcherTests(unittest.TestCase):
    """OBL-GLOBS-046: for the text matcher a separator is an ordinary byte."""

    @PROFILE
    @given(pair=whole_path_pairs_or_free_draws())
    @example(pair=("?", "/"))
    @example(pair=("[!x]", "/"))
    @example(pair=("[!/]", "/"))
    @example(pair=("a?b", "a/b"))
    @example(pair=("a[!x]b", "a/b"))
    @example(pair=("*", "a/b/c"))
    @example(pair=("a[.-z]b", "a/b"))
    def test_the_text_matcher_treats_a_separator_as_an_ordinary_character(
        self, pair
    ):
        """Migration analysis calls this "what Boundver 0.10 selected".

        If the legacy matcher stopped crossing '/', the migration report would
        understate the selection change and a user would ratchet a component
        believing nothing moved. fnmatch over the whole path is exactly what
        0.10 did, so it is both the oracle and the specification here.

        Covers OBL-GLOBS-046.
        """
        pattern, candidate = pair
        verdict = _text_or_none(candidate, pattern)
        assume(verdict is not None)
        self.assertEqual(
            verdict,
            fnmatch.fnmatchcase(candidate, pattern),
            f"pattern {pattern!r} against {candidate!r}",
        )

    @PROFILE
    @given(
        pair=st.one_of(
            whole_path_text_pairs(separator_in_pattern=False),
            st.tuples(
                st.text(alphabet="ab.-*?[]!^", min_size=1, max_size=6),
                multi_segment_paths(),
            ),
        )
    )
    @example(pair=("?", "a/b"))
    @example(pair=("[!x]", "a/b"))
    @example(pair=("*", "a/b"))
    @example(pair=("*b", "a/b"))
    @example(pair=("a[!x]b", "a/b"))
    def test_the_two_matchers_part_company_exactly_at_a_separator(self, pair):
        """A separator-free pattern can never select a multi-segment path.

        Both halves are needed. The path matcher must refuse, and the text
        matcher must go on agreeing with fnmatch, or the pair stops modelling
        the before-and-after that migration analysis reports.

        The refusal is only a claim when the text matcher says yes. Drawn
        apart, the two strings agreed on False 97.1 per cent of the time, so
        the assertion below was about twelve examples of substance per run and
        three hundred and eighty-eight of nothing. Half the pairs are now
        built so that the text matcher matches by construction while the
        pattern still holds no separator.

        Covers OBL-GLOBS-046.
        """
        pattern, candidate = pair
        assume(pattern != "**")
        assume(pattern not in {".", ".."})
        assume("/" not in pattern)
        text = _text_or_none(candidate, pattern)
        assume(text is not None)
        self.assertIs(
            _path_or_none(candidate, pattern),
            False,
            f"{pattern!r} selected the multi-segment path {candidate!r}",
        )
        self.assertEqual(text, fnmatch.fnmatchcase(candidate, pattern))

    @PROFILE
    @given(pair=selecting_path_pairs())
    def test_a_path_match_without_a_recursive_wildcard_is_also_a_text_match(
        self, pair
    ):
        """The segment-aware matcher is a restriction of the text one.

        Every ordinary wildcard the path matcher allows within one segment is
        also allowed by the text matcher, which allows more. Only ``**`` can
        break the containment, because it matches zero segments while its
        collapsed text form still demands the separator, so it is excluded and
        the pattern is built from the path instead of drawn against it — an
        independently drawn pair matches so rarely that the containment is
        never actually tested.

        Covers OBL-GLOBS-046.
        """
        pattern, candidate = pair
        self.assertIs(
            _path_or_none(candidate, pattern),
            True,
            f"{pattern!r} failed to select {candidate!r} by segment",
        )
        self.assertIs(
            _text_or_none(candidate, pattern),
            True,
            f"{pattern!r} matched {candidate!r} by segment but not as text",
        )
        self.assertTrue(fnmatch.fnmatchcase(candidate, pattern))


# ---------------------------------------------------------------------------
# OBL-GLOBS-019 - a literal declaration resolves by whole segments
# ---------------------------------------------------------------------------

#: Names chosen for the byte immediately after the prefix. '-' is 0x2D and '.'
#: is 0x2E, just under the separator; '0' and '2' are 0x30 and 0x32, just over;
#: letters are far above. A prefix comparison that forgets the separator check
#: confuses exactly these. The ASCII half of the set is the one
#: tests/test_obligation_register.py already uses for the migration analyzer's
#: copy of this rule, reused here rather than reinvented.
TRACKED_NAMES = (
    "svc/api/route.yaml",
    "svc/api/nested/deep.yaml",
    "svc/api0/w.py",
    "svc/api2/route.yaml",
    "svc/api-x/route.yaml",
    "svc/api_alt/route.yaml",
    "svc/apiv2/route.yaml",
    "svc/api.bak",
    "svc/apib",
    "svc/apifile",
    "svc/.hidden/x.yaml",
    "svc/.hiddenfile",
    "svc/src/api/q.py",
    "svc/other.yaml",
    "svc/apié/route.yaml",
    "svc/apiéx",
    "svc/with space/f.yaml",
    "svc/hash#name",
)

#: Untracked, so Git lists neither of them. They exist to prove the shared
#: candidate universe is Git's and not the filesystem's.
UNTRACKED_NAMES = ("svc/api/untracked.yaml", "svc/apiv2/untracked.yaml")

NAMED_LITERALS = (
    "api",
    "api/",
    "api//",
    "apiv",
    "apiv2",
    "api2",
    "api0",
    "api-x",
    "api_alt",
    "api.bak",
    "api.bak/",
    "apib",
    "apifile",
    "apifile/",
    "apifil",
    "ap",
    ".hidden",
    ".hidden/",
    ".hiddenfile",
    "src/api",
    "src/api/q.py",
    "other",
    "other.yaml",
    "API",
    "Api/",
    "api/route.yaml",
    "api/ROUTE.yaml",
    "api/nested",
    "api/nested/deep.yaml",
    "apié",
    "apiéx",
    "with space",
    "with space/",
    "hash#name",
)


def literal_declarations() -> st.SearchStrategy:
    """A declared literal: half hand-chosen, half grown from the name set."""
    return st.one_of(
        st.sampled_from(NAMED_LITERALS),
        st.text(alphabet="api.-_0123vxb/", min_size=1, max_size=9),
        st.sampled_from(TRACKED_NAMES).map(lambda name: name[len("svc/"):]),
        st.sampled_from(TRACKED_NAMES).flatmap(
            lambda name: st.integers(min_value=1, max_value=len(name) - 4).map(
                lambda size: name[len("svc/"):][:size]
            )
        ),
    )


def _declaration_is_acceptable(declared: str) -> bool:
    """The declared-path rule restated, so a refusal can be asserted.

    This is a restatement of the implementation and worth nothing as an oracle
    for whether the rule is *right* — the point is only the direction. Without
    it the properties below learn about a refusal by losing an example, which
    means a normalizer that started refusing valid declarations would thin the
    sample rather than fail. Restating the rule lets a refusal be a failure.

    One refusal reason is deliberately missing: ``_normalize_declared_path``
    also runs ``_validate_glob_pattern_complexity``, which rejects a wildcard
    segment past its byte or metacharacter cap. No declaration this file draws
    holds ``*``, ``?`` or ``[`` at all — none of the three alphabets contains
    one — so that branch is unreachable here, and the property below asserts
    as much rather than leaving it to be believed.
    """
    if not declared or not declared.strip() or declared != declared.strip():
        return False
    if "\\" in declared or declared.startswith("/"):
        return False
    if re.match(r"^[A-Za-z]:", declared):
        return False
    if len(declared.encode("utf-8")) > MAX_DECLARED_PATH_BYTES:
        return False
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in declared):
        return False
    normalized = declared[:-1] if declared.endswith("/") else declared
    parts = normalized.split("/")
    if len(parts) > MAX_GLOB_SEGMENTS:
        return False
    return not any(part in ("", ".", "..") for part in parts)


def acceptable_literal_declarations() -> st.SearchStrategy:
    return literal_declarations().filter(_declaration_is_acceptable)


def _git_pathspec_selection(root: Path, source: str, repo_pathspec: str) -> Set[str]:
    """What Git's own pathspec matcher selects for one literal declaration.

    This is the independent authority the section claimed to have and did not.
    ``--literal-pathspecs`` turns off Git's magic prefixes so the declaration
    is matched as a plain path, which is the same input boundver resolves, and
    the leading-directory rule that answers it is Git's C code with nothing of
    boundver's in it.

    ``ls-files --cached`` covers index and working-tree because both read the
    same tracked set; the working-tree source additionally drops tracked files
    deleted from disk, which this fixture never has, so the two remain
    comparable here and would need a third command if it ever did.
    """
    if source == "head":
        arguments = [
            "--literal-pathspecs", "ls-tree", "-r", "--name-only", "-z",
            "HEAD", "--", repo_pathspec,
        ]
    else:
        arguments = [
            "--literal-pathspecs", "ls-files", "--cached", "-z",
            "--", repo_pathspec,
        ]
    completed = subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True
    )
    return {
        entry.decode("utf-8", "surrogateescape")
        for entry in completed.stdout.split(b"\x00")
        if entry
    }


class LiteralSegmentSelectionTests(unittest.TestCase):
    """OBL-GLOBS-019: validation and generation resolve a literal identically.

    This class used to claim the two sides were a Python implementation and a
    C one. They are not. ``_SourceAccessor`` captures a Git snapshot in its
    constructor, so ``list_files`` resolves the declaration through
    ``_snapshot_files`` — ``candidate == normalized or
    candidate.startswith(normalized + "/")`` — which is the same rule
    ``_expand_component_paths`` applies a few lines of Python away. Replacing
    ``_list_files_for_source`` with a counter records zero calls from
    ``list_files`` and three from ``_expand_component_paths``, one per source,
    each passing the *component* path and not the declaration. So Git narrows
    the candidate set to the component subtree and Python does the whole
    segment rule twice.

    That matters because it decides what the equality can catch. Two faults
    were injected into ``_snapshot_files``, ``_snapshot_tracked_files`` and
    ``_expand_component_paths`` together, leaving no branch on either side
    with the old rule: one replacing the whole-segment test with a bare string
    prefix, one folding case. Against the equality alone both passed. They
    were caught only by the hand-written expectation and the case-variant
    table further down, which are this section's real teeth and were never
    described as such. ``_git_pathspec_selection`` is therefore asked as well;
    with Git in the comparison the same two faults fail the equality itself.

    Scope, and every pin here is load-bearing. The source is always explicit,
    because ``source=None`` takes a filesystem branch that sees untracked files
    the provider cannot — recorded as a divergence below. No snapshot is ever
    paired with ``working-tree``, because the two sides then read different
    fields of it, and ``_capture_operation_snapshot`` never produces that
    combination in production. The component path is a real directory, because
    ``_expand_component_paths`` builds its prefix inline and a component path
    of "." would produce the prefix "./", which no Git path carries.
    """

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        init_git_repo(cls.root, initial_branch="main")
        for name in TRACKED_NAMES:
            target = cls.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
        commit_all(cls.root, "literal selection fixture")
        for name in UNTRACKED_NAMES:
            target = cls.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
        cls.accessors = {
            source: _SourceAccessor(cls.root, source)
            for source in ("head", "index", "working-tree")
        }

    @classmethod
    def tearDownClass(cls):
        for accessor in cls.accessors.values():
            accessor.close()
        cls.accessors.clear()
        cls._directory.cleanup()

    def setUp(self):
        # Both caches are keyed by the resolved repository root as a string and
        # temporary directory names get reused within one process.
        _git._ambient_worktree_config_overrides.cache_clear()
        _git._repository_filter_config_overrides.cache_clear()

    def tearDown(self):
        _git._ambient_worktree_config_overrides.cache_clear()
        _git._repository_filter_config_overrides.cache_clear()

    def _normalized_literal(self, declared: str) -> str:
        """The normalized declaration, asserted rather than assumed.

        This used to catch ValueError and return None so the caller could
        ``assume`` the example away. That is the wrong direction for an
        oracle. Three normalizer faults were injected — refusing any segment
        that contains a dot, refusing any non-ASCII byte, and no longer
        dropping a trailing separator — and all three left this class green,
        because each one removed examples rather than failing any assertion.
        (They are caught by tests/test_declared_path_grammar.py, so they were
        not escaping the suite; they were escaping this class while it looked
        like it was exercising them.) The strategy now filters on the restated
        rule, so reaching here means the normalizer must accept.

        Covers OBL-GLOBS-019 (the step before both sides).
        """
        normalized = _normalize_declared_path(declared)
        self.assertEqual(
            normalized,
            declared.rstrip("/"),
            "normalization may drop trailing separators and nothing else",
        )
        self.assertFalse(
            _is_glob(normalized),
            f"{declared!r} is a glob, and this section is about literals",
        )
        return normalized

    def _provider_selection(self, source: str, normalized: str) -> set:
        listed = self.accessors[source].list_files(_join_repo_path("svc", normalized))
        return {_component_relative_path("svc", repo_rel) for repo_rel in listed}

    def _git_selection(self, source: str, normalized: str) -> set:
        listed = _git_pathspec_selection(
            self.root, source, _join_repo_path("svc", normalized)
        )
        return {_component_relative_path("svc", repo_rel) for repo_rel in listed}

    @PROFILE
    @given(declared=literal_declarations())
    @example(declared="api.bak")
    @example(declared="apié")
    @example(declared="api/")
    @example(declared="api//")
    @example(declared=".hidden")
    @example(declared="api/nested/deep.yaml")
    @example(declared="with space/f.yaml")
    @example(declared="a//b")
    @example(declared="a/./b")
    @example(declared="a/../b")
    @example(declared="/api")
    @example(declared="api ")
    def test_the_normalizer_accepts_exactly_the_declarations_the_rule_allows(
        self, declared
    ):
        """Refusal is an answer, so it has to be asserted in both directions.

        The properties below draw only declarations the restated rule accepts.
        That filter is safe only while the normalizer and the rule agree about
        which those are, and this is where that agreement is checked: a
        declaration the rule allows must normalize, one it forbids must raise,
        and the value must be the declared string minus trailing separators.

        Covers OBL-GLOBS-019 (the step before both sides).
        """
        self.assertFalse(
            _is_glob(declared),
            f"{declared!r} is a glob, so the restated rule is incomplete for it",
        )
        acceptable = _declaration_is_acceptable(declared)
        try:
            normalized = _normalize_declared_path(declared)
        except ValueError as error:
            self.assertFalse(
                acceptable, f"{declared!r} was refused: {error}"
            )
            return
        self.assertTrue(acceptable, f"{declared!r} was accepted unexpectedly")
        self.assertEqual(normalized, declared.rstrip("/"))

    @GIT_PROFILE
    @given(
        declared=acceptable_literal_declarations(),
        source=st.sampled_from(("head", "index", "working-tree")),
    )
    @example(declared="api", source="head")
    @example(declared="apiv", source="head")
    @example(declared="api.bak", source="index")
    @example(declared="apifile/", source="working-tree")
    @example(declared="src/api", source="index")
    @example(declared="API", source="head")
    def test_validation_and_generation_select_the_same_files(
        self, declared, source
    ):
        """One rule, run twice in Python, and checked against Git's own.

        The equality between the two boundver sides is the half of the
        obligation that matters operationally: not that the rule is right, but
        that config validation and digest generation agree about it. A
        component that validates against one file set and hashes another is
        how a contract file leaves the fingerprint without anybody being told.

        On its own that equality is weak, because both sides reach the same
        Python whole-segment prefix rule — see this class's docstring. Git's
        pathspec matcher is asked the same question third, and it is the only
        one of the three that cannot be wrong in boundver's way.

        Covers OBL-GLOBS-019.
        """
        normalized = self._normalized_literal(declared)
        expansion = _expand_component_paths(
            self.root, "svc", [declared], source=source
        )
        self.assertEqual(
            expansion,
            self._provider_selection(source, normalized),
            f"{declared!r} disagreed on source {source}",
        )
        self.assertEqual(
            expansion,
            self._git_selection(source, normalized),
            f"{declared!r} disagreed with git's pathspec matcher on {source}",
        )

    @GIT_PROFILE
    @given(declared=acceptable_literal_declarations())
    @example(declared="apiv")
    @example(declared="apifil")
    @example(declared="ap")
    @example(declared="other")
    @example(declared="API")
    def test_a_literal_selects_whole_segments_and_never_a_string_prefix(
        self, declared
    ):
        """The rule itself, not merely agreement about it.

        The equality above cannot see a fault in the step that runs before
        either matcher. ``_normalize_declared_path`` rewrites the declared
        string, and a wrong rewrite reaches both sides at once and leaves them
        agreeing about a set that is wrong on both. So the expected set here is
        derived from the declared string rather than from the normalized one,
        and the normalizer's only licensed edit — dropping trailing separators
        — is asserted rather than assumed.

        Covers OBL-GLOBS-019.
        """
        normalized = self._normalized_literal(declared)
        prefix = declared.rstrip("/")
        relative_names = [name[len("svc/"):] for name in TRACKED_NAMES]
        expected = {
            name
            for name in relative_names
            if name == prefix or name.startswith(prefix + "/")
        }
        self.assertEqual(
            _expand_component_paths(self.root, "svc", [declared], source="head"),
            expected,
            f"{declared!r} did not resolve by whole segments",
        )
        # The expectation above is hand-written, so it is worth knowing it is
        # not simply boundver's rule typed a second time. Git reaches the same
        # set from its own leading-directory implementation.
        self.assertEqual(
            self._git_selection("head", normalized),
            expected,
            f"{declared!r} means something else to git's pathspec matcher",
        )

    def test_a_blob_and_a_tree_at_one_name_are_both_selected(self):
        """The obligation's own example, which no filesystem can hold.

        ``git add`` and ``git update-index --cacheinfo`` both refuse a blob and
        a tree at one name, so the fixture has to be assembled with
        ``git mktree`` and exists only at HEAD. On Windows the entries must be
        fed as bytes: text mode rewrites the newline and silently renames both
        entries to "api\\r", which makes them different names and the whole
        fixture vacuous.

        Covers OBL-GLOBS-019.
        """
        with tempfile.TemporaryDirectory() as location:
            root = Path(location)
            init_git_repo(root, initial_branch="main")

            def git(*arguments: str, stdin: bytes = b"") -> str:
                return subprocess.run(
                    ["git", *arguments],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    input=stdin,
                ).stdout.decode("utf-8").strip()

            blob = git("hash-object", "-w", "--stdin", stdin=b"blob at api\n")
            leaf = git("hash-object", "-w", "--stdin", stdin=b"route\n")
            inner = git(
                "mktree", stdin=f"100644 blob {leaf}\troute.yaml\n".encode("utf-8")
            )
            svc = git(
                "mktree",
                stdin=(
                    f"100644 blob {blob}\tapi\n040000 tree {inner}\tapi\n"
                ).encode("utf-8"),
            )
            tree = git(
                "mktree", stdin=f"040000 tree {svc}\tsvc\n".encode("utf-8")
            )
            commit = git("commit-tree", tree, "-m", "blob and tree at one name")
            git("update-ref", "refs/heads/main", commit)

            _git._ambient_worktree_config_overrides.cache_clear()
            _git._repository_filter_config_overrides.cache_clear()
            try:
                with _SourceAccessor(root, "head") as accessor:
                    listed = accessor.list_files(_join_repo_path("svc", "api"))
                    provider = {
                        _component_relative_path("svc", repo_rel)
                        for repo_rel in listed
                    }
                self.assertEqual(listed, ["svc/api", "svc/api/route.yaml"])
                self.assertEqual(provider, {"api", "api/route.yaml"})
                self.assertEqual(
                    _expand_component_paths(root, "svc", ["api"], source="head"),
                    provider,
                )
            finally:
                _git._ambient_worktree_config_overrides.cache_clear()
                _git._repository_filter_config_overrides.cache_clear()

    def test_a_case_variant_selects_nothing_on_a_case_folding_host(self):
        """Git's pathspec matching stays case-sensitive where the host is not.

        This repository is checked out with core.ignorecase true on Windows and
        macOS, so a matcher that inherited the filesystem's folding would
        select ``api/route.yaml`` for the declaration ``API``. Both sides must
        select nothing, and the lowercase control must select something, or the
        assertion passes because the fixture is empty.

        Both boundver sides answer from the same captured snapshot, so their
        agreement here says nothing about whether the host folded anything.
        Git is asked as well, and it is the side that would actually be
        entitled to fold: ``core.ignorecase`` is a Git setting, and it is true
        in this fixture, which is checked rather than assumed.

        Covers OBL-GLOBS-036 (case sensitivity) through OBL-GLOBS-019's pair.
        """
        ignorecase = subprocess.run(
            ["git", "config", "--get", "core.ignorecase"],
            cwd=self.root, capture_output=True, text=True,
        ).stdout.strip()
        self.assertIn(
            ignorecase,
            ("true", "false", ""),
            "core.ignorecase should be a boolean or unset",
        )
        for source in ("head", "index", "working-tree"):
            with self.subTest(source=source):
                self.assertEqual(
                    _expand_component_paths(
                        self.root, "svc", ["api"], source=source
                    ),
                    {"api/route.yaml", "api/nested/deep.yaml"},
                )
                self.assertEqual(
                    self._git_selection(source, "api"),
                    {"api/route.yaml", "api/nested/deep.yaml"},
                    f"git selected nothing for the control on {source}",
                )
                for variant in ("API", "Api", "api/ROUTE.yaml", "SRC/api"):
                    self.assertEqual(
                        _expand_component_paths(
                            self.root, "svc", [variant], source=source
                        ),
                        set(),
                        f"{variant!r} selected files on a case-folding host"
                        f" (core.ignorecase={ignorecase!r})",
                    )
                    self.assertEqual(
                        self._provider_selection(source, variant),
                        set(),
                        f"{variant!r} selected files through the provider",
                    )
                    self.assertEqual(
                        self._git_selection(source, variant),
                        set(),
                        f"{variant!r} selected files through git's pathspec"
                        f" matcher (core.ignorecase={ignorecase!r})",
                    )

    def test_the_two_sides_agree_when_no_source_is_given(self):
        """Omitting the source has the same working-tree meaning as providers."""
        self.assertEqual(
            _expand_component_paths(self.root, "svc", ["api"]),
            self._provider_selection("working-tree", "api"),
        )


if __name__ == "__main__":
    unittest.main()
