"""Two things a repository writes down, and what boundver makes of them.

A version is a string that identifies a release, so the only safe rendering of
one is the spelling the author wrote. All supported formats therefore require
the selected value to be textual; a parsed number is refused rather than
stringified after its spelling has been lost.

A component path is the other. Two spellings that name one directory must
collide, and two that merely share a prefix must not - which is the difference
between comparing a path and comparing a string.

Covers OBL-CROSSCUTTING-004, 005, 007, 008, 009 and 010.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from boundver._hashing import _ModeAwareBytes
from boundver.versions import extract_version

from tests._parity import run_cli
from tests._scenarios import Scenario

COULD_NOT_CHECK = 2
DRIFT = 1

#: One logical document, in each format that can hold it.
TEMPLATES = {
    "json": '{"project": {"version": %s}}\n',
    "yaml": "project:\n  version: %s\n",
    "yml": "project:\n  version: %s\n",
    "toml": '[project]\nversion = %s\n',
}

#: Formats whose parsers can hand back a number for a bare numeric literal.
NUMERIC_PARSERS = ("json", "yaml", "yml")


def _extracted(suffix: str, rendered: str):
    root = Path(tempfile.mkdtemp())
    (root / "svc").mkdir()
    (root / "svc" / f"meta.{suffix}").write_text(rendered, encoding="utf-8")
    return extract_version(
        root, "svc", {"file": f"meta.{suffix}", "field": "project.version"}
    )


def _for_value(value: str) -> dict:
    return {
        suffix: _extracted(suffix, template % value)
        for suffix, template in TEMPLATES.items()
    }


class CrossFormatParityTests(unittest.TestCase):
    """OBL-CROSSCUTTING-005: one textual value, one answer."""

    def test_a_quoted_version_reads_the_same_in_every_format(self):
        self.assertEqual(
            _for_value('"1.2.3"'),
            {"json": "1.2.3", "yaml": "1.2.3", "yml": "1.2.3", "toml": "1.2.3"},
        )

    def test_a_quoted_version_keeps_its_exact_spelling(self):
        """Leading zeros are part of the string, not a number to be tidied."""
        self.assertEqual(
            _for_value('"0001"'),
            {"json": "0001", "yaml": "0001", "yml": "0001", "toml": "0001"},
        )

    def test_every_format_refuses_a_bare_number(self):
        for value in ("1", "1.10"):
            with self.subTest(value=value):
                self.assertEqual(set(_for_value(value).values()), {None})

    def test_a_yaml_alias_is_not_followed(self):
        """The other documented divergence."""
        aliased = "base: &v '1.2.3'\nproject:\n  version: *v\n"
        self.assertIsNone(_extracted("yaml", aliased))
        self.assertEqual(_extracted("yaml", TEMPLATES["yaml"] % "'1.2.3'"), "1.2.3")

    def test_a_non_numeric_non_string_is_refused_everywhere(self):
        for value in ("true", "0x10"):
            with self.subTest(value=value):
                self.assertEqual(
                    set(_for_value(value).values()), {None}
                )


class NumericVersionTests(unittest.TestCase):
    """OBL-CROSSCUTTING-004: a number is not a version's spelling."""

    def test_a_quoted_version_is_returned_verbatim(self):
        """The premise: the extractor does return what it was given."""
        self.assertEqual(_for_value('"1.10"')["json"], "1.10")

    def test_a_numeric_version_does_not_change_its_spelling(self):
        for suffix in NUMERIC_PARSERS:
            with self.subTest(format=suffix):
                self.assertNotEqual(_for_value("1.10")[suffix], "1.1")

    def test_bare_integers_and_floats_are_refused(self):
        for value in ("1", "1.0", "1.10", "-2.5", "1e5"):
            with self.subTest(value=value):
                self.assertEqual(set(_for_value(value).values()), {None})

    def test_a_quoted_integer_keeps_its_spelling(self):
        self.assertEqual(set(_for_value('"0001"').values()), {"0001"})

    def test_generate_refuses_a_numeric_file_backed_version(self):
        with Scenario() as scene:
            scene.config["components"] = {
                "svc": {
                    "path": "svc",
                    "version_source": {
                        "file": "package.json",
                        "field": "version",
                    },
                    "boundary": {
                        "provider": "path-hash",
                        "paths": ["contract.txt"],
                    },
                }
            }
            scene.file("svc/contract.txt", "contract\n")
            scene.file("svc/package.json", '{"version": 1.10}\n')
            scene.commit()

            result = run_cli(scene.root, "generate", "--source", "head")

            self.assertEqual(result.returncode, COULD_NOT_CHECK)
            self.assertIn(
                "Configured version source did not produce a version",
                result.stderr,
            )
            self.assertFalse((scene.root / "boundary.lock.json").exists())


class TomlNumericTests(unittest.TestCase):
    """OBL-CROSSCUTTING-007: TOML takes a string or nothing."""

    def _toml(self, literal: str):
        return _extracted("toml", TEMPLATES["toml"] % literal)

    def test_every_bare_number_is_refused(self):
        for literal in ("1", "1.5", "0x10", "1_000", "true"):
            with self.subTest(literal=literal):
                self.assertIsNone(self._toml(literal))

    def test_a_quoted_number_extracts_its_source_spelling(self):
        for literal, expected in (('"1"', "1"), ('"1.5"', "1.5"), ('"0001"', "0001")):
            with self.subTest(literal=literal):
                self.assertEqual(self._toml(literal), expected)


class ModeAwareBytesTests(unittest.TestCase):
    """OBL-CROSSCUTTING-008: the constructor's four validation branches."""

    def _make(self, content=b"abc", **keywords):
        return _ModeAwareBytes(
            content, git_mode="100644", git_object_type="blob", **keywords
        )

    def test_the_default_is_the_content_length(self):
        self.assertEqual(self._make().source_size, 3)

    def test_a_larger_explicit_size_is_preserved(self):
        """Normalisation shrinks the bytes; the budget charges what was read."""
        self.assertEqual(self._make(source_size=99).source_size, 99)

    def test_a_bool_is_not_an_integer_here(self):
        with self.assertRaises(TypeError):
            self._make(source_size=True)

    def test_a_non_integer_is_refused(self):
        with self.assertRaises(TypeError):
            self._make(source_size="3")

    def test_a_negative_size_is_refused(self):
        with self.assertRaises(ValueError):
            self._make(source_size=-1)

    def test_the_mode_and_type_are_carried(self):
        """The premise: this is a bytes subclass that still holds bytes."""
        value = self._make()
        self.assertEqual(bytes(value), b"abc")
        self.assertEqual(value.git_mode, "100644")
        self.assertEqual(value.git_object_type, "blob")


def _components(**paths) -> dict:
    return {
        name: {"path": path, "boundary": {"provider": "leaf", "paths": []}}
        for name, path in paths.items()
    }


class PathIdentityTests(unittest.TestCase):
    """OBL-CROSSCUTTING-009: one directory, one component."""

    def _generated(self, components, files):
        with Scenario() as scene:
            scene.config["components"] = components
            for path in files:
                scene.file(f"{path}/index.ts", "export const x = 1;\n")
            scene.commit()
            return run_cli(scene.root, "generate", "--source", "head")

    def test_two_spellings_of_one_directory_collide(self):
        for first, second in (("app", "app/"), ("app", "app")):
            with self.subTest(paths=(first, second)):
                result = self._generated(
                    _components(a=first, b=second), ["app"]
                )
                self.assertEqual(result.returncode, COULD_NOT_CHECK)
                self.assertIn("Duplicate component path 'app'", result.stderr)

    def test_a_nested_pair_is_accepted(self):
        """Pinned as the current rule, which the obligation asks for."""
        result = self._generated(
            _components(a="app", b="app/sub"), ["app", "app/sub"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_sibling_sharing_a_prefix_is_accepted(self):
        """The contrast: 'app' and 'app-v2' are two directories."""
        result = self._generated(
            _components(a="app", b="app-v2"), ["app", "app-v2"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ChangedFromSelectionTests(unittest.TestCase):
    """OBL-CROSSCUTTING-010: a path boundary, not a string prefix."""

    PATHS = {"app": "src/app", "appv2": "src/app-v2", "sub": "src/app/sub"}

    def _selected_by(self, edited: str) -> set:
        with Scenario() as scene:
            scene.config["components"] = _components(**self.PATHS)
            for path in self.PATHS.values():
                scene.file(f"{path}/index.ts", "export const x = 1;\n")
            scene.commit()
            assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
            scene.commit("lock")
            base = scene.head()
            scene.append_line(f"{edited}/index.ts", "// edit\n")
            scene.commit("edit")
            result = run_cli(
                scene.root, "verify", "--source", "head",
                "--changed-from", base, "--format", "json",
            )
            self.assertEqual(result.returncode, DRIFT, result.stdout)
            document = json.loads(result.stdout)
            return {
                line.split()[1].split(".")[0]
                for line in document["issues"] if line.startswith("MISMATCH")
            }

    def test_a_sibling_prefix_selects_only_itself(self):
        """The whole point of comparing a path rather than a string."""
        self.assertEqual(self._selected_by("src/app-v2"), {"appv2"})

    def test_a_change_inside_selects_the_component(self):
        """The premise: selection does happen."""
        self.assertEqual(self._selected_by("src/app"), {"app"})

    def test_a_nested_change_selects_both_components(self):
        self.assertEqual(self._selected_by("src/app/sub"), {"app", "sub"})

    def test_the_degenerate_roots_the_obligation_names_are_unreachable(self):
        """A component path of '', '.' or './src/app/' never validates.

        The obligation asks what those spellings select. The schema refuses
        all three before selection is reached, so the question has no answer
        to assert - which is worth recording rather than leaving as a gap.
        """
        with Scenario() as scene:
            scene.file("src/app/index.ts", "export const x = 1;\n")
            for spelling in ("", ".", "./src/app/"):
                with self.subTest(path=spelling):
                    scene.config["components"] = _components(app=spelling)
                    scene.write_config()
                    scene.commit(f"path {spelling!r}")
                    result = run_cli(scene.root, "generate", "--source", "head")
                    self.assertEqual(result.returncode, COULD_NOT_CHECK)
                    self.assertIn("Schema validation error", result.stderr)

    def test_a_trailing_slash_is_the_one_spelling_that_does_normalize(self):
        """The contrast: 'src/app/' validates and means 'src/app'."""
        with Scenario() as scene:
            scene.config["components"] = _components(app="src/app/")
            scene.file("src/app/index.ts", "export const x = 1;\n")
            scene.commit()
            self.assertEqual(
                run_cli(scene.root, "generate", "--source", "head").returncode, 0
            )


if __name__ == "__main__":
    unittest.main()
