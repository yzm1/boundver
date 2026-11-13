"""The AI-author/account mismatch exception is restricted to PR184 and v0.17."""

from __future__ import annotations

import base64
import hashlib
import os
import unittest

from tests import test_release_review_exception_182 as fixtures


HEAD = fixtures.HEAD
AGENT = fixtures.AGENT
REVIEWED_BASE = "db19d3614ec7d7f0ee6181cabac2549d02086cc5"
PAYLOAD_BLOBS = {
    ".dockerignore": ("modified", "11a33e7f93e487b8d2dc972442796359154a61c8"),
    ".trivyignore.yaml": ("modified", "c5a93ffa50fa60139d67d84724b0896a4057f673"),
    "CHANGELOG.md": ("modified", "d0cc644afb0ce17ce53529d8b8ace7171117ec8d"),
    "Dockerfile": ("modified", "2de66284ee4f947af39e74038c0cd5f7985e859a"),
    "SECURITY.md": ("modified", "d6118e98399ef797119dae0385e1553c4a925598"),
    "docs/distribution.md": ("modified", "d0b0313bc4d67581e9914b82f579423dd722aa51"),
    ".github/workflows/ci.yml": ("modified", "0b872ae3b9cf89d807e41c203fa1bd9f9e1fd2d5"),
    ".github/workflows/publish-container.yml": ("modified", "a20080ce04a1d064836c26aa70b0d9794df2a6d2"),
    "scripts/prepare_git_vex.py": ("added", "c00219c12e9480be1067f53a39a9ab6fe3f4ec4a"),
    "tests/test_git_vex.py": ("added", "9d1b28db144036a36f92ea5f347da16b8df81259"),
    "scripts/minimal_git/README.md": ("added", "49a0f41cc9f5aa990bba62ee34f2270b189f2035"),
    "scripts/minimal_git/audit.py": ("added", "3773eff3cd434017d206bc9d9f477509160dd309"),
    "scripts/minimal_git/build.sh": ("added", "182f66f986cab4d862bedc9ade6524c74686b54e"),
    "scripts/minimal_git/snapshot.sh": ("added", "9fe9926e3b9e817ddc7025fd52b3e190095bcfd4"),
    "scripts/minimal_git/git-release-key.gpg": ("added", "728ec4ea1a64c95e66d3ae358da3d6512c59793e"),
    "scripts/verify_release_readiness.py": ("modified", "e556a864cb2390fca1c9ac930f54ef44fcd04627"),
    "tests/test_distribution_contract.py": ("modified", "1e7c42d982f174a2183c856fd44ea3c1105975f6"),
    "tests/test_minimal_git.py": ("added", "968d3140d086a578937526cb6eeeb558f51b8776"),
    "tests/test_release_readiness.py": ("modified", "092accdf5f247620f1d5050a859d8b9d6a5743a6"),
}
SUPPORT_FILES = (
    "scripts/audit_release_reviews.sh", "docs/RELEASING.md",
    "tests/test_github_api_read.py", "tests/test_release_review_exception_182.py",
)
NEW_TEST = "tests/test_release_review_exception_184.py"


def report(head=HEAD, agent=AGENT, verdict="approve-conditional-scope") -> bytes:
    return (
        f"Reviewed-sha: {head}\nReviewer-agent-id: {agent}\nVerdict: {verdict}\n\n"
        "Conditional source/enforcement review only. Existing Git CVE findings "
        "remain unaccepted; all security and publication gates remain required.\n"
    ).encode()


def body(head=HEAD, payload=None) -> str:
    payload = report(head) if payload is None else payload
    return "\n".join((
        "boundver-ai-maintainer-review-exception/v1",
        "Repository: yzm1/boundver", "Repository-id: 1226008327",
        "Release: v0.17.0", "Pull-request: 184", f"Head-sha: {head}",
        f"Maintainer-reviewed-base: {REVIEWED_BASE}",
        "Review-kind: human maintainer review of AI-written base plus owner-attested exact-head local-agent assessment; authenticated non-author GitHub approval not obtained",
        f"Reviewer-agent-id: {AGENT}", "Verdict: approve-conditional-scope",
        f"Report-sha256: {hashlib.sha256(payload).hexdigest()}",
        f"Report-base64: {base64.b64encode(payload).decode('ascii')}",
        "Owner-authorization: explicit PR184-only account-identity exception; no finding, CI, scan, expiry, CVE-risk or publication waiver",
        "",
    ))


def files() -> str:
    rows = [f"{path}|{status}|{blob}|" for path, (status, blob) in PAYLOAD_BLOBS.items()]
    rows.extend(f"{path}|modified|{'a' * 40}|" for path in SUPPORT_FILES)
    rows.append(f"{NEW_TEST}|added|{'b' * 40}|")
    return "\n".join(rows)


class AiMaintainerExceptionHelperTests(unittest.TestCase):
    def _helper(self, command, *arguments, **overrides):
        fixture = fixtures.LocalAgentExceptionHelperTests()
        fixture.helper_marker = "one-time PR 184 AI-maintainer identity exception helpers"
        fixture.helper_pr_number = 184
        return fixture._helper(command, *arguments, **overrides)

    def _matches(self, record):
        return self._helper(
            'ai_maintainer_exception_record_matches "$1" "$2"',
            record.replace("\0", "").rstrip("\n"), base64.b64encode(record.encode()).decode(),
        )

    def test_only_exact_owner_repository_release_and_pr_are_eligible(self):
        self.assertEqual(self._helper("ai_maintainer_exception_eligible").returncode, 0)
        for key, value in {
            "github_repository_lower": "other/boundver", "release_tag": "v0.17.1",
            "pr_number": "182", "repository_owner_type": "Organization",
            "repository_owner_id": "9", "repository_owner_login": "other",
            "author_id": "9", "author_login": "other", "author_type": "Bot",
        }.items():
            with self.subTest(field=key):
                self.assertNotEqual(
                    self._helper("ai_maintainer_exception_eligible", **{key: value}).returncode, 0,
                )

    def test_canonical_record_distinguishes_human_base_review_from_final_agent_review(self):
        payload = report()
        result = self._helper(
            'ai_maintainer_exception_body "$1" "$2"',
            hashlib.sha256(payload).hexdigest(), base64.b64encode(payload).decode(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, body())
        self.assertEqual(self._matches(body()).returncode, 0)
        self.assertIn("authenticated non-author GitHub approval not obtained", result.stdout)
        self.assertIn("no finding, CI, scan, expiry, CVE-risk or publication waiver", result.stdout)

    def test_stale_tampered_spoofed_noncanonical_or_adverse_reports_fail_closed(self):
        record = body()
        cases = {
            "stale head": record.replace(HEAD, "2" * 40),
            "stale report": body(payload=report(head="2" * 40)),
            "wrong agent": body(payload=report(agent="other-agent")),
            "adverse verdict": body(payload=report(verdict="request-changes")),
            "wrong human base": record.replace(REVIEWED_BASE, "3" * 40),
            "wrong PR": record.replace("Pull-request: 184", "Pull-request: 182"),
            "wrong release": record.replace("Release: v0.17.0", "Release: v0.17.1"),
            "hash": record.replace(hashlib.sha256(report()).hexdigest(), "0" * 64),
            "report NUL": body(payload=report() + b"\0"),
            "body NUL": record.replace("Review-kind:", "Review-kind:\0"),
            "missing LF": record.rstrip("\n"), "extra LF": record + "\n",
            "prefix": "\n" + record, "extra field": record + "Waive-CVEs: yes\n",
            "base64": record.replace(base64.b64encode(report()).decode(),
                                     base64.b64encode(report()).decode() + "="),
            "oversized": "x" * 36865,
        }
        for label, changed in cases.items():
            with self.subTest(case=label):
                self.assertNotEqual(self._matches(changed).returncode, 0)

    def test_all_reviewed_payload_blobs_and_statuses_are_pinned(self):
        self.assertEqual(self._helper('ai_maintainer_exception_scope_matches "$1"', files()).returncode, 0)
        for path, (status, blob) in PAYLOAD_BLOBS.items():
            with self.subTest(path=path):
                self.assertNotEqual(self._helper(
                    'ai_maintainer_exception_scope_matches "$1"', files().replace(blob, "c" * 40),
                ).returncode, 0)
                self.assertNotEqual(self._helper(
                    'ai_maintainer_exception_scope_matches "$1"',
                    files().replace(f"{path}|{status}|", f"{path}|renamed|"),
                ).returncode, 0)

    def test_missing_extra_duplicate_renamed_deleted_or_malformed_scope_is_refused(self):
        rows = files().splitlines()
        for label, changed in {
            "missing": "\n".join(rows[:-1]), "empty": "",
            "extra": files() + f"\nsrc/boundver/core.py|modified|{'a' * 40}|",
            "duplicate": "\n".join(rows[:-1] + [rows[0]]),
            "removed": files().replace("|modified|", "|removed|", 1),
            "previous path": rows[0] + "previous.py\n" + "\n".join(rows[1:]),
            "new test modified": files().replace(f"{NEW_TEST}|added|", f"{NEW_TEST}|modified|"),
            "bad hash": files().replace("a" * 40, "not-a-hash", 1),
            "extra field": rows[0] + "|\n" + "\n".join(rows[1:]),
        }.items():
            with self.subTest(case=label):
                self.assertNotEqual(self._helper(
                    'ai_maintainer_exception_scope_matches "$1"', changed,
                ).returncode, 0)


@unittest.skipIf(os.name == "nt", "full release audit runs on Linux")
class AiMaintainerExceptionAuditTests(unittest.TestCase):
    def _audit(self, **overrides):
        return fixtures.LocalAgentExceptionAuditTests()._audit(
            number=184, record_body=body(), file_records=files(), file_count=24, **overrides,
        )

    def _owner_record(self, **overrides):
        from tests.test_distribution_contract import ReleaseReviewAuditTests
        return ReleaseReviewAuditTests()._comment_record(
            overrides.pop("record_body", body()), actor_id="22440724",
            login="yzm1", actor_type="User", **overrides,
        )

    def test_full_audit_accepts_only_the_exact_owner_attested_scope(self):
        result = self._audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("authenticated non-author GitHub approval NOT obtained", result.stdout)

    def test_full_audit_refuses_missing_duplicate_or_untrusted_records(self):
        from tests.test_distribution_contract import ReleaseReviewAuditTests
        fixture = ReleaseReviewAuditTests()
        owner = self._owner_record()
        records = ["", owner + "\n" + owner.replace("201|", "202|", 1)]
        for identity in (
            {"actor_id": "99", "login": "other", "actor_type": "User"},
            {"actor_id": "22440724", "login": "other", "actor_type": "User"},
            {"actor_id": "22440724", "login": "yzm1", "actor_type": "Bot"},
        ):
            records.append(fixture._comment_record(body(), **identity))
        records.append(self._owner_record(record_body=body() + "\n"))
        for record in records:
            with self.subTest(record=record[:30]):
                self.assertNotEqual(self._audit(records=record).returncode, 0)

    def test_full_audit_refuses_wrong_identity_release_scope_and_api_failures(self):
        for overrides in (
            {"FAKE_EXCEPTION_REPO_ID": "9"}, {"FAKE_EXCEPTION_FILE_COUNT": "19"},
            {"FAKE_OWNER_ID": "99"}, {"FAKE_AUTHOR_ID": "99"},
            {"AUDIT_FIXTURE_RELEASE_TAG": "v0.17.1"},
            {"FAKE_HEAD_SHA": "2" * 40},
            {"FAKE_EXCEPTION_FILES": "\n".join(files().splitlines()[:-1])},
            {"FAKE_FAILURE": "exception-repo"}, {"FAKE_FAILURE": "exception-count"},
            {"FAKE_FAILURE": "exception-files"},
        ):
            with self.subTest(overrides=overrides):
                self.assertNotEqual(self._audit(**overrides).returncode, 0)

    def test_full_audit_keeps_threads_pending_reviews_and_adverse_evidence_blocking(self):
        from tests.test_distribution_contract import ReleaseReviewAuditTests
        fixture = ReleaseReviewAuditTests()
        for overrides in (
            {"FAKE_UNRESOLVED": "1"}, {"FAKE_PENDING_REVIEWERS": "1"},
            {"FAKE_PENDING_TEAMS": "1"}, {"FAKE_DECISION": "CHANGES_REQUESTED"},
            {"FAKE_REVIEWS": fixture._review_record(HEAD, state="PENDING")},
            {"FAKE_REVIEWS": fixture._review_record(HEAD, state="CHANGES_REQUESTED")},
            {"FAKE_COMMENTS": self._owner_record() + "\n" + fixture._comment_record(
                fixture._codex_comment(HEAD, verdict="Codex Review: found a blocking issue."),
            )},
            {"FAKE_COMMENTS": self._owner_record() + "\n" + fixture._comment_record(
                fixture._codex_security_comment(HEAD).replace(
                    "No security issues were found", "A security issue was found",
                ),
            )},
        ):
            with self.subTest(overrides=overrides):
                self.assertNotEqual(self._audit(**overrides).returncode, 0)
