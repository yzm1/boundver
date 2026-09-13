"""Where a team's baseline bytes are when an update cannot finish.

Replacing a baseline is a sequence of individually atomic steps, and the file
being replaced is a record a team decided to keep. So the contract is not only
that the update either happens or does not: at every point where the sequence
can be interrupted, the target has to hold one of the two exact byte strings,
and in the three cases where the bytes can only be kept in a sidecar, the
diagnostic has to name that sidecar exactly. A message that says bytes were
preserved without saying where is not a recovery instruction.

Those three cases all need two things to go wrong at once - a competing writer
between the pre-check and the claim, and then a failing link - which is why
they are reached here by injecting both rather than by racing for them.

Covers OBL-BASELINE-003.
"""

from __future__ import annotations

import unittest
from typing import List, Optional
from unittest import mock

from boundver import _baseline
from boundver._baseline import BaselineError, replace_baseline_if_unchanged

from tests._scenarios import Scenario

REVIEWED = b'{"reviewed": 1}\n'
NEW = b'{"new": 2}\n'

#: What a competing writer puts at the target between the pre-check and the
#: claim, and what a later one puts there while restoration is under way.
RACED = b'{"raced": 3}\n'
LATE = b'{"late": 4}\n'


class _Attempt:
    """One update, with chosen failures injected into the directory layer."""

    def __init__(
        self,
        scene: Scenario,
        *,
        race: bool = False,
        claim_link_error: Optional[BaseException] = None,
        publish_link_error: Optional[BaseException] = None,
        late_writer: bool = False,
        expected: bytes = REVIEWED,
    ) -> None:
        self.root = scene.root
        self.target = scene.root / "base.json"
        self.target.write_bytes(REVIEWED)
        self.error: Optional[BaselineError] = None
        real_replace = _baseline._MutationDirectory.replace
        real_link = _baseline._MutationDirectory.link

        def replace(inner, source, dest):
            real_replace(inner, source, dest)
            if race:
                (self.root / dest).write_bytes(RACED)

        def link(inner, source, dest):
            failure = (
                claim_link_error if source.endswith(".claim")
                else publish_link_error
            )
            if failure is not None:
                if late_writer:
                    (self.root / dest).write_bytes(LATE)
                raise failure
            return real_link(inner, source, dest)

        with mock.patch.object(_baseline._MutationDirectory, "replace", replace), \
                mock.patch.object(_baseline._MutationDirectory, "link", link):
            try:
                replace_baseline_if_unchanged(
                    self.target,
                    NEW.decode("utf-8"),
                    expected,
                    repo_root=self.root,
                )
            except BaselineError as exc:
                self.error = exc

    @property
    def message(self) -> str:
        assert self.error is not None, "expected the update to be refused"
        return str(self.error)

    @property
    def claims(self) -> List[str]:
        return sorted(
            entry.name for entry in self.root.iterdir() if ".claim" in entry.name
        )

    @property
    def sidecar(self) -> str:
        claims = self.claims
        assert len(claims) == 1, claims
        return claims[0]

    def sidecar_bytes(self) -> bytes:
        return (self.root / self.sidecar).read_bytes()

    def target_bytes(self) -> Optional[bytes]:
        return self.target.read_bytes() if self.target.exists() else None


class UninterruptedTests(unittest.TestCase):
    """The premise: nothing is preserved when nothing goes wrong."""

    def test_a_clean_update_publishes_the_new_bytes(self):
        with Scenario() as scene:
            attempt = _Attempt(scene)
            self.assertIsNone(attempt.error)
            self.assertEqual(attempt.target_bytes(), NEW)
            self.assertEqual(attempt.claims, [])

    def test_a_stale_expectation_is_refused_before_anything_moves(self):
        with Scenario() as scene:
            attempt = _Attempt(scene, expected=b"something else\n")
            self.assertIn("changed before update", attempt.message)
            self.assertEqual(attempt.target_bytes(), REVIEWED)
            self.assertEqual(attempt.claims, [])


class InterruptedTargetTests(unittest.TestCase):
    """The first half: the target holds one of the two byte strings."""

    def test_a_competing_target_leaves_the_competitor_in_place(self):
        """The claim matched the reviewed bytes, so it is safe to discard."""
        with Scenario() as scene:
            attempt = _Attempt(
                scene, publish_link_error=FileExistsError(), late_writer=True
            )
            self.assertIn("the competing target was preserved", attempt.message)
            self.assertEqual(attempt.target_bytes(), LATE)
            self.assertEqual(attempt.claims, [])

    def test_a_failed_publication_restores_the_reviewed_bytes(self):
        with Scenario() as scene:
            attempt = _Attempt(scene, publish_link_error=OSError("no publish"))
            self.assertIn("cannot safely publish", attempt.message)
            self.assertEqual(attempt.target_bytes(), REVIEWED)
            self.assertEqual(attempt.claims, [])

    def test_a_raced_baseline_is_restored_rather_than_overwritten(self):
        with Scenario() as scene:
            attempt = _Attempt(scene, race=True)
            self.assertIn("changed during update", attempt.message)
            self.assertEqual(attempt.target_bytes(), RACED)
            self.assertEqual(attempt.claims, [])


class PreservedSidecarTests(unittest.TestCase):
    """The second half: the diagnostic names the file holding the bytes."""

    def test_an_occupied_target_names_the_sidecar_holding_the_earlier_bytes(self):
        with Scenario() as scene:
            attempt = _Attempt(
                scene,
                race=True,
                claim_link_error=FileExistsError(),
                late_writer=True,
            )
            self.assertIn("the current target was preserved", attempt.message)
            self.assertIn(attempt.sidecar, attempt.message)
            self.assertEqual(attempt.sidecar_bytes(), RACED)
            self.assertEqual(attempt.target_bytes(), LATE)

    def test_a_failed_restore_names_the_sidecar_to_recover_from(self):
        with Scenario() as scene:
            attempt = _Attempt(
                scene, race=True, claim_link_error=OSError("no restore")
            )
            self.assertIn("could not be restored", attempt.message)
            self.assertIn(attempt.sidecar, attempt.message)
            self.assertEqual(attempt.sidecar_bytes(), RACED)
            self.assertIsNone(attempt.target_bytes())

    def test_a_failed_publish_and_restore_names_the_reviewed_bytes(self):
        with Scenario() as scene:
            attempt = _Attempt(
                scene,
                claim_link_error=OSError("no restore"),
                publish_link_error=OSError("no publish"),
            )
            self.assertIn("cannot publish or restore", attempt.message)
            self.assertIn(attempt.sidecar, attempt.message)
            self.assertEqual(attempt.sidecar_bytes(), REVIEWED)
            self.assertIsNone(attempt.target_bytes())

    def test_the_named_sidecar_is_the_one_that_exists(self):
        """Not a pattern, a placeholder, or a name that was cleaned up."""
        cases = (
            dict(race=True, claim_link_error=FileExistsError(), late_writer=True),
            dict(race=True, claim_link_error=OSError("no restore")),
            dict(
                claim_link_error=OSError("no restore"),
                publish_link_error=OSError("no publish"),
            ),
        )
        for index, case in enumerate(cases):
            with self.subTest(branch=index):
                with Scenario() as scene:
                    attempt = _Attempt(scene, **case)
                    named = [
                        word.rstrip(".,;")
                        for word in attempt.message.split()
                        if ".claim" in word
                    ]
                    self.assertEqual(named, [attempt.sidecar])
                    self.assertTrue((attempt.root / named[0]).is_file())

    def test_no_lock_or_temporary_file_is_left_behind(self):
        """Only the claim survives, so the sidecar the message names is clear."""
        with Scenario() as scene:
            attempt = _Attempt(
                scene, race=True, claim_link_error=OSError("no restore")
            )
            leftovers = sorted(
                entry.name
                for entry in attempt.root.iterdir()
                if entry.name.startswith(".base.json.")
            )
            self.assertEqual(leftovers, [attempt.sidecar])


if __name__ == "__main__":
    unittest.main()
