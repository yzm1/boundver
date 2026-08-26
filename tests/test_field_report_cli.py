"""Regression tests derived from production Boundver migration field reports."""

import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import boundver
from boundver import core
from tests._repo_fixtures import commit_all, init_git_repo


def _run_main(*args: str, repo_root: Path = None):
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = 0
    with patch.object(sys, "argv", ["boundver", *args]):
        root_patch = (
            patch("boundver.core.git_root", return_value=repo_root)
            if repo_root is not None
            else patch("boundver.core.git_root", wraps=core.git_root)
        )
        with root_patch, redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                core.main()
            except SystemExit as exc:
                code = int(exc.code or 0)
    return code, stdout.getvalue(), stderr.getvalue()


def _component(path: str) -> dict:
    return {
        "path": path,
        "version_source": None,
        "boundary": {"provider": "implicit", "paths": []},
    }


class CrossSchemaDiffTests(unittest.TestCase):
    def _lock(self, schema: str) -> dict:
        return {
            "schema": schema,
            "project": "p",
            "components": {},
            "slices": {},
        }

    def test_cross_schema_diff_reports_one_regeneration_diagnostic(self):
        with tempfile.TemporaryDirectory() as td:
            old = Path(td, "old.json")
            new = Path(td, "new.json")
            old.write_text(json.dumps(self._lock("boundary-lock/v2")))
            new.write_text(json.dumps(self._lock("boundary-lock/v3")))

            code, out, err = _run_main("diff", str(old), str(new))

        self.assertEqual(code, core.EXIT_USAGE)
        self.assertEqual(out, "")
        self.assertEqual(err.count("ERROR:"), 1)
        self.assertIn("incompatible schemas", err)
        self.assertIn("regenerate both", err)
        self.assertNotIn("malformed", err)

    def test_same_legacy_schema_diff_reports_one_unsupported_diagnostic(self):
        with tempfile.TemporaryDirectory() as td:
            old = Path(td, "old.json")
            new = Path(td, "new.json")
            old.write_text(json.dumps(self._lock("boundary-lock/v2")))
            new.write_text(json.dumps(self._lock("boundary-lock/v2")))

            code, _, err = _run_main("diff", str(old), str(new))

            with self.assertRaisesRegex(boundver.LockfileError, "unsupported schema"):
                boundver.diff(str(old), str(new))

        self.assertEqual(code, core.EXIT_USAGE)
        self.assertEqual(err.count("ERROR:"), 1)
        self.assertIn("unsupported schema", err)
        self.assertNotIn("malformed", err)

    def test_schema_diagnostic_is_bounded_for_oversized_values(self):
        with tempfile.TemporaryDirectory() as td:
            old = Path(td, "old.json")
            new = Path(td, "new.json")
            old.write_text(json.dumps(self._lock("old-" + "x" * 100_000)))
            new.write_text(json.dumps(self._lock("new-" + "y" * 100_000)))

            code, out, err = _run_main("diff", str(old), str(new))

        self.assertEqual(code, core.EXIT_USAGE)
        self.assertEqual(out, "")
        self.assertEqual(err.count("ERROR:"), 1)
        self.assertLess(len(err), 10_000)
        self.assertIn("incompatible schemas", err)


class SemanticContractMigrationReviewTests(unittest.TestCase):
    def _locks(self, root: Path):
        init_git_repo(root)
        (root / "svc").mkdir()
        (root / "svc" / "api.json").write_text('{"contract": 1}\n')
        config = {
            "project": "migration-review",
            "components": {
                "svc": {
                    "path": "svc",
                    "version_source": None,
                    "boundary": {
                        "provider": "path-hash",
                        "paths": ["api.json"],
                    },
                }
            },
            "slices": {
                "all": {
                    "description": "all components",
                    "mode": "exact",
                    "components": ["svc"],
                }
            },
        }
        (root / "boundary.config.json").write_text(json.dumps(config))
        commit_all(root, "migration review fixture")
        current = core.generate_lockfile(config, root, source="head")
        legacy = copy.deepcopy(current)
        legacy["config_contract"] = "boundver-semantic-config/v1"
        legacy["config_digest"] = "1" * 64
        legacy["components"]["svc"]["boundary_provider_version"] = "2"
        return config, legacy, current

    def test_v1_to_v2_diff_is_read_only_and_reports_the_contract_transition(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, legacy, current = self._locks(root)
            old = root / "old.lock.json"
            new = root / "new.lock.json"
            old.write_text(json.dumps(legacy))
            new.write_text(json.dumps(current))
            before = (old.read_bytes(), new.read_bytes())

            code, out, err = _run_main(
                "diff", str(old), str(new), "--format", "json"
            )
            library_result = boundver.diff(str(old), str(new))

            self.assertEqual(code, core.EXIT_OK, err)
            self.assertEqual(err, "")
            result = json.loads(out)
            self.assertEqual(result, library_result)
            self.assertEqual(
                result["changed_metadata"]["config_contract"],
                {
                    "old": "boundver-semantic-config/v1",
                    "new": "boundver-semantic-config/v2",
                },
            )
            component = result["components"]["changed"][0]
            self.assertEqual(component["changed_facets"], {})
            self.assertIn(
                "boundary_provider_version", component["changed_metadata"]
            )
            self.assertEqual(result["slices"]["unchanged"], ["all"])
            self.assertEqual((old.read_bytes(), new.read_bytes()), before)

    def test_diff_does_not_open_an_unreviewed_semantic_contract(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, legacy, current = self._locks(root)
            legacy["config_contract"] = "boundver-semantic-config/v0"
            old = root / "old.lock.json"
            new = root / "new.lock.json"
            old.write_text(json.dumps(legacy))
            new.write_text(json.dumps(current))

            code, out, err = _run_main("diff", str(old), str(new))

        self.assertEqual(code, core.EXIT_USAGE)
        self.assertEqual(out, "")
        self.assertIn("unsupported for this read-only comparison", err)
        self.assertIn("boundver-semantic-config/v1", err)
        self.assertIn("boundver-semantic-config/v2", err)
        self.assertNotIn("LOCKFILE malformed", err)

    def test_two_canonical_v1_locks_remain_diffable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, legacy, _ = self._locks(root)
            newer_legacy = copy.deepcopy(legacy)
            newer_legacy["config_digest"] = "2" * 64
            old = root / "old.lock.json"
            new = root / "new.lock.json"
            old.write_text(json.dumps(legacy))
            new.write_text(json.dumps(newer_legacy))

            code, out, err = _run_main(
                "diff", str(old), str(new), "--format", "json"
            )

        self.assertEqual(code, core.EXIT_OK, err)
        self.assertEqual(err, "")
        self.assertEqual(
            json.loads(out)["changed_metadata"]["config_digest"],
            {"old": "1" * 64, "new": "2" * 64},
        )

    def test_diff_rejects_opaque_legacy_extension_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, legacy, current = self._locks(root)
            legacy["components"]["svc"]["x-provider-state"] = "old"
            old = root / "old.lock.json"
            new = root / "new.lock.json"
            old.write_text(json.dumps(legacy))
            new.write_text(json.dumps(current))

            code, out, err = _run_main("diff", str(old), str(new))

        self.assertEqual(code, core.EXIT_USAGE)
        self.assertEqual(out, "")
        self.assertIn("unknown field", err)
        self.assertIn("x-provider-state", err)

    def test_diff_rejects_non_string_published_schema_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, legacy, current = self._locks(root)
            legacy["$schema"] = {"opaque": "old"}
            old = root / "old.lock.json"
            new = root / "new.lock.json"
            old.write_text(json.dumps(legacy))
            new.write_text(json.dumps(current))

            code, out, err = _run_main("diff", str(old), str(new))
            with self.assertRaisesRegex(boundver.LockfileError, "\\$schema"):
                boundver.diff(str(old), str(new))

        self.assertEqual(code, core.EXIT_USAGE)
        self.assertEqual(out, "")
        self.assertIn("LOCKFILE malformed", err)
        self.assertIn("$schema must be a string", err)

    def test_malformed_contract_types_fail_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, legacy, current = self._locks(root)
            old = root / "old.lock.json"
            new = root / "new.lock.json"
            new.write_text(json.dumps(current))

            for invalid in ([], {}, None):
                with self.subTest(invalid=invalid):
                    legacy["config_contract"] = invalid
                    old.write_text(json.dumps(legacy))
                    code, out, err = _run_main("diff", str(old), str(new))
                    self.assertEqual(code, core.EXIT_USAGE)
                    self.assertEqual(out, "")
                    self.assertIn("LOCKFILE malformed", err)
                    self.assertNotIn("Traceback", err)

            legacy.pop("config_contract")
            old.write_text(json.dumps(legacy))
            code, out, err = _run_main("diff", str(old), str(new))
            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("LOCKFILE malformed", err)
            self.assertNotIn("Traceback", err)

    def test_verify_names_the_version_mismatch_and_stays_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, legacy, _ = self._locks(root)
            lock_path = root / "boundary.lock.json"
            lock_path.write_text(json.dumps(legacy))
            before = lock_path.read_bytes()

            with patch("boundver.core._get_version", return_value="0.13.0"):
                code, out, err = _run_main(
                    "verify",
                    "--source",
                    "working-tree",
                    "--update",
                    repo_root=root,
                )

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("semantic configuration contract mismatch", err)
            self.assertIn("boundary-lock/v3", err)
            self.assertIn("boundver-semantic-config/v1", err)
            self.assertIn("boundver-semantic-config/v2", err)
            self.assertIn("boundver 0.13.0", err)
            self.assertIn("repository-pinned", err)
            self.assertIn("boundver generate", err)
            self.assertNotIn("LOCKFILE malformed", err)
            self.assertEqual(lock_path.read_bytes(), before)

    def test_component_generation_does_not_reuse_a_v1_index_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, legacy, _ = self._locks(root)
            lock_path = root / "boundary.lock.json"
            lock_path.write_text(json.dumps(legacy))
            commit_all(root, "record legacy lock")
            before = lock_path.read_bytes()

            with patch("boundver.core._get_version", return_value="0.13.0"):
                code, out, err = _run_main(
                    "generate",
                    "--components",
                    "svc",
                    "--source",
                    "index",
                    repo_root=root,
                )

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("semantic configuration contract mismatch", err)
            self.assertIn("boundver 0.13.0", err)
            self.assertEqual(lock_path.read_bytes(), before)


class IndexCaptureDiagnosticTests(unittest.TestCase):
    def test_cli_and_public_api_preserve_actionable_capture_detail(self):
        detail = (
            "Cannot capture index as a complete Git tree: git write-tree failed "
            "(return code 128; stderr='fatal: svc/api.json: unmerged')"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch(
                "boundver.core._capture_git_source_snapshot",
                side_effect=ValueError(detail),
            ):
                code, out, err = _run_main(
                    "generate", "--source", "index", repo_root=root
                )

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("git write-tree failed", err)
            self.assertIn("unmerged", err)
            self.assertNotIn("Traceback", err)

            with (
                patch("boundver._git.git_root", return_value=root),
                patch(
                    "boundver.core._capture_git_source_snapshot",
                    side_effect=ValueError(detail),
                ),
            ):
                with self.assertRaises(boundver.ConfigError) as raised:
                    boundver.generate(source="index", out_path=None)

            self.assertIn("git write-tree failed", str(raised.exception))
            self.assertIn("unmerged", str(raised.exception))

    def test_working_tree_validation_never_suppresses_index_capture_failure(self):
        config = {
            "project": "p",
            "components": {"svc": _component("svc")},
            "slices": {},
        }
        detail = (
            "Cannot capture index as a complete Git tree: git write-tree failed "
            "(return code 128; stderr='fatal: index file corrupt')"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with (
                patch(
                    "boundver._config._capture_git_source_snapshot",
                    side_effect=ValueError(detail),
                ),
                patch("boundver._config._is_git_repository", return_value=True),
            ):
                errors = core.validate_config(
                    config,
                    root,
                    source="working-tree",
                )

        tracking_errors = [
            error
            for error in errors
            if error.startswith("Working-tree Git tracking state cannot be read:")
        ]
        self.assertEqual(len(tracking_errors), 1, errors)
        self.assertIn("index file corrupt", tracking_errors[0])


class MigrationSelectorAnalysisTests(unittest.TestCase):
    def _analyze(self, config: dict, root: Path) -> dict:
        from boundver._migration_analysis import analyze_selector_migration

        return analyze_selector_migration(
            config,
            root,
            source="working-tree",
            snapshot=None,
            lock_path="old.lock.json",
            lock_schema="boundary-lock/v2",
            migration_action="regenerate",
            migration_reason="regeneration required",
        )

    def test_explain_compares_v010_whole_path_globs_without_writing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc" / "api" / "nested").mkdir(parents=True)
            (root / "svc" / "api" / "top.yaml").write_text("top\n")
            (root / "svc" / "api" / "nested" / "deep.yaml").write_text("deep\n")
            config = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "version_source": None,
                        "boundary": {
                            "provider": "openapi",
                            "paths": ["api/*.yaml"],
                        },
                        "behavior": {"paths": ["api/*.yaml"]},
                    }
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(config))
            commit_all(root)
            old_lock = {
                "schema": "boundary-lock/v2",
                "project": "p",
                "components": {},
                "slices": {},
            }
            lock_path = root / "old.lock.json"
            lock_path.write_text(json.dumps(old_lock))
            original = lock_path.read_bytes()

            code, out, err = _run_main(
                "migrate-lock",
                "--lock",
                str(lock_path),
                "--explain",
                "--config",
                "boundary.config.json",
                "--source",
                "head",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(code, 0, err)
            self.assertEqual(lock_path.read_bytes(), original)
            payload = json.loads(out)
            self.assertEqual(payload["schema"], "boundver-migration-analysis/v1")
            self.assertEqual(payload["lock"]["action"], "regenerate")
            self.assertEqual(payload["summary"]["declaration_count"], 2)
            self.assertEqual(payload["summary"]["changed_declaration_count"], 2)
            for declaration in payload["declarations"]:
                self.assertEqual(declaration["impact"], "narrowed")
                self.assertEqual(declaration["legacy_match_count"], 2)
                self.assertEqual(declaration["current_match_count"], 1)
                self.assertEqual(
                    declaration["legacy_only_examples"],
                    ["api/nested/deep.yaml"],
                )

            try:
                import jsonschema
            except ImportError:
                return
            schema = json.loads(
                (
                    Path(__file__).parents[1]
                    / "spec"
                    / "cli-output.migrate-lock.schema.json"
                ).read_text()
            )
            jsonschema.validate(payload, schema)

    def test_analysis_preserves_legacy_invalid_range_normalization(self):
        for pattern in ("[a--!]*", "[a-!!-a]*"):
            with self.subTest(pattern=pattern):
                config = {
                    "project": "p",
                    "components": {
                        "svc": {
                            "path": "svc",
                            "boundary": {
                                "provider": "openapi",
                                "paths": [pattern],
                            },
                        }
                    },
                    "slices": {},
                }
                with patch(
                    "boundver._migration_analysis._component_files",
                    return_value=["b", "nested/b"],
                ):
                    payload = self._analyze(config, Path("unused"))

                declaration = payload["declarations"][0]
                self.assertEqual(declaration["analysis_status"], "compared")
                self.assertEqual(declaration["impact"], "narrowed")
                self.assertEqual(declaration["legacy_match_count"], 2)
                self.assertEqual(declaration["current_match_count"], 1)
                self.assertEqual(
                    declaration["legacy_only_examples"], ["nested/b"]
                )

    def test_explain_working_tree_uses_filesystem_for_unborn_empty_index(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc" / "api" / "nested").mkdir(parents=True)
            (root / "svc" / "api" / "top.yaml").write_text("top\n")
            (root / "svc" / "api" / "nested" / "deep.yaml").write_text(
                "deep\n"
            )
            config = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "version_source": None,
                        "boundary": {
                            "provider": "openapi",
                            "paths": ["api/*.yaml"],
                        },
                    }
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(config))
            lock_path = root / "old.lock.json"
            lock_path.write_text(
                json.dumps(
                    {
                        "schema": "boundary-lock/v2",
                        "project": "p",
                        "components": {},
                        "slices": {},
                    }
                )
            )

            code, out, err = _run_main(
                "migrate-lock",
                "--lock",
                str(lock_path),
                "--explain",
                "--config",
                "boundary.config.json",
                "--source",
                "working-tree",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(code, 0, err)
            declaration = json.loads(out)["declarations"][0]
            self.assertEqual(declaration["impact"], "narrowed")
            self.assertEqual(declaration["legacy_match_count"], 2)
            self.assertEqual(declaration["current_match_count"], 1)
            self.assertEqual(
                declaration["legacy_only_examples"],
                ["api/nested/deep.yaml"],
            )

    def test_analysis_skips_file_listing_without_selectors(self):
        config = {
            "project": "p",
            "components": {"svc": _component("svc")},
            "slices": {},
        }
        with patch("boundver._migration_analysis._component_files") as list_files:
            payload = self._analyze(config, Path("unused"))

        list_files.assert_not_called()
        self.assertEqual(payload["summary"]["declaration_count"], 0)

    def test_analysis_reports_v010_whitespace_selector_as_current_rejected(self):
        config = {
            "project": "p",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {
                        "provider": "openapi",
                        "paths": [" api/*.yaml "],
                    },
                    "behavior": {"paths": [" api/*.yaml "]},
                }
            },
            "slices": {},
        }
        with patch("boundver._migration_analysis._component_files") as list_files:
            payload = self._analyze(config, Path("unused"))

        list_files.assert_not_called()
        self.assertEqual(payload["summary"]["declaration_count"], 2)
        self.assertEqual(payload["summary"]["compared_declaration_count"], 0)
        self.assertEqual(payload["summary"]["uncompared_declaration_count"], 2)
        for declaration in payload["declarations"]:
            self.assertEqual(declaration["selector"], " api/*.yaml ")
            self.assertEqual(declaration["selector_kind"], "glob")
            self.assertEqual(declaration["analysis_status"], "current-rejected")
            self.assertEqual(declaration["impact"], "not-comparable")
            self.assertIn("trimmed and evaluated 'api/*.yaml'", declaration["detail"])

        try:
            import jsonschema
        except ImportError:
            return
        schema = json.loads(
            (
                Path(__file__).parents[1]
                / "spec"
                / "cli-output.migrate-lock.schema.json"
            ).read_text()
        )
        jsonschema.validate(payload, schema)

    def test_analysis_uses_v010_trimmed_component_root(self):
        config = {
            "project": "p",
            "components": {
                "svc": {
                    "path": " svc ",
                    "boundary": {
                        "provider": "openapi",
                        "paths": ["api/*.yaml"],
                    },
                }
            },
            "slices": {},
        }
        with patch(
            "boundver._migration_analysis._component_files",
            return_value=["api/route.yaml"],
        ) as list_files:
            payload = self._analyze(config, Path("unused"))

        list_files.assert_called_once_with(
            Path("unused"),
            "svc",
            "working-tree",
            None,
            path_index=None,
            step_consumer=unittest.mock.ANY,
        )
        declaration = payload["declarations"][0]
        self.assertEqual(declaration["analysis_status"], "compared")
        self.assertEqual(declaration["impact"], "unchanged")

    def test_legacy_provider_rejection_precedes_current_path_rejection(self):
        config = {
            "project": "p",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {
                        "provider": "openapi-canonical",
                        "paths": [" api/*.yaml "],
                    },
                }
            },
            "slices": {},
        }
        payload = self._analyze(config, Path("unused"))

        declaration = payload["declarations"][0]
        self.assertEqual(declaration["analysis_status"], "legacy-rejected")
        self.assertIn("0.10 rejected glob selectors", declaration["detail"])

    def test_analysis_reports_trailing_slash_file_literal_as_broadened(self):
        config = {
            "project": "p",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {
                        "provider": "openapi",
                        "paths": ["api.yaml/"],
                    },
                }
            },
            "slices": {},
        }
        with patch(
            "boundver._migration_analysis._component_files",
            return_value=["api.yaml"],
        ):
            payload = self._analyze(config, Path("unused"))

        declaration = payload["declarations"][0]
        self.assertEqual(declaration["analysis_status"], "compared")
        self.assertEqual(declaration["impact"], "broadened")
        self.assertEqual(declaration["legacy_match_count"], 0)
        self.assertEqual(declaration["current_match_count"], 1)
        self.assertEqual(declaration["current_only_examples"], ["api.yaml"])

    def test_analysis_classifies_provider_specific_v010_semantics(self):
        config = {
            "project": "p",
            "components": {
                "canonical": {
                    "path": "canonical",
                    "boundary": {
                        "provider": "json-canonical",
                        "paths": ["api/*.json", "api/schema.json"],
                    },
                },
                "custom": {
                    "path": "custom",
                    "boundary": {
                        "provider": "c" * 4096,
                        "paths": ["api/*.json"],
                    },
                },
                "leaf": {
                    "path": "leaf",
                    "boundary": {"provider": "leaf", "paths": ["ignored.txt"]},
                },
                "raw": {
                    "path": "raw",
                    "boundary": {
                        "provider": "json-file",
                        "paths": ["api/*.json"],
                    },
                },
            },
            "slices": {},
        }
        files = ["api/schema.json", "api/nested/deep.json"]
        with patch(
            "boundver._migration_analysis._component_files", return_value=files
        ) as list_files:
            payload = self._analyze(config, Path("unused"))

        # Only components with at least one comparable declaration enumerate
        # their source paths. Leaf/custom-only declarations do no Git work.
        self.assertEqual(list_files.call_count, 2)
        declarations = {
            (item["component"], item["selector"]): item
            for item in payload["declarations"]
        }
        self.assertEqual(
            declarations[("canonical", "api/*.json")]["analysis_status"],
            "legacy-rejected",
        )
        self.assertIsNone(
            declarations[("canonical", "api/*.json")]["legacy_match_count"]
        )
        self.assertEqual(
            declarations[("canonical", "api/schema.json")]["analysis_status"],
            "compared",
        )
        self.assertEqual(
            declarations[("leaf", "ignored.txt")]["analysis_status"],
            "not-applicable",
        )
        self.assertEqual(
            declarations[("custom", "api/*.json")]["analysis_status"],
            "provider-specific",
        )
        self.assertEqual(len(declarations[("custom", "api/*.json")]["detail"]), 4096)
        self.assertEqual(payload["summary"]["declaration_count"], 5)
        self.assertEqual(payload["summary"]["compared_declaration_count"], 2)
        self.assertEqual(payload["summary"]["uncompared_declaration_count"], 3)

        try:
            import jsonschema
        except ImportError:
            return
        schema = json.loads(
            (
                Path(__file__).parents[1]
                / "spec"
                / "cli-output.migrate-lock.schema.json"
            ).read_text()
        )
        jsonschema.validate(payload, schema)

    def test_analysis_reports_public_path_hash_as_unavailable_in_v010(self):
        config = {
            "project": "p",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {
                        "provider": "path-hash",
                        "paths": ["api/*.json"],
                    },
                }
            },
            "slices": {},
        }
        with patch("boundver._migration_analysis._component_files") as list_files:
            payload = self._analyze(config, Path("unused"))

        list_files.assert_not_called()
        declaration = payload["declarations"][0]
        self.assertEqual(declaration["analysis_status"], "legacy-rejected")
        self.assertEqual(declaration["impact"], "not-comparable")
        self.assertIn("did not register path-hash", declaration["detail"])

    def test_analysis_checks_declaration_cap_before_file_listing(self):
        from boundver._migration_analysis import MAX_ANALYZED_DECLARATIONS

        config = {
            "project": "p",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {
                        "provider": "path-hash",
                        "paths": [
                            f"file-{index}.txt"
                            for index in range(MAX_ANALYZED_DECLARATIONS + 1)
                        ],
                    },
                }
            },
            "slices": {},
        }
        with patch("boundver._migration_analysis._component_files") as list_files:
            with self.assertRaisesRegex(core.GuardrailError, "declaration limit"):
                self._analyze(config, Path("unused"))

        list_files.assert_not_called()

    def test_analysis_checks_global_cap_before_listing_earlier_components(self):
        from boundver._migration_analysis import MAX_ANALYZED_DECLARATIONS

        first_count = MAX_ANALYZED_DECLARATIONS // 2 + 1
        second_count = MAX_ANALYZED_DECLARATIONS - first_count + 1
        config = {
            "project": "p",
            "components": {
                "a": {
                    "path": "a",
                    "boundary": {
                        "provider": "json-file",
                        "paths": [f"a-{index}.json" for index in range(first_count)],
                    },
                },
                "b": {
                    "path": "b",
                    "boundary": {
                        "provider": "json-file",
                        "paths": [f"b-{index}.json" for index in range(second_count)],
                    },
                },
            },
            "slices": {},
        }
        with patch("boundver._migration_analysis._component_files") as list_files:
            with self.assertRaisesRegex(core.GuardrailError, "declaration limit"):
                self._analyze(config, Path("unused"))

        list_files.assert_not_called()

    def test_analysis_rejects_oversized_component_label_before_listing(self):
        config = {
            "project": "p",
            "components": {
                "x" * 4097: {
                    "path": "svc",
                    "boundary": {"provider": "openapi", "paths": ["api/*.yaml"]},
                }
            },
            "slices": {},
        }
        with patch("boundver._migration_analysis._component_files") as list_files:
            with self.assertRaisesRegex(core.ConfigError, "Component names.*limit"):
                self._analyze(config, Path("unused"))

        list_files.assert_not_called()

    def test_analysis_charges_current_glob_nfa_steps_to_the_global_budget(self):
        # Alternating recursive and ordinary wildcards keep many NFA states
        # live for each path segment. Candidate-call counting alone therefore
        # understates the work by orders of magnitude.
        pattern = "/".join(["**", "*"] * 128)
        candidate = "/".join(["segment"] * 256)
        config = {
            "project": "p",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {
                        "provider": "openapi",
                        "paths": [pattern],
                    },
                }
            },
            "slices": {},
        }
        with patch(
            "boundver._migration_analysis._component_files",
            return_value=[candidate],
        ), patch(
            "boundver._migration_analysis.MAX_SELECTOR_MATCH_EVALUATIONS",
            100,
        ):
            with self.assertRaisesRegex(
                core.GuardrailError,
                "100-step aggregate matching-work limit",
            ):
                self._analyze(config, Path("unused"))

    def test_analysis_charges_legacy_segment_regex_work_to_global_budget(self):
        pattern = "*a*a*a*a*ab"
        config = {
            "project": "p",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {
                        "provider": "openapi",
                        "paths": [pattern],
                    },
                }
            },
            "slices": {},
        }
        with patch(
            "boundver._migration_analysis._component_files",
            return_value=["a" * 4000],
        ), patch(
            "boundver._migration_analysis.MAX_SELECTOR_MATCH_EVALUATIONS",
            1000,
        ):
            with self.assertRaisesRegex(
                core.GuardrailError,
                "1000-step aggregate matching-work limit",
            ):
                self._analyze(config, Path("unused"))

    def test_analysis_bounds_captured_path_index_work_for_empty_roots(self):
        from boundver._git import GitSourceSnapshot, GitTreeEntry
        from boundver._migration_analysis import analyze_selector_migration

        entries = {
            f"unrelated/file-{index}.txt": GitTreeEntry(
                path=f"unrelated/file-{index}.txt",
                mode="100644",
                object_type="blob",
                oid=f"{index:040x}",
            )
            for index in range(100)
        }
        snapshot = GitSourceSnapshot(
            source="head",
            tree_oid="f" * 40,
            entries=entries,
        )
        config = {
            "project": "p",
            "components": {
                f"svc-{index}": {
                    "path": f"missing-{index}",
                    "boundary": {
                        "provider": "openapi",
                        "paths": ["*.json"],
                    },
                }
                for index in range(3)
            },
            "slices": {},
        }
        with patch(
            "boundver._migration_analysis.MAX_SELECTOR_MATCH_EVALUATIONS",
            25,
        ):
            with self.assertRaisesRegex(
                core.GuardrailError,
                "25-step aggregate matching-work limit",
            ):
                analyze_selector_migration(
                    config,
                    Path("unused"),
                    source="head",
                    snapshot=snapshot,
                    lock_path="old.lock.json",
                    lock_schema="boundary-lock/v2",
                    migration_action="regenerate",
                    migration_reason="regeneration required",
                )

    def test_analysis_match_evaluations_are_deterministic_work_units(self):
        config = {
            "project": "p",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {
                        "provider": "openapi",
                        "paths": ["**/*.json"],
                    },
                }
            },
            "slices": {},
        }
        files = ["api/a.json", "api/nested/b.json", "readme.txt"]
        with patch(
            "boundver._migration_analysis._component_files",
            return_value=files,
        ):
            first = self._analyze(config, Path("unused"))
            second = self._analyze(config, Path("unused"))

        first_steps = first["summary"]["match_evaluations"]
        self.assertEqual(first_steps, second["summary"]["match_evaluations"])
        self.assertGreater(first_steps, len(files) * 2)

    def test_working_tree_analysis_reuses_one_captured_tracked_set(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc" / "api").mkdir(parents=True)
            (root / "svc" / "api" / "route.yaml").write_text("route\n")
            config = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "openapi",
                            "paths": ["api/*.yaml"],
                        },
                    }
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(config))
            lock_path = root / "old.lock.json"
            lock_path.write_text(
                json.dumps(
                    {
                        "schema": "boundary-lock/v2",
                        "project": "p",
                        "components": {},
                        "slices": {},
                    }
                )
            )
            commit_all(root)

            with patch(
                "boundver._migration_analysis._list_files_for_source",
                side_effect=AssertionError("must use the captured tracked set"),
            ):
                code, out, err = _run_main(
                    "migrate-lock",
                    "--lock",
                    str(lock_path),
                    "--explain",
                    "--source",
                    "working-tree",
                    "--format",
                    "json",
                    repo_root=root,
                )

            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["source"]["mode"], "working-tree")

    def test_explain_bounds_an_unknown_oversized_schema_label(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x = 1\n")
            config = {
                "project": "p",
                "components": {"svc": _component("svc")},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(config))
            commit_all(root)
            lock_path = root / "old.lock.json"
            lock_path.write_text(
                json.dumps(
                    {
                        "schema": "x" * 5000,
                        "project": "p",
                        "components": {},
                        "slices": {},
                    }
                )
            )

            code, out, err = _run_main(
                "migrate-lock",
                "--lock",
                str(lock_path),
                "--explain",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(len(payload["lock"]["source_schema"]), 4096)
            self.assertTrue(payload["lock"]["source_schema"].endswith("..."))
            try:
                import jsonschema
            except ImportError:
                return
            schema = json.loads(
                (
                    Path(__file__).parents[1]
                    / "spec"
                    / "cli-output.migrate-lock.schema.json"
                ).read_text()
            )
            jsonschema.validate(payload, schema)


class DiscoveryConfigDiffTests(unittest.TestCase):
    def test_diff_config_reports_discovered_unregistered_roots(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            for name in ("registered", "new-service"):
                (root / name / "src").mkdir(parents=True)
                (root / name / "package.json").write_text('{"version":"1.0.0"}\n')
                (root / name / "src" / "index.js").write_text("export {};\n")
            config = {
                "project": "p",
                "components": {"api": _component("registered")},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(config))
            commit_all(root)

            code, out, err = _run_main(
                "discover",
                "--diff-config",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            comparison = payload["config_diff"]
            self.assertEqual(comparison["registered_count"], 1)
            self.assertEqual(comparison["unregistered_count"], 1)
            self.assertEqual(
                comparison["unregistered"],
                [{"name": "new-service", "path": "new-service"}],
            )
            self.assertEqual(comparison["not_discovered"], [])

            try:
                import jsonschema
            except ImportError:
                return
            schema = json.loads(
                (
                    Path(__file__).parents[1]
                    / "spec"
                    / "cli-output.discover.schema.json"
                ).read_text()
            )
            jsonschema.validate(payload, schema)
            invalid = json.loads(json.dumps(payload))
            invalid["config_diff"]["unregistered"][0]["name"] = ""
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(invalid, schema)


class VerifyBaselineTests(unittest.TestCase):
    def _repo(self, root: Path, *, vendored: bool = False) -> None:
        init_git_repo(root)
        for name in ("a", "b"):
            (root / name).mkdir()
            (root / name / "main.py").write_text(f"VALUE = {name!r}\n")
        if vendored:
            (root / "mirror").mkdir()
            (root / "mirror" / "main.py").write_text("VALUE = 'a'\n")
        component_a = _component("a")
        if vendored:
            component_a["vendored_copies"] = ["mirror"]
        config = {
            "project": "p",
            "components": {"a": component_a, "b": _component("b")},
            "slices": {},
        }
        (root / "boundary.config.json").write_text(json.dumps(config))
        commit_all(root, "initial")
        code, _, err = _run_main("generate", repo_root=root)
        self.assertEqual(code, 0, err)
        commit_all(root, "lock")

    def _change(self, root: Path, name: str, value: int) -> None:
        (root / name / "main.py").write_text(f"VALUE = {value}\n")
        commit_all(root, f"change {name} {value}")

    def _compat_repo(self, root: Path) -> None:
        init_git_repo(root)
        components = {}
        for name in ("a", "b"):
            (root / name).mkdir()
            (root / name / "main.py").write_text(f"VALUE = {name!r}\n")
            (root / name / "version.json").write_text('{"version":"1.0.0"}\n')
            component = _component(name)
            component["version_source"] = {
                "file": "version.json",
                "field": "version",
            }
            components[name] = component
        config = {"project": "p", "components": components, "slices": {}}
        (root / "boundary.config.json").write_text(json.dumps(config))
        commit_all(root, "initial")
        code, _, err = _run_main("generate", repo_root=root)
        self.assertEqual(code, 0, err)
        commit_all(root, "lock")

    def _change_version(self, root: Path, name: str, version: str) -> None:
        (root / name / "version.json").write_text(
            json.dumps({"version": version}) + "\n"
        )
        commit_all(root, f"change {name} version to {version}")

    def test_baseline_accepts_stable_known_identity_and_rejects_new_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            baseline_path = root / ".boundver-baseline.json"
            self._change(root, "a", 1)

            code, out, err = _run_main(
                "verify",
                "--write-baseline",
                baseline_path.name,
                "--format",
                "json",
                repo_root=root,
            )
            self.assertEqual(code, 0, err)
            created = json.loads(out)
            self.assertTrue(created["ok"])
            self.assertEqual(created["baseline"]["action"], "created")
            self.assertEqual(len(created["baseline"]["baselined_issues"]), 1)

            # A different digest for the same component/facet retains the
            # stable violation identity and remains acknowledged.
            self._change(root, "a", 2)
            code, out, err = _run_main(
                "verify",
                "--baseline",
                baseline_path.name,
                "--format",
                "json",
                repo_root=root,
            )
            self.assertEqual(code, 0, err)
            applied = json.loads(out)
            self.assertTrue(applied["ok"])
            self.assertIn(
                "MISMATCH a.exact", applied["baseline"]["baselined_issues"][0]
            )

            self._change(root, "b", 1)
            code, out, err = _run_main(
                "verify",
                "--baseline",
                baseline_path.name,
                "--format",
                "json",
                repo_root=root,
            )
            self.assertEqual(code, core.EXIT_DRIFT, err)
            rejected = json.loads(out)
            self.assertFalse(rejected["ok"])
            self.assertTrue(all("b.exact" in issue for issue in rejected["issues"]))
            self.assertTrue(
                all(
                    "a.exact" in issue
                    for issue in rejected["baseline"]["baselined_issues"]
                )
            )

            before_failed_update = baseline_path.read_bytes()
            code, out, err = _run_main(
                "verify",
                "--update-baseline",
                baseline_path.name,
                "--format",
                "json",
                repo_root=root,
            )
            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("shrink-only", err)
            self.assertEqual(baseline_path.read_bytes(), before_failed_update)

            # Once the known violation is fixed and no new one remains, the
            # update operation may only remove the now-stale identity.
            (root / "a" / "main.py").write_text("VALUE = 'a'\n")
            (root / "b" / "main.py").write_text("VALUE = 'b'\n")
            commit_all(root, "fix all debt")
            code, out, err = _run_main(
                "verify",
                "--update-baseline",
                baseline_path.name,
                "--format",
                "json",
                repo_root=root,
            )
            self.assertEqual(code, 0, err)
            updated = json.loads(out)
            self.assertEqual(updated["baseline"]["action"], "updated")
            self.assertEqual(updated["baseline"]["added_ids"], [])
            self.assertEqual(len(updated["baseline"]["removed_ids"]), 1)

            try:
                import jsonschema
            except ImportError:
                return
            baseline_schema = json.loads(
                (
                    Path(__file__).parents[1] / "spec" / "verify-baseline.schema.json"
                ).read_text()
            )
            stored_baseline = json.loads(baseline_path.read_text())
            jsonschema.validate(stored_baseline, baseline_schema)
            invalid_baseline = json.loads(json.dumps(stored_baseline))
            invalid_baseline["violations"] = [
                {
                    "id": "0" * 64,
                    "kind": "component-facet",
                    "subject": "a",
                    "facet": None,
                }
            ]
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(invalid_baseline, baseline_schema)
            verify_schema = json.loads(
                (
                    Path(__file__).parents[1] / "spec" / "cli-output.verify.schema.json"
                ).read_text()
            )
            jsonschema.validate(updated, verify_schema)

    def test_baseline_reads_the_selected_head_index_or_working_tree_view(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            self._change(root, "a", 1)
            baseline_paths = {
                "head": root / "debt-head.json",
                "index": root / "debt-index.json",
                "working-tree": root / "debt-working.json",
            }
            restrictive = {}
            for source, path in baseline_paths.items():
                code, _, err = _run_main(
                    "verify",
                    "--source",
                    source,
                    "--write-baseline",
                    path.name,
                    repo_root=root,
                )
                self.assertEqual(code, 0, err)
                restrictive[source] = path.read_bytes()
            commit_all(root, "commit restrictive source baselines")

            self._change(root, "b", 1)
            widened = {}
            for source in baseline_paths:
                wide_path = root / f"wide-{source}.json"
                code, _, err = _run_main(
                    "verify",
                    "--source",
                    source,
                    "--write-baseline",
                    wide_path.name,
                    repo_root=root,
                )
                self.assertEqual(code, 0, err)
                widened[source] = wide_path.read_bytes()

            # Commit a second, widened allowlist, then redirect the requested
            # working-tree path to it. Head must still use the requested
            # lexical entry from its snapshot rather than the symlink target.
            subprocess.run(
                ["git", "add", "wide-head.json"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "add alternate widened baseline"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            baseline_paths["head"].unlink()
            symlink_created = True
            try:
                baseline_paths["head"].symlink_to("wide-head.json")
            except OSError:
                # Windows without Developer Mode may forbid test symlinks; the
                # immutable-view assertions below still cover ordinary edits.
                symlink_created = False
                baseline_paths["head"].write_bytes(widened["head"])
            code, out, err = _run_main(
                "verify",
                "--source",
                "head",
                "--baseline",
                "debt-head.json",
                "--format",
                "json",
                repo_root=root,
            )
            self.assertEqual(code, core.EXIT_DRIFT, err)
            self.assertTrue(
                any("MISMATCH b.exact" in item for item in json.loads(out)["issues"])
            )
            if symlink_created:
                code, out, err = _run_main(
                    "verify",
                    "--source",
                    "working-tree",
                    "--baseline",
                    "debt-head.json",
                    repo_root=root,
                )
                self.assertEqual(code, core.EXIT_USAGE)
                self.assertEqual(out, "")
                self.assertIn("symlink", err)

            # The staged widened baseline is selected by index even after the
            # working-tree file is restored to its restrictive contents.
            baseline_paths["index"].write_bytes(widened["index"])
            subprocess.run(
                ["git", "add", baseline_paths["index"].name],
                cwd=root,
                check=True,
                capture_output=True,
            )
            baseline_paths["index"].write_bytes(restrictive["index"])
            code, out, err = _run_main(
                "verify",
                "--source",
                "index",
                "--baseline",
                "debt-index.json",
                "--format",
                "json",
                repo_root=root,
            )
            self.assertEqual(code, 0, err)
            self.assertTrue(json.loads(out)["ok"])

            # Working-tree mode reads the bytes currently on disk.
            baseline_paths["working-tree"].write_bytes(widened["working-tree"])
            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--baseline",
                "debt-working.json",
                "--format",
                "json",
                repo_root=root,
            )
            self.assertEqual(code, 0, err)
            self.assertTrue(json.loads(out)["ok"])

    def test_compat_identity_covers_only_its_ancillary_version_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._compat_repo(root)
            self._change_version(root, "a", "2.0.0")

            code, out, err = _run_main(
                "verify",
                "--facets",
                "compat",
                "--write-baseline",
                "debt.json",
                "--format",
                "json",
                repo_root=root,
            )
            self.assertEqual(code, 0, err)
            created = json.loads(out)
            acknowledged = created["baseline"]["baselined_issues"]
            self.assertTrue(any("MISMATCH a.compat" in item for item in acknowledged))
            self.assertTrue(
                any("METADATA MISMATCH a.version" in item for item in acknowledged)
            )
            stored = json.loads((root / "debt.json").read_text())
            self.assertEqual(len(stored["violations"]), 1)
            self.assertEqual(stored["violations"][0]["facet"], "compat")

            # The same stable compat identity covers its own version/semver
            # representation after the value changes again.
            self._change_version(root, "a", "3.0.0")
            code, out, err = _run_main(
                "verify",
                "--facets",
                "compat",
                "--baseline",
                "debt.json",
                "--format",
                "json",
                repo_root=root,
            )
            self.assertEqual(code, 0, err)
            self.assertTrue(json.loads(out)["ok"])

            # A new component's compat identity is still a new severity-5
            # violation; the known component cannot mask it.
            self._change_version(root, "b", "2.0.0")
            code, out, err = _run_main(
                "verify",
                "--facets",
                "compat",
                "--baseline",
                "debt.json",
                "--format",
                "json",
                repo_root=root,
            )
            self.assertEqual(code, core.EXIT_COMPAT, err)
            payload = json.loads(out)
            self.assertTrue(
                any("MISMATCH b.compat" in item for item in payload["issues"])
            )
            self.assertTrue(
                any("METADATA MISMATCH b.version" in item for item in payload["issues"])
            )

    def test_version_metadata_without_current_compat_mismatch_is_not_baselinable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._compat_repo(root)
            lock_path = root / "boundary.lock.json"
            lock = json.loads(lock_path.read_text())
            lock["components"]["a"]["version"] = "9.9.9"
            lock["components"]["a"]["semver"] = {
                "compat_family": "9",
                "api_surface": "9.9",
                "exact_version": "9.9.9",
            }
            lock_path.write_text(json.dumps(lock))
            commit_all(root, "metadata-only lock tamper")

            code, out, err = _run_main(
                "verify",
                "--facets",
                "compat",
                "--write-baseline",
                "debt.json",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("cannot baseline", err)
            self.assertIn("METADATA MISMATCH a.version", err)
            self.assertFalse((root / "debt.json").exists())

    def test_baseline_capture_is_create_only_and_outside_components(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            self._change(root, "a", 1)
            baseline = root / "debt.json"
            baseline.write_text("sentinel\n")

            code, _, err = _run_main(
                "verify", "--write-baseline", baseline.name, repo_root=root
            )
            self.assertEqual(code, core.EXIT_USAGE)
            self.assertIn("already exists", err)
            self.assertEqual(baseline.read_text(), "sentinel\n")

            code, _, err = _run_main(
                "verify", "--write-baseline", "a/debt.json", repo_root=root
            )
            self.assertEqual(code, core.EXIT_USAGE)
            self.assertIn("inside component", err)
            self.assertFalse((root / "a" / "debt.json").exists())

            code, _, err = _run_main(
                "verify",
                "--write-baseline",
                "fresh.json",
                "--update",
                repo_root=root,
            )
            self.assertEqual(code, core.EXIT_USAGE)
            self.assertIn("cannot be combined", err)
            self.assertFalse((root / "fresh.json").exists())

    def test_baseline_create_race_never_clobbers_the_competing_file(self):
        from boundver import _baseline

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            self._change(root, "a", 1)
            baseline_path = root / "debt.json"
            competing = b"created by a competing process\n"
            real_link = _baseline._MutationDirectory.link

            def inject_competing_create(directory, source, target):
                (directory.path / target).write_bytes(competing)
                return real_link(directory, source, target)

            with patch.object(
                _baseline._MutationDirectory,
                "link",
                new=inject_competing_create,
            ):
                code, out, err = _run_main(
                    "verify",
                    "--write-baseline",
                    baseline_path.name,
                    repo_root=root,
                )

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("already exists", err)
            self.assertEqual(baseline_path.read_bytes(), competing)
            self.assertEqual(list(root.glob(".debt.json.*.tmp")), [])

    def test_baseline_update_race_fails_without_replacing_changed_bytes(self):
        from boundver import _baseline

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            self._change(root, "a", 1)
            baseline_path = root / "debt.json"
            code, _, err = _run_main(
                "verify",
                "--write-baseline",
                baseline_path.name,
                repo_root=root,
            )
            self.assertEqual(code, 0, err)

            # Commit the reviewed baseline and resolve its only identity so a
            # normal update would shrink it to an empty violation set.
            (root / "a" / "main.py").write_text("VALUE = 'a'\n")
            commit_all(root, "record baseline and resolve debt")
            competing = b"changed by a competing process\n"
            real_read = _baseline._read_bounded_sibling_bytes
            reads = 0

            def inject_second_read_race(*args, **kwargs):
                nonlocal reads
                reads += 1
                if reads == 2:
                    baseline_path.write_bytes(competing)
                return real_read(*args, **kwargs)

            with patch(
                "boundver._baseline._read_bounded_sibling_bytes",
                side_effect=inject_second_read_race,
            ):
                code, out, err = _run_main(
                    "verify",
                    "--update-baseline",
                    baseline_path.name,
                    repo_root=root,
                )

            self.assertEqual(reads, 2)
            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("changed before update", err)
            self.assertEqual(baseline_path.read_bytes(), competing)
            self.assertFalse((root / ".debt.json.boundver-update.lock").exists())
            self.assertEqual(list(root.glob(".debt.json.*.tmp")), [])

    def test_baseline_final_publication_race_restores_competing_bytes(self):
        from boundver import _baseline

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            self._change(root, "a", 1)
            baseline_path = root / "debt.json"
            code, _, err = _run_main(
                "verify",
                "--write-baseline",
                baseline_path.name,
                repo_root=root,
            )
            self.assertEqual(code, 0, err)
            (root / "a" / "main.py").write_text("VALUE = 'a'\n")
            commit_all(root, "record baseline and resolve debt")

            competing = b"changed in the final publication gap\n"
            real_replace = _baseline._MutationDirectory.replace
            replacements = 0

            def inject_final_gap_race(directory, source, target):
                nonlocal replacements
                replacements += 1
                baseline_path.write_bytes(competing)
                return real_replace(directory, source, target)

            with patch.object(
                _baseline._MutationDirectory,
                "replace",
                new=inject_final_gap_race,
            ):
                code, out, err = _run_main(
                    "verify",
                    "--update-baseline",
                    baseline_path.name,
                    repo_root=root,
                )

            self.assertEqual(replacements, 1)
            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("changed during update", err)
            self.assertEqual(baseline_path.read_bytes(), competing)
            self.assertFalse((root / ".debt.json.boundver-update.lock").exists())
            self.assertEqual(list(root.glob(".debt.json.*.tmp")), [])
            self.assertEqual(list(root.glob(".debt.json.*.claim")), [])

    def test_baseline_claim_restores_empty_competing_bytes_on_interrupt(self):
        from boundver import _baseline

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            self._change(root, "a", 1)
            baseline_path = root / "debt.json"
            code, _, err = _run_main(
                "verify",
                "--write-baseline",
                baseline_path.name,
                repo_root=root,
            )
            self.assertEqual(code, 0, err)
            (root / "a" / "main.py").write_text("VALUE = 'a'\n")
            commit_all(root, "record baseline and resolve debt")
            real_replace = _baseline._MutationDirectory.replace
            replacements = 0

            def interrupt_after_claim(directory, source, target):
                nonlocal replacements
                replacements += 1
                baseline_path.write_bytes(b"")
                real_replace(directory, source, target)
                raise KeyboardInterrupt

            with patch.object(
                _baseline._MutationDirectory,
                "replace",
                new=interrupt_after_claim,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    _run_main(
                        "verify",
                        "--update-baseline",
                        baseline_path.name,
                        repo_root=root,
                    )

            self.assertEqual(replacements, 1)
            self.assertEqual(baseline_path.read_bytes(), b"")
            self.assertFalse((root / ".debt.json.boundver-update.lock").exists())
            self.assertEqual(list(root.glob(".debt.json.*.tmp")), [])
            self.assertEqual(list(root.glob(".debt.json.*.claim")), [])

    def test_baseline_temp_is_removed_when_durability_sync_is_interrupted(self):
        from boundver import _baseline

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "debt.json"
            with _baseline._mutation_directory(
                target,
                root,
                create_parents=False,
            ) as (_label, directory):
                with patch(
                    "boundver._baseline.os.fsync",
                    side_effect=KeyboardInterrupt,
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        _baseline._write_durable_temp(
                            directory,
                            target.name,
                            b"baseline\n",
                        )

            self.assertEqual(list(root.glob(".debt.json.*.tmp")), [])

    def test_baseline_update_requires_live_bytes_to_match_head_and_index(self):
        for source in ("head", "index"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                self._repo(root)
                self._change(root, "a", 1)
                baseline_path = root / "debt.json"
                code, _, err = _run_main(
                    "verify",
                    "--source",
                    source,
                    "--write-baseline",
                    baseline_path.name,
                    repo_root=root,
                )
                self.assertEqual(code, 0, err)
                (root / "a" / "main.py").write_text("VALUE = 'a'\n")
                commit_all(root, "record baseline and resolve debt")
                reviewed = baseline_path.read_bytes()

                competing = f"unstaged {source} bytes\n".encode()
                baseline_path.write_bytes(competing)
                code, out, err = _run_main(
                    "verify",
                    "--source",
                    source,
                    "--update-baseline",
                    baseline_path.name,
                    repo_root=root,
                )
                self.assertEqual(code, core.EXIT_USAGE)
                self.assertEqual(out, "")
                self.assertIn("changed before update", err)
                self.assertEqual(baseline_path.read_bytes(), competing)
                baseline_path.write_bytes(reviewed)

    def test_baseline_write_rejects_a_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            self._change(root, "a", 1)
            (root / "real-output").mkdir()
            try:
                (root / "redirect").symlink_to("real-output", target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks are unavailable on this platform")

            code, out, err = _run_main(
                "verify", "--write-baseline", "redirect/debt.json", repo_root=root
            )

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("symlink", err)
            self.assertFalse((root / "real-output" / "debt.json").exists())

    def test_baseline_refuses_semantic_config_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            config_path = root / "boundary.config.json"
            config = json.loads(config_path.read_text())
            config["components"]["a"]["external_consumers"] = ["outside"]
            config_path.write_text(json.dumps(config))
            commit_all(root, "change semantic config")

            code, out, err = _run_main(
                "verify", "--write-baseline", "debt.json", repo_root=root
            )

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("cannot baseline", err)
            self.assertIn("config_digest", err)
            self.assertFalse((root / "debt.json").exists())

    def test_baseline_is_bound_to_exact_reviewed_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            self._change(root, "a", 1)
            code, _, err = _run_main(
                "verify", "--write-baseline", "debt.json", repo_root=root
            )
            self.assertEqual(code, 0, err)
            lock_path = root / "boundary.lock.json"
            lock = json.loads(lock_path.read_text())
            lock["components"]["a"]["fingerprints"]["exact"] = "f" * 64
            lock_path.write_text(json.dumps(lock))
            commit_all(root, "tamper reviewed lock anchor")

            code, out, err = _run_main(
                "verify",
                "--baseline",
                "debt.json",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("lock_digest", err)

    def test_baseline_refuses_vendored_copy_roots(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root, vendored=True)

            code, _, err = _run_main(
                "verify", "--write-baseline", "mirror/debt.json", repo_root=root
            )

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertIn("vendored copy", err)
            self.assertFalse((root / "mirror" / "debt.json").exists())

    def test_malformed_baseline_facets_fail_without_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            self._change(root, "a", 1)
            code, _, err = _run_main(
                "verify", "--write-baseline", "debt.json", repo_root=root
            )
            self.assertEqual(code, 0, err)
            baseline_path = root / "debt.json"
            malformed = json.loads(baseline_path.read_text())
            malformed["facets"] = [[]]
            baseline_path.write_text(json.dumps(malformed))

            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--baseline",
                "debt.json",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("baseline facets", err)
            self.assertNotIn("Traceback", err)

    def test_oversized_baseline_output_is_refused_before_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            issues = [
                f"MISMATCH {'x' * 3900}{index}.exact: lockfile=old current=new"
                for index in range(550)
            ]

            with patch("boundver.core.verify_lockfile", return_value=issues):
                code, out, err = _run_main(
                    "verify", "--write-baseline", "debt.json", repo_root=root
                )

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("storage limit", err)
            self.assertFalse((root / "debt.json").exists())

    def test_baseline_duplicate_key_diagnostic_is_bounded(self):
        from boundver._baseline import BaselineError, _parse_baseline_bytes

        key = "x" * 100_000
        raw = ("{" + json.dumps(key) + ": 1, " + json.dumps(key) + ": 2}").encode()

        with self.assertRaises(BaselineError) as raised:
            _parse_baseline_bytes(raw, "debt.json")

        diagnostic = str(raised.exception)
        self.assertIn("duplicate JSON object key", diagnostic)
        self.assertIn("...", diagnostic)
        self.assertLess(len(diagnostic), 1024)

        unknown_raw = json.dumps({key: 1}).encode()
        with self.assertRaises(BaselineError) as raised:
            _parse_baseline_bytes(unknown_raw, "debt.json")
        unknown_diagnostic = str(raised.exception)
        self.assertIn("unknown baseline fields", unknown_diagnostic)
        self.assertIn("...", unknown_diagnostic)
        self.assertLess(len(unknown_diagnostic), 1024)

    def test_affected_consumer_identities_preserve_component_colons(self):
        from boundver._baseline import apply_baseline, create_baseline

        context = {
            "project": "p",
            "lock_schema": "boundary-lock/v3",
            "lock_digest": "a" * 64,
            "config_contract": "boundver-semantic-config/v2",
            "source": "head",
            "components_filter": [],
            "facets": ["boundary"],
            "transitive": False,
            "policy_digest": "b" * 64,
        }
        issues = [
            "MISMATCH api.boundary: lockfile=old current=new",
            "AFFECTED CONSUMERS api: client: mobile",
            "MISMATCH api: client.boundary: lockfile=old current=new",
            "AFFECTED CONSUMERS api: client: web",
        ]

        baseline = create_baseline(context, issues)
        consumer_subjects = {
            entry["subject"]
            for entry in baseline["violations"]
            if entry["kind"] == "affected-consumers"
        }
        self.assertEqual(consumer_subjects, {"api", "api: client"})

        changed_issues = [issue.replace("current=new", "current=newer") for issue in issues]
        new, acknowledged, stale = apply_baseline(
            baseline,
            context,
            changed_issues,
        )
        self.assertEqual(new, [])
        self.assertEqual(acknowledged, changed_issues)
        self.assertEqual(stale, [])


class StrictSliceCLIValidationTests(unittest.TestCase):
    def test_validate_config_allow_partial_mirrors_generate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "leaf").mkdir()
            (root / "leaf" / "main.py").write_text("x = 1\n")
            config = {
                "project": "p",
                "components": {
                    "leaf": {
                        "path": "leaf",
                        "version_source": None,
                        "boundary": {"provider": "leaf", "paths": []},
                    }
                },
                "slices": {
                    "public": {
                        "mode": "boundary",
                        "components": ["leaf"],
                    }
                },
            }
            (root / "boundary.config.json").write_text(json.dumps(config))

            code, out, _ = _run_main("validate-config", repo_root=root)
            self.assertEqual(code, core.EXIT_USAGE)
            self.assertIn("does not produce", out)

            code, out, err = _run_main(
                "validate-config", "--allow-partial", repo_root=root
            )
            self.assertEqual(code, 0, err)
            self.assertIn("Config is valid", out)

    def test_source_help_names_head_as_the_default(self):
        for command in ("generate", "verify", "status", "migrate-lock"):
            with self.subTest(command=command):
                code, out, err = _run_main(command, "--help")
                self.assertEqual(code, 0, err)
                self.assertIn("default: head", out)

    def test_malformed_strict_slice_values_report_errors_without_traceback(self):
        cases = {
            "mode": {
                "project": "p",
                "components": {"svc": _component("svc")},
                "slices": {"bad": {"mode": [], "components": ["svc"]}},
            },
            "boundary.provider": {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "version_source": None,
                        "boundary": {"provider": [], "paths": []},
                    }
                },
                "slices": {"bad": {"mode": "boundary", "components": ["svc"]}},
            },
        }
        for diagnostic, config in cases.items():
            with (
                self.subTest(diagnostic=diagnostic),
                tempfile.TemporaryDirectory() as td,
            ):
                root = Path(td)
                init_git_repo(root)
                (root / "svc").mkdir()
                (root / "svc" / "main.py").write_text("x = 1\n")
                (root / "boundary.config.json").write_text(json.dumps(config))

                code, out, err = _run_main("validate-config", repo_root=root)

                rendered = out + err
                self.assertEqual(code, core.EXIT_USAGE)
                self.assertIn(diagnostic, rendered)
                self.assertNotIn("Traceback", rendered)


if __name__ == "__main__":
    unittest.main()
