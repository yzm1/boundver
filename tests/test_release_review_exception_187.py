"""Owner-authorized PR187-only review-account exception; no standing waiver."""
from __future__ import annotations

import base64
import os
import unittest

from tests import test_release_review_exception_184 as prior

BASE = "577a3b18ed234d36327ca92d844a0b2a97810a50"
PIN = "1b4b51398db0b3c0e70f6b98ab43063ddeb274e4"


def body():
    return prior.body().replace("Pull-request: 184", "Pull-request: 187").replace(
        prior.REVIEWED_BASE, BASE).replace("PR184-only", "PR187-only")


def files():
    return "\n".join((
        f"tests/test_distribution_contract.py|modified|{PIN}|",
        f"scripts/audit_release_reviews.sh|modified|{'a' * 40}|",
        f"tests/test_release_review_exception_187.py|added|{'b' * 40}|",
    ))


class ReceiptExceptionHelperTests(unittest.TestCase):
    def helper(self, command, *args, **overrides):
        fixture = prior.fixtures.LocalAgentExceptionHelperTests()
        fixture.helper_marker = "one-time PR 184 AI-maintainer identity exception helpers"
        fixture.helper_pr_number = 187
        return fixture._helper(command, *args, **overrides)

    def test_canonical_record_and_exact_three_file_scope(self):
        record = body()
        result = self.helper('ai_maintainer_exception_record_matches "$1" "$2"',
                             record.rstrip("\n"), base64.b64encode(record.encode()).decode())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.helper('ai_maintainer_exception_scope_matches "$1"', files()).returncode, 0)
        self.assertEqual(self.helper("ai_maintainer_exception_eligible").returncode, 0)

    def test_wrong_release_identity_and_unapproved_pr_refused(self):
        for key, value in {"pr_number": "188", "release_tag": "v0.17.1",
                           "author_id": "99", "repository_owner_id": "99",
                           "github_repository_lower": "other/boundver"}.items():
            with self.subTest(field=key):
                self.assertNotEqual(self.helper("ai_maintainer_exception_eligible", **{key: value}).returncode, 0)

    def test_old_record_stale_base_and_noncanonical_bytes_refused(self):
        for record in (prior.body(), body().replace(BASE, "2" * 40), body() + "\n",
                       body().replace(prior.HEAD, "2" * 40), body().replace("PR187-only", "all-PRs")):
            result = self.helper('ai_maintainer_exception_record_matches "$1" "$2"',
                                 record.rstrip("\n"), base64.b64encode(record.encode()).decode())
            self.assertNotEqual(result.returncode, 0)

    def test_scope_rejects_modified_payload_missing_extra_duplicate_and_status(self):
        rows = files().splitlines()
        for changed in (files().replace(PIN, "c" * 40), "\n".join(rows[:-1]),
                        files() + "\n" + rows[0], files().replace("|added|", "|modified|"),
                        files().replace("|modified|", "|renamed|", 1),
                        files() + f"\nsrc/boundver/core.py|modified|{'a' * 40}|"):
            self.assertNotEqual(self.helper('ai_maintainer_exception_scope_matches "$1"', changed).returncode, 0)


@unittest.skipIf(os.name == "nt", "full release audit runs on Linux")
class ReceiptExceptionAuditTests(unittest.TestCase):
    def audit(self, **overrides):
        return prior.fixtures.LocalAgentExceptionAuditTests()._audit(
            number=187, record_body=body(), file_records=files(), file_count=3, **overrides)

    def test_full_audit_accepts_exact_owner_record_only(self):
        result = self.audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PR #187:", result.stdout)
        self.assertIn("authenticated non-author GitHub approval NOT obtained", result.stdout)

    def test_missing_record_scope_api_threads_and_pending_review_fail_closed(self):
        for overrides in ({"records": ""}, {"FAKE_EXCEPTION_FILE_COUNT": "24"},
                          {"FAKE_EXCEPTION_REPO_ID": "99"}, {"FAKE_UNRESOLVED": "1"},
                          {"FAKE_PENDING_REVIEWERS": "1"}, {"FAKE_DECISION": "CHANGES_REQUESTED"},
                          {"FAKE_FAILURE": "exception-files"}, {"FAKE_FAILURE": "exception-repo"},
                          {"FAKE_EXCEPTION_FILES": files().replace(PIN, "c" * 40)}):
            with self.subTest(overrides=overrides):
                self.assertNotEqual(self.audit(**overrides).returncode, 0)
