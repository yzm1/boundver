"""The owner-authorized PR #181 exception must not become a general waiver."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "scripts/audit_release_reviews.sh"
FIX_BLOBS = {
    "CHANGELOG.md": "d7c43a511491ef1ecc58a827d29924e5b035bc25",
    "scripts/publish_release.py": "1fd1a251cb82e936a6940d7aa303ddedef0b1bc1",
    "tests/test_publish_release.py": "019b882fffd50e2ed15bc2233fd0c49b4cf1fc06",
}
SUPPORT_FILES = (
    "scripts/audit_release_reviews.sh",
    "docs/RELEASING.md",
    "tests/test_distribution_contract.py",
    "tests/test_github_api_read.py",
)
NEW_TEST = "tests/test_release_review_exception.py"


def exception_files() -> str:
    rows = [f"{path}|modified|{blob}|" for path, blob in FIX_BLOBS.items()]
    rows.extend(f"{path}|modified|{'a' * 40}|" for path in SUPPORT_FILES)
    rows.append(f"{NEW_TEST}|added|{'b' * 40}|")
    return "\n".join(rows)


def exception_body(head: str) -> str:
    return "\n".join((
        "boundver-maintainer-review-exception/v1",
        "Repository: yzm1/boundver",
        "Repository-id: 1226008327",
        "Release: v0.17.0",
        "Pull-request: 181",
        f"Head-sha: {head}",
        "Original-fix: 757904de704ff2984ef5e229e92ecbf58e946ece",
        "Independent-review: not-obtained",
        "Scope: UTF-8 launcher fix and one-time exception enforcement only",
        "Reason: Codex GitHub review capacity unavailable",
        "Owner-risk-acceptance: explicit; no test, scan, finding, or publication waiver",
        "",
    ))


def _bash() -> str | None:
    if os.name == "nt":
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            directory = os.environ.get(variable)
            if directory:
                candidate = Path(directory) / "Git/bin/bash.exe"
                if candidate.is_file():
                    return str(candidate)
        return None
    return shutil.which("bash")


class ReleaseReviewExceptionTests(unittest.TestCase):
    def _helper(self, command: str, *arguments: str, **overrides: str):
        bash = _bash()
        if bash is None:
            self.skipTest("Bash is required for the actual release-audit helpers")
        source = AUDIT.read_text(encoding="utf-8")
        start = "# BEGIN one-time PR 181 maintainer exception helpers\n"
        end = "# END one-time PR 181 maintainer exception helpers"
        self.assertEqual(source.count(start), 1)
        self.assertEqual(source.count(end), 1)
        helpers = source.split(start, 1)[1].split(end, 1)[0]
        environment = dict(os.environ)
        for key in ("BASH_ENV", "ENV", "GH_TOKEN", "GITHUB_TOKEN",
                    "BOUNDVER_RELEASE_REVIEW_TOKEN"):
            environment.pop(key, None)
        environment.update({
            "github_repository_lower": "yzm1/boundver",
            "release_tag": "v0.17.0",
            "pr_number": "181",
            "repository_owner_type": "User",
            "repository_owner_id": "22440724",
            "repository_owner_login": "yzm1",
            "author_id": "22440724",
            "author_login": "yzm1",
            "author_type": "User",
            "pr_head_sha": "1" * 40,
        })
        environment.update(overrides)
        # Windows list2cmdline does not quote arguments containing only newlines.
        # Pass fixture data through quoted environment expansion, not argv text.
        environment.update({
            f"EXCEPTION_TEST_ARGUMENT_{index}": value
            for index, value in enumerate(arguments)
        })
        argument_setup = "set --" + "".join(
            f' "${{EXCEPTION_TEST_ARGUMENT_{index}}}"' for index in range(len(arguments))
        )
        return subprocess.run(
            [bash, "--noprofile", "--norc", "-c",
             helpers + "\n" + argument_setup + "\n" + command, "exception-test"],
            cwd=ROOT, env=environment, capture_output=True, text=True,
            encoding="utf-8", timeout=10,
        )

    def test_only_the_named_release_pr_and_owner_are_eligible(self):
        self.assertEqual(self._helper("maintainer_exception_eligible").returncode, 0)
        cases = {
            "github_repository_lower": "other/boundver",
            "release_tag": "v0.17.1",
            "pr_number": "182",
            "repository_owner_type": "Organization",
            "repository_owner_id": "999",
            "repository_owner_login": "other",
            "author_id": "999",
            "author_login": "other",
            "author_type": "Bot",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                result = self._helper("maintainer_exception_eligible", **{field: value})
                self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_authorization_body_binds_full_head_and_admits_absent_review(self):
        head = "7" * 40
        result = self._helper("maintainer_exception_body", pr_head_sha=head)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, exception_body(head))
        self.assertNotIn("Didn't find any major issues", result.stdout)

    def test_exact_original_fix_and_supporting_policy_scope_is_accepted(self):
        result = self._helper('maintainer_exception_scope_matches "$1"', exception_files())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_changed_original_fix_blobs_are_rejected(self):
        for path, blob in FIX_BLOBS.items():
            with self.subTest(path=path):
                rows = exception_files().replace(blob, "c" * 40)
                result = self._helper('maintainer_exception_scope_matches "$1"', rows)
                self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_missing_duplicate_extra_renamed_or_malformed_files_are_rejected(self):
        rows = exception_files().splitlines()
        cases = {
            "empty": "",
            "missing": "\n".join(rows[:-1]),
            "duplicate": "\n".join(rows[:-1] + [rows[0]]),
            "extra runtime": "\n".join(rows + [f"src/boundver/core.py|modified|{'a' * 40}|"]),
            "workflow": "\n".join(rows[:-1] + [f".github/workflows/publish.yml|added|{'a' * 40}|"]),
            "deleted": exception_files().replace("|modified|", "|removed|", 1),
            "renamed": exception_files().replace("|modified|", "|renamed|", 1),
            "previous filename": "\n".join([rows[0] + "old.txt"] + rows[1:]),
            "wrong new-file status": exception_files().replace("|added|", "|modified|"),
            "bad hash": exception_files().replace("a" * 40, "not-a-blob", 1),
            "extra field": "\n".join([rows[0] + "|"] + rows[1:]),
        }
        for label, records in cases.items():
            with self.subTest(case=label):
                result = self._helper('maintainer_exception_scope_matches "$1"', records)
                self.assertNotEqual(result.returncode, 0, result.stderr)
