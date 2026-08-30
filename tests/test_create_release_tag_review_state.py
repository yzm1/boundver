"""Focused contracts for the create-tag review-state handoff."""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from tests._project_metadata import CURRENT_TAG as RELEASE_TAG


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "create-release-tag.yml"
AUDIT = ROOT / "scripts" / "audit_release_reviews.sh"
RELEASE_SHA = "a" * 40
REPOSITORY = "acme/widget"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _review_state_runner() -> dict:
    """Load only the workflow-defined bounded subprocess helpers."""
    program = _workflow()["env"]["REVIEW_STATE_PROGRAM"]
    tree = ast.parse(program)
    assignments = {
        "MAX_API_BYTES",
        "MAX_STDERR_BYTES",
        "MAX_JSON_NUMBER_DIGITS",
        "READ_CHUNK_BYTES",
        "consumed_api_bytes",
    }
    functions = {
        "kill_process",
        "read_bounded_pipe",
        "run",
        "unique_json_object",
        "bounded_json_int",
        "bounded_json_float",
        "reject_json_constant",
    }
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in assignments
            for target in node.targets
        ):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in functions:
            selected.append(node)
    namespace: dict = {}
    helper_module = ast.fix_missing_locations(
        ast.Module(body=selected, type_ignores=[])
    )
    exec(compile(helper_module, "<review-state-helpers>", "exec"), namespace)
    return namespace


def _publication_state_helpers() -> dict:
    program = _workflow()["env"]["PUBLICATION_RUN_STATE_PROGRAM"]
    tree = ast.parse(program)
    assignments = {
        "MAX_PAGE_BYTES",
        "MAX_TOTAL_BYTES",
        "MAX_STDERR_BYTES",
        "MAX_PAGES",
        "MAX_ITEMS",
        "READ_CHUNK_BYTES",
        "MAX_UINT64",
        "ACTIVE_STATES",
        "consumed_bytes",
    }
    functions = {
        "kill_process",
        "read_pipe",
        "unique_object",
        "parse_integer",
        "reject_constant",
        "api_page",
        "iter_runs",
    }
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in assignments
            for target in node.targets
        ):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in functions:
            selected.append(node)
    namespace: dict = {}
    helper_module = ast.fix_missing_locations(
        ast.Module(body=selected, type_ignores=[])
    )
    exec(compile(helper_module, "<publication-state-helpers>", "exec"), namespace)
    return namespace


class CreateTagReviewStateContracts(unittest.TestCase):
    @unittest.skipUnless(
        hasattr(sys, "set_int_max_str_digits"),
        "runtime integer digit limit requires Python 3.11+",
    )
    def test_snapshot_json_parser_is_strict_and_int_limit_independent(self):
        helpers = _review_state_runner()
        parse_integer = helpers["bounded_json_int"]
        original_limit = sys.get_int_max_str_digits()
        try:
            for limit in (640, 4300, 0):
                with self.subTest(limit=limit):
                    sys.set_int_max_str_digits(limit)
                    value = parse_integer("9" * 1000)
                    self.assertEqual(value, (10**1000) - 1)
                    with self.assertRaisesRegex(ValueError, "oversized JSON integer"):
                        parse_integer("9" * 4301)
        finally:
            sys.set_int_max_str_digits(original_limit)

        with self.assertRaisesRegex(ValueError, "duplicate JSON object key"):
            helpers["unique_json_object"]([("id", 1), ("id", 2)])
        with self.assertRaisesRegex(ValueError, "non-finite JSON"):
            helpers["reject_json_constant"]("NaN")
        with self.assertRaisesRegex(ValueError, "non-finite JSON float"):
            helpers["bounded_json_float"]("1e999")

    def test_publication_run_queries_are_explicitly_paginated_and_bounded(self):
        workflow = _workflow()
        program = workflow["env"]["PUBLICATION_RUN_STATE_PROGRAM"]
        workflow_text = WORKFLOW.read_text(encoding="utf-8")
        dispatch = workflow["jobs"]["dispatch-publication"]

        self.assertNotIn("--paginate", workflow_text)
        self.assertIn("for page_number in range(1, MAX_PAGES + 1):", program)
        self.assertIn("MAX_TOTAL_BYTES = 32 * 1024 * 1024", program)
        self.assertIn("MAX_ITEMS = 10_000", program)
        self.assertIn("len(records) > 100", program)
        self.assertIn("kill_process(process)", program)
        self.assertEqual(
            workflow_text.count('"$PUBLICATION_RUN_STATE_PROGRAM" active'),
            3,
        )
        self.assertEqual(
            workflow_text.count('"$PUBLICATION_RUN_STATE_PROGRAM" exact'),
            1,
        )
        self.assertEqual(
            dispatch["permissions"], {"actions": "write", "contents": "read"}
        )
        self.assertFalse(
            any(
                "actions/checkout@" in str(step.get("uses", ""))
                for step in dispatch["steps"]
            )
        )
        dispatch_script = "\n".join(
            str(step.get("run", "")) for step in dispatch["steps"]
        )
        self.assertNotIn("scripts/", dispatch_script)
        self.assertIn("clean_python_cwd=$(mktemp -d)", dispatch_script)
        self.assertIn("python3 -I -c", dispatch_script)

    @unittest.skipUnless(
        hasattr(sys, "set_int_max_str_digits"),
        "runtime integer digit limit requires Python 3.11+",
    )
    def test_publication_json_integer_parser_is_runtime_setting_independent(self):
        helpers = _publication_state_helpers()
        parse_integer = helpers["parse_integer"]
        original_limit = sys.get_int_max_str_digits()
        try:
            for limit in (640, 4300, 0):
                with self.subTest(limit=limit):
                    sys.set_int_max_str_digits(limit)
                    with self.assertRaisesRegex(ValueError, "oversized JSON integer"):
                        parse_integer("9" * 1000)
                    self.assertEqual(parse_integer("18446744073709551615"), 2**64 - 1)
        finally:
            sys.set_int_max_str_digits(original_limit)

    def test_publication_pagination_finds_active_runs_after_first_page(self):
        helpers = _publication_state_helpers()
        helpers["MAX_PAGES"] = 3
        helpers["MAX_ITEMS"] = 300
        completed = [{"id": run_id, "status": "completed"} for run_id in range(1, 101)]
        active = {"id": 101, "status": "in_progress"}
        pages = [
            {"total_count": 101, "workflow_runs": completed},
            {"total_count": 101, "workflow_runs": [active]},
        ]

        def api_page(_endpoint):
            return pages.pop(0)

        helpers["api_page"] = api_page
        records = list(helpers["iter_runs"]("runs?per_page=100"))
        self.assertEqual(len(records), 101)
        self.assertIn("in_progress", {record["status"] for record in records})

    def test_publication_pagination_fails_closed_at_page_and_item_ceilings(self):
        helpers = _publication_state_helpers()
        helpers["MAX_PAGES"] = 2
        helpers["MAX_ITEMS"] = 200
        next_id = 0

        def full_page(_endpoint):
            nonlocal next_id
            records = []
            for _ in range(100):
                next_id += 1
                records.append({"id": next_id, "status": "completed"})
            return {"total_count": 200, "workflow_runs": records}

        helpers["api_page"] = full_page
        with self.assertRaisesRegex(SystemExit, "exceeds the page limit"):
            list(helpers["iter_runs"]("runs?per_page=100"))

        helpers = _publication_state_helpers()
        helpers["MAX_ITEMS"] = 100
        helpers["api_page"] = lambda _endpoint: {
            "total_count": 101,
            "workflow_runs": [],
        }
        with self.assertRaisesRegex(SystemExit, "invalid page"):
            list(helpers["iter_runs"]("runs?per_page=100"))

    def test_semantic_snapshot_binds_merged_pr_destination(self):
        program = _workflow()["env"]["REVIEW_STATE_PROGRAM"]

        self.assertIn('pr_state = pr.get("state")', program)
        self.assertIn('merged_at = pr.get("merged_at")', program)
        self.assertIn('base_ref = base.get("ref")', program)
        self.assertIn('base_repo.get("full_name")', program)
        self.assertIn('"state": pr_state', program)
        self.assertIn('"merged_at": merged_at', program)
        self.assertIn('"base_repository": base_repository', program)
        self.assertIn('"base_ref": base_ref', program)
        self.assertIn('pr_state != "closed"', program)
        self.assertIn('base_ref != "main"', program)
        self.assertIn(
            "base_repository.casefold() != repository.casefold()",
            program,
        )
        self.assertIn("fullDatabaseId lastEditedAt", program)
        self.assertIn("GitHub REST and GraphQL review identities differ", program)
        self.assertIn('"repository_id": repository_id', program)

    def test_snapshot_normalizes_only_the_local_candidate_tag(self):
        program = _workflow()["env"]["REVIEW_STATE_PROGRAM"]

        self.assertIn("candidate != release_tag", program)
        self.assertIn('"merged_semver_tags": [item[1] for item in merged_tags]', program)

    def test_snapshot_commands_use_streaming_preallocation_caps(self):
        program = _workflow()["env"]["REVIEW_STATE_PROGRAM"]

        self.assertIn("subprocess.Popen(", program)
        self.assertNotIn("subprocess.run(", program)
        self.assertIn("remaining_with_sentinel", program)
        self.assertIn("kill_process(process)", program)

    def test_snapshot_runner_kills_oversize_stdout_before_producer_finishes(self):
        helpers = _review_state_runner()
        helpers["MAX_API_BYTES"] = 1024
        helpers["MAX_STDERR_BYTES"] = 1024
        helpers["consumed_api_bytes"] = 0
        run = helpers["run"]

        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "producer-finished"
            producer = "\n".join(
                (
                    "import os",
                    "import pathlib",
                    "import sys",
                    "for _ in range(1024):",
                    "    os.write(1, b'x' * 65536)",
                    "pathlib.Path(sys.argv[1]).write_text('finished')",
                )
            )
            with self.assertRaisesRegex(
                SystemExit,
                "API responses exceed the safety limit",
            ):
                run(
                    [sys.executable, "-I", "-c", producer, str(marker)],
                    cwd=temporary,
                    api=True,
                )
            self.assertFalse(marker.exists())

    def test_pipe_reader_requests_only_remaining_bytes_plus_sentinel(self):
        helpers = _review_state_runner()
        helpers["READ_CHUNK_BYTES"] = 4

        class FakeProcess:
            killed = False

            def kill(self):
                self.killed = True

        class FakePipe:
            def __init__(self):
                self.remaining = bytearray(b"x" * 100)
                self.requests = []
                self.closed = False

            def read1(self, size):
                self.requests.append(size)
                chunk = self.remaining[:size]
                del self.remaining[:size]
                return bytes(chunk)

            read = read1

            def close(self):
                self.closed = True

        process = FakeProcess()
        pipe = FakePipe()
        buffer = bytearray()
        overflows = []
        errors = []
        helpers["read_bounded_pipe"](
            process,
            pipe,
            "stdout",
            5,
            buffer,
            overflows,
            errors,
        )

        self.assertEqual(pipe.requests, [4, 2])
        self.assertLessEqual(len(buffer), 5)
        self.assertEqual(overflows, ["stdout"])
        self.assertEqual(errors, [])
        self.assertTrue(process.killed)
        self.assertTrue(pipe.closed)

    def test_snapshot_runner_caps_stderr_and_preserves_small_output(self):
        helpers = _review_state_runner()
        helpers["MAX_API_BYTES"] = 1024
        helpers["MAX_STDERR_BYTES"] = 1024
        helpers["consumed_api_bytes"] = 0
        run = helpers["run"]

        output = run(
            [sys.executable, "-I", "-c", "print('bounded')"],
            cwd=ROOT,
            api=True,
        )
        self.assertEqual(output, bytearray(f"bounded{os.linesep}".encode()))
        self.assertEqual(helpers["consumed_api_bytes"], len(output))

        with self.assertRaisesRegex(SystemExit, "stderr exceeds the safety limit"):
            run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import os; os.write(2, b'x' * 65536)",
                ],
                cwd=ROOT,
            )

    def test_write_job_rechecks_digest_after_local_tag_and_auth(self):
        workflow = _workflow()
        tag_job = workflow["jobs"]["tag"]
        steps = {step["name"]: step for step in tag_job["steps"]}
        mutation = steps["Create the tag with credentials scoped to the push boundary"]
        script = mutation["run"]

        local_tag = script.index('git tag "$RELEASE_TAG" "$RELEASE_SHA"')
        auth = script.index("gh auth setup-git", local_tag)
        final_digest = script.index("authenticated_review_state_digest=", auth)
        final_compare = script.index(
            '"$authenticated_review_state_digest" != '
            '"$EXPECTED_REVIEW_STATE_DIGEST"',
            final_digest,
        )
        push = script.index(
            'git -c core.hooksPath=/dev/null push origin '
            '"refs/tags/$RELEASE_TAG"',
            final_compare,
        )

        self.assertLess(local_tag, auth)
        self.assertLess(auth, final_digest)
        self.assertLess(final_digest, final_compare)
        self.assertLess(final_compare, push)
        self.assertNotIn("scripts/", script)
        self.assertEqual(tag_job["permissions"]["contents"], "write")
        self.assertIn("GH_TOKEN", mutation["env"])

        checkout = steps["Checkout the exact candidate"]
        self.assertFalse(checkout["with"]["persist-credentials"])
        for step in tag_job["steps"]:
            if step is mutation:
                continue
            self.assertNotIn("GH_TOKEN", step.get("env", {}))

    def test_read_only_job_owns_candidate_audit(self):
        workflow = _workflow()
        revalidate = workflow["jobs"]["revalidate-release-state"]
        self.assertEqual(revalidate["permissions"]["contents"], "read")
        self.assertEqual(revalidate["permissions"]["issues"], "read")
        self.assertEqual(revalidate["permissions"]["pull-requests"], "read")
        scripts = "\n".join(
            str(step.get("run", "")) for step in revalidate["steps"]
        )
        self.assertIn("bash scripts/audit_release_reviews.sh", scripts)

    def test_shell_audit_requests_and_enforces_merge_destination(self):
        source = AUDIT.read_text(encoding="utf-8")
        self.assertIn(
            '"$python_command" -I - "$release_tag" "$merged_tags_file"',
            source,
        )
        for field in (
            ".state",
            '(.merged_at // "")',
            '(.base.repo.full_name // "")',
            '(.base.ref // "")',
        ):
            self.assertIn(field, source)
        self.assertIn('"$pr_state" != "closed"', source)
        self.assertIn('"$pr_base_ref" != "main"', source)
        self.assertIn("github_repository_lower=$(", source)
        self.assertIn("pr_base_repository_lower=$(", source)
        self.assertIn(
            '"$pr_base_repository_lower" != "$github_repository_lower"',
            source,
        )
        self.assertIn('if (( ${#review_records[@]} > 0 )); then', source)
        self.assertIn('if (( ${#comment_records[@]} > 0 )); then', source)
        cleanup_helper = source.split("cleanup_capture_temp() {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn("local status=$?", cleanup_helper)
        self.assertIn('exit "$status"', cleanup_helper)
        capture_helper = source.split("capture_bounded() {", 1)[1].split("\n}", 1)[0]
        self.assertGreaterEqual(capture_helper.count('rm -f -- "$target"'), 3)


@unittest.skipIf(os.name == "nt", "audit harness requires POSIX executables")
class ReviewAuditMergeDestinationTests(unittest.TestCase):
    def test_rejects_unmerged_or_wrong_destination_associations(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            fake_bin = temporary_path / "bin"
            fake_bin.mkdir()
            fake_git = fake_bin / "git"
            fake_gh = fake_bin / "gh"
            fake_git.write_text(
                """#!/bin/sh
case "$1" in
  fetch) exit 0 ;;
  tag) printf '%s\\n' 'v0.10.0' ;;
  rev-list) printf '%s\\n' "$FAKE_RELEASE_SHA" ;;
  *) printf '%s\\n' "unexpected git invocation: $*" >&2; exit 90 ;;
esac
""",
                encoding="utf-8",
            )
            fake_gh.write_text(
                """#!/bin/sh
if [ -n "${FAKE_CAPTURE_ROOT:-}" ]; then
  capture_count=$(find "$FAKE_CAPTURE_ROOT" -type f -name 'capture-*' | wc -l)
  if [ "$capture_count" -gt 1 ]; then
    : > "$FAKE_ACCUMULATION_MARKER"
  fi
fi
if [ "${FAKE_ASSOCIATION_OVERSIZE:-0}" = 1 ]; then
  case "$*" in
    *repos/acme/widget/commits/*/pulls*)
      python3 -I -c 'import os
import pathlib
import sys
for _ in range(1024):
    os.write(1, b"1\\n" * 32768)
pathlib.Path(sys.argv[1]).write_text("finished")' "$FAKE_COMPLETION_MARKER"
      exit $?
      ;;
  esac
fi
case "$*" in
  *repos/acme/widget/commits/*/pulls*) printf '%s\\n' '17' ;;
  *repos/acme/widget/pulls/17*) printf '%s\\n' "$FAKE_PR_METADATA" ;;
  *'repos/acme/widget --jq'*) printf '%s\\n' '1|acme|Organization' ;;
  *) printf '%s\\n' "unexpected gh invocation: $*" >&2; exit 91 ;;
esac
""",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            fake_gh.chmod(0o755)
            hostile_imports = temporary_path / "hostile-imports"
            hostile_imports.mkdir()
            sitecustomize_marker = temporary_path / "sitecustomize-ran"
            audit_temp_root = temporary_path / "audit-temp"
            audit_temp_root.mkdir()
            accumulation_marker = temporary_path / "captures-accumulated"
            (hostile_imports / "sitecustomize.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(sitecustomize_marker)!r}).write_text('ran')\n",
                encoding="utf-8",
            )

            common = [
                "7",
                "contributor",
                "User",
                RELEASE_SHA,
                "b" * 40,
                "0",
                "0",
            ]
            cases = {
                "open": ["open", "", REPOSITORY, "main"],
                "wrong repository": [
                    "closed",
                    "2026-08-18T12:00:00Z",
                    "acme/other",
                    "main",
                ],
                "wrong base": [
                    "closed",
                    "2026-08-18T12:00:00Z",
                    REPOSITORY,
                    "release",
                ],
            }
            for label, association in cases.items():
                with self.subTest(case=label):
                    environment = os.environ.copy()
                    environment.update(
                        {
                            "FAKE_PR_METADATA": "|".join(common + association),
                            "FAKE_ACCUMULATION_MARKER": str(accumulation_marker),
                            "FAKE_CAPTURE_ROOT": str(audit_temp_root),
                            "FAKE_RELEASE_SHA": RELEASE_SHA,
                            "GH_TOKEN": "test-token",
                            "GITHUB_REPOSITORY": REPOSITORY,
                            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
                            "PYTHON": sys.executable,
                            "PYTHONPATH": str(hostile_imports),
                            "TMPDIR": str(audit_temp_root),
                        }
                    )
                    result = subprocess.run(
                        [bash, str(AUDIT), RELEASE_SHA, RELEASE_TAG],
                        cwd=temporary_path,
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(
                        f"associated PR must be merged into {REPOSITORY}:main",
                        result.stderr,
                    )
            self.assertFalse(sitecustomize_marker.exists())
            self.assertFalse(accumulation_marker.exists())

            bounded_audit = temporary_path / "bounded-audit.sh"
            audit_source = AUDIT.read_text(encoding="utf-8")
            bounded_source = audit_source.replace(
                "readonly max_capture_bytes=$((128 * 1024 * 1024))",
                "readonly max_capture_bytes=1024",
            )
            self.assertNotEqual(audit_source, bounded_source)
            bounded_audit.write_text(
                bounded_source,
                encoding="utf-8",
            )
            bounded_audit.chmod(0o755)
            completion_marker = temporary_path / "oversize-producer-finished"
            environment = os.environ.copy()
            environment.update(
                {
                    "FAKE_ASSOCIATION_OVERSIZE": "1",
                    "FAKE_ACCUMULATION_MARKER": str(accumulation_marker),
                    "FAKE_CAPTURE_ROOT": str(audit_temp_root),
                    "FAKE_COMPLETION_MARKER": str(completion_marker),
                    "FAKE_RELEASE_SHA": RELEASE_SHA,
                    "GH_TOKEN": "test-token",
                    "GITHUB_REPOSITORY": REPOSITORY,
                    "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
                    "PYTHON": sys.executable,
                    "PYTHONPATH": str(hostile_imports),
                    "TMPDIR": str(audit_temp_root),
                }
            )
            result = subprocess.run(
                [bash, str(bounded_audit), RELEASE_SHA, RELEASE_TAG],
                cwd=temporary_path,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("exceeded the 1024-byte safety limit", result.stderr)
            self.assertFalse(completion_marker.exists())
            self.assertFalse(sitecustomize_marker.exists())
            self.assertFalse(accumulation_marker.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
