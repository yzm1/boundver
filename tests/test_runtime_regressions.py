"""Focused regressions for bounded hashing, Git selection, and metadata diffs."""

from __future__ import annotations

import importlib.util
import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import boundver.core as core
from boundver._config import (
    config_warnings,
    discover_components,
    parse_config_text,
    validate_config,
)
from boundver._diff import diff_lockfiles
from boundver._git import (
    GitSourceSnapshot,
    _capture_git_source_snapshot,
    _git_batch_cat,
    _iter_git_blobs,
    changed_components_since_ref,
    dirty_component_paths,
)
from boundver._hashing import (
    _read_path_content,
    canonical_json,
    source_tree_digest,
)
from boundver._output import (
    analyze_component_drift,
    analyze_explain_changes,
    why_component,
)
from boundver.providers import _parse_yaml_strict
from boundver.versions import _load_yaml_with_bounded_integers
from boundver._utils import (
    ConfigError,
    GuardrailError,
    ProviderError,
    MAX_GLOB_MATCH_STEPS,
    MAX_GLOB_METACHARACTERS_PER_SEGMENT,
    MAX_GLOB_SEGMENTS,
    MAX_YAML_INTEGER_CHARACTERS,
    _bounded_int_to_decimal,
    _bounded_exception_text,
    _bounded_json_dumps,
    _bounded_json_int,
    _bounded_yaml_int,
    _match_path_glob,
    _match_text_glob,
    _normalize_declared_path,
)
from tests import conftest as test_conftest
from tests._repo_fixtures import commit_all as _commit_all
from tests._repo_fixtures import init_git_repo as _init_repo


class WindowsTemporaryDirectoryHarnessTests(unittest.TestCase):
    def test_transient_windows_sharing_violation_is_retried(self):
        directory = object.__new__(
            test_conftest._WindowsRetryingTemporaryDirectory
        )
        transient = PermissionError(13, "directory is temporarily busy")
        transient.winerror = 32
        with patch.object(
            test_conftest._BASE_TEMPORARY_DIRECTORY,
            "cleanup",
            side_effect=(transient, None),
        ) as cleanup, patch.object(test_conftest.time, "sleep") as sleep:
            directory.cleanup()

        self.assertEqual(cleanup.call_count, 2)
        sleep.assert_called_once_with(0.01)

    def test_non_sharing_permission_error_is_not_retried(self):
        directory = object.__new__(
            test_conftest._WindowsRetryingTemporaryDirectory
        )
        denied = PermissionError(13, "not a transient sharing violation")
        denied.winerror = 123
        with patch.object(
            test_conftest._BASE_TEMPORARY_DIRECTORY,
            "cleanup",
            side_effect=denied,
        ) as cleanup, patch.object(test_conftest.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                directory.cleanup()

        cleanup.assert_called_once_with()
        sleep.assert_not_called()

    def test_persistent_windows_sharing_violation_still_fails(self):
        directory = object.__new__(
            test_conftest._WindowsRetryingTemporaryDirectory
        )
        persistent = PermissionError(13, "directory remains busy")
        persistent.winerror = 32
        with patch.object(
            test_conftest._BASE_TEMPORARY_DIRECTORY,
            "cleanup",
            side_effect=persistent,
        ) as cleanup, patch.object(test_conftest.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                directory.cleanup()

        self.assertEqual(
            cleanup.call_count,
            len(directory._CLEANUP_DELAYS) + 1,
        )
        self.assertEqual(sleep.call_count, len(directory._CLEANUP_DELAYS))


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


class RuntimeBenchmarkHarnessTests(unittest.TestCase):
    @staticmethod
    def _load_benchmark():
        path = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_runtime.py"
        spec = importlib.util.spec_from_file_location(
            "boundver_runtime_benchmark_test",
            path,
        )
        if spec is None or spec.loader is None:
            raise AssertionError("cannot load runtime benchmark")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_fixture_initialization_precedes_worktree_bound_git_commands(self):
        benchmark = self._load_benchmark()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            benchmark._init_repository(root)

            self.assertTrue((root / ".git").is_dir())

    def test_process_attribution_skips_hardening_options(self):
        benchmark = self._load_benchmark()

        self.assertEqual(
            benchmark._git_command_name(
                [
                    "git",
                    "-C",
                    "repo",
                    "--work-tree=repo",
                    "status",
                    "--porcelain=v1",
                ]
            ),
            "status",
        )
        self.assertEqual(
            benchmark._git_command_name(
                ["git", "-C", "repo", "--work-tree=repo", "--version"]
            ),
            "--version",
        )


class IntegerRuntimeLimitTests(unittest.TestCase):
    @unittest.skipUnless(
        hasattr(sys, "set_int_max_str_digits"),
        "runtime has no configurable integer conversion limit",
    )
    def test_parse_and_canonical_render_ignore_lower_runtime_limit(self):
        previous = sys.get_int_max_str_digits()
        digits = "9" * 1000
        try:
            sys.set_int_max_str_digits(640)
            parsed = _bounded_json_int(digits)
            self.assertEqual(_bounded_int_to_decimal(parsed), digits)
            self.assertEqual(
                canonical_json({"value": parsed}),
                '{"value":' + digits + "}",
            )
        finally:
            sys.set_int_max_str_digits(previous)

    def test_yaml_integer_parser_accepts_only_json_decimal_syntax(self):
        cases = {"0": 0, "1234": 1234, "-10": -10}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(_bounded_yaml_int(value), expected)

        for value in ("+1", "1_234", "0b10", "0x10", "012", "1:20"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "JSON decimal syntax"):
                    _bounded_yaml_int(value)

        with self.assertRaisesRegex(ValueError, "safety limit"):
            _bounded_yaml_int("0" * (MAX_YAML_INTEGER_CHARACTERS + 1))

    def test_json_integer_parser_itself_enforces_json_grammar(self):
        for value in ("", "+1", "01", "-01", "1_000", "0x10"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Invalid JSON integer"):
                    _bounded_json_int(value)

    @unittest.skipUnless(
        hasattr(sys, "set_int_max_str_digits"),
        "runtime has no configurable integer conversion limit",
    )
    def test_pretty_json_render_ignores_lower_runtime_limit(self):
        previous = sys.get_int_max_str_digits()
        digits = "8" * 1000
        try:
            sys.set_int_max_str_digits(640)
            value = _bounded_json_int(digits)
            rendered = _bounded_json_dumps({"value": value}, indent=2)
        finally:
            sys.set_int_max_str_digits(previous)

        self.assertIn(digits, rendered)

    def test_json_render_rejects_integer_beyond_contract(self):
        value = 10 ** 4300
        with self.assertRaisesRegex(ValueError, "4300-decimal-digit limit"):
            _bounded_json_dumps({"value": value})

    def test_bounded_json_renderer_matches_public_encoder_contract(self):
        cases = [
            (
                {"z": [1, True, None, 1.25], "a": "rocket 🚀"},
                {},
            ),
            (
                {"z": [1, {"b": 2}], "a": "value"},
                {"indent": 2, "sort_keys": True},
            ),
            (
                {"items": (1, 2), "text": "é"},
                {
                    "ensure_ascii": False,
                    "sort_keys": True,
                    "separators": (",", ":"),
                },
            ),
            (
                {1: "integer", None: "null", False: "boolean"},
                {},
            ),
            (
                {"path": Path("value")},
                {"default": str},
            ),
        ]
        for value, options in cases:
            with self.subTest(options=options):
                self.assertEqual(
                    _bounded_json_dumps(value, **options),
                    json.dumps(value, **options),
                )

        circular = []
        circular.append(circular)
        with self.assertRaisesRegex(ValueError, "Circular reference"):
            _bounded_json_dumps(circular)
        with self.assertRaises(ValueError):
            _bounded_json_dumps(float("nan"), allow_nan=False)

    @unittest.skipUnless(
        hasattr(sys, "set_int_max_str_digits"),
        "runtime has no configurable integer conversion limit",
    )
    def test_bounded_json_renderer_handles_large_integer_keys(self):
        previous = sys.get_int_max_str_digits()
        digits = "5" * 1000
        try:
            sys.set_int_max_str_digits(640)
            value = _bounded_json_int(digits)
            rendered = _bounded_json_dumps({value: value})
        finally:
            sys.set_int_max_str_digits(previous)

        self.assertEqual(rendered, '{"' + digits + '": ' + digits + "}")

    @unittest.skipUnless(
        hasattr(sys, "set_int_max_str_digits"),
        "runtime has no configurable integer conversion limit",
    )
    def test_partial_lock_copy_uses_setting_independent_serializer(self):
        from boundver._lockfile import (
            generate_lockfile,
            generate_lockfile_for_components,
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("value = 1\n", encoding="utf-8")
            _commit_all(root)
            config = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit", "paths": []},
                    }
                },
                "slices": {},
            }
            existing = generate_lockfile(config, root, source="head")
            digits = "7" * 1000
            existing["components"]["svc"]["boundary_metadata"] = {
                "large": _bounded_json_int(digits)
            }

            previous = sys.get_int_max_str_digits()
            try:
                sys.set_int_max_str_digits(640)
                updated = generate_lockfile_for_components(
                    config,
                    root,
                    ["svc"],
                    root / "boundary.lock.json",
                    source="head",
                    existing_lockfile=existing,
                )
            finally:
                sys.set_int_max_str_digits(previous)

            self.assertEqual(updated["project"], "p")

    @unittest.skipUnless(
        hasattr(sys, "set_int_max_str_digits"),
        "runtime has no configurable integer conversion limit",
    )
    def test_cli_config_rewrite_uses_setting_independent_serializer(self):
        digits = "6" * 1000
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "svc").mkdir()
            (root / "extra").mkdir()
            config_text = (
                '{"project":"p","components":{"svc":{"path":"svc",'
                '"boundary":{"provider":"implicit","paths":[],"options":'
                '{"large":' + digits + '}}}},"slices":{}}\n'
            )
            (root / "boundary.config.json").write_text(
                config_text, encoding="utf-8"
            )

            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            env["PYTHONINTMAXSTRDIGITS"] = "640"
            result = subprocess.run(
                [sys.executable, "-m", "boundver", "add", "extra", "extra"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            rewritten = (root / "boundary.config.json").read_text(encoding="utf-8")
            self.assertIn(digits, rewritten)
            self.assertNotIn("Traceback", result.stderr)

    def test_explicit_yaml_octal_tag_is_rejected_across_parsers(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML is unavailable")

        tagged = "value: !!int 012\n"
        with self.assertRaisesRegex(ConfigError, "invalid YAML integer"):
            parse_config_text(tagged, Path("boundary.config.yaml"))
        with self.assertRaisesRegex(ProviderError, "invalid YAML integer"):
            _parse_yaml_strict(tagged, "openapi.yaml")
        with self.assertRaisesRegex(ValueError, "JSON decimal syntax"):
            _load_yaml_with_bounded_integers(tagged)

    def test_oversized_yaml_float_is_rejected_across_parsers(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML is unavailable")

        oversized = "value: 1." + ("0" * 4_400) + "\n"
        with self.assertRaisesRegex(ConfigError, "character limit"):
            parse_config_text(oversized, Path("boundary.config.yaml"))
        with self.assertRaisesRegex(ProviderError, "character limit"):
            _parse_yaml_strict(oversized, "openapi.yaml")
        with self.assertRaisesRegex(ValueError, "character limit"):
            _load_yaml_with_bounded_integers(oversized)

    def test_malformed_yaml_diagnostic_does_not_echo_source_content(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML is unavailable")

        secret = "SECRET_TOKEN_abc123"
        malformed = f"openapi: 3.1.0\npaths:\n  {secret}: [unclosed\n"
        with self.assertRaises(ProviderError) as raised:
            _parse_yaml_strict(malformed, "api.yaml")

        diagnostic = str(raised.exception)
        self.assertNotIn(secret, diagnostic)
        self.assertIn("ParserError", diagnostic)
        self.assertIn("line 3, column 24", diagnostic)

    def test_malformed_config_yaml_does_not_echo_source_content(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML is unavailable")

        secret = "SECRET_TOKEN_abc123"
        malformed = f"project: x\ncomponents: [{secret}\n"
        with self.assertRaises(ConfigError) as raised:
            parse_config_text(malformed, Path("boundary.config.yaml"))

        diagnostic = str(raised.exception)
        self.assertNotIn(secret, diagnostic)
        self.assertIn("ParserError", diagnostic)
        self.assertIn("line 2, column 13", diagnostic)

    def test_duplicate_yaml_key_diagnostic_is_bounded(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML is unavailable")

        key = "x" * 20_000
        with self.assertRaises(ConfigError) as raised:
            parse_config_text(
                f'? "{key}"\n: first\n? "{key}"\n: second\n',
                Path("boundary.config.yaml"),
            )

        diagnostic = str(raised.exception)
        self.assertIn("duplicate YAML mapping key", diagnostic)
        self.assertLess(len(diagnostic), 5_000)
        self.assertNotIn(key, diagnostic)


class GlobComplexityTests(unittest.TestCase):
    def test_deep_double_star_match_is_iterative(self):
        path = "/".join(["directory"] * (MAX_GLOB_SEGMENTS - 1) + ["target"])
        self.assertTrue(_match_path_glob(path, "**/target"))

    def test_path_beyond_segment_cap_fails_closed(self):
        path = "/".join(["directory"] * (MAX_GLOB_SEGMENTS + 1))
        with self.assertRaisesRegex(GuardrailError, "segment"):
            _match_path_glob(path, "**")

    def test_segment_matcher_accounts_for_former_backtracking_shape(self):
        consumed = 0

        def spend(amount: int) -> None:
            nonlocal consumed
            consumed += amount

        self.assertFalse(
            _match_text_glob(
                "a" * 4000,
                "*a*a*a*a*ab",
                _step_consumer=spend,
            )
        )
        self.assertGreater(consumed, 4000)
        self.assertLess(consumed, MAX_GLOB_MATCH_STEPS)

    def test_text_matcher_preserves_shell_character_class_semantics(self):
        cases = (
            ("a", "[a-c]", True),
            ("z", "[!a-c]", True),
            ("b", "[c-a]", False),
            ("c", "[a--c]", True),
            ("b", "[a--!]", True),
            ("!", "[a--!]", True),
            ("b", "[!c-a]", True),
            ("!", "[a-!!-a]", True),
            ("a", "[a-!!-a]", False),
            ("-", "[a-!!-a]", False),
            ("-", "[a-b-c]", True),
            ("]", "[]]", True),
            ("[", "[[]", True),
            ("[x", "[x", True),
        )
        for candidate, pattern, expected in cases:
            with self.subTest(candidate=candidate, pattern=pattern):
                self.assertEqual(_match_text_glob(candidate, pattern), expected)

    def test_glob_metacharacter_cap_is_enforced_during_path_validation(self):
        pattern = "?" * (MAX_GLOB_METACHARACTERS_PER_SEGMENT + 1)
        with self.assertRaisesRegex(ValueError, "wildcard metacharacters"):
            _normalize_declared_path(pattern)


class DiscoveryStreamingTests(unittest.TestCase):
    def test_index_backed_discovery_ignores_unstaged_manifest_deletion(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "pkg").mkdir()
            (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                '[project]\nname = "example"\nversion = "1.0.0"\n',
                encoding="utf-8",
            )
            _commit_all(root)
            (root / "pyproject.toml").unlink()

            discovered = discover_components(root)

            self.assertEqual(len(discovered), 1)
            component = next(iter(discovered.values()))
            self.assertEqual(component["path"], "pkg")
            self.assertEqual(
                component["boundary"],
                {"provider": "python-exports", "paths": ["__init__.py"]},
            )

    def test_git_listing_guardrail_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            with patch(
                "boundver._discovery._iter_bounded_git_paths",
                side_effect=GuardrailError("bounded listing exceeded"),
            ):
                with self.assertRaisesRegex(GuardrailError, "bounded listing"):
                    discover_components(root)

    def test_cli_reports_listing_guardrail_without_traceback(self):
        stderr = io.StringIO()
        args = type("Args", (), {"format": "json"})()
        with patch.object(
            core,
            "discover_components",
            side_effect=GuardrailError("bounded listing exceeded"),
        ), patch.object(sys, "stderr", stderr):
            with self.assertRaises(SystemExit) as raised:
                core._cmd_discover(args, Path("."))

        self.assertEqual(raised.exception.code, core.EXIT_USAGE)
        self.assertIn("component discovery failed", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


class ConfigExpansionGuardrailTests(unittest.TestCase):
    def _config(self) -> dict:
        return {
            "project": "example",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "json-file", "paths": ["api.json"]},
                    "behavior": {"paths": ["api.json"]},
                }
            },
            "slices": {},
        }

    def test_validation_reports_expansion_guardrail(self):
        with patch(
            "boundver._config._expand_component_paths",
            side_effect=GuardrailError("bounded expansion exceeded"),
        ):
            errors = validate_config(self._config(), Path("."))

        self.assertTrue(
            any("path expansion could not be validated" in error for error in errors)
        )

    def test_warnings_report_expansion_guardrail(self):
        with patch(
            "boundver._config._expand_component_paths",
            side_effect=GuardrailError("bounded expansion exceeded"),
        ):
            warnings = config_warnings(self._config(), Path("."))

        self.assertEqual(len(warnings), 1)
        self.assertIn("behavior coverage could not be inspected", warnings[0])


class GitBlobStreamingTests(unittest.TestCase):
    def test_duplicate_object_ids_are_requested_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "a.txt").write_bytes(b"identical")
            (root / "b.txt").write_bytes(b"identical")
            _commit_all(root)
            oid = _git(root, "rev-parse", "HEAD:a.txt").stdout.strip()

            streamed = list(_iter_git_blobs(root, [oid, oid, oid]))

            self.assertEqual(streamed, [(oid, b"identical")])

    def test_compatibility_collector_has_aggregate_limit(self):
        fake_stream = iter([("a", b"123"), ("b", b"456")])
        with patch("boundver._git.MAX_GIT_BATCH_BYTES", 5):
            with patch("boundver._git._iter_git_blobs", return_value=fake_stream):
                with self.assertRaisesRegex(GuardrailError, "aggregate"):
                    _git_batch_cat(Path("."), ["a", "b"])

    def test_tree_hash_counts_duplicate_blob_bytes_logically(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "a.txt").write_bytes(b"abc")
            (root / "svc" / "b.txt").write_bytes(b"abc")
            _commit_all(root)

            with patch("boundver._hashing.MAX_HASH_TOTAL_BYTES", 5):
                with self.assertRaisesRegex(GuardrailError, "aggregate"):
                    source_tree_digest(root, "svc", source="head")


class ChangedPathSelectionTests(unittest.TestCase):
    def _config(self) -> dict:
        return {
            "components": {
                "a": {"path": "a"},
                "b": {"path": "b"},
                "c": {"path": "c"},
            }
        }

    def test_selected_source_controls_changed_components(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "a").mkdir()
            (root / "b").mkdir()
            (root / "c").mkdir()
            (root / "a" / "value.txt").write_text("base\n", encoding="utf-8")
            (root / "b" / "value.txt").write_text("base\n", encoding="utf-8")
            _commit_all(root)

            (root / "a" / "value.txt").write_text("staged\n", encoding="utf-8")
            _git(root, "add", "a/value.txt")
            (root / "b" / "value.txt").write_text("unstaged\n", encoding="utf-8")

            self.assertEqual(
                changed_components_since_ref(
                    self._config(), root, "HEAD", source="head"
                ),
                [],
            )
            self.assertEqual(
                changed_components_since_ref(
                    self._config(), root, "HEAD", source="index"
                ),
                ["a"],
            )
            self.assertEqual(
                changed_components_since_ref(
                    self._config(), root, "HEAD", source="working-tree"
                ),
                ["a", "b"],
            )

    def test_index_snapshot_is_stable_and_rename_marks_both_components(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "a").mkdir()
            (root / "b").mkdir()
            (root / "c").mkdir()
            (root / "a" / "value.txt").write_text("value\n", encoding="utf-8")
            (root / "b" / "keep.txt").write_text("keep\n", encoding="utf-8")
            (root / "c" / "keep.txt").write_text("keep\n", encoding="utf-8")
            _commit_all(root)

            _git(root, "mv", "a/value.txt", "b/value.txt")
            snapshot = _capture_git_source_snapshot(root, "index")
            self.assertEqual(
                changed_components_since_ref(
                    self._config(),
                    root,
                    "HEAD",
                    source="index",
                    snapshot=snapshot,
                ),
                ["a", "b"],
            )

            (root / "c" / "keep.txt").write_text("later\n", encoding="utf-8")
            _git(root, "add", "c/keep.txt")
            self.assertEqual(
                changed_components_since_ref(
                    self._config(),
                    root,
                    "HEAD",
                    source="index",
                    snapshot=snapshot,
                ),
                ["a", "b"],
            )

    def test_root_component_is_dirty_for_any_repository_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "root.txt").write_text("base\n", encoding="utf-8")
            _commit_all(root)
            (root / "root.txt").write_text("dirty\n", encoding="utf-8")

            self.assertEqual(dirty_component_paths(root, [".", "svc"]), ["."])


@unittest.skipIf(os.name == "nt", "arbitrary-byte symlink targets require POSIX")
class PosixSymlinkByteTests(unittest.TestCase):
    def test_arbitrary_target_bytes_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_link = os.fsencode(root) + b"/link"
            os.symlink(b"invalid-\xff", raw_link)
            self.assertEqual(
                _read_path_content(root, root / "link", "working-tree"),
                b"invalid-\xff",
            )


class DiffMetadataTests(unittest.TestCase):
    def test_top_level_and_slice_metadata_changes_are_visible(self):
        old = {
            "project": "old-project",
            "config_digest": "a" * 64,
            "components": {},
            "slices": {
                "public": {
                    "description": "old",
                    "mode": "exact",
                    "components": ["a"],
                    "component_digests": {"a": "same"},
                    "fingerprint": "same",
                }
            },
        }
        new = {
            "project": "new-project",
            "config_digest": "b" * 64,
            "components": {},
            "slices": {
                "public": {
                    "description": "new",
                    "mode": "boundary",
                    "components": ["a"],
                    "component_digests": {"a": "same"},
                    "fingerprint": "same",
                }
            },
        }

        result = diff_lockfiles(old, new)

        self.assertEqual(
            set(result["changed_metadata"]), {"project", "config_digest"}
        )
        self.assertEqual(result["slices"]["unchanged"], [])
        changed = result["slices"]["changed"][0]
        self.assertEqual(changed["old"], "same")
        self.assertEqual(changed["new"], "same")
        self.assertEqual(
            set(changed["changed_metadata"]), {"description", "mode"}
        )


class DiagnosticGuardrailTests(unittest.TestCase):
    @staticmethod
    def _config() -> dict:
        return {
            "project": "example",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "leaf", "paths": []},
                }
            },
            "slices": {},
        }

    def test_exported_explain_captures_one_index_snapshot(self):
        snapshot = GitSourceSnapshot(
            source="index",
            tree_oid="captured-tree",
            entries={},
            head_oid="captured-head",
        )
        with patch(
            "boundver._output._capture_git_source_snapshot",
            return_value=snapshot,
        ) as capture:
            with patch(
                "boundver._output._git_name_status", return_value=[]
            ) as diff:
                result = analyze_explain_changes(
                    self._config(), Path("."), "svc", source="index"
                )

        self.assertIsNone(result["error"])
        capture.assert_called_once_with(Path("."), "index")
        self.assertEqual(
            diff.call_args.args[1],
            [
                "--literal-pathspecs",
                "diff",
                "--name-status",
                "-z",
                "captured-head",
                "captured-tree",
                "--",
                "svc",
            ],
        )

    def test_exported_explain_reports_snapshot_capture_failure(self):
        with patch(
            "boundver._output._capture_git_source_snapshot",
            side_effect=OSError("git unavailable"),
        ):
            result = analyze_explain_changes(
                self._config(), Path("."), "svc", source="head"
            )

        self.assertEqual(
            result["error"], "cannot capture head source: git unavailable"
        )

    def test_exception_diagnostics_are_bounded_and_tolerate_broken_stringification(self):
        oversized = _bounded_exception_text(ValueError("x" * 10_000))
        self.assertEqual(len(oversized), 4_096)
        self.assertTrue(oversized.endswith("..."))

        class BrokenError(Exception):
            def __str__(self) -> str:
                raise RuntimeError("broken formatter")

        self.assertEqual(_bounded_exception_text(BrokenError()), "BrokenError")

    def test_exported_explain_bounds_snapshot_capture_failure(self):
        with patch(
            "boundver._output._capture_git_source_snapshot",
            side_effect=OSError("x" * 10_000),
        ):
            result = analyze_explain_changes(
                self._config(), Path("."), "svc", source="head"
            )

        self.assertTrue(result["error"].startswith("cannot capture head source: "))
        self.assertLessEqual(len(result["error"]), 4_128)
        self.assertTrue(result["error"].endswith("..."))

    def test_explain_returns_a_controlled_error_for_bounded_git_diff_failure(self):
        config = {
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "leaf", "paths": []},
                }
            }
        }
        with patch(
            "boundver._output._git_name_status",
            side_effect=GuardrailError("Git status paths exceed the limit"),
        ):
            result = analyze_explain_changes(
                config,
                Path("."),
                "svc",
                source="working-tree",
            )

        self.assertIn("failed to diff 'svc'", result["error"])
        self.assertIn("Git status paths exceed the limit", result["error"])

    def test_why_analysis_does_not_traceback_when_git_diagnostics_hit_guardrail(self):
        config = self._config()
        locked_component = {
            "fingerprints": {
                "exact": "a" * 64,
                "behavior": None,
                "boundary": None,
                "compat": None,
            }
        }
        current_component = {
            "fingerprints": {
                "exact": "b" * 64,
                "behavior": None,
                "boundary": None,
                "compat": None,
            }
        }
        with patch(
            "boundver._lockfile.generate_lockfile",
            return_value={"components": {"svc": current_component}},
        ):
            with patch(
                "boundver._output._git_name_status",
                side_effect=GuardrailError("Git status paths exceed the limit"),
            ):
                result = analyze_component_drift(
                    config,
                    {"components": {"svc": locked_component}},
                    Path("."),
                    "svc",
                    source="working-tree",
                )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["changed_files"], [])
        self.assertEqual(result["changed_files_status"], "error")
        self.assertIn(
            "Git status paths exceed the limit",
            result["changed_files_error"],
        )
        self.assertIn("exact", result["changes"])

        stdout = io.StringIO()
        with patch(
            "boundver._output.analyze_component_drift", return_value=result
        ), redirect_stdout(stdout):
            exit_code = why_component(
                config,
                {"components": {"svc": locked_component}},
                Path("."),
                "svc",
                source="working-tree",
            )

        rendered = stdout.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertIn("Changed-file diagnostics failed", rendered)
        self.assertNotIn("Drift is from", rendered)
        self.assertNotIn("No uncommitted changes", rendered)


class RegistryParityTests(unittest.TestCase):
    def test_config_validation_derives_builtin_names_from_provider_registry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "svc").mkdir()
            config = {
                "project": "example",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "future-built-in",
                            "paths": [],
                        },
                    }
                },
                "slices": {},
            }
            with patch(
                "boundver._config.create_registry",
                return_value={"future-built-in": None},
            ):
                errors = validate_config(config, root, source="working-tree")

        self.assertFalse(
            any("unsupported boundary.provider" in error for error in errors),
            errors,
        )


if __name__ == "__main__":
    unittest.main()
