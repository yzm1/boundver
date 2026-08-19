"""Focused mutation-safety and identity regressions for verify baselines."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from boundver import _baseline


class BaselineAncestorSafetyTests(unittest.TestCase):
    def _swap_parent(
        self,
        parent: Path,
        parked: Path,
        outside: Path,
    ) -> bool:
        """Replace *parent* with a competing outside directory when permitted."""
        try:
            parent.replace(parked)
        except OSError:
            return False
        try:
            outside.replace(parent)
        except OSError:
            parked.replace(parent)
            raise
        return True

    @staticmethod
    def _restore_competing_parent(parent: Path, outside: Path, swapped: bool) -> None:
        if swapped:
            parent.replace(outside)

    def test_create_parent_swap_cannot_redirect_publication_outside_repo(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "repo"
            parent = root / "output"
            parked = root / "original-output"
            outside = base / "outside"
            parent.mkdir(parents=True)
            outside.mkdir()
            target = parent / "debt.json"
            swapped = False
            real_link = _baseline._MutationDirectory.link

            def swap_before_publish(directory, source, destination):
                nonlocal swapped
                swapped = self._swap_parent(parent, parked, outside)
                return real_link(directory, source, destination)

            try:
                with patch.object(
                    _baseline._MutationDirectory,
                    "link",
                    new=swap_before_publish,
                ):
                    _baseline.write_baseline_create_only(
                        target,
                        "reviewed debt\n",
                        repo_root=root,
                    )
            finally:
                self._restore_competing_parent(parent, outside, swapped)

            self.assertTrue(swapped, "the publication-time parent swap must execute")
            self.assertFalse((outside / target.name).exists())
            safe_target = (parked if swapped else parent) / target.name
            self.assertEqual(safe_target.read_text(), "reviewed debt\n")
            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(list(safe_target.parent.glob(".debt.json.*.tmp")), [])

    def test_update_parent_swap_cannot_redirect_any_mutation_outside_repo(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "repo"
            parent = root / "output"
            parked = root / "original-output"
            outside = base / "outside"
            parent.mkdir(parents=True)
            outside.mkdir()
            target = parent / "debt.json"
            _baseline.write_baseline_create_only(
                target,
                "old debt\n",
                repo_root=root,
            )
            expected = target.read_bytes()
            swapped = False
            real_replace = _baseline._MutationDirectory.replace

            def swap_before_claim(directory, source, destination):
                nonlocal swapped
                swapped = self._swap_parent(parent, parked, outside)
                return real_replace(directory, source, destination)

            try:
                with patch.object(
                    _baseline._MutationDirectory,
                    "replace",
                    new=swap_before_claim,
                ):
                    _baseline.replace_baseline_if_unchanged(
                        target,
                        "reduced debt\n",
                        expected,
                        repo_root=root,
                    )
            finally:
                self._restore_competing_parent(parent, outside, swapped)

            self.assertTrue(swapped, "the claim-time parent swap must execute")
            self.assertFalse((outside / target.name).exists())
            safe_target = (parked if swapped else parent) / target.name
            self.assertEqual(safe_target.read_text(), "reduced debt\n")
            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(list(safe_target.parent.glob(".debt.json.*.tmp")), [])
            self.assertEqual(list(safe_target.parent.glob(".debt.json.*.claim")), [])
            self.assertFalse(
                (safe_target.parent / ".debt.json.boundver-update.lock").exists()
            )

    def test_update_interrupt_during_lock_sync_removes_only_its_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "debt.json"
            _baseline.write_baseline_create_only(
                target,
                "old debt\n",
                repo_root=root,
            )
            expected = target.read_bytes()
            lock_path = root / ".debt.json.boundver-update.lock"

            with patch.object(
                _baseline.os,
                "fsync",
                side_effect=KeyboardInterrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    _baseline.replace_baseline_if_unchanged(
                        target,
                        "reduced debt\n",
                        expected,
                        repo_root=root,
                    )

            self.assertEqual(target.read_bytes(), expected)
            self.assertFalse(lock_path.exists())
            self.assertEqual(list(root.glob(".debt.json.*.tmp")), [])
            self.assertEqual(list(root.glob(".debt.json.*.claim")), [])

    def test_update_interrupt_preserves_a_competing_lock_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "debt.json"
            _baseline.write_baseline_create_only(
                target,
                "old debt\n",
                repo_root=root,
            )
            expected = target.read_bytes()
            lock_path = root / ".debt.json.boundver-update.lock"
            competing = b"competing update lock\n"

            def replace_lock_and_interrupt(_fd):
                lock_path.unlink()
                lock_path.write_bytes(competing)
                raise KeyboardInterrupt

            with patch.object(
                _baseline.os,
                "fsync",
                side_effect=replace_lock_and_interrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    _baseline.replace_baseline_if_unchanged(
                        target,
                        "reduced debt\n",
                        expected,
                        repo_root=root,
                    )

            self.assertEqual(target.read_bytes(), expected)
            self.assertEqual(lock_path.read_bytes(), competing)


class BaselineIdentityTests(unittest.TestCase):
    def test_component_and_slice_identities_allow_newlines_in_names(self):
        component = "payments\napi"
        component_identity = _baseline.violation_identity(
            f"MISMATCH {component}.exact: changed"
        )
        slice_name = "release\ntrain"
        slice_identity = _baseline.violation_identity(
            f"SLICE MISMATCH {slice_name}.boundary: changed"
        )

        self.assertEqual(component_identity["subject"], component)
        self.assertEqual(component_identity["kind"], "component-facet")
        self.assertEqual(slice_identity["subject"], slice_name)
        self.assertEqual(slice_identity["kind"], "slice-facet")

    def test_newline_component_identity_binds_its_consumer_diagnostic(self):
        component = "payments\napi"
        identified = _baseline._issues_with_identities(
            [
                f"MISMATCH {component}.compat: changed",
                f"AFFECTED CONSUMERS {component}: web",
            ]
        )

        self.assertEqual(identified[0][1]["subject"], component)
        self.assertEqual(identified[1][1]["subject"], component)
        self.assertEqual(identified[1][1]["kind"], "affected-consumers")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
