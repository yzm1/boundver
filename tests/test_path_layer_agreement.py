"""Two layers that must agree about a path, and one host that disagrees.

A declared path passes two independent gates: the `relativePath` pattern in
boundary.config.schema.json and `_normalize_declared_path`. Two implementations
of one rule drift, and only a differential notices.

Separately, a version-source path is opened rather than matched, and Windows
resolves some names to things that are not files.

Covers OBL-CONFIG-003 and OBL-CONFIG-055.
"""

from __future__ import annotations

import io
import itertools
import json
import os
import re
import unittest
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver import versions
from boundver._utils import _normalize_declared_path

from tests._scenarios import SOURCE_MODES, Scenario

SCHEMA = Path(__file__).resolve().parents[1] / "boundary.config.schema.json"
LF = chr(10)

PROFILE = settings(
    max_examples=400,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)


def _relative_path_pattern() -> re.Pattern:
    document = json.loads(io.open(SCHEMA, encoding="utf-8").read())
    return re.compile(document["$defs"]["relativePath"]["pattern"])


PATTERN = _relative_path_pattern()


def schema_accepts(value: str) -> bool:
    return bool(PATTERN.search(value))


def normalizer_accepts(value: str) -> bool:
    try:
        _normalize_declared_path(value)
    except ValueError:
        return False
    return True


class SchemaVersusNormalizerTests(unittest.TestCase):
    """OBL-CONFIG-003: the two gates must accept exactly the same strings."""

    #: Characters the obligation calls out, plus the separators and the
    #: prefixes each gate special-cases.
    ALPHABET = "ab/.:~ " + chr(92) + chr(9) + chr(13) + chr(0) + "\u00a0\u2028\u0085"

    @PROFILE
    @given(value=st.text(alphabet=ALPHABET, min_size=1, max_size=6))
    def test_the_two_gates_agree_for_generated_paths(self, value):
        self.assertEqual(
            schema_accepts(value),
            normalizer_accepts(value),
            f"{value!r}: schema={schema_accepts(value)} "
            f"normalizer={normalizer_accepts(value)}",
        )

    def test_exhaustive_short_paths_have_no_unexplained_divergence(self):
        alphabet = "a/.:" + chr(92) + " " + LF + chr(13) + chr(9)
        unexplained = []
        for length in range(1, 5):
            for combination in itertools.product(alphabet, repeat=length):
                value = "".join(combination)
                if schema_accepts(value) == normalizer_accepts(value):
                    continue
                unexplained.append(value)
        self.assertEqual(unexplained, [], f"a divergent class: {unexplained[:5]}")

    def test_the_named_spellings_agree(self):
        for value in (
            "a/b", "a", "./a", "a/./b", "a/../b", "..", ".", "/abs", "C:/x",
            "a" + chr(92) + "b", "", " ", "a//b", "api/", "  lead", "trail  ",
            "~/x", "a b", "a.", ".hidden", "x/.hidden", "a" + chr(0) + "b",
        ):
            with self.subTest(value=value):
                self.assertEqual(schema_accepts(value), normalizer_accepts(value))

    def test_an_interior_newline_is_refused_by_both_gates(self):
        self.assertFalse(schema_accepts("a" + LF + "b"))
        self.assertFalse(normalizer_accepts("a" + LF + "b"))

    def test_a_trailing_run_of_slashes_is_refused_by_both_gates(self):
        for value in ("a//", "a///", "a/b//", "://"):
            with self.subTest(value=value):
                self.assertFalse(schema_accepts(value))
                self.assertFalse(normalizer_accepts(value))

    def test_one_trailing_slash_is_not_a_divergence(self):
        """A single trailing slash is accepted by both, which is the contrast."""
        for value in ("a/", "a/b/", ":/"):
            with self.subTest(value=value):
                self.assertTrue(schema_accepts(value))
                self.assertTrue(normalizer_accepts(value))

    def test_a_bare_double_slash_is_refused_by_both(self):
        for value in ("//", "///"):
            with self.subTest(value=value):
                self.assertFalse(schema_accepts(value))
                self.assertFalse(normalizer_accepts(value))

    def test_other_control_and_space_characters_agree(self):
        for code in (0x0d, 0x0b, 0x0c, 0x09, 0x85, 0xa0, 0x2028, 0x2029, 0x00):
            character = chr(code)
            for shape in ("a" + character + "b", character + "ab", "ab" + character):
                with self.subTest(code=hex(code), shape=repr(shape)):
                    self.assertEqual(
                        schema_accepts(shape), normalizer_accepts(shape)
                    )

    def test_the_stricter_gate_runs_first(self):
        """Which is why the disagreement is not reachable through a config."""
        from boundver._config import validate_config

        with Scenario() as scene:
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.component("svc", path="svc", boundary=["api" + LF + "b"])
            scene.commit()
            errors = validate_config(scene.config, scene.root)
            self.assertTrue(errors)
            self.assertIn("Schema validation error", errors[0])


@unittest.skipUnless(os.name == "nt", "alternate data streams are NTFS-only")
class AlternateDataStreamTests(unittest.TestCase):
    """OBL-CONFIG-055: a version source must not read a hidden stream.

    `meta:v.json` names a file on POSIX and an alternate data stream of `meta`
    on Windows. A version read through the disk fallback therefore depends on
    the host, and a version reaches the compat facet.
    """

    STREAM_VERSION = "9.9.9"

    def _repository(self) -> Scenario:
        scene = Scenario()
        scene.component(
            "svc", path="svc", boundary=["api"],
            version_source={"file": "meta:v.json", "field": "version"},
        )
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("svc/meta", "plain content\n")
        scene.commit()
        target = str(scene.root / "svc" / "meta") + ":v.json"
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"version": self.STREAM_VERSION}))
        return scene

    def test_the_stream_exists_and_git_does_not_track_it(self):
        """The premise: a stream is not a file Git can see."""
        with self._repository() as scene:
            self.assertNotIn("meta:v.json", scene.git("ls-files"))
            with open(str(scene.root / "svc" / "meta") + ":v.json", encoding="utf-8") as handle:
                self.assertIn(self.STREAM_VERSION, handle.read())

    def test_no_source_mode_reads_the_stream(self):
        """Generation resolves against the captured tree, which has no stream."""
        with self._repository() as scene:
            for source in SOURCE_MODES:
                with self.subTest(source=source):
                    with self.assertRaises(ValueError) as caught:
                        scene.generate(source=source)
                    self.assertIn("meta:v.json", str(caught.exception))

    def test_the_disk_fallback_does_not_read_the_stream(self):
        """The public fallback applies the same portable-path rule.

        Called without an accessor, extract_version still rejects a colon in
        the declared filename before Windows can interpret it as a stream.
        """
        with self._repository() as scene:
            version = versions.extract_version(
                scene.root, "svc",
                {"file": "meta:v.json", "field": "version"}, None,
            )
            self.assertIsNone(version)

    def test_neither_version_source_spelling_reads_the_stream(self):
        with self._repository() as scene:
            self.assertIsNone(
                versions.extract_version(
                    scene.root, "svc",
                    {"file": "meta:v.json", "field": "version"}, None,
                )
            )
            self.assertIsNone(
                versions.extract_version(
                    scene.root, "svc",
                    {"file": "meta", "field": "version"}, None,
                )
            )


if __name__ == "__main__":
    unittest.main()
