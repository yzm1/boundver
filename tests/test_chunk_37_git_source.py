"""Six claims that only bite where two conditions meet at once.

Every obligation here asks the product to distinguish inputs that look similar
at first glance: a directory-only ignore rule from a name rule, a POSIX
backslash from a path separator, provider aliases from their canonical names,
raw bytes from canonical documents, and competing review unavailability
reasons.

Finding inputs that force the choice took most of the work. For the line-ending
obligation the discriminating input is not a digest at all: a document the
strict parser rejects comes back with the offset of the byte it stopped on, so
a canonical provider that had normalised CRLF to LF before parsing reports the
LF document's offset for the CRLF document. The fixtures compute those offsets
from their own bytes with ``bytes.index`` and require the reported offset to
match, which kills a normalising read path that no digest comparison can reach:
every canonical CRLF/LF digest pairing agrees whether or not the normalisation
is applied, because JSON whitespace is insignificant either way. Two witnesses
are needed rather than one. The obvious one, a raw NUL inside a string, is
invisible to a mutant that copies the NUL guard along with the replacement -
its own guard suppresses it on exactly the document that would have caught it -
so a NUL-free malformed document sits beside it. For the precedence obligation the two
conditions have to be made to hold simultaneously without a custom provider
module on disk, so the registry error comes from a malformed ``providers``
entry that fails before any import is attempted, the capability failures come
from a doctored registry injected at the ``create_registry`` seam, and the
exhausted budget comes from a ``StructuralDiffBudget`` subclass that reports
itself spent from birth. Each of those needed its own premise: an exhausted
budget only reports ``limit-exceeded`` for a provider that would otherwise have
passed the capability gate, which on this registry is ``openapi-canonical``
alone, so the premise had to use that provider and not the ``json-canonical``
the other rows use.

Git itself is the ignore-rule oracle. Explicit ``--base-ref`` values follow the
same immutable resolution rule as review and disclose the requested name beside
its commit object ID. The backslash filename assertion is POSIX-only because a
backslash is a path separator on Windows.

Covers OBL-GIT-SOURCE-116, OBL-GIT-SOURCE-117, OBL-GIT-SOURCE-133,
OBL-HASHING-088, OBL-HASHING-089 and OBL-PROVIDERS-024.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from boundver import _structural_review as structural_review
from boundver._git import (
    _capture_git_source_snapshot,
    _GitignoreRules,
    _is_ignored,
    _list_files_for_source,
    _to_posix,
)
from boundver._hashing import _normalize_hash_content
from boundver._lockfile import _SourceAccessor
from boundver._provider_diff import STRUCTURAL_DIFF_INTERFACE
from boundver._review import _ReviewWorkBudget
from boundver.providers import (
    PathHashProvider,
    ProviderContext,
    compute_boundary,
    create_registry,
)

from tests._parity import describe, run_cli
from tests._scenarios import Scenario


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-116 — directory-only gitignore patterns
# ---------------------------------------------------------------------------

#: The two spellings the obligation contrasts, and the two filesystem shapes
#: they have to be told apart on. Git's own answer is not written here; it is
#: read from `git check-ignore` at runtime, because a memorised oracle is worth
#: nothing against an obligation that says the matcher disagrees with Git.
IGNORE_SPELLINGS = ("build/", "build")

#: A second name carries the same contrast without colliding with `_is_ignored`,
#: whose hardcoded list drops anything called `build`, `dist` or `node_modules`
#: for reasons that have nothing to do with .gitignore.
NEUTRAL_NAME = "artifacts"


def _rules(*lines: str) -> _GitignoreRules:
    ruleset = _GitignoreRules()
    for line in lines:
        ruleset.add(line)
    return ruleset


def _git_says_ignored(root: Path, path: str) -> bool:
    """Git's own verdict. Exit 0 means ignored, exit 1 means not ignored."""
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", path],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise AssertionError(
            f"git check-ignore failed for {path!r}: "
            f"rc={result.returncode} stderr={result.stderr!r}"
        )
    return result.returncode == 0


class DirectoryOnlyIgnorePatternTests(unittest.TestCase):
    """OBL-GIT-SOURCE-116: `build/` is a directory, `build` is a name."""

    def test_git_distinguishes_the_two_spellings_on_a_regular_file(self):
        """Use Git itself to pin the regular-file distinction."""
        with Scenario() as scene:
            (scene.root / "build").write_text("a regular file\n", encoding="utf-8")
            observed = {}
            for spelling in IGNORE_SPELLINGS:
                (scene.root / ".gitignore").write_text(
                    spelling + "\n", encoding="utf-8"
                )
                observed[spelling] = _git_says_ignored(scene.root, "build")
            self.assertEqual(observed, {"build/": False, "build": True})

    def test_git_ignores_a_real_directory_under_both_spellings(self):
        """The direction where the two spellings agree, which is why the
        existing tests could not see the difference."""
        with Scenario() as scene:
            (scene.root / "build").mkdir()
            (scene.root / "build" / "x.txt").write_text("x\n", encoding="utf-8")
            observed = {}
            for spelling in IGNORE_SPELLINGS:
                (scene.root / ".gitignore").write_text(
                    spelling + "\n", encoding="utf-8"
                )
                observed[spelling] = _git_says_ignored(scene.root, "build/x.txt")
            self.assertEqual(observed, {"build/": True, "build": True})

    def test_the_matcher_can_report_a_path_as_not_ignored(self):
        """The premise: `is_ignored` is not constantly True, so a True answer
        below is a match and not the only answer it knows how to give."""
        ruleset = _rules("build/")
        self.assertFalse(ruleset.is_ignored("src/main.py"))
        self.assertFalse(ruleset.is_ignored("builder"))

    def test_the_trailing_slash_is_preserved_as_directory_metadata(self):
        self.assertEqual(
            _rules("build/")._rules,
            [(False, "build", False, True)],
        )
        self.assertEqual(
            _rules("build")._rules,
            [(False, "build", False, False)],
        )

    def test_a_directory_only_rule_leaves_a_regular_file_of_that_name_alone(self):
        """The fallback retains Git's file-versus-directory distinction."""
        self.assertFalse(_rules("build/").is_ignored("build"))

    def test_a_directory_only_rule_ignores_a_directory_of_that_name(self):
        self.assertTrue(_rules("build/").is_ignored("build", is_dir=True))
        self.assertTrue(
            _rules(f"{NEUTRAL_NAME}/").is_ignored(NEUTRAL_NAME, is_dir=True)
        )

    def test_the_spelling_without_a_slash_agrees_with_git_in_both_directions(self):
        ruleset = _rules("build")
        self.assertTrue(ruleset.is_ignored("build"))
        self.assertTrue(ruleset.is_ignored("build/x.txt"))

    def test_a_regular_file_remains_in_the_fallback_listing(self):
        """A directory-only rule must not remove a same-named regular file."""
        self.assertFalse(_is_ignored(Path(NEUTRAL_NAME)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)  # deliberately not a Git repository
            (root / "svc").mkdir()
            (root / "svc" / "keep.txt").write_text("k\n", encoding="utf-8")
            (root / "svc" / NEUTRAL_NAME).write_text("regular\n", encoding="utf-8")
            listed = _list_files_for_source(root, "svc", "working-tree")
            self.assertEqual(
                listed, [f"svc/{NEUTRAL_NAME}", "svc/keep.txt"],
                "premise: without a rule the regular file is part of the corpus",
            )
            (root / ".gitignore").write_text(
                f"{NEUTRAL_NAME}/\n", encoding="utf-8"
            )
            self.assertEqual(
                _list_files_for_source(root, "svc", "working-tree"),
                [f"svc/{NEUTRAL_NAME}", "svc/keep.txt"],
            )


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-117 — a literal backslash is one path segment on POSIX
# ---------------------------------------------------------------------------

class LiteralBackslashSegmentTests(unittest.TestCase):
    """OBL-GIT-SOURCE-117: `a\\b` and `a/b` are two files, not one."""

    def test_the_fallback_lister_really_consults_the_ignore_matcher(self):
        """The premise for everything below: `_GitignoreRules.is_ignored` is on
        the path a file actually travels, so a rule that matches removes it."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)  # deliberately not a Git repository
            (root / "svc").mkdir()
            (root / "svc" / "keep.txt").write_text("k\n", encoding="utf-8")
            (root / "svc" / "drop.txt").write_text("d\n", encoding="utf-8")
            self.assertEqual(
                _list_files_for_source(root, "svc", "working-tree"),
                ["svc/drop.txt", "svc/keep.txt"],
            )
            (root / ".gitignore").write_text("svc/drop.txt\n", encoding="utf-8")
            self.assertEqual(
                _list_files_for_source(root, "svc", "working-tree"),
                ["svc/keep.txt"],
            )

    def test_the_helper_the_module_documents_is_platform_aware(self):
        """`_to_posix` at `_git.py:2250` carries the comment that a blind string
        replacement would collapse two distinct Git paths on POSIX. It keeps the
        backslash there and converts it here, which is the whole point."""
        expected = "a/b" if os.name == "nt" else "a\\b"
        self.assertEqual(_to_posix("a\\b"), expected)

    def test_the_matcher_uses_platform_aware_path_normalization(self):
        self.assertEqual(_rules("a/b").is_ignored("a\\b"), os.name == "nt")
        self.assertTrue(_rules("a/b").is_ignored("a/b"))

    def test_a_backslash_quotes_the_following_character_in_a_rule(self):
        ruleset = _rules("a\\b")
        self.assertEqual(ruleset._rules, [(False, "ab", False, False)])
        self.assertTrue(ruleset.is_ignored("ab"))
        self.assertFalse(ruleset.is_ignored("a/b"))

    @unittest.skipIf(
        os.name == "nt",
        "a backslash is a real path separator on Windows, so the obligation's "
        "POSIX-only claim has no witness on this host",
    )
    def test_a_literal_backslash_filename_is_not_matched_by_a_nested_rule(self):
        """A POSIX backslash filename is distinct from a nested path."""
        self.assertFalse(_rules("a/b").is_ignored("a\\b"))

    @unittest.skipIf(
        os.name == "nt",
        "a backslash is a path separator on Windows",
    )
    def test_a_quoted_backslash_matches_a_literal_posix_backslash(self):
        ruleset = _rules("a\\\\b")
        self.assertEqual(ruleset._rules, [(False, "a\\b", False, False)])
        self.assertTrue(ruleset.is_ignored("a\\b"))
        self.assertFalse(ruleset.is_ignored("a/b"))


# ---------------------------------------------------------------------------
# OBL-HASHING-088 — every raw provider name is one digest
# ---------------------------------------------------------------------------

#: The nine names the obligation lists. The surface itself is derived from the
#: live registry below; this is here so that a tenth raw provider makes the
#: roster check fail loudly rather than quietly widening the obligation.
OBLIGATION_RAW_NAMES = frozenset(
    {
        "path-hash",
        "openapi",
        "openapi-raw",
        "json-file",
        "json-file-raw",
        "python-exports",
        "python-exports-raw",
        "typescript-exports",
        "typescript-exports-raw",
    }
)

#: One selector name shared by every component, because the entry label carries
#: the component-relative filename into the digest.
RAW_SELECTOR = "contract.json"

#: Valid JSON, so a canonical provider could read it too if the enumeration ever
#: widened past the raw classes.
RAW_DOCUMENT = json.dumps({"a": 1, "b": [2, 3]}, indent=2) + "\n"


def _raw_provider_names(registry: Dict[str, Any]) -> List[str]:
    """The raw surface, read from the registry rather than listed."""
    return sorted(
        name
        for name, provider in registry.items()
        if isinstance(provider, PathHashProvider)
    )


def _context(scene: Scenario, component: str, accessor: _SourceAccessor):
    return ProviderContext(
        repo_root=scene.root,
        component_path=scene.config["components"][component]["path"],
        boundary_cfg=scene.config["components"][component]["boundary"],
        source="head",
        read_file=accessor.read_file,
        read_file_limited=accessor.read_file_limited,
        list_files=accessor.list_files,
    )


class RawProviderNameEquivalenceTests(unittest.TestCase):
    """OBL-HASHING-088: relabelling a raw provider is digest-neutral."""

    def setUp(self):
        self.registry = create_registry()
        self.raw_names = _raw_provider_names(self.registry)

    def _slug(self, name: str) -> str:
        return name.replace("-", "_")

    def _one_component_each(self, scene: Scenario, document: str = RAW_DOCUMENT):
        for name in self.raw_names:
            slug = self._slug(name)
            scene.component(
                slug, path=f"c/{slug}", provider=name, boundary=[RAW_SELECTOR]
            )
            scene.file(f"c/{slug}/{RAW_SELECTOR}", document)
        scene.commit()

    def test_the_enumerated_raw_surface_is_the_roster_the_obligation_names(self):
        self.assertEqual(
            set(self.raw_names),
            set(OBLIGATION_RAW_NAMES),
            "the raw provider surface moved; the register's list of names "
            "must move with it",
        )

    def test_each_alias_resolves_to_the_very_provider_it_renames(self):
        for alias, target in (
            ("openapi-raw", "openapi"),
            ("json-file-raw", "json-file"),
            ("python-exports-raw", "python-exports"),
            ("typescript-exports-raw", "typescript-exports"),
        ):
            with self.subTest(alias=alias):
                self.assertIs(self.registry[alias], self.registry[target])

    def test_every_registered_raw_name_yields_one_boundary_digest(self):
        with Scenario() as scene:
            self._one_component_each(scene)
            accessor = _SourceAccessor(scene.root, "head")
            digests = {}
            for name in self.raw_names:
                digest, status, errors = compute_boundary(
                    self.registry[name], _context(scene, self._slug(name), accessor)
                )
                self.assertEqual(status, "ok", f"{name}: {errors}")
                self.assertIsNotNone(digest, name)
                digests[name] = digest
            self.assertEqual(
                len(set(digests.values())), 1, describe(digests)
            )

    def test_that_shared_digest_is_not_a_constant(self):
        """The premise. Nine names agreeing means nothing unless the digest
        moves when the selected bytes or the entry label move."""
        with Scenario() as scene:
            self._one_component_each(scene)
            accessor = _SourceAccessor(scene.root, "head")
            reference, _, _ = compute_boundary(
                self.registry["path-hash"], _context(scene, "path_hash", accessor)
            )
        with Scenario() as scene:
            self._one_component_each(scene, document=RAW_DOCUMENT + "\n")
            accessor = _SourceAccessor(scene.root, "head")
            other_bytes, _, _ = compute_boundary(
                self.registry["path-hash"], _context(scene, "path_hash", accessor)
            )
        self.assertNotEqual(reference, other_bytes, "content is not hashed")
        with Scenario() as scene:
            scene.component(
                "path_hash", path="c/path_hash", provider="path-hash",
                boundary=["other.json"],
            )
            scene.file("c/path_hash/other.json", RAW_DOCUMENT)
            scene.commit()
            accessor = _SourceAccessor(scene.root, "head")
            other_label, _, _ = compute_boundary(
                self.registry["path-hash"], _context(scene, "path_hash", accessor)
            )
        self.assertNotEqual(reference, other_label, "the entry label is not hashed")

    def test_only_the_recorded_provider_name_differs_in_the_lock(self):
        """One component, one path, one document, nine spellings of the provider
        - so every field of the lock entry is directly comparable."""
        entries = {}
        for name in self.raw_names:
            with Scenario() as scene:
                scene.component(
                    "svc", path="svc", provider=name, boundary=[RAW_SELECTOR]
                )
                scene.file(f"svc/{RAW_SELECTOR}", RAW_DOCUMENT)
                scene.commit()
                entries[name] = scene.generate()["components"]["svc"]
        reference = entries[self.raw_names[0]]
        differing = set()
        for name, entry in entries.items():
            self.assertEqual(set(entry), set(reference), name)
            differing.update(
                field for field in entry if entry[field] != reference[field]
            )
        self.assertEqual(differing, {"boundary_provider"}, describe(entries))
        self.assertEqual(
            {name: entry["boundary_provider"] for name, entry in entries.items()},
            {name: name for name in self.raw_names},
        )


# ---------------------------------------------------------------------------
# OBL-HASHING-089 — CRLF collapse, guarded by the NUL byte
# ---------------------------------------------------------------------------

#: The guard as `_hashing._normalize_hash_content` states it, and as the raw
#: providers restate it at `providers.py:988`. Each row is input -> output.
NORMALIZATION_TABLE = {
    "crlf text collapses": (b"a\r\nb\r\n", b"a\nb\n"),
    "lf text is untouched": (b"a\nb\n", b"a\nb\n"),
    "crlf beside a nul is untouched": (b"a\r\nb\x00\r\n", b"a\r\nb\x00\r\n"),
    "lf beside a nul is untouched": (b"a\nb\x00\n", b"a\nb\x00\n"),
    "a lone cr is untouched": (b"a\rb", b"a\rb"),
}

#: A JSON document with a raw NUL inside a string, written twice with the two
#: line-ending conventions. Both are rejected by the strict parser; the offset
#: it reports is what tells a normalising read path from an honest one.
NUL_CRLF = b'{\r\n  "a": 1,\r\n  "b": "\x00"\r\n}\r\n'
NUL_LF = b'{\n  "a": 1,\n  "b": "\x00"\n}\n'

#: The same trick without a NUL anywhere, so that a normalisation carrying the
#: NUL guard with it is caught too: `%` cannot start a JSON value, and the
#: parser reports where it found it. Verified against both mutant shapes; the
#: NUL pair alone lets a guarded mutant through, because its own guard then
#: suppresses the mutation on exactly the witness that would have seen it.
SYNTAX_CRLF = b'{\r\n  "a": 1,\r\n  "b": %\r\n}\r\n'
SYNTAX_LF = b'{\n  "a": 1,\n  "b": %\n}\n'

#: Each row: the payload, and the byte whose position the parser must report.
PARSE_OFFSET_WITNESSES = {
    "nul, crlf framing": (NUL_CRLF, b"\x00"),
    "nul, lf framing": (NUL_LF, b"\x00"),
    "syntax error, crlf framing": (SYNTAX_CRLF, b"%"),
    "syntax error, lf framing": (SYNTAX_LF, b"%"),
}

#: The same JSON value with the two conventions, no NUL anywhere.
PLAIN_CRLF = b'{\r\n  "note": "one"\r\n}\r\n'
PLAIN_LF = b'{\n  "note": "one"\n}\n'

#: Two distinct JSON values that differ only in an escaped line ending inside a
#: string literal. Nothing at the byte level tells these apart from each other
#: by a CRLF collapse, which is why the canonical clause needs them.
ESCAPED_CRLF = b'{\n  "note": "one\\r\\ntwo"\n}\n'
ESCAPED_LF = b'{\n  "note": "one\\ntwo"\n}\n'


def _enum_contract(separator: str) -> bytes:
    """An OpenAPI 3.1 contract whose only variable is one enum value."""
    return (
        json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "t", "version": "1.0.0"},
                "paths": {},
                "components": {
                    "schemas": {
                        "Sep": {"type": "string", "enum": [f"one{separator}two"]}
                    }
                },
            },
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


#: The same contract twice, differing only in a line ending inside an enum
#: value that `_strip_openapi` keeps.
ENUM_CRLF = _enum_contract("\r\n")
ENUM_LF = _enum_contract("\n")


class LineEndingNormalizationTests(unittest.TestCase):
    """OBL-HASHING-089: collapse CRLF, but never across a NUL, and never in a
    canonical provider."""

    def setUp(self):
        self.registry = create_registry()

    def _resolve(self, provider: str, payload: bytes) -> Tuple[Optional[str], str, list, bytes]:
        with Scenario() as scene:
            scene.component(
                "svc", path="svc", provider=provider, boundary=["payload.json"]
            )
            target = scene.root / "svc" / "payload.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            scene.commit()
            stored = scene.blob("svc/payload.json")
            accessor = _SourceAccessor(scene.root, "head")
            digest, status, errors = compute_boundary(
                self.registry[provider], _context(scene, "svc", accessor)
            )
            return digest, status, errors, stored

    def test_the_normalizer_applies_the_nul_guard_it_documents(self):
        for label, (raw, expected) in NORMALIZATION_TABLE.items():
            with self.subTest(case=label):
                self.assertEqual(_normalize_hash_content(raw), expected)

    def test_git_stores_the_line_endings_the_fixture_wrote(self):
        """The premise for every equality below. Under `core.autocrlf=true` Git
        would rewrite the CRLF file on the way into the index and the raw
        providers would then be agreeing about identical bytes."""
        _, _, _, crlf = self._resolve("path-hash", PLAIN_CRLF)
        _, _, _, lf = self._resolve("path-hash", PLAIN_LF)
        self.assertEqual(crlf, PLAIN_CRLF)
        self.assertEqual(lf, PLAIN_LF)
        self.assertNotEqual(crlf, lf)

    def test_a_raw_provider_hashes_a_crlf_file_as_its_lf_twin(self):
        crlf, status, errors, _ = self._resolve("path-hash", PLAIN_CRLF)
        self.assertEqual(status, "ok", errors)
        lf, _, _, _ = self._resolve("path-hash", PLAIN_LF)
        self.assertEqual(crlf, lf)

    def test_a_raw_provider_keeps_a_nul_bearing_pair_apart(self):
        crlf, crlf_status, crlf_errors, crlf_bytes = self._resolve(
            "path-hash", NUL_CRLF
        )
        lf, lf_status, lf_errors, lf_bytes = self._resolve("path-hash", NUL_LF)
        self.assertEqual(crlf_status, "ok", crlf_errors)
        self.assertEqual(lf_status, "ok", lf_errors)
        self.assertEqual(crlf_bytes, NUL_CRLF)
        self.assertEqual(lf_bytes, NUL_LF)
        self.assertNotEqual(
            crlf, lf,
            "the NUL guard was dropped: two different binaries now collide",
        )

    def test_an_escaped_line_ending_is_a_distinct_canonical_value(self):
        """The harm the obligation names: a contract whose enum value embeds
        \\r\\n must not compare equal to one that embeds \\n. The OpenAPI pair
        puts the sequence in an enum precisely because `_strip_openapi` throws
        descriptions away and would have thrown the witness away with them."""
        pairs = {
            "json-canonical": (ESCAPED_CRLF, ESCAPED_LF),
            "openapi-canonical": (ENUM_CRLF, ENUM_LF),
            "path-hash": (ESCAPED_CRLF, ESCAPED_LF),
        }
        for provider, (crlf_bytes, lf_bytes) in pairs.items():
            with self.subTest(provider=provider):
                self.assertNotIn(
                    b"\r\n", crlf_bytes,
                    "premise: the witness is an escape sequence, not a raw "
                    "CRLF, so no byte-level collapse can reach it",
                )
                crlf, status, errors, _ = self._resolve(provider, crlf_bytes)
                self.assertEqual(status, "ok", errors)
                lf, lf_status, lf_errors, _ = self._resolve(provider, lf_bytes)
                self.assertEqual(lf_status, "ok", lf_errors)
                self.assertNotEqual(crlf, lf)

    def test_a_canonical_digest_survives_a_reformat_into_crlf(self):
        """Agreement here comes from canonicalisation, not from normalisation -
        which is exactly why this pairing cannot police the canonical clause and
        the parse-offset test below has to."""
        crlf, status, errors, crlf_bytes = self._resolve("json-canonical", PLAIN_CRLF)
        self.assertEqual(status, "ok", errors)
        lf, _, _, lf_bytes = self._resolve("json-canonical", PLAIN_LF)
        self.assertNotEqual(crlf_bytes, lf_bytes)
        self.assertEqual(crlf, lf)

    def test_the_canonical_read_path_parses_the_bytes_it_was_handed(self):
        """The discriminator. A canonical provider that collapsed CRLF before
        parsing would report the LF document's offset for the CRLF document,
        and no digest comparison inside the accepted JSON language can see that
        - every canonical CRLF/LF pairing agrees either way.

        The expected offsets are read out of the fixtures with `bytes.index`,
        so the oracle does not depend on the code under test.
        """
        for label, (payload, marker) in PARSE_OFFSET_WITNESSES.items():
            with self.subTest(witness=label):
                digest, status, errors, stored = self._resolve(
                    "json-canonical", payload
                )
                self.assertEqual(stored, payload, "premise: bytes reached the blob")
                self.assertEqual(status, "error", digest)
                match = re.search(r"\(char (\d+)\)", errors[0])
                self.assertIsNotNone(
                    match,
                    f"the parser stopped reporting an offset: {errors[0]!r}",
                )
                self.assertEqual(
                    int(match.group(1)),
                    payload.index(marker),
                    "the canonical read path normalised the source before "
                    "parsing it",
                )
        for crlf, lf, marker in (
            (NUL_CRLF, NUL_LF, b"\x00"),
            (SYNTAX_CRLF, SYNTAX_LF, b"%"),
        ):
            self.assertEqual(
                crlf.index(marker) - lf.index(marker),
                2,
                "premise: the two framings really do put the reported byte at "
                "different offsets, so the assertion above can fail",
            )
        self.assertNotIn(
            b"\x00", SYNTAX_CRLF,
            "premise: the syntax-error witness carries no NUL, so a "
            "normalisation that kept the NUL guard would still reach it",
        )


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-024 — unavailability reason precedence
# ---------------------------------------------------------------------------

#: A document that is valid JSON and a valid OpenAPI 3.1 contract, so both
#: canonical providers can be named in a lock entry against it.
STRUCTURAL_DOCUMENT = json.dumps(
    {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1.0.0"},
        "paths": {"/ping": {"get": {"responses": {"200": {"description": "ok"}}}}},
    },
    indent=2,
) + "\n"

#: A `providers` declaration that fails before any import is attempted, so the
#: registry-load error needs no module on disk.
BROKEN_PROVIDERS = [["not an object"]]


class _ExhaustedBudget(structural_review.StructuralDiffBudget):
    """A structural-diff budget that reports itself spent from birth."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.exhausted = True


class _StubProvider:
    """A registry member that clears the capability gate."""

    name = "openapi-canonical"
    version = "4"
    structural_diff_interface = STRUCTURAL_DIFF_INTERFACE

    def resolve(self, ctx):  # pragma: no cover - never reached
        raise NotImplementedError

    def structural_diff(self, base_ctx, target_ctx, budget):  # pragma: no cover
        raise NotImplementedError


class _OtherStubProvider(_StubProvider):
    """The same identity, a different implementation type."""


class _OldInterfaceProvider(_StubProvider):
    structural_diff_interface = "boundver-structural-diff/v0"


def _lock_entry(provider: str, version: str, digest: str) -> dict:
    return {
        "path": "svc",
        "boundary_provider": provider,
        "boundary_provider_version": version,
        "fingerprints": {"boundary": digest},
    }


#: A component present at both endpoints with nothing wrong with it.
SOUND = _lock_entry("json-canonical", "1", "a" * 64)


def _doctored_registry(cls) -> dict:
    registry = create_registry()
    registry["openapi-canonical"] = cls()
    return registry


class UnavailabilityReasonPrecedenceTests(unittest.TestCase):
    """OBL-PROVIDERS-024: the reason is the first condition in a fixed order."""

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario()
        cls.scene.component(
            "svc", path="svc", provider="openapi-canonical",
            boundary=["contract.json"],
        )
        cls.scene.file("svc/contract.json", STRUCTURAL_DOCUMENT)
        cls.scene.commit()
        cls.snapshot = _capture_git_source_snapshot(cls.scene.root, "head")

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def _report(
        self,
        *,
        base_entry: Optional[dict],
        target_entry: Optional[dict],
        base_providers: Optional[list] = None,
        allow_custom: bool = False,
        exhausted: bool = False,
        registries: Optional[list] = None,
    ) -> dict:
        base_config = dict(self.scene.config)
        if base_providers is not None:
            base_config = {**base_config, "providers": base_providers}
        target_config = dict(self.scene.config)
        base_lock = {"components": {"svc": base_entry} if base_entry else {}}
        target_lock = {"components": {"svc": target_entry} if target_entry else {}}
        budget_class = _ExhaustedBudget if exhausted else structural_review.StructuralDiffBudget
        registry_factory = (
            mock.Mock(side_effect=list(registries))
            if registries is not None
            else structural_review.create_registry
        )
        with mock.patch.object(
            structural_review, "StructuralDiffBudget", budget_class
        ), mock.patch.object(
            structural_review, "create_registry", registry_factory
        ):
            result = structural_review.structural_boundary_changes(
                self.scene.root,
                self.snapshot,
                self.snapshot,
                base_config,
                target_config,
                base_lock,
                target_lock,
                [{"name": "svc", "facets": [{"facet": "boundary"}]}],
                base_ref="base",
                target_ref="target",
                requested_base_commit=self.snapshot.head_oid,
                requested_target_commit=self.snapshot.head_oid,
                allow_custom_providers=allow_custom,
                review_budget=_ReviewWorkBudget(),
            )
        return result["reports"][0]

    #: One condition at a time. These are the premises: each reason below is
    #: reachable on its own, so a pair reporting one of them is a choice.
    SINGLE = {
        "component absent at base": (
            dict(base_entry=None, target_entry=SOUND),
            "component-absent",
            "Structural comparison requires the component at both endpoints",
        ),
        "provider name changed": (
            dict(base_entry=SOUND, target_entry=_lock_entry("json-file", "1", "b" * 64)),
            "provider-changed",
            "Structural comparison requires the same provider at both endpoints",
        ),
        "provider version changed": (
            dict(
                base_entry=SOUND,
                target_entry=_lock_entry("json-canonical", "2", "b" * 64),
            ),
            "provider-version-changed",
            "Structural comparison requires the same provider version at both "
            "endpoints",
        ),
        "registry load error": (
            dict(
                base_entry=SOUND, target_entry=SOUND,
                base_providers=BROKEN_PROVIDERS, allow_custom=True,
            ),
            "provider-unavailable",
            "Provider entry must be an object, got list",
        ),
        "provider not in either registry": (
            dict(
                base_entry=_lock_entry("ghost", "1", "a" * 64),
                target_entry=_lock_entry("ghost", "1", "b" * 64),
            ),
            "provider-unavailable",
            "Provider 'ghost' is unavailable at one or both endpoints",
        ),
        "provider publishes no structural diff": (
            dict(
                base_entry=_lock_entry("path-hash", "1", "a" * 64),
                target_entry=_lock_entry("path-hash", "1", "b" * 64),
            ),
            "provider-unsupported",
            "Provider 'path-hash' does not expose bounded structural diff output",
        ),
        "implementation differs between endpoints": (
            dict(
                base_entry=_lock_entry("openapi-canonical", "4", "a" * 64),
                target_entry=_lock_entry("openapi-canonical", "4", "b" * 64),
                registries=[
                    _doctored_registry(_StubProvider),
                    _doctored_registry(_OtherStubProvider),
                ],
            ),
            "provider-implementation-changed",
            "Structural comparison requires the same provider implementation at "
            "both endpoints",
        ),
        "interface version is not this host's": (
            dict(
                base_entry=_lock_entry("openapi-canonical", "4", "a" * 64),
                target_entry=_lock_entry("openapi-canonical", "4", "b" * 64),
                registries=[
                    _doctored_registry(_OldInterfaceProvider),
                    _doctored_registry(_OldInterfaceProvider),
                ],
            ),
            "provider-interface-unsupported",
            "Provider structural-diff interface is not supported by this host "
            f"(expected {STRUCTURAL_DIFF_INTERFACE})",
        ),
    }

    #: Two conditions at once, and the reason the fixed order requires. Each
    #: row's two conditions both appear on their own in SINGLE above.
    PAIRS = {
        "absent at base beats a renamed provider": (
            dict(
                base_entry=None,
                target_entry=_lock_entry("json-file", "9", "b" * 64),
            ),
            "component-absent",
        ),
        "a renamed provider beats a bumped version": (
            dict(
                base_entry=SOUND,
                target_entry=_lock_entry("json-file", "9", "b" * 64),
            ),
            "provider-changed",
        ),
        "a bumped version beats a failing registry": (
            dict(
                base_entry=SOUND,
                target_entry=_lock_entry("json-canonical", "9", "b" * 64),
                base_providers=BROKEN_PROVIDERS,
                allow_custom=True,
            ),
            "provider-version-changed",
        ),
        "a failing registry beats a missing provider": (
            dict(
                base_entry=_lock_entry("ghost", "1", "a" * 64),
                target_entry=_lock_entry("ghost", "1", "b" * 64),
                base_providers=BROKEN_PROVIDERS,
                allow_custom=True,
            ),
            "provider-unavailable",
        ),
        "a capability failure beats an exhausted budget": (
            dict(
                base_entry=_lock_entry("path-hash", "1", "a" * 64),
                target_entry=_lock_entry("path-hash", "1", "b" * 64),
                exhausted=True,
            ),
            "provider-unsupported",
        ),
        "a changed implementation beats an unsupported interface": (
            dict(
                base_entry=_lock_entry("openapi-canonical", "4", "a" * 64),
                target_entry=_lock_entry("openapi-canonical", "4", "b" * 64),
                registries=[
                    _doctored_registry(_OldInterfaceProvider),
                    _doctored_registry(_OtherStubProvider),
                ],
            ),
            "provider-implementation-changed",
        ),
    }

    def test_each_condition_reports_its_own_reason_on_its_own(self):
        for label, (kwargs, reason, detail) in self.SINGLE.items():
            with self.subTest(condition=label):
                report = self._report(**kwargs)
                self.assertEqual(report["status"], "unavailable")
                self.assertEqual(report["reason"], reason)
                self.assertEqual(report["detail"], detail)
                self.assertFalse(report["truncated"])

    def test_an_exhausted_budget_alone_reports_limit_exceeded(self):
        """The premise the budget rows need. `openapi-canonical` is the only
        registered provider that clears the capability gate, so it is the only
        one that can reach the budget check at all."""
        report = self._report(
            base_entry=_lock_entry("openapi-canonical", "4", "a" * 64),
            target_entry=_lock_entry("openapi-canonical", "4", "b" * 64),
            exhausted=True,
        )
        self.assertEqual(report["reason"], "limit-exceeded")
        self.assertTrue(report["truncated"])
        self.assertEqual(
            report["detail"],
            "The aggregate structural-diff budget was already exhausted; "
            "no partial rows were retained",
        )

    def test_the_capability_gate_can_be_cleared(self):
        """The other premise: with a whole budget and a supported provider the
        chain runs past every unavailability branch and calls the provider."""
        report = self._report(
            base_entry=_lock_entry("openapi-canonical", "4", "a" * 64),
            target_entry=_lock_entry("openapi-canonical", "4", "b" * 64),
        )
        self.assertEqual(
            report["detail"], "Changed boundary digest produced no structural changes"
        )

    def test_two_conditions_at_once_report_the_first_in_the_fixed_order(self):
        for label, (kwargs, reason) in self.PAIRS.items():
            with self.subTest(pair=label):
                self.assertEqual(self._report(**kwargs)["reason"], reason)

    def test_a_component_absent_at_base_with_a_renamed_provider_is_absent(self):
        """The obligation names this case explicitly."""
        report = self._report(
            base_entry=None, target_entry=_lock_entry("json-file", "9", "b" * 64)
        )
        self.assertEqual(report["reason"], "component-absent")
        self.assertEqual(
            report["detail"],
            "Structural comparison requires the component at both endpoints",
        )
        self.assertEqual(report["inputs"]["base"]["present"], False)
        self.assertEqual(report["inputs"]["target"]["provider"], "json-file")

    def test_the_failing_registry_row_is_told_apart_by_its_detail(self):
        """Two conditions share the reason `provider-unavailable`, so the pair
        that ranks them can only be read from the detail string."""
        both = self._report(
            base_entry=_lock_entry("ghost", "1", "a" * 64),
            target_entry=_lock_entry("ghost", "1", "b" * 64),
            base_providers=BROKEN_PROVIDERS,
            allow_custom=True,
        )
        self.assertEqual(both["detail"], "Provider entry must be an object, got list")
        self.assertNotEqual(
            both["detail"], "Provider 'ghost' is unavailable at one or both endpoints"
        )


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-133 — explicit diagnostic refs resolve to immutable commits
# ---------------------------------------------------------------------------

#: A 40-character lowercase hex commit id, which is what the obligation asks the
#: disclosure to carry and what `review` already puts there.
COMMIT_OID = re.compile(r"\b[0-9a-f]{40}\b")


def _explain_repository() -> Scenario:
    """A locked history with a branch `moving` left behind at the first lock.

    The branch is what makes the reproducibility question concrete: moving it
    changes the comparison without changing anything the command discloses. Both
    endpoints carry a committed config and lock, because `review` - the contrast
    these tests are built around - refuses an endpoint that has neither. The
    intermediate commit is tagged rather than reached with `HEAD~n`, so the
    fixture stays readable when a commit is added to it.
    """
    scene = Scenario()
    scene.component("svc", path="svc", provider="path-hash", boundary=["api.json"])
    scene.file("svc/api.json", '{"v": 1}\n')
    scene.file("svc/extra.txt", "one\n")
    scene.commit("first")
    run_cli(scene.root, "generate")
    scene.commit("lock at first")
    scene.git("branch", "moving")
    scene.file("svc/api.json", '{"v": 2}\n')
    scene.commit("second")
    scene.git("tag", "second-commit")
    scene.file("svc/extra.txt", "two\n")
    scene.commit("third")
    run_cli(scene.root, "generate")
    scene.commit("lock at third")
    return scene


def _drifted_repository() -> Scenario:
    """A repository whose lock is one boundary edit out of date, so `why` has a
    changed-file section to fill in."""
    scene = Scenario()
    scene.component("svc", path="svc", provider="path-hash", boundary=["api.json"])
    scene.file("svc/api.json", '{"v": 1}\n')
    scene.commit("first")
    scene.git("branch", "moving")
    scene.file("svc/api.json", '{"v": 2}\n')
    scene.commit("second")
    run_cli(scene.root, "generate")
    scene.git("add", "--all")
    scene.git("commit", "-m", "lock")
    scene.file("svc/api.json", '{"v": 3}\n')
    scene.commit("third")
    return scene


def _line(text: str, prefix: str) -> str:
    for line in text.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no {prefix!r} line in:\n{text}")


class ExplicitBaseRefDisclosureTests(unittest.TestCase):
    """OBL-GIT-SOURCE-133: resolve and disclose an explicit `--base-ref`."""

    def test_the_review_endpoint_resolves_and_discloses_both_halves(self):
        """The premise, and the shape the obligation is asking `explain` and
        `why` to match: the same product already does this one ref away."""
        with _explain_repository() as scene:
            resolved = scene.git("rev-parse", "moving")
            result = run_cli(scene.root, "review", "moving..HEAD")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                _line(result.stdout, "Requested base:"),
                f"Requested base: moving -> {resolved}",
            )
            self.assertEqual(
                _line(result.stdout, "Effective base:"), f"Effective base: {resolved}"
            )
            payload = json.loads(
                run_cli(scene.root, "review", "moving..HEAD", "--format", "json").stdout
            )
            self.assertEqual(payload["endpoints"]["base"]["requested_ref"], "moving")
            self.assertEqual(
                payload["endpoints"]["base"]["requested_commit"], resolved
            )
            self.assertEqual(payload["endpoints"]["base"]["commit"], resolved)

    def test_the_review_endpoint_rejects_an_unknown_ref_as_a_usage_error(self):
        """The other half of the premise: a typo there is a usage error, named
        as one, before any diff runs."""
        with _explain_repository() as scene:
            result = run_cli(scene.root, "review", "no-such-ref..HEAD")
            self.assertEqual(result.returncode, 2)
            self.assertIn(
                "Cannot resolve base Git ref 'no-such-ref' to one commit",
                result.stderr,
            )

    def test_explain_discloses_the_resolved_commit(self):
        with _explain_repository() as scene:
            resolved = scene.git("rev-parse", "moving")
            result = run_cli(scene.root, "explain", "svc", "--base-ref", "moving")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                _line(result.stdout, "Base ref:"),
                f"Base ref: {resolved} (explicit --base-ref)",
            )
            self.assertEqual(
                _line(result.stdout, "Requested base:"),
                f"Requested base: moving -> {resolved}",
            )

    def test_explain_does_disclose_a_commit_when_it_infers_the_base(self):
        """The premise for the absence above: the disclosure line is capable of
        carrying a commit id, and does when nothing was requested."""
        with _explain_repository() as scene:
            result = run_cli(scene.root, "explain", "svc")
            line = _line(result.stdout, "Base ref:")
            self.assertIsNotNone(COMMIT_OID.search(line), line)
            self.assertIn(
                "(commit that introduced the current lock entry for svc)", line
            )

    def test_moving_the_branch_changes_both_the_answer_and_disclosure(self):
        with _explain_repository() as scene:
            second = scene.git("rev-parse", "second-commit")
            before = run_cli(scene.root, "explain", "svc", "--base-ref", "moving")
            scene.git("branch", "-f", "moving", second)
            after = run_cli(scene.root, "explain", "svc", "--base-ref", "moving")
            self.assertEqual(
                _line(before.stdout, "Requested base:").split(" -> ")[0],
                _line(after.stdout, "Requested base:").split(" -> ")[0],
            )
            self.assertNotEqual(
                _line(before.stdout, "Base ref:"), _line(after.stdout, "Base ref:")
            )
            self.assertIn(second, _line(after.stdout, "Base ref:"))
            self.assertEqual(_line(before.stdout, "Changed files"), "Changed files (2):")
            self.assertEqual(_line(after.stdout, "Changed files"), "Changed files (1):")

    def test_why_records_the_resolved_ref_as_the_diagnostic_base(self):
        with _drifted_repository() as scene:
            resolved = scene.git("rev-parse", "moving")
            result = run_cli(
                scene.root, "why", "svc", "--format", "json", "--base-ref", "moving"
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["changed_files_status"], "ok")
            self.assertEqual(payload["diagnostic_base"], resolved)
            self.assertEqual(payload["diagnostic_base_requested"], "moving")
            self.assertEqual(payload["diagnostic_base_origin"], "explicit --base-ref")

    def test_the_json_view_carries_requested_and_effective_bases(self):
        with _drifted_repository() as scene:
            payload = json.loads(
                run_cli(
                    scene.root, "why", "svc", "--format", "json",
                    "--base-ref", "moving",
                ).stdout
            )
            self.assertEqual(
                sorted(key for key in payload if "base" in key),
                [
                    "diagnostic_base",
                    "diagnostic_base_origin",
                    "diagnostic_base_requested",
                ],
            )
            self.assertEqual(payload["diagnostic_base_requested"], "moving")
            self.assertRegex(payload["diagnostic_base"], r"^[0-9a-f]{40}$")

    def test_an_unknown_ref_is_a_usage_error_in_why(self):
        with _drifted_repository() as scene:
            result = run_cli(
                scene.root, "why", "svc", "--format", "json",
                "--base-ref", "no-such-ref",
            )
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["component"], "svc")
            self.assertIn("analysis failed", payload["error"])
            self.assertIn(
                "Cannot resolve diagnostic base Git ref 'no-such-ref' to one commit",
                result.stderr,
            )

    def test_an_unknown_ref_is_a_usage_error_in_explain(self):
        with _explain_repository() as scene:
            result = run_cli(
                scene.root, "explain", "svc", "--base-ref", "no-such-ref"
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn(
                "Cannot resolve diagnostic base Git ref 'no-such-ref' to one commit",
                result.stderr,
            )

    def test_an_explicit_base_ref_is_disclosed_as_a_commit_object_id(self):
        with _explain_repository() as scene:
            result = run_cli(scene.root, "explain", "svc", "--base-ref", "moving")
            line = _line(result.stdout, "Base ref:")
            self.assertIsNotNone(COMMIT_OID.search(line), line)

    def test_a_nonexistent_base_ref_is_rejected_before_the_diff(self):
        with _drifted_repository() as scene:
            result = run_cli(
                scene.root, "why", "svc", "--format", "json",
                "--base-ref", "no-such-ref",
            )
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
