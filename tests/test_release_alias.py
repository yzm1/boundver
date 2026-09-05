from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "release_alias.py"
REPOSITORY = "yzm1/boundver"
REMOTE = f"https://github.com/{REPOSITORY}.git"
TAG = "v0.11.1"
ALIAS = "v0.11"
SHA = "a" * 40
OLD_SHA = "b" * 40
PUBLICATION_SHA = "c" * 40
RUN_ID = 123456
ATTEMPT = 2


def _load_script():
    name = "boundver_release_alias_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _publication_payloads(*, status="in_progress", conclusion=None):
    run = {
        "id": RUN_ID,
        "run_attempt": ATTEMPT,
        "event": "workflow_dispatch",
        "status": status,
        "conclusion": conclusion,
        "path": ".github/workflows/publish.yml",
        "head_sha": PUBLICATION_SHA,
        "head_branch": "main",
        "repository": {"full_name": REPOSITORY},
    }
    job = {
        "id": 987,
        "name": "Verify PyPI bytes, installation, and provenance",
        "run_id": RUN_ID,
        "run_attempt": ATTEMPT,
        "head_sha": PUBLICATION_SHA,
        "status": "completed",
        "conclusion": "success",
    }
    verify_release = {
        **job,
        "id": 988,
        "name": "verify-release",
    }
    verify_container = {
        **job,
        "id": 989,
        "name": "Publish and verify the release container / verify-public",
    }
    alias_decision = {
        **job,
        "id": 990,
        "name": "Apply the explicit Action alias decision",
        "status": "waiting",
        "conclusion": None,
    }
    return run, {
        "total_count": 4,
        "jobs": [job, verify_release, verify_container, alias_decision],
    }


class PublicationBindingTests(unittest.TestCase):
    def setUp(self):
        self.alias = _load_script()

    def _validate(
        self,
        run,
        jobs,
        *,
        publication_ref="main",
        publication_sha=PUBLICATION_SHA,
    ):
        return self.alias._validate_publication_payloads(
            run,
            jobs,
            repository=REPOSITORY,
            publication_run_id=RUN_ID,
            publication_attempt=ATTEMPT,
            publication_ref=publication_ref,
            publication_sha=publication_sha,
            tag=TAG,
            release_sha=SHA,
        )

    def test_accepts_only_one_successful_exact_pypi_verification_job(self):
        run, jobs = _publication_payloads()
        self.assertEqual(self._validate(run, jobs), 988)

        run["status"] = "waiting"
        self.assertEqual(self._validate(run, jobs), 988)

        duplicate = {"total_count": 8, "jobs": jobs["jobs"] * 2}
        with self.assertRaisesRegex(
            self.alias.AliasError, "duplicate successful"
        ):
            self._validate(run, duplicate)

    def test_accepts_explicit_exact_tag_control_for_initial_publication(self):
        run, jobs = _publication_payloads()
        run["head_branch"] = TAG
        run["head_sha"] = SHA
        for job in jobs["jobs"]:
            job["head_sha"] = SHA
        self.assertEqual(
            self._validate(
                run,
                jobs,
                publication_ref=TAG,
                publication_sha=SHA,
            ),
            988,
        )

    def test_accepts_successful_prerequisites_from_an_earlier_attempt(self):
        run, jobs = _publication_payloads()
        for job in jobs["jobs"][:3]:
            job["run_attempt"] = ATTEMPT - 1
        self.assertEqual(self._validate(run, jobs), 988)

        # A historical failed retry does not erase the exact prior success.
        failed = {
            **jobs["jobs"][0],
            "id": 989,
            "run_attempt": ATTEMPT,
            "conclusion": "failure",
        }
        jobs = {"total_count": 5, "jobs": [*jobs["jobs"], failed]}
        self.assertEqual(self._validate(run, jobs), 988)

    def test_rerun_all_uses_the_latest_successful_verify_release_log(self):
        run, jobs = _publication_payloads()
        earlier = [
            {**job, "id": job["id"] - 100, "run_attempt": ATTEMPT - 1}
            for job in jobs["jobs"]
        ]
        jobs = {"total_count": 8, "jobs": [*earlier, *jobs["jobs"]]}
        self.assertEqual(self._validate(run, jobs), 988)

    def test_rejects_inactive_or_mismatched_publication(self):
        run, jobs = _publication_payloads(status="completed", conclusion="success")
        with self.assertRaisesRegex(self.alias.AliasError, "active exact workflow"):
            self._validate(run, jobs)

        run, jobs = _publication_payloads()
        run["head_sha"] = "d" * 40
        with self.assertRaisesRegex(self.alias.AliasError, "active exact workflow"):
            self._validate(run, jobs)

    def test_rejects_missing_success_or_future_attempt_verification_job(self):
        run, jobs = _publication_payloads()
        jobs["jobs"][0]["conclusion"] = "failure"
        with self.assertRaisesRegex(self.alias.AliasError, "successful PyPI"):
            self._validate(run, jobs)

        run, jobs = _publication_payloads()
        jobs["jobs"][0]["run_attempt"] = ATTEMPT + 1
        with self.assertRaisesRegex(self.alias.AliasError, "not bound to this run"):
            self._validate(run, jobs)

    def test_requires_public_container_and_active_current_alias_gate(self):
        run, jobs = _publication_payloads()
        jobs["jobs"][2]["conclusion"] = "failure"
        with self.assertRaisesRegex(self.alias.AliasError, "public-container"):
            self._validate(run, jobs)

        run, jobs = _publication_payloads()
        jobs["jobs"][3]["status"] = "completed"
        jobs["jobs"][3]["conclusion"] = "failure"
        with self.assertRaisesRegex(self.alias.AliasError, "active alias-decision"):
            self._validate(run, jobs)

    def test_rejects_boolean_api_ids_attempts_and_counts(self):
        run, jobs = _publication_payloads()
        run["id"] = True
        with self.assertRaisesRegex(self.alias.AliasError, "positive integer"):
            self._validate(run, jobs)

        run, jobs = _publication_payloads()
        jobs["total_count"] = True
        with self.assertRaisesRegex(self.alias.AliasError, "nonnegative integer"):
            self._validate(run, jobs)

        run, jobs = _publication_payloads()
        jobs["jobs"][0]["run_attempt"] = True
        with self.assertRaisesRegex(self.alias.AliasError, "positive integer"):
            self._validate(run, jobs)

    def test_release_input_log_evidence_must_be_complete_and_exact(self):
        timestamp = "2026-08-18T07:00:00Z   "
        log = "\n".join(
            [
                f"{timestamp}RELEASE_TAG: {TAG}",
                f"{timestamp}RELEASE_SHA: {SHA}",
                f"{timestamp}COMPATIBILITY_ALIAS: {ALIAS}",
            ]
        )
        self.assertIsNone(
            self.alias._require_release_input_evidence(
                log, tag=TAG, sha=SHA, alias=ALIAS
            )
        )

        with self.assertRaisesRegex(self.alias.AliasError, "does not bind RELEASE_SHA"):
            self.alias._require_release_input_evidence(
                log.replace(SHA, OLD_SHA), tag=TAG, sha=SHA, alias=ALIAS
            )
        with self.assertRaisesRegex(self.alias.AliasError, "complete release input"):
            self.alias._require_release_input_evidence(
                "\n".join(log.splitlines()[:2]), tag=TAG, sha=SHA, alias=ALIAS
            )

    def test_parent_verification_fetches_the_bound_verify_release_log(self):
        run, jobs = _publication_payloads()
        timestamp = "2026-08-18T07:00:00Z   "
        log = "\n".join(
            [
                f"{timestamp}RELEASE_TAG: {TAG}",
                f"{timestamp}RELEASE_SHA: {SHA}",
                f"{timestamp}COMPATIBILITY_ALIAS: {ALIAS}",
            ]
        )
        with mock.patch.object(
            self.alias, "_gh_json", side_effect=[run, jobs]
        ) as gh_json, mock.patch.object(
            self.alias, "_gh_job_log", return_value=log
        ) as job_log:
            self.alias.verify_originating_publication(
                repo=Path("repo"),
                repository=REPOSITORY,
                publication_run_id=RUN_ID,
                publication_attempt=ATTEMPT,
                publication_ref="main",
                publication_sha=PUBLICATION_SHA,
                tag=TAG,
                release_sha=SHA,
                alias=ALIAS,
            )
        job_log.assert_called_once_with(Path("repo"), REPOSITORY, 988)
        endpoint = gh_json.call_args_list[1].args[1]
        self.assertIn("/jobs?filter=all", endpoint)
        self.assertNotIn("/attempts/", endpoint)


class ReleaseAliasHardeningTests(unittest.TestCase):
    def setUp(self):
        self.alias = _load_script()

    def test_json_shape_is_rejected_before_decoder_allocation(self):
        with mock.patch.object(
            self.alias, "MAX_JSON_TOKENS", 2
        ), mock.patch.object(
            self.alias.json,
            "loads",
            side_effect=AssertionError("decoder must not run"),
        ), self.assertRaisesRegex(ValueError, "structural limit"):
            self.alias._strict_json_loads("[0,0,0]")

    def test_json_parser_rejects_duplicates_and_nonfinite_numbers(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.alias._strict_json_loads('{"id":1,"id":2}')
        for token in ("NaN", "Infinity", "-Infinity", "1e9999"):
            with self.subTest(token=token), self.assertRaisesRegex(
                ValueError, "non-finite"
            ):
                self.alias._strict_json_loads('{"value":' + token + "}")

    def test_command_output_is_bounded_while_the_process_is_running(self):
        with self.assertRaisesRegex(self.alias.AliasError, "stdout exceeds"):
            self.alias._run_bytes(
                (
                    sys.executable,
                    "-I",
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'x' * 4096)",
                ),
                cwd=REPO_ROOT,
                max_stdout_bytes=1024,
                timeout=10,
            )

    def test_job_log_uses_the_bounded_runner(self):
        log = b"2026-08-18T07:00:00Z   RELEASE_TAG: v0.11.1\n"
        help_result = subprocess.CompletedProcess(
            [], 0, b"--allow-escape-sequences", b""
        )
        log_result = subprocess.CompletedProcess([], 0, log, b"")
        with mock.patch.object(
            self.alias,
            "_run_bytes",
            side_effect=(help_result, log_result),
        ) as run:
            self.assertEqual(
                self.alias._gh_job_log(REPO_ROOT, REPOSITORY, 987),
                log.decode("utf-8"),
            )
        self.assertEqual(
            run.call_args_list[1].kwargs["max_stdout_bytes"],
            self.alias.MAX_JOB_LOG_BYTES,
        )
        self.assertIn("--hostname", run.call_args_list[1].args[0])

    def test_git_commands_disable_replacements_hooks_and_prompts(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(
            self.alias,
            "_git_credential_helper",
            return_value="!'trusted-gh' auth git-credential",
        ), mock.patch.object(self.alias, "_run", return_value=completed) as run:
            self.alias._git_result(
                REPO_ROOT,
                ("ls-remote", REMOTE, f"refs/tags/{TAG}"),
                check=False,
            )
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(environment["GIT_CONFIG_KEY_0"], "core.hooksPath")
        self.assertEqual(environment["GIT_CONFIG_VALUE_0"], self.alias.os.devnull)
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], self.alias.os.devnull)
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "4")
        self.assertEqual(
            environment["GIT_CONFIG_KEY_2"],
            "credential.https://github.com.helper",
        )
        self.assertEqual(environment["GIT_CONFIG_VALUE_2"], "")
        self.assertEqual(
            environment["GIT_CONFIG_VALUE_3"],
            "!'trusted-gh' auth git-credential",
        )

    def test_credential_helper_quotes_the_trusted_absolute_gh_path(self):
        with mock.patch.object(
            self.alias,
            "_trusted_tool",
            return_value="/trusted/it's gh",
        ):
            helper = self.alias._git_credential_helper(REPO_ROOT)
        self.assertEqual(helper, "!'/trusted/it'\"'\"'s gh' auth git-credential")

    def test_git_replace_cannot_forge_alias_ancestry(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            replacement_environment = self.alias.sanitize_git_environment()

            def git(*arguments: str, check: bool = True):
                return subprocess.run(
                    ("git", *arguments),
                    cwd=repo,
                    env=replacement_environment,
                    text=True,
                    capture_output=True,
                    check=check,
                )

            git("init", "-q")
            git("config", "user.name", "Boundver Tests")
            git("config", "user.email", "tests@boundver.invalid")
            (repo / "base.txt").write_text("base\n", encoding="utf-8")
            git("add", "base.txt")
            git("commit", "-q", "-m", "base")
            base = git("rev-parse", "HEAD").stdout.strip()
            (repo / "child.txt").write_text("child\n", encoding="utf-8")
            git("add", "child.txt")
            git("commit", "-q", "-m", "child")
            descendant = git("rev-parse", "HEAD").stdout.strip()
            git("checkout", "-q", "--orphan", "unrelated")
            (repo / "base.txt").write_text("unrelated\n", encoding="utf-8")
            git("add", "base.txt")
            git("commit", "-q", "-m", "unrelated")
            unrelated = git("rev-parse", "HEAD").stdout.strip()
            self.assertEqual(
                git(
                    "merge-base",
                    "--is-ancestor",
                    base,
                    unrelated,
                    check=False,
                ).returncode,
                1,
            )
            git("replace", unrelated, descendant)
            self.assertEqual(
                git(
                    "merge-base",
                    "--is-ancestor",
                    base,
                    unrelated,
                    check=False,
                ).returncode,
                0,
            )
            self.assertEqual(
                self.alias._git_result(
                    repo,
                    ("merge-base", "--is-ancestor", base, unrelated),
                    check=False,
                ).returncode,
                1,
            )

class AliasDispatchTests(unittest.TestCase):
    def setUp(self):
        self.alias = _load_script()
        self.repo = Path("repo")

    def _arguments(self):
        return {
            "repo": self.repo,
            "repository": REPOSITORY,
            "remote": REMOTE,
            "tag": TAG,
            "sha": SHA,
            "alias": ALIAS,
            "publication_run_id": RUN_ID,
            "publication_attempt": ATTEMPT,
            "publication_ref": "main",
            "publication_sha": PUBLICATION_SHA,
            "attempts": 3,
            "delay_seconds": 0,
        }

    def test_release_tag_checkout_must_contain_regular_alias_workflow(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaisesRegex(
                self.alias.AliasError, "predates the exact-tag alias verification"
            ):
                self.alias._require_release_alias_workflow(root, TAG)

            workflow = root / self.alias.WORKFLOW_PATH
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: alias\n", encoding="utf-8")
            self.alias._require_release_alias_workflow(root, TAG)

    def test_recovery_dispatches_release_tag_verification_after_local_handoff(self):
        completed = {
            "id": 111,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://github.com/yzm1/boundver/actions/runs/111",
        }
        command_result = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(
            self.alias, "_remote_ref", side_effect=[SHA, SHA, SHA]
        ), mock.patch.object(
            self.alias, "_release_alias_workflow_available", return_value=True
        ), mock.patch.object(
            self.alias, "_find_alias_run", side_effect=[None, completed]
        ), mock.patch.object(
            self.alias, "_run", return_value=command_result
        ) as run:
            result = self.alias.dispatch_alias_workflow(**self._arguments())

        self.assertEqual(result, completed["html_url"])
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ("gh", "workflow", "run", "advance-release-alias.yml"))
        self.assertIn("--ref", command)
        self.assertEqual(command[command.index("--ref") + 1], TAG)
        for field in (
            f"release_tag={TAG}",
            f"release_sha={SHA}",
            f"compatibility_alias={ALIAS}",
            f"publication_run_id={RUN_ID}",
            f"publication_attempt={ATTEMPT}",
            "publication_ref=main",
            f"publication_sha={PUBLICATION_SHA}",
        ):
            self.assertIn(field, command)

    def test_dispatch_rejects_missing_local_alias_handoff(self):
        arguments = {
            **self._arguments(),
            "publication_ref": TAG,
            "publication_sha": SHA,
        }
        with mock.patch.object(
            self.alias, "_remote_ref", side_effect=[SHA, None]
        ), mock.patch.object(self.alias, "_run") as run, self.assertRaisesRegex(
            self.alias.AliasError, "local publishing script's alias phase"
        ):
            self.alias.dispatch_alias_workflow(**arguments)

        run.assert_not_called()

    def test_dispatch_polling_policy_and_numeric_ids_are_bounded(self):
        for attempts, delay in (
            (self.alias.MAX_POLL_ATTEMPTS + 1, 0),
            (1, self.alias.MAX_POLL_DELAY_SECONDS + 1),
            (1, float("nan")),
            (1, float("inf")),
        ):
            with self.subTest(
                attempts=attempts, delay=delay
            ), self.assertRaisesRegex(self.alias.AliasError, "bounded"):
                self.alias.dispatch_alias_workflow(
                    **{
                        **self._arguments(),
                        "attempts": attempts,
                        "delay_seconds": delay,
                    }
                )

        with self.assertRaisesRegex(self.alias.AliasError, "supported range"):
            self.alias._parse_positive_int(
                str(self.alias.MAX_GITHUB_ID + 1), "publication run ID"
            )
        with self.assertRaisesRegex(self.alias.AliasError, "positive decimal"):
            self.alias._parse_positive_int("9" * 21, "publication run ID")

    def test_recovery_dispatch_ref_remains_release_tag_when_control_sha_matches(self):
        arguments = {**self._arguments(), "publication_sha": SHA}
        command_result = subprocess.CompletedProcess([], 1, "", "queued")
        with mock.patch.object(
            self.alias, "_remote_ref", side_effect=[SHA, SHA]
        ), mock.patch.object(
            self.alias, "_release_alias_workflow_available", return_value=True
        ), mock.patch.object(
            self.alias, "_find_alias_run", return_value=None
        ), mock.patch.object(
            self.alias, "_run", return_value=command_result
        ) as run, self.assertRaisesRegex(self.alias.AliasError, "polling window"):
            self.alias.dispatch_alias_workflow(**arguments)

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--ref") + 1], TAG)

    def test_exact_legacy_alias_without_child_workflow_is_idempotent(self):
        with mock.patch.object(
            self.alias, "_remote_ref", side_effect=[SHA, SHA]
        ), mock.patch.object(
            self.alias,
            "_release_alias_workflow_available",
            return_value=False,
        ), mock.patch.object(self.alias, "_run") as run:
            result = self.alias.dispatch_alias_workflow(**self._arguments())

        self.assertIn("predates", result)
        run.assert_not_called()

    def test_rejects_unknown_or_mismatched_publication_control(self):
        for overrides, message in (
            ({"publication_ref": "feature"}, "exact release tag or main"),
            ({"publication_ref": TAG}, "must match the release SHA"),
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(self.alias.AliasError, message):
                    self.alias.dispatch_alias_workflow(
                        **{**self._arguments(), **overrides}
                    )

    def test_exact_existing_alias_still_dispatches_independent_verification(self):
        completed = {
            "id": 111,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://github.com/yzm1/boundver/actions/runs/111",
        }
        with mock.patch.object(
            self.alias, "_remote_ref", side_effect=[SHA, SHA, SHA]
        ), mock.patch.object(
            self.alias, "_release_alias_workflow_available", return_value=True
        ), mock.patch.object(
            self.alias, "_find_alias_run", side_effect=[None, completed]
        ), mock.patch.object(
            self.alias,
            "_run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            result = self.alias.dispatch_alias_workflow(**self._arguments())
        self.assertEqual(result, completed["html_url"])
        run.assert_called_once()

    def test_matching_run_must_be_unique_and_exact(self):
        title = self.alias._run_title(ALIAS, TAG, RUN_ID, ATTEMPT)
        base = {
            "id": 1,
            "run_attempt": 1,
            "display_title": title,
            "path": ".github/workflows/advance-release-alias.yml",
            "event": "workflow_dispatch",
            "head_branch": TAG,
            "head_sha": SHA,
            "status": "queued",
            "conclusion": None,
            "html_url": "https://example.invalid/run/1",
        }
        with self.assertRaisesRegex(self.alias.AliasError, "multiple alias workflow"):
            self.alias._matching_alias_runs(
                {"total_count": 2, "workflow_runs": [base, {**base, "id": 2}]},
                title=title,
                control_ref=TAG,
                control_sha=SHA,
            )

        wrong = {**base, "head_sha": "d" * 40}
        with self.assertRaisesRegex(self.alias.AliasError, "different inputs"):
            self.alias._matching_alias_runs(
                {"total_count": 1, "workflow_runs": [wrong]},
                title=title,
                control_ref=TAG,
                control_sha=SHA,
            )

        with self.assertRaisesRegex(self.alias.AliasError, "listing is incomplete"):
            self.alias._matching_alias_runs(
                {"total_count": 2, "workflow_runs": [base]},
                title=title,
                control_ref=TAG,
                control_sha=SHA,
            )


class AliasMutationTests(unittest.TestCase):
    def setUp(self):
        self.alias = _load_script()
        self.repo = Path("repo")

    def _arguments(self, *, tag=TAG):
        return {
            "repo": self.repo,
            "repository": REPOSITORY,
            "remote": REMOTE,
            "tag": tag,
            "sha": SHA,
            "alias": ALIAS,
            "publication_run_id": RUN_ID,
            "publication_attempt": ATTEMPT,
            "publication_ref": "main",
            "publication_sha": PUBLICATION_SHA,
        }

    def test_creates_absent_alias_with_empty_force_with_lease(self):
        with mock.patch.object(
            self.alias, "verify_originating_publication"
        ) as verify, mock.patch.object(
            self.alias, "_remote_ref", side_effect=[SHA, None, SHA, SHA]
        ), mock.patch.object(self.alias, "_git", return_value="") as git:
            result = self.alias.advance_alias(**self._arguments())

        self.assertEqual(verify.call_count, 2)
        self.assertIn("advanced", result)
        calls = [call.args[1:] for call in git.call_args_list]
        self.assertIn(
            (
                "push",
                f"--force-with-lease=refs/tags/{ALIAS}:",
                REMOTE,
                f"{SHA}:refs/tags/{ALIAS}",
            ),
            calls,
        )
        self.assertNotIn(("tag", "--force", ALIAS, SHA), calls)

    def test_advances_only_from_an_ancestral_earlier_same_line_release(self):
        def git(repo, *arguments):
            if arguments[:3] == ("ls-remote", "--tags", "--refs"):
                return f"{OLD_SHA}\trefs/tags/v0.11.0"
            return ""

        ancestry = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(
            self.alias, "verify_originating_publication"
        ), mock.patch.object(
            self.alias, "_remote_ref", side_effect=[SHA, OLD_SHA, SHA, SHA]
        ), mock.patch.object(self.alias, "_git", side_effect=git) as git_mock, mock.patch.object(
            self.alias, "_run", return_value=ancestry
        ):
            self.alias.advance_alias(**self._arguments())

        calls = [call.args[1:] for call in git_mock.call_args_list]
        self.assertIn(
            (
                "push",
                f"--force-with-lease=refs/tags/{ALIAS}:{OLD_SHA}",
                REMOTE,
                f"{SHA}:refs/tags/{ALIAS}",
            ),
            calls,
        )

    def test_read_only_child_requires_exact_advanced_alias(self):
        with mock.patch.object(
            self.alias, "verify_alias_request"
        ) as verify, mock.patch.object(
            self.alias, "_remote_ref", return_value=SHA
        ):
            result = self.alias.require_advanced_alias(**self._arguments())
        verify.assert_called_once()
        self.assertIn("resolves exactly", result)

        with mock.patch.object(
            self.alias, "verify_alias_request"
        ), mock.patch.object(
            self.alias, "_remote_ref", return_value=OLD_SHA
        ), self.assertRaisesRegex(self.alias.AliasError, "does not resolve"):
            self.alias.require_advanced_alias(**self._arguments())

    def test_rejects_patch_rollback_before_mutation(self):
        def git(repo, *arguments):
            if arguments[:3] == ("ls-remote", "--tags", "--refs"):
                return f"{OLD_SHA}\trefs/tags/v0.11.1"
            return ""

        ancestry = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(
            self.alias, "verify_originating_publication"
        ), mock.patch.object(
            self.alias, "_remote_ref", side_effect=[SHA, OLD_SHA]
        ), mock.patch.object(self.alias, "_git", side_effect=git) as git_mock, mock.patch.object(
            self.alias, "_run", return_value=ancestry
        ), self.assertRaisesRegex(self.alias.AliasError, "rollback from patch 1"):
            self.alias.advance_alias(**self._arguments(tag="v0.11.0"))

        self.assertFalse(any(call.args[1:2] == ("push",) for call in git_mock.call_args_list))

    def test_rejects_non_ancestral_alias_before_mutation(self):
        not_ancestor = subprocess.CompletedProcess([], 1, "", "")
        with mock.patch.object(
            self.alias, "verify_originating_publication"
        ), mock.patch.object(
            self.alias, "_remote_ref", side_effect=[SHA, OLD_SHA]
        ), mock.patch.object(self.alias, "_git", return_value="") as git_mock, mock.patch.object(
            self.alias, "_run", return_value=not_ancestor
        ), self.assertRaisesRegex(self.alias.AliasError, "non-ancestral"):
            self.alias.advance_alias(**self._arguments())
        self.assertFalse(any(call.args[1:2] == ("push",) for call in git_mock.call_args_list))

    def test_rejects_option_like_or_noncanonical_release_remote(self):
        for remote in (
            "--upload-pack=malicious",
            "upstream",
            "https://example.invalid/repo",
        ):
            with self.subTest(remote=remote), self.assertRaisesRegex(
                self.alias.AliasError,
                "release remote must be origin or the canonical",
            ):
                self.alias._validate_release_remote(self.repo, REPOSITORY, remote)

    def test_alias_mutation_rejects_noncanonical_remote_before_api_or_git(self):
        with mock.patch.object(
            self.alias,
            "verify_originating_publication",
        ) as verify, mock.patch.object(self.alias, "_remote_ref") as remote_ref:
            with self.assertRaisesRegex(
                self.alias.AliasError,
                "release remote must be origin or the canonical",
            ):
                self.alias.advance_alias(
                    **{
                        **self._arguments(),
                        "remote": "https://example.invalid/steal-token",
                    }
                )
        verify.assert_not_called()
        remote_ref.assert_not_called()

    def test_origin_must_resolve_to_the_canonical_repository(self):
        with mock.patch.object(
            self.alias,
            "_git",
            return_value="https://example.invalid/attacker/repo.git",
        ), self.assertRaisesRegex(
            self.alias.AliasError,
            "origin does not identify the canonical GitHub repository",
        ):
            self.alias._validate_release_remote(self.repo, REPOSITORY, "origin")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
