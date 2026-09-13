"""A sentence that means something, written by someone who can commit.

The diagnostic collector says "DIAGNOSTICS TRUNCATED" when it runs out of its
count or byte budget, and everything downstream reads that line as "there were
more problems than fit". The sentence is also just a string, and a lock file
records per-component error lists as strings, so a repository can write that
exact sentence into one and have it read as the collector's own.

The collector distinguishes its private truncation marker from equal text that
came from a repository. External text remains attributed content, while real
budget exhaustion still propagates through nested collectors.

Covers OBL-OUTPUT-015.
"""

from __future__ import annotations

import json
import unittest

from boundver._lockfile import _generation_errors
from boundver._utils import (
    MAX_DIAGNOSTIC_ITEMS,
    BoundedDiagnosticList,
    DIAGNOSTIC_TRUNCATION_SENTINEL,
)

from tests._parity import run_cli
from tests._scenarios import Scenario

COULD_NOT_CHECK = 2


def _lock_with(*entries) -> dict:
    return {
        "components": {
            name: {"boundary_status": "error", "boundary_errors": list(messages)}
            for name, messages in entries
        }
    }


class CollectorDispositionTests(unittest.TestCase):
    """What the collector does with its own sentence, arriving from outside."""

    def test_an_ordinary_message_is_collected(self):
        """The premise: the collector is not simply refusing everything."""
        collected = BoundedDiagnosticList(["first", "second"])
        self.assertEqual(list(collected), ["first", "second"])
        self.assertFalse(collected.truncated)

    def test_an_imported_sentinel_is_treated_as_ordinary_text(self):
        collected = BoundedDiagnosticList(
            ["first", DIAGNOSTIC_TRUNCATION_SENTINEL, "third"]
        )
        self.assertEqual(
            list(collected), ["first", DIAGNOSTIC_TRUNCATION_SENTINEL, "third"]
        )
        self.assertFalse(collected.truncated)

    def test_a_genuine_budget_truncation_propagates(self):
        genuine = BoundedDiagnosticList(
            [f"message {index}" for index in range(MAX_DIAGNOSTIC_ITEMS + 5)]
        )
        propagated = BoundedDiagnosticList(genuine)
        self.assertTrue(genuine.truncated)
        self.assertTrue(propagated.truncated)
        self.assertEqual(genuine[-1], propagated[-1])
        self.assertEqual(propagated[-1], DIAGNOSTIC_TRUNCATION_SENTINEL)

    def test_a_near_miss_is_not_a_signal(self):
        """The match is exact, so a suffix does not trigger it."""
        for variant in (
            DIAGNOSTIC_TRUNCATION_SENTINEL + " ",
            " " + DIAGNOSTIC_TRUNCATION_SENTINEL,
            DIAGNOSTIC_TRUNCATION_SENTINEL.upper(),
        ):
            with self.subTest(variant=variant[:20]):
                collected = BoundedDiagnosticList([variant, "after"])
                self.assertFalse(collected.truncated)
                self.assertEqual(list(collected), [variant, "after"])


class LockfileFeedTests(unittest.TestCase):
    """The untrusted feed: error lists a lock file records verbatim."""

    def test_real_errors_from_every_component_are_reported(self):
        """The premise: without the sentence, both components are named."""
        errors = _generation_errors(
            _lock_with(("aaa", ["aaa failed"]), ("bbb", ["bbb failed"]))
        )
        self.assertEqual(errors, ["aaa: aaa failed", "bbb: bbb failed"])

    def test_an_injected_sentence_does_not_suppress_the_other_component(self):
        errors = _generation_errors(
            _lock_with(
                ("aaa", [DIAGNOSTIC_TRUNCATION_SENTINEL]), ("bbb", ["bbb failed"])
            )
        )
        self.assertEqual(
            errors,
            [
                f"aaa: {DIAGNOSTIC_TRUNCATION_SENTINEL}",
                "bbb: bbb failed",
            ],
        )

    def test_the_component_that_carried_it_is_named(self):
        errors = _generation_errors(
            _lock_with(("aaa", [DIAGNOSTIC_TRUNCATION_SENTINEL]))
        )
        self.assertEqual(errors, [f"aaa: {DIAGNOSTIC_TRUNCATION_SENTINEL}"])

    def test_the_report_is_still_a_failure(self):
        """Fail-closed: the injection cannot empty the error list."""
        self.assertNotEqual(
            _generation_errors(
                _lock_with(("aaa", [DIAGNOSTIC_TRUNCATION_SENTINEL]))
            ),
            [],
        )


class _Tampered:
    """A repository whose committed lock carries the chosen error lists."""

    def __init__(self, first) -> None:
        self.scene = Scenario()
        self.scene.component("aaa", path="aaa", provider="leaf")
        self.scene.component("bbb", path="bbb", provider="leaf")
        self.scene.file("aaa/main.py", "x\n")
        self.scene.file("bbb/main.py", "y\n")
        self.scene.commit()
        assert run_cli(
            self.scene.root, "generate", "--source", "head"
        ).returncode == 0
        path = self.scene.root / "boundary.lock.json"
        lock = json.loads(path.read_text(encoding="utf-8"))
        for name, messages in (("aaa", first), ("bbb", ["bbb really failed"])):
            lock["components"][name]["boundary_status"] = "error"
            lock["components"][name]["boundary_errors"] = list(messages)
        path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
        self.scene.commit("recorded errors")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def verify(self):
        result = run_cli(self.scene.root, "verify", "--source", "head")
        return result.returncode, result.stdout + result.stderr


class CommittedLockTests(unittest.TestCase):
    """End to end: the string a repository can write is the one that lands."""

    def test_both_recorded_failures_are_reported(self):
        with _Tampered(["aaa really failed"]) as repo:
            code, output = repo.verify()
            self.assertEqual(code, COULD_NOT_CHECK)
            self.assertIn("LOCKED DIGEST ERROR aaa: aaa really failed", output)
            self.assertIn("LOCKED DIGEST ERROR bbb: bbb really failed", output)

    def test_the_injected_sentence_preserves_the_other_failure(self):
        with _Tampered([DIAGNOSTIC_TRUNCATION_SENTINEL]) as repo:
            code, output = repo.verify()
            self.assertEqual(code, COULD_NOT_CHECK)
            self.assertIn("DIAGNOSTICS TRUNCATED", output)
            self.assertIn("aaa", output)
            self.assertIn("bbb really failed", output)

    def test_the_operation_still_refuses_to_pass(self):
        """The half that holds: nothing here turns a failure into a pass."""
        with _Tampered([DIAGNOSTIC_TRUNCATION_SENTINEL]) as repo:
            self.assertEqual(repo.verify()[0], COULD_NOT_CHECK)


if __name__ == "__main__":
    unittest.main()
