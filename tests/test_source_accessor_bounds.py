"""Integration contracts for caller-bounded source reads."""

from __future__ import annotations

import os
import subprocess
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from boundver._config import load_config_file
from boundver._git import _working_tree_mode
from boundver._hashing import _ModeAwareBytes, _verify_working_tree_ancestors
from boundver._lockfile import _SourceAccessor
from boundver._utils import (
    ConfigError,
    GuardrailError,
    _is_windows_reparse_point,
    _iter_bounded_filesystem_paths,
)
from tests._repo_fixtures import commit_all, init_git_repo


class SourceAccessorBoundTests(unittest.TestCase):
    def _repository(self, root: Path) -> None:
        init_git_repo(root)
        (root / "data.bin").write_bytes(b"12345678")
        commit_all(root, "fixture")

    def test_head_read_honors_provider_remaining_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._repository(root)
            with _SourceAccessor(root, "head") as accessor:
                with self.assertRaisesRegex(
                    GuardrailError, "Git blob too large"
                ):
                    accessor.read_file_limited("data.bin", 4)

                content = accessor.read_file_limited("data.bin", 8)
                self.assertEqual(content, b"12345678")
                self.assertEqual(content.git_mode, "100644")

    def test_working_tree_read_honors_provider_remaining_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._repository(root)
            accessor = _SourceAccessor(root, "working-tree")

            with self.assertRaisesRegex(GuardrailError, "file too large"):
                accessor.read_file_limited("data.bin", 4)

            self.assertEqual(
                accessor.read_file_limited("data.bin", 8),
                b"12345678",
            )

    def test_working_tree_symlink_size_is_checked_before_readlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accessor = _SourceAccessor(root, "working-tree")
            link_stat = types.SimpleNamespace(
                st_dev=1,
                st_ino=2,
                st_size=2,
                st_mtime_ns=3,
                st_mode=stat.S_IFLNK | 0o777,
            )
            with (
                patch.object(Path, "lstat", return_value=link_stat),
                patch("boundver._hashing.os.readlink") as readlink,
            ):
                with self.assertRaisesRegex(
                    GuardrailError,
                    "file too large",
                ):
                    accessor.read_file_limited("link", 1)
            readlink.assert_not_called()

    def test_working_tree_symlink_identity_change_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accessor = _SourceAccessor(root, "working-tree")
            before = types.SimpleNamespace(
                st_dev=1,
                st_ino=2,
                st_size=1,
                st_mtime_ns=3,
                st_mode=stat.S_IFLNK | 0o777,
            )
            after = types.SimpleNamespace(
                st_dev=1,
                st_ino=9,
                st_size=1,
                st_mtime_ns=3,
                st_mode=stat.S_IFLNK | 0o777,
            )
            with (
                patch.object(Path, "lstat", side_effect=[before, after]),
                patch("boundver._hashing.os.readlink", return_value="x"),
            ):
                with self.assertRaisesRegex(ValueError, "changed while hashing"):
                    accessor.read_file_limited("link", 1)

    def test_working_tree_type_change_before_content_read_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accessor = _SourceAccessor(root, "working-tree")
            regular = types.SimpleNamespace(
                st_dev=1,
                st_ino=2,
                st_size=1,
                st_mtime_ns=3,
                st_mode=stat.S_IFREG | 0o644,
            )
            symlink = types.SimpleNamespace(
                st_dev=1,
                st_ino=9,
                st_size=1,
                st_mtime_ns=4,
                st_mode=stat.S_IFLNK | 0o777,
            )
            with (
                patch.object(Path, "lstat", side_effect=[regular, symlink]),
                patch(
                    "boundver._hashing._read_bounded_path_bytes",
                    return_value=b"x",
                ),
            ):
                with self.assertRaisesRegex(ValueError, "changed while hashing"):
                    accessor.read_file_limited("file", 1)

    def _link_and_target_samples(self):
        """Return a symlink identity sample and the sample of its target.

        The two samples stand for one path answered two ways: the first is
        what ``lstat`` reports for the link itself, the second is what ``stat``
        reports after following it to an ordinary file.  They are fabricated
        rather than made from a real symlink on disk because a Windows host
        without the symlink privilege can only skip such a test, and a skipped
        case is no coverage at all.  ``_working_tree_mode`` reads nothing but
        ``st_mode`` from the sample it is given, so a stand-in carrying the
        same fields the rest of this file fabricates answers the classifier
        exactly as a real sample would.
        """
        link = types.SimpleNamespace(
            st_dev=1,
            st_ino=2,
            st_size=9,
            st_mtime_ns=3,
            st_mode=stat.S_IFLNK | 0o777,
        )
        target = types.SimpleNamespace(
            st_dev=1,
            st_ino=4,
            st_size=9,
            st_mtime_ns=3,
            st_mode=stat.S_IFREG | 0o644,
        )
        return link, target

    def test_working_tree_mode_samples_a_path_with_lstat_not_stat(self):
        """The mode classifier describes the path, never what it points at.

        When no caller hands ``_working_tree_mode`` an identity sample it takes
        one of its own, and obligation OBL-GIT-SOURCE-027 makes that sample the
        thing being hashed rather than whatever lies at the end of a link.
        Mutant MUT-GIT-SOURCE-525 turns that ``lstat`` into a ``stat``, so the
        classifier follows the link and answers for the target: a symlink to an
        ordinary file is framed as mode 100644 instead of 120000, and a symlink
        whose target is gone is reported as a file that disappeared while it is
        still sitting there.  Nothing caught that, because the only cases that
        let the function sample for itself are a real regular file, where
        ``lstat`` and ``stat`` agree, and a path that is genuinely absent;
        every other case passes ``path_stat`` in and skips the sampling call
        altogether.  Here the path answers one way to ``lstat`` and another to
        ``stat``, so only the link's own mode can produce the expected result,
        and the dereferencing call must not happen at all.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            link, target = self._link_and_target_samples()
            with (
                patch.object(Path, "lstat", return_value=link) as link_sample,
                patch.object(Path, "stat", return_value=target) as target_sample,
            ):
                classified = _working_tree_mode(root, "svc/link")

            self.assertEqual(classified, ("120000", "blob"))
            target_sample.assert_not_called()
            self.assertEqual(link_sample.call_count, 1)

    def test_link_and_target_samples_premise_disagree_about_the_mode(self):
        """The two samples really give different answers to the classifier.

        This is the premise the test above rests on.  If a dereferenced sample
        happened to classify as 120000 as well, then the expected result would
        hold no matter which call the classifier made and the assertion would
        say nothing about mutant MUT-GIT-SOURCE-525.  We therefore feed both
        samples to ``_working_tree_mode`` explicitly and show that they part
        company: the link is a symlink blob and the target is a plain file.
        """
        link, target = self._link_and_target_samples()

        self.assertTrue(stat.S_ISLNK(link.st_mode))
        self.assertTrue(stat.S_ISREG(target.st_mode))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(
                _working_tree_mode(root, "svc/link", path_stat=link),
                ("120000", "blob"),
            )
            self.assertEqual(
                _working_tree_mode(root, "svc/link", path_stat=target),
                ("100644", "blob"),
            )

    def test_self_sampled_classifier_still_reads_and_still_refuses_real_paths(self):
        """A classifier that answered 120000 for everything would also pass.

        The contrast case walks the same sampling branch against the real
        filesystem, with no patching in the way.  An ordinary tracked file must
        still classify as a regular blob, and a path that is genuinely not
        there must still be refused by name, so the test above pins which call
        the classifier makes rather than making it answer symlink to all
        comers.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "present.txt").write_bytes(b"A\n")

            self.assertEqual(
                _working_tree_mode(root, "present.txt"),
                ("100644", "blob"),
            )
            with self.assertRaisesRegex(
                ValueError,
                "File disappeared while hashing: gone.txt",
            ):
                _working_tree_mode(root, "gone.txt")

    def test_working_tree_rejects_symlink_directory_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo"
            external = base / "external"
            root.mkdir()
            external.mkdir()
            (external / "data.bin").write_bytes(b"external")
            try:
                (root / "svc").symlink_to(external, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")

            accessor = _SourceAccessor(root, "working-tree")
            with self.assertRaisesRegex(ValueError, "ancestor"):
                accessor.read_file_limited("svc/data.bin", 8)

    def _ancestor_with_altered_permissions(self, directory: Path):
        """Return the live identity of ``directory`` and a permission twin.

        The twin is an ``os.stat_result`` that copies every field of the live
        sample except the permission bits, so it stands for an ancestor that
        was captured before somebody changed the directory's mode.  Device and
        inode are carried over deliberately: the guard must refuse on the mode
        alone, and it can only be shown to do so when the identity fields agree.
        """
        live = directory.lstat()
        permissions = live.st_mode & 0o777
        replacement = 0o700 if permissions != 0o700 else 0o755
        captured = os.stat_result(
            (
                (live.st_mode & ~0o777) | replacement,
                live.st_ino,
                live.st_dev,
                live.st_nlink,
                live.st_uid,
                live.st_gid,
                live.st_size,
                int(live.st_atime),
                int(live.st_mtime),
                int(live.st_ctime),
            )
        )
        return live, captured

    def test_ancestor_permission_change_during_read_fails_closed(self):
        """The ancestor re-check refuses a directory whose mode moved.

        Obligation OBL-GIT-SOURCE-078 makes the ancestor identity the triple
        ``(st_dev, st_ino, st_mode)``, but until this test no case ever moved an
        ancestor's mode between capture and verification.  Mutant
        MUT-GIT-SOURCE-308 deletes the ``before.st_mode != after.st_mode``
        disjunct from ``_verify_working_tree_ancestors`` and the whole suite
        stayed green, because the two existing identity tests drive
        single-segment paths whose ancestor list is empty and therefore only
        exercise the leaf-file re-check.  This test calls the ancestor verifier
        directly with a captured sample that differs from the live directory in
        its permission bits alone, so only the deleted comparison can reject it.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "svc"
            directory.mkdir()
            live, captured = self._ancestor_with_altered_permissions(directory)

            with self.assertRaisesRegex(ValueError, "changed while hashing"):
                _verify_working_tree_ancestors([(directory, captured)], "svc/a.txt")

            # The live directory is untouched, so a re-run must refuse again
            # rather than having consumed a one-shot condition.
            self.assertEqual(directory.lstat().st_mode, live.st_mode)

    def test_ancestor_permission_premise_isolates_the_mode_comparison(self):
        """The fixture really differs from the live directory only in its mode.

        This is the premise the refusal above rests on.  If the captured sample
        happened to carry a different device or inode, or to describe something
        that is not a plain directory, then ``_verify_working_tree_ancestors``
        could refuse for a reason that survives mutant MUT-GIT-SOURCE-308 and
        the refusal would prove nothing about the mode comparison.  Here we
        assert that every other clause of the guard is satisfied and that the
        mode genuinely changed.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "svc"
            directory.mkdir()
            live, captured = self._ancestor_with_altered_permissions(directory)

            self.assertNotEqual(captured.st_mode, live.st_mode)
            self.assertEqual(
                (captured.st_dev, captured.st_ino),
                (live.st_dev, live.st_ino),
            )
            self.assertTrue(stat.S_ISDIR(live.st_mode))
            self.assertTrue(stat.S_ISDIR(captured.st_mode))
            self.assertFalse(_is_windows_reparse_point(live))

    def test_unchanged_ancestor_identity_is_still_accepted(self):
        """An ancestor that did not move is accepted, both alone and in a read.

        A guard that refused every ancestor would also kill mutant
        MUT-GIT-SOURCE-308, so this contrast case pins the ordinary path.  We
        verify a freshly captured ancestor against itself, and we read a real
        nested file end to end through the working-tree accessor, which
        captures and re-checks the same ancestor for us.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "svc"
            directory.mkdir()
            (directory / "a.txt").write_bytes(b"A\n")

            self.assertIsNone(
                _verify_working_tree_ancestors(
                    [(directory, directory.lstat())],
                    "svc/a.txt",
                )
            )

            accessor = _SourceAccessor(root, "working-tree")
            self.assertEqual(accessor.read_file_limited("svc/a.txt", 16), b"A\n")

    def _ancestor_reparse_samples(self, directory: Path, *, live_is_reparse: bool):
        """Return the captured sample of ``directory`` and a live twin of it.

        The twin copies every identity field of the real directory and differs
        only in its Windows attribute word, where the reparse-point flag is
        either set or cleared to order.  It stands for an ancestor that was a
        plain directory when the read began and had a junction or another
        reparse point mounted over it in place, which leaves device, inode and
        mode exactly as they were.  The twin is fabricated because
        ``os.stat_result`` carries ``st_file_attributes`` on Windows alone, so
        a test built on a real junction could never run on the Linux and macOS
        legs of CI, and ``_verify_working_tree_ancestors`` reads nothing from a
        sample beyond the identity and timestamp fields copied here.
        """
        captured = directory.lstat()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        attributes = getattr(captured, "st_file_attributes", 0) & ~reparse_flag
        if live_is_reparse:
            attributes |= reparse_flag
        live = types.SimpleNamespace(
            st_dev=captured.st_dev,
            st_ino=captured.st_ino,
            st_mode=captured.st_mode,
            st_size=captured.st_size,
            st_ctime_ns=captured.st_ctime_ns,
            st_mtime_ns=captured.st_mtime_ns,
            st_file_attributes=attributes,
        )
        return captured, live

    def test_ancestor_that_becomes_a_reparse_point_fails_closed(self):
        """The ancestor re-check refuses a directory that grew a reparse point.

        Clause (c) of obligation OBL-GIT-SOURCE-078 says the read must fail
        closed when an ancestor's identity changes between capture and
        completion, and the guard spells that identity out as four conditions.
        Until this test only two of them were ever driven, the ``(st_dev,
        st_ino)`` pair and ``st_mode``, so mutant MUT-GIT-SOURCE-531 could
        delete the ``_is_windows_reparse_point(after)`` disjunct and leave the
        suite green.  The clause carries its own weight: that helper reads
        ``st_file_attributes`` and nothing else, so a directory that acquires a
        junction or another non-symlink reparse tag in place keeps its device,
        inode and mode, stays a directory, and slips past every remaining
        condition.  Its twin at capture time is already covered by
        ``test_working_tree_rejects_junction_directory_ancestor``; only the
        re-check was blind.  Here the captured sample is the real directory and
        the live one differs from it in the reparse flag alone, so the refusal
        can come from nowhere else.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "svc"
            directory.mkdir()
            captured, live = self._ancestor_reparse_samples(
                directory, live_is_reparse=True
            )

            with patch.object(Path, "lstat", return_value=live):
                with self.assertRaisesRegex(
                    ValueError,
                    "File changed while hashing: svc/a.txt",
                ):
                    _verify_working_tree_ancestors(
                        [(directory, captured)],
                        "svc/a.txt",
                    )

    def test_ancestor_reparse_premise_isolates_the_attribute_comparison(self):
        """The live twin differs from the captured sample in that flag alone.

        This is the premise the refusal above rests on.  Should the twin carry
        a different device, inode or mode, or describe something other than a
        plain directory, ``_verify_working_tree_ancestors`` would have another
        reason to refuse, that reason would survive mutant MUT-GIT-SOURCE-531,
        and the refusal would prove nothing about the reparse-point check.  We
        assert that every other condition of the guard is satisfied and that
        the attribute word is the one thing that moved.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "svc"
            directory.mkdir()
            captured, live = self._ancestor_reparse_samples(
                directory, live_is_reparse=True
            )

            self.assertEqual(captured.st_mode, live.st_mode)
            self.assertEqual(
                (captured.st_dev, captured.st_ino),
                (live.st_dev, live.st_ino),
            )
            self.assertTrue(stat.S_ISDIR(live.st_mode))
            self.assertTrue(_is_windows_reparse_point(live))
            self.assertFalse(_is_windows_reparse_point(captured))

    def test_ancestor_without_the_reparse_flag_is_still_accepted(self):
        """The same machinery with the flag cleared must be waved through.

        A re-check that refused every ancestor would kill mutant
        MUT-GIT-SOURCE-531 just as well, so this contrast keeps the ordinary
        path open.  The fabricated live sample and the patched ``lstat`` are
        exactly as above and only the reparse flag is missing, which leaves the
        flag as the sole cause of the refusal rather than the stand-in sample
        or the patch around it.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "svc"
            directory.mkdir()
            captured, live = self._ancestor_reparse_samples(
                directory, live_is_reparse=False
            )

            self.assertFalse(_is_windows_reparse_point(live))
            with patch.object(Path, "lstat", return_value=live):
                self.assertIsNone(
                    _verify_working_tree_ancestors(
                        [(directory, captured)],
                        "svc/a.txt",
                    )
                )

    @unittest.skipUnless(os.name == "nt", "NTFS junction behavior is Windows-only")
    def test_working_tree_rejects_junction_directory_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo"
            external = base / "external"
            root.mkdir()
            external.mkdir()
            (external / "data.bin").write_bytes(b"external")
            junction = root / "svc"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                capture_output=True,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"junctions unavailable: {created.stderr.strip()}")

            try:
                accessor = _SourceAccessor(root, "working-tree")
                with self.assertRaisesRegex(ValueError, "reparse point"):
                    accessor.read_file_limited("svc/data.bin", 8)
            finally:
                if junction.exists():
                    junction.rmdir()

    @unittest.skipUnless(os.name == "nt", "NTFS junction behavior is Windows-only")
    def test_filesystem_traversal_does_not_descend_junction(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo"
            external = base / "external"
            root.mkdir()
            external.mkdir()
            (external / "secret.txt").write_text("external", encoding="utf-8")
            junction = root / "linked"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                capture_output=True,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"junctions unavailable: {created.stderr.strip()}")

            try:
                discovered = [
                    path.relative_to(root).as_posix()
                    for path in _iter_bounded_filesystem_paths(
                        root,
                        recursive=True,
                        max_entries=10,
                        exceeded_message="too many entries",
                    )
                ]
                self.assertIn("linked", discovered)
                self.assertNotIn("linked/secret.txt", discovered)
            finally:
                if junction.exists():
                    junction.rmdir()

    def test_version_read_rechecks_symlink_mode_after_content_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accessor = _SourceAccessor(root, "working-tree")
            symlink_data = _ModeAwareBytes(b"target", "120000", "blob")
            with (
                patch.object(Path, "is_symlink", return_value=False),
                patch.object(
                    accessor,
                    "read_file_limited",
                    return_value=symlink_data,
                ),
            ):
                with self.assertRaisesRegex(ConfigError, "must not be a symlink"):
                    accessor.version_read_file("version.json")

    def test_disk_config_loader_uses_its_own_hard_read_ceiling(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "boundary.config.json"
            path.write_bytes(b'{"project":"too large"}')

            with patch("boundver._config.MAX_CONFIG_BYTES", 8):
                with self.assertRaisesRegex(ConfigError, "8-byte limit"):
                    load_config_file(path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
