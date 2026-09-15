"""What the command line accepts, and whether its two views tell one story.

Three questions that share a shape. A range expression has exactly one legal
spelling and several near-misses, each of which must be refused rather than
guessed at. A flag that limits a report must limit only the report. And a
command offering a human view and a machine view must not disagree about what
happened - not on the verdict, not on the exit code, and not on which files
moved.

Covers OBL-CLI-001, OBL-CLI-002, OBL-CLI-003 and OBL-REVIEW-002.
"""

from __future__ import annotations

import json
import unittest

from boundver._completions import _COMMAND_POSITIONALS, _COMMANDS
from boundver.core import build_parser

from tests._parity import run_cli
from tests._scenarios import Scenario

DRIFT = 1
COULD_NOT_CHECK = 2

#: The message each rejected endpoint spelling must produce.
ENDPOINT_REFUSALS = {
    "three dots": ("{base}...{target}", "must use exactly BASE..TARGET"),
    "two ranges": ("{base}..{target}..{target}", "must use exactly BASE..TARGET"),
    "empty left": ("..{target}", "must name both BASE and TARGET"),
    "empty right": ("{base}..", "must name both BASE and TARGET"),
}

#: Spellings that name no complete range at all.
INCOMPLETE = ("--base only", "--target only", "neither")


def _lock(scene, message: str) -> None:
    result = run_cli(scene.root, "generate", "--source", "head")
    assert result.returncode == 0, result.stderr
    scene.commit(message)


class _Range:
    """A repository with a reviewable range."""

    def __init__(self) -> None:
        scene = Scenario()
        scene.component("svc", path="svc", boundary=["api"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.commit()
        _lock(scene, "lock")
        self.base = scene.head()
        scene.append_line("svc/api/v1.yaml", "change\n")
        scene.commit("edit")
        _lock(scene, "relock")
        self.scene = scene
        self.target = scene.head()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def review(self, *arguments):
        return run_cli(self.scene.root, "review", *arguments)


class ReviewEndpointTests(unittest.TestCase):
    """OBL-REVIEW-002: one spelling accepted, six refused by name."""

    def test_the_only_accepted_spellings_work(self):
        """The premise: both legal forms succeed on the same range."""
        with _Range() as review:
            self.assertEqual(
                review.review(f"{review.base}..{review.target}").returncode, 0
            )
            self.assertEqual(
                review.review(
                    "--base", review.base, "--target", review.target
                ).returncode,
                0,
            )

    def test_every_malformed_range_is_refused_by_name(self):
        with _Range() as review:
            for label, (template, message) in ENDPOINT_REFUSALS.items():
                with self.subTest(spelling=label):
                    spelling = template.format(
                        base=review.base, target=review.target
                    )
                    result = review.review(spelling)
                    self.assertEqual(result.returncode, COULD_NOT_CHECK, result.stdout)
                    self.assertIn(message, result.stderr)

    def test_a_positional_range_cannot_be_combined_with_a_flag(self):
        with _Range() as review:
            spelling = f"{review.base}..{review.target}"
            for flag, value in (("--base", review.base), ("--target", review.target)):
                with self.subTest(flag=flag):
                    result = review.review(spelling, flag, value)
                    self.assertEqual(result.returncode, COULD_NOT_CHECK)
                    self.assertIn("not both", result.stderr)

    def test_an_incomplete_selection_is_refused(self):
        with _Range() as review:
            arguments = {
                "--base only": ("--base", review.base),
                "--target only": ("--target", review.target),
                "neither": (),
            }
            for label in INCOMPLETE:
                with self.subTest(spelling=label):
                    result = review.review(*arguments[label])
                    self.assertEqual(result.returncode, COULD_NOT_CHECK)
                    self.assertIn(
                        "requires BASE..TARGET or both", result.stderr
                    )


class FailFastTests(unittest.TestCase):
    """OBL-CLI-002: report-limiting, and nothing else."""

    def _drifted(self):
        scene = Scenario()
        scene.component("svc", path="svc", boundary=["api"], verify_facets=["exact"])
        scene.component("other", path="other", provider="leaf")
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("svc/impl.txt", "one\n")
        scene.file("other/index.ts", "export const x = 1;\n")
        scene.commit()
        _lock(scene, "lock")
        scene.append_line("svc/api/v1.yaml", "change\n")
        scene.append_line("svc/impl.txt", "two\n")
        scene.append_line("other/index.ts", "// edit\n")
        scene.commit("drift")
        return scene

    def _verified(self, scene, *extra):
        result = run_cli(
            scene.root, "verify", "--source", "head", "--format", "json", *extra
        )
        return result, json.loads(result.stdout)

    def test_the_exit_code_is_the_same(self):
        scene = self._drifted()
        try:
            plain, _ = self._verified(scene)
            fast, _ = self._verified(scene, "--fail-fast")
            self.assertEqual(plain.returncode, fast.returncode)
            self.assertEqual(plain.returncode, DRIFT)
        finally:
            scene.close()

    def test_the_issue_list_is_cut_to_one(self):
        scene = self._drifted()
        try:
            _plain, full = self._verified(scene)
            _fast, limited = self._verified(scene, "--fail-fast")
            self.assertGreater(len(full["issues"]), 1)
            self.assertEqual(len(limited["issues"]), 1)
            self.assertEqual(limited["issues"], full["issues"][:1])
        finally:
            scene.close()

    def test_the_observations_are_not_cut(self):
        """The clause the gap named: non-gating findings survive intact."""
        scene = self._drifted()
        try:
            _plain, full = self._verified(scene)
            _fast, limited = self._verified(scene, "--fail-fast")
            self.assertTrue(full["observations"])
            self.assertEqual(limited["observations"], full["observations"])
        finally:
            scene.close()

    def test_a_clean_repository_is_unaffected(self):
        """The contrast: the flag changes nothing when there is nothing to cut."""
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/main.py", "x\n")
            scene.commit()
            _lock(scene, "lock")
            plain, first = self._verified(scene)
            fast, second = self._verified(scene, "--fail-fast")
            self.assertEqual((plain.returncode, fast.returncode), (0, 0))
            self.assertEqual(first["issues"], second["issues"])


class WhyViewAgreementTests(unittest.TestCase):
    """OBL-CLI-003: one run, two renderings, one story."""

    def _both(self, drift: bool):
        with Scenario() as scene:
            scene.component("svc", path="svc", boundary=["api"])
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.file("svc/impl.txt", "one\n")
            scene.commit()
            _lock(scene, "lock")
            if drift:
                scene.append_line("svc/impl.txt", "two\n")
                scene.append_line("svc/api/v1.yaml", "change\n")
                scene.commit("edit")
            text = run_cli(scene.root, "why", "svc", "--source", "head")
            document = run_cli(
                scene.root, "why", "svc", "--source", "head", "--format", "json"
            )
            return text, json.loads(document.stdout), document.returncode

    def _listed_files(self, text: str) -> set:
        listed = set()
        collecting = False
        for line in text.splitlines():
            if line.startswith("Modified files under "):
                collecting = True
                continue
            if collecting:
                if not line.strip():
                    break
                listed.add(line.split()[-1])
        return listed

    def test_the_two_views_agree_on_a_clean_component(self):
        text, document, code = self._both(drift=False)
        self.assertEqual((text.returncode, code), (0, 0))
        self.assertFalse(document["drifted"])
        self.assertIn("Status: UP TO DATE", text.stdout)

    def test_the_two_views_agree_on_a_drifted_component(self):
        text, document, code = self._both(drift=True)
        self.assertEqual((text.returncode, code), (DRIFT, DRIFT))
        self.assertTrue(document["drifted"])
        self.assertIn("Status: DRIFTED", text.stdout)

    def test_the_changed_file_sets_are_equal(self):
        text, document, _code = self._both(drift=True)
        self.assertEqual(
            self._listed_files(text.stdout),
            {entry["path"] for entry in document["changed_files"]},
        )
        self.assertEqual(len(document["changed_files"]), 2)

    def test_a_clean_component_lists_no_files_in_either_view(self):
        text, document, _code = self._both(drift=False)
        self.assertEqual(self._listed_files(text.stdout), set())
        self.assertEqual(document["changed_files"], [])


def _live_positionals() -> dict:
    parser = build_parser(version="0", epilog="")
    for action in parser._actions:
        if hasattr(action, "choices") and isinstance(action.choices, dict):
            return {
                name: tuple(
                    argument.dest
                    for argument in subparser._actions
                    if not argument.option_strings
                )
                for name, subparser in action.choices.items()
            }
    raise AssertionError("the parser exposes no subcommands")


class CompletionParityTests(unittest.TestCase):
    """OBL-CLI-001: the completion tables against the live parser."""

    def test_the_command_sets_are_equal(self):
        self.assertEqual(set(_live_positionals()), set(_COMMANDS))

    def test_every_command_declares_the_right_number_of_positionals(self):
        live = _live_positionals()
        for command, dests in sorted(live.items()):
            with self.subTest(command=command):
                declared = _COMMAND_POSITIONALS.get(command, ())
                self.assertEqual(len(declared), len(dests))

    def test_a_declared_label_names_the_positional_it_sits_beside(self):
        """Order, where the label is derived from the dest.

        The table stores display labels rather than dests - `review` shows
        'BASE..TARGET' for a dest called `range` - so identity cannot be
        compared everywhere. Where the label does begin with the dest, the
        pairing is asserted positionally, which covers every command that has
        more than one positional and could therefore have them swapped.
        """
        live = _live_positionals()
        checked = 0
        for command, dests in sorted(live.items()):
            declared = _COMMAND_POSITIONALS.get(command, ())
            for index, (label, _completion) in enumerate(declared):
                if label.split(" ")[0] == dests[index]:
                    checked += 1
                    continue
                self.assertEqual(len(declared), 1, (command, label, dests))
        self.assertGreater(checked, 0)

    def test_every_command_with_two_positionals_is_covered_by_that_rule(self):
        """So the rule above is not vacuous where a swap is possible."""
        live = _live_positionals()
        multiple = {
            command: dests for command, dests in live.items() if len(dests) > 1
        }
        self.assertTrue(multiple)
        for command, dests in multiple.items():
            with self.subTest(command=command):
                declared = _COMMAND_POSITIONALS[command]
                for index, (label, _completion) in enumerate(declared):
                    self.assertEqual(label.split(" ")[0], dests[index])


if __name__ == "__main__":
    unittest.main()
