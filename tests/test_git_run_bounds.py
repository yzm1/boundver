"""Hard-bound regressions for small Git command capture."""

import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import boundver._git as git_helpers
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
            ["git", "-C", "repo", "rev-parse", "HEAD"],
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "object-id\n")
        self.assertEqual(result.stderr, "warning\n")
        popen.assert_called_once_with(
            ["git", "-C", "repo", "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

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
        self.assertEqual(error.cmd, ["git", "-C", "repo", "write-tree"])
        self.assertEqual(error.output, "partial\n")
        self.assertEqual(error.stdout, "partial\n")
        self.assertEqual(error.stderr, "failure\n")

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


if __name__ == "__main__":
    unittest.main()
