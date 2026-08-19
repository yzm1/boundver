"""Regression tests for the v3 hash wire format and Git-backed sources."""

import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from boundver._git import (
    _capture_git_source_snapshot,
    _git_batch_cat,
    _git_cat_blob,
    _iter_git_nul_records,
    _list_files_for_source,
    list_head_files,
)
from boundver._hashing import (
    HASH_DOMAIN_CONTENT_ONLY,
    HASH_DOMAIN_EXACT,
    _content_only_digest,
    _hash_framed_entries,
    _read_bounded_path_bytes,
    _read_path_content,
    source_tree_digest,
)
from boundver._utils import GuardrailError
from tests._repo_fixtures import commit_all, init_git_repo


class FramedHashContractTests(unittest.TestCase):
    def test_known_v3_wire_format_vector(self):
        digest = _hash_framed_entries(
            [("file:svc/a.txt", "100644", "blob", b"A\n")],
            domain=HASH_DOMAIN_EXACT,
        )
        self.assertEqual(
            digest,
            "f511ac9b9ab7f9e4b6abb6bbae12dbd8a81ed550ad602e45170f3cefe12a2801",
        )

    def test_length_framing_prevents_file_content_ambiguity(self):
        one_file = [("file:svc/a", b"Xfile:svc/b\nY")]
        two_files = [("file:svc/a", b"X"), ("file:svc/b", b"Y")]
        self.assertNotEqual(
            _hash_framed_entries(one_file, domain=HASH_DOMAIN_EXACT),
            _hash_framed_entries(two_files, domain=HASH_DOMAIN_EXACT),
        )

    def test_domains_separate_identical_entries(self):
        entries = [("file:a", b"same")]
        self.assertNotEqual(
            _hash_framed_entries(entries, domain=HASH_DOMAIN_EXACT),
            _hash_framed_entries(entries, domain=HASH_DOMAIN_CONTENT_ONLY),
        )

    def test_input_order_does_not_change_digest(self):
        entries = [("file:b", b"B"), ("file:a", b"A")]
        self.assertEqual(
            _hash_framed_entries(entries, domain=HASH_DOMAIN_EXACT),
            _hash_framed_entries(reversed(entries), domain=HASH_DOMAIN_EXACT),
        )


class GitSourceContractTests(unittest.TestCase):
    def test_snapshot_tree_entry_limit_is_enforced_during_enumeration(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "a.txt").write_bytes(b"a")
            (root / "b.txt").write_bytes(b"b")
            commit_all(root)
            with patch("boundver._git.MAX_GIT_TREE_ENTRIES", 1):
                with self.assertRaisesRegex(GuardrailError, "1-entry limit"):
                    _capture_git_source_snapshot(root, "head")

    def test_unicode_filename_hashes_and_matches_all_clean_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "café.txt").write_bytes(b"line 1\r\nline 2\r\n")
            commit_all(root)

            self.assertEqual(list_head_files(root, "svc"), ["svc/café.txt"])
            head = source_tree_digest(root, "svc", source="head")
            index = source_tree_digest(root, "svc", source="index")
            working = source_tree_digest(root, "svc", source="working-tree")
            self.assertEqual(head, index)
            self.assertEqual(index, working)

    def test_unicode_filename_content_change_changes_head_digest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            target = root / "svc" / "café.txt"
            target.write_bytes(b"before")
            commit_all(root, "before")
            before = source_tree_digest(root, "svc", source="head")
            target.write_bytes(b"after")
            commit_all(root, "after")
            after = source_tree_digest(root, "svc", source="head")
            self.assertNotEqual(before, after)

    @unittest.skipIf(os.name == "nt", "Windows paths cannot contain backslashes")
    def test_literal_backslash_filename_is_not_treated_as_a_separator(self):
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            root1 = Path(td1)
            root2 = Path(td2)
            init_git_repo(root1)
            init_git_repo(root2)
            (root1 / "svc").mkdir()
            (root1 / "svc" / "a\\b").write_bytes(b"same")
            (root2 / "svc" / "a").mkdir(parents=True)
            (root2 / "svc" / "a" / "b").write_bytes(b"same")
            commit_all(root1)
            commit_all(root2)
            self.assertNotEqual(
                source_tree_digest(root1, "svc", source="head"),
                source_tree_digest(root2, "svc", source="head"),
            )

    @unittest.skipIf(os.name == "nt", "Windows paths cannot contain newlines")
    def test_newline_filename_hashes_without_batch_protocol_ambiguity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "line\nbreak.txt").write_bytes(b"content")
            commit_all(root)
            head = source_tree_digest(root, "svc", source="head")
            index = source_tree_digest(root, "svc", source="index")
            working = source_tree_digest(root, "svc", source="working-tree")
            self.assertEqual(head, index)
            self.assertEqual(index, working)

    @unittest.skipIf(
        os.name == "nt" or sys.platform == "darwin",
        "Test requires a filesystem that accepts non-UTF-8 POSIX byte filenames",
    )
    def test_non_utf8_filename_bytes_round_trip_from_git(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            raw_path = os.fsencode(root / "svc") + b"/invalid-\xff.txt"
            fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                os.write(fd, b"content")
            finally:
                os.close(fd)
            commit_all(root)
            head = source_tree_digest(root, "svc", source="head")
            index = source_tree_digest(root, "svc", source="index")
            working = source_tree_digest(root, "svc", source="working-tree")
            self.assertEqual(head, index)
            self.assertEqual(index, working)

    def test_working_tree_uses_tracked_files_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "tracked.txt").write_bytes(b"tracked")
            commit_all(root)
            before = source_tree_digest(root, "svc", source="working-tree")
            (root / "svc" / "untracked.txt").write_bytes(b"untracked")
            after = source_tree_digest(root, "svc", source="working-tree")
            self.assertEqual(before, after)

    def test_successful_empty_index_does_not_fallback_to_untracked_disk_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "baseline.txt").write_bytes(b"committed")
            commit_all(root)
            subprocess.run(
                ["git", "read-tree", "--empty"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            (root / "untracked.txt").write_bytes(b"not in the index")
            self.assertEqual(
                _list_files_for_source(root, ".", source="index"),
                [],
            )
            self.assertEqual(
                _list_files_for_source(root, ".", source="working-tree"),
                [],
            )

    def test_unborn_working_tree_uses_filesystem_for_initial_setup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "first-file.txt").write_bytes(b"not committed yet")

            files = _list_files_for_source(root, ".", source="working-tree")

            self.assertIn("first-file.txt", files)
            self.assertFalse(
                any(path == ".git" or path.startswith(".git/") for path in files),
                files,
            )

    def test_filesystem_fallback_fails_instead_of_truncating_at_file_limit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "a.txt").write_bytes(b"a")
            (root / "svc" / "b.txt").write_bytes(b"b")

            with patch("boundver._git.MAX_FALLBACK_FILES", 1):
                with self.assertRaisesRegex(GuardrailError, ">1 files"):
                    _list_files_for_source(root, "svc", source="working-tree")

    def test_content_only_digest_matches_all_clean_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "a.txt").write_bytes(b"one\r\ntwo\r\n")
            commit_all(root)
            digests = {
                _content_only_digest(root, "svc", source=source)
                for source in ("head", "index", "working-tree")
            }
            self.assertEqual(len(digests), 1)


class FailClosedGitBlobTests(unittest.TestCase):
    @staticmethod
    def _popen_with_stdout(stdout: bytes, returncode: int = 0):
        proc = MagicMock()
        proc.stdin = io.BytesIO()
        proc.stdout = io.BytesIO(stdout)
        proc.wait.return_value = returncode
        proc.poll.return_value = returncode
        return proc

    def test_real_missing_batch_blob_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "present.txt").write_bytes(b"present")
            commit_all(root)
            with self.assertRaisesRegex(ValueError, "not found"):
                _git_batch_cat(root, ["HEAD:missing.txt"])

    def test_real_empty_blob_remains_valid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "empty.txt").write_bytes(b"")
            commit_all(root)
            self.assertEqual(
                _git_batch_cat(root, ["HEAD:empty.txt"]),
                {"HEAD:empty.txt": b""},
            )

    def test_truncated_batch_header_raises(self):
        proc = self._popen_with_stdout(b"unterminated header")
        with patch("boundver._git.subprocess.Popen", return_value=proc):
            with self.assertRaisesRegex(ValueError, "Truncated"):
                _git_batch_cat(Path("."), ["HEAD:file.txt"])

    def test_truncated_batch_content_raises(self):
        proc = self._popen_with_stdout(b"object-id blob 5\nabc\n")
        with patch("boundver._git.subprocess.Popen", return_value=proc):
            with self.assertRaisesRegex(ValueError, "Truncated"):
                _git_batch_cat(Path("."), ["HEAD:file.txt"])

    def test_oversized_batch_blob_raises_before_reading_content(self):
        proc = self._popen_with_stdout(b"object-id blob 52428801\n")
        with patch("boundver._git.subprocess.Popen", return_value=proc):
            with self.assertRaises(GuardrailError):
                _git_batch_cat(Path("."), ["HEAD:file.txt"])

    def test_oversized_single_blob_raises(self):
        proc = self._popen_with_stdout(b"12345")
        with patch("boundver._git.MAX_GIT_BLOB_BYTES", 4):
            with patch("boundver._git.subprocess.Popen", return_value=proc):
                with self.assertRaises(GuardrailError):
                    _git_cat_blob(Path("."), "HEAD:file.txt")

    def test_filesystem_read_race_raises_instead_of_hashing_empty_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "file.txt"
            target.write_bytes(b"present")
            with patch.object(Path, "open", side_effect=FileNotFoundError):
                with self.assertRaisesRegex(ValueError, "disappeared"):
                    _read_path_content(root, target, source="working-tree")

    def test_bounded_file_read_detects_growth_beyond_limit_after_open(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "growing.bin"
            target.write_bytes(b"12345")
            real_fstat = os.fstat
            calls = 0

            def underreport_first_size(fd):
                nonlocal calls
                calls += 1
                result = real_fstat(fd)
                if calls != 1:
                    return result
                return types.SimpleNamespace(
                    st_mode=result.st_mode,
                    st_size=4,
                    st_mtime_ns=result.st_mtime_ns,
                    st_dev=result.st_dev,
                    st_ino=result.st_ino,
                )

            with patch("boundver._hashing.os.fstat", side_effect=underreport_first_size):
                with self.assertRaisesRegex(GuardrailError, "file too large"):
                    _read_bounded_path_bytes(target, "growing.bin", max_bytes=4)

    def test_bounded_file_read_rejects_a_file_changed_during_read(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "changing.bin"
            target.write_bytes(b"data")
            real_fstat = os.fstat
            calls = 0

            def report_growth_after_read(fd):
                nonlocal calls
                calls += 1
                result = real_fstat(fd)
                if calls != 2:
                    return result
                return types.SimpleNamespace(
                    st_mode=result.st_mode,
                    st_size=result.st_size + 1,
                    st_mtime_ns=result.st_mtime_ns + 1,
                    st_dev=result.st_dev,
                    st_ino=result.st_ino,
                )

            with patch("boundver._hashing.os.fstat", side_effect=report_growth_after_read):
                with self.assertRaisesRegex(ValueError, "changed while hashing"):
                    _read_bounded_path_bytes(target, "changing.bin")

    def test_git_listing_transport_is_bounded_while_streaming(self):
        proc = self._popen_with_stdout(b"record-without-a-terminator")
        with patch("boundver._git.MAX_GIT_LIST_OUTPUT_BYTES", 4):
            with patch("boundver._git.subprocess.Popen", return_value=proc):
                with self.assertRaisesRegex(GuardrailError, "transport limit"):
                    list(_iter_git_nul_records(Path("."), ["ls-tree", "-z"]))


if __name__ == "__main__":
    unittest.main()
