"""Hard-bound regressions for small Git command capture."""

import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

import boundver._git as git_helpers
from boundver._lockfile import _SourceAccessor
from boundver._utils import GuardrailError


class _FakeProcess:
    def __init__(self, stdout, stderr, *, returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.killed = False

    def wait(self):
        return self.returncode

    def kill(self):
        self.killed = True


class _AdversarialPipe:
    """Act like an endless producer and reject unbounded consumer reads."""

    def __init__(self):
        self.read_sizes = []
        self.closed = False

    def read(self, size=-1):
        if size < 0:
            raise AssertionError("Git output must never be read without a cap")
        self.read_sizes.append(size)
        return b"x" * size

    def close(self):
        self.closed = True


class GitRunBoundTests(unittest.TestCase):
    def test_success_returns_text_completed_process(self):
        process = _FakeProcess(
            io.BytesIO(b"object-id\r\n"),
            io.BytesIO(b"warning\r"),
        )
        with patch("boundver._git.subprocess.Popen", return_value=process) as popen:
            result = git_helpers._git_run(Path("repo"), ["rev-parse", "HEAD"])

        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual(
            result.args,
            [
                "git",
                "--no-pager",
                "-C",
                "repo",
                "-c",
                "core.fsmonitor=false",
                "rev-parse",
                "HEAD",
            ],
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "object-id\n")
        self.assertEqual(result.stderr, "warning\n")
        popen.assert_called_once_with(
            [
                "git",
                "--no-pager",
                "-C",
                "repo",
                "-c",
                "core.fsmonitor=false",
                "rev-parse",
                "HEAD",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=ANY,
        )

    def test_success_uses_filesystem_codec_and_losslessly_preserves_bytes(self):
        process = _FakeProcess(
            io.BytesIO("réf".encode("utf-8") + b"-\xff\n"),
            io.BytesIO(),
        )
        with (
            patch("boundver._git.subprocess.Popen", return_value=process),
            patch("boundver._git.sys.getfilesystemencoding", return_value="ascii"),
        ):
            result = git_helpers._git_run(Path("repo"), ["symbolic-ref", "HEAD"])

        self.assertEqual(
            result.stdout.encode("ascii", "surrogateescape"),
            "réf".encode("utf-8") + b"-\xff\n",
        )

    def test_success_does_not_consult_hostile_preferred_locale(self):
        process = _FakeProcess(
            io.BytesIO("réf\n".encode("utf-8")),
            io.BytesIO(),
        )
        with (
            patch("boundver._git.subprocess.Popen", return_value=process),
            patch("boundver._git.sys.getfilesystemencoding", return_value="utf-8"),
            patch("locale.getpreferredencoding", return_value="ascii") as preferred,
        ):
            result = git_helpers._git_run(Path("repo"), ["symbolic-ref", "HEAD"])

        self.assertEqual(result.stdout, "réf\n")
        preferred.assert_not_called()

    def test_nonzero_exit_preserves_text_called_process_error(self):
        process = _FakeProcess(
            io.BytesIO(b"partial\r\n"),
            io.BytesIO(b"failure\r"),
            returncode=7,
        )
        with patch("boundver._git.subprocess.Popen", return_value=process):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                git_helpers._git_run(Path("repo"), ["write-tree"])

        error = raised.exception
        self.assertEqual(error.returncode, 7)
        self.assertEqual(
            error.cmd,
            [
                "git",
                "--no-pager",
                "-C",
                "repo",
                "-c",
                "core.fsmonitor=false",
                "write-tree",
            ],
        )
        self.assertEqual(error.output, "partial\n")
        self.assertEqual(error.stdout, "partial\n")
        self.assertEqual(error.stderr, "failure\n")

    def test_nonzero_exit_preserves_invalid_diagnostic_bytes(self):
        process = _FakeProcess(
            io.BytesIO(),
            io.BytesIO(b"fatal: invalid \xff\n"),
            returncode=6,
        )
        with (
            patch("boundver._git.subprocess.Popen", return_value=process),
            patch("boundver._git.sys.getfilesystemencoding", return_value="utf-8"),
        ):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                git_helpers._git_run(Path("repo"), ["write-tree"])

        self.assertIn("fatal", raised.exception.stderr)
        self.assertIn("\\xff", raised.exception.stderr)

    def test_oversized_stderr_is_killed_at_one_byte_sentinel(self):
        stderr = _AdversarialPipe()
        process = _FakeProcess(io.BytesIO(), stderr)
        with (
            patch("boundver._git.subprocess.Popen", return_value=process),
            patch("boundver._git.MAX_GIT_DIAGNOSTIC_BYTES", 4),
        ):
            with self.assertRaisesRegex(
                GuardrailError,
                "Git command stderr exceeds the 4-byte limit",
            ):
                git_helpers._git_run(Path("repo"), ["write-tree"])

        self.assertTrue(process.killed)
        self.assertTrue(stderr.closed)
        self.assertEqual(stderr.read_sizes, [5])

    def test_oversized_stdout_is_killed_at_one_byte_sentinel(self):
        stdout = _AdversarialPipe()
        process = _FakeProcess(stdout, io.BytesIO())
        with (
            patch("boundver._git.subprocess.Popen", return_value=process),
            patch("boundver._git.MAX_GIT_COMMAND_OUTPUT_BYTES", 4),
        ):
            with self.assertRaisesRegex(
                GuardrailError,
                "Git command stdout exceeds the 4-byte limit",
            ):
                git_helpers._git_run(Path("repo"), ["rev-parse", "HEAD"])

        self.assertTrue(process.killed)
        self.assertTrue(stdout.closed)
        self.assertEqual(stdout.read_sizes, [5])

    def test_index_capture_surfaces_terminal_safe_write_tree_stderr(self):
        failure = subprocess.CalledProcessError(
            128,
            ["git", "write-tree"],
            stderr=b"svc/conflict: unmerged\n\x1b[31mfatal: index is not clean",
        )
        with (
            patch("boundver._git._resolve_head_oid", return_value="a" * 40),
            patch("boundver._git._git_run", side_effect=failure),
        ):
            with self.assertRaises(ValueError) as raised:
                git_helpers._capture_git_source_snapshot(Path("repo"), "index")

        message = str(raised.exception)
        self.assertIs(raised.exception.__cause__, failure)
        self.assertIn("git write-tree failed", message)
        self.assertIn("return code 128", message)
        self.assertIn("unmerged", message)
        self.assertIn("\\n", message)
        self.assertIn("\\x1b", message)
        self.assertNotIn("\n", message)
        self.assertNotIn("\x1b", message)

    def test_git_failure_detail_is_bounded_and_handles_empty_stderr(self):
        oversized = subprocess.CalledProcessError(
            7,
            ["git", "write-tree"],
            stderr="x" * 100_000,
        )
        detail = git_helpers._git_failure_detail(oversized)
        self.assertIn("return code 7", detail)
        self.assertIn("...", detail)
        self.assertLessEqual(
            len(detail), git_helpers.MAX_GIT_FAILURE_DETAIL_CHARS + 64
        )

        empty = subprocess.CalledProcessError(
            9,
            ["git", "write-tree"],
            stderr=b" \r\n ",
        )
        self.assertEqual(
            git_helpers._git_failure_detail(empty),
            "return code 9; no stderr diagnostic",
        )

    def test_git_failure_detail_preserves_invalid_filesystem_bytes_safely(self):
        failure = subprocess.CalledProcessError(
            3,
            ["git", "write-tree"],
            stderr=b"fatal: bad byte \xff\n",
        )
        with patch("boundver._git.sys.getfilesystemencoding", return_value="utf-8"):
            detail = git_helpers._git_failure_detail(failure)

        self.assertIn("fatal", detail)
        self.assertIn("\\xff", detail)
        self.assertNotIn("\n", detail)

    def test_index_capture_surfaces_bounded_os_errors(self):
        failure = PermissionError(13, "object database is read-only")
        with (
            patch("boundver._git._resolve_head_oid", return_value="a" * 40),
            patch("boundver._git._git_run", side_effect=failure),
        ):
            with self.assertRaises(ValueError) as raised:
                git_helpers._capture_git_source_snapshot(Path("repo"), "index")

        message = str(raised.exception)
        self.assertIs(raised.exception.__cause__, failure)
        self.assertIn("PermissionError", message)
        self.assertIn("read-only", message)

    def test_index_capture_rejects_malformed_tree_oid_before_listing(self):
        result = subprocess.CompletedProcess(
            ["git", "write-tree"], 0, "x" * 100_000, ""
        )
        with (
            patch("boundver._git._resolve_head_oid", return_value="a" * 40),
            patch("boundver._git._git_run", return_value=result),
            patch("boundver._git._iter_git_nul_records") as listing,
        ):
            with self.assertRaises(ValueError) as raised:
                git_helpers._capture_git_source_snapshot(Path("repo"), "index")

        self.assertIn("malformed object ID", str(raised.exception))
        self.assertLess(len(str(raised.exception)), 512)
        listing.assert_not_called()

    def test_index_capture_surfaces_streamed_ls_tree_stderr(self):
        listing_failure = subprocess.CalledProcessError(
            128,
            ["git", "ls-tree"],
            stderr=b"fatal: missing tree object\n",
        )
        write_tree = subprocess.CompletedProcess(
            ["git", "write-tree"], 0, "b" * 40 + "\n", ""
        )
        with (
            patch("boundver._git._resolve_head_oid", return_value="a" * 40),
            patch("boundver._git._git_run", return_value=write_tree),
            patch(
                "boundver._git._iter_git_nul_records",
                side_effect=listing_failure,
            ),
        ):
            with self.assertRaises(ValueError) as raised:
                git_helpers._capture_git_source_snapshot(Path("repo"), "index")

        message = str(raised.exception)
        self.assertIs(raised.exception.__cause__, listing_failure)
        self.assertIn("git ls-tree failed", message)
        self.assertIn("missing tree object", message)

    def test_index_capture_rejects_concurrent_index_change(self):
        first_tree = subprocess.CompletedProcess(
            ["git", "write-tree"], 0, "a" * 40 + "\n", ""
        )
        changed_tree = subprocess.CompletedProcess(
            ["git", "write-tree"], 0, "b" * 40 + "\n", ""
        )
        with (
            patch("boundver._git._resolve_head_oid", return_value="c" * 40),
            patch(
                "boundver._git._git_run",
                side_effect=[first_tree, changed_tree],
            ),
            patch(
                "boundver._git._iter_git_nul_records",
                side_effect=[iter(()), iter(())],
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "Index changed while capturing tracked paths",
            ):
                git_helpers._capture_git_source_snapshot(Path("repo"), "index")

    def test_resolve_head_distinguishes_unborn_from_operational_failure(self):
        unresolved = subprocess.CalledProcessError(
            1,
            ["git", "rev-parse"],
            stderr="",
        )
        symbolic = subprocess.CompletedProcess(
            ["git", "symbolic-ref"], 0, "refs/heads/main\n", ""
        )
        absent_ref = subprocess.CalledProcessError(
            1,
            ["git", "show-ref"],
            stderr="",
        )
        valid_ref = subprocess.CompletedProcess(
            ["git", "check-ref-format"], 0, "", ""
        )
        with patch(
            "boundver._git._git_run",
            side_effect=[unresolved, symbolic, valid_ref, absent_ref],
        ):
            self.assertIsNone(git_helpers._resolve_head_oid(Path("repo")))

        failure = subprocess.CalledProcessError(
            128,
            ["git", "rev-parse"],
            stderr="fatal: corrupt object database\n",
        )
        with patch("boundver._git._git_run", side_effect=failure):
            with self.assertRaises(ValueError) as raised:
                git_helpers._resolve_head_oid(Path("repo"))

        self.assertIs(raised.exception.__cause__, failure)
        self.assertIn("return code 128", str(raised.exception))
        self.assertIn("corrupt object database", str(raised.exception))

        noisy_return_one = subprocess.CalledProcessError(
            1,
            ["git", "rev-parse"],
            stderr="fatal: cannot resolve object store\n",
        )
        with patch("boundver._git._git_run", side_effect=noisy_return_one):
            with self.assertRaises(ValueError) as raised:
                git_helpers._resolve_head_oid(Path("repo"))

        self.assertIn("cannot resolve object store", str(raised.exception))

    def test_resolve_head_does_not_hide_unpeelable_existing_or_detached_head(self):
        unresolved = subprocess.CalledProcessError(
            1,
            ["git", "rev-parse"],
            stderr="",
        )
        symbolic = subprocess.CompletedProcess(
            ["git", "symbolic-ref"], 0, "refs/heads/main\n", ""
        )
        existing_ref = subprocess.CompletedProcess(
            ["git", "show-ref"], 0, "", ""
        )
        valid_ref = subprocess.CompletedProcess(
            ["git", "check-ref-format"], 0, "", ""
        )
        peel_failure = subprocess.CalledProcessError(
            128,
            ["git", "rev-parse"],
            stderr="fatal: bad object HEAD\n",
        )
        with patch(
            "boundver._git._git_run",
            side_effect=[
                unresolved,
                symbolic,
                valid_ref,
                existing_ref,
                peel_failure,
            ],
        ):
            with self.assertRaises(ValueError) as raised:
                git_helpers._resolve_head_oid(Path("repo"))

        self.assertIs(raised.exception.__cause__, peel_failure)
        self.assertIn("HEAD is not unborn", str(raised.exception))
        self.assertIn("bad object HEAD", str(raised.exception))

        detached = subprocess.CalledProcessError(
            1,
            ["git", "symbolic-ref"],
            stderr="",
        )
        with patch(
            "boundver._git._git_run",
            side_effect=[unresolved, detached, peel_failure],
        ):
            with self.assertRaises(ValueError) as raised:
                git_helpers._resolve_head_oid(Path("repo"))

        self.assertIs(raised.exception.__cause__, peel_failure)
        self.assertIn("bad object HEAD", str(raised.exception))

    def test_resolve_head_rejects_non_branch_or_invalid_symbolic_targets(self):
        unresolved = subprocess.CalledProcessError(
            1,
            ["git", "rev-parse"],
            stderr="",
        )
        non_branch = subprocess.CompletedProcess(
            ["git", "symbolic-ref"], 0, "refs/tags/missing\n", ""
        )
        with patch(
            "boundver._git._git_run",
            side_effect=[unresolved, non_branch],
        ):
            with self.assertRaises(ValueError) as raised:
                git_helpers._resolve_head_oid(Path("repo"))

        self.assertIn("malformed HEAD ref", str(raised.exception))

        invalid_branch = subprocess.CompletedProcess(
            ["git", "symbolic-ref"], 0, "refs/heads/x..y\n", ""
        )
        invalid_format = subprocess.CalledProcessError(
            1,
            ["git", "check-ref-format"],
            stderr="",
        )
        with patch(
            "boundver._git._git_run",
            side_effect=[unresolved, invalid_branch, invalid_format],
        ):
            with self.assertRaises(ValueError) as raised:
                git_helpers._resolve_head_oid(Path("repo"))

        self.assertIn("invalid HEAD branch ref", str(raised.exception))

    def test_resolve_head_surfaces_unborn_classification_failure(self):
        unresolved = subprocess.CalledProcessError(
            1,
            ["git", "rev-parse"],
            stderr="",
        )
        classification_failure = subprocess.CalledProcessError(
            128,
            ["git", "symbolic-ref"],
            stderr="fatal: cannot read HEAD\n",
        )
        with patch(
            "boundver._git._git_run",
            side_effect=[unresolved, classification_failure],
        ):
            with self.assertRaises(ValueError) as raised:
                git_helpers._resolve_head_oid(Path("repo"))

        message = str(raised.exception)
        self.assertIs(raised.exception.__cause__.__cause__, classification_failure)
        self.assertIn("unborn-HEAD check failed", message)
        self.assertIn("cannot read HEAD", message)

    def test_resolve_head_rejects_malformed_success_oid(self):
        result = subprocess.CompletedProcess(
            ["git", "rev-parse"], 0, "not-an-object-id\n", ""
        )
        with patch("boundver._git._git_run", return_value=result):
            with self.assertRaisesRegex(ValueError, "malformed object ID"):
                git_helpers._resolve_head_oid(Path("repo"))

    def test_repository_probe_failure_uses_only_established_git_marker(self):
        failure = subprocess.CalledProcessError(
            128,
            ["git", "rev-parse"],
            stderr="fatal: I/O error\n",
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch("boundver._git._git_run", side_effect=failure):
                self.assertFalse(git_helpers._is_git_repository(root))

            (root / ".git").mkdir()
            with patch("boundver._git._git_run", side_effect=failure):
                self.assertTrue(git_helpers._is_git_repository(root))

    def test_working_tree_accessor_preserves_confirmed_non_git_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "tracked.txt").write_text("filesystem fallback\n", encoding="utf-8")

            accessor = _SourceAccessor(root, "working-tree")

        self.assertIsNone(accessor.snapshot)
        self.assertIsNone(accessor.head_oid)

    def test_unset_core_filemode_defaults_true(self):
        write_tree = subprocess.CompletedProcess(
            ["git", "write-tree"], 0, "b" * 40 + "\n", ""
        )
        unset = subprocess.CalledProcessError(
            1,
            ["git", "config"],
            stderr="",
        )
        with (
            patch("boundver._git._resolve_head_oid", return_value="a" * 40),
            patch(
                "boundver._git._git_run",
                side_effect=[write_tree, write_tree, unset],
            ),
            patch("boundver._git._iter_git_nul_records", return_value=iter(())),
        ):
            snapshot = git_helpers._capture_git_source_snapshot(
                Path("repo"), "index"
            )

        self.assertTrue(snapshot.filemode)

    def test_core_filemode_operational_failure_is_not_suppressed(self):
        write_tree = subprocess.CompletedProcess(
            ["git", "write-tree"], 0, "b" * 40 + "\n", ""
        )
        failure = subprocess.CalledProcessError(
            128,
            ["git", "config"],
            stderr="fatal: cannot read repository config\n",
        )
        with (
            patch("boundver._git._resolve_head_oid", return_value="a" * 40),
            patch(
                "boundver._git._git_run",
                side_effect=[write_tree, write_tree, failure],
            ),
            patch("boundver._git._iter_git_nul_records", return_value=iter(())),
        ):
            with self.assertRaises(ValueError) as raised:
                git_helpers._capture_git_source_snapshot(Path("repo"), "index")

        self.assertIs(raised.exception.__cause__, failure)
        self.assertIn("Cannot read Git core.filemode", str(raised.exception))
        self.assertIn("cannot read repository config", str(raised.exception))

        noisy_return_one = subprocess.CalledProcessError(
            1,
            ["git", "config"],
            output="unexpected output\n",
            stderr="",
        )
        with (
            patch("boundver._git._resolve_head_oid", return_value="a" * 40),
            patch(
                "boundver._git._git_run",
                side_effect=[write_tree, write_tree, noisy_return_one],
            ),
            patch("boundver._git._iter_git_nul_records", return_value=iter(())),
        ):
            with self.assertRaises(ValueError) as raised:
                git_helpers._capture_git_source_snapshot(Path("repo"), "index")

        self.assertIs(raised.exception.__cause__, noisy_return_one)


if __name__ == "__main__":
    unittest.main()
