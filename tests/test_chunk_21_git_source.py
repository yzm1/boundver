"""Six places where boundver decides what counts as one file's real bytes.

The obligations gathered here look unrelated until you notice they all answer
the same question from different ends: given a path and a repository, which
bytes are the ones the digest is entitled to see? The strict JSON reader
answers it for a committed document, and refuses a second value for a key that
was written twice; the gitignore fallback answers it for a repository Git
cannot enumerate; the pruning guard answers it for a directory the fallback
would like to skip; the glob expander answers it for an entry that is a link to
somewhere else; the Action exporter answers it for a value crossing into a
workflow; and the baseline updater answers it for a file that Git and the
filesystem describe differently. Each of them, got wrong, makes a lock that
looks authoritative and is not.

The tests use strict parser assertions, compare the fallback ignore matcher
directly with the installed Git, verify that directory pruning preserves the
same corpus, and exercise path-containment and atomic-update boundaries. The
Git oracle covers directory-only rules, escaped prefixes and spaces, anchored
patterns, invalid UTF-8, and negation below excluded parents.

Two things made this harder to write than it reads. The first is that the
non-Git fallback is genuinely hard to enter - `_list_files_for_source` reaches
it only when `rev-parse --git-dir` itself fails, and the two existing tests
that meant to exercise it call `init_git_repo` first and so measure Git against
Git. Every fallback test here therefore builds a plain directory and proves, by
capturing the warning the fallback prints, that it really is the code path
under test. The second is that this host refuses to create symbolic links, so
the symlink obligation is answered two ways that do not need the privilege: a
Windows junction for the directory case, and a mode-120000 index entry written
with `hash-object` plus `update-index` for the tracked-symlink case, which is
exactly what `head` and `index` sources read anyway. The one shape left
untested is a *file* symlink matched directly by a wildcard, and it is named in
the residual gap rather than faked.

Covers OBL-GIT-SOURCE-045, OBL-GIT-SOURCE-046, OBL-GIT-SOURCE-047,
OBL-GIT-SOURCE-048, OBL-GIT-SOURCE-049 and OBL-GIT-SOURCE-054.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Sequence, Set, Tuple
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver._config import (
    _expand_component_paths,
    _validate_component_path_entries,
)
from boundver._git import (
    _GitignoreRules,
    _list_files_for_source,
    _load_gitignore_patterns,
)
from boundver._structured_data import StrictJSONError, strict_json_loads
from boundver._utils import MAX_JSON_INTEGER_DIGITS, MAX_JSON_NUMBER_CHARACTERS

from tests._parity import run_cli
from tests._scenarios import Scenario

LF = b"\n"
RAW_E9_PATH = b"caf\xe9".decode("utf-8", errors="surrogateescape")

PROPERTY_PROFILE = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-045: strict JSON value rules
# ---------------------------------------------------------------------------

#: Payloads whose two object keys collide, with the diagnostic each must
#: produce. Every entry after the first is equal only once the JSON string
#: grammar has been applied, which is the clause the suite never asserted: a
#: permissive parser keeps the second value and two tools then read different
#: digests out of one committed file.
DUPLICATE_KEY_PAYLOADS = {
    "literal repeat": ('{"a":1,"a":2}', "duplicate JSON object key 'a'"),
    "escape after literal": (
        '{"a":1,"\\u0061":2}',
        "duplicate JSON object key 'a'",
    ),
    "literal after escape": (
        '{"\\u0061":1,"a":2}',
        "duplicate JSON object key 'a'",
    ),
    "escaped solidus": (
        '{"a/b":1,"a\\/b":2}',
        "duplicate JSON object key 'a/b'",
    ),
    "nested object": (
        '{"o":{"k":1,"\\u006b":2}}',
        "duplicate JSON object key 'k'",
    ),
    "object inside an array": (
        '[{"k":1,"\\u006b":2}]',
        "duplicate JSON object key 'k'",
    ),
}

#: The three constants Python's json module will happily invent, and the
#: diagnostic each must draw instead.
NON_FINITE_PAYLOADS = {
    "NaN": ("NaN", "non-finite JSON number 'NaN' is not supported"),
    "Infinity": ("Infinity", "non-finite JSON number 'Infinity' is not supported"),
    "-Infinity": (
        "-Infinity",
        "non-finite JSON number '-Infinity' is not supported",
    ),
    "NaN nested in an array": (
        '{"a":[NaN]}',
        "non-finite JSON number 'NaN' is not supported",
    ),
}

#: The largest legal integer and the largest legal number, spelled exactly at
#: the limit rather than comfortably inside it. `_bounded_json_int` counts
#: digits after the sign; `_bounded_json_float` counts every character of the
#: literal, and only a literal carrying '.' or 'e' reaches it at all.
LARGEST_INTEGER_DIGITS = "1" * MAX_JSON_INTEGER_DIGITS
LARGEST_NUMBER_LITERAL = "0." + "1" * (MAX_JSON_NUMBER_CHARACTERS - 2)

#: One character past each limit, with the message observed today.
OVERSIZED_PAYLOADS = {
    "integer": (
        "1" * (MAX_JSON_INTEGER_DIGITS + 1),
        f"JSON integer exceeds the {MAX_JSON_INTEGER_DIGITS}-decimal-digit limit",
    ),
    "negative integer": (
        "-" + "1" * (MAX_JSON_INTEGER_DIGITS + 1),
        f"JSON integer exceeds the {MAX_JSON_INTEGER_DIGITS}-decimal-digit limit",
    ),
    "number": (
        "0." + "1" * (MAX_JSON_NUMBER_CHARACTERS - 1),
        f"JSON number exceeds the {MAX_JSON_NUMBER_CHARACTERS}-character limit",
    ),
}


class StrictJsonValueRuleTests(unittest.TestCase):
    """OBL-GIT-SOURCE-045: duplicates, non-finite constants, and the limits."""

    def test_a_key_repeated_only_after_unescaping_is_still_a_duplicate(self):
        for label, (payload, message) in DUPLICATE_KEY_PAYLOADS.items():
            with self.subTest(payload=label):
                with self.assertRaises(StrictJSONError) as caught:
                    strict_json_loads(payload)
                self.assertEqual(str(caught.exception), message)

    def test_two_keys_that_differ_after_unescaping_are_accepted(self):
        """The premise: the hook does not refuse every object with two keys.

        Without this, the table above would pass against a `strict_json_loads`
        that rejected all objects, and the escape-equality clause would be
        asserting nothing at all.
        """
        self.assertEqual(strict_json_loads('{"a":1,"\\u0062":2}'), {"a": 1, "b": 2})
        self.assertEqual(strict_json_loads('{"o":{"k":1,"j":2}}'), {"o": {"k": 1, "j": 2}})

    def test_every_non_finite_constant_is_refused(self):
        for label, (payload, message) in NON_FINITE_PAYLOADS.items():
            with self.subTest(payload=label):
                with self.assertRaises(StrictJSONError) as caught:
                    strict_json_loads(payload)
                self.assertEqual(str(caught.exception), message)

    def test_a_finite_number_in_the_same_position_is_accepted(self):
        """The premise for the non-finite table: that position parses at all."""
        self.assertEqual(strict_json_loads('{"a":[1.5]}'), {"a": [1.5]})
        self.assertEqual(strict_json_loads("1.5"), 1.5)

    def test_the_value_one_unit_inside_each_limit_is_accepted(self):
        """Acceptance exactly at the boundary, not comfortably inside it.

        An off-by-one that rejected a legal 4300-digit integer would pass every
        existing test, because the largest integer the suite offers today is a
        thousand digits long.
        """
        parsed = strict_json_loads(LARGEST_INTEGER_DIGITS)
        self.assertEqual(parsed, int(LARGEST_INTEGER_DIGITS))
        # Arithmetic rather than str(), so the check itself cannot trip over
        # the interpreter's own decimal-conversion ceiling.
        self.assertEqual(parsed // 10 ** (MAX_JSON_INTEGER_DIGITS - 1), 1)

        negative = strict_json_loads("-" + LARGEST_INTEGER_DIGITS)
        self.assertEqual(negative, -int(LARGEST_INTEGER_DIGITS))

        self.assertEqual(len(LARGEST_NUMBER_LITERAL), MAX_JSON_NUMBER_CHARACTERS)
        self.assertEqual(
            strict_json_loads(LARGEST_NUMBER_LITERAL), float(LARGEST_NUMBER_LITERAL)
        )

    def test_one_character_past_each_limit_is_refused(self):
        """Both numeric ceilings use the strict-parser exception contract."""
        for label, (payload, message) in OVERSIZED_PAYLOADS.items():
            with self.subTest(payload=label):
                with self.assertRaises(StrictJSONError) as caught:
                    strict_json_loads(payload)
                self.assertEqual(str(caught.exception), message)

    def test_an_oversized_number_raises_strict_json_error(self):
        """Oversized numeric tokens are strict structured-data failures."""
        for label, (payload, _message) in OVERSIZED_PAYLOADS.items():
            with self.subTest(payload=label):
                with self.assertRaises(StrictJSONError):
                    strict_json_loads(payload)


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-046: the fallback gitignore matcher against the installed Git
# ---------------------------------------------------------------------------

#: The families the obligation names. Each value is a `.gitignore` written as
#: exact bytes and a mapping from candidate path to the matching Git/boundver
#: verdict pair.
GITIGNORE_FAMILIES = {
    "directory-only rule": (
        b"build/" + LF,
        {
            "build": (False, False),
            "build/out.js": (True, True),
            "src/build": (False, False),
            "src/build/out.js": (True, True),
        },
    ),
    "leading whitespace": (
        b"  spaced.txt" + LF,
        {"spaced.txt": (False, False), "  spaced.txt": (True, True)},
    ),
    "escaped bang": (
        b"\\!bang.txt" + LF,
        {"!bang.txt": (True, True), "bang.txt": (False, False)},
    ),
    "escaped trailing space": (
        b"trail\\ " + LF,
        {"trail ": (True, True), "trail": (False, False)},
    ),
    "re-inclusion under an ignored parent": (
        b"logs/" + LF + b"!logs/keep.txt" + LF,
        {"logs/keep.txt": (True, True), "logs/drop.txt": (True, True)},
    ),
    "anchored, doublestar and negation together": (
        b"/vendor/**" + LF + b"!/vendor/keep/**" + LF,
        {
            "vendor/a.txt": (True, True),
            "vendor/keep/b.txt": (True, True),
            "sub/vendor/c.txt": (False, False),
        },
    ),
    "a line whose bytes are not valid UTF-8": (
        b"caf\xe9" + LF,
        {
            RAW_E9_PATH: (True, True),
            "caf�": (False, False),
            "café": (False, False),
            "cafe": (False, False),
        },
    ),
}

#: Rulesets where the two really do agree, so the harness can be shown to
#: report agreement rather than always reporting a difference.
AGREEING_FAMILIES = {
    "a plain filename": (
        b"secret.txt" + LF,
        {"secret.txt": True, "a/secret.txt": True, "secret.txt.bak": False},
    ),
    "a negated sibling": (
        b"*.log" + LF + b"!keep.log" + LF,
        {"a.log": True, "keep.log": False, "sub/keep.log": False},
    ),
    "a trailing doublestar": (
        b"docs/**" + LF,
        {"docs": False, "docs/a.md": True, "docs/x/b.md": True},
    ),
}

#: Lines the generated corpus draws from, chosen so that a sampled ruleset can
#: mix an anchored rule, a `**` rule and a negation.
CORPUS_LINES = (
    "build/",
    "build",
    "/build",
    "logs/",
    "!logs/keep.txt",
    "*.txt",
    "!keep.txt",
    "docs/**",
    "/vendor/**",
    "!/vendor/keep/**",
    "  spaced.txt",
    "\\!bang.txt",
    "trail\\ ",
)

#: Candidate paths the corpus is crossed with.
CORPUS_PATHS = (
    "build",
    "build/out.js",
    "src/build/out.js",
    "logs/keep.txt",
    "logs/drop.txt",
    "docs/a.md",
    "vendor/a.txt",
    "vendor/keep/b.txt",
    "keep.txt",
    "!bang.txt",
    "trail ",
    "  spaced.txt",
)


class GitignoreDifferentialTests(unittest.TestCase):
    """OBL-GIT-SOURCE-046: `_GitignoreRules` against `git check-ignore`."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )

    def tearDown(self):
        self._directory.cleanup()

    def _git_verdicts(self, candidates: Sequence[str]) -> Set[str]:
        """Which candidates the installed Git excludes, in one invocation."""
        payload = (
            b"\0".join(
                c.encode("utf-8", errors="surrogateescape") for c in candidates
            )
            + b"\0"
        )
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--stdin", "-z"],
            cwd=self.root,
            input=payload,
            capture_output=True,
        )
        if result.returncode not in (0, 1):
            raise AssertionError(
                f"git check-ignore failed ({result.returncode}): {result.stderr!r}"
            )
        return {
            chunk.decode("utf-8", errors="surrogateescape")
            for chunk in result.stdout.split(b"\0")
            if chunk
        }

    def _compare(
        self, gitignore: bytes, candidates: Sequence[str]
    ) -> Dict[str, Tuple[bool, bool]]:
        """Return {candidate: (git says ignored, boundver says ignored)}."""
        (self.root / ".gitignore").write_bytes(gitignore)
        rules = _load_gitignore_patterns(self.root)
        excluded = self._git_verdicts(candidates)
        return {
            candidate: (
                candidate in excluded,
                False if rules is None else rules.is_ignored(candidate),
            )
            for candidate in candidates
        }

    def test_the_harness_reports_agreement_where_the_two_really_agree(self):
        """The premise. Both columns come from live runs, so a harness that
        always reported a difference - a broken oracle, a `.gitignore` never
        written, a `check-ignore` invocation that errored - would show up here
        rather than being read as a divergence in the table below.
        """
        for label, (gitignore, expected) in AGREEING_FAMILIES.items():
            with self.subTest(family=label):
                observed = self._compare(gitignore, tuple(expected))
                for candidate, verdict in expected.items():
                    self.assertEqual(
                        observed[candidate],
                        (verdict, verdict),
                        f"{candidate!r} under {gitignore!r}",
                    )

    def test_each_named_family_agrees_with_git(self):
        """Pin the matching answers on both sides, family by family."""
        divergences = 0
        for label, (gitignore, expected) in GITIGNORE_FAMILIES.items():
            with self.subTest(family=label):
                observed = self._compare(gitignore, tuple(expected))
                for candidate, pair in expected.items():
                    self.assertEqual(
                        observed[candidate],
                        pair,
                        f"{candidate!r} under {gitignore!r}",
                    )
            divergences += sum(
                1 for git_says, mine in expected.values() if git_says != mine
            )
        self.assertEqual(divergences, 0)

    @given(lines=st.lists(st.sampled_from(CORPUS_LINES), max_size=4))
    @PROPERTY_PROFILE
    def test_the_fallback_matcher_agrees_with_git_check_ignore(self, lines: List[str]):
        """Compare generated rule sets with Git without another code path."""
        gitignore = "".join(f"{line}\n" for line in lines).encode("utf-8")
        observed = self._compare(gitignore, CORPUS_PATHS)
        for candidate, (git_says, mine) in observed.items():
            self.assertEqual(
                mine, git_says, f"{candidate!r} under {lines!r}"
            )


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-047: directory pruning must not change the fallback file list
# ---------------------------------------------------------------------------

#: The tree every pruning example is enumerated over. It carries a directory a
#: rule can exclude wholesale, a descendant a later negation can re-include,
#: and a same-named directory at two depths so an anchored rule means
#: something.
FALLBACK_TREE = (
    "svc/a.txt",
    "svc/logs/keep.txt",
    "svc/logs/drop.txt",
    "svc/logs/deep/keep.txt",
    "svc/vendor/x.txt",
    "svc/vendor/keep/y.txt",
    "svc/src/main.py",
)

#: The warning the fallback prints on its way in. Capturing it is how a test
#: proves it reached `_GitignoreRules` rather than Git's own enumeration.
FALLBACK_WARNING = (
    "WARNING: git file listing failed; falling back to filesystem enumeration. "
    "Fingerprints may differ from git-based computation.\n"
)

#: Rulesets exercised deterministically as well as generatively, so a failure
#: names the ruleset instead of a shrunk sample.
PRUNING_RULESETS = {
    "no gitignore at all": None,
    "a bare directory name": "logs\n",
    "a directory-only rule": "logs/\n",
    "a negation under an ignored directory": "svc/logs/\n!svc/logs/keep.txt\n",
    "anchored, doublestar and negation": "/svc/vendor/**\n!/svc/vendor/keep/**\n",
    "a wildcard with a negated sibling": "*.txt\n!keep.txt\n",
    "a trailing doublestar": "svc/logs/**\n",
}

#: Lines the generated rulesets draw from.
PRUNING_LINES = (
    "logs",
    "logs/",
    "!svc/logs/keep.txt",
    "/svc/vendor/**",
    "!/svc/vendor/keep/**",
    "*.txt",
    "!keep.txt",
    "svc/**",
    "!svc/src/main.py",
    "vendor",
)


def _never_prune(self, rel_path: str) -> bool:  # noqa: ARG001 - signature match
    """`can_prune_directory` with pruning switched off entirely."""
    return False


def _prune_ignoring_the_negation_guard(self, rel_path: str) -> bool:
    """`can_prune_directory` with its `_has_negation` short-circuit removed."""
    return self.is_ignored(rel_path)


class FallbackPruningEquivalenceTests(unittest.TestCase):
    """OBL-GIT-SOURCE-047: the same list with pruning on and with it off."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        for relative in FALLBACK_TREE:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x" + LF)

    def tearDown(self):
        self._directory.cleanup()

    def _write_rules(self, gitignore):
        target = self.root / ".gitignore"
        if gitignore is None:
            if target.exists():
                target.unlink()
        else:
            target.write_bytes(gitignore.encode("utf-8"))

    def _enumerate(self, replacement=None) -> List[str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            if replacement is None:
                files = _list_files_for_source(self.root, "svc", "working-tree")
            else:
                with mock.patch.object(
                    _GitignoreRules, "can_prune_directory", replacement
                ):
                    files = _list_files_for_source(self.root, "svc", "working-tree")
        self._last_stderr = stderr.getvalue()
        return files

    def test_the_case_under_test_really_is_the_non_git_fallback(self):
        """The premise, and the one the two existing fallback tests fail.

        `_list_files_for_source` reaches the filesystem enumerator only after
        `rev-parse --git-dir` itself fails. A fixture that called
        `init_git_repo` would take Git's `ls-files --exclude-standard` branch
        instead and never construct a `_GitignoreRules` at all, so every
        assertion below would be about Git rather than about boundver.
        """
        self._write_rules("logs\n")
        files = self._enumerate()
        self.assertEqual(self._last_stderr, FALLBACK_WARNING)
        self.assertIsNotNone(_load_gitignore_patterns(self.root))
        self.assertEqual(
            files, ["svc/a.txt", "svc/src/main.py", "svc/vendor/keep/y.txt", "svc/vendor/x.txt"]
        )

    def test_dropping_the_negation_guard_would_drop_a_re_included_file(self):
        """The premise for the equivalence: the comparison can see a difference.

        With `_has_negation` removed, `/svc/vendor/**` prunes the whole vendor
        subtree and `!/svc/vendor/keep/**` never gets the chance to re-include
        anything under it. That is the exact failure the guard exists to
        prevent, and it is what the equivalence test would have to detect.
        """
        self._write_rules(
            "/svc/vendor/**\n"
            "!/svc/vendor/keep/\n"
            "!/svc/vendor/keep/**\n"
        )
        guarded = self._enumerate()
        unguarded = self._enumerate(_prune_ignoring_the_negation_guard)
        self.assertIn("svc/vendor/keep/y.txt", guarded)
        self.assertNotIn("svc/vendor/keep/y.txt", unguarded)
        self.assertEqual(
            sorted(set(guarded) - set(unguarded)), ["svc/vendor/keep/y.txt"]
        )
        self.assertIs(
            _GitignoreRules.can_prune_directory.__name__, "can_prune_directory"
        )

    def test_pruning_changes_nothing_for_each_named_ruleset(self):
        for label, gitignore in PRUNING_RULESETS.items():
            with self.subTest(ruleset=label):
                self._write_rules(gitignore)
                self.assertEqual(self._enumerate(), self._enumerate(_never_prune))

    @given(lines=st.lists(st.sampled_from(PRUNING_LINES), max_size=4))
    @PROPERTY_PROFILE
    def test_pruning_never_changes_the_fallback_file_list(self, lines: List[str]):
        """Every ruleset, including ones mixing anchors, `**` and negations.

        The oracle is the same enumeration with `can_prune_directory` forced to
        False, which is the definition the obligation gives: what the traversal
        would produce if it descended everything and filtered with `is_ignored`
        alone. Both runs go through the product's own code, so the comparison
        cannot drift from what `_list_files_for_source` actually does.
        """
        self._write_rules("".join(f"{line}\n" for line in lines))
        self.assertEqual(
            self._enumerate(),
            self._enumerate(_never_prune),
            f"pruning changed the list for {lines!r}",
        )


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-048: a wildcard must not reach outside through a link
# ---------------------------------------------------------------------------

#: The exact diagnostic a literal declaration draws when the path it names
#: resolves outside the component root.
ESCAPE_DIAGNOSTIC = (
    "Component 'svc' boundary path escapes component root: linkdir/secret.txt"
)

#: The refusal the hasher raises when a working-tree path turns out to sit
#: behind a link. It is not the diagnostic a literal gets, but it is a refusal.
HASHING_REFUSAL = (
    "Symlink, reparse point, or non-directory ancestor while hashing: "
    "svc/linkdir/secret.txt"
)


def _directory_link(link: Path, target: Path) -> bool:
    """Point *link* at directory *target*, however this host allows.

    A POSIX host gets an ordinary symlink. Windows refuses those without a
    privilege this machine does not grant, but a junction is a reparse point
    that `Path.resolve` follows just the same and `mklink /J` needs no
    privilege at all. Returns False when neither works, so the caller can skip
    rather than assert against a link that was never created.
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name != "nt":
        return False
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
    )
    return made.returncode == 0 and link.is_dir()


def _tracked_symlink(scene: Scenario, path: str, target: str) -> str:
    """Record *path* as a mode-120000 entry whose blob holds *target*.

    This is what a symlink is in a Git tree, and it is exactly what the `head`
    and `index` sources read. Writing it with `hash-object` plus `update-index`
    needs no filesystem symlink and so runs on every host.
    """
    oid = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=scene.root,
        input=target.encode("utf-8"),
        capture_output=True,
        check=True,
    ).stdout.decode().strip()
    scene.git("update-index", "--add", "--cacheinfo", f"120000,{oid},{path}")
    return oid


class GlobContainmentTests(unittest.TestCase):
    """OBL-GIT-SOURCE-048: containment is checked for literals only."""

    def _linked_tree(self, boundary: Sequence[str]) -> Scenario:
        """A component holding a link to a sibling directory outside it."""
        scene = Scenario()
        scene.component("svc", path="svc", provider="path-hash", boundary=list(boundary))
        scene.file("svc/main.py", "x\n")
        scene.file("outside/secret.txt", "SECRET\n")
        scene.write_config()
        if not _directory_link(scene.root / "svc" / "linkdir", scene.root / "outside"):
            scene.close()
            self.skipTest("this host creates neither a symlink nor a junction")
        return scene

    @staticmethod
    def _drop_link(scene: Scenario) -> None:
        link = scene.root / "svc" / "linkdir"
        with contextlib.suppress(OSError):
            if link.is_symlink():
                link.unlink()
            else:
                os.rmdir(link)

    def test_a_literal_is_containment_checked_and_a_glob_is_not(self):
        """The mechanism itself: `if _is_glob(normalized): continue`.

        `_normalize_declared_path` rejects a `..` segment outright, so the only
        way a declared path can leave its component root is by resolving
        through a link - and `_is_within` calls `resolve()`, so it sees that.
        The literal is refused. The two globs naming the same file draw no
        diagnostic of any kind, because line 247 of `_config.py` skips every
        per-path check for anything holding a metacharacter.
        """
        scene = self._linked_tree(["*"])
        try:
            component_root = scene.root / "svc"
            escaping = component_root / "linkdir" / "secret.txt"
            # The premise: the link really does reach outside the component.
            self.assertTrue(escaping.is_file())
            self.assertFalse(
                Path(os.path.realpath(escaping)).is_relative_to(
                    Path(os.path.realpath(component_root))
                )
            )

            declarations = {
                "literal": (["linkdir/secret.txt"], [ESCAPE_DIAGNOSTIC]),
                "glob on the filename": (["linkdir/*.txt"], []),
                "recursive glob": (["**/*"], []),
            }
            for label, (declared, expected) in declarations.items():
                with self.subTest(declaration=label):
                    errors: List[str] = []
                    _validate_component_path_entries(
                        errors, scene.root, "svc", "svc", "boundary", declared
                    )
                    self.assertEqual(errors, expected)
        finally:
            self._drop_link(scene)
            scene.close()

    def test_the_filesystem_expansion_never_descends_the_link(self):
        """One half of the hole is already closed, and this pins it closed.

        `_iter_bounded_filesystem_paths` yields a directory link but refuses to
        descend it, so the sourceless expansion `**/*` cannot reach the file on
        the other side even though nothing validated the pattern.
        """
        scene = self._linked_tree(["**/*"])
        try:
            self.assertEqual(
                sorted(
                    _expand_component_paths(
                        scene.root, "svc", ["**/*"], source=None
                    )
                ),
                ["main.py"],
            )
            self.assertEqual(
                sorted(
                    _expand_component_paths(
                        scene.root, "svc", ["linkdir/*.txt"], source=None
                    )
                ),
                [],
            )
        finally:
            self._drop_link(scene)
            scene.close()

    def test_what_each_source_does_today_with_a_glob_that_reaches_outside(self):
        """Pins the current end-to-end answer, source by source.

        Where Git resolved the link when the tree was staged - which is what
        Git for Windows does with a junction, since it does not recognise one
        as a symlink - the committed tree holds a real blob at
        `svc/linkdir/secret.txt`. The literal declaration is still refused,
        because `_is_within` consults the live filesystem; the glob is not, and
        on `head` and `index` a boundary digest is published over content whose
        only home on disk is outside the component.
        """
        probe = self._linked_tree(["*"])
        try:
            probe.git("add", "--all")
            tracked = probe.git("ls-files").splitlines()
        finally:
            self._drop_link(probe)
            probe.close()
        if "svc/linkdir/secret.txt" not in tracked:
            self.skipTest(
                "this host's Git recorded the link itself rather than following it"
            )

        expected = {
            ("literal", "working-tree"): (2, ESCAPE_DIAGNOSTIC),
            ("literal", "head"): (2, ESCAPE_DIAGNOSTIC),
            ("literal", "index"): (2, ESCAPE_DIAGNOSTIC),
            ("glob", "working-tree"): (2, ESCAPE_DIAGNOSTIC),
            ("glob", "head"): (2, ESCAPE_DIAGNOSTIC),
            ("glob", "index"): (2, ESCAPE_DIAGNOSTIC),
        }
        declarations = {"literal": ["linkdir/secret.txt"], "glob": ["linkdir/*.txt"]}
        for label, declared in declarations.items():
            scene = self._linked_tree(declared)
            try:
                scene.git("add", "--all")
                scene.git("commit", "-m", "linked")
                for source in ("working-tree", "head", "index"):
                    code, fragment = expected[(label, source)]
                    with self.subTest(declaration=label, source=source):
                        result = run_cli(scene.root, "generate", "--source", source)
                        self.assertEqual(result.returncode, code, result.stderr)
                        if fragment:
                            self.assertIn(fragment, result.stderr)
                        else:
                            self.assertEqual(result.stderr.strip(), "")
            finally:
                self._drop_link(scene)
                scene.close()

    def test_a_glob_reaching_outside_is_refused_the_way_a_literal_is(self):
        """A wildcard-selected foreign path gets the literal escape diagnostic."""
        scene = self._linked_tree(["linkdir/*.txt"])
        try:
            scene.git("add", "--all")
            scene.git("commit", "-m", "linked")
            if "svc/linkdir/secret.txt" not in scene.git("ls-files").splitlines():
                self.skipTest(
                    "this host's Git recorded the link itself rather than following it"
                )
            for source in ("head", "index"):
                with self.subTest(source=source):
                    result = run_cli(scene.root, "generate", "--source", source)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(ESCAPE_DIAGNOSTIC, result.stderr)
        finally:
            self._drop_link(scene)
            scene.close()

    def _symlinked_component(self, link_target: str, secret: str, mode: str) -> Scenario:
        scene = Scenario()
        scene.component("svc", path="svc", provider="path-hash", boundary=["*"])
        scene.file("svc/main.py", "x\n")
        scene.file("outside/secret.txt", secret)
        scene.write_config()
        scene.git("add", "--all")
        if mode == "120000":
            _tracked_symlink(scene, "svc/link", link_target)
        else:
            scene.file("svc/link", link_target)
            scene.git("add", "svc/link")
        scene.git("commit", "-m", "linked")
        return scene

    def test_a_glob_that_matches_a_tracked_symlink_selects_the_link_itself(self):
        """The second answer the obligation allows, and boundver gives it.

        Three scenarios differ in one thing each. Changing the *target file's*
        bytes must not move the digest, or the boundary is being fingerprinted
        over foreign content. Changing the *link text* must move it, or the
        link is not in the boundary at all. And a regular file holding that
        same text must digest differently, or the mode is not part of the
        identity and an attacker could swap one for the other.
        """
        cases = {
            "link, target body A": ("../outside/secret.txt", "AAA\n", "120000"),
            "link, target body B": ("../outside/secret.txt", "BBB\n", "120000"),
            "link elsewhere": ("../outside/other.txt", "AAA\n", "120000"),
            "regular file, same text": ("../outside/secret.txt", "AAA\n", "100644"),
        }
        digests: Dict[Tuple[str, str], str] = {}
        for label, (target, secret, mode) in cases.items():
            scene = self._symlinked_component(target, secret, mode)
            try:
                listing = scene.git("ls-tree", "-r", "HEAD")
                self.assertIn(f"{mode} blob", listing, listing)
                self.assertIn("svc/link", listing)
                for source in ("head", "index"):
                    digests[(label, source)] = scene.fingerprints("svc", source)["boundary"]
            finally:
                scene.close()

        for source in ("head", "index"):
            with self.subTest(source=source):
                self.assertEqual(
                    digests[("link, target body A", source)],
                    digests[("link, target body B", source)],
                    "the target file's bytes reached the boundary digest",
                )
                self.assertNotEqual(
                    digests[("link, target body A", source)],
                    digests[("link elsewhere", source)],
                    "the link's own bytes did not reach the boundary digest",
                )
                self.assertNotEqual(
                    digests[("link, target body A", source)],
                    digests[("regular file, same text", source)],
                    "mode 120000 and mode 100644 produced one digest",
                )


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-049: one block, one name/value pair
# ---------------------------------------------------------------------------


def _export_module():
    """The Action exporter, which lives in scripts/ rather than the package."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "export_action_outputs.py"
    spec = importlib.util.spec_from_file_location("export_action_outputs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXPORT = _export_module()


def _exporter_output_names() -> Tuple[str, ...]:
    """Every name the exporter writes, read out of its source rather than listed.

    The names live in a dict literal built inline, so `ast` is how the surface
    is enumerated: an output added tomorrow is covered by the property below
    without anyone remembering to add it here.
    """
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "export_action_outputs.py"
    ).read_text(encoding="utf-8")
    names: List[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        keys = [
            key.value
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
        if "transport-complete" in keys:
            names.extend(keys)
    return tuple(dict.fromkeys((*names, *EXPORT.PLAN_ARRAY_OUTPUTS)))


OUTPUT_NAMES = _exporter_output_names()

#: A .NET `StreamReader` ends a line at CRLF, at a bare LF, and at a bare CR,
#: which is the file reader the Actions runner parses `$GITHUB_OUTPUT` with.
_RUNNER_TERMINATOR = re.compile(r"\r\n|\r|\n")

#: Values worth generating on purpose: the delimiter's own prefix, a lone CR
#: (which takes the heredoc branch while containing no LF), and text shaped
#: like a second entry.
SEEDED_VALUES = (
    "",
    "plain",
    "a\nb",
    "a\n",
    "\n",
    "a\r\nb",
    "a\rb",
    "\r",
    "line=two",
    "x<<y",
    "BOUNDVER_OUTPUT_deadbeef",
    "exit-code=0",
    "\nexit-code=0\n",
    "a\n\nb",
    "trailing  ",
)


def _runner_lines(text: str) -> List[str]:
    pieces = _RUNNER_TERMINATOR.split(text)
    if pieces and pieces[-1] == "":
        pieces.pop()
    return pieces


def _lf_lines(text: str) -> List[str]:
    pieces = text.split("\n")
    if pieces and pieces[-1] == "":
        pieces.pop()
    return pieces


def _parse_output(lines: Sequence[str]) -> List[Tuple[str, str]]:
    """Parse `$GITHUB_OUTPUT` lines the way the Actions runner does.

    Outside a heredoc an empty line is skipped; whichever of `=` and `<<`
    appears first decides how the line is read; anything else is a format
    error, and an unterminated heredoc is an error too. Nothing here consults
    `_append_output`, which is the point: it is the reader the writer has to
    satisfy.
    """
    entries: List[Tuple[str, str]] = []
    key = None
    delimiter = ""
    collected: List[str] = []
    for line in lines:
        if key is not None:
            if line == delimiter:
                entries.append((key, "\n".join(collected)))
                key, collected = None, []
            else:
                collected.append(line)
            continue
        if line == "":
            continue
        equals, heredoc = line.find("="), line.find("<<")
        if equals >= 0 and (heredoc < 0 or equals < heredoc):
            entries.append((line[:equals], line[equals + 1:]))
        elif heredoc >= 0 and (equals < 0 or heredoc < equals):
            key, delimiter = line[:heredoc], line[heredoc + 2:]
        else:
            raise ValueError(f"invalid format {line!r}")
    if key is not None:
        raise ValueError("matching delimiter not found")
    return entries


def _block(name: str, value: str) -> str:
    handle = io.StringIO()
    EXPORT._append_output(handle, name, value)
    return handle.getvalue()


class _FixedDigest:
    def __init__(self, hexdigest: str) -> None:
        self._hexdigest = hexdigest

    def hexdigest(self) -> str:
        return self._hexdigest


class _CollidingHashlib:
    """A stand-in that always names one delimiter, so the loop can be entered.

    The real derivation is `sha256(name\\0value)`, so a value containing its
    own delimiter as a line would be a self-referential preimage: the
    collision branch is unreachable with honest inputs and has therefore never
    executed in this suite. Fixing the digest makes it reachable without
    weakening anything the writer does afterwards.
    """

    DIGEST = "00" * 32

    @staticmethod
    def sha256(data: bytes) -> _FixedDigest:
        return _FixedDigest(_CollidingHashlib.DIGEST)


class ActionOutputTransportTests(unittest.TestCase):
    """OBL-GIT-SOURCE-049: no forged second entry, no truncation."""

    def test_the_parser_reports_the_second_entry_a_forged_block_carries(self):
        """The premise. Everything below asserts that a second pair is absent,
        which is worth nothing unless the parser would have shown one.
        """
        forged = "result-file<<D\nevil\nD\nexit-code=0\n"
        self.assertEqual(
            _parse_output(_lf_lines(forged)),
            [("result-file", "evil"), ("exit-code", "0")],
        )
        self.assertEqual(
            _parse_output(_runner_lines(forged)),
            [("result-file", "evil"), ("exit-code", "0")],
        )

    def test_the_heredoc_branch_is_the_one_a_multiline_value_takes(self):
        """The second premise: the property is not all `name=value` lines."""
        self.assertTrue(_block("issues", "a\nb").startswith("issues<<BOUNDVER_OUTPUT_"))
        self.assertTrue(_block("issues", "a\rb").startswith("issues<<BOUNDVER_OUTPUT_"))
        self.assertEqual(_block("issues", "plain"), "issues=plain\n")

    def test_every_exporter_output_name_is_enumerated_from_the_source(self):
        """The surface, so a new output cannot quietly escape the property."""
        self.assertIn("result-file", OUTPUT_NAMES)
        self.assertIn("transport-complete", OUTPUT_NAMES)
        self.assertIn("changed-components", OUTPUT_NAMES)
        self.assertEqual(len(OUTPUT_NAMES), 16)
        for name in OUTPUT_NAMES:
            with self.subTest(name=name):
                self.assertNotIn("=", name)
                self.assertNotIn("<<", name)
                self.assertEqual(_runner_lines(name + "\n"), [name])

    @given(
        name=st.sampled_from(OUTPUT_NAMES),
        value=st.one_of(st.sampled_from(SEEDED_VALUES), st.text(max_size=200)),
    )
    @PROPERTY_PROFILE
    def test_a_block_parses_back_to_exactly_one_pair(self, name: str, value: str):
        """No second entry, and no truncation, for any value.

        Two readers are used because the block has to survive both. Splitting
        on LF alone recovers the value byte for byte. Splitting the way a .NET
        `StreamReader` does - CR is a terminator there too - recovers it with
        carriage returns folded into line feeds, which is a property of
        `$GITHUB_OUTPUT` itself and not of this writer. Under either reader
        there is exactly one pair and its name is the one that was asked for.
        """
        block = _block(name, value)
        self.assertTrue(block.endswith("\n"))

        by_lf = _parse_output(_lf_lines(block))
        self.assertEqual(by_lf, [(name, value)])

        runner_value = "\n".join(_runner_lines(value + "\n"))
        self.assertEqual(_parse_output(_runner_lines(block)), [(name, runner_value)])

    def test_a_value_carrying_the_delimiter_extends_it_instead_of_ending_it(self):
        """The collision loop, reached by fixing the digest rather than the value."""
        collision = "BOUNDVER_OUTPUT_" + _CollidingHashlib.DIGEST
        cases = {
            "the delimiter on its own line": (
                f"before\n{collision}\nafter",
                collision + "X",
            ),
            "the delimiter and its extension": (
                f"before\n{collision}\n{collision}X\nafter",
                collision + "XX",
            ),
            "a separator only Python calls a line break": (
                f"x\v{collision}",
                collision + "X",
            ),
        }
        for label, (value, expected_delimiter) in cases.items():
            with self.subTest(value=label):
                with mock.patch.object(EXPORT, "hashlib", _CollidingHashlib):
                    self.assertEqual(
                        EXPORT._delimiter("exit-code", value), expected_delimiter
                    )
                    block = _block("exit-code", value)
                self.assertEqual(_parse_output(_lf_lines(block)), [("exit-code", value)])
        self.assertIs(EXPORT.hashlib, sys.modules["hashlib"])

    def test_a_lone_carriage_return_reaches_the_reader_as_a_line_feed(self):
        """Pinned, because it is the one value that does not survive verbatim.

        `_append_output` takes the heredoc branch for a CR-only value - the
        `"\\r" in value` test at line 364 is what puts it there - and the block
        it writes is faithful. The loss happens in the reader, which ends a
        line at a bare CR, and no writer can prevent it. What matters for the
        obligation is that it costs a character and not an extra entry.
        """
        block = _block("result-file", "a\rb")
        self.assertEqual(_parse_output(_lf_lines(block)), [("result-file", "a\rb")])
        self.assertEqual(_parse_output(_runner_lines(block)), [("result-file", "a\nb")])
        self.assertEqual(
            _parse_output(_runner_lines(_block("result-file", "\r"))),
            [("result-file", "")],
        )

    def test_the_block_reaches_the_file_as_the_bytes_the_property_examined(self):
        """`StringIO` is the property's stand-in; this is the real handle.

        The exporter opens `$GITHUB_OUTPUT` with `newline="\\n"`, so a Windows
        runner writes LF and not CRLF. Without that argument every heredoc line
        would gain a CR and the reader would fold it back, which is exactly the
        loss pinned above - so this is the assertion that keeps the property
        honest about what lands on disk.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "github_output"
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                EXPORT._append_output(handle, "issues", "a\nb")
            written = path.read_bytes()
        self.assertEqual(written, _block("issues", "a\nb").encode("utf-8"))
        self.assertNotIn(b"\r", written)

    def test_a_name_outside_the_exporter_s_own_set_breaks_the_file(self):
        """The residual precondition, stated rather than assumed.

        Values are repository-controlled and names are not, and the property
        above is quantified over the names the exporter actually writes. A name
        carrying `=` or a newline is not defended against; what it produces is
        a file the runner rejects outright, which is a broken step rather than
        a forged routing signal. Observed, not proved for all names.
        """
        for name in ("a=b", "plain\nexit-code=0"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as caught:
                    _parse_output(_runner_lines(_block(name, "x\ny")))
                self.assertTrue(str(caught.exception).startswith("invalid format"))
        with self.assertRaises(ValueError) as caught:
            _parse_output(_runner_lines(_block("a<<b", "x\ny")))
        self.assertEqual(str(caught.exception), "matching delimiter not found")


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-054: selected bytes against live bytes before publishing
# ---------------------------------------------------------------------------

#: The refusal, verbatim, including the label and the trailing clause.
BASELINE_REFUSAL = (
    "ERROR: verification baseline changed before update: b.json; refusing to write"
)


class BaselineLiveByteTests(unittest.TestCase):
    """OBL-GIT-SOURCE-054: --update-baseline compares before it publishes."""

    def _locked(self, autocrlf: str = "false") -> Scenario:
        scene = Scenario(autocrlf=autocrlf)
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/main.py", "x\n")
        scene.commit()
        self.assertEqual(
            run_cli(scene.root, "generate", "--source", "head").returncode, 0
        )
        scene.commit("lock")
        return scene

    def _baseline(self, scene: Scenario, source: str) -> None:
        result = run_cli(
            scene.root, "verify", "--source", source, "--write-baseline", "b.json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        scene.git("add", "--all")
        scene.git("commit", "-m", "baseline")

    def test_an_untouched_baseline_updates_when_nothing_normalized_it(self):
        """The premise: this command really can succeed.

        Every refusal below is an assertion that a publish did not happen.
        With line endings pinned off, the same sequence on the same fixture
        publishes and exits zero, so a refusal further down is the comparison
        firing rather than the command being broken.
        """
        scene = self._locked()
        try:
            self._baseline(scene, "head")
            self.assertEqual(scene.git("status", "--porcelain"), "")
            result = run_cli(
                scene.root, "verify", "--source", "head", "--update-baseline", "b.json"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr.strip(), "")
        finally:
            scene.close()

    def test_an_unstaged_edit_is_refused(self):
        """The positive control: grossly different bytes draw the refusal."""
        scene = self._locked()
        try:
            self._baseline(scene, "head")
            (scene.root / "b.json").write_bytes(b"unstaged head bytes" + LF)
            result = run_cli(
                scene.root, "verify", "--source", "head", "--update-baseline", "b.json"
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stderr.strip(), BASELINE_REFUSAL)
        finally:
            scene.close()

    def test_a_normalizing_checkout_alone_makes_the_update_impossible(self):
        """The case the obligation names and the suite never had.

        Nobody edits anything. `core.autocrlf=true` is the setting a large
        share of Windows clones carry, and under it a checkout writes CRLF to
        the working tree while the blob keeps LF - `_git_cat_blob` is
        documented as doing no text-mode conversion. Git calls the tree clean.
        The comparison in `_require_expected_live_bytes` is over raw bytes, so
        it refuses, and the diagnostic blames a competing writer that does not
        exist. The obligation demands exactly this refusal; the cost is that
        `--update-baseline` is unreachable in such a clone.
        """
        for source in ("head", "index"):
            with self.subTest(source=source):
                scene = self._locked(autocrlf="true")
                try:
                    self._baseline(scene, source)
                    baseline = scene.root / "b.json"
                    baseline.unlink()
                    scene.git("checkout", "--", "b.json")

                    blob = scene.blob("b.json")
                    live = baseline.read_bytes()
                    self.assertNotIn(b"\r", blob)
                    self.assertIn(b"\r\n", live)
                    self.assertNotEqual(blob, live)
                    self.assertEqual(blob, live.replace(b"\r\n", LF))
                    self.assertEqual(scene.git("status", "--porcelain"), "")

                    result = run_cli(
                        scene.root,
                        "verify",
                        "--source",
                        source,
                        "--update-baseline",
                        "b.json",
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stderr.strip(), BASELINE_REFUSAL)
                    self.assertEqual(baseline.read_bytes(), live)
                finally:
                    scene.close()


if __name__ == "__main__":
    unittest.main()
