"""Contracts for the shared release-candidate verification sequence."""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests._project_metadata import CURRENT_TAG, CURRENT_VERSION


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify_release_candidate.py"
PLATFORM_HELPER = REPO_ROOT / "scripts" / "_release_platform.py"
TAG = CURRENT_TAG
SHA = "1" * 40


def _load_script():
    spec = importlib.util.spec_from_file_location("verify_release_candidate", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_platform_helper():
    spec = importlib.util.spec_from_file_location("release_platform", PLATFORM_HELPER)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {PLATFORM_HELPER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class VerifyReleaseCandidateTests(unittest.TestCase):
    def test_signal_and_cancellation_exits_are_not_ordinary_gate_failures(self):
        verifier = _load_script()
        for returncode in (-9, -15, -2, 125, 130, 143):
            with self.subTest(returncode=returncode):
                process = mock.Mock(returncode=returncode)
                process.communicate.return_value = (None, None)
                with mock.patch.object(verifier.subprocess, "Popen", return_value=process), mock.patch.object(
                    verifier, "_terminate_command_tree"
                ):
                    with self.assertRaises(verifier.UnsafeCandidateCleanupError):
                        verifier._run([sys.executable], cwd=REPO_ROOT, env={})

    def test_exit_classification_preserves_ordinary_failure_codes(self):
        helper = _load_platform_helper()
        for returncode in (None, 0, 1, 2, 7, 126, 128, 129):
            with self.subTest(returncode=returncode):
                self.assertFalse(helper.uncertain_command_exit(returncode))

    def test_uncertain_cleanup_has_distinct_exit_status(self):
        verifier = _load_script()
        for error, expected in (
            (verifier.CandidateVerificationError("ordinary failure"), 1),
            (verifier.UnsafeCandidateCleanupError("uncertain containment"), 125),
        ):
            with self.subTest(expected=expected), mock.patch.object(
                verifier, "verify_candidate", side_effect=error
            ), mock.patch.object(verifier.sys, "stderr"):
                self.assertEqual(
                    verifier.main(["--tag", TAG, "--release-sha", SHA]), expected
                )

    def test_windows_tree_failure_is_unsafe_even_after_root_exit(self):
        import ctypes

        helper = _load_platform_helper()
        process = mock.Mock(pid=12345)
        process.poll.return_value = 0

        def system_directory(buffer, size):
            buffer.value = r"C:\Windows\System32"
            return len(buffer.value)

        windows = SimpleNamespace(
            kernel32=SimpleNamespace(GetSystemDirectoryW=system_directory)
        )
        with mock.patch.object(helper, "os", SimpleNamespace(name="nt")), mock.patch.object(
            ctypes, "windll", windows, create=True
        ), mock.patch.object(
            helper.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)
        ):
            with self.assertRaisesRegex(RuntimeError, "could not be terminated"):
                helper.terminate_windows_process_tree(process)

    def test_cleanup_drain_interruption_is_unsafe(self):
        verifier = _load_script()
        process = mock.Mock()
        process.communicate.side_effect = [KeyboardInterrupt(), KeyboardInterrupt()]
        with mock.patch.object(verifier.subprocess, "Popen", return_value=process), mock.patch.object(
            verifier, "_terminate_command_tree"
        ):
            with self.assertRaises(verifier.UnsafeCandidateCleanupError):
                verifier._run([sys.executable], cwd=REPO_ROOT, env={})

    @unittest.skipUnless(os.name == "posix", "POSIX terminal interruption")
    def test_sigint_stops_detached_command_before_checkout_cleanup(self):
        self._assert_signal_contained(signal.SIGINT)

    @unittest.skipUnless(os.name == "posix", "POSIX termination")
    def test_sigterm_stops_detached_command_before_checkout_cleanup(self):
        self._assert_signal_contained(signal.SIGTERM)

    @unittest.skipUnless(os.name == "posix", "POSIX termination")
    def test_termination_signals_during_spawn_and_cleanup_are_deferred_until_reap(self):
        verifier = _load_script()
        previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signum=signum):
                process = mock.Mock()
                process.communicate.return_value = (None, None)

                def spawning(*args, **kwargs):
                    signal.raise_signal(signum)
                    return process

                with mock.patch.object(verifier.subprocess, "Popen", side_effect=spawning), mock.patch.object(
                    verifier, "_terminate_command_tree", side_effect=lambda _: signal.raise_signal(signum)
                ) as terminate:
                    with self.assertRaises(verifier.UnsafeCandidateCleanupError):
                        verifier._run([sys.executable], cwd=REPO_ROOT, env={})
                terminate.assert_called_once_with(process)
                process.communicate.assert_called_once_with(timeout=5)
                for saved_signum, handler in previous.items():
                    self.assertEqual(signal.getsignal(saved_signum), handler)

    def _assert_signal_contained(self, termination_signal):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ready = root / "ready"
            marker = root / "survived"
            child = (
                "import time; from pathlib import Path; "
                f"Path({str(ready)!r}).touch(); time.sleep(3); "
                f"Path({str(marker)!r}).touch()"
            )
            worker_code = (
                "import importlib.util, os, sys; from pathlib import Path; "
                f"spec=importlib.util.spec_from_file_location('verifier', {str(SCRIPT)!r}); "
                "verifier=importlib.util.module_from_spec(spec); spec.loader.exec_module(verifier)\n"
                "try:\n"
                f" verifier._run([sys.executable, '-I', '-c', {child!r}], "
                f"cwd=Path({str(root)!r}), env=os.environ, timeout_seconds=30)\n"
                "except KeyboardInterrupt:\n raise SystemExit(0)\n"
                "except verifier.UnsafeCandidateCleanupError:\n raise SystemExit(0)\n"
                "except SystemExit as error:\n"
                " if error.code == 143: raise SystemExit(0)\n"
                " raise\n"
                "raise SystemExit('interruption was not propagated')\n"
            )
            worker = subprocess.Popen(
                [sys.executable, "-I", "-c", worker_code],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and worker.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "detached command never started")
                worker.send_signal(termination_signal)
                _, stderr = worker.communicate(timeout=10)
                self.assertEqual(worker.returncode, 0, stderr)
                time.sleep(3)
                self.assertFalse(marker.exists(), "detached command survived cancellation")
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait(timeout=5)

    def test_interruption_and_communication_error_terminate_and_reap(self):
        verifier = _load_script()
        for failure in (KeyboardInterrupt(), OSError("communication failed")):
            with self.subTest(failure=type(failure).__name__):
                process = mock.Mock()
                process.communicate.side_effect = [failure, (None, None)]
                with mock.patch.object(verifier.subprocess, "Popen", return_value=process), mock.patch.object(
                    verifier, "_terminate_command_tree"
                ) as terminate:
                    expected = verifier.UnsafeCandidateCleanupError if os.name == "posix" else type(failure)
                    with self.assertRaises(expected):
                        verifier._run([sys.executable], cwd=REPO_ROOT, env={})
                terminate.assert_called_once_with(process)
                self.assertEqual(process.communicate.call_args_list, [
                    mock.call(timeout=verifier.MAX_COMMAND_SECONDS), mock.call(timeout=5)
                ])

    def test_timeout_terminates_descendant_before_it_can_modify_checkout(self):
        verifier = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "surviving-child"
            ready = root / "child-started"
            child = (
                "import time; from pathlib import Path; "
                f"Path({str(ready)!r}).touch(); time.sleep(3); "
                f"Path({str(marker)!r}).touch()"
            )
            parent = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-I', '-c', {child!r}]); "
                "time.sleep(30)"
            )
            # Start the deliberately short timeout only after the child is
            # ready. A saturated host can take longer than one second merely
            # to start the Windows virtualenv redirector and two interpreters.
            original_popen = verifier.subprocess.Popen

            def spawn_ready(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                if args[0][0] == sys.executable:
                    deadline = time.monotonic() + 15
                    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if not ready.exists():
                        verifier._terminate_command_tree(process)
                        process.communicate(timeout=5)
                        self.fail("regression child never started")
                return process

            started = time.monotonic()
            with mock.patch.object(verifier.subprocess, "Popen", side_effect=spawn_ready), self.assertRaisesRegex(
                verifier.CandidateVerificationError, "timed out"
            ):
                verifier._run(
                    [sys.executable, "-I", "-c", parent],
                    cwd=root,
                    env=os.environ,
                    capture_output=True,
                    timeout_seconds=1,
                )
            # Include the bounded Windows tree terminator and pipe-drain grace;
            # correctness comes from the absent child write, not spawn speed.
            self.assertLess(time.monotonic() - started, 40)
            self.assertTrue(ready.exists(), "regression child never started")
            time.sleep(3)
            self.assertFalse(marker.exists(), "descendant survived the phase timeout")

    def test_command_failure_retains_exit_status_and_captured_diagnostic(self):
        verifier = _load_script()
        with self.assertRaisesRegex(verifier.CandidateVerificationError, "fixture failure"):
            verifier._run(
                [sys.executable, "-I", "-c", "import sys; sys.exit('fixture failure')"],
                cwd=REPO_ROOT,
                env=os.environ,
                capture_output=True,
            )

    def test_isolated_direct_startup_loads_adjacent_platform_helper(self):
        result = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("release candidate", result.stdout.lower())

    def test_release_tag_numeric_identifiers_are_ascii_only(self):
        verifier = _load_script()
        with self.assertRaisesRegex(
            verifier.CandidateVerificationError,
            "exact vMAJOR.MINOR.PATCH",
        ):
            verifier.verify_candidate(REPO_ROOT, "v1.2.\u0663", SHA)

    def test_windows_prefers_git_bash_over_wsl_bash(self):
        platform = _load_platform_helper()
        git = r"C:\Program Files\Git\cmd\git.exe"
        git_bash = r"C:\Program Files\Git\bin\bash.exe"

        def which(command, *, path):
            self.assertEqual(path, "safe-tools")
            return git if command == "git" else r"C:\Windows\System32\bash.exe"

        with mock.patch.object(
            platform.shutil, "which", side_effect=which
        ) as finder, mock.patch.object(
                platform.os.path,
                "isfile",
                side_effect=lambda candidate: candidate in {git, git_bash},
        ):
            bash = platform.resolve_bash("safe-tools", platform_name="nt")

        self.assertEqual(bash, git_bash)
        finder.assert_called_once_with("git", path="safe-tools")

    def test_windows_rejects_wsl_bash_when_git_bash_is_unavailable(self):
        platform = _load_platform_helper()

        def which(command, *, path):
            self.assertEqual(path, "safe-tools")
            if command == "git":
                return None
            if command == "bash":
                return r"C:\Windows\System32\bash.exe"
            self.fail(f"unexpected lookup: {command}")

        with mock.patch.object(platform.shutil, "which", side_effect=which) as finder:
            bash = platform.resolve_bash("safe-tools", platform_name="nt")

        self.assertIsNone(bash)
        finder.assert_called_once_with("git", path="safe-tools")

    def test_platform_helper_rejects_workspace_bash(self):
        platform = _load_platform_helper()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / "bash"
            fake.write_bytes(b"fake")
            with mock.patch.object(
                platform.shutil, "which", return_value=str(fake)
            ):
                bash = platform.resolve_bash(
                    "safe-tools",
                    platform_name="posix",
                    forbidden_root=root,
                )
        self.assertIsNone(bash)

    def test_verifier_rejects_repository_local_tools(self):
        verifier = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            fake = repo / "git.exe"
            fake.write_bytes(b"fake")
            with mock.patch.object(
                verifier.shutil, "which", return_value=str(fake)
            ), self.assertRaisesRegex(
                verifier.CandidateVerificationError,
                "release repository",
            ):
                verifier._trusted_tool("git", repo, "safe-tools")

    def test_verifier_validates_both_sides_of_tool_symlinks(self):
        verifier = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            external = root / "trusted-python"
            external.write_bytes(b"external tool")
            internal = repo / "untrusted-python"
            internal.write_bytes(b"repository tool")
            links = (
                (root / "external-link", external, False),
                (repo / "internal-link", external, True),
                (root / "redirect-to-repo", internal, True),
            )
            for link, target, rejected in links:
                with self.subTest(link=link.name):
                    try:
                        link.symlink_to(target)
                    except OSError:
                        if sys.platform == "win32":
                            self.skipTest("Windows symlink permission unavailable")
                        raise
                    if rejected:
                        with self.assertRaisesRegex(
                            verifier.CandidateVerificationError,
                            "release repository",
                        ):
                            verifier._trusted_tool(str(link), repo, None)
                    else:
                        self.assertEqual(
                            verifier._trusted_tool(str(link), repo, None), str(link)
                        )

    @unittest.skipIf(sys.platform == "win32", "POSIX virtualenv symlink contract")
    def test_verifier_preserves_real_virtualenv_interpreter_and_path(self):
        verifier = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repo = root / "repo"
            repo.mkdir()
            virtualenv = root / "tools"
            subprocess.run(
                [sys.executable, "-I", "-m", "venv", "--without-pip",
                 "--symlinks", str(virtualenv)],
                check=True, capture_output=True, timeout=60,
            )
            interpreter = virtualenv / "bin" / "python"
            self.assertTrue(interpreter.is_symlink())
            selected = verifier._trusted_tool(str(interpreter), repo, None)
            result = subprocess.run(
                [selected, "-I", "-c", "import sys; print(sys.prefix)"],
                check=True, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(Path(result.stdout.strip()), virtualenv)
            with mock.patch.object(
                verifier, "_git_output", side_effect=(SHA, "1700000000")
            ), mock.patch.object(verifier, "_run") as run, mock.patch.object(
                verifier, "_packaging_bash", return_value="/bin/bash"
            ), mock.patch.object(
                verifier, "_release_distributions", return_value=(root / "a.whl", root / "a.tar.gz")
            ):
                verifier.verify_candidate(
                    repo, TAG, SHA, python=str(interpreter),
                )
            for call in run.call_args_list:
                self.assertEqual(
                    call.kwargs["env"]["PATH"].split(verifier.os.pathsep)[0],
                    str(interpreter.parent),
                )
            self.assertEqual(run.call_args_list[0].args[0][0], str(interpreter))

    def test_verifier_git_disables_callbacks_replacements_and_prompts(self):
        verifier = _load_script()
        completed = subprocess.CompletedProcess(
            ["trusted-git"], 0, SHA + "\n", ""
        )
        with mock.patch.object(verifier, "_run", return_value=completed) as run:
            result = verifier._git_output(
                REPO_ROOT,
                "trusted-git",
                ("rev-parse", "HEAD"),
                {"PATH": "safe-tools"},
            )
        self.assertEqual(result, SHA)
        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertIn(f"core.hooksPath={verifier.os.devnull}", command)
        self.assertIn("core.fsmonitor=false", command)
        self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")

    def test_shared_sequence_uses_exact_commit_epoch_and_artifacts(self):
        verifier = _load_script()
        commands: list[tuple[tuple[str, ...], dict[str, str]]] = []
        timeouts: list[int] = []

        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "dist").mkdir()

            def run(
                command,
                *,
                cwd,
                env,
                capture_output=False,
                timeout_seconds=verifier.MAX_COMMAND_SECONDS,
            ):
                self.assertEqual(cwd, repo.resolve())
                commands.append((tuple(command), dict(env)))
                timeouts.append(timeout_seconds)
                if command[-1] == "scripts/packaging_smoke.sh":
                    (repo / "dist" / f"boundver-{CURRENT_VERSION}-py3-none-any.whl").touch()
                    (repo / "dist" / f"boundver-{CURRENT_VERSION}.tar.gz").touch()
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(
                verifier, "_git_output", side_effect=(SHA, "1700000000")
            ), mock.patch.object(verifier, "_run", side_effect=run), mock.patch.object(
                verifier, "_packaging_bash", return_value="/tools/bash"
            ), mock.patch.object(
                verifier,
                "_trusted_tool",
                side_effect=lambda command, _repo, _path: command
                if Path(command).is_absolute()
                else "/tools/git",
            ):
                wheel, sdist = verifier.verify_candidate(
                    repo,
                    TAG,
                    SHA,
                    python=sys.executable,
                    environment={
                        "PATH": "safe-tools",
                        "SAFE_VALUE": "kept",
                        "BASH_ENV": "repo/startup.sh",
                        "ENV": "repo/posix-startup.sh",
                        "SHELLOPTS": "xtrace",
                    },
                )

        self.assertEqual(wheel.name, f"boundver-{CURRENT_VERSION}-py3-none-any.whl")
        self.assertEqual(sdist.name, f"boundver-{CURRENT_VERSION}.tar.gz")
        self.assertEqual(
            commands[0][0],
            (
                sys.executable,
                "-I",
                "scripts/verify_release_readiness.py",
                "--tag",
                TAG,
            ),
        )
        self.assertEqual(
            commands[1][0],
            (
                sys.executable,
                "-I",
                "-m",
                "boundver",
                "coverage",
                "--source",
                "head",
                "--strict",
                "--quiet",
            ),
        )
        self.assertEqual(
            commands[2][0],
            (sys.executable, "-I", "scripts/test_tiers.py", "check"),
        )
        self.assertEqual(
            commands[3][0],
            (
                sys.executable,
                "-I",
                "scripts/test_tiers.py",
                "run",
                "all",
                "--",
                "-v",
                "-x",
            ),
        )
        self.assertEqual(verifier.MAX_COMMAND_SECONDS, 300)
        self.assertEqual(verifier.MAX_TEST_TIER_SECONDS, 7_200)
        self.assertEqual(timeouts[3], verifier.MAX_TEST_TIER_SECONDS)
        self.assertEqual(verifier.MAX_MUTATION_SECONDS, 3_600)
        self.assertEqual(timeouts[4], verifier.MAX_MUTATION_SECONDS)
        self.assertEqual(timeouts[7], verifier.MAX_PACKAGING_SECONDS)
        installer_path = REPO_ROOT / "scripts" / "install_locked_tools.py"
        spec = importlib.util.spec_from_file_location("packaging_installer", installer_path)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        # Packaging installs release tools and the sdist's action dependencies.
        # Both installers can consume their watchdog; leave 30 minutes for the
        # reproducible builds and installation checks under the aggregate cap.
        self.assertGreaterEqual(
            verifier.MAX_PACKAGING_SECONDS,
            2 * installer.MAX_INSTALL_SECONDS + 1_800,
        )
        # Include the two Git metadata commands, which the mock records apart.
        self.assertLess(sum(timeouts) + 2 * verifier.MAX_COMMAND_SECONDS, 19_800)
        self.assertTrue(
            all(
                timeout == verifier.MAX_COMMAND_SECONDS
                for index, timeout in enumerate(timeouts)
                if index not in {3, 4, 7}
            )
        )
        self.assertEqual(
            commands[4][0],
            (sys.executable, "-I", "scripts/mutation_check.py"),
        )
        self.assertEqual(
            commands[5][0],
            (sys.executable, "-I", "scripts/demo_consumer_impact.py"),
        )
        self.assertEqual(
            commands[6][0],
            (sys.executable, "-I", "scripts/demo_range_review.py"),
        )
        self.assertEqual(
            commands[7][0], ("/tools/bash", "scripts/packaging_smoke.sh")
        )
        self.assertEqual(commands[7][1]["SOURCE_DATE_EPOCH"], "1700000000")
        for name in ("BASH_ENV", "ENV", "SHELLOPTS"):
            self.assertNotIn(name, commands[7][1])
        for index in range(7):
            self.assertNotIn("SOURCE_DATE_EPOCH", commands[index][1])
        self.assertEqual(
            commands[8][0][:5],
            (sys.executable, "-I", "-m", "twine", "check"),
        )
        self.assertEqual(commands[8][1]["SAFE_VALUE"], "kept")

    def test_hosted_profile_runs_the_complete_sequence_with_setup_headroom(self):
        verifier = _load_script()
        calls = []
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "dist").mkdir()

            def run(command, *, cwd, env, timeout_seconds=verifier.MAX_COMMAND_SECONDS):
                calls.append((tuple(command), timeout_seconds))
                if command[-1] == "scripts/packaging_smoke.sh":
                    (repo / "dist" / f"boundver-{CURRENT_VERSION}-py3-none-any.whl").touch()
                    (repo / "dist" / f"boundver-{CURRENT_VERSION}.tar.gz").touch()
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(verifier, "_git_output", side_effect=(SHA, "1700000000")), mock.patch.object(
                verifier, "_run", side_effect=run
            ), mock.patch.object(verifier, "_packaging_bash", return_value="/tools/bash"), mock.patch.object(
                verifier, "_trusted_tool", side_effect=lambda command, *_args: command
            ):
                verifier.verify_candidate(repo, TAG, SHA, profile="hosted")
        self.assertEqual(len(calls), 9)
        self.assertIn("scripts/test_tiers.py", calls[3][0])
        self.assertIn("all", calls[3][0])
        self.assertIn("scripts/mutation_check.py", calls[4][0])
        self.assertEqual(calls[3][1], 5_400)
        self.assertEqual(calls[4][1], 1_800)
        self.assertEqual(calls[7][1], 5_400)
        total = sum(timeout for _, timeout in calls) + 2 * verifier.MAX_COMMAND_SECONDS
        self.assertEqual(total, 250 * 60)
        self.assertLess(total, 270 * 60)
        # The 270-minute step leaves a separate 90-minute setup/publication
        # window within GitHub's six-hour hosted-job hard limit.
        self.assertGreaterEqual(360 * 60 - 270 * 60, 1_800 + 32 * 30 + 30 * 60)

    def test_unknown_profile_fails_before_running_commands(self):
        verifier = _load_script()
        with mock.patch.object(verifier, "_run") as runner:
            with self.assertRaisesRegex(verifier.CandidateVerificationError, "profile"):
                verifier.verify_candidate(REPO_ROOT, TAG, SHA, profile="skip-tests")
        runner.assert_not_called()

    def test_verifier_rejects_wrong_checkout_before_running_candidate_code(self):
        verifier = _load_script()
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            verifier, "_git_output", return_value="2" * 40
        ), mock.patch.object(verifier, "_run") as runner:
            with self.assertRaisesRegex(
                verifier.CandidateVerificationError,
                "does not match release SHA",
            ):
                verifier.verify_candidate(Path(temporary), TAG, SHA)
        runner.assert_not_called()

    def test_verifier_requires_exactly_one_wheel_and_sdist(self):
        verifier = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "dist").mkdir()
            (repo / "dist" / "one.whl").touch()
            (repo / "dist" / "two.whl").touch()
            (repo / "dist" / "one.tar.gz").touch()
            with self.assertRaisesRegex(
                verifier.CandidateVerificationError,
                "exactly one wheel and one source distribution",
            ):
                verifier._release_distributions(repo)

    def test_distribution_inventory_is_lazy_and_entry_bounded(self):
        verifier = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            distribution_dir = repo / "dist"
            distribution_dir.mkdir()
            (distribution_dir / "one.whl").touch()
            (distribution_dir / "one.tar.gz").touch()
            (distribution_dir / "extra.pyz").touch()
            with mock.patch.object(
                verifier.Path,
                "glob",
                side_effect=AssertionError("Path.glob must not be used"),
            ), mock.patch.object(verifier, "MAX_DIST_ENTRIES", 2):
                with self.assertRaisesRegex(
                    verifier.CandidateVerificationError,
                    "2-entry limit",
                ):
                    verifier._release_distributions(repo)

    def test_distribution_inventory_bounds_names_and_rejects_nonregular_entries(self):
        verifier = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            distribution_dir = repo / "dist"
            distribution_dir.mkdir()
            (distribution_dir / "one.whl").touch()
            (distribution_dir / "one.tar.gz").touch()
            with mock.patch.object(verifier, "MAX_DIST_NAME_BYTES", 4):
                with self.assertRaisesRegex(
                    verifier.CandidateVerificationError,
                    "name exceeds",
                ):
                    verifier._release_distributions(repo)

        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            distribution_dir = repo / "dist"
            distribution_dir.mkdir()
            (distribution_dir / "one.whl").mkdir()
            (distribution_dir / "one.tar.gz").touch()
            with self.assertRaisesRegex(
                verifier.CandidateVerificationError,
                "non-regular",
            ):
                verifier._release_distributions(repo)

    def test_distribution_inventory_bounds_aggregate_names_and_file_growth(self):
        verifier = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            distribution_dir = repo / "dist"
            distribution_dir.mkdir()
            (distribution_dir / "one.whl").touch()
            (distribution_dir / "one.tar.gz").touch()
            with mock.patch.object(verifier, "MAX_DIST_TOTAL_NAME_BYTES", 10):
                with self.assertRaisesRegex(
                    verifier.CandidateVerificationError,
                    "aggregate limit",
                ):
                    verifier._release_distributions(repo)

        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            distribution_dir = repo / "dist"
            distribution_dir.mkdir()
            (distribution_dir / "one.whl").touch()
            (distribution_dir / "one.tar.gz").touch()
            with mock.patch.object(
                verifier,
                "_changed",
                side_effect=(False, True),
            ):
                with self.assertRaisesRegex(
                    verifier.CandidateVerificationError,
                    "changed while being inspected",
                ):
                    verifier._release_distributions(repo)

    def test_windows_reparse_attribute_is_rejected(self):
        verifier = _load_script()
        identity = mock.Mock(st_file_attributes=0x400)

        self.assertTrue(verifier._is_windows_reparse_point(identity))

    def test_audit_and_install_boundaries_stay_outside_verifier(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("audit_release_reviews.sh", source)
        self.assertNotIn("pip install", source)
        self.assertNotIn("GH_TOKEN", source)
        self.assertNotIn("TWINE_PASSWORD", source)

        publisher = (REPO_ROOT / "scripts" / "publish_release.py").read_text(
            encoding="utf-8"
        )
        audit = publisher.index("scripts/audit_release_reviews.sh")
        install = publisher.index("scripts/install_locked_tools.py", audit)
        verify = publisher.index("scripts/verify_release_candidate.py", install)
        self.assertLess(audit, install)
        self.assertLess(install, verify)

    def test_release_workflows_call_the_same_verifier(self):
        import yaml

        for relative in (
            ".github/workflows/create-release-tag.yml",
            ".github/workflows/publish.yml",
        ):
            with self.subTest(workflow=relative):
                workflow = (REPO_ROOT / relative).read_text(encoding="utf-8")
                self.assertEqual(
                    workflow.count("scripts/verify_release_candidate.py"), 1
                )
                self.assertIn('--tag "$RELEASE_TAG"', workflow)
                self.assertIn('--release-sha "$RELEASE_SHA"', workflow)
                self.assertNotIn("python -m twine check dist/*.whl", workflow)
                self.assertNotIn("SOURCE_DATE_EPOCH=$(git show", workflow)

                parsed = yaml.safe_load(workflow)
                job_name = (
                    "verify-candidate"
                    if "create-release-tag" in relative
                    else "verify-release"
                )
                self.assertEqual(parsed["jobs"][job_name]["timeout-minutes"], 360)
                self.assertEqual(parsed["jobs"][job_name]["runs-on"], "ubuntu-latest")
                steps = parsed["jobs"][job_name]["steps"]
                by_name = {step["name"]: step for step in steps}
                install = by_name["Install hash-locked release verification tools"]
                verify = by_name[
                    "Verify release candidate source and distributions"
                ]
                self.assertIn("--profile hosted", verify["run"])
                self.assertEqual(verify["timeout-minutes"], 270)
                self.assertIn("scripts/install_locked_tools.py release", install["run"])
                self.assertIn("--no-index --no-deps", install["run"])
                self.assertNotIn("pip install", verify["run"])
                self.assertNotIn("GH_TOKEN", verify.get("env", {}))
                self.assertLess(steps.index(install), steps.index(verify))

                if job_name == "verify-candidate":
                    audit = by_name[
                        "Require completed reviews since the previous release"
                    ]
                    self.assertIn("GH_TOKEN", audit["env"])
                    self.assertLess(steps.index(audit), steps.index(install))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
