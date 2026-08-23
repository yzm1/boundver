from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "release_alias.py"
REPOSITORY = "yzm1/boundver"
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
    return run, {"total_count": 2, "jobs": [job, verify_release]}


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

        duplicate = {"total_count": 4, "jobs": jobs["jobs"] * 2}
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
        for job in jobs["jobs"]:
            job["run_attempt"] = ATTEMPT - 1
        self.assertEqual(self._validate(run, jobs), 988)

        # A historical failed retry does not erase the exact prior success.
        failed = {
            **jobs["jobs"][0],
            "id": 989,
            "run_attempt": ATTEMPT,
            "conclusion": "failure",
        }
        jobs = {"total_count": 3, "jobs": [*jobs["jobs"], failed]}
        self.assertEqual(self._validate(run, jobs), 988)

    def test_rerun_all_uses_the_latest_successful_verify_release_log(self):
        run, jobs = _publication_payloads()
        earlier = [
            {**job, "id": job["id"] - 100, "run_attempt": ATTEMPT - 1}
            for job in jobs["jobs"]
        ]
        jobs = {"total_count": 4, "jobs": [*earlier, *jobs["jobs"]]}
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

class AliasDispatchTests(unittest.TestCase):
    def setUp(self):
        self.alias = _load_script()
        self.repo = Path("repo")

    def _arguments(self):
        return {
            "repo": self.repo,
            "repository": REPOSITORY,
            "remote": "origin",
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

    def test_recovery_dispatches_current_main_control_and_requires_alias(self):
        completed = {
            "id": 111,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://github.com/yzm1/boundver/actions/runs/111",
        }
        command_result = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(
            self.alias, "_remote_ref", side_effect=[SHA, None, SHA]
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
        self.assertEqual(command[command.index("--ref") + 1], "main")
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

    def test_initial_publication_dispatches_exact_release_tag_control(self):
        arguments = {
            **self._arguments(),
            "publication_ref": TAG,
            "publication_sha": SHA,
        }
        command_result = subprocess.CompletedProcess([], 1, "", "queued")
        with mock.patch.object(
            self.alias, "_remote_ref", side_effect=[SHA, None]
        ), mock.patch.object(
            self.alias, "_find_alias_run", return_value=None
        ) as find, mock.patch.object(
            self.alias, "_run", return_value=command_result
        ) as run, self.assertRaisesRegex(self.alias.AliasError, "polling window"):
            self.alias.dispatch_alias_workflow(**arguments)

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--ref") + 1], TAG)
        self.assertEqual(find.call_args.kwargs["control_ref"], TAG)
        self.assertEqual(find.call_args.kwargs["control_sha"], SHA)

    def test_recovery_ref_remains_main_when_control_sha_equals_release(self):
        arguments = {**self._arguments(), "publication_sha": SHA}
        command_result = subprocess.CompletedProcess([], 1, "", "queued")
        with mock.patch.object(
            self.alias, "_remote_ref", side_effect=[SHA, None]
        ), mock.patch.object(
            self.alias, "_find_alias_run", return_value=None
        ), mock.patch.object(
            self.alias, "_run", return_value=command_result
        ) as run, self.assertRaisesRegex(self.alias.AliasError, "polling window"):
            self.alias.dispatch_alias_workflow(**arguments)

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--ref") + 1], "main")

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

    def test_exact_existing_alias_is_idempotent_without_dispatch(self):
        with mock.patch.object(
            self.alias, "_remote_ref", side_effect=[SHA, SHA]
        ), mock.patch.object(self.alias, "_run") as run:
            result = self.alias.dispatch_alias_workflow(**self._arguments())
        self.assertIn("already resolves", result)
        run.assert_not_called()

    def test_matching_run_must_be_unique_and_exact(self):
        title = self.alias._run_title(ALIAS, TAG, RUN_ID, ATTEMPT)
        base = {
            "id": 1,
            "run_attempt": 1,
            "display_title": title,
            "path": ".github/workflows/advance-release-alias.yml",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": PUBLICATION_SHA,
            "status": "queued",
            "conclusion": None,
            "html_url": "https://example.invalid/run/1",
        }
        with self.assertRaisesRegex(self.alias.AliasError, "multiple alias workflow"):
            self.alias._matching_alias_runs(
                {"total_count": 2, "workflow_runs": [base, {**base, "id": 2}]},
                title=title,
                control_ref="main",
                control_sha=PUBLICATION_SHA,
            )

        wrong = {**base, "head_sha": "d" * 40}
        with self.assertRaisesRegex(self.alias.AliasError, "different inputs"):
            self.alias._matching_alias_runs(
                {"total_count": 1, "workflow_runs": [wrong]},
                title=title,
                control_ref="main",
                control_sha=PUBLICATION_SHA,
            )

        with self.assertRaisesRegex(self.alias.AliasError, "listing is incomplete"):
            self.alias._matching_alias_runs(
                {"total_count": 2, "workflow_runs": [base]},
                title=title,
                control_ref="main",
                control_sha=PUBLICATION_SHA,
            )


class AliasMutationTests(unittest.TestCase):
    def setUp(self):
        self.alias = _load_script()
        self.repo = Path("repo")

    def _arguments(self, *, tag=TAG):
        return {
            "repo": self.repo,
            "repository": REPOSITORY,
            "remote": "origin",
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
        self.assertIn(("tag", "--force", ALIAS, SHA), calls)
        self.assertIn(
            ("push", f"--force-with-lease=refs/tags/{ALIAS}:", "origin", f"refs/tags/{ALIAS}"),
            calls,
        )

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
                "origin",
                f"refs/tags/{ALIAS}",
            ),
            calls,
        )

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
