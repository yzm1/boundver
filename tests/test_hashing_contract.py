"""Regression tests for the v3 hash wire format and Git-backed sources."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from boundver._git import (
    _git_batch_cat,
    _git_cat_blob,
    _list_files_for_source,
    list_head_files,
)
from boundver._hashing import (
    HASH_DOMAIN_CONTENT_ONLY,
    HASH_DOMAIN_EXACT,
    _content_only_digest,
    _hash_framed_entries,
    _read_path_content,
    source_tree_digest,
)
from boundver._utils import GuardrailError


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Boundver Test"],
        cwd=root,
        check=True,
    )


def _commit_all(root: Path, message: str = "test") -> None:
    subprocess.run(["git", "add", "--all"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=root, check=True)


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
    def test_unicode_filename_hashes_and_matches_all_clean_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "café.txt").write_bytes(b"line 1\r\nline 2\r\n")
            _commit_all(root)

            self.assertEqual(list_head_files(root, "svc"), ["svc/café.txt"])
            head = source_tree_digest(root, "svc", source="head")
            index = source_tree_digest(root, "svc", source="index")
            working = source_tree_digest(root, "svc", source="working-tree")
            self.assertEqual(head, index)
            self.assertEqual(index, working)

    def test_unicode_filename_content_change_changes_head_digest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "svc").mkdir()
            target = root / "svc" / "café.txt"
            target.write_bytes(b"before")
            _commit_all(root, "before")
            before = source_tree_digest(root, "svc", source="head")
            target.write_bytes(b"after")
            _commit_all(root, "after")
            after = source_tree_digest(root, "svc", source="head")
            self.assertNotEqual(before, after)

    @unittest.skipIf(os.name == "nt", "Windows paths cannot contain backslashes")
    def test_literal_backslash_filename_is_not_treated_as_a_separator(self):
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            root1 = Path(td1)
            root2 = Path(td2)
            _init_repo(root1)
            _init_repo(root2)
            (root1 / "svc").mkdir()
            (root1 / "svc" / "a\\b").write_bytes(b"same")
            (root2 / "svc" / "a").mkdir(parents=True)
            (root2 / "svc" / "a" / "b").write_bytes(b"same")
            _commit_all(root1)
            _commit_all(root2)
            self.assertNotEqual(
                source_tree_digest(root1, "svc", source="head"),
                source_tree_digest(root2, "svc", source="head"),
            )

    @unittest.skipIf(os.name == "nt", "Windows paths cannot contain newlines")
    def test_newline_filename_hashes_without_batch_protocol_ambiguity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "line\nbreak.txt").write_bytes(b"content")
            _commit_all(root)
            head = source_tree_digest(root, "svc", source="head")
            index = source_tree_digest(root, "svc", source="index")
            working = source_tree_digest(root, "svc", source="working-tree")
            self.assertEqual(head, index)
            self.assertEqual(index, working)

    @unittest.skipIf(os.name == "nt", "Test requires POSIX byte filenames")
    def test_non_utf8_filename_bytes_round_trip_from_git(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "svc").mkdir()
            raw_path = os.fsencode(root / "svc") + b"/invalid-\xff.txt"
            fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                os.write(fd, b"content")
            finally:
                os.close(fd)
            _commit_all(root)
            head = source_tree_digest(root, "svc", source="head")
            index = source_tree_digest(root, "svc", source="index")
            working = source_tree_digest(root, "svc", source="working-tree")
            self.assertEqual(head, index)
            self.assertEqual(index, working)

    def test_working_tree_uses_tracked_files_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "tracked.txt").write_bytes(b"tracked")
            _commit_all(root)
            before = source_tree_digest(root, "svc", source="working-tree")
            (root / "svc" / "untracked.txt").write_bytes(b"untracked")
            after = source_tree_digest(root, "svc", source="working-tree")
            self.assertEqual(before, after)

    def test_successful_empty_index_does_not_fallback_to_untracked_disk_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "baseline.txt").write_bytes(b"committed")
            _commit_all(root)
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
            _init_repo(root)
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
            _init_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "a.txt").write_bytes(b"one\r\ntwo\r\n")
            _commit_all(root)
            digests = {
                _content_only_digest(root, "svc", source=source)
                for source in ("head", "index", "working-tree")
            }
            self.assertEqual(len(digests), 1)


class FailClosedGitBlobTests(unittest.TestCase):
    def test_real_missing_batch_blob_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "present.txt").write_bytes(b"present")
            _commit_all(root)
            with self.assertRaisesRegex(ValueError, "not found"):
                _git_batch_cat(root, ["HEAD:missing.txt"])

    def test_real_empty_blob_remains_valid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "empty.txt").write_bytes(b"")
            _commit_all(root)
            self.assertEqual(
                _git_batch_cat(root, ["HEAD:empty.txt"]),
                {"HEAD:empty.txt": b""},
            )

    def test_truncated_batch_header_raises(self):
        completed = subprocess.CompletedProcess(
            args=["git", "cat-file"],
            returncode=0,
            stdout=b"unterminated header",
            stderr=b"",
        )
        with patch("boundver._git.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(ValueError, "Truncated"):
                _git_batch_cat(Path("."), ["HEAD:file.txt"])

    def test_truncated_batch_content_raises(self):
        completed = subprocess.CompletedProcess(
            args=["git", "cat-file"],
            returncode=0,
            stdout=b"object-id blob 5\nabc\n",
            stderr=b"",
        )
        with patch("boundver._git.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(ValueError, "Truncated"):
                _git_batch_cat(Path("."), ["HEAD:file.txt"])

    def test_oversized_batch_blob_raises_before_reading_content(self):
        completed = subprocess.CompletedProcess(
            args=["git", "cat-file"],
            returncode=0,
            stdout=b"object-id blob 52428801\n",
            stderr=b"",
        )
        with patch("boundver._git.subprocess.run", return_value=completed):
            with self.assertRaises(GuardrailError):
                _git_batch_cat(Path("."), ["HEAD:file.txt"])

    def test_oversized_single_blob_raises(self):
        completed = subprocess.CompletedProcess(
            args=["git", "show"],
            returncode=0,
            stdout=b"12345",
            stderr=b"",
        )
        with patch("boundver._git.MAX_GIT_BLOB_BYTES", 4):
            with patch("boundver._git.subprocess.run", return_value=completed):
                with self.assertRaises(GuardrailError):
                    _git_cat_blob(Path("."), "HEAD:file.txt")

    def test_filesystem_read_race_raises_instead_of_hashing_empty_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "file.txt"
            target.write_bytes(b"present")
            with patch.object(Path, "read_bytes", side_effect=FileNotFoundError):
                with self.assertRaisesRegex(ValueError, "disappeared"):
                    _read_path_content(root, target, source="working-tree")


if __name__ == "__main__":
    unittest.main()
