import tempfile
import unittest
from pathlib import Path
import subprocess
import os
import json

import boundver.core as boundary_lock
import boundver
import boundver.versions as versions


class BoundaryLockTests(unittest.TestCase):
    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True, capture_output=True, text=True)

    def test_package_exposes_version(self):
        self.assertTrue(isinstance(boundver.__version__, str))
        self.assertNotEqual(boundver.__version__.strip(), "")

    def test_extract_version_git_tag_prefix_without_resolver_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            version = versions.extract_version(root, ".", {"git_tag_prefix": "v"}, None)
            self.assertIsNone(version)

    def _run_cli(self, root: Path, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        repo_src = str(Path(__file__).resolve().parents[1] / "src")
        env["PYTHONPATH"] = repo_src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return subprocess.run(
            ["python", "-m", "boundver.core", *args],
            cwd=root,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_parse_semver(self):
        self.assertEqual(boundary_lock.parse_semver("1.2.3"), ("1", "1.2", "1.2.3"))
        self.assertEqual(boundary_lock.parse_semver("v2.4"), ("2", "2.4", "2.4.0"))
        self.assertEqual(boundary_lock.parse_semver(None), (None, None, None))

    def test_validate_config_unsupported_compat_mode(self):
        cfg = {
            "defaults": {"compat_mode": "unsupported"},
            "components": {},
            "slices": {},
        }
        errs = boundary_lock.validate_config(cfg, Path.cwd())
        self.assertTrue(any("Unsupported defaults.compat_mode" in e for e in errs))

    def test_validate_config_boundary_slice_leaf_component(self):
        cfg = {
            "components": {
                "x": {
                    "path": "service/x",
                    "boundary": {"provider": "leaf", "paths": []},
                }
            },
            "slices": {"s1": {"mode": "boundary", "components": ["x"]}},
        }
        errs = boundary_lock.validate_config(cfg, Path.cwd())
        self.assertTrue(any("boundary mode cannot include" in e for e in errs))

    def test_validate_config_accepts_boundary_provider_key(self):
        cfg = {
            "components": {
                "x": {
                    "path": ".",
                    "boundary": {"provider": "openapi", "paths": []},
                }
            },
            "slices": {},
        }
        errs = boundary_lock.validate_config(cfg, Path.cwd())
        self.assertFalse(any("unknown" in e.lower() for e in errs))

    def test_validate_config_rejects_legacy_boundary_kind(self):
        cfg = {
            "components": {
                "x": {
                    "path": ".",
                    "boundary": {"kind": "implicit", "paths": []},
                }
            },
            "slices": {},
        }
        errs = boundary_lock.validate_config(cfg, Path.cwd())
        self.assertTrue(any("uses legacy boundary.kind" in e for e in errs))

    def test_validate_config_requires_top_level_fields(self):
        errs = boundary_lock.validate_config({}, Path.cwd())
        self.assertTrue(any("Missing required top-level field: project" in e for e in errs))
        self.assertTrue(any("Missing required top-level field: components" in e for e in errs))
        self.assertTrue(any("Missing required top-level field: slices" in e for e in errs))

    def test_validate_config_requires_boundary_provider(self):
        cfg = {
            "project": "p",
            "components": {
                "x": {"path": ".", "boundary": {"paths": []}},
            },
            "slices": {},
        }
        errs = boundary_lock.validate_config(cfg, Path.cwd())
        self.assertTrue(any("missing required field: boundary.provider" in e for e in errs))

    def test_validate_config_rejects_invalid_boundary_paths_type(self):
        cfg = {
            "project": "x",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "openapi", "paths": "api.yaml"},
                }
            },
            "slices": {},
        }
        errs = boundary_lock.validate_config(cfg, Path.cwd())
        self.assertTrue(any("boundary.paths' must be an array of strings" in e for e in errs))

    def test_validate_config_rejects_invalid_slice_components_type(self):
        cfg = {
            "project": "x",
            "components": {
                "svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": []}}
            },
            "slices": {"s1": {"mode": "exact", "components": "svc"}},
        }
        errs = boundary_lock.validate_config(cfg, Path.cwd())
        self.assertTrue(any("field 'components' must be an array of strings" in e for e in errs))

    def test_schema_engine_errors_returns_empty_without_schema(self):
        errs = boundary_lock._schema_engine_errors({"project": "x"}, None)
        self.assertEqual(errs, [])

    def test_validate_config_includes_schema_engine_errors(self):
        original = boundary_lock._schema_engine_errors
        try:
            boundary_lock._schema_engine_errors = lambda cfg, schema: ["Schema validation error at <root>: boom"]
            cfg = {"project": "x", "components": {}, "slices": {}}
            errs = boundary_lock.validate_config(cfg, Path.cwd())
            self.assertTrue(any("Schema validation error at <root>: boom" in e for e in errs))
        finally:
            boundary_lock._schema_engine_errors = original

    def test_validate_config_rejects_duplicate_component_paths(self):
        cfg = {
            "project": "x",
            "components": {
                "a": {"path": "svc/a", "boundary": {"provider": "openapi", "paths": []}},
                "b": {"path": "svc/a", "boundary": {"provider": "openapi", "paths": []}},
            },
            "slices": {},
        }
        errs = boundary_lock.validate_config(cfg, Path.cwd())
        self.assertTrue(any("Duplicate component path" in e for e in errs))

    def test_validate_config_rejects_unknown_provider_without_custom_namespace(self):
        cfg = {
            "project": "x",
            "components": {
                "a": {"path": "svc/a", "boundary": {"provider": "my-provider", "paths": []}},
            },
            "slices": {},
        }
        errs = boundary_lock.validate_config(cfg, Path.cwd())
        self.assertTrue(any("unsupported boundary.provider" in e for e in errs))

    def test_validate_config_rejects_boundary_path_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "x",
                "components": {
                    "a": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["../secrets.yaml"]},
                    },
                },
                "slices": {},
            }
            errs = boundary_lock.validate_config(cfg, root)
            self.assertTrue(any("escapes component root" in e for e in errs), errs)

    def test_generate_lockfile_marks_partial_when_no_boundary_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comp_dir = root / "svc"
            comp_dir.mkdir(parents=True)
            (comp_dir / "x.txt").write_text("hello")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit", "paths": []},
                    }
                },
                "slices": {},
            }
            lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            entry = lock["components"]["svc"]
            self.assertEqual(entry["boundary_status"], "partial")
            self.assertIn("No boundary paths declared for implicit boundary", entry.get("boundary_errors", []))

    def test_generate_lockfile_deterministic_omits_generated_at(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comp_dir = root / "svc"
            comp_dir.mkdir(parents=True)
            (comp_dir / "api.yaml").write_text("openapi: 3.0.0")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                    }
                },
                "slices": {},
            }
            lock1 = boundary_lock.generate_lockfile(cfg, root, source="working-tree", deterministic=True)
            lock2 = boundary_lock.generate_lockfile(cfg, root, source="working-tree", deterministic=True)
            self.assertNotIn("generated_at", lock1)
            self.assertEqual(lock1, lock2)

    def test_generate_lockfile_marks_ok_when_boundary_hashes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comp_dir = root / "svc"
            comp_dir.mkdir(parents=True)
            (comp_dir / "api.yaml").write_text("openapi: 3.0.0")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                    }
                },
                "slices": {},
            }
            lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            entry = lock["components"]["svc"]
            self.assertEqual(entry["boundary_status"], "ok")
            self.assertIsNotNone(entry["fingerprints"]["boundary"])

    def test_generate_lockfile_boundary_slice_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comp_dir = root / "svc"
            comp_dir.mkdir(parents=True)
            (comp_dir / "api.yaml").write_text("openapi: 3.0.0")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                    }
                },
                "slices": {"s1": {"mode": "boundary", "components": ["svc"]}},
            }
            lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            self.assertEqual(lock["slices"]["s1"]["mode"], "boundary")

    def test_schema_accepts_boundary_slice_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            schema_target = root / "boundary.config.schema.json"
            source_schema = Path(__file__).resolve().parents[1] / "boundary.config.schema.json"
            schema_target.write_text(source_schema.read_text())

            cfg = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}
                },
                "slices": {"s1": {"mode": "boundary", "components": ["svc"]}},
            }
            errs = boundary_lock.validate_config(cfg, root)
            self.assertFalse(any("Schema validation error" in e for e in errs), errs)

    def test_generate_lockfile_marks_error_for_explicit_kind_without_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comp_dir = root / "svc"
            comp_dir.mkdir(parents=True)
            (comp_dir / "main.py").write_text("print('x')")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": []},
                    }
                },
                "slices": {},
            }
            lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            entry = lock["components"]["svc"]
            self.assertEqual(entry["boundary_status"], "error")
            self.assertIn(
                "No boundary paths declared for explicit boundary provider",
                entry.get("boundary_errors", []),
            )

    def test_exact_hash_matches_between_head_and_working_tree_for_same_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            comp_dir = root / "svc"
            comp_dir.mkdir(parents=True)
            (comp_dir / "main.py").write_text("print('v1')\n")
            (comp_dir / "api.yaml").write_text("openapi: 3.0.0\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True, text=True)

            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                    }
                },
                "slices": {},
            }
            prev_cwd = Path.cwd()
            os.chdir(root)
            try:
                head_lock = boundary_lock.generate_lockfile(cfg, root, source="head")
                wt_lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            finally:
                os.chdir(prev_cwd)
            self.assertIsNotNone(head_lock["components"]["svc"]["fingerprints"]["exact"])
            self.assertEqual(
                head_lock["components"]["svc"]["fingerprints"]["exact"],
                wt_lock["components"]["svc"]["fingerprints"]["exact"],
            )

    def test_boundary_source_head_ignores_working_tree_deletion(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            comp_dir = root / "svc"
            comp_dir.mkdir(parents=True)
            (comp_dir / "main.py").write_text("print('v1')\n")
            (comp_dir / "api.yaml").write_text("openapi: 3.0.0\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True, text=True)

            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                    }
                },
                "slices": {"s1": {"mode": "boundary", "components": ["svc"]}},
            }

            before = boundary_lock.generate_lockfile(cfg, root, source="head")
            (comp_dir / "api.yaml").unlink()
            after = boundary_lock.generate_lockfile(cfg, root, source="head")

            self.assertEqual(
                before["components"]["svc"]["fingerprints"]["boundary"],
                after["components"]["svc"]["fingerprints"]["boundary"],
            )

    def test_git_latest_tag_uses_repo_root_not_process_cwd(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as other:
            root = Path(td)
            self._init_git_repo(root)
            svc = root / "svc"
            svc.mkdir(parents=True)
            (svc / "main.py").write_text("print('v1')\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "tag", "svc-v1.2.3"], cwd=root, check=True, capture_output=True, text=True)

            prev_cwd = Path.cwd()
            os.chdir(other)
            try:
                version = boundary_lock.git_latest_tag(root, "svc-v")
            finally:
                os.chdir(prev_cwd)

            self.assertEqual(version, "1.2.3")

    def test_git_latest_tag_prefers_reachable_tags(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "app.py").write_text("print('base')\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "tag", "svc-v1.2.3"], cwd=root, check=True, capture_output=True, text=True)

            # Create an orphan branch with a higher tag that should be unreachable from current HEAD.
            subprocess.run(["git", "checkout", "--orphan", "other"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "rm", "-rf", "."], cwd=root, check=True, capture_output=True, text=True)
            (root / "other.py").write_text("print('other')\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "other"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "tag", "svc-v9.9.9"], cwd=root, check=True, capture_output=True, text=True)

            subprocess.run(["git", "checkout", "master"], cwd=root, check=True, capture_output=True, text=True)

            version = boundary_lock.git_latest_tag(root, "svc-v")
            self.assertEqual(version, "1.2.3")

    def test_extract_toml_field_supports_three_level_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                "[tool.poetry]\n"
                "name = \"svc\"\n"
                "version = \"2.5.1\"\n"
            )
            version = boundary_lock._extract_toml_field(pyproject, "tool.poetry.version")
            self.assertEqual(version, "2.5.1")

    def test_extract_yaml_field_fallback_parser(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            spec = root / "openapi.yaml"
            spec.write_text("info:\n  version: 1.4.0\n")
            original_yaml = boundary_lock._extract_yaml_field.__globals__.get("yaml")
            try:
                boundary_lock._extract_yaml_field.__globals__["yaml"] = None
                version = boundary_lock._extract_yaml_field(spec, "info.version")
            finally:
                boundary_lock._extract_yaml_field.__globals__["yaml"] = original_yaml
            self.assertEqual(version, "1.4.0")

    def test_init_writes_schema_and_refuses_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            res1 = self._run_cli(root, "init")
            self.assertEqual(res1.returncode, 0, res1.stderr)
            cfg = json.loads((root / "boundary.config.json").read_text())
            self.assertIn("$schema", cfg)
            self.assertEqual(cfg["components"]["example-component"]["boundary"]["provider"], "implicit")

            res2 = self._run_cli(root, "init")
            self.assertNotEqual(res2.returncode, 0)
            self.assertIn("Config already exists", res2.stderr)

    def test_init_supports_out_and_force(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            target = "custom-config.json"
            res1 = self._run_cli(root, "init", "--out", target)
            self.assertEqual(res1.returncode, 0, res1.stderr)
            path = root / target
            self.assertTrue(path.exists())

            path.write_text('{"project":"mutated","components":{},"slices":{}}\n')
            res2 = self._run_cli(root, "init", "--out", target, "--force")
            self.assertEqual(res2.returncode, 0, res2.stderr)
            self.assertNotEqual(path.read_text(), '{"project":"mutated","components":{},"slices":{}}\n')

    def test_init_then_validate_config_succeeds_when_component_path_exists(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "src").mkdir()
            init_res = self._run_cli(root, "init")
            self.assertEqual(init_res.returncode, 0, init_res.stderr)
            validate_res = self._run_cli(root, "validate-config")
            self.assertEqual(validate_res.returncode, 0, validate_res.stderr)

    def test_generate_dry_run_does_not_write_lockfile(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "$schema": "https://raw.githubusercontent.com/yzm1/boundver/main/boundary.config.schema.json",
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg, indent=2) + "\n")
            res = self._run_cli(root, "generate", "--source", "working-tree", "--dry-run")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("Dry run: lockfile not written", res.stdout)
            self.assertFalse((root / "boundary.lock.json").exists())

    def test_verify_json_output_machine_readable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "$schema": "https://raw.githubusercontent.com/yzm1/boundver/main/boundary.config.schema.json",
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg, indent=2) + "\n")
            gen = self._run_cli(root, "generate", "--source", "working-tree")
            self.assertEqual(gen.returncode, 0, gen.stderr)
            ver = self._run_cli(root, "verify", "--source", "working-tree", "--json")
            self.assertEqual(ver.returncode, 0, ver.stderr)
            payload = json.loads(ver.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["issues"], [])

    def test_verify_verbose_prints_diagnostics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "$schema": "https://raw.githubusercontent.com/yzm1/boundver/main/boundary.config.schema.json",
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg, indent=2) + "\n")
            self.assertEqual(self._run_cli(root, "generate", "--source", "working-tree").returncode, 0)
            ver = self._run_cli(root, "--verbose", "verify", "--source", "working-tree")
            self.assertEqual(ver.returncode, 0, ver.stderr)
            self.assertIn("Verified source=working-tree with 0 issues.", ver.stdout)

    def test_quiet_and_verbose_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            res = self._run_cli(root, "--quiet", "--verbose", "status")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("not allowed with argument", res.stderr)

    def test_status_json_returns_single_json_payload_with_issues(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "$schema": "https://raw.githubusercontent.com/yzm1/boundver/main/boundary.config.schema.json",
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg, indent=2) + "\n")
            self.assertEqual(self._run_cli(root, "generate", "--source", "working-tree").returncode, 0)
            (root / "svc" / "api.yaml").write_text("openapi: 3.1.0\n")
            res = self._run_cli(root, "status", "--source", "working-tree", "--json")
            self.assertEqual(res.returncode, 0, res.stderr)
            payload = json.loads(res.stdout)
            self.assertIn("lockfile", payload)
            self.assertIn("issues", payload)
            self.assertTrue(any("MISMATCH svc.boundary" in i for i in payload["issues"]))

    def test_examples_expected_lockfiles_are_current(self):
        repo_root = Path(__file__).resolve().parents[1]
        examples = [
            "openapi",
            "json-file",
            "implicit-and-leaf",
            "python-package",
            "typescript-package",
        ]
        for ex in examples:
            cfg_path = repo_root / "examples" / ex / "boundary.config.json"
            exp_path = repo_root / "examples" / ex / "expected.boundary.lock.json"
            cfg = json.loads(cfg_path.read_text())
            generated = boundary_lock.generate_lockfile(
                cfg, repo_root, source="working-tree", deterministic=True, strict=False
            )
            expected = json.loads(exp_path.read_text())
            self.assertEqual(
                generated,
                expected,
                f"Example lockfile out of date for {ex}. Regenerate expected.boundary.lock.json",
            )

    def test_verify_lockfile_reports_missing_or_unsupported_schema(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}},
                "slices": {},
            }
            good = boundary_lock.generate_lockfile(cfg, root, source="working-tree", deterministic=True)

            missing_schema = dict(good)
            missing_schema.pop("schema", None)
            issues_missing = boundary_lock.verify_lockfile(cfg, missing_schema, root, source="working-tree")
            self.assertTrue(any("schema missing" in i for i in issues_missing), issues_missing)

            bad_schema = dict(good)
            bad_schema["schema"] = "boundary-lock/v9"
            issues_bad = boundary_lock.verify_lockfile(cfg, bad_schema, root, source="working-tree")
            self.assertTrue(any("schema unsupported" in i for i in issues_bad), issues_bad)

    def test_verify_lockfile_reports_malformed_structure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}},
                "slices": {},
            }
            lock = {"schema": "boundary-lock/v1", "components": {"svc": {}}, "slices": []}
            issues = boundary_lock.verify_lockfile(cfg, lock, root, source="working-tree")
            self.assertTrue(any("LOCKFILE malformed" in i for i in issues), issues)

    def test_verify_lockfile_malformed_components_type_does_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}},
                "slices": {},
            }
            malformed = {"schema": "boundary-lock/v1", "components": [], "slices": {}}
            issues = boundary_lock.verify_lockfile(cfg, malformed, root, source="working-tree")
            self.assertTrue(any("components must be an object" in i for i in issues), issues)

    def test_verify_lockfile_detects_new_removed_and_stale_components(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a").mkdir()
            (root / "a" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "project": "p",
                "components": {"a": {"path": "a", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}},
                "slices": {},
            }
            lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree", deterministic=True)

            # stale boundary
            (root / "a" / "api.yaml").write_text("openapi: 3.1.0\n")
            stale_issues = boundary_lock.verify_lockfile(cfg, lock, root, source="working-tree")
            self.assertTrue(any("MISMATCH a.boundary" in i for i in stale_issues), stale_issues)

            # new component
            (root / "b").mkdir()
            (root / "b" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg_new = {
                "project": "p",
                "components": {
                    "a": {"path": "a", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}},
                    "b": {"path": "b", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}},
                },
                "slices": {},
            }
            new_issues = boundary_lock.verify_lockfile(cfg_new, lock, root, source="working-tree")
            self.assertTrue(any("NEW component not in lockfile: b" in i for i in new_issues), new_issues)

            # removed component
            cfg_removed = {"project": "p", "components": {}, "slices": {}}
            removed_issues = boundary_lock.verify_lockfile(cfg_removed, lock, root, source="working-tree")
            self.assertTrue(any("REMOVED component still in lockfile: a" in i for i in removed_issues), removed_issues)

    def test_diff_lockfiles_uses_boundary_first_summary(self):
        old = {
            "components": {
                "svc": {"version": "1.0.0", "fingerprints": {"exact": "x1", "boundary": "b1", "compat": "c1"}}
            },
            "slices": {},
        }
        new = {
            "components": {
                "svc": {"version": "1.0.1", "fingerprints": {"exact": "x1", "boundary": "b2", "compat": "c1"}}
            },
            "slices": {},
        }
        diff = boundary_lock.diff_lockfiles(old, new)
        self.assertEqual(len(diff["components"]["changed"]), 1)
        self.assertEqual(
            diff["components"]["changed"][0]["summary"],
            "declared boundary changed (compatibility unchanged)",
        )

    def test_generate_lockfile_reports_boundary_digest_failures(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}},
                "slices": {},
            }
            original = boundary_lock.boundary_paths_digest
            try:
                boundary_lock.boundary_paths_digest = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("denied"))
                lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree", strict=False)
            finally:
                boundary_lock.boundary_paths_digest = original
            comp = lock["components"]["svc"]
            self.assertEqual(comp["boundary_status"], "error")
            self.assertTrue(any("Boundary digest failed" in e for e in comp.get("boundary_errors", [])))

    def test_hash_guardrail_reports_large_file_boundary_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("x" * 200)
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}},
                "slices": {},
            }
            old_limit = boundary_lock.MAX_HASH_FILE_BYTES
            try:
                boundary_lock.MAX_HASH_FILE_BYTES = 16
                lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree", strict=False)
            finally:
                boundary_lock.MAX_HASH_FILE_BYTES = old_limit
            comp = lock["components"]["svc"]
            self.assertEqual(comp["boundary_status"], "error")
            self.assertTrue(any("Hash guardrail exceeded" in e for e in comp.get("boundary_errors", [])))

    def test_hash_guardrail_applies_to_head_source_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("x" * 200)
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True, text=True)
            (root / "svc" / "api.yaml").unlink()  # ensure HEAD path is used, not working tree stat
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}},
                "slices": {},
            }
            old_limit = boundary_lock.MAX_HASH_FILE_BYTES
            try:
                boundary_lock.MAX_HASH_FILE_BYTES = 16
                lock = boundary_lock.generate_lockfile(cfg, root, source="head", strict=False)
            finally:
                boundary_lock.MAX_HASH_FILE_BYTES = old_limit
            self.assertEqual(lock["components"]["svc"]["boundary_status"], "error")
            self.assertTrue(
                any("Hash guardrail exceeded" in e for e in lock["components"]["svc"].get("boundary_errors", []))
            )

    def test_internal_change_updates_exact_but_not_api(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            comp_dir = root / "svc"
            comp_dir.mkdir(parents=True)
            (comp_dir / "main.py").write_text("print('v1')\n")
            (comp_dir / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                    }
                },
                "slices": {},
            }
            prev_cwd = Path.cwd()
            os.chdir(root)
            try:
                lock_before = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
                (comp_dir / "main.py").write_text("print('v2')\n")
                lock_after = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            finally:
                os.chdir(prev_cwd)
            self.assertNotEqual(
                lock_before["components"]["svc"]["fingerprints"]["exact"],
                lock_after["components"]["svc"]["fingerprints"]["exact"],
            )
            self.assertEqual(
                lock_before["components"]["svc"]["fingerprints"]["boundary"],
                lock_after["components"]["svc"]["fingerprints"]["boundary"],
            )

    def test_boundary_change_updates_exact_and_api_not_compat(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            comp_dir = root / "svc"
            comp_dir.mkdir(parents=True)
            (comp_dir / "main.py").write_text("print('v1')\n")
            (comp_dir / "api.yaml").write_text("openapi: 3.0.0\n")
            (comp_dir / "version.json").write_text('{"version":"1.2.3"}\n')
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "version_source": {"file": "version.json", "field": "version"},
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                    }
                },
                "slices": {},
            }
            prev_cwd = Path.cwd()
            os.chdir(root)
            try:
                lock_before = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
                (comp_dir / "api.yaml").write_text("openapi: 3.0.1\n")
                lock_after = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            finally:
                os.chdir(prev_cwd)
            before = lock_before["components"]["svc"]["fingerprints"]
            after = lock_after["components"]["svc"]["fingerprints"]
            self.assertNotEqual(before["exact"], after["exact"])
            self.assertNotEqual(before["boundary"], after["boundary"])
            self.assertEqual(before["compat"], after["compat"])

    def test_major_version_bump_updates_compat(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            comp_dir = root / "svc"
            comp_dir.mkdir(parents=True)
            (comp_dir / "main.py").write_text("print('v1')\n")
            (comp_dir / "api.yaml").write_text("openapi: 3.0.0\n")
            (comp_dir / "version.json").write_text('{"version":"1.2.3"}\n')
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "version_source": {"file": "version.json", "field": "version"},
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                    }
                },
                "slices": {},
            }
            prev_cwd = Path.cwd()
            os.chdir(root)
            try:
                lock_before = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
                (comp_dir / "version.json").write_text('{"version":"2.0.0"}\n')
                lock_after = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            finally:
                os.chdir(prev_cwd)
            self.assertNotEqual(
                lock_before["components"]["svc"]["fingerprints"]["compat"],
                lock_after["components"]["svc"]["fingerprints"]["compat"],
            )

    def test_adding_unrelated_component_does_not_change_existing_slice(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            a_dir = root / "a"
            a_dir.mkdir(parents=True)
            (a_dir / "main.py").write_text("print('a')\n")
            b_dir = root / "b"
            b_dir.mkdir(parents=True)
            (b_dir / "main.py").write_text("print('b')\n")
            cfg_before = {
                "project": "p",
                "components": {"a": {"path": "a", "boundary": {"provider": "implicit", "paths": []}}},
                "slices": {"a-slice": {"mode": "exact", "components": ["a"]}},
            }
            cfg_after = {
                "project": "p",
                "components": {
                    "a": {"path": "a", "boundary": {"provider": "implicit", "paths": []}},
                    "b": {"path": "b", "boundary": {"provider": "implicit", "paths": []}},
                },
                "slices": {"a-slice": {"mode": "exact", "components": ["a"]}},
            }
            prev_cwd = Path.cwd()
            os.chdir(root)
            try:
                lock_before = boundary_lock.generate_lockfile(cfg_before, root, source="working-tree")
                lock_after = boundary_lock.generate_lockfile(cfg_after, root, source="working-tree")
            finally:
                os.chdir(prev_cwd)
            self.assertEqual(
                lock_before["slices"]["a-slice"]["fingerprint"],
                lock_after["slices"]["a-slice"]["fingerprint"],
            )

    def test_boundary_slice_with_missing_boundary_fails_in_strict_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comp_dir = root / "svc"
            comp_dir.mkdir(parents=True)
            (comp_dir / "main.py").write_text("print('v1')\n")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["missing.yaml"]},
                    }
                },
                "slices": {"boundary-slice": {"mode": "boundary", "components": ["svc"]}},
            }
            with self.assertRaises(ValueError):
                boundary_lock.generate_lockfile(cfg, root, source="working-tree", strict=True)

    def test_source_hashing_independent_of_cwd_for_head_and_index(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            comp_dir = root / "svc"
            comp_dir.mkdir(parents=True)
            (comp_dir / "main.py").write_text("print('v1')\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True, text=True)

            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit", "paths": []}}},
                "slices": {},
            }
            outside_cwd = Path("/tmp")
            prev_cwd = Path.cwd()
            os.chdir(outside_cwd)
            try:
                head_lock = boundary_lock.generate_lockfile(cfg, root, source="head")
                idx_lock = boundary_lock.generate_lockfile(cfg, root, source="index")
                wt_lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            finally:
                os.chdir(prev_cwd)

            exact_head = head_lock["components"]["svc"]["fingerprints"]["exact"]
            exact_idx = idx_lock["components"]["svc"]["fingerprints"]["exact"]
            exact_wt = wt_lock["components"]["svc"]["fingerprints"]["exact"]
            self.assertIsNotNone(exact_head)
            self.assertEqual(exact_head, exact_idx)
            self.assertEqual(exact_idx, exact_wt)


if __name__ == "__main__":
    unittest.main()
