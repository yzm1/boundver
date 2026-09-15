"""One name, two namespaces: what the writer creates and what the reader finds.

boundver publishes its lockfile through a relative NT open with a directory
handle as the root, where a trailing dot, a trailing space and a reserved
device name are ordinary filename characters. Every guard that validates the
path, and every reader that later opens it, uses Win32 pathnames, where they
are not. So the two can disagree about which object a path denotes, and the
disagreement is silent: generate reports success and verify reports a lock it
cannot find.

The obligation admits two acceptable answers for any spelling the CLI takes:
the bytes come back, or the spelling is refused before anything is created.
This file asserts that disjunction and records where it fails, and then asks
the narrower question the name rule is supposed to answer on its own: is an
unportable output name refused by an explicit rule, before an ancestor is
created, rather than incidentally by something further down?

Covers OBL-LOCKFILE-064 and OBL-GLOBS-068.
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

from boundver._utils import ConfigError
from boundver.core import _prepare_atomic_output, _repository_relative_path
from tests._parity import run_cli
from tests._scenarios import Scenario

COULD_NOT_CHECK = 2

on_windows = unittest.skipUnless(
    os.name == "nt", "the two namespaces only differ on Windows"
)


class _Published:
    """Generate a lock at one spelling, then read it back at the same one."""

    def __init__(self, spelling: str) -> None:
        scene = Scenario()
        scene.component("svc", path="svc", boundary=["api"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.commit()
        self.scene = scene
        self.spelling = spelling
        self.generate = run_cli(scene.root, "generate", "--source", "head", "--out", spelling)
        self.verify = run_cli(
            scene.root, "verify", "--source", "working-tree", "--lock", spelling
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def entries(self):
        """The directory entries the writer left, as the filesystem spells them."""
        directory = self.scene.root / "out"
        return sorted(os.listdir(directory)) if directory.is_dir() else []

    def enumerated_size(self, name: str) -> int:
        """The size the directory itself reports, without opening the file."""
        with os.scandir(self.scene.root / "out") as scan:
            for entry in scan:
                if entry.name == name:
                    return entry.stat().st_size
        raise AssertionError(f"{name} is not in {self.entries()}")

    def bytes_through_a_win32_open(self) -> bytes:
        return (self.scene.root / self.spelling).read_bytes()


class OutputPathRoundTripTests(unittest.TestCase):
    """The ordinary spellings, on every host."""

    def test_an_output_path_round_trips(self):
        for spelling in ("out/lock.json", "out/nested/deep/lock.json"):
            with self.subTest(spelling=spelling):
                with _Published(spelling) as case:
                    self.assertEqual(case.generate.returncode, 0, case.generate.stderr)
                    self.assertEqual(case.verify.returncode, 0, case.verify.stderr)

    def test_the_bytes_read_back_are_a_lockfile(self):
        """The premise: verify's exit 0 is a lock it read, not one it skipped."""
        with _Published("out/lock.json") as case:
            document = json.loads(case.bytes_through_a_win32_open())
            self.assertIn("svc", document["components"])


@on_windows
class WindowsNamespaceSplitTests(unittest.TestCase):
    """Round-trip, or refuse before creating anything. These do neither."""

    def _assert_round_trips_or_is_refused_cleanly(self, spelling: str) -> None:
        with _Published(spelling) as case:
            if case.generate.returncode == 0:
                self.assertEqual(case.verify.returncode, 0, case.verify.stderr)
            else:
                self.assertEqual(case.entries(), [])

    def test_a_reserved_device_name_round_trips_or_is_refused(self):
        """Known divergence: generate exits 0 over bytes no reader can reach."""
        self._assert_round_trips_or_is_refused_cleanly("out/nul")

    def test_a_trailing_space_round_trips_or_is_refused(self):
        """Known divergence: the same, at a name Win32 cannot address at all."""
        self._assert_round_trips_or_is_refused_cleanly("out/lock.json ")

    def test_a_trailing_dot_round_trips_or_is_refused(self):
        """Known divergence: as above."""
        self._assert_round_trips_or_is_refused_cleanly("out/lock.json.")

    def test_a_trailing_dot_parent_is_refused_before_it_is_created(self):
        """Known divergence: refused, but the directory is already on disk."""
        self._assert_round_trips_or_is_refused_cleanly("out/dist./boundver.lock")


@on_windows
class WindowsNamespaceScopeTests(unittest.TestCase):
    """What each divergence is, exactly, so a partial fix cannot pass unnoticed."""

    def test_a_reserved_name_is_refused_before_publication(self):
        with _Published("out/nul") as case:
            self.assertEqual(case.generate.returncode, COULD_NOT_CHECK)
            self.assertEqual(case.entries(), [])
            self.assertEqual(case.verify.returncode, COULD_NOT_CHECK)

    def test_a_trailing_character_is_refused_without_leaving_bytes(self):
        for spelling in ("out/lock.json ", "out/lock.json."):
            with self.subTest(spelling=spelling):
                with _Published(spelling) as case:
                    self.assertEqual(case.generate.returncode, COULD_NOT_CHECK)
                    self.assertEqual(case.entries(), [])
                    self.assertEqual(case.verify.returncode, COULD_NOT_CHECK)

    def test_the_refused_parent_is_not_created(self):
        with _Published("out/dist./boundver.lock") as case:
            self.assertEqual(case.generate.returncode, COULD_NOT_CHECK)
            self.assertIn("portable filenames", case.generate.stderr)
            self.assertEqual(case.entries(), [])
            self.assertFalse(os.path.exists(str(case.scene.root / "out" / "dist.")))

    def test_git_add_all_remains_usable_after_a_refusal(self):
        for spelling in ("out/nul", "out/lock.json ", "out/lock.json."):
            with self.subTest(spelling=spelling):
                with _Published(spelling) as case:
                    added = subprocess.run(
                        ["git", "add", "--all"], cwd=case.scene.root,
                        capture_output=True, text=True,
                    )
                    self.assertEqual(added.returncode, 0, added.stderr)
                    self.assertNotIn(spelling, case.scene.git("ls-files").splitlines())

    def test_the_same_directory_takes_an_ordinary_name(self):
        """The contrast: nothing here is a general failure to write to out/."""
        with _Published("out/lock.json") as case:
            self.assertEqual(case.generate.returncode, 0, case.generate.stderr)
            self.assertEqual(case.verify.returncode, 0, case.verify.stderr)
            self.assertEqual(case.entries(), ["lock.json"])


#: Reserved device names, with and without an extension. The rule is meant to
#: apply to the stem, so a suffix changes nothing.
RESERVED = (
    "NUL",
    "CON.txt",
    "com1",
    "LPT9.json",
    "COM¹.txt",
    "lpt³.json",
    "aux",
    "prn",
)

#: Segments that end in a character Win32 strips and NT keeps.
TRAILING = ("lock.json.", "lock.json ")

# Windows refuses these inside a filename. They must also be rejected on
# POSIX so one output argument cannot denote different paths across hosts.
WINDOWS_INVALID_CHARACTERS = (
    "<", ">", '"', ":", "|", "?", "*", "\\", "\x01", "\x1f",
)


class OutputNameRuleTests(unittest.TestCase):
    """OBL-GLOBS-068: an unportable name is refused by rule, not by accident.

    The obligation asks for a refusal from the name check at the top of
    _prepare_atomic_output, before a directory handle is opened or an ancestor
    created. Both halves are observable from outside: the exit code says
    whether it was refused, and whether the ancestor directory exists
    afterwards says whether the refusal came first.
    """

    def _generated(self, spelling: str):
        with Scenario() as scene:
            scene.component("svc", path="svc", boundary=["api"])
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.commit()
            result = run_cli(scene.root, "generate", "--source", "head", "--out", spelling)
            return result, (scene.root / "deep").exists()

    def test_a_portable_name_is_accepted(self):
        """The premise: the rule under test is about the name, not the depth."""
        result, ancestor = self._generated("deep/here/lock.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(ancestor)

    def test_a_lone_surrogate_is_refused_before_path_construction(self):
        with self.assertRaisesRegex(ConfigError, "portable filenames"):
            _repository_relative_path(
                Path.cwd(),
                "deep/lock\udc80.json",
                label="Output path",
            )

    def test_the_atomic_writer_refuses_a_preconstructed_surrogate_path(self):
        with self.assertRaisesRegex(ConfigError, "portable filenames"):
            _prepare_atomic_output(Path.cwd() / "deep" / "lock\udc80.json")

    def test_a_reserved_device_name_is_refused(self):
        """Known divergence: the name check tests for ':' and little else."""
        for spelling in RESERVED:
            with self.subTest(spelling=spelling):
                result, _ancestor = self._generated(spelling)
                self.assertEqual(result.returncode, COULD_NOT_CHECK, result.stdout)

    def test_a_trailing_dot_or_space_leaf_is_refused(self):
        """Known divergence: as above."""
        for spelling in TRAILING:
            with self.subTest(spelling=spelling):
                result, _ancestor = self._generated(spelling)
                self.assertEqual(result.returncode, COULD_NOT_CHECK, result.stdout)

    def test_an_unportable_interior_segment_is_refused_before_it_is_created(self):
        """Known divergence: refused, but the ancestors already exist."""
        for spelling in ("deep/here./lock.json", "deep/here /lock.json"):
            with self.subTest(spelling=spelling):
                result, ancestor = self._generated(spelling)
                self.assertEqual(result.returncode, COULD_NOT_CHECK, result.stdout)
                self.assertFalse(ancestor)

    def test_the_rule_that_does_fire_is_the_colon(self):
        """Pin what the name check catches, so its scope is on the record."""
        result, _ancestor = self._generated("lock.json:stream")
        self.assertEqual(result.returncode, COULD_NOT_CHECK, result.stdout)
        self.assertIn("portable filename", result.stderr)

    def test_windows_invalid_characters_are_refused_on_every_host(self):
        for character in WINDOWS_INVALID_CHARACTERS:
            with self.subTest(character=repr(character)):
                result, ancestor = self._generated(
                    f"deep/lock{character}.json"
                )
                self.assertEqual(result.returncode, COULD_NOT_CHECK, result.stdout)
                self.assertIn("portable filenames", result.stderr)
                if ord(character) < 32:
                    self.assertNotIn(character, result.stderr)
                self.assertFalse(ancestor)

    @on_windows
    def test_the_interior_refusal_is_early_and_names_portability(self):
        result, ancestor = self._generated("deep/here./lock.json")
        self.assertEqual(result.returncode, COULD_NOT_CHECK, result.stdout)
        self.assertIn("portable filenames", result.stderr)
        self.assertFalse(ancestor)

    def test_every_reserved_leaf_is_refused(self):
        for spelling in RESERVED + TRAILING:
            with self.subTest(spelling=spelling):
                result, _ancestor = self._generated(spelling)
                self.assertEqual(result.returncode, COULD_NOT_CHECK, result.stdout)


if __name__ == "__main__":
    unittest.main()
