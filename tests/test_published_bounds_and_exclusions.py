"""A document that must satisfy the schema it ships with, and a prefix that
must stop at a directory boundary.

A published JSON schema is a promise to whoever parses the output, so a field
with a maxLength is a claim about every accepted input and not only about the
ones a repository is likely to produce. `--base-ref` is the interesting case
because argparse imposes no length of its own.

The exclusion question is the same shape as component selection: `src/a` must
name a directory, so `src/ab` is a different one.

Covers OBL-GRAPH-019, OBL-OUTPUT-024 and OBL-OUTPUT-031.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests._parity import run_cli
from tests._scenarios import Scenario

COULD_NOT_CHECK = 2
DRIFT = 1

#: The bounds the shipped why schema publishes for the base fields.
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "spec" / "cli-output.why.schema.json"

MANIFEST = '{"name": "n", "version": "1.0.0"}\n'

#: Directories that share a string prefix without sharing a boundary.
DISCOVERED = ("src/a", "src/ab", "src/a-b", "src/a/deep")

#: Exclusion spellings that must be refused rather than interpreted.
WILDCARDS = ("src/*", "src/a?", "src/[ab]")


def _published_bounds() -> dict:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return {
        name: schema["properties"][name]["maxLength"]
        for name in (
            "diagnostic_base_requested",
            "diagnostic_base",
            "diagnostic_base_origin",
        )
    }


class _Drifted:
    """One component whose exact digest has moved."""

    def __init__(self) -> None:
        scene = Scenario()
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/main.py", "x\n")
        scene.commit()
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        scene.commit("lock")
        scene.append_line("svc/main.py", "y\n")
        scene.commit("edit")
        self.scene = scene

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def why(self, *extra) -> dict:
        result = self.run_why(*extra)
        assert result.returncode == DRIFT, result.stderr
        return json.loads(result.stdout)

    def run_why(self, *extra):
        return run_cli(
            self.scene.root, "why", "svc", "--source", "head", "--format", "json",
            *extra,
        )


class PublishedBoundTests(unittest.TestCase):
    """OBL-GRAPH-019: the schema's maxLength is a claim about every input."""

    def test_the_schema_publishes_the_base_bounds(self):
        """The premise: these are the numbers the document promises."""
        self.assertEqual(
            _published_bounds(),
            {
                "diagnostic_base_requested": 16384,
                "diagnostic_base": 16384,
                "diagnostic_base_origin": 32768,
            },
        )

    def test_an_ordinary_base_ref_is_well_inside_them(self):
        with _Drifted() as repo:
            document = repo.why("--base-ref", "HEAD~1")
            bounds = _published_bounds()
            for field, limit in bounds.items():
                with self.subTest(field=field):
                    value = document[field]
                    if value is not None:
                        self.assertLessEqual(len(value), limit)

    def test_an_oversized_base_ref_is_refused_before_json_output(self):
        bounds = _published_bounds()
        oversized = "r" * (bounds["diagnostic_base"] + 1)
        with _Drifted() as repo:
            result = repo.run_why("--base-ref", oversized)
        self.assertEqual(result.returncode, COULD_NOT_CHECK)
        self.assertEqual(result.stdout, "")
        self.assertIn("invalid diagnostic base ref", result.stderr)

    def test_an_oversized_ref_has_a_bounded_refusal_diagnostic(self):
        bounds = _published_bounds()
        for extra in (1, 4096):
            length = bounds["diagnostic_base"] + extra
            with self.subTest(length=length):
                with _Drifted() as repo:
                    result = repo.run_why("--base-ref", "r" * length)
                self.assertEqual(result.returncode, COULD_NOT_CHECK)
                self.assertLess(len(result.stderr), 1024)
                self.assertIn("...", result.stderr)

    def test_a_resolved_ref_satisfies_every_published_base_bound(self):
        with _Drifted() as repo:
            document = repo.why("--base-ref", "HEAD~1")
        for field, limit in _published_bounds().items():
            with self.subTest(field=field):
                self.assertLessEqual(len(document[field]), limit)


class DiscoveryExclusionTests(unittest.TestCase):
    """OBL-OUTPUT-024: an exclusion names a directory, not a string."""

    def _discovered(self, *extra) -> set:
        with Scenario() as scene:
            scene.config["components"] = {
                "root": {"path": "keep", "boundary": {"provider": "leaf", "paths": []}}
            }
            scene.file("keep/x.py", "x = 1\n")
            for name in DISCOVERED:
                scene.file(f"{name}/package.json", MANIFEST)
                scene.file(f"{name}/index.js", "module.exports = 1;\n")
            scene.commit()
            result = run_cli(scene.root, "discover", "--format", "json", *extra)
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(result.stdout)
            return {entry["path"] for entry in document["components"].values()}

    def test_every_directory_is_discovered_without_an_exclusion(self):
        """The premise: the siblings exist to be confused with each other."""
        self.assertEqual(self._discovered(), set(DISCOVERED))

    def test_an_exclusion_takes_the_directory_and_its_subtree_only(self):
        self.assertEqual(
            self._discovered("--exclude", "src/a"), {"src/ab", "src/a-b"}
        )

    def test_a_wildcard_exclusion_is_refused(self):
        for spelling in WILDCARDS:
            with self.subTest(spelling=spelling):
                with Scenario() as scene:
                    scene.component("svc", path="svc", provider="leaf")
                    scene.file("svc/main.py", "x\n")
                    scene.commit()
                    result = run_cli(scene.root, "discover", "--exclude", spelling)
                    self.assertEqual(result.returncode, COULD_NOT_CHECK)
                    self.assertIn("literal path prefixes", result.stderr)

    def test_a_root_or_empty_exclusion_is_refused_by_name(self):
        for spelling, message in ((".", "must not contain"), ("", "must not be empty")):
            with self.subTest(spelling=spelling):
                with Scenario() as scene:
                    scene.component("svc", path="svc", provider="leaf")
                    scene.file("svc/main.py", "x\n")
                    scene.commit()
                    result = run_cli(scene.root, "discover", "--exclude", spelling)
                    self.assertEqual(result.returncode, COULD_NOT_CHECK)
                    self.assertIn(message, result.stderr)


class SummaryFileFlagTests(unittest.TestCase):
    """OBL-OUTPUT-031: the flag belongs to one format."""

    def _reviewed(self, output_format: str):
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/main.py", "x\n")
            scene.commit()
            assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
            scene.commit("lock")
            base = scene.head()
            scene.append_line("svc/main.py", "y\n")
            scene.commit("edit")
            assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
            scene.commit("relock")
            name = f"summary-{output_format}.md"
            result = run_cli(
                scene.root, "review", "--base", base, "--target", scene.head(),
                "--format", output_format, "--summary-file", name,
            )
            return result, (scene.root / name).exists()

    def test_the_flag_requires_the_plan_format(self):
        for output_format in ("text", "json"):
            with self.subTest(format=output_format):
                result, written = self._reviewed(output_format)
                self.assertEqual(result.returncode, COULD_NOT_CHECK)
                self.assertIn("--summary-file requires --format plan", result.stderr)
                self.assertFalse(written)

    def test_the_plan_format_writes_the_file(self):
        """The contrast: the flag does work where it belongs."""
        result, written = self._reviewed("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(written)


if __name__ == "__main__":
    unittest.main()
