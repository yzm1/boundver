"""PR182 owner attestation must not become a general reviewer-identity waiver."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "scripts/audit_release_reviews.sh"
HEAD = "1" * 40
AGENT = "01a1c535-0f5a-7cb1-b670-cf583e847831"
PAYLOAD_BLOBS = {
    ".trivyignore.yaml": "46f01ab58625d4efb2b0e92da9c691039c78b54e",
    "SECURITY.md": "785ca715363f2cc0a7df0aee1469c1369b73cb10",
    "tests/test_distribution_contract.py": "0067dc243afb20b4cd09a55619b7c678ec0c4d6e",
}
SUPPORT_FILES = (
    "scripts/audit_release_reviews.sh",
    "docs/RELEASING.md",
    "tests/test_github_api_read.py",
)
NEW_TEST = "tests/test_release_review_exception_182.py"


def report(head=HEAD, agent=AGENT, verdict="approve-conditional-scope") -> bytes:
    return (
        f"Reviewed-sha: {head}\nReviewer-agent-id: {agent}\nVerdict: {verdict}\n\n"
        "Separate local agent assessment; no blocking findings. "
        "Final exact two-platform scans and other release gates remain required.\n"
    ).encode("utf-8")


def body(head=HEAD, payload=None) -> str:
    payload = report(head) if payload is None else payload
    return "\n".join((
        "boundver-local-agent-review-exception/v1",
        "Repository: yzm1/boundver",
        "Repository-id: 1226008327",
        "Release: v0.17.0",
        "Pull-request: 182",
        f"Head-sha: {head}",
        "Original-policy: 441a68a81fc551433ffe19de9c6f4d3d999c181d",
        "Review-kind: owner-attested local-agent assessment; authenticated GitHub approval not obtained",
        f"Reviewer-agent-id: {AGENT}",
        "Verdict: approve-conditional-scope",
        f"Report-sha256: {hashlib.sha256(payload).hexdigest()}",
        f"Report-base64: {base64.b64encode(payload).decode('ascii')}",
        "Owner-authorization: explicit PR182-only identity exception; no finding, CI, scan, expiry, risk-scope or publication waiver",
        "",
    ))


def files() -> str:
    rows = [f"{path}|modified|{blob}|" for path, blob in PAYLOAD_BLOBS.items()]
    rows.extend(f"{path}|modified|{'a' * 40}|" for path in SUPPORT_FILES)
    rows.append(f"{NEW_TEST}|added|{'b' * 40}|")
    return "\n".join(rows)


def bash() -> str | None:
    if os.name == "nt":
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            directory = os.environ.get(key)
            if directory:
                candidate = Path(directory) / "Git/bin/bash.exe"
                if candidate.is_file():
                    return str(candidate)
        return None
    return shutil.which("bash")


class LocalAgentExceptionHelperTests(unittest.TestCase):
    def _helper(self, command, *arguments, **overrides):
        executable = bash()
        if executable is None:
            self.skipTest("Bash required for actual audit helpers")
        source = AUDIT.read_text(encoding="utf-8")
        start = "# BEGIN one-time PR 182 local-agent exception helpers\n"
        end = "# END one-time PR 182 local-agent exception helpers"
        self.assertEqual(source.count(start), 1)
        self.assertEqual(source.count(end), 1)
        helpers = source.split(start, 1)[1].split(end, 1)[0]
        environment = dict(os.environ)
        for key in ("BASH_ENV", "ENV", "GH_TOKEN", "GITHUB_TOKEN",
                    "BOUNDVER_RELEASE_REVIEW_TOKEN"):
            environment.pop(key, None)
        environment.update({
            "github_repository_lower": "yzm1/boundver",
            "release_tag": "v0.17.0", "pr_number": "182",
            "repository_owner_type": "User", "repository_owner_id": "22440724",
            "repository_owner_login": "yzm1", "author_id": "22440724",
            "author_login": "yzm1", "author_type": "User", "pr_head_sha": HEAD,
            "python_command": Path(sys.executable).as_posix(),
        })
        environment.update(overrides)
        environment.update({
            f"LOCAL_REVIEW_ARGUMENT_{index}": value
            for index, value in enumerate(arguments)
        })
        setup = "set --" + "".join(
            f' "${{LOCAL_REVIEW_ARGUMENT_{index}}}"'
            for index in range(len(arguments))
        )
        return subprocess.run(
            [executable, "--noprofile", "--norc", "-c",
             "set -euo pipefail\n" + helpers + "\n" + setup + "\n" + command],
            cwd=ROOT, env=environment, capture_output=True, text=True,
            encoding="utf-8", timeout=15,
        )

    def _matches(self, record):
        encoded = base64.b64encode(record.encode()).decode()
        return self._helper(
            'local_agent_exception_record_matches "$1" "$2"',
            record.replace("\0", "").rstrip("\n"), encoded,
        )

    def test_only_the_named_repository_owner_release_and_pr_are_eligible(self):
        self.assertEqual(self._helper("local_agent_exception_eligible").returncode, 0)
        for key, value in {
            "github_repository_lower": "other/boundver", "release_tag": "v0.17.1",
            "pr_number": "181", "repository_owner_type": "Organization",
            "repository_owner_id": "9", "repository_owner_login": "other",
            "author_id": "9", "author_login": "other", "author_type": "Bot",
        }.items():
            with self.subTest(field=key):
                self.assertNotEqual(
                    self._helper("local_agent_exception_eligible", **{key: value}).returncode,
                    0,
                )

    def test_body_and_report_bind_full_head_and_disclose_reduced_assurance(self):
        payload = report()
        generated = self._helper(
            'local_agent_exception_body "$1" "$2"',
            hashlib.sha256(payload).hexdigest(), base64.b64encode(payload).decode(),
        )
        self.assertEqual(generated.returncode, 0, generated.stderr)
        self.assertEqual(generated.stdout, body())
        self.assertEqual(self._matches(body()).returncode, 0)
        self.assertNotIn("Didn't find any major issues", generated.stdout)

    def test_tampered_stale_wrong_agent_or_adverse_reports_are_refused(self):
        record = body()
        cases = {
            "stale head": record.replace(HEAD, "2" * 40),
            "wrong report head": body(payload=report(head="2" * 40)),
            "wrong agent": body(payload=report(agent="other-agent")),
            "adverse verdict": body(payload=report(verdict="request-changes")),
            "tampered hash": record.replace(hashlib.sha256(report()).hexdigest(), "0" * 64),
            "report NUL": body(payload=report() + b"\0"),
            "record NUL": record.replace("Review-kind:", "Review-kind:\0"),
            "missing LF": record.rstrip("\n"),
            "extra LF": record + "\n",
            "prefix": "\n" + record,
            "wrong PR": record.replace("Pull-request: 182", "Pull-request: 183"),
            "extra field": record + "Another-field: value\n",
            "noncanonical base64": record.replace(
                base64.b64encode(report()).decode(),
                base64.b64encode(report()).decode() + "=",
            ),
        }
        for label, changed in cases.items():
            with self.subTest(case=label):
                result = self._matches(changed)
                self.assertNotEqual(result.returncode, 0, result.stderr)
        oversized = self._helper(
            'oversized=$(printf "%040000d" 0); local_agent_exception_record_matches "$oversized" x'
        )
        self.assertNotEqual(oversized.returncode, 0)

    def test_exact_seven_file_scope_is_accepted_and_original_payload_is_pinned(self):
        result = self._helper('local_agent_exception_scope_matches "$1"', files())
        self.assertEqual(result.returncode, 0, result.stderr)
        for path, blob in PAYLOAD_BLOBS.items():
            with self.subTest(path=path):
                result = self._helper(
                    'local_agent_exception_scope_matches "$1"', files().replace(blob, "c" * 40)
                )
                self.assertNotEqual(result.returncode, 0)

    def test_missing_extra_duplicate_renamed_deleted_or_malformed_scope_is_refused(self):
        rows = files().splitlines()
        for label, changed in {
            "empty": "", "missing": "\n".join(rows[:-1]),
            "duplicate": "\n".join(rows[:-1] + [rows[0]]),
            "extra": files() + f"\nsrc/boundver/core.py|modified|{'a' * 40}|",
            "renamed": files().replace("|modified|", "|renamed|", 1),
            "deleted": files().replace("|modified|", "|removed|", 1),
            "previous path": rows[0] + "previous.py\n" + "\n".join(rows[1:]),
            "new-file modified": files().replace("|added|", "|modified|"),
            "bad hash": files().replace("a" * 40, "not-a-hash", 1),
            "extra field": rows[0] + "|\n" + "\n".join(rows[1:]),
        }.items():
            with self.subTest(case=label):
                result = self._helper('local_agent_exception_scope_matches "$1"', changed)
                self.assertNotEqual(result.returncode, 0, result.stderr)


@unittest.skipIf(os.name == "nt", "full release audit runs on Linux")
class LocalAgentExceptionAuditTests(unittest.TestCase):
    def _audit(self, *, records=None, **overrides):
        # Reuse the existing real-Git/full-audit fixture, not another setup helper.
        from tests.test_distribution_contract import ReleaseReviewAuditTests

        fixture = ReleaseReviewAuditTests()
        original_write = Path.write_bytes
        original_run = subprocess.run
        routes = """
if [[ "$endpoint" == "repos/$GITHUB_REPOSITORY" && "$args" == *" --jq .id "* ]]; then
  if [[ "$FAKE_FAILURE" == exception-repo ]]; then exit 73; fi
  printf '%s' "$FAKE_EXCEPTION_REPO_ID"; exit 0
fi
if [[ "$endpoint" == */pulls/182 && "$args" == *" --jq .changed_files "* ]]; then
  if [[ "$FAKE_FAILURE" == exception-count ]]; then exit 73; fi
  printf '%s' "$FAKE_EXCEPTION_FILE_COUNT"; exit 0
fi
if [[ "$endpoint" == */pulls/182/files* ]]; then
  if [[ "$FAKE_FAILURE" == exception-files ]]; then exit 73; fi
  printf '%s' "$FAKE_EXCEPTION_FILES"; exit 0
fi
"""

        def write_bytes(path, data):
            if path.name == "gh":
                source = data.decode()
                source = source.replace("echo 17\n", "echo 182\n")
                source = source.replace("/pulls/17", "/pulls/182")
                source = source.replace("/issues/17", "/issues/182")
                marker = 'if [[ "$endpoint" == "repos/$GITHUB_REPOSITORY/releases?per_page=100" ]]; then'
                self.assertEqual(source.count(marker), 1)
                data = source.replace(marker, routes + "\n" + marker).encode()
            return original_write(path, data)

        def run(command, *args, **kwargs):
            if len(command) > 1 and command[1] == "./audit_release_reviews.sh":
                environment = dict(kwargs["env"])
                environment["FAKE_PUBLICATION_RUNS"] = environment["FAKE_PUBLICATION_RUNS"].replace(
                    "owner/repository", "yzm1/boundver"
                )
                kwargs["env"] = environment
            return original_run(command, *args, **kwargs)

        comments = fixture._comment_record(
            body(), actor_id="22440724", login="yzm1", actor_type="User"
        ) if records is None else records
        environment = {
            "GITHUB_REPOSITORY": "yzm1/boundver",
            "FAKE_OWNER_ID": "22440724", "FAKE_OWNER_LOGIN": "yzm1",
            "FAKE_AUTHOR_ID": "22440724", "FAKE_AUTHOR_LOGIN": "yzm1",
            "FAKE_BASE_REPOSITORY": "yzm1/boundver", "FAKE_HEAD_SHA": HEAD,
            "FAKE_REVIEWS": "", "FAKE_COMMENTS": comments,
            "FAKE_EXCEPTION_REPO_ID": "1226008327",
            "FAKE_EXCEPTION_FILE_COUNT": "7", "FAKE_EXCEPTION_FILES": files(),
        }
        environment.update(overrides)
        with mock.patch.object(Path, "write_bytes", autospec=True, side_effect=write_bytes), \
                mock.patch.object(subprocess, "run", side_effect=run):
            return fixture._run_audit(**environment)

    def test_full_audit_accepts_only_the_scoped_owner_attested_record(self):
        result = self._audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("authenticated GitHub approval NOT obtained", result.stdout)

    def test_full_audit_refuses_missing_spoofed_duplicate_or_tampered_records(self):
        from tests.test_distribution_contract import ReleaseReviewAuditTests

        fixture = ReleaseReviewAuditTests()
        owner = fixture._comment_record(
            body(), actor_id="22440724", login="yzm1", actor_type="User"
        )
        cases = (
            "",
            fixture._comment_record(body(), actor_id="99", login="other", actor_type="User"),
            fixture._comment_record(body(), actor_id="22440724", login="other", actor_type="User"),
            fixture._comment_record(body(), actor_id="22440724", login="yzm1", actor_type="Bot"),
            owner + "\n" + owner.replace("201|", "202|", 1),
            fixture._comment_record(
                body() + "\n", actor_id="22440724", login="yzm1", actor_type="User"
            ),
        )
        for records in cases:
            with self.subTest(records=records[:30]):
                self.assertNotEqual(self._audit(records=records).returncode, 0)

    def test_full_audit_refuses_incomplete_wrong_scope_or_failed_api_reads(self):
        cases = (
            {"FAKE_EXCEPTION_REPO_ID": "9"},
            {"FAKE_EXCEPTION_FILE_COUNT": "6"},
            {"FAKE_EXCEPTION_FILES": "\n".join(files().splitlines()[:-1])},
            {"FAKE_EXCEPTION_FILES": files().replace("46f01ab58625d4efb2b0e92da9c691039c78b54e", "d" * 40)},
            {"FAKE_FAILURE": "exception-repo"},
            {"FAKE_FAILURE": "exception-count"},
            {"FAKE_FAILURE": "exception-files"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.assertNotEqual(self._audit(**overrides).returncode, 0)

    def test_full_audit_does_not_waive_pending_threads_or_adverse_reviews(self):
        from tests.test_distribution_contract import ReleaseReviewAuditTests

        fixture = ReleaseReviewAuditTests()
        cases = [
            {"FAKE_PENDING_REVIEWERS": "1"},
            {"FAKE_PENDING_TEAMS": "1"},
            {"FAKE_UNRESOLVED": "1"},
            {"FAKE_DECISION": "CHANGES_REQUESTED"},
            {"FAKE_REVIEWS": fixture._review_record(HEAD, state="CHANGES_REQUESTED")},
            {"FAKE_REVIEWS": fixture._review_record(
                HEAD, state="CHANGES_REQUESTED", actor_id="99", login="other", actor_type="User"
            )},
            {"FAKE_REVIEWS": fixture._review_record(HEAD, state="PENDING")},
            {"FAKE_REVIEWS": fixture._review_record(
                HEAD, state="PENDING", actor_id="99", login="other", actor_type="User"
            )},
            {"FAKE_COMMENTS": fixture._comment_record(
                body(), actor_id="22440724", login="yzm1", actor_type="User"
            ) + "\n" + fixture._comment_record(
                fixture._codex_comment(HEAD, verdict="Codex Review: found a blocking issue.")
            )},
            {"FAKE_COMMENTS": fixture._comment_record(
                body(), actor_id="22440724", login="yzm1", actor_type="User"
            ) + "\n" + fixture._comment_record(
                fixture._codex_security_comment(HEAD).replace(
                    "No security issues were found", "A security issue was found"
                )
            )},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.assertNotEqual(self._audit(**overrides).returncode, 0)
