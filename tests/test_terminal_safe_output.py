"""End-to-end contracts for terminal-safe human output."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import boundver._output as output
import boundver.core as core


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _run_cli(root: Path, *arguments: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    previous_argv = sys.argv[:]
    previous_directory = Path.cwd()
    try:
        os.chdir(root)
        sys.argv = ["boundver", *arguments]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                core.main()
            except SystemExit as exc:
                code = int(exc.code or 0)
            else:
                code = 0
    finally:
        sys.argv = previous_argv
        os.chdir(previous_directory)
    return code, stdout.getvalue(), stderr.getvalue()


class TerminalSafeOutputTests(unittest.TestCase):
    def assert_no_forged_output(self, rendered: str) -> None:
        self.assertNotIn("\x00", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x7f", rendered)
        self.assertNotIn("\x85", rendered)
        self.assertNotIn("\x9b", rendered)
        self.assertNotIn("\r", rendered)
        self.assertNotIn("\u2028", rendered)
        self.assertNotIn("\u2029", rendered)
        self.assertFalse(
            any(line.startswith("::") for line in rendered.splitlines()),
            rendered,
        )

    def _repository(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "test@example.invalid")
        _git(root, "config", "user.name", "Test")
        (root / "svc").mkdir()
        (root / "svc" / "contract.txt").write_text(
            "contract\n",
            encoding="utf-8",
        )
        return temporary, root

    def test_argparse_error_escapes_caller_controlled_argument(self):
        temporary, root = self._repository()
        self.addCleanup(temporary.cleanup)
        malicious = "--bad\x1b[2J\n::warning title=forged::not real\x9b31m"

        code, stdout, stderr = _run_cli(root, "verify", malicious)

        self.assertEqual(code, core.EXIT_USAGE)
        self.assertEqual(stdout, "")
        self.assert_no_forged_output(stderr)
        self.assertIn("--bad\\x1b[2J\\n::warning", stderr)
        self.assertIn("\\x9b31m", stderr)

    def test_generate_text_escapes_repository_identifiers_and_consumers(self):
        temporary, root = self._repository()
        self.addCleanup(temporary.cleanup)
        project = "project café שלום\n::warning title=forged::not real\x1b[2J"
        component = "svc\r::error title=forged::not real\x9b31m"
        external = "partner\x00\x85::notice::not real"
        slice_name = "slice\x7f::warning::not real\u2028\u2029"
        config = {
            "project": project,
            "components": {
                component: {
                    "path": "svc",
                    "boundary": {
                        "provider": "path-hash",
                        "paths": ["contract.txt"],
                    },
                    "external_consumers": [external],
                }
            },
            "slices": {
                slice_name: {
                    "mode": "exact",
                    "components": [component],
                }
            },
        }
        (root / "boundary.config.json").write_text(
            json.dumps(config) + "\n",
            encoding="utf-8",
        )
        _git(root, "add", "boundary.config.json", "svc/contract.txt")
        _git(root, "commit", "-q", "-m", "fixture")

        code, stdout, stderr = _run_cli(
            root,
            "generate",
            "--source",
            "head",
            "--dry-run",
        )

        self.assertEqual(code, core.EXIT_OK, stdout + stderr)
        self.assertEqual(stderr, "")
        self.assert_no_forged_output(stdout)
        self.assertIn("café שלום\\n::warning", stdout)
        self.assertIn("svc\\r::error", stdout)
        self.assertIn("partner\\x00\\x85::notice", stdout)
        self.assertIn("slice\\x7f::warning", stdout)
        self.assertIn("\\x1b[2J", stdout)
        self.assertIn("\\x9b31m", stdout)
        self.assertIn("\\x85", stdout)
        self.assertIn("\\u2028\\u2029", stdout)

    def test_generate_json_preserves_exact_repository_values(self):
        temporary, root = self._repository()
        self.addCleanup(temporary.cleanup)
        project = "project café\n::warning::machine data\x1b[2J"
        component = "svc\r::error::machine data"
        config = {
            "project": project,
            "components": {
                component: {
                    "path": "svc",
                    "boundary": {
                        "provider": "path-hash",
                        "paths": ["contract.txt"],
                    },
                }
            },
            "slices": {},
        }
        (root / "boundary.config.json").write_text(
            json.dumps(config) + "\n",
            encoding="utf-8",
        )
        _git(root, "add", "boundary.config.json", "svc/contract.txt")
        _git(root, "commit", "-q", "-m", "fixture")

        code, stdout, stderr = _run_cli(
            root,
            "generate",
            "--source",
            "head",
            "--dry-run",
            "--format",
            "json",
        )

        self.assertEqual(code, core.EXIT_OK, stderr)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["project"], project)
        self.assertIn(component, payload["components"])
        self.assertFalse(
            any(line.startswith("::") for line in stdout.splitlines()),
            stdout,
        )

    def test_validation_error_escapes_repository_component_name(self):
        temporary, root = self._repository()
        self.addCleanup(temporary.cleanup)
        component = "bad\n::error title=forged::not real\x1b[2J"
        config = {
            "project": "project",
            "components": {
                component: {
                    "path": "missing",
                    "boundary": {"provider": "implicit"},
                }
            },
            "slices": {},
        }
        (root / "boundary.config.json").write_text(
            json.dumps(config) + "\n",
            encoding="utf-8",
        )

        code, stdout, stderr = _run_cli(root, "validate-config")

        self.assertEqual(code, core.EXIT_USAGE, stdout + stderr)
        self.assert_no_forged_output(stdout + stderr)
        self.assertIn("bad\\n::error", stdout + stderr)
        self.assertIn("\\x1b[2J", stdout + stderr)

    def test_diff_escapes_names_versions_and_summaries(self):
        diff = {
            "changed_metadata": {
                "project": {
                    "old": "before\n::warning::not real",
                    "new": "after\x1b[2J café",
                }
            },
            "components": {
                "added": [
                    {
                        "name": "added\n::warning::not real",
                        "version": "1.0.0\r::error::not real",
                    }
                ],
                "removed": [],
                "changed": [
                    {
                        "name": "changed\x00::notice::not real",
                        "old_version": "1.0.0",
                        "new_version": "2.0.0\x9b31m",
                        "summary": "boundary changed\u2028::warning::not real",
                        "changed_facets": {
                            "boundary\x7f::error::not real": {
                                "old": "a" * 64,
                                "new": "b" * 64,
                            }
                        },
                        "changed_metadata": {},
                    }
                ],
                "unchanged": [],
            },
            "slices": {
                "added": [{"name": "slice\x85::warning::not real"}],
                "removed": [],
                "changed": [],
                "unchanged": [],
            },
        }
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            output.print_diff(diff)

        rendered = stdout.getvalue()
        self.assert_no_forged_output(rendered)
        self.assertIn("added\\n::warning", rendered)
        self.assertIn("1.0.0\\r::error", rendered)
        self.assertIn("changed\\x00::notice", rendered)
        self.assertIn("2.0.0\\x9b31m", rendered)
        self.assertIn("boundary changed\\u2028::warning", rendered)
        self.assertIn("boundary\\x7f::error", rendered)
        self.assertIn("slice\\x85::warning", rendered)

    def test_why_escapes_provider_diagnostics_and_consumer_labels(self):
        provider_detail = "detail\n::warning title=forged::not real\x1b]8;;url\x07"
        digest_error = "failure\r::error::not real\x9b31m"
        external = "partner\n::notice::not real"
        drift = {
            "changes": {"boundary": {"old": "a" * 64, "new": "b" * 64}},
            "metadata_changes": {},
            "digest_errors": [digest_error],
            "summary": "boundary changed\n::warning::not real",
            "locked_fps": {"boundary": "a" * 64},
            "current_fps": {"boundary": "b" * 64},
            "changed_files": [],
            "version": "1.0.0\n::warning::not real",
            "provider_explanation": provider_detail,
        }
        config = {
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "path-hash"},
                    "external_consumers": [external],
                }
            }
        }
        lock = {"components": {"svc": {}}}
        stdout = io.StringIO()

        with patch.object(output, "analyze_component_drift", return_value=drift):
            with redirect_stdout(stdout):
                result = output.why_component(
                    config,
                    lock,
                    Path.cwd(),
                    "svc",
                )

        rendered = stdout.getvalue()
        self.assertEqual(result, 1)
        self.assert_no_forged_output(rendered)
        self.assertIn("detail\\n::warning", rendered)
        self.assertIn("failure\\r::error", rendered)
        self.assertIn("partner\\n::notice", rendered)
        self.assertIn("1.0.0\\n::warning", rendered)


if __name__ == "__main__":
    unittest.main()
