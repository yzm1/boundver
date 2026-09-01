"""Executable assurance checks for the semantic-provider design gate."""

from __future__ import annotations

import copy
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "spec" / "semantic-provider-proposal.json"
SCHEMA = ROOT / "spec" / "semantic-provider-proposal.schema.json"
CHECKER_PATH = ROOT / "scripts" / "check_semantic_provider_proposal.py"
AUDITOR_PATH = ROOT / "scripts" / "audit_semantic_provider_proposal.py"

_SPEC = importlib.util.spec_from_file_location(
    "boundver_semantic_provider_proposal_checker",
    CHECKER_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
_CHECKER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CHECKER)

_AUDITOR_SPEC = importlib.util.spec_from_file_location(
    "boundver_semantic_provider_proposal_auditor",
    AUDITOR_PATH,
)
assert _AUDITOR_SPEC is not None and _AUDITOR_SPEC.loader is not None
_AUDITOR = importlib.util.module_from_spec(_AUDITOR_SPEC)
_AUDITOR_SPEC.loader.exec_module(_AUDITOR)


class SemanticProviderProposalTests(unittest.TestCase):
    def test_reviewed_checker_runs_without_audit_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            validation_root = Path(temporary)
            scripts = validation_root / "scripts"
            scripts.mkdir()
            checker = scripts / "check_semantic_provider_proposal.py"
            checker.write_text(
                """
import os

def validate_proposal(repo, manifest, **options):
    forbidden = {
        'GH_TOKEN',
        'GITHUB_TOKEN',
        'BOUNDVER_RELEASE_REVIEW_TOKEN',
        'AWS_SECRET_ACCESS_KEY',
    }
    if forbidden.intersection(os.environ):
        raise RuntimeError('credential reached isolated checker')
    return {
        'ok': True,
        'proposal': 'boundver-semantic-provider-system/v1',
        'options': options,
    }
""",
                encoding="utf-8",
            )
            manifest = validation_root / "manifest.json"
            manifest.write_text("{}\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "GH_TOKEN": "secret",
                    "GITHUB_TOKEN": "secret",
                    "BOUNDVER_RELEASE_REVIEW_TOKEN": "secret",
                    "AWS_SECRET_ACCESS_KEY": "secret",
                },
            ):
                result = _AUDITOR._run_checker(
                    ROOT,
                    validation_root,
                    manifest,
                    authoritative_review_passed=True,
                    authoritative_release_passed=False,
                    require_accepted=True,
                    require_v0_15_work=False,
                    require_v0_15_release=False,
                )

        self.assertTrue(result["ok"])
        self.assertTrue(result["options"]["authoritative_review_passed"])

    AUDIT_TIME = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    def _manifest(self):
        return json.loads(MANIFEST.read_text(encoding="utf-8"))

    def _validate_mutation(
        self,
        mutate,
        *,
        document_status=None,
        document_implementation_allowed=None,
        document_v0_15_work_allowed=None,
        **validation_options,
    ):
        value = self._manifest()
        mutate(value)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proposal.json"
            path.write_text(
                json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            def validate():
                return _CHECKER.validate_proposal(
                    ROOT,
                    path,
                    **validation_options,
                )

            if document_status is None:
                return validate()
            if document_implementation_allowed is None:
                document_implementation_allowed = document_status == "accepted"
            if document_v0_15_work_allowed is None:
                document_v0_15_work_allowed = document_status == "accepted"
            original_reader = _CHECKER._read_document

            def read_document(repo, raw, field):
                text = original_reader(repo, raw, field)
                if field in {"documents.rfc", "documents.threat_model"}:
                    text = text.replace(
                        "semantic-provider-proposal-status: review-ready",
                        f"semantic-provider-proposal-status: {document_status}",
                    )
                    text = text.replace(
                        "semantic-provider-implementation-allowed: false",
                        "semantic-provider-implementation-allowed: "
                        + str(document_implementation_allowed).lower(),
                    )
                    text = text.replace(
                        "semantic-provider-v0.15-work-allowed: false",
                        "semantic-provider-v0.15-work-allowed: "
                        + str(document_v0_15_work_allowed).lower(),
                    )
                return text

            with mock.patch.object(
                _CHECKER,
                "_read_document",
                side_effect=read_document,
            ):
                return validate()

    @staticmethod
    def _mark_accepted(value):
        value["status"] = "accepted"
        value["implementation_allowed"] = True
        value["v0_15_work_allowed"] = True
        value["red_team"]["status"] = "passed"
        value["red_team"]["residual_risks_accepted"] = True
        for round_record in value["red_team"]["rounds"]:
            round_record["status"] = "passed"
            if not round_record["evidence"]:
                round_record["evidence"] = ["exact reviewed proposal evidence"]
        for verification in value["verifications"]:
            if verification["phase"] == "proposal":
                verification["status"] = "passed"
                if not verification["evidence"]:
                    verification["evidence"] = ["exact proposal gate evidence"]
        for finding in value["open_findings"]:
            finding["disposition"] = "closed"
            finding["rationale"] = "Closed by exact live-state evidence."
            finding["evidence"] = ["exact closure evidence"]

    def _review_requirements(self):
        return self._manifest()["review_requirements"]

    def _release_requirements(self):
        return self._manifest()["release_gates"]["v0.15.0"]

    @staticmethod
    def _configured_roster_body(
        *,
        security_id=2,
        security_login="security-reviewer",
        product_id=3,
        product_login="product-reviewer",
    ):
        return "\n".join(
            (
                "semantic-provider-review-roster/v2",
                "Repository-id: 1226008327",
                "Repository-owner-id: 22440724",
                f"Security-reviewer: {security_id}:{security_login}",
                f"Product-reviewer: {product_id}:{product_login}",
                "Independent-beneficial-owners-attested: true",
                "Owner-exclusive-mutation-authority-attested: true",
                "Attested-by: 22440724:yzm1",
            )
        )

    def _review_snapshot(self):
        record_commit = "a" * 40
        record_parent = "d" * 40
        reviewed_commit = "b" * 40
        tree = "c" * 40
        marker = self._review_requirements()["security_review_marker"]
        return {
            "repository": "yzm1/boundver",
            "repository_id": 1226008327,
            "repository_owner": {
                "id": 22440724,
                "login": "yzm1",
                "type": "User",
            },
            "review_authority": {
                "source": "github-account-owned-public-gist/v1",
                "repository_mutation_authority": {
                    "owner": {
                        "id": 22440724,
                        "login": "yzm1",
                        "type": "User",
                    },
                    "owner_attested_exclusive_mutation_authority": True,
                    "repository_collaborators": [
                        {
                            "actor": {
                                "id": 22440724,
                                "login": "yzm1",
                                "type": "User",
                            },
                            "role_name": "admin",
                            "permissions": {
                                "admin": True,
                                "maintain": True,
                                "push": True,
                                "triage": True,
                                "pull": True,
                            },
                        }
                    ],
                },
                "roster": {
                    "id": "0caedb798d168b974f9d9fb63c377f73",
                    "node_id": (
                        "G_kwDOAVZrFNoAIDBjYWVkYjc5OGQxNjhiOTc0ZjlkOWZiNjNjMzc3Zjcz"
                    ),
                    "description": (
                        "boundver semantic-provider independent reviewer roster"
                    ),
                    "url": (
                        "https://api.github.com/gists/"
                        "0caedb798d168b974f9d9fb63c377f73"
                    ),
                    "html_url": (
                        "https://gist.github.com/yzm1/"
                        "0caedb798d168b974f9d9fb63c377f73"
                    ),
                    "owner": {
                        "id": 22440724,
                        "login": "yzm1",
                        "type": "User",
                    },
                    "public": True,
                    "created_at": "2026-08-30T09:00:00Z",
                    "updated_at": "2026-08-30T10:00:00Z",
                    "latest_revision": {
                        "version": "1" * 40,
                        "committed_at": "2026-08-30T09:59:59Z",
                        "owner": {
                            "id": 22440724,
                            "login": "yzm1",
                            "type": "User",
                        },
                        "url": (
                            "https://api.github.com/gists/"
                            "0caedb798d168b974f9d9fb63c377f73/"
                            + "1" * 40
                        ),
                        "change_status": {
                            "total": 7,
                            "additions": 7,
                            "deletions": 0,
                        },
                    },
                    "file": {
                        "filename": "semantic-provider-review-roster.txt",
                        "type": "text/plain",
                        "language": "Text",
                        "raw_url": (
                            "https://gist.githubusercontent.com/yzm1/"
                            "0caedb798d168b974f9d9fb63c377f73/raw/"
                            + "2" * 40
                            + "/semantic-provider-review-roster.txt"
                        ),
                        "size": len(self._configured_roster_body().encode("utf-8")),
                        "truncated": False,
                        "content": self._configured_roster_body(),
                        "encoding": "utf-8",
                    },
                },
                "reviewers": {
                    "product": {
                        "reviewer": {
                            "id": 3,
                            "login": "product-reviewer",
                            "type": "User",
                        },
                        "repository_permission": {
                            "permission": "read",
                            "role_name": "read",
                            "permissions": {
                                "admin": False,
                                "maintain": False,
                                "push": False,
                                "triage": False,
                                "pull": True,
                            },
                        },
                    },
                    "security": {
                        "reviewer": {
                            "id": 2,
                            "login": "security-reviewer",
                            "type": "User",
                        },
                        "repository_permission": {
                            "permission": "read",
                            "role_name": "read",
                            "permissions": {
                                "admin": False,
                                "maintain": False,
                                "push": False,
                                "triage": False,
                                "pull": True,
                            },
                        },
                    },
                },
            },
            "record_commit": record_commit,
            "record_parent": record_parent,
            "local_tree": tree,
            "record_tree": tree,
            "canonical_main": record_commit,
            "main_comparison_status": "identical",
            "main_merge_base": record_commit,
            "pull_request": {
                "number": 80,
                "author": {"id": 22440724, "login": "yzm1", "type": "User"},
                "head_sha": reviewed_commit,
                "reviewed_tree": tree,
                "merge_commit": record_commit,
                "merged_at": "2026-08-30T10:10:00Z",
                "base_repository": "yzm1/boundver",
                "base_ref": "main",
                "base_commit": record_parent,
                "requested_reviewers": [],
                "requested_teams": [],
                "review_decision": "APPROVED",
                "threads": [{"id": "thread-1", "is_resolved": True}],
                "reviews": [
                    {
                        "id": 101,
                        "state": "APPROVED",
                        "submitted_at": "2026-08-30T10:01:00Z",
                        "last_edited_at": None,
                        "commit_id": reviewed_commit,
                        "body": (
                            f"{marker}\n"
                            f"Reviewed-commit: {reviewed_commit}\n"
                            "Independent-reviewer: confirmed\n"
                            "Verdict: approved\n"
                        ),
                        "reviewer": {
                            "id": 2,
                            "login": "security-reviewer",
                            "type": "User",
                        },
                    },
                    {
                        "id": 102,
                        "state": "APPROVED",
                        "submitted_at": "2026-08-30T10:02:00Z",
                        "last_edited_at": None,
                        "commit_id": reviewed_commit,
                        "body": (
                            f"{self._review_requirements()['product_review_marker']}\n"
                            f"Reviewed-commit: {reviewed_commit}\n"
                            "Independent-reviewer: confirmed\n"
                            "Verdict: approved\n"
                        ),
                        "reviewer": {
                            "id": 3,
                            "login": "product-reviewer",
                            "type": "User",
                        },
                    },
                ],
            },
        }

    def _evaluate_review(self, snapshot):
        return _AUDITOR.evaluate_snapshot(
            snapshot,
            self._review_requirements(),
            evaluated_at=self.AUDIT_TIME,
        )

    def _release_snapshot(self):
        snapshot = self._review_snapshot()
        record_commit = "e" * 40
        record_parent = "f" * 40
        reviewed_commit = "9" * 40
        tree = "8" * 40
        snapshot.update(
            {
                "record_commit": record_commit,
                "record_parent": record_parent,
                "local_tree": tree,
                "record_tree": tree,
                "canonical_main": record_commit,
                "main_comparison_status": "identical",
                "main_merge_base": record_commit,
            }
        )
        pull_request = snapshot["pull_request"]
        pull_request.update(
            {
                "number": 95,
                "head_sha": reviewed_commit,
                "reviewed_tree": tree,
                "merge_commit": record_commit,
                "base_commit": record_parent,
            }
        )
        for review in pull_request["reviews"]:
            review["commit_id"] = reviewed_commit
        pull_request["reviews"][0]["body"] = "\n".join(
            (
                self._release_requirements()["security_review_marker"],
                f"Reviewed-commit: {reviewed_commit}",
                "Independent-reviewer: confirmed",
                *_AUDITOR.V015_RELEASE_ATTESTATIONS,
                "Verdict: approved",
                "",
            )
        )
        pull_request["reviews"][1]["body"] = "\n".join(
            (
                self._release_requirements()["product_review_marker"],
                f"Reviewed-commit: {reviewed_commit}",
                "Independent-reviewer: confirmed",
                "Verdict: approved",
                "",
            )
        )
        return snapshot

    def _evaluate_release(self, snapshot):
        return _AUDITOR.evaluate_snapshot(
            snapshot,
            self._release_requirements(),
            evaluated_at=self.AUDIT_TIME,
            attestations=_AUDITOR.V015_RELEASE_ATTESTATIONS,
        )

    def _initialize_gate_repository(self, directory):
        root = Path(directory)
        subprocess.run(["git", "init", "--quiet"], cwd=root, check=True, timeout=30)
        subprocess.run(
            ["git", "config", "user.name", "Proposal Test"],
            cwd=root,
            check=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "config", "user.email", "proposal@example.invalid"],
            cwd=root,
            check=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "config", "core.autocrlf", "false"],
            cwd=root,
            check=True,
            timeout=30,
        )
        for relative in _AUDITOR.BOOTSTRAP_PATHS:
            path = root.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"bootstrap:{relative}\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "--", *_AUDITOR.BOOTSTRAP_PATHS],
            cwd=root,
            check=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "bootstrap gate"],
            cwd=root,
            check=True,
            timeout=30,
        )
        return root

    def test_current_proposal_matches_json_schema(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        manifest = self._manifest()
        Draft202012Validator.check_schema(schema)
        errors = sorted(
            Draft202012Validator(schema).iter_errors(manifest),
            key=lambda error: list(error.absolute_path),
        )
        self.assertEqual(errors, [])

    def test_current_review_ready_proposal_is_complete_and_fail_closed(self):
        result = _CHECKER.validate_proposal(ROOT, MANIFEST)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "review-ready")
        self.assertEqual(result["threats"], 43)
        self.assertEqual(result["controls"], 45)
        self.assertEqual(result["verifications"], 38)
        self.assertFalse(result["implementation_allowed"])
        self.assertFalse(result["v0_15_work_allowed"])
        self.assertFalse(result["v0_15_release_allowed"])
        self.assertTrue(result["acceptance_blockers"])

    def test_acceptance_work_and_release_modes_are_blocked(self):
        for keyword in (
            "require_accepted",
            "require_v0_15_work",
            "require_v0_15_release",
        ):
            with self.subTest(keyword=keyword):
                with self.assertRaises(_CHECKER.ProposalError):
                    _CHECKER.validate_proposal(ROOT, MANIFEST, **{keyword: True})

    def test_unaccepted_record_cannot_enable_implementation(self):
        def mutate(value):
            value["implementation_allowed"] = True
            value["v0_15_work_allowed"] = True

        with self.assertRaisesRegex(
            _CHECKER.ProposalError,
            "unaccepted proposal cannot allow",
        ):
            self._validate_mutation(
                mutate,
                document_status="review-ready",
                document_implementation_allowed=True,
                document_v0_15_work_allowed=True,
            )

    def test_exact_external_authority_unlocks_only_complete_accepted_record(self):
        with self.assertRaisesRegex(_CHECKER.ProposalError, "marker must equal"):
            self._validate_mutation(
                self._mark_accepted,
                authoritative_review_passed=True,
            )

        result = self._validate_mutation(
            self._mark_accepted,
            document_status="accepted",
            authoritative_review_passed=True,
            require_accepted=True,
            require_v0_15_work=True,
        )
        self.assertTrue(result["implementation_allowed"])
        self.assertTrue(result["v0_15_work_allowed"])
        self.assertEqual(result["acceptance_blockers"], [])

        result = self._validate_mutation(
            self._mark_accepted,
            document_status="accepted",
            authoritative_review_passed=True,
            authoritative_release_passed=True,
            require_v0_15_release=True,
        )
        self.assertTrue(result["v0_15_release_allowed"])

        with self.assertRaisesRegex(_CHECKER.ProposalError, "release is blocked"):
            self._validate_mutation(
                self._mark_accepted,
                document_status="accepted",
                authoritative_review_passed=True,
                require_v0_15_release=True,
            )
        with self.assertRaisesRegex(_CHECKER.ProposalError, "cannot bypass"):
            self._validate_mutation(
                self._mark_accepted,
                document_status="accepted",
                authoritative_release_passed=True,
            )

    def test_structural_gate_rejects_malformed_assurance_records(self):
        def orphan_verification(value):
            threat = next(item for item in value["threats"] if item["id"] == "SPT-042")
            threat["verifications"].remove("SPV-037")
            control = next(
                item for item in value["controls"] if item["id"] == "SPC-042"
            )
            control["verifications"].remove("SPV-037")

        def duplicate_finding(value):
            value["red_team"]["rounds"][1]["findings"].insert(0, "RTF-001")

        malformed = (
            ("unknown root", lambda value: value.update({"unexpected": True})),
            ("missing root", lambda value: value.pop("proposal")),
            ("boolean schema", lambda value: value.update({"schema_version": True})),
            ("wrong proposal", lambda value: value.update({"proposal": "wrong/v1"})),
            ("bad status", lambda value: value.update({"status": "almost"})),
            (
                "bad documents",
                lambda value: value["documents"].update({"extra": "README.md"}),
            ),
            (
                "threat fields",
                lambda value: value["threats"][0].update({"extra": True}),
            ),
            (
                "threat severity",
                lambda value: value["threats"][0].update({"severity": "urgent"}),
            ),
            (
                "unknown threat control",
                lambda value: value["threats"][0]["controls"].append("SPC-999"),
            ),
            (
                "unknown threat verification",
                lambda value: value["threats"][0]["verifications"].append("SPV-999"),
            ),
            (
                "control fields",
                lambda value: value["controls"][0].update({"extra": True}),
            ),
            (
                "control kinds",
                lambda value: value["controls"][0].update({"kinds": []}),
            ),
            (
                "duplicate control kind",
                lambda value: value["controls"][1].update(
                    {"kinds": ["preventive", "preventive"]}
                ),
            ),
            (
                "unknown control threat",
                lambda value: value["controls"][0].update({"threats": ["SPT-999"]}),
            ),
            (
                "unknown control verification",
                lambda value: value["controls"][0].update(
                    {"verifications": ["SPV-999"]}
                ),
            ),
            (
                "verification fields",
                lambda value: value["verifications"][0].update({"extra": True}),
            ),
            (
                "verification phase",
                lambda value: value["verifications"][0].update({"phase": "later"}),
            ),
            (
                "verification status",
                lambda value: value["verifications"][0].update({"status": "green"}),
            ),
            (
                "not-applicable evidence",
                lambda value: value["verifications"][3].update(
                    {"status": "not-applicable", "evidence": []}
                ),
            ),
            ("orphan verification", orphan_verification),
            (
                "red team fields",
                lambda value: value["red_team"].update({"extra": True}),
            ),
            (
                "red team status",
                lambda value: value["red_team"].update({"status": "green"}),
            ),
            (
                "round fields",
                lambda value: value["red_team"]["rounds"][0].update({"extra": True}),
            ),
            (
                "round status",
                lambda value: value["red_team"]["rounds"][0].update(
                    {"status": "green"}
                ),
            ),
            ("duplicate finding", duplicate_finding),
            (
                "review fields",
                lambda value: value["review_requirements"].update({"extra": True}),
            ),
            (
                "review repository",
                lambda value: value["review_requirements"].update(
                    {"repository": "attacker/fork"}
                ),
            ),
            (
                "review repository ID",
                lambda value: value["review_requirements"].update({"repository_id": 1}),
            ),
            (
                "review base",
                lambda value: value["review_requirements"].update(
                    {"base_branch": "develop"}
                ),
            ),
            (
                "review authority",
                lambda value: value["review_requirements"].update(
                    {"reviewer_authority": "repository-write/v1"}
                ),
            ),
            (
                "review roster gist id",
                lambda value: value["review_requirements"].update(
                    {"review_roster_gist_id": "attacker"}
                ),
            ),
            (
                "review roster gist node",
                lambda value: value["review_requirements"].update(
                    {"review_roster_gist_node_id": "attacker"}
                ),
            ),
            (
                "review roster gist description",
                lambda value: value["review_requirements"].update(
                    {"review_roster_gist_description": "attacker-controlled"}
                ),
            ),
            (
                "review roster gist filename",
                lambda value: value["review_requirements"].update(
                    {"review_roster_gist_filename": "attacker.txt"}
                ),
            ),
            (
                "review distinct roster",
                lambda value: value["review_requirements"].update(
                    {"distinct_roster_reviewers_required": False}
                ),
            ),
            (
                "review owner-exclusive mutation authority",
                lambda value: value["review_requirements"].update(
                    {"owner_exclusive_repository_collaborators_required": False}
                ),
            ),
            (
                "review owner mutation attestation",
                lambda value: value["review_requirements"].update(
                    {
                        "owner_exclusive_mutation_authority_attestation_required": False
                    }
                ),
            ),
            (
                "review count",
                lambda value: value["review_requirements"].update(
                    {"minimum_non_author_reviews": 1}
                ),
            ),
            (
                "review age",
                lambda value: value["review_requirements"].update(
                    {"maximum_review_age_days": 365}
                ),
            ),
            (
                "review flag",
                lambda value: value["review_requirements"].update(
                    {"security_review_required": False}
                ),
            ),
            (
                "review marker",
                lambda value: value["review_requirements"].update(
                    {"security_review_marker": "looks-approved"}
                ),
            ),
            (
                "product review flag",
                lambda value: value["review_requirements"].update(
                    {"product_review_required": False}
                ),
            ),
            (
                "product review marker",
                lambda value: value["review_requirements"].update(
                    {"product_review_marker": "looks-approved"}
                ),
            ),
            (
                "reviewer independence marker",
                lambda value: value["review_requirements"].update(
                    {"reviewer_independence_attestation": "self-asserted"}
                ),
            ),
            (
                "review auditor",
                lambda value: value["review_requirements"].update(
                    {"authoritative_audit": "scripts/other.py"}
                ),
            ),
            ("empty references", lambda value: value.update({"references": []})),
            (
                "insecure reference",
                lambda value: value["references"].__setitem__(0, "http://invalid"),
            ),
            (
                "release names",
                lambda value: value["release_gates"].update({"v9": {}}),
            ),
            (
                "release fields",
                lambda value: value["release_gates"]["v0.15.0"].update({"extra": True}),
            ),
            (
                "release evidence source",
                lambda value: value["release_gates"]["v0.15.0"].update(
                    {"evidence_source": "manifest-self-attestation/v1"}
                ),
            ),
            (
                "release reviewer authority",
                lambda value: value["release_gates"]["v0.15.0"].update(
                    {"reviewer_authority": "repository-write/v1"}
                ),
            ),
            (
                "release reviewer roster",
                lambda value: value["release_gates"]["v0.15.0"].update(
                    {"review_roster_gist_id": "attacker"}
                ),
            ),
            (
                "release distinct roster",
                lambda value: value["release_gates"]["v0.15.0"].update(
                    {"distinct_roster_reviewers_required": False}
                ),
            ),
            (
                "release owner-exclusive mutation authority",
                lambda value: value["release_gates"]["v0.15.0"].update(
                    {"owner_exclusive_repository_collaborators_required": False}
                ),
            ),
            (
                "release owner mutation attestation",
                lambda value: value["release_gates"]["v0.15.0"].update(
                    {
                        "owner_exclusive_mutation_authority_attestation_required": False
                    }
                ),
            ),
            (
                "release product marker",
                lambda value: value["release_gates"]["v0.15.0"].update(
                    {"product_review_marker": "looks-approved"}
                ),
            ),
            (
                "release repository ID",
                lambda value: value["release_gates"]["v0.15.0"].update(
                    {"repository_id": 1}
                ),
            ),
            (
                "release exact tree",
                lambda value: value["release_gates"]["v0.15.0"].update(
                    {"exact_tree_required": False}
                ),
            ),
            (
                "release attestation order",
                lambda value: value["release_gates"]["v0.15.0"][
                    "required_attestations"
                ].reverse(),
            ),
        )
        for label, mutate in malformed:
            with self.subTest(label=label):
                with self.assertRaises(_CHECKER.ProposalError):
                    self._validate_mutation(mutate)

    def test_finding_dispositions_are_bounded_and_blocking(self):
        base = {
            "id": "RTF-039",
            "severity": "high",
            "title": "Selected-output disclosure",
            "owner": "boundver maintainers",
            "disposition": "open",
        }

        def add_open(value):
            value["open_findings"] = [copy.deepcopy(base)]

        result = self._validate_mutation(add_open)
        self.assertIn(
            "Open red-team findings remain unresolved", result["acceptance_blockers"]
        )

        def add_closed(value):
            finding = copy.deepcopy(base)
            finding.update(
                {
                    "disposition": "closed",
                    "rationale": "Closed by reviewed controls.",
                    "evidence": ["exact closure evidence"],
                }
            )
            value["open_findings"] = [finding]

        result = self._validate_mutation(add_closed)
        self.assertNotIn(
            "Open red-team findings remain unresolved", result["acceptance_blockers"]
        )

        def add_accepted(value):
            finding = copy.deepcopy(base)
            finding.update(
                {
                    "disposition": "accepted",
                    "rationale": "Temporary bounded exception.",
                    "expires": "2099-01-01",
                    "compensating_controls": ["SPC-039"],
                    "evidence": ["exact acceptance evidence"],
                }
            )
            value["open_findings"] = [finding]

        result = self._validate_mutation(add_accepted)
        self.assertIn(
            "Critical/High/Medium findings remain unresolved",
            result["acceptance_blockers"],
        )

    def test_release_authority_is_external_and_cannot_bypass_proposal_review(self):
        with self.assertRaisesRegex(_CHECKER.ProposalError, "cannot bypass"):
            _CHECKER.validate_proposal(
                ROOT,
                MANIFEST,
                authoritative_release_passed=True,
            )

    def test_threat_control_mapping_must_be_bidirectional(self):
        def mutate(value):
            value["controls"][0]["threats"] = ["SPT-001"]

        with self.assertRaisesRegex(
            _CHECKER.ProposalError,
            "mapping is not bidirectional",
        ):
            self._validate_mutation(mutate)

    def test_high_threat_requires_defense_in_depth(self):
        def mutate(value):
            threat = next(item for item in value["threats"] if item["id"] == "SPT-008")
            threat["controls"] = ["SPC-027"]
            control = next(
                item for item in value["controls"] if item["id"] == "SPC-028"
            )
            control["threats"].remove("SPT-008")

        with self.assertRaisesRegex(
            _CHECKER.ProposalError,
            "at least two defense-in-depth controls",
        ):
            self._validate_mutation(mutate)

    def test_passed_verification_requires_evidence(self):
        def mutate(value):
            verification = next(
                item for item in value["verifications"] if item["status"] == "planned"
            )
            verification["status"] = "passed"

        with self.assertRaisesRegex(
            _CHECKER.ProposalError,
            "must contain evidence",
        ):
            self._validate_mutation(mutate)

    def test_cli_emits_bounded_machine_result(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                str(CHECKER_PATH),
                "--repo",
                str(ROOT),
                "--format",
                "json",
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertFalse(result["v0_15_work_allowed"])
        self.assertEqual(completed.stderr, "")

    def test_authoritative_review_snapshot_passes(self):
        snapshot = self._review_snapshot()
        self.assertNotIn("permissions", snapshot["pull_request"])
        result = self._evaluate_review(snapshot)
        self.assertEqual(result["pull_request"], 80)
        self.assertEqual(result["reviewers"], ["product-reviewer", "security-reviewer"])
        self.assertEqual(result["security_reviewers"], ["security-reviewer"])
        self.assertEqual(result["product_reviewers"], ["product-reviewer"])

    def test_legacy_unconfigured_roster_fails_closed_with_specific_reason(self):
        body = "\n".join(
            (
                "semantic-provider-review-roster/v1",
                "Repository-id: 1226008327",
                "Repository-owner-id: 22440724",
                "Security-reviewer: unconfigured",
                "Product-reviewer: unconfigured",
                "Independent-beneficial-owners-attested: false",
                "Attested-by: 22440724:yzm1",
            )
        )
        with self.assertRaisesRegex(
            _AUDITOR.AuditError,
            "semantic review roster is not configured and attested",
        ):
            _AUDITOR._parse_review_roster_body(
                body,
                repository_id=1226008327,
                repository_owner_id=22440724,
            )

    def test_review_authority_public_gist_matrix_fails_closed(self):
        def replace_content(value, content):
            file_record = value["review_authority"]["roster"]["file"]
            file_record["content"] = content
            file_record["size"] = len(content.encode("utf-8"))

        mutations = {
            "source": lambda value: value["review_authority"].update(
                {"source": "repository-write/v1"}
            ),
            "missing mutation authority": lambda value: value[
                "review_authority"
            ].pop("repository_mutation_authority"),
            "extra repository collaborator": lambda value: value["review_authority"][
                "repository_mutation_authority"
            ]["repository_collaborators"].append(
                {
                    "actor": {"id": 4, "login": "attacker", "type": "User"},
                    "role_name": "write",
                    "permissions": {
                        "admin": False,
                        "maintain": False,
                        "push": True,
                        "triage": True,
                        "pull": True,
                    },
                }
            ),
            "mutation owner mismatch": lambda value: value["review_authority"][
                "repository_mutation_authority"
            ]["owner"].update({"id": 4, "login": "attacker"}),
            "mutation owner attestation false": lambda value: value[
                "review_authority"
            ]["repository_mutation_authority"].update(
                {"owner_attested_exclusive_mutation_authority": False}
            ),
            "mutation role downgrade": lambda value: value["review_authority"][
                "repository_mutation_authority"
            ]["repository_collaborators"][0].update({"role_name": "write"}),
            "mutation permission numeric bool": lambda value: value[
                "review_authority"
            ]["repository_mutation_authority"]["repository_collaborators"][0][
                "permissions"
            ].update({"admin": 1}),
            "missing roster": lambda value: value["review_authority"].pop("roster"),
            "missing reviewer": lambda value: value["review_authority"][
                "reviewers"
            ].pop("product"),
            "gist id": lambda value: value["review_authority"]["roster"].update(
                {"id": "attacker"}
            ),
            "gist node": lambda value: value["review_authority"]["roster"].update(
                {"node_id": "attacker"}
            ),
            "gist description": lambda value: value["review_authority"][
                "roster"
            ].update(
                {"description": "attacker-controlled"}
            ),
            "gist url": lambda value: value["review_authority"]["roster"].update(
                {"url": "https://api.github.com/attacker"}
            ),
            "gist owner": lambda value: value["review_authority"]["roster"][
                "owner"
            ].update({"id": 4, "login": "attacker"}),
            "private gist": lambda value: value["review_authority"][
                "roster"
            ].update({"public": False}),
            "timestamp inversion": lambda value: value["review_authority"][
                "roster"
            ].update({"created_at": "2026-08-30T10:00:01Z"}),
            "revision id": lambda value: value["review_authority"]["roster"][
                "latest_revision"
            ].update({"version": "2" * 40}),
            "revision owner": lambda value: value["review_authority"]["roster"][
                "latest_revision"
            ]["owner"].update({"id": 4, "login": "attacker"}),
            "revision after update": lambda value: value["review_authority"][
                "roster"
            ]["latest_revision"].update(
                {"committed_at": "2026-08-30T10:00:01Z"}
            ),
            "revision count bool": lambda value: value["review_authority"][
                "roster"
            ]["latest_revision"]["change_status"].update({"total": True}),
            "revision count mismatch": lambda value: value["review_authority"][
                "roster"
            ]["latest_revision"]["change_status"].update({"total": 8}),
            "file name": lambda value: value["review_authority"]["roster"][
                "file"
            ].update({"filename": "attacker.txt"}),
            "file truncated": lambda value: value["review_authority"]["roster"][
                "file"
            ].update({"truncated": True}),
            "file size bool": lambda value: value["review_authority"]["roster"][
                "file"
            ].update({"size": True}),
            "file raw url": lambda value: value["review_authority"]["roster"][
                "file"
            ].update({"raw_url": "https://attacker.invalid/roster"}),
            "unconfigured body": lambda value: replace_content(
                value,
                self._configured_roster_body().replace(
                        "Security-reviewer: 2:security-reviewer",
                        "Security-reviewer: unconfigured",
                ),
            ),
            "false independence": lambda value: replace_content(
                value,
                self._configured_roster_body().replace(
                    "Independent-beneficial-owners-attested: true",
                    "Independent-beneficial-owners-attested: false",
                ),
            ),
            "false owner mutation attestation": lambda value: replace_content(
                value,
                self._configured_roster_body().replace(
                    "Owner-exclusive-mutation-authority-attested: true",
                    "Owner-exclusive-mutation-authority-attested: false",
                ),
            ),
            "non-canonical body": lambda value: replace_content(
                value, self._configured_roster_body() + "\n"
            ),
            "body reviewer mismatch": lambda value: replace_content(
                value,
                self._configured_roster_body(
                    product_id=30, product_login="replacement"
                ),
            ),
            "write permission": lambda value: value["review_authority"]["reviewers"][
                "product"
            ]["repository_permission"].update(
                {"permission": "write", "role_name": "write"}
            ),
            "numeric permission flag": lambda value: value["review_authority"][
                "reviewers"
            ]["product"]["repository_permission"]["permissions"].update(
                {"admin": 0}
            ),
            "repository owner": lambda value: (
                replace_content(
                    value,
                    self._configured_roster_body(
                        product_id=22440724, product_login="yzm1"
                    ),
                ),
                value["review_authority"]["reviewers"]["product"].update(
                    {
                        "reviewer": {
                            "id": 22440724,
                            "login": "yzm1",
                            "type": "User",
                        }
                    }
                ),
            ),
            "roster changed after approval": lambda value: value["review_authority"][
                "roster"
            ].update({"updated_at": "2026-08-30T10:02:00Z"}),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                snapshot = self._review_snapshot()
                mutate(snapshot)
                with self.assertRaises(_AUDITOR.AuditError):
                    self._evaluate_review(snapshot)

    def test_external_review_aggregate_state_is_fail_closed_but_not_required(self):
        snapshot = self._review_snapshot()
        snapshot["pull_request"]["review_decision"] = None
        self.assertEqual(self._evaluate_review(snapshot)["pull_request"], 80)

        for decision in ("CHANGES_REQUESTED", "REVIEW_REQUIRED"):
            with self.subTest(decision=decision):
                snapshot = self._review_snapshot()
                snapshot["pull_request"]["review_decision"] = decision
                with self.assertRaisesRegex(_AUDITOR.AuditError, "blocking"):
                    self._evaluate_review(snapshot)

    def test_only_designated_security_reviewer_can_supply_marker(self):
        snapshot = self._review_snapshot()
        marker = snapshot["pull_request"]["reviews"][0]["body"]
        snapshot["pull_request"]["reviews"][0]["body"] = ""
        snapshot["pull_request"]["reviews"][1]["body"] = marker
        with self.assertRaisesRegex(_AUDITOR.AuditError, "security review"):
            self._evaluate_review(snapshot)

    def test_product_marker_is_role_specific_and_exact(self):
        reviewed = "b" * 40
        for body in (
            "",
            (
                "semantic-provider-security-review/v1\n"
                f"Reviewed-commit: {reviewed}\n"
                "Independent-reviewer: confirmed\n"
                "Verdict: approved\n"
            ),
            (
                "semantic-provider-product-review/v1\n"
                f"Reviewed-commit: {reviewed}\n"
                "Verdict: approved\n"
            ),
            (
                "semantic-provider-product-review/v1\n"
                f"Reviewed-commit: {'d' * 40}\n"
                "Independent-reviewer: confirmed\n"
                "Verdict: approved\n"
            ),
        ):
            with self.subTest(body=body):
                snapshot = self._review_snapshot()
                snapshot["pull_request"]["reviews"][1]["body"] = body
                with self.assertRaisesRegex(_AUDITOR.AuditError, "product review"):
                    self._evaluate_review(snapshot)

    def test_public_gist_api_normalization_is_bounded_and_fail_closed(self):
        def records():
            normalized = self._review_snapshot()["review_authority"]
            roster = normalized["roster"]
            revision = roster["latest_revision"]

            def api_record(url):
                return {
                    "id": roster["id"],
                    "node_id": roster["node_id"],
                    "description": roster["description"],
                    "url": url,
                    "html_url": roster["html_url"],
                    "owner": copy.deepcopy(roster["owner"]),
                    "public": True,
                    "user": None,
                    "truncated": False,
                    "created_at": roster["created_at"],
                    "updated_at": roster["updated_at"],
                    "history": [
                        {
                            "version": revision["version"],
                            "committed_at": revision["committed_at"],
                            "user": copy.deepcopy(revision["owner"]),
                            "url": revision["url"],
                            "change_status": copy.deepcopy(
                                revision["change_status"]
                            ),
                        }
                    ],
                    "files": {
                        roster["file"]["filename"]: copy.deepcopy(roster["file"])
                    },
                }

            gist_endpoint = "gists/0caedb798d168b974f9d9fb63c377f73"
            revision_endpoint = gist_endpoint + "/" + revision["version"]
            result = {
                gist_endpoint: api_record(roster["url"]),
                revision_endpoint: api_record(revision["url"]),
                "repos/yzm1/boundver/collaborators": [
                    {
                        "id": 22440724,
                        "login": "yzm1",
                        "type": "User",
                        "role_name": "admin",
                        "permissions": {
                            "admin": True,
                            "maintain": True,
                            "push": True,
                            "triage": True,
                            "pull": True,
                        },
                    }
                ],
            }
            for entry in normalized["reviewers"].values():
                reviewer = entry["reviewer"]
                result[
                    "repos/yzm1/boundver/collaborators/"
                    + reviewer["login"]
                    + "/permission"
                ] = {
                    "permission": "read",
                    "role_name": "read",
                    "user": {
                        **copy.deepcopy(reviewer),
                        "permissions": {
                            "admin": False,
                            "maintain": False,
                            "push": False,
                            "triage": False,
                            "pull": True,
                        },
                    },
                }
            return result

        def collect(values):
            client = mock.Mock(spec=_AUDITOR.GitHubClient)
            client.rest.side_effect = lambda endpoint, _label: copy.deepcopy(
                values[endpoint]
            )
            client.rest_pages.side_effect = lambda endpoint, _label: copy.deepcopy(
                values[endpoint]
            )
            return _AUDITOR._collect_review_authority(
                client, self._review_requirements()
            )

        authority = collect(records())
        self.assertEqual(authority["reviewers"]["security"]["reviewer"]["id"], 2)

        gist_endpoint = "gists/0caedb798d168b974f9d9fb63c377f73"
        revision_endpoint = gist_endpoint + "/" + "1" * 40
        product_permission = (
            "repos/yzm1/boundver/collaborators/product-reviewer/permission"
        )
        repository_collaborators = "repos/yzm1/boundver/collaborators"

        def replace_content(values, content, endpoint=gist_endpoint):
            file_record = values[endpoint]["files"][
                "semantic-provider-review-roster.txt"
            ]
            file_record["content"] = content
            file_record["size"] = len(content.encode("utf-8"))

        mutations = {
            "extra repository collaborator": lambda values: values[
                repository_collaborators
            ].append(
                {
                    "id": 4,
                    "login": "attacker",
                    "type": "User",
                    "role_name": "write",
                    "permissions": {
                        "admin": False,
                        "maintain": False,
                        "push": True,
                        "triage": True,
                        "pull": True,
                    },
                }
            ),
            "repository permission numeric bool": lambda values: values[
                repository_collaborators
            ][0]["permissions"].update({"admin": 1}),
            "record type": lambda values: values.__setitem__(gist_endpoint, []),
            "gist id": lambda values: values[gist_endpoint].update({"id": "wrong"}),
            "gist node": lambda values: values[gist_endpoint].update(
                {"node_id": "wrong"}
            ),
            "description": lambda values: values[gist_endpoint].update(
                {"description": "wrong"}
            ),
            "gist url": lambda values: values[gist_endpoint].update(
                {"url": "https://api.github.com/wrong"}
            ),
            "gist html url": lambda values: values[gist_endpoint].update(
                {"html_url": "https://gist.github.com/attacker/wrong"}
            ),
            "wrong owner": lambda values: values[gist_endpoint]["owner"].update(
                {"id": 4, "login": "attacker"}
            ),
            "private": lambda values: values[gist_endpoint].update({"public": False}),
            "authenticated user": lambda values: values[gist_endpoint].update(
                {"user": copy.deepcopy(values[gist_endpoint]["owner"])}
            ),
            "top truncated": lambda values: values[gist_endpoint].update(
                {"truncated": True}
            ),
            "unconfigured": lambda values: replace_content(
                values,
                self._configured_roster_body().replace(
                        "Product-reviewer: 3:product-reviewer",
                        "Product-reviewer: unconfigured",
                ),
            ),
            "non-canonical": lambda values: replace_content(
                values, self._configured_roster_body() + "\n"
            ),
            "invalid timestamp": lambda values: values[gist_endpoint].update(
                {"updated_at": "not-a-time"}
            ),
            "inverted timestamp": lambda values: values[gist_endpoint].update(
                {"created_at": "2026-08-30T10:00:01Z"}
            ),
            "missing history": lambda values: values[gist_endpoint].update(
                {"history": []}
            ),
            "excessive history": lambda values: values[gist_endpoint].update(
                {"history": values[gist_endpoint]["history"] * 101}
            ),
            "revision owner": lambda values: values[gist_endpoint]["history"][0][
                "user"
            ].update({"id": 4, "login": "attacker"}),
            "revision url": lambda values: values[gist_endpoint]["history"][0].update(
                {"url": "https://api.github.com/gists/wrong/revision"}
            ),
            "revision after update": lambda values: values[gist_endpoint]["history"][
                0
            ].update({"committed_at": "2026-08-30T10:00:01Z"}),
            "revision count bool": lambda values: values[gist_endpoint]["history"][
                0
            ]["change_status"].update({"total": True}),
            "revision count mismatch": lambda values: values[gist_endpoint][
                "history"
            ][0]["change_status"].update({"total": 8}),
            "canonical immutable race": lambda values: replace_content(
                values,
                self._configured_roster_body(
                    product_id=30, product_login="replacement"
                ),
                revision_endpoint,
            ),
            "immutable timestamp race": lambda values: values[
                revision_endpoint
            ].update({"updated_at": "2026-08-30T10:00:01Z"}),
            "extra file": lambda values: values[gist_endpoint]["files"].update(
                {
                    "extra.txt": copy.deepcopy(
                        values[gist_endpoint]["files"][
                            "semantic-provider-review-roster.txt"
                        ]
                    )
                }
            ),
            "file metadata extra": lambda values: values[gist_endpoint]["files"][
                "semantic-provider-review-roster.txt"
            ].update({"extra": True}),
            "file truncated": lambda values: values[gist_endpoint]["files"][
                "semantic-provider-review-roster.txt"
            ].update({"truncated": True}),
            "file size bool": lambda values: values[gist_endpoint]["files"][
                "semantic-provider-review-roster.txt"
            ].update({"size": True}),
            "file encoding": lambda values: values[gist_endpoint]["files"][
                "semantic-provider-review-roster.txt"
            ].update({"encoding": "base64"}),
            "file raw url": lambda values: values[gist_endpoint]["files"][
                "semantic-provider-review-roster.txt"
            ].update({"raw_url": "https://attacker.invalid/roster"}),
            "write permission": lambda values: values[product_permission].update(
                {"permission": "write", "role_name": "write"}
            ),
            "permission record type": lambda values: values.__setitem__(
                product_permission, []
            ),
            "permission role": lambda values: values[product_permission].update(
                {"role_name": "custom"}
            ),
            "permission actor mismatch": lambda values: values[product_permission][
                "user"
            ].update({"id": 30}),
            "missing permission flag": lambda values: values[product_permission][
                "user"
            ]["permissions"].pop("triage"),
            "numeric permission flag": lambda values: values[product_permission][
                "user"
            ]["permissions"].update({"admin": 0}),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                values = records()
                mutate(values)
                with self.assertRaises(_AUDITOR.AuditError):
                    collect(values)

    def test_authoritative_release_snapshot_passes_with_exact_attestation(self):
        result = self._evaluate_release(self._release_snapshot())
        self.assertEqual(result["pull_request"], 95)
        self.assertEqual(result["record_commit"], "e" * 40)
        self.assertEqual(result["reviewed_commit"], "9" * 40)
        self.assertEqual(result["valid_until"], "2026-09-13T10:01:00Z")
        self.assertEqual(result["reviewers"], ["product-reviewer", "security-reviewer"])
        self.assertEqual(result["security_reviewers"], ["security-reviewer"])
        self.assertEqual(result["product_reviewers"], ["product-reviewer"])

    def test_release_attestation_body_is_exact_and_ordered(self):
        mutations = {
            "missing": lambda lines: lines.pop(4),
            "reordered": lambda lines: lines.__setitem__(
                slice(2, 4), reversed(lines[2:4])
            ),
            "changed": lambda lines: lines.__setitem__(
                2, "Full-source-bug-scan: almost"
            ),
            "extra": lambda lines: lines.insert(-1, "Extra: passed"),
            "wrong commit": lambda lines: lines.__setitem__(
                1, f"Reviewed-commit: {'7' * 40}"
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                snapshot = self._release_snapshot()
                lines = snapshot["pull_request"]["reviews"][0]["body"].splitlines()
                mutate(lines)
                snapshot["pull_request"]["reviews"][0]["body"] = "\n".join(lines)
                with self.assertRaisesRegex(
                    _AUDITOR.AuditError,
                    "no qualifying exact-head security review marker",
                ):
                    self._evaluate_release(snapshot)

    def test_release_product_marker_is_exact_and_independent(self):
        for body in (
            "",
            (
                "semantic-provider-v0.15-product-review/v1\n"
                f"Reviewed-commit: {'9' * 40}\n"
                "Verdict: approved\n"
            ),
            (
                "semantic-provider-v0.15-product-review/v1\n"
                f"Reviewed-commit: {'7' * 40}\n"
                "Independent-reviewer: confirmed\n"
                "Verdict: approved\n"
            ),
        ):
            with self.subTest(body=body):
                snapshot = self._release_snapshot()
                snapshot["pull_request"]["reviews"][1]["body"] = body
                with self.assertRaisesRegex(_AUDITOR.AuditError, "product review"):
                    self._evaluate_release(snapshot)

    def test_release_review_is_fresh_independent_and_exact_tree_bound(self):
        mutations = {
            "one reviewer": lambda value: value["pull_request"]["reviews"].pop(),
            "wrong local tree": lambda value: value.update({"local_tree": "7" * 40}),
            "wrong reviewed tree": lambda value: value["pull_request"].update(
                {"reviewed_tree": "7" * 40}
            ),
            "wrong base": lambda value: value["pull_request"].update(
                {"base_commit": "7" * 40}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                snapshot = self._release_snapshot()
                mutate(snapshot)
                with self.assertRaises(_AUDITOR.AuditError):
                    self._evaluate_release(snapshot)

        stale = self._release_snapshot()
        stale["pull_request"]["merged_at"] = "2026-08-01T10:10:00Z"
        for index, review in enumerate(stale["pull_request"]["reviews"], start=1):
            review["submitted_at"] = f"2026-08-01T10:0{index}:00Z"
        with self.assertRaisesRegex(_AUDITOR.AuditError, "qualifying exact-head"):
            self._evaluate_release(stale)

    def test_release_requirements_are_fixed_and_external(self):
        requirements = _AUDITOR._load_release_requirements(MANIFEST)
        self.assertEqual(
            tuple(requirements["required_attestations"]),
            _AUDITOR.V015_RELEASE_ATTESTATIONS,
        )
        self.assertNotIn("exact_candidate_commit", requirements)
        self.assertNotIn("release_allowed", requirements)
        value = self._manifest()
        value["release_gates"]["v0.15.0"]["maximum_review_age_days"] = 15
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proposal.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(_AUDITOR.AuditError, "not authoritative"):
                _AUDITOR._load_release_requirements(path)

    def test_release_gate_enforcement_paths_are_structurally_protected(self):
        original_reader = _CHECKER._read_document
        mutations = {
            "semantic-provider release-tag gate": (
                "--gate v0.15-release",
                "--gate accepted",
            ),
            "semantic-provider publication gate": (
                '--release-sha "$RELEASE_SHA"',
                '--release-sha "$CONTROL_SHA"',
            ),
            "semantic-provider local release gate": (
                'if tag == "v0.15.0":',
                'if tag == "never":',
            ),
        }
        for target_field, (old, new) in mutations.items():
            with self.subTest(target_field=target_field):

                def read_document(repo, raw, field):
                    text = original_reader(repo, raw, field)
                    return text.replace(old, new) if field == target_field else text

                with mock.patch.object(
                    _CHECKER,
                    "_read_document",
                    side_effect=read_document,
                ):
                    with self.assertRaises(_CHECKER.ProposalError):
                        _CHECKER.validate_proposal(ROOT, MANIFEST)

    def test_release_workflows_reject_user_secret_authority(self):
        original_reader = _CHECKER._read_document
        for target_field in (
            "semantic-provider release-tag gate",
            "semantic-provider publication gate",
        ):
            with self.subTest(target_field=target_field):

                def read_document(repo, raw, field):
                    text = original_reader(repo, raw, field)
                    if field == target_field:
                        return text.replace(
                            "GH_TOKEN: ${{ github.token }}",
                            "GH_TOKEN: ${{ secrets.GIST_WRITE_TOKEN }}",
                            1,
                        )
                    return text

                with mock.patch.object(
                    _CHECKER,
                    "_read_document",
                    side_effect=read_document,
                ):
                    with self.assertRaisesRegex(
                        _CHECKER.ProposalError,
                        "must not receive user-secret authority",
                    ):
                        _CHECKER.validate_proposal(ROOT, MANIFEST)

    def test_release_workflow_requires_anonymous_fixed_host_gist_reads(self):
        original_reader = _CHECKER._read_document

        def read_document(repo, raw, field):
            text = original_reader(repo, raw, field)
            if field == "semantic-provider release-tag gate":
                return text.replace(
                    'public_gist_json(f"gists/',
                    'rest(f"gists/',
                    1,
                )
            return text

        with mock.patch.object(
            _CHECKER,
            "_read_document",
            side_effect=read_document,
        ):
            with self.assertRaisesRegex(
                _CHECKER.ProposalError,
                "release-tag workflow must enforce",
            ):
                _CHECKER.validate_proposal(ROOT, MANIFEST)

    def test_authoritative_snapshot_identity_matrix_fails_closed(self):
        def duplicate_review(value):
            duplicate = copy.deepcopy(value["pull_request"]["reviews"][0])
            value["pull_request"]["reviews"].append(duplicate)

        mutations = (
            lambda value: value.update({"repository": "attacker/fork"}),
            lambda value: value.update({"repository_id": 1}),
            lambda value: value["repository_owner"].update({"id": 1}),
            lambda value: value.update({"local_tree": "e" * 40}),
            lambda value: value.update({"canonical_main": "e" * 40}),
            lambda value: value.update({"pull_request": None}),
            lambda value: value["pull_request"].update({"merge_commit": "e" * 40}),
            lambda value: value["pull_request"].update(
                {"base_repository": "attacker/fork"}
            ),
            lambda value: value["pull_request"].update({"base_ref": "develop"}),
            lambda value: value["pull_request"].update(
                {"merged_at": "2026-08-30T12:01:00Z"}
            ),
            lambda value: value["pull_request"].update({"reviews": "not-a-list"}),
            lambda value: value["pull_request"]["reviews"].__setitem__(0, "bad"),
            duplicate_review,
            lambda value: value["pull_request"]["reviews"][0].update(
                {"state": "UNKNOWN"}
            ),
            lambda value: value["pull_request"]["reviews"][0].update(
                {"last_edited_at": "not-a-time"}
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                snapshot = self._review_snapshot()
                mutate(snapshot)
                with self.assertRaises(_AUDITOR.AuditError):
                    self._evaluate_review(snapshot)

        with self.assertRaisesRegex(_AUDITOR.AuditError, "timezone-aware"):
            _AUDITOR.evaluate_snapshot(
                self._review_snapshot(),
                self._review_requirements(),
                evaluated_at=datetime(2026, 8, 30, 12, 0),
            )

    def test_authoritative_auditor_pins_supported_github_contract(self):
        self.assertEqual(_AUDITOR.GITHUB_REST_API_VERSION, "2022-11-28")

    def test_owner_and_non_designated_reviews_do_not_qualify(self):
        snapshot = self._review_snapshot()
        body = self._configured_roster_body(
            security_id=22440724, security_login="yzm1"
        )
        roster_file = snapshot["review_authority"]["roster"]["file"]
        roster_file["content"] = body
        roster_file["size"] = len(body.encode("utf-8"))
        snapshot["review_authority"]["reviewers"]["security"]["reviewer"] = {
            "id": 22440724,
            "login": "yzm1",
            "type": "User",
        }
        reviews = snapshot["pull_request"]["reviews"]
        reviews[0]["reviewer"] = {
            "id": 22440724,
            "login": "yzm1",
            "type": "User",
        }
        with self.assertRaisesRegex(_AUDITOR.AuditError, "external non-author"):
            self._evaluate_review(snapshot)

        snapshot = self._review_snapshot()
        snapshot["pull_request"]["reviews"][1]["reviewer"] = {
            "id": 4,
            "login": "drive-by-reviewer",
            "type": "User",
        }
        with self.assertRaisesRegex(_AUDITOR.AuditError, "product review"):
            self._evaluate_review(snapshot)

    def test_stale_review_commit_does_not_qualify(self):
        snapshot = self._review_snapshot()
        snapshot["pull_request"]["reviews"][1]["commit_id"] = "d" * 40
        with self.assertRaisesRegex(_AUDITOR.AuditError, "product review"):
            self._evaluate_review(snapshot)

    def test_later_changes_requested_invalidates_reviewer(self):
        snapshot = self._review_snapshot()
        snapshot["pull_request"]["reviews"].append(
            {
                "id": 103,
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-08-30T10:03:00Z",
                "commit_id": snapshot["pull_request"]["head_sha"],
                "body": "",
                "reviewer": {
                    "id": 2,
                    "login": "security-reviewer",
                    "type": "User",
                },
            }
        )
        with self.assertRaisesRegex(_AUDITOR.AuditError, "security review"):
            self._evaluate_review(snapshot)

    def test_security_marker_is_strict_and_exact_commit_bound(self):
        for body in (
            "semantic-provider-security-review/v1\nVerdict: approved\n",
            (
                "semantic-provider-security-review/v1\n"
                f"Reviewed-commit: {'d' * 40}\n"
                "Verdict: approved\n"
            ),
            (
                "semantic-provider-security-review/v1\n"
                f"Reviewed-commit: {'b' * 40}\n"
                "Verdict: approved\nextra assurance\n"
            ),
            (
                " semantic-provider-security-review/v1\n"
                f"Reviewed-commit: {'b' * 40}\n"
                "Verdict: approved\n"
            ),
            (
                "semantic-provider-security-review/v1\n"
                f"Reviewed-commit: {'b' * 40} \n"
                "Verdict: approved\n"
            ),
        ):
            with self.subTest(body=body):
                snapshot = self._review_snapshot()
                snapshot["pull_request"]["reviews"][0]["body"] = body
                with self.assertRaisesRegex(_AUDITOR.AuditError, "security review"):
                    self._evaluate_review(snapshot)

    def test_reviewed_and_merged_trees_must_match(self):
        snapshot = self._review_snapshot()
        snapshot["pull_request"]["reviewed_tree"] = "d" * 40
        with self.assertRaisesRegex(_AUDITOR.AuditError, "trees differ"):
            self._evaluate_review(snapshot)

    def test_record_parent_must_equal_reviewed_pull_request_base(self):
        snapshot = self._review_snapshot()
        snapshot["pull_request"]["base_commit"] = "e" * 40
        with self.assertRaisesRegex(_AUDITOR.AuditError, "record parent differs"):
            self._evaluate_review(snapshot)

    def test_proposal_record_must_remain_on_canonical_main(self):
        snapshot = self._review_snapshot()
        snapshot["canonical_main"] = "d" * 40
        snapshot["main_comparison_status"] = "diverged"
        snapshot["main_merge_base"] = "e" * 40
        with self.assertRaisesRegex(_AUDITOR.AuditError, "ancestor of canonical main"):
            self._evaluate_review(snapshot)

    def test_pending_requests_and_unresolved_threads_fail_closed(self):
        mutations = (
            lambda value: value["pull_request"]["requested_reviewers"].append(
                {"id": 4, "login": "pending", "type": "User"}
            ),
            lambda value: value["pull_request"]["threads"].append(
                {"id": "thread-2", "is_resolved": False}
            ),
            lambda value: value["pull_request"].update(
                {"review_decision": "CHANGES_REQUESTED"}
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                snapshot = self._review_snapshot()
                mutate(snapshot)
                with self.assertRaises(_AUDITOR.AuditError):
                    self._evaluate_review(snapshot)

    def test_duplicate_or_rebound_reviewer_identity_fails_closed(self):
        snapshot = self._review_snapshot()
        conflicting = copy.deepcopy(snapshot["pull_request"]["reviews"][0])
        conflicting["id"] = 103
        conflicting["submitted_at"] = "2026-08-30T10:03:00Z"
        conflicting["reviewer"]["login"] = "renamed-security-reviewer"
        snapshot["pull_request"]["reviews"].append(conflicting)
        with self.assertRaisesRegex(_AUDITOR.AuditError, "identity changed"):
            self._evaluate_review(snapshot)

        snapshot = self._review_snapshot()
        snapshot["pull_request"]["reviews"][1]["reviewer"].update(
            {"id": 4, "login": "security-reviewer"}
        )
        with self.assertRaisesRegex(_AUDITOR.AuditError, "multiple GitHub IDs"):
            self._evaluate_review(snapshot)

    def test_fractional_review_timestamps_sort_chronologically(self):
        snapshot = self._review_snapshot()
        snapshot["pull_request"]["reviews"].extend(
            [
                {
                    "id": 103,
                    "state": "CHANGES_REQUESTED",
                    "submitted_at": "2026-08-30T10:02:00.1Z",
                    "commit_id": snapshot["pull_request"]["head_sha"],
                    "body": "",
                    "reviewer": {
                        "id": 3,
                        "login": "product-reviewer",
                        "type": "User",
                    },
                },
                {
                    "id": 104,
                    "state": "APPROVED",
                    "submitted_at": "2026-08-30T10:02:00.01Z",
                    "commit_id": snapshot["pull_request"]["head_sha"],
                    "body": "",
                    "reviewer": {
                        "id": 3,
                        "login": "product-reviewer",
                        "type": "User",
                    },
                },
            ]
        )
        with self.assertRaisesRegex(_AUDITOR.AuditError, "product review"):
            self._evaluate_review(snapshot)

    def test_bot_actor_is_well_formed_but_never_qualifies(self):
        actor = _AUDITOR._actor(
            {"id": 55, "login": "trusted-reviewer[bot]", "type": "Bot"},
            "bot",
        )
        self.assertEqual(actor["type"], "Bot")
        snapshot = self._review_snapshot()
        snapshot["pull_request"]["reviews"][1]["reviewer"] = actor
        with self.assertRaisesRegex(_AUDITOR.AuditError, "product review"):
            self._evaluate_review(snapshot)

    def test_expired_reviews_do_not_qualify(self):
        snapshot = self._review_snapshot()
        snapshot["pull_request"]["merged_at"] = "2026-05-02T10:10:00Z"
        for index, review in enumerate(snapshot["pull_request"]["reviews"], 1):
            review["submitted_at"] = f"2026-05-01T10:0{index}:00Z"
        with self.assertRaisesRegex(_AUDITOR.AuditError, "security review"):
            self._evaluate_review(snapshot)

    def test_post_merge_review_does_not_qualify(self):
        snapshot = self._review_snapshot()
        snapshot["pull_request"]["reviews"][0]["submitted_at"] = "2026-08-30T10:11:00Z"
        with self.assertRaisesRegex(_AUDITOR.AuditError, "security review"):
            self._evaluate_review(snapshot)

    def test_post_merge_review_body_edit_does_not_qualify(self):
        snapshot = self._review_snapshot()
        for timestamp in ("2026-08-30T10:10:00Z", "2026-08-30T10:11:00Z"):
            with self.subTest(timestamp=timestamp):
                candidate = copy.deepcopy(snapshot)
                candidate["pull_request"]["reviews"][0]["last_edited_at"] = timestamp
                with self.assertRaisesRegex(_AUDITOR.AuditError, "security review"):
                    self._evaluate_review(candidate)

    def test_review_submission_must_strictly_precede_merge(self):
        snapshot = self._review_snapshot()
        snapshot["pull_request"]["reviews"][0]["submitted_at"] = snapshot[
            "pull_request"
        ]["merged_at"]
        with self.assertRaisesRegex(_AUDITOR.AuditError, "security review"):
            self._evaluate_review(snapshot)

    def test_authoritative_manifest_path_cannot_be_overridden(self):
        with self.assertRaises(SystemExit):
            _AUDITOR._parser().parse_args(
                ["--manifest", "outside-the-reviewed-tree.json"]
            )

    def test_v015_release_audit_requires_external_tag_and_sha(self):
        for arguments in (
            ["--gate", "v0.15-release"],
            [
                "--gate",
                "v0.15-release",
                "--release-tag",
                "v0.15.1",
                "--release-sha",
                "e" * 40,
            ],
            ["--gate", "accepted", "--release-sha", "e" * 40],
        ):
            with self.subTest(arguments=arguments):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    result = _AUDITOR.main(arguments)
                self.assertEqual(result, 1)
                self.assertIn("audit failed", stderr.getvalue())

    def test_v015_release_main_double_audits_proposal_and_release_records(self):
        proposal = self._review_snapshot()
        release = self._release_snapshot()
        now = datetime.now(timezone.utc)
        merged_at = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        for snapshot in (proposal, release):
            snapshot["pull_request"]["merged_at"] = merged_at
            roster = snapshot["review_authority"]["roster"]
            roster["created_at"] = (
                (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
            )
            roster["updated_at"] = (
                (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
            )
            roster["latest_revision"]["committed_at"] = (
                (now - timedelta(minutes=6)).isoformat().replace("+00:00", "Z")
            )
            for index, review in enumerate(
                snapshot["pull_request"]["reviews"], start=2
            ):
                review["submitted_at"] = (
                    (now - timedelta(minutes=index)).isoformat().replace("+00:00", "Z")
                )
        checker_result = {
            "ok": True,
            "proposal": "boundver-semantic-provider-system/v1"
        }
        stdout = io.StringIO()
        with (
            mock.patch.object(
                _AUDITOR,
                "_local_release_record",
                return_value=("e" * 40, "f" * 40, "8" * 40),
            ),
            mock.patch.object(
                _AUDITOR,
                "_local_record",
                return_value=("a" * 40, "d" * 40, "c" * 40),
            ),
            mock.patch.object(_AUDITOR, "_materialize_validation_tree"),
            mock.patch.object(
                _AUDITOR,
                "_load_requirements",
                return_value=self._review_requirements(),
            ),
            mock.patch.object(
                _AUDITOR,
                "_load_release_requirements",
                return_value=self._release_requirements(),
            ),
            mock.patch.object(_AUDITOR, "GitHubClient", return_value=mock.Mock()),
            mock.patch.object(
                _AUDITOR,
                "collect_snapshot",
                side_effect=[proposal, proposal, release, release],
            ) as collect,
            mock.patch.object(
                _AUDITOR, "_run_checker", return_value=checker_result
            ) as run_checker,
            contextlib.redirect_stdout(stdout),
        ):
            result = _AUDITOR.main(
                [
                    "--repo",
                    str(ROOT),
                    "--gate",
                    "v0.15-release",
                    "--release-tag",
                    "v0.15.0",
                    "--release-sha",
                    "e" * 40,
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(result, 0, stdout.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["release_review"]["pull_request"], 95)
        self.assertEqual(collect.call_count, 4)
        run_checker.assert_called_once()
        options = run_checker.call_args.kwargs
        self.assertTrue(options["authoritative_review_passed"])
        self.assertTrue(options["authoritative_release_passed"])
        self.assertTrue(options["require_v0_15_release"])

    def test_graphql_review_edit_metadata_is_id_bound_and_bounded(self):
        client = _AUDITOR.GitHubClient(ROOT, gh_executable=sys.executable)
        response = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviews": {
                            "nodes": [
                                {"fullDatabaseId": "101", "lastEditedAt": None},
                                {
                                    "fullDatabaseId": "102",
                                    "lastEditedAt": "2026-08-30T10:00:00.1Z",
                                },
                            ],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": "unused",
                            },
                        }
                    }
                }
            }
        }
        with mock.patch.object(client, "api", return_value=response):
            self.assertEqual(
                client.review_edit_times("yzm1", "boundver", 80),
                {101: None, 102: "2026-08-30T10:00:00.1Z"},
            )

        reviews = [{"id": 101}]
        with self.assertRaisesRegex(_AUDITOR.AuditError, "review identities differ"):
            _AUDITOR._bind_review_edit_times(reviews, {102: None})

    def test_graphql_review_thread_pagination_is_complete_and_stable(self):
        client = _AUDITOR.GitHubClient(ROOT, gh_executable=sys.executable)
        responses = [
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewDecision": "APPROVED",
                            "reviewThreads": {
                                "nodes": [{"id": "thread-2", "isResolved": True}],
                                "pageInfo": {
                                    "hasNextPage": True,
                                    "endCursor": "next-page",
                                },
                            },
                        }
                    }
                }
            },
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewDecision": "APPROVED",
                            "reviewThreads": {
                                "nodes": [{"id": "thread-1", "isResolved": True}],
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                            },
                        }
                    }
                }
            },
        ]
        with mock.patch.object(client, "api", side_effect=responses):
            result = client.review_state("yzm1", "boundver", 80)
        self.assertEqual(result["review_decision"], "APPROVED")
        self.assertEqual(
            [thread["id"] for thread in result["threads"]],
            ["thread-1", "thread-2"],
        )

    def test_complete_github_snapshot_normalization_and_evaluation(self):
        record = "a" * 40
        parent = "d" * 40
        head = "b" * 40
        tree = "c" * 40
        client = mock.Mock(spec=_AUDITOR.GitHubClient)
        gist_id = "0caedb798d168b974f9d9fb63c377f73"
        gist_revision = "1" * 40
        gist_endpoint = f"gists/{gist_id}"
        gist_revision_endpoint = f"{gist_endpoint}/{gist_revision}"

        def gist_record(url):
            content = self._configured_roster_body()
            return {
                "id": gist_id,
                "node_id": (
                    "G_kwDOAVZrFNoAIDBjYWVkYjc5OGQxNjhiOTc0ZjlkOWZiNjNjMzc3Zjcz"
                ),
                "description": (
                    "boundver semantic-provider independent reviewer roster"
                ),
                "url": url,
                "html_url": f"https://gist.github.com/yzm1/{gist_id}",
                "owner": {
                    "id": 22440724,
                    "login": "yzm1",
                    "type": "User",
                },
                "public": True,
                "user": None,
                "truncated": False,
                "created_at": "2026-08-30T09:00:00Z",
                "updated_at": "2026-08-30T10:00:00Z",
                "history": [
                    {
                        "version": gist_revision,
                        "committed_at": "2026-08-30T09:59:59Z",
                        "user": {
                            "id": 22440724,
                            "login": "yzm1",
                            "type": "User",
                        },
                        "url": (
                            f"https://api.github.com/gists/{gist_id}/"
                            f"{gist_revision}"
                        ),
                        "change_status": {
                            "total": 7,
                            "additions": 7,
                            "deletions": 0,
                        },
                    }
                ],
                "files": {
                    "semantic-provider-review-roster.txt": {
                        "filename": "semantic-provider-review-roster.txt",
                        "type": "text/plain",
                        "language": "Text",
                        "raw_url": (
                            f"https://gist.githubusercontent.com/yzm1/{gist_id}/raw/"
                            + "2" * 40
                            + "/semantic-provider-review-roster.txt"
                        ),
                        "size": len(content.encode("utf-8")),
                        "truncated": False,
                        "content": content,
                        "encoding": "utf-8",
                    }
                },
            }

        def rest(endpoint, _label):
            values = {
                "repos/yzm1/boundver": {
                    "id": 1226008327,
                    "full_name": "yzm1/boundver",
                    "owner": {
                        "id": 22440724,
                        "login": "yzm1",
                        "type": "User",
                    },
                },
                "repos/yzm1/boundver/git/ref/heads/main": {
                    "object": {"type": "commit", "sha": record}
                },
                f"repos/yzm1/boundver/compare/{record}...{record}?per_page=1": {
                    "status": "identical",
                    "merge_base_commit": {"sha": record},
                },
                "repos/yzm1/boundver/pulls/80": {
                    "number": 80,
                    "user": {
                        "id": 22440724,
                        "login": "yzm1",
                        "type": "User",
                    },
                    "head": {"sha": head},
                    "base": {
                        "ref": "main",
                        "sha": parent,
                        "repo": {"full_name": "yzm1/boundver"},
                    },
                    "merge_commit_sha": record,
                    "state": "closed",
                    "merged_at": "2026-08-30T10:10:00Z",
                    "requested_reviewers": [],
                    "requested_teams": [],
                },
                gist_endpoint: gist_record(
                    f"https://api.github.com/gists/{gist_id}"
                ),
                gist_revision_endpoint: gist_record(
                    f"https://api.github.com/gists/{gist_id}/{gist_revision}"
                ),
                "repos/yzm1/boundver/collaborators/product-reviewer/permission": {
                    "permission": "read",
                    "role_name": "read",
                    "user": {
                        "id": 3,
                        "login": "product-reviewer",
                        "type": "User",
                        "permissions": {
                            "admin": False,
                            "maintain": False,
                            "push": False,
                            "triage": False,
                            "pull": True,
                        },
                    },
                },
                "repos/yzm1/boundver/collaborators/security-reviewer/permission": {
                    "permission": "read",
                    "role_name": "read",
                    "user": {
                        "id": 2,
                        "login": "security-reviewer",
                        "type": "User",
                        "permissions": {
                            "admin": False,
                            "maintain": False,
                            "push": False,
                            "triage": False,
                            "pull": True,
                        },
                    },
                },
                f"repos/yzm1/boundver/git/commits/{record}": {
                    "sha": record,
                    "tree": {"sha": tree},
                },
                f"repos/yzm1/boundver/git/commits/{head}": {
                    "sha": head,
                    "tree": {"sha": tree},
                },
            }
            return copy.deepcopy(values[endpoint])

        marker = self._review_requirements()["security_review_marker"]
        reviews = [
            {
                "id": 101,
                "state": "APPROVED",
                "submitted_at": "2026-08-30T10:01:00Z",
                "commit_id": head,
                "body": (
                    f"{marker}\n"
                    f"Reviewed-commit: {head}\n"
                    "Independent-reviewer: confirmed\n"
                    "Verdict: approved\n"
                ),
                "user": {
                    "id": 2,
                    "login": "security-reviewer",
                    "type": "User",
                },
            },
            {
                "id": 102,
                "state": "APPROVED",
                "submitted_at": "2026-08-30T10:02:00Z",
                "commit_id": head,
                "body": (
                    f"{self._review_requirements()['product_review_marker']}\n"
                    f"Reviewed-commit: {head}\n"
                    "Independent-reviewer: confirmed\n"
                    "Verdict: approved\n"
                ),
                "user": {
                    "id": 3,
                    "login": "product-reviewer",
                    "type": "User",
                },
            },
        ]

        def rest_pages(endpoint, _label):
            if endpoint == "repos/yzm1/boundver/collaborators":
                return [
                    {
                        "id": 22440724,
                        "login": "yzm1",
                        "type": "User",
                        "role_name": "admin",
                        "permissions": {
                            "admin": True,
                            "maintain": True,
                            "push": True,
                            "triage": True,
                            "pull": True,
                        },
                    }
                ]
            if endpoint == f"repos/yzm1/boundver/commits/{record}/pulls":
                return [{"number": 80}]
            if endpoint == "repos/yzm1/boundver/pulls/80/reviews":
                return copy.deepcopy(reviews)
            self.fail(f"unexpected paginated endpoint: {endpoint}")

        client.rest.side_effect = rest
        client.rest_pages.side_effect = rest_pages
        client.review_edit_times.return_value = {101: None, 102: None}
        client.review_state.return_value = {
            "review_decision": "APPROVED",
            "threads": [{"id": "thread-1", "is_resolved": True}],
        }
        snapshot = _AUDITOR.collect_snapshot(
            client,
            self._review_requirements(),
            record,
            parent,
            tree,
        )
        result = self._evaluate_review(snapshot)
        self.assertEqual(result["pull_request"], 80)
        self.assertEqual(result["reviewers"], ["product-reviewer", "security-reviewer"])

    def test_review_requirements_are_fixed_and_fail_closed(self):
        requirements = _AUDITOR._load_requirements(MANIFEST)
        self.assertEqual(requirements["repository"], "yzm1/boundver")
        self.assertEqual(requirements["repository_id"], 1226008327)
        self.assertEqual(requirements["repository_owner_id"], 22440724)
        value = self._manifest()
        value["review_requirements"]["minimum_non_author_reviews"] = 1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proposal.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(_AUDITOR.AuditError, "fewer than two"):
                _AUDITOR._load_requirements(path)

    def test_red_team_finding_catalog_must_match_documented_headings(self):
        def mutate(value):
            value["red_team"]["rounds"][1]["findings"][-1] = "RTF-999"

        with self.assertRaisesRegex(
            _CHECKER.ProposalError, "finding documentation mismatch"
        ):
            self._validate_mutation(mutate)

    def test_bounded_subprocess_rejects_output_overflow(self):
        with self.assertRaisesRegex(_AUDITOR.AuditError, "stdout exceeds"):
            _AUDITOR._run_bounded(
                [sys.executable, "-I", "-c", "import sys;sys.stdout.write('x'*1025)"],
                cwd=ROOT,
                stdout_limit=1024,
                timeout=10,
            )

        with self.assertRaisesRegex(_AUDITOR.AuditError, "exited with status 7"):
            _AUDITOR._run_bounded(
                [sys.executable, "-I", "-c", "raise SystemExit(7)"],
                cwd=ROOT,
                timeout=10,
            )

        with self.assertRaisesRegex(_AUDITOR.AuditError, "timeout"):
            _AUDITOR._run_bounded(
                [sys.executable, "-I", "-c", "import time;time.sleep(2)"],
                cwd=ROOT,
                timeout=1,
            )

    def test_bounded_subprocess_pipe_close_uses_one_shared_deadline(self):
        readers = []

        class NeverClosingReader:
            def __init__(self, *args, **kwargs):
                self.join_timeouts = []
                readers.append(self)

            def start(self):
                return None

            def join(self, timeout=None):
                self.join_timeouts.append(timeout)

            def is_alive(self):
                return True

        class ExitedProcess:
            stdout = object()
            stderr = object()

            def __init__(self):
                self.kill_calls = 0

            def wait(self, timeout=None):
                return 0

            def kill(self):
                self.kill_calls += 1

        process = ExitedProcess()
        with mock.patch.object(
            _AUDITOR.subprocess,
            "Popen",
            return_value=process,
        ), mock.patch.object(
            _AUDITOR.threading,
            "Thread",
            NeverClosingReader,
        ), mock.patch.object(
            _AUDITOR.time,
            "monotonic",
            side_effect=(100.0, 103.0, 106.0),
        ), self.assertRaisesRegex(
            _AUDITOR.AuditError,
            "output pipes did not close",
        ):
            _AUDITOR._run_bounded(["checker"], cwd=ROOT)

        self.assertEqual(
            [reader.join_timeouts for reader in readers],
            [[2.0], [0.0]],
        )
        self.assertEqual(process.kill_calls, 1)

    def test_gate_json_shape_is_rejected_before_decoder_allocation(self):
        raw = b'{"items":[0,0,0]}'
        with mock.patch.object(
            _AUDITOR, "MAX_JSON_TOKENS", 2
        ), mock.patch.object(
            _AUDITOR.json,
            "loads",
            side_effect=AssertionError("decoder must not run"),
        ), self.assertRaisesRegex(_AUDITOR.AuditError, "structural limit"):
            _AUDITOR._decode_json(raw, "hostile evidence")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hostile.json"
            path.write_bytes(raw)
            with mock.patch.object(
                _CHECKER, "MAX_JSON_TOKENS", 2
            ), mock.patch.object(
                _CHECKER.json,
                "loads",
                side_effect=AssertionError("decoder must not run"),
            ), self.assertRaisesRegex(_CHECKER.ProposalError, "structural limit"):
                _CHECKER._load_json(path)

    def test_audit_git_reads_disable_replacements_hooks_and_lazy_fetch(self):
        with mock.patch.object(
            _AUDITOR, "_trusted_tool", return_value="trusted-git"
        ), mock.patch.object(
            _AUDITOR, "_run_bounded", return_value=b""
        ) as run:
            _AUDITOR._git(ROOT, "rev-parse", "HEAD")
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["GIT_CONFIG_KEY_0"], "core.hooksPath")
        self.assertEqual(environment["GIT_CONFIG_VALUE_0"], os.devnull)

    def test_gate_json_parsers_reject_unbounded_or_ambiguous_numbers(self):
        hostile_values = (
            b'{"value":1.5}',
            b'{"value":99999999999999999999999999999999999999999999999999}',
        )
        for raw in hostile_values:
            with self.subTest(raw=raw):
                with self.assertRaises(_AUDITOR.AuditError):
                    _AUDITOR._decode_json(raw, "hostile evidence")
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "hostile.json"
                    path.write_bytes(raw)
                    with self.assertRaises(_CHECKER.ProposalError):
                        _CHECKER._load_json(path)

    def test_github_client_enforces_global_request_and_time_budgets(self):
        client = _AUDITOR.GitHubClient(ROOT, gh_executable=sys.executable)
        client.request_count = _AUDITOR.MAX_API_REQUESTS
        with self.assertRaisesRegex(_AUDITOR.AuditError, "request-count budget"):
            client.api(["rate_limit"], "rate limit")

        client = _AUDITOR.GitHubClient(ROOT, gh_executable=sys.executable)
        client.deadline = time.monotonic() - 1
        with self.assertRaisesRegex(_AUDITOR.AuditError, "total time budget"):
            client.api(["rate_limit"], "rate limit")

    def test_github_client_enforces_aggregate_response_budget(self):
        client = _AUDITOR.GitHubClient(ROOT, gh_executable=sys.executable)
        client.response_bytes = _AUDITOR.MAX_API_TOTAL_BYTES
        with mock.patch.object(_AUDITOR, "_run_bounded", return_value=b"{}"):
            with self.assertRaisesRegex(
                _AUDITOR.AuditError, "aggregate response budget"
            ):
                client.api(["rate_limit"], "rate limit")

    def test_repository_local_tool_shadowing_and_host_redirect_are_rejected(self):
        with mock.patch.object(
            _AUDITOR.shutil,
            "which",
            return_value=str(AUDITOR_PATH),
        ):
            with self.assertRaisesRegex(_AUDITOR.AuditError, "inside the repository"):
                _AUDITOR._trusted_tool("gh", ROOT)

        client = _AUDITOR.GitHubClient(ROOT, gh_executable="trusted-gh")
        with mock.patch.object(_AUDITOR, "_run_bounded", return_value=b"{}") as run:
            client.api(["rate_limit"], "rate limit")
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["trusted-gh", "api", "--hostname", "github.com"])

    def test_validation_tree_uses_reviewed_blobs_not_later_worktree_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._initialize_gate_repository(directory)
            for relative in _AUDITOR.VALIDATION_PATHS:
                path = root.joinpath(*relative.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"reviewed:{relative}\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "--", *_AUDITOR.VALIDATION_PATHS],
                cwd=root,
                check=True,
                timeout=30,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "reviewed proposal tree"],
                cwd=root,
                check=True,
                timeout=30,
            )
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
                timeout=30,
            ).stdout.strip()
            manifest = root / _AUDITOR.CANONICAL_MANIFEST
            manifest.write_text("unreviewed replacement\n", encoding="utf-8")
            with tempfile.TemporaryDirectory() as snapshot_directory:
                snapshot = Path(snapshot_directory)
                _AUDITOR._materialize_validation_tree(root, commit, snapshot)
                self.assertEqual(
                    (snapshot / _AUDITOR.CANONICAL_MANIFEST).read_text(
                        encoding="utf-8"
                    ),
                    "reviewed:spec/semantic-provider-proposal.json\n",
                )

    def test_reviewed_validation_paths_must_be_regular_git_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._initialize_gate_repository(directory)
            payload = root / "link-payload"
            payload.write_text("outside-target\n", encoding="utf-8")
            blob = subprocess.run(
                ["git", "hash-object", "-w", "link-payload"],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
                timeout=30,
            ).stdout.strip()
            relative = _AUDITOR.CANONICAL_MANIFEST.as_posix()
            subprocess.run(
                [
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "120000",
                    blob,
                    relative,
                ],
                cwd=root,
                check=True,
                timeout=30,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "symlink proposal"],
                cwd=root,
                check=True,
                timeout=30,
            )
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
                timeout=30,
            ).stdout.strip()
            with self.assertRaisesRegex(_AUDITOR.AuditError, "not a regular Git file"):
                _AUDITOR._git_blob(root, commit, relative)

    def test_acceptance_record_must_use_parent_bootstrap_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._initialize_gate_repository(directory)
            manifest = root / "spec" / "semantic-provider-proposal.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("{}\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "--", "spec/semantic-provider-proposal.json"],
                cwd=root,
                check=True,
                timeout=30,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "accept proposal"],
                cwd=root,
                check=True,
                timeout=30,
            )
            record, parent, tree = _AUDITOR._local_record(root)
            self.assertRegex(record, r"^[0-9a-f]{40}$")
            self.assertRegex(parent, r"^[0-9a-f]{40}$")
            self.assertRegex(tree, r"^[0-9a-f]{40}$")

    def test_acceptance_record_follows_canonical_first_parent_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._initialize_gate_repository(directory)
            base_branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
                timeout=30,
            ).stdout.strip()
            subprocess.run(
                ["git", "switch", "--quiet", "-c", "proposal"],
                cwd=root,
                check=True,
                timeout=30,
            )
            manifest = root / _AUDITOR.CANONICAL_MANIFEST
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("{}\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "--", _AUDITOR.CANONICAL_MANIFEST.as_posix()],
                cwd=root,
                check=True,
                timeout=30,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "proposal declaration"],
                cwd=root,
                check=True,
                timeout=30,
            )
            subprocess.run(
                ["git", "switch", "--quiet", base_branch],
                cwd=root,
                check=True,
                timeout=30,
            )
            subprocess.run(
                [
                    "git",
                    "merge",
                    "--quiet",
                    "--no-ff",
                    "-m",
                    "merge proposal",
                    "proposal",
                ],
                cwd=root,
                check=True,
                timeout=30,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
                timeout=30,
            ).stdout.strip()
            record, _, _ = _AUDITOR._local_record(root)
            self.assertEqual(record, head)

    def test_acceptance_record_cannot_modify_its_own_auditor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._initialize_gate_repository(directory)
            manifest = root / "spec" / "semantic-provider-proposal.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("{}\n", encoding="utf-8")
            auditor = root / "scripts" / "audit_semantic_provider_proposal.py"
            auditor.write_text("candidate-controlled gate\n", encoding="utf-8")
            subprocess.run(
                [
                    "git",
                    "add",
                    "--",
                    "spec/semantic-provider-proposal.json",
                    "scripts/audit_semantic_provider_proposal.py",
                ],
                cwd=root,
                check=True,
                timeout=30,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "unsafe acceptance"],
                cwd=root,
                check=True,
                timeout=30,
            )
            with self.assertRaisesRegex(
                _AUDITOR.AuditError, "changes bootstrap gate code"
            ):
                _AUDITOR._local_record(root)

    def test_local_release_record_requires_clean_exact_head(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._initialize_gate_repository(directory)
            tracked = root / "release.txt"
            tracked.write_text("release candidate\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "--", "release.txt"],
                cwd=root,
                check=True,
                timeout=30,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "release candidate"],
                cwd=root,
                check=True,
                timeout=30,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
                timeout=30,
            ).stdout.strip()
            record, parent, tree = _AUDITOR._local_release_record(root, head)
            self.assertEqual(record, head)
            self.assertRegex(parent, r"^[0-9a-f]{40}$")
            self.assertRegex(tree, r"^[0-9a-f]{40}$")

            with self.assertRaisesRegex(_AUDITOR.AuditError, "does not equal"):
                _AUDITOR._local_release_record(root, parent)
            tracked.write_text("dirty candidate\n", encoding="utf-8")
            with self.assertRaisesRegex(_AUDITOR.AuditError, "uncommitted"):
                _AUDITOR._local_release_record(root, head)


if __name__ == "__main__":
    unittest.main()
