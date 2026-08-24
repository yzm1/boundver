from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests._project_metadata import CURRENT_MINOR_TAG, CURRENT_VERSION


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "release_workflow.py"
REPOSITORY = "yzm1/boundver"
TAG = f"v{CURRENT_VERSION}"
SHA = "a" * 40
RUN_ID = 12345


def _load_script():
    name = "boundver_release_workflow_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _payloads():
    run = {
        "id": RUN_ID,
        "run_attempt": 2,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "failure",
        "path": ".github/workflows/publish.yml",
        "head_branch": TAG,
        "head_sha": SHA,
        "repository": {"full_name": REPOSITORY},
    }
    verify = {
        "id": 456,
        "name": "verify-release",
        "run_id": RUN_ID,
        "run_attempt": 1,
        "head_sha": SHA,
        "status": "completed",
        "conclusion": "success",
    }
    jobs = {"total_count": 1, "jobs": [verify]}
    artifacts = []
    for artifact_id, prefix, digest in (
        (1001, "python-dist", "b" * 64),
        (1002, "release-assets", "c" * 64),
    ):
        artifacts.append(
            {
                "id": artifact_id,
                "name": f"{prefix}-{TAG}-{RUN_ID}-1",
                "size_in_bytes": 100,
                "expired": False,
                "digest": f"sha256:{digest}",
                "expires_at": "2099-01-01T00:00:00Z",
                "workflow_run": {
                    "id": RUN_ID,
                    "head_branch": TAG,
                    "head_sha": SHA,
                },
            }
        )
    return run, jobs, {"total_count": 2, "artifacts": artifacts}


class RecoverySelectionTests(unittest.TestCase):
    def setUp(self):
        self.release = _load_script()

    def _select(self, run, jobs, artifacts):
        return self.release.select_recovery_artifacts(
            run,
            jobs,
            artifacts,
            repository=REPOSITORY,
            run_id=RUN_ID,
            tag=TAG,
            sha=SHA,
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    def test_selects_exact_prior_attempt_artifacts(self):
        selection = self._select(*_payloads())
        self.assertEqual(selection.source_run_attempt, 2)
        self.assertEqual(selection.artifact_attempt, 1)
        self.assertEqual(selection.verification_job_id, 456)
        self.assertEqual(selection.python_dist_artifact_id, 1001)
        self.assertEqual(selection.release_assets_artifact_id, 1002)
        self.assertEqual(selection.outputs()["source-run-id"], str(RUN_ID))

    def test_rejects_wrong_repository_incomplete_jobs_and_expired_artifact(self):
        run, jobs, artifacts = _payloads()
        run["repository"]["full_name"] = "attacker/fork"
        with self.assertRaisesRegex(self.release.ReleaseWorkflowError, "repository"):
            self._select(run, jobs, artifacts)

        run, jobs, artifacts = _payloads()
        jobs["total_count"] = 2
        with self.assertRaisesRegex(
            self.release.ReleaseWorkflowError, "completely inspect"
        ):
            self._select(run, jobs, artifacts)

        run, jobs, artifacts = _payloads()
        artifacts["artifacts"][0]["expires_at"] = "2025-01-01T00:00:00Z"
        with self.assertRaisesRegex(self.release.ReleaseWorkflowError, "expired"):
            self._select(run, jobs, artifacts)

    def test_rejects_boolean_identifiers_and_incomplete_artifact_pages(self):
        run, jobs, artifacts = _payloads()
        jobs["jobs"][0]["id"] = True
        with self.assertRaisesRegex(
            self.release.ReleaseWorkflowError, "successful exact"
        ):
            self._select(run, jobs, artifacts)

        run, jobs, artifacts = _payloads()
        artifacts["total_count"] = 3
        with self.assertRaisesRegex(self.release.ReleaseWorkflowError, "incomplete"):
            self._select(run, jobs, artifacts)

    def test_reuses_prior_verified_attempt_and_accepts_bound_release_notes(self):
        run, jobs, artifacts = _payloads()
        run["run_attempt"] = 3
        note = {
            "id": 1003,
            "name": f"release-notes-{SHA}-{RUN_ID}-2",
            "size_in_bytes": 100,
            "expired": False,
            "digest": "sha256:" + "d" * 64,
            "expires_at": "2099-01-01T00:00:00Z",
            "workflow_run": {
                "id": RUN_ID,
                "head_branch": TAG,
                "head_sha": SHA,
            },
        }
        artifacts["artifacts"].append(note)
        artifacts["total_count"] = 3

        selection = self._select(run, jobs, artifacts)

        self.assertEqual(selection.source_run_attempt, 3)
        self.assertEqual(selection.artifact_attempt, 1)
        self.assertEqual(selection.release_note_artifact_count, 1)

        jobs["jobs"].append(dict(jobs["jobs"][0]))
        jobs["total_count"] = 2
        with self.assertRaisesRegex(
            self.release.ReleaseWorkflowError, "retained artifact attempt"
        ):
            self._select(run, jobs, artifacts)

    def test_cli_writes_only_validated_outputs(self):
        run, jobs, artifacts = _payloads()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {}
            for name, payload in (("run", run), ("jobs", jobs), ("artifacts", artifacts)):
                paths[name] = root / f"{name}.json"
                paths[name].write_text(json.dumps(payload), encoding="utf-8")
            output = root / "output"
            rc = self.release.main(
                [
                    "select-recovery",
                    "--repository",
                    REPOSITORY,
                    "--run-id",
                    str(RUN_ID),
                    "--tag",
                    TAG,
                    "--sha",
                    SHA,
                    "--run-json",
                    str(paths["run"]),
                    "--jobs-json",
                    str(paths["jobs"]),
                    "--artifacts-json",
                    str(paths["artifacts"]),
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(rc, 0)
            values = dict(
                line.split("=", 1)
                for line in output.read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(values["verification-job-id"], "456")
            self.assertEqual(values["python-dist-artifact-id"], "1001")

    def test_json_inputs_are_strict_and_byte_bounded(self):
        for document in (
            '{"id":1,"id":2}',
            '{"id":NaN}',
            '{"id":123456789012345678901}',
        ):
            with self.subTest(document=document), self.assertRaises(ValueError):
                self.release._strict_json_loads(document)

        with tempfile.TemporaryDirectory() as temporary:
            payload = Path(temporary) / "payload.json"
            payload.write_bytes(b'{"x":1}')
            with mock.patch.object(self.release, "MAX_GITHUB_RESPONSE_BYTES", 4):
                with self.assertRaisesRegex(
                    self.release.ReleaseWorkflowError, "invalid JSON payload"
                ):
                    self.release._json_file(payload)


class ReleasePolicyEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.release = _load_script()

    def test_requires_complete_exact_log_triples(self):
        prefix = "2026-08-18T10:00:00Z   "
        log = "\n".join(
            [
                f"{prefix}RELEASE_TAG: {TAG}",
                f"{prefix}RELEASE_SHA: {SHA}",
                f"{prefix}COMPATIBILITY_ALIAS: {CURRENT_MINOR_TAG}",
            ]
        )
        self.assertIn(
            "1 release input triple",
            self.release.require_release_input_evidence(
                log, tag=TAG, sha=SHA, alias=CURRENT_MINOR_TAG
            ),
        )
        with self.assertRaisesRegex(self.release.ReleaseWorkflowError, "RELEASE_SHA"):
            self.release.require_release_input_evidence(
                log.replace(SHA, "d" * 40), tag=TAG, sha=SHA, alias=CURRENT_MINOR_TAG
            )
        with self.assertRaisesRegex(self.release.ReleaseWorkflowError, "complete"):
            self.release.require_release_input_evidence(
                "\n".join(log.splitlines()[:2]),
                tag=TAG,
                sha=SHA,
                alias=CURRENT_MINOR_TAG,
            )

        reordered = "\n".join(
            [log.splitlines()[1], log.splitlines()[0], log.splitlines()[2]]
        )
        with self.assertRaisesRegex(
            self.release.ReleaseWorkflowError, "RELEASE_TAG"
        ):
            self.release.require_release_input_evidence(
                reordered,
                tag=TAG,
                sha=SHA,
                alias=CURRENT_MINOR_TAG,
            )

    def test_selects_and_normalizes_fresh_or_recovered_outputs(self):
        common = {
            "current_run_id": "12",
            "fresh_python_id": "13",
            "fresh_python_digest": "a" * 64,
            "fresh_release_id": "14",
            "fresh_release_digest": "sha256:" + "b" * 64,
            "recovered_run_id": "22",
            "recovered_python_id": "23",
            "recovered_python_digest": "sha256:" + "c" * 64,
            "recovered_release_id": "24",
            "recovered_release_digest": "d" * 64,
        }
        fresh = self.release.select_artifact_values(resume_run_id="", **common)
        self.assertEqual(fresh["source-run-id"], "12")
        self.assertEqual(fresh["python-dist-artifact-digest"], "sha256:" + "a" * 64)
        recovered = self.release.select_artifact_values(
            resume_run_id="22", **common
        )
        self.assertEqual(recovered["source-run-id"], "22")
        self.assertEqual(recovered["release-assets-artifact-id"], "24")


class ArtifactPayloadTests(unittest.TestCase):
    def setUp(self):
        self.release = _load_script()

    def test_archive_digest_and_extracted_bytes_are_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            extracted = root / "extracted"
            extracted.mkdir()
            files = {"a.whl": b"wheel", "a.tar.gz": b"sdist"}
            archive_path = root / "artifact.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                for name, content in files.items():
                    archive.writestr(name, content)
                    (extracted / name).write_bytes(content)
            digest = "sha256:" + hashlib.sha256(archive_path.read_bytes()).hexdigest()
            self.release.verify_artifact_archive(archive_path, extracted, digest)

            (extracted / "a.whl").write_bytes(b"changed")
            with self.assertRaisesRegex(
                self.release.ReleaseWorkflowError,
                "entry size disagrees|changed artifact bytes",
            ):
                self.release.verify_artifact_archive(archive_path, extracted, digest)

    def test_recovered_payload_requires_exact_checksums_and_duplicate_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python_dist = root / "python-dist"
            release_assets = root / "release-assets"
            python_dist.mkdir()
            release_assets.mkdir()
            names = {
                f"boundver-{CURRENT_VERSION}-py3-none-any.whl": b"wheel",
                f"boundver-{CURRENT_VERSION}.tar.gz": b"sdist",
                f"boundver-{CURRENT_VERSION}.pyz": b"pyz",
            }
            for name, content in names.items():
                (release_assets / name).write_bytes(content)
                if not name.endswith(".pyz"):
                    (python_dist / name).write_bytes(content)
            manifest = "".join(
                f"{hashlib.sha256(content).hexdigest()}  {name}\n"
                for name, content in names.items()
            )
            (release_assets / "SHA256SUMS").write_text(manifest, encoding="utf-8")
            self.release.validate_recovered_payload(
                python_dist, release_assets, TAG
            )

            (python_dist / f"boundver-{CURRENT_VERSION}.tar.gz").write_bytes(
                b"different"
            )
            with self.assertRaisesRegex(
                self.release.ReleaseWorkflowError, "copies differ"
            ):
                self.release.validate_recovered_payload(
                    python_dist, release_assets, TAG
                )


class GitHubReleaseProbeTests(unittest.TestCase):
    def setUp(self):
        self.release = _load_script()

    def test_parses_absent_draft_public_and_redirected_responses(self):
        cases = {
            "HTTP/2 404 Not Found\n\n": ("404", "absent"),
            'HTTP/2 200 OK\r\n\r\n{"draft": true}\r\n': ("200", "draft"),
            (
                "HTTP/2 301 Moved Permanently\n\n"
                'HTTP/2 200 OK\n\n{"draft": false}\n'
            ): ("200", "public"),
            "HTTP/2 503 Service Unavailable\n\n": ("503", "error"),
        }
        for response, expected in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(
                    self.release.parse_github_release_probe(response), expected
                )

    def test_rejects_malformed_or_ambiguous_success_payloads(self):
        for response in (
            "not HTTP",
            "HTTP/2 200 OK\n",
            "HTTP/2 200 OK\n\nnot-json",
            'HTTP/2 200 OK\n\n{"draft": 1}',
        ):
            with self.subTest(response=response), self.assertRaises(
                self.release.ReleaseWorkflowError
            ):
                self.release.parse_github_release_probe(response)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
