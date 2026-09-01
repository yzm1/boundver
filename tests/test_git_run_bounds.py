"""Hard-bound regressions for small Git command capture."""

import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import boundver._git as git_helpers
from boundver._lockfile import _SourceAccessor
from boundver._utils import GuardrailError


class _FakeProcess:
    def __init__(self, stdout, stderr, *, returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.killed = False

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
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


class _HangingProcess:
    """Expose finite pipes while refusing to exit until the deadline kills it."""

    def __init__(self):
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.returncode = None
        self.killed = False
        self._finished = threading.Event()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._finished.wait(timeout):
            raise subprocess.TimeoutExpired(["git"], timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._finished.set()


class GitRunBoundTests(unittest.TestCase):
    def tearDown(self):
        git_helpers._ambient_worktree_config_overrides.cache_clear()
        git_helpers._repository_filter_config_overrides.cache_clear()
        super().tearDown()

    def test_filesystem_git_root_rejects_windows_reparse_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            with patch(
                "boundver._git._is_windows_reparse_point", return_value=True
            ), self.assertRaisesRegex(ValueError, "junction, or reparse point"):
                git_helpers._filesystem_git_root(root)

    def test_filesystem_git_root_accepts_plain_linked_worktree_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").write_text(
                "gitdir: ../repository/.git/worktrees/example\n",
                encoding="utf-8",
            )

            self.assertEqual(git_helpers._filesystem_git_root(root), root.resolve())

    def test_success_returns_text_completed_process(self):
        process = _FakeProcess(
            io.BytesIO(b"object-id\r\n"),
            io.BytesIO(b"warning\r"),
        )
        with patch("boundver._git.subprocess.Popen", return_value=process) as popen:
            result = git_helpers._git_run(Path("repo"), ["rev-parse", "HEAD"])

        command = git_helpers._git_command(Path("repo"), "rev-parse", "HEAD")
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual(result.args, command)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "object-id\n")
        self.assertEqual(result.stderr, "warning\n")
        popen.assert_called_once_with(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_helpers._git_subprocess_env(Path("repo")),
        )

    def test_git_environment_is_offline_noninteractive_and_replace_free(self):
        hostile = {
            "GIT_CONFIG_KEY_99": "credential.helper",
            "GIT_CONFIG_PARAMETERS": "'core.fsmonitor'='malicious'",
            "GIT_CONFIG": "alternate-config",
            "GIT_DIR": "elsewhere.git",
            "GIT_EXEC_PATH": "repo-tools",
            "GIT_GRAFT_FILE": "grafts",
            "GIT_INDEX_FILE": "alternate-index",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_OBJECT_DIRECTORY": "alternate-objects",
            "GIT_TRACE2_EVENT": "trace.json",
            "GIT_FUTURE_REDIRECT": "future-control",
        }
        with patch.dict(os.environ, hostile, clear=False):
            environment = git_helpers._git_subprocess_env()
        self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["GCM_INTERACTIVE"], "never")
        self.assertEqual(environment["GIT_PAGER"], "cat")
        self.assertEqual(environment["GIT_CONFIG_KEY_0"], "core.hooksPath")
        self.assertEqual(environment["GIT_CONFIG_VALUE_0"], git_helpers.os.devnull)
        self.assertEqual(environment["GIT_CONFIG_KEY_1"], "core.fsmonitor")
        self.assertEqual(environment["GIT_CONFIG_VALUE_1"], "false")
        self.assertEqual(environment["GIT_CONFIG_KEY_2"], "diff.ignoreSubmodules")
        self.assertEqual(environment["GIT_CONFIG_VALUE_2"], "dirty")
        self.assertEqual(environment["GIT_CONFIG_KEY_3"], "status.submoduleSummary")
        self.assertEqual(environment["GIT_CONFIG_VALUE_3"], "false")
        self.assertEqual(environment["GIT_CONFIG_KEY_4"], "submodule.recurse")
        self.assertEqual(environment["GIT_CONFIG_VALUE_4"], "false")
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], git_helpers.os.devnull)
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_ATTR_NOSYSTEM"], "1")
        for name in hostile:
            self.assertNotIn(name, environment)

    def test_git_environment_promotes_only_prevalidated_worktree_semantics(self):
        with patch(
            "boundver._git._ambient_worktree_config_overrides",
            return_value=(
                ("core.autocrlf", "true"),
                ("core.eol", "crlf"),
            ),
        ):
            environment = git_helpers._git_subprocess_env(Path("repo"))

        self.assertEqual(environment["GIT_CONFIG_COUNT"], "8")
        self.assertEqual(environment["GIT_CONFIG_KEY_6"], "core.autocrlf")
        self.assertEqual(environment["GIT_CONFIG_VALUE_6"], "true")
        self.assertEqual(environment["GIT_CONFIG_KEY_7"], "core.eol")
        self.assertEqual(environment["GIT_CONFIG_VALUE_7"], "crlf")

    def test_ambient_config_query_ignores_local_and_invalid_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / ".git").mkdir()
            response = subprocess.CompletedProcess(
                ["git", "config"],
                0,
                (
                    "system\0core.autocrlf\ntrue\0"
                    "global\0core.eol\ncrlf\0"
                    "local\0core.autocrlf\ninvalid\0"
                    "local\0core.filemode\nfalse\0"
                ),
                "",
            )
            with patch("boundver._git._git_run", return_value=response) as run:
                overrides = git_helpers._ambient_worktree_config_overrides(
                    str(root)
                )

        self.assertEqual(overrides, (("core.eol", "crlf"),))
        query = run.call_args.args[1]
        self.assertIn("--no-includes", query)
        self.assertIn("--show-scope", query)
        query_environment = run.call_args.kwargs["environment"]
        self.assertNotIn("GIT_CONFIG_GLOBAL", query_environment)
        self.assertNotIn("GIT_CONFIG_NOSYSTEM", query_environment)
        self.assertEqual(
            run.call_args.kwargs["deadline_seconds"],
            git_helpers.MAX_GIT_CONFIG_QUERY_SECONDS,
        )

    def test_repository_filter_query_neutralizes_every_driver_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / ".git").mkdir()
            response = subprocess.CompletedProcess(
                ["git", "config"],
                0,
                (
                    "filter.evil.clean\0"
                    "filter.evil.required\0"
                    "filter.LFS.process\0"
                ),
                "",
            )
            with patch("boundver._git._git_run", return_value=response) as run:
                overrides = git_helpers._repository_filter_config_overrides(
                    str(root)
                )

        self.assertEqual(
            overrides,
            (
                ("filter.evil.clean", ""),
                ("filter.evil.smudge", ""),
                ("filter.evil.process", ""),
                ("filter.evil.required", "false"),
                ("filter.LFS.clean", ""),
                ("filter.LFS.smudge", ""),
                ("filter.LFS.process", ""),
                ("filter.LFS.required", "false"),
            ),
        )
        query = run.call_args.args[1]
        self.assertIn("--includes", query)
        self.assertIn("--name-only", query)
        environment = run.call_args.kwargs["environment"]
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")

    def test_repository_clean_filter_cannot_execute_during_git_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "audit@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Audit"],
                check=True,
            )
            helper = root / "filter.py"
            helper.write_text(
                "import pathlib, sys\n"
                "pathlib.Path('filter-ran').write_text('executed')\n"
                "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
                encoding="utf-8",
            )
            (root / ".gitattributes").write_text(
                "*.txt filter=hostile\n",
                encoding="utf-8",
            )
            target = root / "data.txt"
            target.write_text("initial\n", encoding="utf-8")
            filter_command = f'"{sys.executable}" filter.py'
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "config",
                    "filter.hostile.clean",
                    filter_command,
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "config",
                    "filter.hostile.smudge",
                    "cat",
                ],
                check=True,
            )
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "--quiet", "-m", "initial"],
                check=True,
            )
            marker = root / "filter-ran"
            marker.unlink(missing_ok=True)
            target.write_text("changed\n", encoding="utf-8")

            status = git_helpers._git_run(
                root,
                ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            )

            self.assertIn("data.txt", status.stdout)
            self.assertFalse(marker.exists())
            environment = git_helpers._git_subprocess_env(root)
            configured = {
                environment[f"GIT_CONFIG_KEY_{index}"]:
                environment[f"GIT_CONFIG_VALUE_{index}"]
                for index in range(int(environment["GIT_CONFIG_COUNT"]))
            }
            self.assertEqual(configured["filter.hostile.clean"], "")
            self.assertEqual(configured["filter.hostile.smudge"], "")
            self.assertEqual(configured["filter.hostile.process"], "")
            self.assertEqual(configured["filter.hostile.required"], "false")

    def test_submodule_clean_filter_cannot_execute_during_git_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source"
            repository = root / "repository"
            subprocess.run(["git", "init", "--quiet", str(source)], check=True)
            subprocess.run(
                ["git", "-C", str(source), "config", "user.email", "audit@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "config", "user.name", "Audit"],
                check=True,
            )
            (source / ".gitattributes").write_text(
                "*.txt filter=hostile\n",
                encoding="utf-8",
            )
            (source / "filter.py").write_text(
                "import pathlib, sys\n"
                "pathlib.Path('filter-ran').write_text('executed')\n"
                "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
                encoding="utf-8",
            )
            (source / "data.txt").write_text("initial\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(source), "commit", "--quiet", "-m", "source"],
                check=True,
            )
            first_commit = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            (source / "revision.md").write_text("second\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "revision.md"], check=True)
            subprocess.run(
                ["git", "-C", str(source), "commit", "--quiet", "-m", "second"],
                check=True,
            )

            subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "config",
                    "user.email",
                    "audit@example.invalid",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.name", "Audit"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "protocol.file.allow=always",
                    "-C",
                    str(repository),
                    "submodule",
                    "add",
                    "--quiet",
                    str(source),
                    "nested",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "commit", "--quiet", "-m", "super"],
                check=True,
            )
            checkout = repository / "nested"
            filter_command = f'"{sys.executable}" filter.py'
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "config",
                    "filter.hostile.clean",
                    filter_command,
                ],
                check=True,
            )
            (checkout / "data.txt").write_text("changed\n", encoding="utf-8")
            marker = checkout / "filter-ran"

            status = git_helpers._git_run(
                repository,
                ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            )

            self.assertEqual(status.returncode, 0)
            self.assertEqual(status.stdout, "")
            self.assertFalse(marker.exists())

            (checkout / "data.txt").write_text("initial\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(checkout), "checkout", "--quiet", first_commit],
                check=True,
            )
            marker.unlink(missing_ok=True)
            changed_gitlink = git_helpers._git_run(
                repository,
                ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            )

            self.assertIn("nested", changed_gitlink.stdout)
            self.assertFalse(marker.exists())

    def test_exact_repository_is_the_only_process_local_safe_directory(self):
        repository = Path("repo")
        environment = git_helpers._git_subprocess_env(repository)
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "6")
        self.assertEqual(environment["GIT_CONFIG_KEY_5"], "safe.directory")
        self.assertEqual(
            environment["GIT_CONFIG_VALUE_5"],
            str(repository.resolve(strict=False)),
        )

    def test_ambient_git_config_cannot_redirect_config_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            alternate = root / "alternate.config"
            subprocess.run(
                ["git", "init", "--quiet", str(repository)],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "config",
                    "core.filemode",
                    "true",
                ],
                check=True,
            )
            alternate.write_text("[core]\n\tfilemode = false\n", encoding="utf-8")

            with patch.dict(os.environ, {"GIT_CONFIG": str(alternate)}):
                result = git_helpers._git_run(
                    repository,
                    ["config", "--bool", "core.filemode"],
                )

        self.assertEqual(result.stdout.strip(), "true")

    def test_repository_local_git_executable_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            executable = repo / "git.exe"
            executable.write_bytes(b"not git")
            with patch(
                "boundver._git.shutil.which", return_value=str(executable)
            ), self.assertRaisesRegex(ValueError, "inside the inspected repository"):
                git_helpers._trusted_git_executable(repo)

    def test_repository_local_core_worktree_cannot_redirect_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            outside = root / "outside"
            repository.mkdir()
            outside.mkdir()
            subprocess.run(
                ["git", "init", "--quiet", str(repository)],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "config",
                    "core.worktree",
                    str(outside),
                ],
                check=True,
            )
            nested = repository / "nested"
            nested.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(nested)
                self.assertEqual(git_helpers.git_root(), repository.resolve())
            finally:
                os.chdir(previous)

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
            git_helpers._git_command(Path("repo"), "write-tree"),
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

    def test_streaming_git_stderr_never_spools_past_the_diagnostic_limit(self):
        stderr = _AdversarialPipe()
        process = _FakeProcess(io.BytesIO(b"path.txt\0"), stderr)
        with (
            patch("boundver._git.subprocess.Popen", return_value=process),
            patch("boundver._git.MAX_GIT_DIAGNOSTIC_BYTES", 4),
        ):
            with self.assertRaisesRegex(
                GuardrailError,
                "Git command stderr exceeds the 4-byte limit",
            ):
                list(
                    git_helpers._iter_git_nul_records(
                        Path("repo"), ["ls-files", "-z"]
                    )
                )

        self.assertTrue(process.killed)
        self.assertTrue(stderr.closed)
        self.assertEqual(stderr.read_sizes, [5])

    def test_stalled_git_command_is_killed_at_wall_clock_deadline(self):
        process = _HangingProcess()
        with (
            patch("boundver._git.subprocess.Popen", return_value=process),
            patch("boundver._git.MAX_GIT_COMMAND_SECONDS", 0.02),
        ):
            with self.assertRaisesRegex(
                GuardrailError,
                "Git command exceeds the 0.02-second wall-clock limit",
            ):
                git_helpers._git_run(Path("repo"), ["rev-parse", "HEAD"])

        self.assertTrue(process.killed)
        self.assertEqual(process.returncode, -9)

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
            patch("boundver._git._git_run", side_effect=[write_tree, unset]),
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
            patch("boundver._git._git_run", side_effect=[write_tree, failure]),
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
                side_effect=[write_tree, noisy_return_one],
            ),
            patch("boundver._git._iter_git_nul_records", return_value=iter(())),
        ):
            with self.assertRaises(ValueError) as raised:
                git_helpers._capture_git_source_snapshot(Path("repo"), "index")

        self.assertIs(raised.exception.__cause__, noisy_return_one)


if __name__ == "__main__":
    unittest.main()
