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
        "MAX_COMMAND_BYTES",
        "MAX_STDERR_BYTES",
        "MAX_RECORDS",
        "MAX_PAGES",
        "MAX_JSON_NUMBER_DIGITS",
        "MAX_GITHUB_ID",
        "READ_CHUNK_BYTES",
        "GITHUB_TIMESTAMP_RE",
        "PUBLICATION_TITLE_RE",
        "PUBLISH_WORKFLOW_NAME",
        "PUBLISH_WORKFLOW_FILE",
        "PUBLISH_WORKFLOW_PATH",
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
        "workflow_run_records",
        "github_timestamp_parts",
        "timestamp_key",
        "trusted_publication_runs",
        "published_release_anchors",
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
    namespace["command_environment"] = dict(os.environ)
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
        "MAX_JSON_NUMBER_CHARACTERS",
        "ACTIVE_STATES",
        "consumed_bytes",
    }
    functions = {
        "kill_process",
        "read_pipe",
        "unique_object",
        "parse_integer",
        "parse_float",
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
    namespace["command_environment"] = dict(os.environ)
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

        parse_float = helpers["parse_float"]
        self.assertEqual(parse_float("1.25"), 1.25)
        with self.assertRaisesRegex(ValueError, "oversized JSON float"):
            parse_float("0." + ("1" * 100))
        with self.assertRaisesRegex(ValueError, "non-finite JSON float"):
            parse_float("1e999")

    def test_workflow_commands_are_bound_to_safe_git_and_github_state(self):
        workflow = _workflow()
        for name, value in {
            "GCM_INTERACTIVE": "never",
            "GH_HOST": "github.com",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }.items():
            self.assertEqual(workflow["env"][name], value)

        review_program = workflow["env"]["REVIEW_STATE_PROGRAM"]
        self.assertIn('"GIT_CONFIG_GLOBAL": os.devnull', review_program)
        self.assertIn('"GIT_CONFIG_NOSYSTEM": "1"', review_program)
        self.assertIn('"core.fsmonitor"', review_program)
        self.assertIn("env=command_environment", review_program)

        publication_program = workflow["env"]["PUBLICATION_RUN_STATE_PROGRAM"]
        self.assertIn('"GH_CONFIG_DIR": str(gh_config_dir)', publication_program)
        self.assertIn('parse_float=parse_float', publication_program)
        self.assertIn('server_url = "https://github.com"', publication_program)

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
        self.assertIn("SEMANTIC_REVIEW_ROSTER_GIST_ID", program)
        self.assertIn("G_kwDOAVZrFNoAIDBjYWVkYjc5OGQxNjhiOTc0ZjlkOWZiNjNjMzc3Zjcz", program)
        self.assertIn("parse_semantic_review_roster", program)
        self.assertIn("def public_gist_json(endpoint):", program)
        self.assertIn("http.client.HTTPSConnection", program)
        self.assertIn('"api.github.com"', program)
        self.assertNotIn('"Authorization"', program)
        self.assertIn('GITHUB_REST_API_VERSION = "2022-11-28"', program)
        self.assertIn("Independent-beneficial-owners-attested: true", program)
        self.assertIn(
            "Owner-exclusive-mutation-authority-attested: true", program
        )
        self.assertIn("normalize_semantic_roster_gist", program)
        self.assertIn("Semantic review roster gist changed during collection", program)
        self.assertIn('record.get("public") is not True', program)
        self.assertIn('record.get("truncated") is not False', program)
        self.assertIn('permission_record.get("permission") != "read"', program)
        self.assertIn("Semantic roster reviewer is not read-only", program)
        self.assertIn('f"repos/{repository}/collaborators?per_page=100"', program)
        self.assertIn("Repository mutation authority is not owner-exclusive", program)
        self.assertIn('"repository_mutation_authority": repository_mutation_authority', program)
        self.assertIn(
            '"owner_attested_exclusive_mutation_authority": True', program
        )
        self.assertIn('"semantic_review_authority": semantic_review_authority', program)

    def test_snapshot_anchors_only_to_immutable_published_releases(self):
        program = _workflow()["env"]["REVIEW_STATE_PROGRAM"]

        self.assertIn(
            'rest_records(f"repos/{repository}/releases?per_page=100")',
            program,
        )
        self.assertIn(
            'actions/workflows/{PUBLISH_WORKFLOW_FILE}/runs',
            program,
        )
        self.assertIn('"published_release_anchors": release_anchors', program)
        self.assertNotIn('"merged_semver_tags"', program)

        helpers = _review_state_runner()
        helpers["git_text"] = lambda *_arguments: "1" * 40
        records = [
            {
                "id": 101,
                "tag_name": "v0.14.1",
                "draft": False,
                "prerelease": False,
                "immutable": True,
                "published_at": "2026-08-27T15:05:36Z",
            },
            {
                "id": 102,
                "tag_name": "v0.14.2",
                "draft": False,
                "prerelease": False,
                "immutable": False,
                "published_at": "2026-08-28T15:05:36Z",
            },
            {
                "id": 103,
                "tag_name": "v0.14.3",
                "draft": False,
                "prerelease": True,
                "immutable": True,
                "published_at": "2026-08-29T15:05:36Z",
            },
        ]
        anchors = helpers["published_release_anchors"](
            records,
            (0, 15, 0),
            {"v0.14.1", "v0.14.2", "v0.14.3", "v0.14.999"},
            {
                ("v0.14.1", "1" * 40): {
                    "completed_at": "2026-08-27T16:05:36Z",
                }
            },
        )

        self.assertEqual([item["tag"] for item in anchors], ["v0.14.1"])
        self.assertEqual(anchors[0]["sha"], "1" * 40)

        malformed = dict(records[0])
        malformed.pop("immutable")
        with self.assertRaisesRegex(
            SystemExit,
            "malformed semantic release metadata",
        ):
            helpers["published_release_anchors"](
                [malformed],
                (0, 15, 0),
                {"v0.14.1"},
                {},
            )

    def test_snapshot_requires_exact_successful_publication_provenance(self):
        helpers = _review_state_runner()
        tag = "v0.14.1"
        tag_sha = "1" * 40
        control_sha = "2" * 40
        repository = "acme/widget"
        title = (
            f"publish:{tag}@{tag_sha}:alias=v0.14:resume=33058238333"
        )
        run = {
            "id": 33112740009,
            "name": title,
            "path": ".github/workflows/publish.yml",
            "display_title": title,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_sha": control_sha,
            "head_branch": "main",
            "run_attempt": 1,
            "workflow_id": 270075259,
            "repository": {"full_name": repository},
            "head_repository": {"full_name": repository},
            "created_at": "2026-08-27T20:19:13Z",
            "updated_at": "2026-08-29T07:02:04Z",
            "html_url": (
                "https://github.com/acme/widget/actions/runs/33112740009"
            ),
        }

        trusted = helpers["trusted_publication_runs"](
            [run], 270075259, repository, {tag_sha, control_sha}
        )
        self.assertEqual(trusted[(tag, tag_sha)]["run_id"], 33112740009)

        no_alias_title = title.replace("alias=v0.14", "alias=none")
        no_alias_run = dict(
            run,
            id=33112740010,
            name=no_alias_title,
            display_title=no_alias_title,
            html_url="https://github.com/acme/widget/actions/runs/33112740010",
        )
        trusted_no_alias = helpers["trusted_publication_runs"](
            [no_alias_run], 270075259, repository, {tag_sha, control_sha}
        )
        self.assertEqual(
            trusted_no_alias[(tag, tag_sha)]["run_id"],
            33112740010,
        )

        release = [{
            "id": 101,
            "tag_name": tag,
            "draft": False,
            "prerelease": False,
            "immutable": True,
            "published_at": "2026-08-27T15:05:36Z",
        }]
        helpers["git_text"] = lambda *_arguments: tag_sha
        self.assertEqual(
            helpers["published_release_anchors"](
                release, (0, 15, 0), {tag}, trusted
            )[0]["publication"]["control_sha"],
            control_sha,
        )
        self.assertEqual(
            helpers["published_release_anchors"](
                release, (0, 15, 0), {tag}, {}
            ),
            [],
        )
        postdated = [dict(
            release[0],
            published_at="2026-08-30T15:05:36Z",
        )]
        self.assertEqual(
            helpers["published_release_anchors"](
                postdated, (0, 15, 0), {tag}, trusted
            ),
            [],
        )

        unmerged = dict(run, head_sha="3" * 40)
        with self.assertRaisesRegex(SystemExit, "untrusted publication-run"):
            helpers["trusted_publication_runs"](
                [unmerged], 270075259, repository, {tag_sha, control_sha}
            )

    def test_snapshot_rejects_calendar_invalid_github_timestamps(self):
        helpers = _review_state_runner()

        for value in (
            "2026-02-31T12:00:00Z",
            "2026-08-27T25:00:00Z",
            "2026-08-27T15:05:60Z",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                SystemExit, "malformed timestamp"
            ):
                helpers["timestamp_key"](value)

        self.assertLess(
            helpers["timestamp_key"]("2026-08-27T15:05:36.1Z"),
            helpers["timestamp_key"]("2026-08-27T15:05:36.2Z"),
        )

    def test_snapshot_publication_run_pagination_is_complete_and_unique(self):
        helpers = _review_state_runner()
        first_page = [{"id": item} for item in range(1, 101)]
        second_page = [{"id": 101}]

        def complete(endpoint):
            page = 2 if endpoint.endswith("page=2") else 1
            return {
                "total_count": 101,
                "workflow_runs": first_page if page == 1 else second_page,
            }

        helpers["rest"] = complete
        records = helpers["workflow_run_records"]("runs?per_page=100")
        self.assertEqual([record["id"] for record in records], list(range(1, 102)))

        def duplicate(endpoint):
            page = 2 if endpoint.endswith("page=2") else 1
            return {
                "total_count": 101,
                "workflow_runs": first_page if page == 1 else [{"id": 100}],
            }

        helpers["rest"] = duplicate
        with self.assertRaisesRegex(SystemExit, "malformed publication-run"):
            helpers["workflow_run_records"]("runs?per_page=100")

        helpers["rest"] = lambda _endpoint: {
            "total_count": 2,
            "workflow_runs": [{"id": 1}],
        }
        with self.assertRaisesRegex(SystemExit, "pagination was incomplete"):
            helpers["workflow_run_records"]("runs?per_page=100")

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
        for setting in (
            "GIT_NO_LAZY_FETCH=1",
            "GIT_NO_REPLACE_OBJECTS=1",
            "GIT_OPTIONAL_LOCKS=0",
            "GIT_TERMINAL_PROMPT=0",
            "GH_HOST=github.com",
        ):
            self.assertIn(setting, source)
        self.assertIn(
            '"$python_command" -I - "$release_tag" "$published_releases_file"',
            source,
        )
        self.assertIn(
            '"repos/${GITHUB_REPOSITORY}/releases?per_page=100"',
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
  for-each-ref) : ;;
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
  *repos/acme/widget/releases?per_page=100*) : ;;
  *repos/acme/widget/actions/workflows/publish.yml/runs*) : ;;
  *repos/acme/widget/actions/workflows/publish.yml*)
    printf '%s\\n' '270075259\tPromote verified release\t.github/workflows/publish.yml\tactive'
    ;;
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
