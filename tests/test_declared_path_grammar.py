"""The declared-path grammar, and what Windows device names do to it.

Several obligations require one verdict on every host for the same config and
the same tree. That is hard to test from one host, so each test here either
compares two spellings whose verdicts must match, in which case a divergence
fails wherever it happens, or asserts a rule that holds everywhere.

The working-tree existence check requires a real file or directory, so device
aliases receive the same missing-path verdict as ordinary absent names.

Covers OBL-CONFIG-027, OBL-CONFIG-054, OBL-GLOBS-066 and OBL-HASHING-118.
"""

from __future__ import annotations

import unittest

from boundver._config import validate_config
from boundver._utils import ConfigError, _normalize_declared_path

from tests._scenarios import SOURCE_MODES, Scenario

BACKSLASH = chr(92)
NUL_BYTE = chr(0)

#: Windows resolves a bare NUL to the null device whatever directory is named,
#: so `Path.exists()` answers True for a file nobody created. Every other
#: reserved name on this host answers False, which is narrower than folklore
#: suggests and is why the table below is measured rather than assumed.
DIVERGING_DEVICE_NAMES = ("NUL", "nul")

ORDINARY_DEVICE_NAMES = (
    "CON", "PRN", "AUX", "COM1", "LPT1", "NUL.txt", "nul.json", "CONIN$",
)


class DeclaredPathGrammarTests(unittest.TestCase):
    """OBL-CONFIG-027: one verdict per spelling, on every host."""

    REJECTED = (
        ("single dot segment", "./api.yaml"),
        ("interior dot segment", "a/./b"),
        ("parent segment", "a/../b"),
        ("bare parent", ".."),
        ("bare dot", "."),
        ("absolute posix", "/abs/path"),
        ("drive letter", "C:/x"),
        ("backslash separator", "a" + BACKSLASH + "b"),
        ("empty", ""),
        ("whitespace only", " "),
        ("leading whitespace", "  lead"),
        ("trailing whitespace", "trail  "),
        ("empty segment", "api//b"),
        ("escaping parent", "a/b/../c"),
    )

    ACCEPTED = (
        ("plain file", "api.yaml", "api.yaml"),
        ("nested", "api/v1/spec.yaml", "api/v1/spec.yaml"),
        ("trailing slash normalized", "api/", "api"),
        ("trailing slash on a file name", "api.yaml/", "api.yaml"),
    )

    def test_every_unportable_spelling_is_refused(self):
        for label, spelling in self.REJECTED:
            with self.subTest(spelling=label):
                with self.assertRaises(ValueError):
                    _normalize_declared_path(spelling)

    def test_every_accepted_spelling_normalizes_predictably(self):
        for label, spelling, expected in self.ACCEPTED:
            with self.subTest(spelling=label):
                self.assertEqual(_normalize_declared_path(spelling), expected)

    def test_the_grammar_rejects_control_characters(self):
        """Control characters are refused before filesystem or Git lookup."""
        for label, spelling in (
            ("NUL byte", "a" + NUL_BYTE + "b"),
            ("carriage return", "a" + chr(13) + "b"),
            ("line feed", "a" + chr(10) + "b"),
            ("tab", "a" + chr(9) + "b"),
        ):
            with self.subTest(character=label):
                with self.assertRaisesRegex(
                    ValueError, "must not contain control characters"
                ):
                    _normalize_declared_path(spelling)


class WindowsDeviceNameTests(unittest.TestCase):
    """OBL-CONFIG-054: one config and one tree must give one verdict.

    `validate_config` decides whether a declared boundary path exists with
    `Path.exists()`. On Windows a bare NUL resolves to the null device in any
    directory, so that call answers True for a file nobody created and
    validation reports no error. On POSIX the same config reports "boundary
    path not found". Same config, same tree, different answer by host.
    """

    def _errors(self, declared: str):
        with Scenario() as scene:
            scene.file("svc/real.py", "x = 1\n")
            scene.component("svc", path="svc", boundary=[declared])
            scene.commit()
            return validate_config(scene.config, scene.root)

    def test_an_ordinary_missing_path_is_reported(self):
        self.assertTrue(self._errors("missing.py"))

    def test_the_cited_existence_check_reports_in_its_own_words(self):
        """The anchor the comparisons in this class rest on.

        Every other test here asserts that two verdicts agree, and an
        assertion of that shape is invariant under any change that breaks
        both sides at once. Disabling the `full.exists()` check this
        obligation names leaves a second layer still refusing the same
        path, so the error list stays non-empty, every comparison still
        holds, and MUT-CONFIG-202 survived the whole file.

        So pin the line the obligation cites, by its own wording. The two
        layers word it differently - this one lowercases 'boundary path'
        and puts no colon after the component name - which is what makes
        them separable here, and is also worth knowing: a missing path is
        currently reported twice.
        """
        errors = self._errors("missing.py")
        self.assertTrue(
            any(
                error.startswith("Component 'svc' boundary path not found:")
                for error in errors
            ),
            errors,
        )

    def test_a_present_path_is_accepted(self):
        """The contrast: the assertion above is not true of every config."""
        self.assertEqual(self._errors("real.py"), [])

    def test_most_reserved_names_behave_like_any_missing_path(self):
        """Measured, not assumed: only a bare NUL diverges on this host."""
        baseline = bool(self._errors("missing.py"))
        for name in ORDINARY_DEVICE_NAMES:
            with self.subTest(name=name):
                self.assertEqual(bool(self._errors(name)), baseline)

    def test_a_bare_null_device_is_reported_like_any_missing_path(self):
        """A device alias is not accepted as a declared file or directory."""
        baseline = bool(self._errors("missing.py"))
        for name in DIVERGING_DEVICE_NAMES:
            self.assertEqual(bool(self._errors(name)), baseline, name)


class DeviceNameFailClosedTests(unittest.TestCase):
    """OBL-HASHING-118: no device may contribute bytes to a digest.

    Selection runs over tracked files rather than the filesystem, so a device
    name matches nothing and generation refuses. That holds on every host and
    in every source mode, reinforcing the working-tree validation check above.
    """

    def _refusal(self, declared: str, source: str) -> str:
        with Scenario() as scene:
            scene.file("svc/real.py", "x = 1\n")
            scene.component("svc", path="svc", boundary=[declared])
            scene.commit()
            with self.assertRaises(ConfigError) as caught:
                scene.generate(source=source)
            return str(caught.exception)

    def test_a_device_name_never_reaches_a_digest(self):
        for name in DIVERGING_DEVICE_NAMES + ORDINARY_DEVICE_NAMES:
            for source in SOURCE_MODES:
                with self.subTest(name=name, source=source):
                    self.assertIn("matched no tracked files", self._refusal(name, source))

    def test_the_refusal_matches_an_ordinary_missing_path(self):
        """The device name gets no special treatment, which is the point."""
        for source in SOURCE_MODES:
            with self.subTest(source=source):
                self.assertEqual(
                    self._refusal("NUL", source).replace("NUL", "missing.py"),
                    self._refusal("missing.py", source),
                )


if __name__ == "__main__":
    unittest.main()
