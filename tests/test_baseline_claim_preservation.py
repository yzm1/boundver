"""Where a team's baseline bytes are when an update cannot finish.

Baseline publication uses one native atomic replace-with-backup operation. The
canonical path therefore always names a complete file, while the displaced
bytes remain available until the compare-and-publish check succeeds. These
tests inject competitors and failures at each native operation so every unique
writer is either restored to the canonical path or named in a sidecar.

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
RACED = b'{"raced": 3}\n'
LATE = b'{"late": 4}\n'


class _Attempt:
    """One update, with chosen failures injected into the native operation."""

    def __init__(
        self,
        scene: Scenario,
        *,
        race: bool = False,
        late_writer_on_call: Optional[int] = None,
        fail_call: Optional[int] = None,
        interrupt_after_call: Optional[int] = None,
        expected: bytes = REVIEWED,
    ) -> None:
        self.root = scene.root
        self.target = scene.root / "base.json"
        self.target.write_bytes(REVIEWED)
        self.error: Optional[BaselineError] = None
        self.interrupted = False
        self.calls = 0
        real_publish = _baseline._MutationDirectory.replace_preserving_target

        def publish(inner, replacement, target, backup):
            self.calls += 1
            if self.calls == 1 and race:
                (self.root / target).write_bytes(RACED)
            if self.calls == late_writer_on_call:
                (self.root / target).write_bytes(LATE)
            if self.calls == fail_call:
                raise OSError(f"native operation {self.calls} failed")
            displaced = real_publish(inner, replacement, target, backup)
            if self.calls == interrupt_after_call:
                raise KeyboardInterrupt(f"interrupted after operation {self.calls}")
            return displaced

        with mock.patch.object(
            _baseline._MutationDirectory,
            "replace_preserving_target",
            publish,
        ):
            try:
                replace_baseline_if_unchanged(
                    self.target,
                    NEW.decode("utf-8"),
                    expected,
                    repo_root=self.root,
                )
            except BaselineError as exc:
                self.error = exc
            except KeyboardInterrupt:
                self.interrupted = True

    @property
    def message(self) -> str:
        assert self.error is not None, "expected the update to be refused"
        return str(self.error)

    @property
    def sidecars(self) -> List[str]:
        return sorted(
            entry.name
            for entry in self.root.iterdir()
            if entry.name.startswith(".base.json.")
            and not entry.name.endswith(".boundver-update.lock")
        )

    @property
    def sidecar(self) -> str:
        sidecars = self.sidecars
        assert len(sidecars) == 1, sidecars
        return sidecars[0]

    def sidecar_bytes(self) -> bytes:
        return (self.root / self.sidecar).read_bytes()

    def target_bytes(self) -> Optional[bytes]:
        return self.target.read_bytes() if self.target.exists() else None


class UninterruptedTests(unittest.TestCase):
    def test_a_clean_update_publishes_the_new_bytes(self):
        with Scenario() as scene:
            attempt = _Attempt(scene)
            self.assertIsNone(attempt.error)
            self.assertEqual(attempt.target_bytes(), NEW)
            self.assertEqual(attempt.sidecars, [])

    def test_a_stale_expectation_is_refused_before_anything_moves(self):
        with Scenario() as scene:
            attempt = _Attempt(scene, expected=b"something else\n")
            self.assertIn("changed before update", attempt.message)
            self.assertEqual(attempt.target_bytes(), REVIEWED)
            self.assertEqual(attempt.sidecars, [])


class AtomicPublicationTests(unittest.TestCase):
    def test_a_competing_target_is_restored_without_an_absent_path(self):
        with Scenario() as scene:
            attempt = _Attempt(scene, race=True)
            self.assertIn("competing bytes were restored", attempt.message)
            self.assertEqual(attempt.target_bytes(), RACED)
            self.assertEqual(attempt.sidecars, [])

    def test_a_failed_first_publication_leaves_reviewed_bytes_in_place(self):
        with Scenario() as scene:
            attempt = _Attempt(scene, fail_call=1)
            self.assertIn("cannot safely update", attempt.message)
            self.assertEqual(attempt.target_bytes(), REVIEWED)
            self.assertEqual(attempt.sidecars, [])

    def test_interrupt_after_publication_leaves_new_and_old_bytes_reachable(self):
        with Scenario() as scene:
            attempt = _Attempt(scene, interrupt_after_call=1)
            self.assertTrue(attempt.interrupted)
            self.assertEqual(attempt.target_bytes(), NEW)
            self.assertEqual(attempt.sidecar_bytes(), REVIEWED)

    def test_a_failed_rollback_names_the_competing_bytes(self):
        with Scenario() as scene:
            attempt = _Attempt(scene, race=True, fail_call=2)
            self.assertIn("could not be restored", attempt.message)
            self.assertIn(attempt.sidecar, attempt.message)
            self.assertEqual(attempt.target_bytes(), NEW)
            self.assertEqual(attempt.sidecar_bytes(), RACED)

    def test_interrupt_after_rollback_names_the_actual_new_bytes_sidecar(self):
        with Scenario() as scene:
            attempt = _Attempt(
                scene,
                race=True,
                interrupt_after_call=2,
            )
            self.assertIn("could not be restored", attempt.message)
            self.assertIn(attempt.sidecar, attempt.message)
            self.assertEqual(attempt.target_bytes(), RACED)
            self.assertEqual(attempt.sidecar_bytes(), NEW)

    def test_a_late_in_place_writer_is_restored_and_earlier_bytes_are_named(self):
        with Scenario() as scene:
            attempt = _Attempt(scene, race=True, late_writer_on_call=2)
            self.assertIn("latest target was preserved", attempt.message)
            self.assertIn(attempt.sidecar, attempt.message)
            self.assertEqual(attempt.target_bytes(), LATE)
            self.assertEqual(attempt.sidecar_bytes(), RACED)

    def test_interrupted_late_writer_recovery_keeps_both_competitors(self):
        with Scenario() as scene:
            attempt = _Attempt(
                scene,
                race=True,
                late_writer_on_call=2,
                fail_call=3,
            )
            self.assertIn("recovery was interrupted", attempt.message)
            self.assertEqual(attempt.target_bytes(), RACED)
            self.assertEqual(attempt.sidecar_bytes(), LATE)

    def test_only_the_diagnostic_sidecar_survives_an_ambiguous_failure(self):
        with Scenario() as scene:
            attempt = _Attempt(scene, race=True, fail_call=2)
            self.assertEqual(attempt.sidecars, [attempt.sidecar])
            self.assertIn(attempt.sidecar, attempt.message)
            self.assertFalse(
                (attempt.root / ".base.json.boundver-update.lock").exists()
            )


if __name__ == "__main__":
    unittest.main()
