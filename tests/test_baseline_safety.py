"""Focused mutation-safety and identity regressions for verify baselines."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from boundver import _baseline, core


class CreateOnlyPublicationTests(unittest.TestCase):
    """The no-replace guarantee itself, not the friendly check in front of it.

    Two layers refuse an existing baseline. The CLI checks the path first
    and reports a helpful error; write_baseline_create_only publishes
    through a same-directory hard link, which is what makes the refusal
    atomic and closes the ancestor-swap gap the comment there describes.
    Only the first was reachable from any test, so swallowing the second
    entirely changed nothing that ran (MUT-HASHING-217). Deleting the
    atomicity would have stayed green.

    Calling the writer directly skips the pre-check, which is the only way
    to put the guarantee itself to a test.
    """

    FIRST = '{"baseline": "first"}\n'
    SECOND = '{"baseline": "second"}\n'

    def test_a_second_publication_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "debt.json"
            _baseline.write_baseline_create_only(
                target, self.FIRST, repo_root=root
            )
            with self.assertRaises(_baseline.BaselineError) as raised:
                _baseline.write_baseline_create_only(
                    target, self.SECOND, repo_root=root
                )
            self.assertIn(
                "verification baseline already exists", str(raised.exception)
            )

    def test_the_refused_publication_leaves_the_first_bytes_alone(self):
        """The substance: refusing is not the same as not replacing."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "debt.json"
            _baseline.write_baseline_create_only(
                target, self.FIRST, repo_root=root
            )
            published = target.read_bytes()
            with self.assertRaises(_baseline.BaselineError):
                _baseline.write_baseline_create_only(
                    target, self.SECOND, repo_root=root
                )
            self.assertEqual(target.read_bytes(), published)
            self.assertNotIn(b"second", target.read_bytes())

    def test_the_refusal_leaves_no_temporary_file_behind(self):
        """A failed publication must not litter the directory it guards."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "debt.json"
            _baseline.write_baseline_create_only(
                target, self.FIRST, repo_root=root
            )
            with self.assertRaises(_baseline.BaselineError):
                _baseline.write_baseline_create_only(
                    target, self.SECOND, repo_root=root
                )
            self.assertEqual(
                sorted(child.name for child in root.iterdir()), ["debt.json"]
            )

    def test_the_first_publication_succeeds(self):
        """The premise: the refusals above are about the second call."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "debt.json"
            _baseline.write_baseline_create_only(
                target, self.FIRST, repo_root=root
            )
            self.assertTrue(target.exists())
            self.assertIn(b"first", target.read_bytes())


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

    def test_alternate_data_stream_spelling_in_a_parent_is_rejected(self):
        """Refuse ':' in every baseline component, not only in the file name.

        _validate_baseline_relative_path scans every component of the
        repository-relative path, but until now each test spelled the colon
        into the leaf, so narrowing the rule to the leaf alone
        (MUT-PROVIDERS-308) left the whole suite green. The lexical resolution
        used for head and index baseline lookups has no second layer behind
        that rule: under the narrowed rule core._resolve_baseline_path simply
        returns the stream path.

        Both entry points are driven here. Matching the ':' message rather
        than merely BaselineError matters for the write path, because
        _open_plain_child_directory refuses the component for its own reason
        and reports "directory traversal requires a child directory name".
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "sub:stream" / "debt.json"

            with self.assertRaisesRegex(
                _baseline.BaselineError, "alternate-data-stream"
            ):
                core._resolve_baseline_path(root, "sub:stream/debt.json")
            with self.assertRaisesRegex(
                _baseline.BaselineError, "alternate-data-stream"
            ):
                _baseline.write_baseline_create_only(
                    target, "reviewed debt\n", repo_root=root
                )

            self.assertEqual(list(root.iterdir()), [])

    def test_premise_the_parent_stream_spelling_keeps_its_colon_off_the_leaf(self):
        """Show that the rejected spelling really tests a non-leaf component.

        The rejection above would hold vacuously if pathlib folded
        `sub:stream` into a drive, or if the colon reached the leaf after
        normalization. Either way the test would only re-cover the file-name
        rule it is meant to look past, so assert the shape of the path the
        validator actually receives.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate = Path(os.path.abspath(root / "sub:stream" / "debt.json"))
            relative = candidate.relative_to(Path(os.path.abspath(root)))

            self.assertEqual(relative.parts, ("sub:stream", "debt.json"))
            self.assertNotIn(":", relative.name)
            self.assertTrue(
                any(":" in part for part in relative.parts[:-1]),
                "the fixture must carry its colon in a directory component",
            )

    def test_contrast_a_plain_nested_baseline_path_is_still_accepted(self):
        """Keep the ':' rule from degenerating into refusing every subdirectory.

        A guard that rejected any multi-component baseline path would satisfy
        the rejection test above while breaking ordinary layouts such as
        `sub/debt.json`, so assert that both entry points still accept one.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "sub" / "debt.json"

            resolved = core._resolve_baseline_path(root, "sub/debt.json")
            _baseline.write_baseline_create_only(
                target, "reviewed debt\n", repo_root=root
            )

            self.assertEqual(
                resolved, Path(os.path.abspath(root)) / "sub" / "debt.json"
            )
            self.assertEqual(target.read_text(), "reviewed debt\n")

    def test_ntfs_alternate_data_stream_baseline_spelling_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "debt.json:review.json"

            with self.assertRaisesRegex(
                _baseline.BaselineError,
                "alternate-data-stream",
            ):
                _baseline.write_baseline_create_only(
                    target,
                    "reviewed debt\n",
                    repo_root=root,
                )
            with self.assertRaisesRegex(
                _baseline.BaselineError,
                "alternate-data-stream",
            ):
                core._resolve_baseline_path(root, target.name)

            self.assertEqual(list(root.iterdir()), [])

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

            if os.name == "nt":
                self.assertFalse(
                    swapped,
                    "held Windows directory handles must deny an ancestor rename",
                )
            else:
                self.assertTrue(
                    swapped,
                    "the publication-time parent swap must execute",
                )
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
            real_publish = _baseline._MutationDirectory.replace_preserving_target

            def swap_before_publish(directory, replacement, destination, backup):
                nonlocal swapped
                swapped = self._swap_parent(parent, parked, outside)
                return real_publish(directory, replacement, destination, backup)

            try:
                with patch.object(
                    _baseline._MutationDirectory,
                    "replace_preserving_target",
                    new=swap_before_publish,
                ):
                    _baseline.replace_baseline_if_unchanged(
                        target,
                        "reduced debt\n",
                        expected,
                        repo_root=root,
                    )
            finally:
                self._restore_competing_parent(parent, outside, swapped)

            if os.name == "nt":
                self.assertFalse(
                    swapped,
                    "held Windows directory handles must deny an ancestor rename",
                )
            else:
                self.assertTrue(
                    swapped,
                    "the publication-time parent swap must execute",
                )
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
