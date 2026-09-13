"""Two renderings of one run, and a flag that must not reach the baseline.

A command offering both a text and a JSON view is making a promise: the two
describe the same run. Comparing them needs care, because the JSON carries
context the text does not print. A policy block listing every component is not
a finding, and treating it as one makes a parity test fail for the wrong
reason.

Covers OBL-LOCKFILE-008 and OBL-LOCKFILE-011.
"""

from __future__ import annotations

import json
import re
import unittest

from tests._parity import run_cli
from tests._scenarios import Scenario

MISMATCH = re.compile(r"^\s*MISMATCH (\S+?)\.(\w+):", re.MULTILINE)


def _mismatched(text: str) -> set:
    """The (component, facet) pairs a rendering reports as mismatched."""
    return set(MISMATCH.findall(text))


class _Drifted:
    def __init__(self) -> None:
        scene = Scenario()
        scene.component("svc", path="svc", boundary=["api"], consumers=["sdk"])
        scene.component("sdk", path="sdk", provider="leaf")
        scene.component("other", path="other", provider="leaf")
        scene.slice("all", mode="exact", components=["other", "sdk", "svc"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("sdk/index.ts", "export const x = 1;\n")
        scene.file("other/x.py", "x = 1\n")
        scene.commit()
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("lock")
        self.clean_ref = scene.head()
        scene.append_line("svc/api/v1.yaml", "drift\n")
        scene.commit("drift")
        self.scene = scene

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def both(self, *arguments):
        text = run_cli(self.scene.root, *arguments)
        data = run_cli(self.scene.root, *arguments, "--format", "json")
        return text, data


class TextJsonParityTests(unittest.TestCase):
    """OBL-LOCKFILE-011: the two views must describe the same run."""

    COMMANDS = (
        ("status", ["status"]),
        ("verify", ["verify", "--source", "head"]),
        ("why", ["why", "svc", "--source", "head"]),
        ("slice", ["slice", "all"]),
        ("discover", ["discover"]),
    )

    def test_the_exit_code_does_not_depend_on_the_view(self):
        with _Drifted() as repo:
            for label, arguments in self.COMMANDS:
                with self.subTest(command=label):
                    text, data = repo.both(*arguments)
                    self.assertEqual(text.returncode, data.returncode,
                                     (text.stderr or "") + (data.stderr or ""))

    def test_the_two_views_count_the_same_issues(self):
        """The text states its own count, which is the honest thing to compare.

        The two do not render findings the same way. The text prints two
        MISMATCH lines and a Consumer impact block; the JSON lists three
        issues, one of them AFFECTED CONSUMERS. Both describe three findings,
        and the text says so in its header, so the count is the claim rather
        than the line shapes.
        """
        with _Drifted() as repo:
            text, data = repo.both("verify", "--source", "head")
            stated = re.search(r"\((\d+) issues?\)", text.stdout)
            self.assertIsNotNone(stated, text.stdout)
            document = json.loads(data.stdout)
            self.assertEqual(int(stated.group(1)), len(document["issues"]))

    def test_the_two_views_name_the_same_components_and_facets(self):
        with _Drifted() as repo:
            text, data = repo.both("verify", "--source", "head")
            document = json.loads(data.stdout)
            self.assertEqual(
                _mismatched(text.stdout), _mismatched("\n".join(document["issues"]))
            )

    def test_the_two_views_name_the_same_affected_consumers(self):
        """Rendered as a block in one and as an issue line in the other."""
        with _Drifted() as repo:
            text, data = repo.both("verify", "--source", "head")
            document = json.loads(data.stdout)
            from_json = {
                entry["component"]: set(entry.get("components", []))
                for entry in document.get("consumer_impact", [])
            }
            self.assertEqual(from_json, {"svc": {"sdk"}})
            self.assertEqual(
                {entry["component"]: set(entry["facets"])
                 for entry in document["consumer_impact"]},
                {"svc": {"boundary"}},
            )
            self.assertIn("Consumer impact:", text.stdout)
            self.assertIn("svc", text.stdout)
            self.assertIn("sdk", text.stdout)

    def test_the_findings_are_not_empty(self):
        """Without this the comparisons above could hold on two empty sets."""
        with _Drifted() as repo:
            text, _data = repo.both("verify", "--source", "head")
            self.assertEqual(_mismatched(text.stdout), {("svc", "boundary"), ("svc", "exact")})

    def test_a_clean_run_reports_nothing_in_either_view(self):
        with _Drifted() as repo:
            repo.scene.git("checkout", "-q", repo.clean_ref, "--", ".")
            repo.scene.commit("restore")
            text, data = repo.both("verify", "--source", "head")
            self.assertEqual(text.returncode, 0, text.stdout)
            self.assertEqual(_mismatched(text.stdout), set())
            self.assertEqual(json.loads(data.stdout)["issues"], [])

    def test_slice_names_the_same_members_in_both_views(self):
        with _Drifted() as repo:
            text, data = repo.both("slice", "all")
            members = {
                entry["name"] for entry in json.loads(data.stdout)["components"]
            }
            self.assertEqual(members, {"other", "sdk", "svc"})
            for name in members:
                self.assertIn(name, text.stdout)


class ChangedFromBaselineScopeTests(unittest.TestCase):
    """OBL-LOCKFILE-008: the flag narrows a report, not a baseline."""

    def _captured(self, repo: _Drifted, *extra) -> dict:
        result = run_cli(
            repo.scene.root, "verify", "--source", "head", *extra,
            "--write-baseline", "b.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(
            (repo.scene.root / "b.json").read_text(encoding="utf-8")
        )
        repo.scene.git("add", "--all")
        repo.scene.git("commit", "-m", "baseline")
        return document

    def test_the_captured_filter_is_empty(self):
        """The mechanism behind the requirement, asserted directly."""
        with _Drifted() as repo:
            document = self._captured(repo, "--changed-from", repo.clean_ref)
            self.assertEqual(document["components_filter"], [])
            self.assertTrue(document["violations"])

    def test_a_baseline_captured_under_the_flag_applies_without_it(self):
        with _Drifted() as repo:
            self._captured(repo, "--changed-from", repo.clean_ref)
            for label, extra in (
                ("no flag", []),
                ("the same ref", ["--changed-from", repo.clean_ref]),
                ("a different ref", ["--changed-from", "HEAD"]),
            ):
                with self.subTest(applied=label):
                    applied = run_cli(
                        repo.scene.root, "verify", "--source", "head",
                        "--baseline", "b.json", *extra,
                    )
                    self.assertEqual(applied.returncode, 0, applied.stderr)

    def test_a_baseline_captured_without_the_flag_applies_under_it(self):
        """The other direction, so the independence is not one-way."""
        with _Drifted() as repo:
            self._captured(repo)
            applied = run_cli(
                repo.scene.root, "verify", "--source", "head",
                "--baseline", "b.json", "--changed-from", repo.clean_ref,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)


if __name__ == "__main__":
    unittest.main()
