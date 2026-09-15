"""A component rooted at the repository, in a report about an older release.

Migration analysis compares what a v0.10 selector would have matched against
what the current one matches. A component path of '.' is a special case on
both sides: the file list is the whole repository rather than a subtree, and
the current release refuses the spelling outright. The analysis handles the
first and says nothing about the second, so the report a user reads is a
clean comparison of a configuration that will not load.

Covers OBL-CROSSCUTTING-003.
"""

from __future__ import annotations

import json
import unittest

from tests._parity import run_cli
from tests._scenarios import Scenario

COULD_NOT_CHECK = 2

#: Every spelling that posixpath.normpath folds to '.', including the one
#: that only gets there after a strip.
ROOT_SPELLINGS = (".", "./", "a/..", " . ")

#: A v3 lock, so migrate-lock has something to explain.
OLD_LOCK = {
    "schema": "boundver/v3",
    "project": "scenario",
    "generated_source": "head",
    "components": {},
}


class _Analysed:
    """One repository, analysed for one component path spelling."""

    def __init__(
        self, path: str, selector: str = "api/v1.yaml", provider: str = "openapi"
    ) -> None:
        scene = Scenario()
        scene.config["components"] = {
            "svc": {
                "path": path,
                "boundary": {"provider": provider, "paths": [selector]},
            }
        }
        scene.file("api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("elsewhere/z.yaml", "openapi: 3.1.0\n")
        scene.commit()
        (scene.root / "boundary.lock.json").write_text(
            json.dumps(OLD_LOCK, indent=2), encoding="utf-8"
        )
        self.scene = scene
        self.json = run_cli(
            scene.root, "migrate-lock", "--explain", "--format", "json"
        )
        self.text = run_cli(scene.root, "migrate-lock", "--explain")
        self.validated = run_cli(scene.root, "validate-config")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def declaration(self) -> dict:
        document = json.loads(self.json.stdout)
        declarations = document["declarations"]
        assert len(declarations) == 1, declarations
        return declarations[0]


class RootComponentFileListTests(unittest.TestCase):
    """Both columns must see the whole repository, not a subtree."""

    def test_a_root_component_is_marked_current_rejected(self):
        with _Analysed(".", selector="elsewhere/z.yaml") as case:
            entry = case.declaration()
            self.assertEqual(entry["analysis_status"], "current-rejected", entry)
            self.assertEqual(entry["impact"], "not-comparable", entry)
            self.assertIsNone(entry["legacy_match_count"], entry)
            self.assertIsNone(entry["current_match_count"], entry)

    def test_a_subtree_component_does_not(self):
        """The contrast: the whole-repository list is the special case."""
        with _Analysed("svc", selector="elsewhere/z.yaml") as case:
            entry = case.declaration()
            self.assertEqual(entry["legacy_match_count"], 0, entry)
            self.assertEqual(entry["current_match_count"], 0, entry)

    def test_a_root_glob_is_not_presented_as_a_clean_comparison(self):
        with _Analysed(".", selector="*.yaml") as case:
            entry = case.declaration()
            self.assertEqual(entry["analysis_status"], "current-rejected", entry)
            self.assertEqual(entry["impact"], "not-comparable", entry)

    def test_every_root_spelling_reaches_the_same_analysis(self):
        answers = {}
        for spelling in ROOT_SPELLINGS:
            with _Analysed(spelling, selector="elsewhere/z.yaml") as case:
                answers[spelling] = case.declaration()
        for spelling, entry in answers.items():
            with self.subTest(spelling=spelling):
                self.assertEqual(entry["analysis_status"], "current-rejected")
                self.assertEqual(entry["impact"], "not-comparable")
                self.assertIn(repr(spelling), entry["detail"])


class RootComponentDisclosureTests(unittest.TestCase):
    """The report must say the current release refuses the path it analysed."""

    def test_the_current_release_does_refuse_it(self):
        """The premise, without which there would be nothing to disclose."""
        for spelling in ROOT_SPELLINGS:
            with self.subTest(spelling=spelling):
                with _Analysed(spelling) as case:
                    self.assertEqual(
                        case.validated.returncode, COULD_NOT_CHECK, case.validated.stdout
                    )

    def test_an_ordinary_path_is_accepted(self):
        """The contrast: it is this spelling that is refused, not the config."""
        with _Analysed("svc") as case:
            self.assertEqual(case.validated.returncode, 0, case.validated.stderr)

    def test_the_report_discloses_the_refusal(self):
        """Both report views disclose that current Boundver rejects the path."""
        for spelling in ROOT_SPELLINGS:
            with self.subTest(spelling=spelling):
                with _Analysed(spelling) as case:
                    self.assertIn("reject", case.text.stdout.lower())

    def test_the_report_marks_the_declaration_not_comparable(self):
        with _Analysed(".") as case:
            entry = case.declaration()
            self.assertEqual(entry["analysis_status"], "current-rejected")
            self.assertEqual(entry["impact"], "not-comparable")
            self.assertIn("rejects component path", entry["detail"])
            summary = json.loads(case.json.stdout)["summary"]
            self.assertEqual(summary["uncompared_declaration_count"], 1)
            self.assertEqual(case.json.returncode, 0)

    def test_the_vocabulary_for_saying_so_already_exists(self):
        """A declaration the analysis cannot compare says so, with a reason."""
        with _Analysed(".", provider="path-hash") as case:
            entry = case.declaration()
            self.assertEqual(entry["analysis_status"], "legacy-rejected", entry)
            self.assertIsNotNone(entry["detail"])
            summary = json.loads(case.json.stdout)["summary"]
            self.assertEqual(summary["uncompared_declaration_count"], 1)


if __name__ == "__main__":
    unittest.main()
