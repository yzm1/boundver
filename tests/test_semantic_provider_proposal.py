"""Executable assurance checks for the semantic-provider design gate."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
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
    def _mark_accepted(value, *, release=False):
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
        if release:
            gate = value["release_gates"]["v0.15.0"]
            gate["release_allowed"] = True
            gate["exact_candidate_commit"] = "f" * 40
            for field in (
                "full_source_bug_scan",
                "full_issue_audit",
                "full_security_scan",
                "all_blockers_closed",
                "supported_platforms_passed",
                "publication_gates_passed",
            ):
                gate[field] = True
            gate["evidence"] = ["exact v0.15.0 release evidence"]

    def _review_requirements(self):
        return self._manifest()["review_requirements"]

    def _review_snapshot(self):
        record_commit = "a" * 40
        record_parent = "d" * 40
        reviewed_commit = "b" * 40
        tree = "c" * 40
        marker = self._review_requirements()["security_review_marker"]
        return {
            "repository": "yzm1/boundver",
            "repository_owner": {"id": 1, "login": "yzm1", "type": "User"},
            "record_commit": record_commit,
            "record_parent": record_parent,
            "local_tree": tree,
            "record_tree": tree,
            "canonical_main": record_commit,
            "main_comparison_status": "identical",
            "main_merge_base": record_commit,
            "pull_request": {
                "number": 80,
                "author": {"id": 1, "login": "yzm1", "type": "User"},
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
                        "body": "",
                        "reviewer": {
                            "id": 3,
                            "login": "product-reviewer",
                            "type": "User",
                        },
                    },
                ],
                "permissions": {
                    "security-reviewer": "write",
                    "product-reviewer": "maintain",
                },
            },
        }

    def _evaluate_review(self, snapshot):
        return _AUDITOR.evaluate_snapshot(
            snapshot,
            self._review_requirements(),
            evaluated_at=self.AUDIT_TIME,
        )

    def _initialize_gate_repository(self, directory):
        root = Path(directory)
        subprocess.run(
            ["git", "init", "--quiet"], cwd=root, check=True, timeout=30
        )
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
        self.assertEqual(result["threats"], 42)
        self.assertEqual(result["controls"], 42)
        self.assertEqual(result["verifications"], 37)
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
            lambda value: self._mark_accepted(value, release=True),
            document_status="accepted",
            authoritative_review_passed=True,
            require_v0_15_release=True,
        )
        self.assertTrue(result["v0_15_release_allowed"])

        def incomplete_release(value):
            self._mark_accepted(value, release=True)
            value["release_gates"]["v0.15.0"]["full_security_scan"] = False

        with self.assertRaisesRegex(_CHECKER.ProposalError, "every exact gate"):
            self._validate_mutation(
                incomplete_release,
                document_status="accepted",
                authoritative_review_passed=True,
            )

    def test_structural_gate_rejects_malformed_assurance_records(self):
        def orphan_verification(value):
            threat = next(item for item in value["threats"] if item["id"] == "SPT-042")
            threat["verifications"].remove("SPV-037")
            control = next(item for item in value["controls"] if item["id"] == "SPC-042")
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
                lambda value: value["red_team"]["rounds"][0].update(
                    {"extra": True}
                ),
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
                "review base",
                lambda value: value["review_requirements"].update(
                    {"base_branch": "develop"}
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
                lambda value: value["release_gates"]["v0.15.0"].update(
                    {"extra": True}
                ),
            ),
            (
                "release candidate",
                lambda value: value["release_gates"]["v0.15.0"].update(
                    {"exact_candidate_commit": "short"}
                ),
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
        self.assertIn("Open red-team findings remain unresolved", result["acceptance_blockers"])

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
        self.assertNotIn("Open red-team findings remain unresolved", result["acceptance_blockers"])

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

    def test_release_cannot_be_enabled_by_one_boolean(self):
        def mutate(value):
            value["release_gates"]["v0.15.0"]["release_allowed"] = True

        with self.assertRaisesRegex(
            _CHECKER.ProposalError,
            "evidence must contain evidence|release cannot be allowed",
        ):
            self._validate_mutation(mutate)

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
            control = next(item for item in value["controls"] if item["id"] == "SPC-028")
            control["threats"].remove("SPT-008")

        with self.assertRaisesRegex(
            _CHECKER.ProposalError,
            "at least two defense-in-depth controls",
        ):
            self._validate_mutation(mutate)

    def test_passed_verification_requires_evidence(self):
        def mutate(value):
            verification = next(
                item
                for item in value["verifications"]
                if item["status"] == "planned"
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
        result = self._evaluate_review(self._review_snapshot())
        self.assertEqual(result["pull_request"], 80)
        self.assertEqual(
            result["reviewers"], ["product-reviewer", "security-reviewer"]
        )
        self.assertEqual(result["security_reviewers"], ["security-reviewer"])

    def test_authoritative_snapshot_identity_matrix_fails_closed(self):
        def duplicate_review(value):
            duplicate = copy.deepcopy(value["pull_request"]["reviews"][0])
            value["pull_request"]["reviews"].append(duplicate)

        mutations = (
            lambda value: value.update({"repository": "attacker/fork"}),
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

    def test_author_and_read_only_reviews_do_not_qualify(self):
        snapshot = self._review_snapshot()
        reviews = snapshot["pull_request"]["reviews"]
        reviews[0]["reviewer"] = {"id": 1, "login": "yzm1", "type": "User"}
        snapshot["pull_request"]["permissions"]["product-reviewer"] = "read"
        with self.assertRaisesRegex(
            _AUDITOR.AuditError, "0 qualifying exact-head non-author reviews"
        ):
            self._evaluate_review(snapshot)

    def test_stale_review_commit_does_not_qualify(self):
        snapshot = self._review_snapshot()
        snapshot["pull_request"]["reviews"][1]["commit_id"] = "d" * 40
        with self.assertRaisesRegex(_AUDITOR.AuditError, "1 qualifying"):
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
        with self.assertRaisesRegex(_AUDITOR.AuditError, "1 qualifying"):
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
        with self.assertRaisesRegex(_AUDITOR.AuditError, "1 qualifying"):
            self._evaluate_review(snapshot)

    def test_bot_actor_is_well_formed_but_never_qualifies(self):
        actor = _AUDITOR._actor(
            {"id": 55, "login": "trusted-reviewer[bot]", "type": "Bot"},
            "bot",
        )
        self.assertEqual(actor["type"], "Bot")
        snapshot = self._review_snapshot()
        snapshot["pull_request"]["reviews"][1]["reviewer"] = actor
        with self.assertRaisesRegex(_AUDITOR.AuditError, "1 qualifying"):
            self._evaluate_review(snapshot)

    def test_expired_reviews_do_not_qualify(self):
        snapshot = self._review_snapshot()
        snapshot["pull_request"]["merged_at"] = "2026-05-02T10:10:00Z"
        for index, review in enumerate(snapshot["pull_request"]["reviews"], 1):
            review["submitted_at"] = f"2026-05-01T10:0{index}:00Z"
        with self.assertRaisesRegex(_AUDITOR.AuditError, "0 qualifying"):
            self._evaluate_review(snapshot)

    def test_post_merge_review_does_not_qualify(self):
        snapshot = self._review_snapshot()
        snapshot["pull_request"]["reviews"][0][
            "submitted_at"
        ] = "2026-08-30T10:11:00Z"
        with self.assertRaisesRegex(_AUDITOR.AuditError, "1 qualifying"):
            self._evaluate_review(snapshot)

    def test_post_merge_review_body_edit_does_not_qualify(self):
        snapshot = self._review_snapshot()
        for timestamp in ("2026-08-30T10:10:00Z", "2026-08-30T10:11:00Z"):
            with self.subTest(timestamp=timestamp):
                candidate = copy.deepcopy(snapshot)
                candidate["pull_request"]["reviews"][0][
                    "last_edited_at"
                ] = timestamp
                with self.assertRaisesRegex(_AUDITOR.AuditError, "1 qualifying"):
                    self._evaluate_review(candidate)

    def test_review_submission_must_strictly_precede_merge(self):
        snapshot = self._review_snapshot()
        snapshot["pull_request"]["reviews"][0][
            "submitted_at"
        ] = snapshot["pull_request"]["merged_at"]
        with self.assertRaisesRegex(_AUDITOR.AuditError, "1 qualifying"):
            self._evaluate_review(snapshot)

    def test_authoritative_manifest_path_cannot_be_overridden(self):
        with self.assertRaises(SystemExit):
            _AUDITOR._parser().parse_args(
                ["--manifest", "outside-the-reviewed-tree.json"]
            )

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

        def rest(endpoint, _label):
            values = {
                "repos/yzm1/boundver": {
                    "full_name": "yzm1/boundver",
                    "owner": {"id": 1, "login": "yzm1", "type": "User"},
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
                    "user": {"id": 1, "login": "yzm1", "type": "User"},
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
                "repos/yzm1/boundver/collaborators/security-reviewer/permission": {
                    "permission": "write"
                },
                "repos/yzm1/boundver/collaborators/product-reviewer/permission": {
                    "permission": "maintain"
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
                "body": f"{marker}\nReviewed-commit: {head}\nVerdict: approved\n",
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
                "body": "",
                "user": {
                    "id": 3,
                    "login": "product-reviewer",
                    "type": "User",
                },
            },
        ]

        def rest_pages(endpoint, _label):
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
            "yzm1/boundver",
            "main",
            record,
            parent,
            tree,
        )
        result = self._evaluate_review(snapshot)
        self.assertEqual(result["pull_request"], 80)
        self.assertEqual(
            result["reviewers"], ["product-reviewer", "security-reviewer"]
        )

    def test_review_requirements_are_fixed_and_fail_closed(self):
        requirements = _AUDITOR._load_requirements(MANIFEST)
        self.assertEqual(requirements["repository"], "yzm1/boundver")
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


if __name__ == "__main__":
    unittest.main()
