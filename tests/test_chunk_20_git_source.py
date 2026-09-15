"""Six places where a version identity, a digest, or a HEAD classification can quietly be wrong.

What links these obligations is that every one of them fails silently. A YAML
loader that reads `01` as the integer 1 still produces a version string; a
submodule entry hashed as empty bytes still produces a digest; a detached HEAD
misread as unborn still produces a lockfile; a component whose tag prefix is a
strict prefix of its neighbour's still reports a plausible SemVer. Nothing
raises, nothing is logged, and the wrong answer looks exactly like the right
one. That is why each test below asserts an identity or an exception rather
than merely that a call returned, and why the negative assertions are all
preceded by a premise showing the same machinery reporting the positive case.

Three of the six needed a fixture that does not exist elsewhere. The
4,300-digit boundary cannot be tested by writing the number in Python, because
`int("1" + "0" * 4300)` raises under this interpreter's own default limit
before boundver sees it; the accepting side is built by string arithmetic and
the rejecting side by `10 ** MAX_JSON_INTEGER_DIGITS`, and the independence
claim is checked by lowering `sys.set_int_max_str_digits` to its floor of 640
and proving, in the same block, that plain `int()` and `str()` do raise there
while boundver's chunked parse and render do not. The unsupported-file-type
guard is POSIX-shaped - FIFOs, sockets, devices - and this host is Windows, so
the guard is driven by handing `_working_tree_mode` a fabricated
`os.stat_result` for each type instead of skipping the case entirely; a
directory covers the same branch through a real repository as a cross-check.
And the raw-provider clause of OBL-GIT-SOURCE-029 is quantified over "a raw
path provider", so the surface is read at runtime from `create_registry()` and
each member is classified by the prefix its own entry labels carry, which means
a provider added tomorrow is placed by a rule and an unrecognised prefix fails
the check rather than being skipped. That enumeration runs twice, over a file
recorded `100644` and the same file re-recorded `100755` through `update-index
--chmod`, because `core.filemode` is false on this host and an `os.chmod` would
have compared a constant against a constant.

Two obligations turned out to disagree with the code, and both are pinned
rather than softened. OBL-GIT-SOURCE-024 lists `0o17` among the spellings a
YAML version must reject: PyYAML implements YAML 1.1, whose integer resolver
knows `017`, `0x10` and `0b10` but not `0o`, so `0o17` never reaches
`_bounded_yaml_int` at all and is accepted verbatim as the version string
`'0o17'`. The bounded parser itself rejects that spelling, so the guard is
sound and the obligation is aimed one layer too low; `017`, the spelling that
really is octal under YAML 1.1, is in the rejection table beside it.
OBL-GIT-SOURCE-044 asks for the strict-prefix overlap to be *documented*, and
`docs/reference.md` says nothing about it, so the documentation clause is an
expected failure while the behaviour it describes is pinned exactly: a single
tag `v11.0.0` gives a component with prefix `v` the version `11.0.0` and a
component with prefix `v1` the version `1.0.0`, both valid SemVer, both
reaching the lockfile with different compat digests and no diagnostic anywhere.

Covers OBL-GIT-SOURCE-024, OBL-GIT-SOURCE-027, OBL-GIT-SOURCE-029,
OBL-GIT-SOURCE-040, OBL-GIT-SOURCE-042 and OBL-GIT-SOURCE-044.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest import mock

from boundver import _git as git
from boundver._config_contract import git_tag_prefix_error
from boundver._git import (
    MAX_GIT_FAILURE_DETAIL_CHARS,
    GitSourceSnapshot,
    GitTreeEntry,
    _capture_git_source_snapshot,
    _head_is_provably_unborn,
    _validated_symbolic_head_ref,
    _working_tree_mode,
    _working_tree_name_status,
    git_latest_tag,
)
from boundver._hashing import (
    HASH_DOMAIN_BOUNDARY,
    _ModeAwareBytes,
    _hash_framed_entries,
)
from boundver._lockfile import _SourceAccessor
from boundver._utils import (
    MAX_JSON_INTEGER_DIGITS,
    ConfigError,
    _bounded_int_to_decimal,
    _bounded_json_int,
    _bounded_yaml_int,
)
from boundver.providers import ProviderContext, create_registry
from boundver.versions import extract_version

from tests._scenarios import SOURCE_MODES, Scenario

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-024: the JSON decimal grammar, and nothing else
# ---------------------------------------------------------------------------

#: One numeric spelling and what a version field holding it extracts to, per
#: file format. `None` means the value was refused and no version was recorded.
#: The two columns differ only where the two grammars genuinely differ: JSON
#: has no syntax at all for these spellings, while YAML 1.1 resolves most of
#: them to integers that `_bounded_yaml_int` then refuses. Boundver adds the
#: YAML 1.2 `0o` spelling to that same bounded numeric resolver.
VERSION_SPELLINGS: Dict[str, Tuple[str, Optional[str], Optional[str]]] = {
    "a plus sign": ("+1", None, None),
    "a leading zero": ("01", None, None),
    "YAML 1.1 octal": ("017", None, None),
    "an underscore separator": ("1_000", None, None),
    "a hexadecimal base": ("0x10", None, None),
    "a binary base": ("0b10", None, None),
    "sexagesimal notation": ("1:30", None, None),
    "a boolean": ("true", None, None),
    "a null": ("null", None, None),
    "a mapping": ("{}", None, None),
    "a sequence": ("[]", None, None),
    "positive infinity": (".inf", None, None),
    "a not-a-number": (".nan", None, None),
    "a plain decimal": ("1000", None, None),
    "a finite float": ("1.5", None, None),
    "a negative float": ("-2.5", None, None),
    "a positive float": ("+2.5", None, None),
    "a positive exponent": ("+1e5", None, None),
    # The premise row: the same harness records textual values.
    "a quoted string": ('"01"', "01", "01"),
}

#: JSON-only numeric edges. YAML has no `Infinity`/`NaN` literals to compare.
JSON_NUMBER_EDGES: Dict[str, Tuple[str, Optional[str]]] = {
    "the Infinity constant": ("Infinity", None),
    "the -Infinity constant": ("-Infinity", None),
    "the NaN constant": ("NaN", None),
    "an exponent that overflows to infinity": ("1e400", None),
    "an exponent that stays finite": ("1e5", None),
    "a float spelled with a trailing zero": ("1.0", None),
}

#: Spellings the bounded integer parsers themselves must refuse, whatever the
#: surrounding loader chose to do with them. `0o17` belongs here and passes
#: here; it is the YAML *resolver*, one layer up, that never asks.
NON_JSON_INTEGER_SPELLINGS = (
    "+1",
    "01",
    "017",
    "1_000",
    "0x10",
    "0o17",
    "0b10",
    "1:30",
    "",
)


class _VersionFile:
    """A directory holding one version file, reused across a whole table.

    A `Scenario` would be the usual fixture, but `extract_version` reads a
    file-backed version source straight off disk and never consults Git, so a
    repository per table row would cost thirty `git init` calls to observe
    nothing extra.
    """

    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        (self.root / "svc").mkdir()

    def close(self) -> None:
        self._directory.cleanup()

    def extract(self, suffix: str, text: str) -> Optional[str]:
        name = f"version{suffix}"
        (self.root / "svc" / name).write_text(text, encoding="utf-8")
        return extract_version(
            self.root, "svc", {"file": name, "field": "version"}
        )

    def json(self, spelling: str) -> Optional[str]:
        return self.extract(".json", '{"version": ' + spelling + "}\n")

    def yaml(self, spelling: str) -> Optional[str]:
        return self.extract(".yaml", "version: " + spelling + "\n")


class VersionNumericGrammarTests(unittest.TestCase):
    """OBL-GIT-SOURCE-024: one integer grammar, whichever loader ran."""

    @classmethod
    def setUpClass(cls):
        cls.files = _VersionFile()

    @classmethod
    def tearDownClass(cls):
        cls.files.close()

    def test_a_file_backed_version_reaches_the_lockfile(self):
        """The premise for the whole class: this source really is consulted."""
        with Scenario("version-premise") as scene:
            scene.component(
                "svc",
                path="svc",
                provider="path-hash",
                boundary=["*.txt"],
                version_source={"file": "package.json", "field": "version"},
            )
            scene.file("svc/a.txt", "a\n")
            scene.json_file("svc/package.json", {"version": "1.2.3"})
            scene.commit()
            lockfile = scene.generate()
            self.assertEqual(lockfile["components"]["svc"]["version"], "1.2.3")

    def test_only_textual_versions_survive_either_loader(self):
        for label, (spelling, expected_json, expected_yaml) in (
            VERSION_SPELLINGS.items()
        ):
            with self.subTest(value=label, format="json"):
                self.assertEqual(self.files.json(spelling), expected_json)
            with self.subTest(value=label, format="yaml"):
                self.assertEqual(self.files.yaml(spelling), expected_yaml)

    def test_json_number_literals_outside_the_finite_grammar_are_refused(self):
        for label, (spelling, expected) in JSON_NUMBER_EDGES.items():
            with self.subTest(value=label):
                self.assertEqual(self.files.json(spelling), expected)

    def test_the_bounded_integer_parsers_refuse_every_non_json_spelling(self):
        """Including `0o17`, which the YAML resolver never hands them."""
        self.assertEqual(_bounded_json_int("1000"), 1000)
        self.assertEqual(_bounded_yaml_int("-5"), -5)
        for spelling in NON_JSON_INTEGER_SPELLINGS:
            with self.subTest(spelling=spelling, parser="json"):
                with self.assertRaises(ValueError):
                    _bounded_json_int(spelling)
            with self.subTest(spelling=spelling, parser="yaml"):
                with self.assertRaises(ValueError):
                    _bounded_yaml_int(spelling)

    def test_numeric_versions_are_refused_at_every_bounded_digit_length(self):
        self.assertEqual(MAX_JSON_INTEGER_DIGITS, 4300)
        for digits in (1, MAX_JSON_INTEGER_DIGITS - 1, MAX_JSON_INTEGER_DIGITS):
            value = "1" + "0" * (digits - 1)
            with self.subTest(digits=digits, format="json"):
                self.assertIsNone(self.files.json(value))
            with self.subTest(digits=digits, format="yaml"):
                self.assertIsNone(self.files.yaml(value))
        for digits in (MAX_JSON_INTEGER_DIGITS + 1, MAX_JSON_INTEGER_DIGITS + 700):
            value = "1" + "0" * (digits - 1)
            with self.subTest(digits=digits, format="json"):
                self.assertIsNone(self.files.json(value))
            with self.subTest(digits=digits, format="yaml"):
                self.assertIsNone(self.files.yaml(value))

    def test_the_renderer_stops_at_the_same_digit_limit(self):
        """`10 ** MAX` is a 4,301-digit integer built without a str conversion."""
        largest = 10 ** (MAX_JSON_INTEGER_DIGITS - 1)
        self.assertEqual(len(_bounded_int_to_decimal(largest)), 4300)
        self.assertEqual(len(_bounded_int_to_decimal(-largest)), 4301)
        self.assertEqual(_bounded_int_to_decimal(0), "0")
        for value in (10 ** MAX_JSON_INTEGER_DIGITS, -(10 ** MAX_JSON_INTEGER_DIGITS)):
            with self.subTest(sign="-" if value < 0 else "+"):
                with self.assertRaises(ValueError) as raised:
                    _bounded_int_to_decimal(value)
                self.assertEqual(
                    str(raised.exception),
                    "JSON integer exceeds the 4300-decimal-digit limit",
                )

    def test_a_4300_digit_version_is_unchanged_by_the_process_wide_digit_limit(self):
        """And the same block proves the lowered limit really was in force.

        Results are collected while the limit is low and asserted after it is
        restored: a failed assertion inside the block would try to render a
        4,300-digit integer in its own message and raise there instead.
        """
        value = "1" + "0" * (MAX_JSON_INTEGER_DIGITS - 1)
        floor = sys.int_info.str_digits_check_threshold
        previous = sys.get_int_max_str_digits()
        plain_int_raised = False
        plain_str_raised = False
        try:
            sys.set_int_max_str_digits(floor)
            try:
                int(value)
            except ValueError:
                plain_int_raised = True
            parsed_json = self.files.json(value)
            parsed_yaml = self.files.yaml(value)
            try:
                str(_bounded_json_int(value))
            except ValueError:
                plain_str_raised = True
            rendered = _bounded_int_to_decimal(_bounded_json_int(value))
        finally:
            sys.set_int_max_str_digits(previous)
        self.assertEqual(sys.get_int_max_str_digits(), previous)
        self.assertTrue(
            plain_int_raised,
            "premise failed: int() accepted 4300 digits under a 640-digit limit",
        )
        self.assertTrue(
            plain_str_raised,
            "premise failed: str() rendered 4300 digits under a 640-digit limit",
        )
        self.assertIsNone(parsed_json)
        self.assertIsNone(parsed_yaml)
        self.assertEqual(rendered, value)

    def test_finite_and_non_finite_float_versions_are_refused(self):
        for spelling in ("1.5", "-2.5", ".inf", "-.inf", ".nan"):
            with self.subTest(spelling=spelling):
                self.assertIsNone(self.files.yaml(spelling))
        for spelling in ("1.5", "-2.5", "1e5"):
            with self.subTest(spelling=spelling):
                self.assertIsNone(self.files.json(spelling))

    def test_an_octal_prefixed_yaml_version_is_refused(self):
        self.assertIsNone(self.files.yaml("0o17"))

    def test_a_quoted_yaml_0o17_version_remains_a_literal_string(self):
        self.assertEqual(self.files.yaml('"0o17"'), "0o17")
        self.assertIsNone(self.files.yaml("0o17"))
        self.assertIsNone(self.files.json("0o17"))
        with self.assertRaises(ValueError):
            _bounded_yaml_int("0o17")


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-029: semantic bytes and file bytes are never the same entry
# ---------------------------------------------------------------------------

#: The label every provider in the enumeration selects. Held constant because
#: the digest covers the label as well as the content.
PROVIDER_SELECTOR = "contract.json"

#: Valid JSON and a valid OpenAPI 3.1 document at once, so that every one of
#: the registered providers resolves it rather than erroring out and dropping
#: out of the classification unnoticed.
PROVIDER_CONTRACT = (
    json.dumps(
        {
            "openapi": "3.1.0",
            "info": {"title": "t", "version": "1.0.0"},
            "paths": {"/ping": {"get": {"responses": {"200": {"description": "ok"}}}}},
        },
        indent=2,
    )
    + "\n"
)

#: What each derived provider class must hand back for a real file. Keyed on
#: the prefix the provider's own entry labels carry, never on a provider name,
#: so a member added tomorrow is placed by what it returns. `file` providers
#: read bytes off a path and must carry that path's Git metadata with them;
#: `canonical` providers build a new value and are supposed to lose it.
ENTRY_METADATA_BY_LABEL_CLASS = {
    "file": True,
    "canonical": False,
}


def _provider_slug(key: str) -> str:
    return key.replace("-", "_")


class SemanticAndFileEntryFramingTests(unittest.TestCase):
    """OBL-GIT-SOURCE-029: the silent `getattr` fallback and who can reach it."""

    def test_plain_bytes_and_mode_aware_bytes_never_share_a_digest(self):
        plain = _hash_framed_entries([("c", b"v")], domain=HASH_DOMAIN_BOUNDARY)
        mode_aware = _hash_framed_entries(
            [("c", _ModeAwareBytes(b"v", "100644"))], domain=HASH_DOMAIN_BOUNDARY
        )
        self.assertNotEqual(plain, mode_aware)
        self.assertEqual(
            plain,
            "ceaf6f4e3a82ffed088bf2723a7bae4365397959a38851f3418e2cb3118adbf4",
        )
        self.assertEqual(
            mode_aware,
            "9662dcb6dd33e0f5e7256a9a3d34773e614cc7f4fceca1fe75e342da311d16af",
        )

    def test_the_fallback_discriminator_is_exactly_semantic_and_value(self):
        """Pins the two constants the fallback substitutes, by construction."""
        plain = _hash_framed_entries([("c", b"v")], domain=HASH_DOMAIN_BOUNDARY)
        self.assertEqual(
            plain,
            _hash_framed_entries(
                [("c", "semantic", "value", b"v")], domain=HASH_DOMAIN_BOUNDARY
            ),
        )
        self.assertNotEqual(
            plain,
            _hash_framed_entries(
                [("c", "100644", "blob", b"v")], domain=HASH_DOMAIN_BOUNDARY
            ),
        )
        self.assertEqual(
            _hash_framed_entries(
                [("c", _ModeAwareBytes(b"v", "100644"))], domain=HASH_DOMAIN_BOUNDARY
            ),
            _hash_framed_entries(
                [("c", "100644", "blob", b"v")], domain=HASH_DOMAIN_BOUNDARY
            ),
        )

    def test_the_fallback_is_silent_when_a_file_entry_reaches_it(self):
        """The premise for the enumeration below: nothing would raise.

        A plain-bytes entry carrying a `file:` label hashes as semantic/value
        without complaint, so the only thing standing between a regressed raw
        provider and an invisible loss of mode tracking is the check that
        follows.
        """
        self.assertEqual(
            _hash_framed_entries(
                [("file:contract.json", b"v")], domain=HASH_DOMAIN_BOUNDARY
            ),
            _hash_framed_entries(
                [("file:contract.json", "semantic", "value", b"v")],
                domain=HASH_DOMAIN_BOUNDARY,
            ),
        )

    def _check_every_provider(self, recorded_mode: str) -> Tuple[int, int]:
        """Resolve the whole registry against a file recorded with *mode*.

        The mode is set through `update-index --chmod`, not `os.chmod`: this
        host leaves `core.filemode` at false, so a chmod changes nothing Git
        records and the check would compare a constant against a constant.
        """
        registry = create_registry()
        keys = sorted(registry)
        self.assertGreater(len(keys), 1, "the registry surface is empty")
        file_entries_seen = 0
        canonical_entries_seen = 0
        with Scenario("mode-aware-fallback") as scene:
            for key in keys:
                scene.component(
                    key,
                    path=f"c/{_provider_slug(key)}",
                    provider=key,
                    boundary=[PROVIDER_SELECTOR],
                )
                scene.file(
                    f"c/{_provider_slug(key)}/{PROVIDER_SELECTOR}", PROVIDER_CONTRACT
                )
            scene.commit()
            if recorded_mode == "100755":
                for key in keys:
                    os.chmod(
                        scene.root / f"c/{_provider_slug(key)}/{PROVIDER_SELECTOR}",
                        0o700,
                    )
                    scene.git(
                        "update-index",
                        "--chmod=+x",
                        f"c/{_provider_slug(key)}/{PROVIDER_SELECTOR}",
                    )
                scene.commit_index("executable")
            for key in keys:
                self.assertTrue(
                    scene.git(
                        "ls-tree", "HEAD", f"c/{_provider_slug(key)}/{PROVIDER_SELECTOR}"
                    ).startswith(recorded_mode),
                    f"fixture failed to record mode {recorded_mode} for {key}",
                )
            for source in SOURCE_MODES:
                with _SourceAccessor(scene.root, source) as accessor:
                    for key in keys:
                        context = ProviderContext(
                            repo_root=scene.root,
                            component_path=f"c/{_provider_slug(key)}",
                            boundary_cfg=scene.config["components"][key]["boundary"],
                            source=source,
                            read_file=accessor.read_file,
                            read_file_limited=accessor.read_file_limited,
                            list_files=accessor.list_files,
                        )
                        resolved = registry[key].resolve(context)
                        for label, content in resolved.entries:
                            label_class = label.split(":", 1)[0]
                            with self.subTest(
                                provider=key, source=source, label=label
                            ):
                                self.assertIn(
                                    label_class,
                                    ENTRY_METADATA_BY_LABEL_CLASS,
                                    "unclassified entry label prefix",
                                )
                                carries = ENTRY_METADATA_BY_LABEL_CLASS[label_class]
                                if carries:
                                    file_entries_seen += 1
                                    self.assertIsInstance(content, _ModeAwareBytes)
                                    self.assertEqual(content.git_mode, recorded_mode)
                                    self.assertEqual(content.git_object_type, "blob")
                                else:
                                    canonical_entries_seen += 1
                                    self.assertNotIsInstance(content, _ModeAwareBytes)
                                    self.assertFalse(hasattr(content, "git_mode"))
        return file_entries_seen, canonical_entries_seen

    def test_no_registered_path_provider_ever_returns_plain_bytes(self):
        """Every registry member, every source mode, classified by its labels."""
        file_entries, canonical_entries = self._check_every_provider("100644")
        self.assertEqual(file_entries, 30)
        self.assertEqual(canonical_entries, 6)

    def test_the_propagated_mode_follows_the_recorded_one_rather_than_a_constant(self):
        """The same enumeration over an executable file, so 100644 is not fixed."""
        file_entries, canonical_entries = self._check_every_provider("100755")
        self.assertEqual(file_entries, 30)
        self.assertEqual(canonical_entries, 6)


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-027: no unreadable entry ever becomes empty bytes
# ---------------------------------------------------------------------------

#: Every `st_mode` file type and the mode/type pair `_working_tree_mode` must
#: answer with, or `None` where it must refuse. Fabricated rather than created,
#: because this host cannot make a FIFO, a socket or a device node and a
#: skipped case is no coverage at all.
WORKING_TREE_FILE_TYPES = {
    "a regular file": (stat.S_IFREG | 0o644, ("100644", "blob")),
    "an executable regular file": (stat.S_IFREG | 0o755, ("100755", "blob")),
    "a symlink": (stat.S_IFLNK | 0o777, ("120000", "blob")),
    "a FIFO": (stat.S_IFIFO | 0o644, None),
    "a socket": (stat.S_IFSOCK | 0o644, None),
    "a block device": (stat.S_IFBLK | 0o644, None),
    "a character device": (stat.S_IFCHR | 0o644, None),
    "a directory": (stat.S_IFDIR | 0o755, None),
}


def _fabricated_stat(mode_bits: int) -> os.stat_result:
    return os.stat_result((mode_bits, 0, 0, 1, 0, 0, 0, 0, 0, 0))


def _empty_file_scenario(with_empty_file: bool) -> Scenario:
    scene = Scenario("zero-byte")
    scene.component("svc", path="svc", provider="path-hash", boundary=["*.txt"])
    scene.file("svc/present.txt", "content\n")
    if with_empty_file:
        scene.file("svc/blank.txt", "")
    scene.commit()
    return scene


class UnreadableEntryTests(unittest.TestCase):
    """OBL-GIT-SOURCE-027: refuse, or hash the real thing; never substitute."""

    def test_a_tracked_empty_file_is_hashed_and_is_not_the_same_as_its_absence(self):
        digests: Dict[bool, Dict[Tuple[str, str], str]] = {}
        for present in (True, False):
            with _empty_file_scenario(present) as scene:
                if present:
                    self.assertEqual(scene.blob("svc/blank.txt"), b"")
                digests[present] = {
                    (source, facet): scene.fingerprints("svc", source=source)[facet]
                    for source in SOURCE_MODES
                    for facet in ("exact", "boundary")
                }
        for key, digest in digests[True].items():
            with self.subTest(source=key[0], facet=key[1]):
                self.assertIsInstance(digest, str)
                self.assertNotEqual(
                    digest,
                    digests[False][key],
                    "a tracked zero-byte file digests the same as no file at all",
                )

    def test_a_gitlink_refuses_the_component_digest_rather_than_hashing_nothing(self):
        with Scenario("gitlink") as scene:
            scene.component("svc", path="svc", provider="path-hash", boundary=["*.txt"])
            scene.file("svc/a.txt", "a\n")
            scene.commit()
            clean = scene.fingerprints("svc", source="head")["exact"]
            self.assertIsInstance(clean, str)
            scene.gitlink("svc/sub")
            scene.commit_index("with gitlink")
            for source in ("head", "index"):
                with self.subTest(source=source):
                    with self.assertRaises(ConfigError) as raised:
                        scene.fingerprints("svc", source=source)
                    self.assertIn(
                        "Cannot hash non-blob Git entry at svc/sub: "
                        "commit mode 160000",
                        str(raised.exception),
                    )

    def test_a_tracked_path_replaced_by_a_directory_refuses_the_worktree_digest(self):
        with Scenario("directory-type") as scene:
            scene.component("svc", path="svc", provider="path-hash", boundary=["*.txt"])
            scene.file("svc/a.txt", "a\n")
            scene.commit()
            (scene.root / "svc" / "a.txt").unlink()
            (scene.root / "svc" / "a.txt").mkdir()
            with self.assertRaises(ConfigError) as raised:
                scene.fingerprints("svc", source="working-tree")
            self.assertIn(
                "Unsupported working-tree file type at svc/a.txt",
                str(raised.exception),
            )
            # The premise, and the point: the committed trees are unaffected,
            # so the refusal is about the working tree rather than the repo.
            self.assertEqual(
                scene.fingerprints("svc", source="head")["exact"],
                scene.fingerprints("svc", source="index")["exact"],
            )

    def test_every_non_file_filesystem_type_is_refused_a_mode(self):
        with Scenario("file-types") as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/a.txt", "a\n")
            scene.commit()
            for label, (mode_bits, expected) in WORKING_TREE_FILE_TYPES.items():
                with self.subTest(kind=label):
                    if expected is None:
                        with self.assertRaises(ValueError) as raised:
                            _working_tree_mode(
                                scene.root,
                                "svc/a.txt",
                                path_stat=_fabricated_stat(mode_bits),
                            )
                        self.assertEqual(
                            str(raised.exception),
                            "Unsupported working-tree file type at svc/a.txt",
                        )
                    else:
                        self.assertEqual(
                            _working_tree_mode(
                                scene.root,
                                "svc/a.txt",
                                path_stat=_fabricated_stat(mode_bits),
                            ),
                            expected,
                        )

    def test_a_path_that_disappears_before_the_read_is_refused_by_name(self):
        """The neighbouring refusal, so the guard is not one branch wide."""
        with Scenario("disappeared") as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/a.txt", "a\n")
            scene.commit()
            with self.assertRaises(ValueError) as raised:
                _working_tree_mode(scene.root, "svc/gone.txt")
            self.assertEqual(
                str(raised.exception),
                "File disappeared while hashing: svc/gone.txt",
            )


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-040: unborn, detached, and broken are three different answers
# ---------------------------------------------------------------------------

#: What `_validated_symbolic_head_ref` must do with one `symbolic-ref` answer.
#: `None` means it must raise. A trailing newline is what Git really writes,
#: so an "embedded" newline has to be followed by more text to survive the
#: function's own `rstrip`.
SYMBOLIC_HEAD_ANSWERS = {
    "a well formed branch ref": ("refs/heads/main\n", "refs/heads/main"),
    "a ref exactly at the length cap": (
        "refs/heads/" + "a" * (MAX_GIT_FAILURE_DETAIL_CHARS - 11) + "\n",
        "refs/heads/" + "a" * (MAX_GIT_FAILURE_DETAIL_CHARS - 11),
    ),
    "the tag namespace": ("refs/tags/x\n", None),
    "a bare refs/heads/": ("refs/heads/\n", None),
    "an embedded newline": ("refs/heads/a\nb\n", None),
    "an embedded control character": ("refs/heads/a\x01b\n", None),
    "an embedded delete character": ("refs/heads/a\x7fb\n", None),
    "leading whitespace": (" refs/heads/main\n", None),
    "trailing whitespace": ("refs/heads/main \n", None),
    "one character past the length cap": (
        "refs/heads/" + "a" * (MAX_GIT_FAILURE_DETAIL_CHARS - 10) + "\n",
        None,
    ),
    "nothing at all": ("", None),
}


def _git_failure(code: int, stdout: str = "", stderr: str = ""):
    return subprocess.CalledProcessError(code, ["git"], output=stdout, stderr=stderr)


class _ScriptedGit:
    """A `_git_run` stand-in that answers per Git subcommand."""

    def __init__(self, answers: Dict[str, object]) -> None:
        self.answers = answers
        self.calls: List[Tuple[str, ...]] = []

    def __call__(self, repo_root, args, **kwargs):
        self.calls.append(tuple(args))
        answer = self.answers[args[0]]
        if isinstance(answer, BaseException):
            raise answer
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=0, stdout=answer, stderr=""
        )


#: One scripted repository state and the answer `_head_is_provably_unborn`
#: owes it. `None` means it must raise `ValueError` rather than return either
#: boolean, which is the whole point of the obligation.
UNBORN_CLASSIFICATIONS = {
    "symbolic-ref exits 1 with both streams empty": (
        {"symbolic-ref": _git_failure(1)},
        False,
    ),
    "symbolic-ref exits 1 having written to stderr": (
        {"symbolic-ref": _git_failure(1, "", "fatal: not a git repository")},
        None,
    ),
    "symbolic-ref exits 1 having written to stdout": (
        {"symbolic-ref": _git_failure(1, "refs/heads/x\n", "")},
        None,
    ),
    "symbolic-ref exits 128": (
        {"symbolic-ref": _git_failure(128, "", "")},
        None,
    ),
    "symbolic-ref cannot be launched": (
        {"symbolic-ref": OSError("no git on PATH")},
        None,
    ),
    "symbolic-ref returns a tag ref": (
        {"symbolic-ref": "refs/tags/x\n"},
        None,
    ),
    "check-ref-format rejects the branch name": (
        {
            "symbolic-ref": "refs/heads/main\n",
            "check-ref-format": _git_failure(1, "", "fatal: bad name"),
        },
        None,
    ),
    "show-ref exits 1 with both streams empty": (
        {
            "symbolic-ref": "refs/heads/main\n",
            "check-ref-format": "",
            "show-ref": _git_failure(1),
        },
        True,
    ),
    "show-ref exits 1 having written to stderr": (
        {
            "symbolic-ref": "refs/heads/main\n",
            "check-ref-format": "",
            "show-ref": _git_failure(1, "", "fatal: broken ref store"),
        },
        None,
    ),
    "show-ref exits 128": (
        {
            "symbolic-ref": "refs/heads/main\n",
            "check-ref-format": "",
            "show-ref": _git_failure(128, "", ""),
        },
        None,
    ),
    "show-ref finds the branch": (
        {
            "symbolic-ref": "refs/heads/main\n",
            "check-ref-format": "",
            "show-ref": "",
        },
        False,
    ),
}


class UnbornHeadClassificationTests(unittest.TestCase):
    """OBL-GIT-SOURCE-040: never call a detached or damaged HEAD unborn."""

    def test_a_real_repository_moves_from_unborn_to_born_across_its_first_commit(self):
        """The premise: both boolean answers are reachable without a mock."""
        with Scenario("unborn") as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/a.txt", "a\n")
            self.assertTrue(_head_is_provably_unborn(scene.root))
            scene.commit()
            self.assertFalse(_head_is_provably_unborn(scene.root))

    def test_a_real_detached_head_is_not_unborn(self):
        with Scenario("detached") as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/a.txt", "a\n")
            scene.commit()
            scene.git("checkout", "--detach", scene.head())
            probe = subprocess.run(
                ["git", "symbolic-ref", "--quiet", "HEAD"],
                cwd=scene.root,
                capture_output=True,
                text=True,
            )
            # The precondition the classifier relies on, observed rather than
            # assumed: exit 1 and nothing on either stream.
            self.assertEqual(probe.returncode, 1)
            self.assertEqual(probe.stdout, "")
            self.assertEqual(probe.stderr, "")
            self.assertFalse(_head_is_provably_unborn(scene.root))

    def test_only_a_well_formed_branch_ref_is_passed_onward(self):
        for label, (answer, expected) in SYMBOLIC_HEAD_ANSWERS.items():
            with self.subTest(answer=label):
                if expected is None:
                    with self.assertRaises(ValueError) as raised:
                        _validated_symbolic_head_ref(answer)
                    self.assertIn(
                        "git symbolic-ref returned a malformed HEAD ref",
                        str(raised.exception),
                    )
                else:
                    self.assertEqual(_validated_symbolic_head_ref(answer), expected)

    def test_a_noisy_or_unexpected_git_failure_raises_instead_of_answering(self):
        for label, (answers, expected) in UNBORN_CLASSIFICATIONS.items():
            with self.subTest(state=label):
                scripted = _ScriptedGit(answers)
                with mock.patch.object(git, "_git_run", scripted):
                    if expected is None:
                        with self.assertRaises(ValueError):
                            _head_is_provably_unborn(Path("."))
                    else:
                        self.assertIs(
                            _head_is_provably_unborn(Path(".")), expected
                        )
                self.assertEqual(scripted.calls[0][0], "symbolic-ref")

    def test_each_refusal_names_the_command_that_produced_it(self):
        """So a partial fix cannot swap one diagnostic for the other."""
        cases = {
            "symbolic-ref": (
                {"symbolic-ref": _git_failure(128, "", "")},
                "git symbolic-ref failed while classifying unresolved HEAD",
            ),
            "check-ref-format": (
                {
                    "symbolic-ref": "refs/heads/main\n",
                    "check-ref-format": _git_failure(1, "", "fatal: bad name"),
                },
                "git symbolic-ref returned an invalid HEAD branch ref "
                "'refs/heads/main'",
            ),
            "show-ref": (
                {
                    "symbolic-ref": "refs/heads/main\n",
                    "check-ref-format": "",
                    "show-ref": _git_failure(1, "", "fatal: broken ref store"),
                },
                "git show-ref failed while checking symbolic HEAD "
                "'refs/heads/main'",
            ),
        }
        for command, (answers, expected) in cases.items():
            with self.subTest(command=command):
                with mock.patch.object(git, "_git_run", _ScriptedGit(answers)):
                    with self.assertRaises(ValueError) as raised:
                        _head_is_provably_unborn(Path("."))
                self.assertIn(expected, str(raised.exception))


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-042: a sparse absence is not a deletion
# ---------------------------------------------------------------------------


def _sparse_scenario() -> Scenario:
    """Two components, then a cone sparse checkout that drops the second."""
    scene = Scenario("sparse")
    scene.component("a", path="a", provider="path-hash", boundary=["*.txt"])
    scene.component("b", path="b", provider="path-hash", boundary=["*.txt"])
    scene.file("a/x.txt", "visible\n")
    scene.file("b/y.txt", "sparse\n")
    scene.commit()
    scene.git("sparse-checkout", "init", "--cone")
    scene.git("sparse-checkout", "set", "a")
    return scene


class SparseCheckoutStatusTests(unittest.TestCase):
    """OBL-GIT-SOURCE-042: compare an absent sparse path by its index blob."""

    def _status(self, scene: Scenario) -> List[Tuple[str, str]]:
        snapshot = _capture_git_source_snapshot(scene.root, "index")
        return _working_tree_name_status(
            scene.root, "HEAD", tracking_snapshot=snapshot
        )

    def test_an_ordinary_missing_tracked_path_is_reported_as_a_deletion(self):
        """The premise: "D" is a letter this comparison really can produce."""
        with Scenario("deleted") as scene:
            scene.component("a", path="a", provider="path-hash", boundary=["*.txt"])
            scene.file("a/x.txt", "visible\n")
            scene.file("a/z.txt", "also\n")
            scene.commit()
            (scene.root / "a" / "z.txt").unlink()
            snapshot = _capture_git_source_snapshot(scene.root, "index")
            self.assertEqual(sorted(snapshot.skip_worktree_paths), [])
            self.assertEqual(self._status(scene), [("D", "a/z.txt")])

    def test_an_absent_sparse_path_matching_the_base_tree_reports_nothing(self):
        with _sparse_scenario() as scene:
            self.assertFalse((scene.root / "b" / "y.txt").exists())
            snapshot = _capture_git_source_snapshot(scene.root, "index")
            self.assertEqual(sorted(snapshot.skip_worktree_paths), ["b/y.txt"])
            self.assertEqual(self._status(scene), [])

    def test_an_absent_sparse_path_whose_index_blob_moved_is_M_and_never_D(self):
        with _sparse_scenario() as scene:
            (scene.root / "replacement.txt").write_text("changed\n", encoding="utf-8")
            oid = scene.git("hash-object", "-w", "replacement.txt")
            scene.git("update-index", "--cacheinfo", f"100644,{oid},b/y.txt")
            scene.git("update-index", "--skip-worktree", "b/y.txt")
            snapshot = _capture_git_source_snapshot(scene.root, "index")
            self.assertEqual(sorted(snapshot.skip_worktree_paths), ["b/y.txt"])
            status = self._status(scene)
            self.assertEqual(status, [("M", "b/y.txt")])
            self.assertNotIn("D", [letter for letter, _ in status])

    def test_a_sparse_path_written_back_to_disk_loses_its_S_tag_to_git_itself(self):
        """Pins where the "present despite S" case actually goes: nowhere.

        Git clears the skip-worktree bit as soon as the path is materialized,
        so `ls-files -t` tags it `H` and the comparison takes the ordinary
        working-tree route. boundver never sees an `S` path that is present.
        """
        with _sparse_scenario() as scene:
            self.assertIn("S b/y.txt", scene.git("ls-files", "--cached", "-t"))
            (scene.root / "b").mkdir(exist_ok=True)
            (scene.root / "b" / "y.txt").write_text("rewritten\n", encoding="utf-8")
            self.assertIn("H b/y.txt", scene.git("ls-files", "--cached", "-t"))
            snapshot = _capture_git_source_snapshot(scene.root, "index")
            self.assertEqual(sorted(snapshot.skip_worktree_paths), [])
            self.assertEqual(self._status(scene), [("M", "b/y.txt")])

    def test_a_snapshot_whose_sparse_paths_are_not_materialized_is_refused(self):
        entry = GitTreeEntry(
            path="a.txt", mode="100644", object_type="blob", oid="0" * 40
        )
        accepted = GitSourceSnapshot(
            source="index",
            tree_oid="1" * 40,
            entries={"a.txt": entry},
            skip_worktree_paths=frozenset({"a.txt"}),
        )
        self.assertEqual(sorted(accepted.skip_worktree_paths), ["a.txt"])
        cases = {
            "a sparse path with no entry at all": {
                "skip_worktree_paths": frozenset({"b.txt"}),
            },
            "a sparse path tracked but not materialized": {
                "tracked_paths": frozenset({"a.txt", "b.txt"}),
                "skip_worktree_paths": frozenset({"b.txt"}),
            },
        }
        for label, extra in cases.items():
            with self.subTest(snapshot=label):
                with self.assertRaises(ValueError) as raised:
                    GitSourceSnapshot(
                        source="index",
                        tree_oid="1" * 40,
                        entries={"a.txt": entry},
                        **extra,
                    )
                self.assertEqual(
                    str(raised.exception),
                    "Skip-worktree paths must have materialized captured "
                    "index entries",
                )

    def test_an_intent_to_add_path_marked_sparse_stops_the_capture(self):
        """The same subset rule, one layer up, on a real repository.

        An intent-to-add path is tracked but has no blob in the written tree,
        so marking it skip-worktree produces exactly the state the dataclass
        guard forbids - and the capture refuses it before a snapshot exists.
        """
        with Scenario("intent-to-add") as scene:
            scene.component("svc", path="svc", provider="path-hash", boundary=["*.txt"])
            scene.file("svc/a.txt", "a\n")
            scene.commit()
            scene.file("svc/new.txt", "new\n")
            scene.git("add", "-N", "--", "svc/new.txt")
            # The premise: intent-to-add alone captures fine, tracked but
            # unmaterialized, so the refusal below is about the sparse bit.
            plain = _capture_git_source_snapshot(scene.root, "index")
            self.assertIn("svc/new.txt", plain.tracked_paths)
            self.assertNotIn("svc/new.txt", plain.entries)
            scene.git("update-index", "--skip-worktree", "svc/new.txt")
            self.assertIn("S svc/new.txt", scene.git("ls-files", "--cached", "-t"))
            with self.assertRaises(ValueError) as raised:
                _capture_git_source_snapshot(scene.root, "index")
            self.assertEqual(
                str(raised.exception),
                "Captured skip-worktree membership omitted materialized tree paths",
            )


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-044: the tag prefix is a literal, and literals can overlap
# ---------------------------------------------------------------------------

#: One `git_tag_prefix` and whether the config contract accepts it. Every
#: rejected row is a form the obligation names; the accepted rows are the
#: premise, including the three deliberate edge allowances.
TAG_PREFIXES = {
    "a star": ("v*", False),
    "a question mark": ("v?", False),
    "an open bracket": ("v[", False),
    "a backslash": ("v\\", False),
    "a trailing space": ("v ", False),
    "a tab": ("v\t", False),
    "a control character": ("v\x01", False),
    "a tilde": ("v~", False),
    "a caret": ("v^", False),
    "a colon": ("v:", False),
    "a double dot": ("v..x", False),
    "an at-brace": ("v@{x", False),
    "a completed .lock component": ("team.lock/v", False),
    "a bare letter": ("v", True),
    "a trailing slash": ("releases/", True),
    "a trailing dot": ("candidate.", True),
    "a trailing .lock": ("candidate.lock", True),
}

#: Phrases that would show the strict-prefix overlap is documented. None of
#: them appears in the `git_tag_prefix` section today.
OVERLAP_DOCUMENTATION_PHRASES = (
    "strict prefix",
    "shorter prefix",
    "longer prefix",
    "prefix of another",
    "another component's tags",
    "overlap",
)


class GitTagPrefixLiteralTests(unittest.TestCase):
    """OBL-GIT-SOURCE-044: a literal prefix, and what literal really costs."""

    def test_the_config_contract_accepts_only_a_literal_tag_prefix(self):
        for label, (prefix, valid) in TAG_PREFIXES.items():
            with self.subTest(prefix=label):
                error = git_tag_prefix_error(prefix)
                if valid:
                    self.assertIsNone(error)
                else:
                    self.assertIsNotNone(error)
                    self.assertIn("literal prefix", error)

    def test_a_reachable_tag_equal_to_the_prefix_yields_no_version(self):
        with Scenario("prefix-only-tag") as scene:
            scene.component(
                "only",
                path="only",
                provider="leaf",
                version_source={"git_tag_prefix": "release-"},
            )
            scene.file("only/a.txt", "a\n")
            scene.commit()
            scene.git("tag", "release-")
            self.assertIsNone(git_latest_tag(scene.root, "release-"))
            # And it keeps yielding none once a versioned tag joins it on the
            # same commit: `describe --abbrev=0` picks `release-` out of the
            # two, so the empty slice masks a real version rather than losing
            # a tie to it.
            scene.git("tag", "release-2.0.0")
            self.assertEqual(
                scene.git("describe", "--tags", "--match", "release-*", "--abbrev=0"),
                "release-",
            )
            self.assertIsNone(git_latest_tag(scene.root, "release-"))
            # The premise: the same repository does resolve a tag that carries
            # a version after the prefix, so `None` above is the empty slice
            # and not an unreachable tag.
            scene.git("tag", "-d", "release-")
            self.assertEqual(git_latest_tag(scene.root, "release-"), "2.0.0")

    def test_a_shorter_prefix_matches_a_longer_ones_tag_and_slices_at_its_length(self):
        with Scenario("prefix-overlap") as scene:
            scene.component(
                "short",
                path="short",
                provider="leaf",
                version_source={"git_tag_prefix": "v"},
            )
            scene.component(
                "long",
                path="long",
                provider="leaf",
                version_source={"git_tag_prefix": "va"},
            )
            scene.file("short/a.txt", "a\n")
            scene.file("long/b.txt", "b\n")
            scene.commit()
            scene.git("tag", "va1.0.0")
            self.assertEqual(git_latest_tag(scene.root, "va"), "1.0.0")
            self.assertEqual(git_latest_tag(scene.root, "v"), "a1.0.0")
            self.assertIsNone(git_latest_tag(scene.root, "vb"))
            # Here the overlap is caught downstream, by the SemVer check, and
            # the message names the sliced remainder rather than the overlap.
            with self.assertRaises(ConfigError) as raised:
                scene.generate()
            self.assertIn(
                "short: Configured version is not valid SemVer: 'a1.0.0'",
                str(raised.exception),
            )

    def test_one_tag_can_give_two_components_two_different_valid_versions(self):
        """The case the SemVer check does not catch, pinned end to end."""
        with Scenario("prefix-collision") as scene:
            scene.component(
                "platform",
                path="platform",
                provider="leaf",
                version_source={"git_tag_prefix": "v"},
            )
            scene.component(
                "service",
                path="service",
                provider="leaf",
                version_source={"git_tag_prefix": "v1"},
            )
            scene.file("platform/a.txt", "a\n")
            scene.file("service/b.txt", "b\n")
            scene.commit()
            scene.git("tag", "v11.0.0")
            self.assertEqual(scene.git("tag", "--list"), "v11.0.0")
            lockfile = scene.generate()
            self.assertEqual(lockfile["components"]["platform"]["version"], "11.0.0")
            self.assertEqual(lockfile["components"]["service"]["version"], "1.0.0")
            self.assertNotEqual(
                lockfile["components"]["platform"]["fingerprints"]["compat"],
                lockfile["components"]["service"]["fingerprints"]["compat"],
            )

    def test_the_strict_prefix_overlap_is_documented(self):
        """The reference makes literal strict-prefix overlap explicit."""
        reference = (REPOSITORY_ROOT / "docs" / "reference.md").read_text(
            encoding="utf-8"
        )
        anchor = "`git_tag_prefix` is a literal prefix, not a glob."
        self.assertIn(anchor, reference)
        section = reference[reference.index(anchor):][:2000].lower()
        self.assertTrue(
            any(phrase in section for phrase in OVERLAP_DOCUMENTATION_PHRASES),
            "the git_tag_prefix section does not mention prefix overlap",
        )


if __name__ == "__main__":
    unittest.main()
