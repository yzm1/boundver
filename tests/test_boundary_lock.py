import tempfile
import unittest
from pathlib import Path
import subprocess
import os
import json
import io
import sys
from contextlib import redirect_stdout

import boundver.core as boundary_lock
import boundver
import boundver.versions as versions
from tests._repo_fixtures import commit_all, init_git_repo


class BoundaryLockTests(unittest.TestCase):

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
            [sys.executable, "-m", "boundver", *args],
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

    def test_validate_config_allows_boundary_slice_leaf_component_for_partial_lock(self):
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
        self.assertFalse(any("boundary mode cannot include" in e for e in errs))

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

    def test_validate_config_requires_top_level_fields(self):
        errs = boundary_lock.validate_config({}, Path.cwd())
        self.assertTrue(any("Missing required top-level field: project" in e for e in errs))
        self.assertTrue(any("Missing required top-level field: components" in e for e in errs))
        # slices are optional
        self.assertFalse(any("Missing required top-level field: slices" in e for e in errs))

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

    def test_validate_config_rejects_invalid_behavior_paths_without_schema_engine(self):
        import boundver._config as _config_mod

        cfg = {
            "project": "x",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "openapi", "paths": []},
                    "behavior": {"paths": "config.json"},
                }
            },
            "slices": {},
        }

        original = _config_mod._schema_engine_errors
        try:
            _config_mod._schema_engine_errors = lambda cfg, schema: []
            errs = boundary_lock.validate_config(cfg, Path.cwd())
        finally:
            _config_mod._schema_engine_errors = original

        self.assertTrue(any("behavior.paths' must be an array of strings" in e for e in errs), errs)

    def test_validate_config_invalid_path_type_does_not_crash_without_schema_engine(self):
        import boundver._config as _config_mod

        cfg = {
            "project": "x",
            "components": {
                "svc": {
                    "path": 123,
                    "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                    "behavior": {"paths": ["config.json"]},
                }
            },
            "slices": {},
        }

        original = _config_mod._schema_engine_errors
        try:
            _config_mod._schema_engine_errors = lambda cfg, schema: []
            errs = boundary_lock.validate_config(cfg, Path.cwd())
        finally:
            _config_mod._schema_engine_errors = original

        self.assertTrue(any("field 'path' must be a non-empty string" in e for e in errs), errs)

    def test_config_warnings_flags_behavior_not_covering_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0\n")
            (root / "svc" / "config.json").write_text('{"timeout": 30}\n')
            cfg = {
                "project": "x",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["config.json"]},
                    }
                },
                "slices": {},
            }

            warnings = boundary_lock.config_warnings(cfg, root)

            self.assertTrue(any("behavior.paths does not currently cover boundary files" in w for w in warnings), warnings)
            self.assertTrue(any("api.yaml" in w for w in warnings), warnings)

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
        import boundver._config as _config_mod
        original = _config_mod._schema_engine_errors
        try:
            _config_mod._schema_engine_errors = lambda cfg, schema: ["Schema validation error at <root>: boom"]
            cfg = {"project": "x", "components": {}, "slices": {}}
            errs = boundary_lock.validate_config(cfg, Path.cwd())
            self.assertTrue(any("Schema validation error at <root>: boom" in e for e in errs))
        finally:
            _config_mod._schema_engine_errors = original

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
            self.assertTrue(
                any(
                    "must not contain '..' path segments" in error
                    and "escapes" in error
                    for error in errs
                ),
                errs,
            )

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
            lock = boundary_lock.generate_lockfile(
                cfg, root, source="working-tree", strict=False
            )
            entry = lock["components"]["svc"]
            self.assertEqual(entry["boundary_status"], "partial")
            self.assertIn("No boundary paths declared for implicit boundary", entry.get("boundary_errors", []))

    def test_explain_component_changes_reports_boundary_relevant_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            comp = root / "svc"
            comp.mkdir(parents=True)
            (comp / "openapi.yaml").write_text("openapi: 3.0.0\n")
            (comp / "impl.py").write_text("print('ok')\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True, text=True)

            (comp / "openapi.yaml").write_text("openapi: 3.1.0\n")
            (comp / "impl.py").write_text("print('changed')\n")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["openapi.yaml"]},
                    }
                },
                "slices": {},
            }
            out = io.StringIO()
            with redirect_stdout(out):
                rc = boundary_lock.explain_component_changes(cfg, root, "svc", source="working-tree")
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("Changed files (2):", text)
            self.assertIn("svc/openapi.yaml", text)
            self.assertIn("Boundary-relevant changed files (1):", text)

    def test_explain_component_changes_unknown_component(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = {"project": "p", "components": {}, "slices": {}}
            rc = boundary_lock.explain_component_changes(cfg, root, "missing")
            self.assertEqual(rc, 2)

    def test_verify_lockfile_components_filter_scopes_mismatches(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a_dir = root / "a"
            b_dir = root / "b"
            a_dir.mkdir(parents=True)
            b_dir.mkdir(parents=True)
            (a_dir / "api.yaml").write_text("v1")
            (b_dir / "api.yaml").write_text("v1")
            cfg = {
                "project": "p",
                "components": {
                    "a": {"path": "a", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}},
                    "b": {"path": "b", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}},
                },
                "slices": {},
            }
            lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            (b_dir / "api.yaml").write_text("v2")

            issues_all = boundary_lock.verify_lockfile(cfg, lock, root, source="working-tree")
            self.assertTrue(any(i.startswith("MISMATCH b.") for i in issues_all), issues_all)

            issues_a_only = boundary_lock.verify_lockfile(
                cfg, lock, root, source="working-tree", components_filter=["a"]
            )
            self.assertEqual(issues_a_only, [])

    def test_generate_lockfile_for_components_updates_only_selected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a_dir = root / "a"
            b_dir = root / "b"
            a_dir.mkdir(parents=True)
            b_dir.mkdir(parents=True)
            (a_dir / "api.yaml").write_text("a-v1")
            (b_dir / "api.yaml").write_text("b-v1")
            cfg = {
                "project": "p",
                "components": {
                    "a": {"path": "a", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}},
                    "b": {"path": "b", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}},
                },
                "slices": {
                    "only-a": {"mode": "boundary", "components": ["a"]},
                    "only-b": {"mode": "boundary", "components": ["b"]},
                },
            }
            out = root / "boundary.lock.json"
            full = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            out.write_text(json.dumps(full, indent=2))
            old_b_exact = full["components"]["b"]["fingerprints"]["exact"]
            old_b_slice = full["slices"]["only-b"]["fingerprint"]

            (a_dir / "api.yaml").write_text("a-v2")
            partial = boundary_lock.generate_lockfile_for_components(
                cfg, root, selected_components=["a"], out_path=out, source="working-tree"
            )

            self.assertNotEqual(
                partial["components"]["a"]["fingerprints"]["exact"],
                full["components"]["a"]["fingerprints"]["exact"],
            )
            self.assertEqual(partial["components"]["b"]["fingerprints"]["exact"], old_b_exact)
            self.assertEqual(partial["slices"]["only-b"]["fingerprint"], old_b_slice)

    def test_discover_components_finds_common_manifests(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "services" / "auth").mkdir(parents=True)
            (root / "libs" / "core").mkdir(parents=True)
            (root / "services" / "auth" / "package.json").write_text('{"version":"1.0.0"}')
            (root / "libs" / "core" / "pyproject.toml").write_text("[project]\nversion='0.1.0'\n")
            comps = boundary_lock.discover_components(root)
            self.assertIn("auth", comps)
            self.assertIn("core", comps)
            self.assertEqual(comps["auth"]["path"], "services/auth")
            self.assertEqual(comps["core"]["path"], "libs/core")
            # version_source.file must be relative to component path, not repo root
            self.assertEqual(comps["auth"]["version_source"]["file"], "package.json")
            self.assertEqual(comps["auth"]["version_source"]["field"], "version")
            self.assertEqual(comps["core"]["version_source"]["file"], "pyproject.toml")
            self.assertEqual(comps["core"]["version_source"]["field"], "project.version")

    def test_discover_components_uses_tracked_manifests_and_deduplicates_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / ".gitignore").write_text("ignored/\n")
            (root / "packages" / "real").mkdir(parents=True)
            (root / "packages" / "real" / "package.json").write_text(
                '{"version":"2.0.0"}'
            )
            (root / "packages" / "real" / "pyproject.toml").write_text(
                "[project]\nversion='2.0.0'\n"
            )
            (root / "crates" / "engine").mkdir(parents=True)
            (root / "crates" / "engine" / "Cargo.toml").write_text(
                "[package]\nversion='3.1.4'\n"
            )
            (root / "cmd" / "tool").mkdir(parents=True)
            (root / "cmd" / "tool" / "go.mod").write_text(
                "module example.com/tool\n"
            )
            (root / "ignored" / "dep").mkdir(parents=True)
            (root / "ignored" / "dep" / "package.json").write_text(
                '{"version":"9.9.9"}'
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "manifests"],
                cwd=root,
                check=True,
                capture_output=True,
            )

            components = boundary_lock.discover_components(root)

            self.assertEqual(set(components), {"real", "engine", "tool"})
            self.assertEqual(
                components["real"]["version_source"],
                {"file": "package.json", "field": "version"},
            )
            self.assertEqual(
                components["engine"]["version_source"],
                {"file": "Cargo.toml", "field": "package.version"},
            )
            self.assertIsNone(components["tool"]["version_source"])

    def test_discover_components_excludes_ignored_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "node_modules" / "dep").mkdir(parents=True)
            (root / "node_modules" / "dep" / "package.json").write_text('{"version":"1.0.0"}')
            (root / ".venv" / "lib" / "pkg").mkdir(parents=True)
            (root / ".venv" / "lib" / "pkg" / "pyproject.toml").write_text("[project]\nversion='0.1.0'\n")
            (root / "__pycache__" / "cached").mkdir(parents=True)
            (root / "__pycache__" / "cached" / "pyproject.toml").write_text("[project]\nversion='0.1.0'\n")
            (root / "vendor" / "copied").mkdir(parents=True)
            (root / "vendor" / "copied" / "package.json").write_text('{"version":"9.9.9"}')
            (root / "packages" / "real").mkdir(parents=True)
            (root / "packages" / "real" / "package.json").write_text('{"version":"2.0.0"}')
            comps = boundary_lock.discover_components(root)
            self.assertEqual(list(comps.keys()), ["real"])

    def test_init_discover_creates_config_with_discovered_components(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir(parents=True)
            (root / "svc" / "go.mod").write_text("module example.com/svc\n")
            proc = self._run_cli(root, "init", "--discover")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            cfg = json.loads((root / "boundary.config.json").read_text())
            self.assertIn("svc", cfg["components"])
            # Slices are optional and not generated by default anymore
            self.assertNotIn("slices", cfg)

    def test_changed_components_since_ref_detects_modified_component_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "a").mkdir()
            (root / "b").mkdir()
            (root / "a" / "x.txt").write_text("a1")
            (root / "b" / "y.txt").write_text("b1")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True, text=True)
            cfg = {
                "project": "p",
                "components": {
                    "a": {"path": "a", "boundary": {"provider": "implicit", "paths": []}},
                    "b": {"path": "b", "boundary": {"provider": "implicit", "paths": []}},
                },
                "slices": {},
            }
            (root / "b" / "y.txt").write_text("b2")
            changed = boundary_lock.changed_components_since_ref(cfg, root, "HEAD")
            self.assertEqual(changed, ["b"])

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

    def test_boundary_digest_deduplicates_overlapping_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comp = root / "svc"
            comp.mkdir(parents=True)
            (comp / "api").mkdir()
            (comp / "api" / "openapi.yaml").write_text("openapi: 3.0.0\n")
            cfg_base = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api"]}}},
                "slices": {},
            }
            cfg_overlap = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api", "api/openapi.yaml"]}}},
                "slices": {},
            }
            lock1 = boundary_lock.generate_lockfile(cfg_base, root, source="working-tree")
            lock2 = boundary_lock.generate_lockfile(cfg_overlap, root, source="working-tree")
            d1 = lock1["components"]["svc"]["fingerprints"]["boundary"]
            d2 = lock2["components"]["svc"]["fingerprints"]["boundary"]
            self.assertEqual(d1, d2)

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
            with self.assertRaisesRegex(
                ValueError, "No boundary paths declared for explicit boundary provider"
            ):
                boundary_lock.generate_lockfile(
                    cfg, root, source="working-tree", strict=False
                )

    def test_exact_hash_matches_between_head_and_working_tree_for_same_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
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
            init_git_repo(root)
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
            init_git_repo(root)
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
            init_git_repo(root)
            (root / "app.py").write_text("print('base')\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "tag", "svc-v1.2.3"], cwd=root, check=True, capture_output=True, text=True)

            # Capture initial branch name before switching.
            initial_branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=root, capture_output=True, text=True,
            ).stdout.strip() or "master"

            # Create an orphan branch with a higher tag that should be unreachable from current HEAD.
            subprocess.run(["git", "checkout", "--orphan", "other"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "rm", "-rf", "."], cwd=root, check=True, capture_output=True, text=True)
            (root / "other.py").write_text("print('other')\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "other"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "tag", "svc-v9.9.9"], cwd=root, check=True, capture_output=True, text=True)

            subprocess.run(["git", "checkout", initial_branch], cwd=root, check=True, capture_output=True, text=True)

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

    def test_extract_yaml_field_fails_closed_without_parser(self):
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
            self.assertIsNone(version)

    def test_init_writes_schema_and_refuses_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
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
            init_git_repo(root)
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
            init_git_repo(root)
            (root / "src").mkdir()
            init_res = self._run_cli(root, "init")
            self.assertEqual(init_res.returncode, 0, init_res.stderr)
            validate_res = self._run_cli(root, "validate-config")
            self.assertEqual(validate_res.returncode, 0, validate_res.stderr)

    def test_generate_dry_run_does_not_write_lockfile(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "$schema": "https://raw.githubusercontent.com/yzm1/boundver/v0.11.0/boundary.config.schema.json",
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
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "$schema": "https://raw.githubusercontent.com/yzm1/boundver/v0.11.0/boundary.config.schema.json",
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg, indent=2) + "\n")
            gen = self._run_cli(root, "generate", "--source", "working-tree")
            self.assertEqual(gen.returncode, 0, gen.stderr)
            ver = self._run_cli(root, "verify", "--source", "working-tree", "--format", "json")
            self.assertEqual(ver.returncode, 0, ver.stderr)
            payload = json.loads(ver.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["issues"], [])

    def test_verify_verbose_prints_diagnostics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "$schema": "https://raw.githubusercontent.com/yzm1/boundver/v0.11.0/boundary.config.schema.json",
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
            init_git_repo(root)
            res = self._run_cli(root, "--quiet", "--verbose", "status")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("not allowed with argument", res.stderr)

    def test_status_json_returns_single_json_payload_with_issues(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "$schema": "https://raw.githubusercontent.com/yzm1/boundver/v0.11.0/boundary.config.schema.json",
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg, indent=2) + "\n")
            self.assertEqual(self._run_cli(root, "generate", "--source", "working-tree").returncode, 0)
            (root / "svc" / "api.yaml").write_text("openapi: 3.1.0\n")
            res = self._run_cli(root, "status", "--source", "working-tree", "--format", "json")
            self.assertEqual(res.returncode, 0, res.stderr)
            payload = json.loads(res.stdout)
            self.assertIn("lockfile", payload)
            self.assertIn("issues", payload)
            self.assertTrue(any("MISMATCH svc.boundary" in i for i in payload["issues"]))

    def test_examples_expected_lockfiles_are_current(self):
        repo_root = Path(__file__).resolve().parents[1]
        examples = [
            "behavior",
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
                cfg, repo_root, source="head", strict=False
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
            good = boundary_lock.generate_lockfile(cfg, root, source="working-tree")

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
            lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree")

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
            cfg_removed = {
                "project": "p",
                "components": {
                    "b": {
                        "path": "b",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                    }
                },
                "slices": {},
            }
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
            "declared boundary changed; compatibility family is unchanged",
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
            import boundver._lockfile as _lockfile_mod
            import boundver.providers as _providers_mod
            original = _providers_mod.compute_boundary
            try:
                _lockfile_mod.compute_boundary = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("denied"))
                with self.assertRaisesRegex(ValueError, "Boundary digest failed"):
                    boundary_lock.generate_lockfile(
                        cfg, root, source="working-tree", strict=False
                    )
            finally:
                _lockfile_mod.compute_boundary = original

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
            import boundver._hashing as _hashing_mod
            old_limit = _hashing_mod.MAX_HASH_FILE_BYTES
            try:
                _hashing_mod.MAX_HASH_FILE_BYTES = 16
                with self.assertRaisesRegex(ValueError, "Hash guardrail exceeded"):
                    boundary_lock.generate_lockfile(
                        cfg, root, source="working-tree", strict=False
                    )
            finally:
                _hashing_mod.MAX_HASH_FILE_BYTES = old_limit

    def test_hash_guardrail_applies_to_head_source_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
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
            import boundver._hashing as _hashing_mod
            old_limit = _hashing_mod.MAX_HASH_FILE_BYTES
            try:
                _hashing_mod.MAX_HASH_FILE_BYTES = 16
                with self.assertRaisesRegex(ValueError, "Hash guardrail exceeded"):
                    boundary_lock.generate_lockfile(
                        cfg, root, source="head", strict=False
                    )
            finally:
                _hashing_mod.MAX_HASH_FILE_BYTES = old_limit

    def test_internal_change_updates_exact_but_not_api(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
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
            init_git_repo(root)
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

    def test_symlink_content_matches_between_head_and_working_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            comp = root / "svc"
            comp.mkdir(parents=True)
            target = comp / "target.txt"
            target.write_text("payload\n")
            link = comp / "link.txt"
            try:
                os.symlink("target.txt", link)
            except (OSError, NotImplementedError):
                self.skipTest("symlink not supported in this environment")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "symlink"], cwd=root, check=True, capture_output=True, text=True)
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["link.txt"]}}},
                "slices": {},
            }
            head_lock = boundary_lock.generate_lockfile(cfg, root, source="head")
            wt_lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            self.assertEqual(
                head_lock["components"]["svc"]["fingerprints"]["exact"],
                wt_lock["components"]["svc"]["fingerprints"]["exact"],
            )
            self.assertEqual(
                head_lock["components"]["svc"]["fingerprints"]["boundary"],
                wt_lock["components"]["svc"]["fingerprints"]["boundary"],
            )

    def test_major_version_bump_updates_compat(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
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
            init_git_repo(root)
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
            init_git_repo(root)
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
            outside_cwd = Path(tempfile.gettempdir())
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

    def test_component_without_version_source_has_none_version(self):
        """A component with no version_source produces version=None without crashing."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comp_dir = root / "svc"
            comp_dir.mkdir(parents=True)
            (comp_dir / "main.py").write_text("print('x')\n")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit", "paths": []},
                        # no version_source key
                    }
                },
                "slices": {},
            }
            lock = boundary_lock.generate_lockfile(
                cfg, root, source="working-tree", strict=False
            )
            self.assertIsNone(lock["components"]["svc"]["version"])

    def test_vendored_copy_drift_fails_strict_generation(self):
        """A divergent vendored copy cannot be blessed by strict generation."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            svc_dir = root / "svc"
            vendor_dir = root / "vendor" / "svc"
            svc_dir.mkdir(parents=True)
            vendor_dir.mkdir(parents=True)
            (svc_dir / "main.py").write_text("print('v1')\n")
            (vendor_dir / "main.py").write_text("print('STALE')\n")  # intentionally different
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit", "paths": []},
                        "vendored_copies": ["vendor/svc"],
                    }
                },
                "slices": {},
            }
            with self.assertRaisesRegex(ValueError, "differs from source"):
                boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            with self.assertRaisesRegex(ValueError, "differs from source"):
                boundary_lock.generate_lockfile(
                    cfg, root, source="working-tree", strict=False
                )

    def test_vendored_copy_in_sync_has_no_warning(self):
        """When vendored copy matches source, no warning is produced."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            svc_dir = root / "svc"
            vendor_dir = root / "vendor" / "svc"
            svc_dir.mkdir(parents=True)
            vendor_dir.mkdir(parents=True)
            (svc_dir / "main.py").write_text("print('v1')\n")
            (vendor_dir / "main.py").write_text("print('v1')\n")  # identical
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit", "paths": []},
                        "vendored_copies": ["vendor/svc"],
                    }
                },
                "slices": {},
            }
            lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            entry = lock["components"]["svc"]
            self.assertNotIn("warnings", entry)

    def test_repo_with_no_commits_rejects_head_source(self):
        """HEAD source fails closed when no commit can be captured."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            comp_dir = root / "svc"
            comp_dir.mkdir(parents=True)
            (comp_dir / "main.py").write_text("print('x')\n")
            # Files exist but are not committed
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
            with self.assertRaisesRegex(ValueError, "HEAD does not resolve"):
                boundary_lock.generate_lockfile(
                    cfg, root, source="head", strict=False
                )

    def test_format_json_flag_accepted_by_verify(self):
        """--format json is accepted and produces JSON output."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg, indent=2) + "\n")
            self.assertEqual(self._run_cli(root, "generate", "--source", "working-tree").returncode, 0)
            res = self._run_cli(root, "verify", "--source", "working-tree", "--format", "json")
            self.assertEqual(res.returncode, 0, res.stderr)
            payload = json.loads(res.stdout)
            self.assertTrue(payload["ok"])

    def test_format_json_flag_accepted_by_diff(self):
        """--format json is accepted by diff and produces JSON."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg, indent=2) + "\n")
            self.assertEqual(self._run_cli(root, "generate", "--source", "working-tree").returncode, 0)
            lock_path = root / "boundary.lock.json"
            res = self._run_cli(root, "diff", str(lock_path), str(lock_path), "--format", "json")
            self.assertEqual(res.returncode, 0, res.stderr)
            payload = json.loads(res.stdout)
            self.assertIn("components", payload)


    def test_version_source_missing_file_reported_as_error(self):
        """validate_config reports an actionable error when version_source.file does not exist."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        "version_source": {"file": "pyproject.toml", "field": "tool.poetry.version"},
                    }
                },
                "slices": {},
            }
            errors = boundary_lock.validate_config(cfg, root)
            self.assertTrue(
                any("version_source.file not found" in e for e in errors),
                f"Expected 'version_source.file not found' error, got: {errors}",
            )

    def test_version_source_missing_field_key_reported_as_error(self):
        """validate_config reports an error when version_source has 'file' but no 'field'."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "pyproject.toml").write_text("[tool.poetry]\nversion = \"1.0.0\"\n")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        "version_source": {"file": "pyproject.toml"},
                    }
                },
                "slices": {},
            }
            errors = boundary_lock.validate_config(cfg, root)
            self.assertTrue(
                any("no 'field'" in e for e in errors),
                f"Expected missing 'field' error, got: {errors}",
            )

    def test_version_source_unsupported_extension_reported_as_error(self):
        """validate_config reports an error for an unsupported version_source.file extension."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "VERSION").write_text("1.0.0\n")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        "version_source": {"file": "VERSION", "field": "version"},
                    }
                },
                "slices": {},
            }
            errors = boundary_lock.validate_config(cfg, root)
            self.assertTrue(
                any("unsupported extension" in e for e in errors),
                f"Expected unsupported extension error, got: {errors}",
            )

    def test_version_source_missing_file_or_git_tag_prefix_reported(self):
        """validate_config reports error when version_source has neither file nor git_tag_prefix."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        "version_source": {"unknown_key": "something"},
                    }
                },
                "slices": {},
            }
            errors = boundary_lock.validate_config(cfg, root)
            self.assertTrue(
                any("file" in e and "git_tag_prefix" in e for e in errors),
                f"Expected missing file/git_tag_prefix error, got: {errors}",
            )

    def test_version_source_valid_git_tag_prefix_passes(self):
        """validate_config accepts a valid git_tag_prefix version_source without errors."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        "version_source": {"git_tag_prefix": "svc-v"},
                    }
                },
                "slices": {},
            }
            errors = boundary_lock.validate_config(cfg, root)
            version_source_errors = [e for e in errors if "version_source" in e]
            self.assertEqual(version_source_errors, [], f"Unexpected version_source errors: {version_source_errors}")

    def test_version_source_valid_file_passes(self):
        """validate_config accepts an existing version_source.file with field."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "package.json").write_text('{"version": "1.2.3"}\n')
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        "version_source": {"file": "package.json", "field": "version"},
                    }
                },
                "slices": {},
            }
            errors = boundary_lock.validate_config(cfg, root)
            version_source_errors = [e for e in errors if "version_source" in e]
            self.assertEqual(version_source_errors, [], f"Unexpected version_source errors: {version_source_errors}")

    def test_validate_config_reports_missing_boundary_path_file(self):
        """validate_config emits an actionable error with component+file path when boundary file is absent."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["openapi.yaml"]},
                    }
                },
                "slices": {},
            }
            errors = boundary_lock.validate_config(cfg, root)
            self.assertTrue(
                any("svc/openapi.yaml" in e for e in errors),
                f"Expected component-path/file in error, got: {errors}",
            )
            self.assertTrue(
                any("ensure the file exists" in e for e in errors),
                f"Expected actionable guidance in error, got: {errors}",
            )

    # ------------------------------------------------------------------
    # CLI: slice command
    # ------------------------------------------------------------------

    def test_cli_slice_shows_fingerprint(self):
        """slice command prints fingerprint for a named slice."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}},
                "slices": {"s1": {"mode": "exact", "components": ["svc"]}},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            self._run_cli(root, "generate", "--source", "working-tree")
            res = self._run_cli(root, "slice", "s1")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("s1", res.stdout)
            self.assertIn("exact", res.stdout)

    def test_cli_slice_not_found_exits_nonzero(self):
        """slice command exits 1 when slice name is unknown."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            self._run_cli(root, "generate", "--source", "working-tree")
            res = self._run_cli(root, "slice", "no-such-slice")
            self.assertNotEqual(res.returncode, 0)

    # ------------------------------------------------------------------
    # CLI: validate-config
    # ------------------------------------------------------------------

    def test_cli_validate_config_exits_zero_for_valid_config(self):
        """validate-config returns 0 for a valid config."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            res = self._run_cli(root, "validate-config")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("valid", res.stdout.lower())

    def test_cli_validate_config_exits_nonzero_for_invalid_config(self):
        """validate-config returns non-zero for a config with errors."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "unknown-provider"}}
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            res = self._run_cli(root, "validate-config")
            self.assertNotEqual(res.returncode, 0)

    def test_cli_validate_config_missing_file_exits_nonzero(self):
        """validate-config exits non-zero when config file does not exist."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            res = self._run_cli(root, "validate-config", "--config", "nonexistent.json")
            self.assertNotEqual(res.returncode, 0)

    # ------------------------------------------------------------------
    # CLI: explain command
    # ------------------------------------------------------------------

    def test_cli_explain_unknown_component_exits_2(self):
        """explain exits 2 when component name is not in config."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            commit_all(root, "add config")
            res = self._run_cli(root, "explain", "no-such-component")
            self.assertEqual(res.returncode, 2)

    def test_cli_explain_known_component_exits_zero(self):
        """explain exits 0 for a known component with no changes."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            commit_all(root, "add config")
            res = self._run_cli(root, "explain", "svc")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("svc", res.stdout)

    # ------------------------------------------------------------------
    # CLI: generate --format json and --verbose
    # ------------------------------------------------------------------

    def test_cli_generate_format_json_outputs_lockfile(self):
        """generate --format json prints the lockfile JSON to stdout."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            res = self._run_cli(root, "--quiet", "generate", "--source", "working-tree", "--format", "json")
            self.assertEqual(res.returncode, 0, res.stderr)
            payload = json.loads(res.stdout)
            self.assertIn("components", payload)
            self.assertIn("svc", payload["components"])

    def test_cli_generate_verbose_prints_diagnostics(self):
        """generate --verbose prints source info."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            res = self._run_cli(root, "--verbose", "generate", "--source", "working-tree")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("source=", res.stdout)

    # ------------------------------------------------------------------
    # CLI: verify --changed-from and unknown --components
    # ------------------------------------------------------------------

    def test_cli_verify_changed_from_auto_selects_components(self):
        """verify --changed-from skips components with no changes."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            self._run_cli(root, "generate", "--source", "working-tree")
            # No changes since HEAD — verify should pass with changed-from
            res = self._run_cli(root, "verify", "--source", "working-tree", "--changed-from", "HEAD")
            self.assertEqual(res.returncode, 0, res.stderr)

    def test_cli_verify_unknown_components_exits_2(self):
        """verify --components with unknown name exits 2."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            self._run_cli(root, "generate", "--source", "working-tree")
            res = self._run_cli(root, "verify", "--source", "working-tree", "--components", "no-such-comp")
            self.assertEqual(res.returncode, 2)

    # ------------------------------------------------------------------
    # CLI: discover --format json
    # ------------------------------------------------------------------

    def test_cli_discover_format_json(self):
        """discover --format json returns JSON with count and components."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "package.json").write_text('{"name":"svc","version":"1.0.0"}\n')
            res = self._run_cli(root, "discover", "--format", "json")
            self.assertEqual(res.returncode, 0, res.stderr)
            payload = json.loads(res.stdout)
            self.assertIn("count", payload)
            self.assertIn("components", payload)
            self.assertGreater(payload["count"], 0)

    def test_cli_discover_text_format(self):
        """discover text output lists component names."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "package.json").write_text('{"name":"svc","version":"1.0.0"}\n')
            res = self._run_cli(root, "discover")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("Discovered", res.stdout)

    # ------------------------------------------------------------------
    # CLI: status with no lockfile
    # ------------------------------------------------------------------

    def test_cli_status_no_lockfile_exits_nonzero(self):
        """status exits non-zero when no lockfile exists."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            res = self._run_cli(root, "status")
            self.assertNotEqual(res.returncode, 0)

    # ------------------------------------------------------------------
    # diff_lockfiles: boundary-only and compat change summaries
    # ------------------------------------------------------------------

    def test_diff_lockfiles_boundary_only_change_summary(self):
        """diff_lockfiles summarizes boundary-only changes correctly."""
        old = {
            "schema": "boundary-lock/v1",
            "components": {
                "svc": {
                    "version": "1.0.0",
                    "fingerprints": {"exact": "aaa", "boundary": "bbb", "compat": "ccc"},
                }
            },
            "slices": {},
        }
        new = {
            "schema": "boundary-lock/v1",
            "components": {
                "svc": {
                    "version": "1.0.0",
                    "fingerprints": {"exact": "aaa", "boundary": "bbb2", "compat": "ccc"},
                }
            },
            "slices": {},
        }
        result = boundary_lock.diff_lockfiles(old, new)
        changed = result["components"]["changed"]
        self.assertEqual(len(changed), 1)
        self.assertIn("boundary", changed[0]["summary"].lower())

    def test_diff_lockfiles_compat_change_summary(self):
        """diff_lockfiles summarizes compat-breaking changes as BREAKING."""
        old = {
            "schema": "boundary-lock/v1",
            "components": {
                "svc": {
                    "version": "1.0.0",
                    "fingerprints": {"exact": "aaa", "boundary": "bbb", "compat": "ccc"},
                }
            },
            "slices": {},
        }
        new = {
            "schema": "boundary-lock/v1",
            "components": {
                "svc": {
                    "version": "2.0.0",
                    "fingerprints": {"exact": "aaa2", "boundary": "bbb2", "compat": "ccc2"},
                }
            },
            "slices": {},
        }
        result = boundary_lock.diff_lockfiles(old, new)
        changed = result["components"]["changed"]
        self.assertEqual(len(changed), 1)
        self.assertIn("BREAKING", changed[0]["summary"])

    def test_diff_lockfiles_exact_only_change_summary(self):
        """diff_lockfiles labels exact-only changes as implementation-only."""
        old = {
            "schema": "boundary-lock/v1",
            "components": {
                "svc": {
                    "version": "1.0.0",
                    "fingerprints": {"exact": "aaa", "boundary": "bbb", "compat": "ccc"},
                }
            },
            "slices": {},
        }
        new = {
            "schema": "boundary-lock/v1",
            "components": {
                "svc": {
                    "version": "1.0.0",
                    "fingerprints": {"exact": "aaa2", "boundary": "bbb", "compat": "ccc"},
                }
            },
            "slices": {},
        }
        result = boundary_lock.diff_lockfiles(old, new)
        changed = result["components"]["changed"]
        self.assertEqual(len(changed), 1)
        self.assertIn("implementation", changed[0]["summary"].lower())

    def test_diff_lockfiles_slice_change_detected(self):
        """diff_lockfiles detects changed slice fingerprints."""
        old = {"schema": "boundary-lock/v1", "components": {}, "slices": {"s1": {"fingerprint": "aaa"}}}
        new = {"schema": "boundary-lock/v1", "components": {}, "slices": {"s1": {"fingerprint": "bbb"}}}
        result = boundary_lock.diff_lockfiles(old, new)
        self.assertEqual(len(result["slices"]["changed"]), 1)
        self.assertEqual(result["slices"]["changed"][0]["name"], "s1")

    def test_diff_lockfiles_added_and_removed_components(self):
        """diff_lockfiles correctly categorises added and removed components."""
        old = {
            "schema": "boundary-lock/v1",
            "components": {
                "removed": {"version": "1.0", "fingerprints": {"exact": "aaa", "boundary": None, "compat": None}},
            },
            "slices": {},
        }
        new = {
            "schema": "boundary-lock/v1",
            "components": {
                "added": {"version": "1.0", "fingerprints": {"exact": "bbb", "boundary": None, "compat": None}},
            },
            "slices": {},
        }
        result = boundary_lock.diff_lockfiles(old, new)
        self.assertEqual([c["name"] for c in result["components"]["added"]], ["added"])
        self.assertEqual([c["name"] for c in result["components"]["removed"]], ["removed"])

    # ------------------------------------------------------------------
    # print_diff / print_status — smoke tests (no crash, correct output)
    # ------------------------------------------------------------------

    def test_print_diff_produces_output(self):
        """print_diff doesn't crash and produces some output."""
        diff = {
            "components": {
                "added": [{"name": "new-svc", "version": "1.0"}],
                "removed": [{"name": "old-svc", "version": "0.9"}],
                "changed": [
                    {
                        "name": "svc",
                        "old_version": "1.0",
                        "new_version": "2.0",
                        "summary": "BREAKING: compatibility family changed",
                        "changed_facets": {
                            "exact": {"old": "aaa", "new": "bbb"},
                            "compat": {"old": "ccc", "new": "ddd"},
                        },
                    }
                ],
                "unchanged": ["stable-svc"],
            },
            "slices": {
                "changed": [{"name": "s1", "old": "xxx", "new": "yyy"}],
                "unchanged": ["s2"],
            },
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            boundary_lock.print_diff(diff)
        out = buf.getvalue()
        self.assertIn("new-svc", out)
        self.assertIn("old-svc", out)
        self.assertIn("svc", out)
        self.assertIn("UNCHANGED", out)  # count shown, not each name

    def test_print_status_produces_output(self):
        """print_status doesn't crash and includes project name."""
        lockfile = {
            "project": "myproject",
            "components": {
                "svc": {
                    "version": "1.0.0",
                    "boundary_provider": "openapi",
                    "boundary_status": "ok",
                    "fingerprints": {"exact": "abc", "boundary": "def", "compat": "ghi"},
                    "warnings": ["Vendored copy at vendor/svc differs from source"],
                    "boundary_errors": [],
                }
            },
            "slices": {
                "s1": {
                    "mode": "exact",
                    "components": ["svc"],
                    "fingerprint": "fp123",
                    "component_digests": {"svc": "abc"},
                }
            },
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            boundary_lock.print_status(lockfile)
        out = buf.getvalue()
        self.assertIn("myproject", out)
        self.assertIn("svc", out)

    # ------------------------------------------------------------------
    # changed_components_since_ref: no changes case
    # ------------------------------------------------------------------

    def test_changed_components_since_ref_no_changes(self):
        """changed_components_since_ref returns empty list when nothing changed."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}},
                "slices": {},
            }
            result = boundary_lock.changed_components_since_ref(cfg, root, "HEAD")
            self.assertEqual(result, [])

    # ------------------------------------------------------------------
    # _list_files_for_source: filesystem fallback (no git)
    # ------------------------------------------------------------------

    def test_list_files_for_source_working_tree_filesystem_fallback(self):
        """source_tree_digest works on working-tree without git tracking."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # No git init — filesystem fallback path
            svc = root / "svc"
            svc.mkdir()
            (svc / "main.py").write_text("x=1\n")
            digest = boundary_lock.source_tree_digest(root, "svc", source="working-tree")
            self.assertIsNotNone(digest)
            self.assertEqual(len(digest), 64)

    def test_list_files_for_source_filesystem_fallback_ignores_pyc(self):
        """Working-tree filesystem fallback ignores .pyc files."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            svc = root / "svc"
            svc.mkdir()
            (svc / "main.py").write_text("x=1\n")
            digest1 = boundary_lock.source_tree_digest(root, "svc", source="working-tree")
            (svc / "main.pyc").write_bytes(b"\x00\x01")  # .pyc should be ignored
            digest2 = boundary_lock.source_tree_digest(root, "svc", source="working-tree")
            self.assertEqual(digest1, digest2)

    # ------------------------------------------------------------------
    # git_latest_tag: reachable tag lookup
    # ------------------------------------------------------------------

    def test_git_latest_tag_returns_reachable_tag(self):
        """git_latest_tag returns a tag reachable from the selected commit."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            # Create and tag the selected orphan history.
            subprocess.run(["git", "checkout", "--orphan", "orphan-branch"], cwd=root, check=True, capture_output=True)
            (root / "f.txt").write_text("v\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "tag", "svc-v1.2.3"], cwd=root, check=True, capture_output=True)
            result = boundary_lock.git_latest_tag(root, "svc-v")
            self.assertEqual(result, "1.2.3")

    # ------------------------------------------------------------------
    # validate_config: slice with unknown mode
    # ------------------------------------------------------------------

    def test_validate_config_slice_unknown_mode(self):
        """validate_config reports an error for an unknown slice mode."""
        cfg = {
            "project": "p",
            "components": {
                "svc": {"path": "svc", "boundary": {"provider": "implicit"}}
            },
            "slices": {
                "s1": {"mode": "super-mode", "components": ["svc"]}
            },
        }
        errors = boundary_lock.validate_config(cfg, Path("."))
        self.assertTrue(any("unknown mode" in e for e in errors))

    def test_validate_config_slice_references_unknown_component(self):
        """validate_config reports error when slice references a nonexistent component."""
        cfg = {
            "project": "p",
            "components": {},
            "slices": {"s1": {"mode": "exact", "components": ["ghost"]}},
        }
        errors = boundary_lock.validate_config(cfg, Path("."))
        self.assertTrue(any("ghost" in e for e in errors))

    # ------------------------------------------------------------------
    # generate_lockfile_for_components: compat/boundary slice modes
    # ------------------------------------------------------------------

    def test_generate_lockfile_compat_slice_mode(self):
        """generate_lockfile produces a slice with compat mode."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\ninfo:\n  version: 1.0.0\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "tag", "svc-v1.0.0"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "version_source": {"git_tag_prefix": "svc-v"},
                    }
                },
                "slices": {"compat-slice": {"mode": "compat", "components": ["svc"]}},
            }
            lock = boundary_lock.generate_lockfile(cfg, root, source="head", strict=False)
            self.assertIn("compat-slice", lock["slices"])
            self.assertEqual(lock["slices"]["compat-slice"]["mode"], "compat")

    # ------------------------------------------------------------------
    # CLI: completions subcommand
    # ------------------------------------------------------------------

    def test_completions_bash_outputs_completion_script(self):
        """completions --shell bash prints a bash completion function."""
        res = self._run_cli(Path("."), "completions", "--shell", "bash")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("_boundver_completions", res.stdout)
        self.assertIn("complete -F _boundver_completions boundver", res.stdout)

    def test_completions_zsh_outputs_completion_script(self):
        """completions --shell zsh prints a zsh _boundver function."""
        res = self._run_cli(Path("."), "completions", "--shell", "zsh")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("#compdef boundver", res.stdout)
        self.assertIn("_boundver", res.stdout)

    def test_completions_fish_outputs_completion_script(self):
        """completions --shell fish prints fish complete commands."""
        res = self._run_cli(Path("."), "completions", "--shell", "fish")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("complete -c boundver", res.stdout)
        self.assertIn("generate", res.stdout)

    def test_completions_works_outside_git_repo(self):
        """completions does not require a git repository."""
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            # Use a plain directory — no git init
            res = self._run_cli(Path(td), "completions", "--shell", "bash")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("_boundver_completions", res.stdout)

    def test_completions_requires_shell_argument(self):
        """completions without --shell exits non-zero."""
        res = self._run_cli(Path("."), "completions")
        self.assertNotEqual(res.returncode, 0)

    def test_completions_covers_all_subcommands(self):
        """All CLI subcommands appear in each completion script."""
        subcommands = [
            "generate", "verify", "diff", "slice", "validate-config",
            "init", "status", "explain", "discover", "completions",
        ]
        for shell in ("bash", "zsh", "fish"):
            res = self._run_cli(Path("."), "completions", "--shell", shell)
            self.assertEqual(res.returncode, 0, f"{shell}: {res.stderr}")
            for cmd in subcommands:
                self.assertIn(cmd, res.stdout, f"{shell} completion missing '{cmd}'")

    # ------------------------------------------------------------------
    # explain_component_changes: boundary paths branch
    # ------------------------------------------------------------------

    def test_explain_component_with_boundary_paths_shows_boundary_section(self):
        """explain shows boundary-relevant changed files when boundary paths declared."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            (root / "svc" / "impl.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            # Modify a boundary-relevant file
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.1\n")
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
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            rc = boundary_lock.explain_component_changes(cfg, root, "svc", base_ref="HEAD", source="working-tree")
            self.assertEqual(rc, 0)

    def test_explain_component_with_boundary_paths_no_boundary_changes(self):
        """explain shows 'no boundary-relevant changes' when only impl files changed."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            (root / "svc" / "impl.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            # Modify only a non-boundary file
            (root / "svc" / "impl.py").write_text("x = 2\n")
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
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = boundary_lock.explain_component_changes(cfg, root, "svc", base_ref="HEAD", source="working-tree")
            self.assertEqual(rc, 0)
            self.assertIn("none", buf.getvalue().lower())

    # ------------------------------------------------------------------
    # validate_config: additional edge cases
    # ------------------------------------------------------------------

    def test_validate_config_rejects_non_dict_component(self):
        """validate_config reports error when a component value is not an object."""
        cfg = {
            "project": "p",
            "components": {"svc": "not-a-dict"},
            "slices": {},
        }
        errors = boundary_lock.validate_config(cfg, Path("."))
        self.assertTrue(any("must be an object" in e for e in errors))

    def test_validate_config_rejects_component_missing_path(self):
        """validate_config reports error when component has no path field."""
        cfg = {
            "project": "p",
            "components": {"svc": {"boundary": {"provider": "implicit"}}},
            "slices": {},
        }
        errors = boundary_lock.validate_config(cfg, Path("."))
        self.assertTrue(any("missing required field: path" in e for e in errors))

    def test_validate_config_rejects_empty_path(self):
        """validate_config reports error for empty path string."""
        cfg = {
            "project": "p",
            "components": {"svc": {"path": "  ", "boundary": {"provider": "implicit"}}},
            "slices": {},
        }
        errors = boundary_lock.validate_config(cfg, Path("."))
        self.assertTrue(any("non-empty string" in e for e in errors))

    def test_validate_config_rejects_non_dict_boundary(self):
        """validate_config reports error when boundary is not an object."""
        cfg = {
            "project": "p",
            "components": {"svc": {"path": "svc", "boundary": "implicit"}},
            "slices": {},
        }
        errors = boundary_lock.validate_config(cfg, Path("."))
        self.assertTrue(any("boundary must be an object" in e for e in errors))

    def test_validate_config_rejects_empty_provider_string(self):
        """validate_config reports error when boundary.provider is an empty string."""
        cfg = {
            "project": "p",
            "components": {"svc": {"path": "svc", "boundary": {"provider": ""}}},
            "slices": {},
        }
        errors = boundary_lock.validate_config(cfg, Path("."))
        self.assertTrue(any("non-empty string" in e for e in errors))

    def test_validate_config_accepts_custom_namespace_provider(self):
        """validate_config accepts providers in the custom.* namespace."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "custom.my-provider"}}},
                "slices": {},
            }
            errors = boundary_lock.validate_config(cfg, root)
            provider_errors = [e for e in errors if "unsupported boundary.provider" in e]
            self.assertEqual(provider_errors, [])

    # ------------------------------------------------------------------
    # Phase 2 — providers top-level key validation
    # ------------------------------------------------------------------

    def test_validate_config_accepts_valid_providers_list(self):
        """validate_config accepts a well-formed top-level providers array."""
        cfg = {
            "project": "p",
            "components": {},
            "slices": {},
            "providers": [
                {"module": "some.module", "class": "SomeClass"},
            ],
        }
        errors = boundary_lock.validate_config(cfg, Path("."))
        provider_errs = [e for e in errors if "providers" in e.lower()]
        self.assertEqual(provider_errs, [])

    def test_validate_config_rejects_providers_not_list(self):
        """validate_config rejects providers that is not an array."""
        cfg = {
            "project": "p",
            "components": {},
            "slices": {},
            "providers": {"module": "a", "class": "B"},
        }
        errors = boundary_lock.validate_config(cfg, Path("."))
        self.assertTrue(any("providers" in e.lower() and "array" in e.lower() for e in errors))

    def test_validate_config_rejects_providers_entry_missing_fields(self):
        """validate_config rejects providers entry missing module or class."""
        cfg = {
            "project": "p",
            "components": {},
            "slices": {},
            "providers": [{"module": "a.module"}],  # missing class
        }
        errors = boundary_lock.validate_config(cfg, Path("."))
        self.assertTrue(any("class" in e for e in errors))

    def test_validate_config_rejects_providers_entry_non_dict(self):
        """validate_config rejects providers entry that is not an object."""
        cfg = {
            "project": "p",
            "components": {},
            "slices": {},
            "providers": ["not-a-dict"],
        }
        errors = boundary_lock.validate_config(cfg, Path("."))
        self.assertTrue(any("providers" in e.lower() for e in errors))

    def test_generate_lockfile_errors_when_custom_provider_without_flag(self):
        """generate_lockfile raises ValueError when providers list requires --allow-custom-providers."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}},
                "slices": {},
                "providers": [{"module": "some.module", "class": "SomeClass"}],
            }
            with self.assertRaises(ValueError) as cm:
                boundary_lock.generate_lockfile(cfg, root, source="working-tree",
                                                allow_custom_providers=False)
            self.assertIn("--allow-custom-providers", str(cm.exception))

    def test_generate_lockfile_with_boundary_options_passthrough(self):
        """boundary.options is passed through to provider via ProviderContext.boundary_cfg."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "openapi",
                            "paths": ["api.yaml"],
                            "options": {"normalize": True},
                        },
                    }
                },
                "slices": {},
            }
            lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            # Should succeed; options are stored in config and passed through boundary_cfg
            self.assertEqual(lock["components"]["svc"]["boundary_status"], "ok")
            self.assertIsNotNone(lock["components"]["svc"]["fingerprints"]["boundary"])

    def test_validate_config_rejects_non_dict_slice(self):
        """validate_config reports error when slice value is not an object."""
        cfg = {
            "project": "p",
            "components": {},
            "slices": {"s1": "not-a-dict"},
        }
        errors = boundary_lock.validate_config(cfg, Path("."))
        self.assertTrue(any("must be an object" in e for e in errors))

    def test_validate_config_rejects_non_dict_components_field(self):
        """validate_config reports error when components field is not an object."""
        cfg = {"project": "p", "components": ["a", "b"], "slices": {}}
        errors = boundary_lock.validate_config(cfg, Path("."))
        self.assertTrue(any("'components' must be an object" in e for e in errors))

    def test_validate_config_rejects_non_dict_slices_field(self):
        """validate_config reports error when slices field is not an object."""
        cfg = {"project": "p", "components": {}, "slices": ["s1"]}
        errors = boundary_lock.validate_config(cfg, Path("."))
        self.assertTrue(any("'slices' must be an object" in e for e in errors))

    def test_validate_config_rejects_empty_git_tag_prefix(self):
        """validate_config reports error for an empty git_tag_prefix."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        "version_source": {"git_tag_prefix": ""},
                    }
                },
                "slices": {},
            }
            errors = boundary_lock.validate_config(cfg, root)
            self.assertTrue(any("git_tag_prefix" in e for e in errors))

    def test_validate_config_rejects_non_dict_version_source(self):
        """validate_config reports error when version_source is not an object."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        "version_source": "package.json",
                    }
                },
                "slices": {},
            }
            errors = boundary_lock.validate_config(cfg, root)
            self.assertTrue(any("version_source" in e and "object" in e for e in errors))

    # ------------------------------------------------------------------
    # generate_lockfile: exact digest error path and boundary error paths
    # ------------------------------------------------------------------

    def test_generate_lockfile_leaf_provider_boundary_ok(self):
        """leaf provider produces boundary_status=ok even with no boundary paths."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "leaf"}}},
                "slices": {},
            }
            lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            self.assertEqual(lock["components"]["svc"]["boundary_status"], "ok")

    def test_generate_lockfile_explicit_provider_no_paths_is_error(self):
        """A named provider with no boundary paths produces boundary_status=error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": []}}
                },
                "slices": {},
            }
            with self.assertRaisesRegex(
                ValueError, "No boundary paths declared for explicit boundary provider"
            ):
                boundary_lock.generate_lockfile(
                    cfg, root, source="working-tree", strict=False
                )

    # ------------------------------------------------------------------
    # Phase 3 — semantic provider integration tests
    # ------------------------------------------------------------------

    def test_json_canonical_provider_stable_on_key_reordering(self):
        """json-canonical: same content in different key order → same boundary digest."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "json-canonical", "paths": ["contract.json"]},
                    }
                },
                "slices": {},
            }
            (root / "svc" / "contract.json").write_text('{"b":2,"a":1}')
            lock1 = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            (root / "svc" / "contract.json").write_text('{"a":1,"b":2}')
            lock2 = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            fp1 = lock1["components"]["svc"]["fingerprints"]["boundary"]
            fp2 = lock2["components"]["svc"]["fingerprints"]["boundary"]
            self.assertIsNotNone(fp1)
            self.assertEqual(fp1, fp2, "key reordering should not change boundary digest")

    def test_json_canonical_provider_detects_value_change(self):
        """json-canonical: changing a value changes the boundary digest."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "json-canonical", "paths": ["contract.json"]},
                    }
                },
                "slices": {},
            }
            (root / "svc" / "contract.json").write_text('{"version":1}')
            lock1 = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            (root / "svc" / "contract.json").write_text('{"version":2}')
            lock2 = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            fp1 = lock1["components"]["svc"]["fingerprints"]["boundary"]
            fp2 = lock2["components"]["svc"]["fingerprints"]["boundary"]
            self.assertNotEqual(fp1, fp2)

    def test_openapi_canonical_stable_on_description_change(self):
        """openapi-canonical: description-only change does not alter boundary digest."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi-canonical", "paths": ["openapi.yaml"]},
                    }
                },
                "slices": {},
            }
            api_v1 = (
                "openapi: '3.0.0'\n"
                "paths:\n"
                "  /ping:\n"
                "    get:\n"
                "      description: Original description.\n"
                "      operationId: ping\n"
                "      responses:\n"
                "        '200':\n"
                "          description: OK\n"
            )
            api_v2 = (
                "openapi: '3.0.0'\n"
                "paths:\n"
                "  /ping:\n"
                "    get:\n"
                "      description: Completely different docs!\n"
                "      operationId: ping\n"
                "      responses:\n"
                "        '200':\n"
                "          description: Success\n"
            )
            (root / "svc" / "openapi.yaml").write_text(api_v1)
            lock1 = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            (root / "svc" / "openapi.yaml").write_text(api_v2)
            lock2 = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            fp1 = lock1["components"]["svc"]["fingerprints"]["boundary"]
            fp2 = lock2["components"]["svc"]["fingerprints"]["boundary"]
            self.assertIsNotNone(fp1)
            self.assertEqual(fp1, fp2, "description-only change should not alter boundary digest")

    def test_openapi_canonical_detects_endpoint_addition(self):
        """openapi-canonical: adding an endpoint changes the boundary digest."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi-canonical", "paths": ["openapi.yaml"]},
                    }
                },
                "slices": {},
            }
            api_v1 = (
                "openapi: '3.0.0'\n"
                "paths:\n"
                "  /ping:\n"
                "    get:\n"
                "      operationId: ping\n"
                "      responses:\n"
                "        '200': {}\n"
            )
            api_v2 = api_v1 + (
                "  /pong:\n"
                "    get:\n"
                "      operationId: pong\n"
                "      responses:\n"
                "        '200': {}\n"
            )
            (root / "svc" / "openapi.yaml").write_text(api_v1)
            lock1 = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            (root / "svc" / "openapi.yaml").write_text(api_v2)
            lock2 = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            fp1 = lock1["components"]["svc"]["fingerprints"]["boundary"]
            fp2 = lock2["components"]["svc"]["fingerprints"]["boundary"]
            self.assertNotEqual(fp1, fp2)

    def test_validate_config_accepts_openapi_canonical_provider(self):
        """validate_config accepts openapi-canonical as a known provider."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "openapi-canonical"}}},
                "slices": {},
            }
            errors = boundary_lock.validate_config(cfg, root)
            provider_errors = [e for e in errors if "unsupported" in e]
            self.assertEqual(provider_errors, [])

    def test_validate_config_accepts_json_canonical_provider(self):
        """validate_config accepts json-canonical as a known provider."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "json-canonical"}}},
                "slices": {},
            }
            errors = boundary_lock.validate_config(cfg, root)
            provider_errors = [e for e in errors if "unsupported" in e]
            self.assertEqual(provider_errors, [])

    # ------------------------------------------------------------------
    # boundary.config.yaml / .toml loading (Phase: config format support)
    # ------------------------------------------------------------------

    def test_load_config_file_parses_json(self):
        """load_config_file handles JSON config files."""
        from boundver._config import load_config_file
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "boundary.config.json"
            p.write_text('{"project": "x", "components": {}, "slices": {}}')
            cfg = load_config_file(p)
            self.assertEqual(cfg["project"], "x")

    def test_load_config_file_parses_yaml(self):
        """load_config_file handles YAML config files."""
        from boundver._config import load_config_file
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "boundary.config.yaml"
            p.write_text("project: myproject\ncomponents: {}\nslices: {}\n")
            try:
                cfg = load_config_file(p)
                self.assertEqual(cfg["project"], "myproject")
            except ValueError as exc:
                if "PyYAML" in str(exc):
                    self.skipTest("PyYAML not installed")
                raise

    def test_load_config_file_yaml_non_dict_raises(self):
        """load_config_file raises ValueError when YAML root is not a mapping."""
        from boundver._config import load_config_file
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "boundary.config.yaml"
            p.write_text("- item1\n- item2\n")
            try:
                with self.assertRaises(ValueError) as cm:
                    load_config_file(p)
                self.assertIn("mapping", str(cm.exception))
            except ValueError as exc:
                if "PyYAML" not in str(exc):
                    raise
                self.skipTest("PyYAML not installed")

    def test_load_config_file_invalid_json_raises(self):
        """load_config_file raises ValueError on JSON syntax error."""
        from boundver._config import load_config_file
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("{not valid json}")
            with self.assertRaises(ValueError) as cm:
                load_config_file(p)
            self.assertIn("JSON parse error", str(cm.exception))

    def test_load_config_file_missing_file_raises(self):
        """load_config_file raises FileNotFoundError for missing files."""
        from boundver._config import load_config_file
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "missing.json"
            with self.assertRaises(FileNotFoundError):
                load_config_file(p)

    def test_load_config_file_unsupported_extension_raises(self):
        """load_config_file raises ValueError for unsupported extensions."""
        from boundver._config import load_config_file
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "boundary.config.xml"
            p.write_text("<config/>")
            with self.assertRaises(ValueError) as cm:
                load_config_file(p)
            self.assertIn("Unsupported", str(cm.exception))

    def test_find_config_file_prefers_json_when_present(self):
        """find_config_file returns JSON when both JSON and YAML exist."""
        from boundver._config import find_config_file
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "boundary.config.json").write_text("{}")
            (root / "boundary.config.yaml").write_text("project: x")
            result = find_config_file(root, "boundary.config.json")
            self.assertEqual(result.name, "boundary.config.json")

    def test_find_config_file_falls_back_to_yaml(self):
        """find_config_file discovers .yaml when .json is absent."""
        from boundver._config import find_config_file
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "boundary.config.yaml").write_text("project: x")
            result = find_config_file(root, "boundary.config.json")
            self.assertEqual(result.name, "boundary.config.yaml")

    def test_find_config_file_falls_back_to_yml(self):
        """find_config_file discovers .yml when .json and .yaml are absent."""
        from boundver._config import find_config_file
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "boundary.config.yml").write_text("project: x")
            result = find_config_file(root, "boundary.config.json")
            self.assertEqual(result.name, "boundary.config.yml")

    def test_find_config_file_explicit_path_not_probed(self):
        """find_config_file does not probe alternatives for explicit (non-default) paths."""
        from boundver._config import find_config_file
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "boundary.config.yaml").write_text("project: x")
            # Explicit non-default name — should return as-is even if missing
            result = find_config_file(root, "custom.config.json")
            self.assertEqual(result.name, "custom.config.json")

    def test_yaml_config_generates_lockfile(self):
        """Full generate flow works with a YAML config file."""
        from boundver._config import load_config_file, validate_config
        from boundver._lockfile import generate_lockfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x = 1\n")
            cfg_yaml = (
                "project: myproject\n"
                "components:\n"
                "  svc:\n"
                "    path: svc\n"
                "    boundary:\n"
                "      provider: implicit\n"
                "      paths: []\n"
                "slices: {}\n"
            )
            cfg_path = root / "boundary.config.yaml"
            cfg_path.write_text(cfg_yaml)
            try:
                cfg = load_config_file(cfg_path)
            except ValueError as exc:
                if "PyYAML" in str(exc):
                    self.skipTest("PyYAML not installed")
                raise
            errors = validate_config(cfg, root)
            self.assertEqual(errors, [])
            lock = generate_lockfile(cfg, root, source="working-tree")
            self.assertEqual(lock["project"], "myproject")
            self.assertIn("svc", lock["components"])

    # ------------------------------------------------------------------
    # changed_components_since_ref: git failure path
    # ------------------------------------------------------------------

    def test_changed_components_since_ref_bad_ref_raises(self):
        """An invalid baseline must fail closed instead of skipping verification."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}},
                "slices": {},
            }
            with self.assertRaisesRegex(ValueError, "nonexistent-ref-12345"):
                boundary_lock.changed_components_since_ref(
                    cfg, root, "nonexistent-ref-12345"
                )

    # ------------------------------------------------------------------
    # _lockfile_schema_issues
    # ------------------------------------------------------------------

    def test_lockfile_schema_issues_missing_schema(self):
        """_lockfile_schema_issues reports missing schema field."""
        issues = boundary_lock._lockfile_schema_issues({"components": {}, "slices": {}})
        self.assertTrue(any("schema missing" in i for i in issues))

    def test_lockfile_schema_issues_wrong_schema(self):
        """_lockfile_schema_issues reports unsupported schema version."""
        issues = boundary_lock._lockfile_schema_issues({"schema": "boundary-lock/v99"})
        self.assertTrue(any("unsupported" in i for i in issues))

    def test_lockfile_schema_issues_correct_schema_passes(self):
        """_lockfile_schema_issues returns empty for correct schema."""
        issues = boundary_lock._lockfile_schema_issues({"schema": "boundary-lock/v3"})
        self.assertEqual(issues, [])


class TestWhyComponent(unittest.TestCase):
    """Tests for why_component() in _output.py."""

    def _build_fixture(self, td: str):
        """Create a git repo with one component and generate a lockfile. Returns (root, config, lockfile)."""
        root = Path(td)
        init_git_repo(root)
        (root / "svc").mkdir()
        (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
        commit_all(root)
        config = {
            "project": "proj",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                }
            },
            "slices": {},
        }
        lockfile = boundary_lock.generate_lockfile(config, root)
        return root, config, lockfile

    def test_unknown_component_returns_2(self):
        with tempfile.TemporaryDirectory() as td:
            root, config, lockfile = self._build_fixture(td)
            import io as _io
            err = _io.StringIO()
            from contextlib import redirect_stderr
            with redirect_stderr(err):
                rc = boundary_lock.why_component(config, lockfile, root, "missing")
            self.assertEqual(rc, 2)
            self.assertIn("unknown component", err.getvalue())

    def test_component_not_in_lockfile_returns_2(self):
        with tempfile.TemporaryDirectory() as td:
            root, config, _ = self._build_fixture(td)
            empty_lockfile = {"components": {}, "slices": {}}
            out = io.StringIO()
            with redirect_stdout(out):
                rc = boundary_lock.why_component(config, empty_lockfile, root, "svc")
            self.assertEqual(rc, 2)

    def test_up_to_date_returns_0(self):
        with tempfile.TemporaryDirectory() as td:
            root, config, lockfile = self._build_fixture(td)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = boundary_lock.why_component(config, lockfile, root, "svc")
            self.assertEqual(rc, 0)
            self.assertIn("UP TO DATE", out.getvalue())

    def test_exact_drift_returns_1_with_drifted_message(self):
        with tempfile.TemporaryDirectory() as td:
            root, config, lockfile = self._build_fixture(td)
            # Commit impl.py at v1 so it is tracked by git
            (root / "svc" / "impl.py").write_text("x = 1\n")
            commit_all(root, "add impl v1")
            config2 = {
                "project": "proj",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                    }
                },
                "slices": {},
            }
            lockfile2 = boundary_lock.generate_lockfile(config2, root)  # HEAD has x=1
            # Commit impl.py at v2 to advance HEAD
            (root / "svc" / "impl.py").write_text("x = 2\n")
            commit_all(root, "change impl v2")
            out = io.StringIO()
            with redirect_stdout(out):
                rc = boundary_lock.why_component(config2, lockfile2, root, "svc")
            self.assertEqual(rc, 1)
            text = out.getvalue()
            self.assertIn("DRIFTED", text)
            self.assertIn("exact", text)

    def test_boundary_drift_shows_boundary_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root, config, lockfile = self._build_fixture(td)
            # Generate lockfile from working-tree so file reads come from disk
            lockfile = boundary_lock.generate_lockfile(config, root, source="working-tree")
            # Change the API (boundary drift)
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\npaths:\n  /new: {}\n")
            out = io.StringIO()
            with redirect_stdout(out):
                rc = boundary_lock.why_component(config, lockfile, root, "svc", source="working-tree")
            self.assertEqual(rc, 1)
            text = out.getvalue()
            self.assertIn("DRIFTED", text)
            self.assertIn("boundary", text)
            self.assertIn("api.yaml", text)

    def test_up_to_date_after_generate(self):
        with tempfile.TemporaryDirectory() as td:
            root, config, _ = self._build_fixture(td)
            # Regenerate so it's fresh
            fresh_lockfile = boundary_lock.generate_lockfile(config, root)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = boundary_lock.why_component(config, fresh_lockfile, root, "svc")
            self.assertEqual(rc, 0)

    def test_why_via_cli_returns_0_for_up_to_date(self):
        with tempfile.TemporaryDirectory() as td:
            root, config, lockfile = self._build_fixture(td)
            (root / "boundary.config.json").write_text(json.dumps(config))
            (root / "boundary.lock.json").write_text(json.dumps(lockfile))
            commit_all(root, "add config and lock")
            import sys as _sys
            old_argv = _sys.argv[:]
            old_dir = os.getcwd()
            os.chdir(str(root))
            try:
                _sys.argv = ["boundver", "why", "svc",
                             "--config", "boundary.config.json",
                             "--lock", "boundary.lock.json"]
                try:
                    boundary_lock.main()
                    rc = 0
                except SystemExit as e:
                    rc = e.code
            finally:
                _sys.argv = old_argv
                os.chdir(old_dir)
            self.assertEqual(rc, 0)

    def test_why_via_cli_missing_component_returns_2(self):
        with tempfile.TemporaryDirectory() as td:
            root, config, lockfile = self._build_fixture(td)
            (root / "boundary.config.json").write_text(json.dumps(config))
            (root / "boundary.lock.json").write_text(json.dumps(lockfile))
            commit_all(root, "add config and lock")
            import sys as _sys
            old_argv = _sys.argv[:]
            old_dir = os.getcwd()
            os.chdir(str(root))
            try:
                _sys.argv = ["boundver", "why", "nosuchcomp"]
                try:
                    boundary_lock.main()
                    rc = 0
                except SystemExit as e:
                    rc = e.code
            finally:
                _sys.argv = old_argv
                os.chdir(old_dir)
            self.assertEqual(rc, 2)


class MigrateLockTests(unittest.TestCase):
    """Tests for migrate_lockfile() and the migrate-lock CLI command."""

    def _minimal_v1(self, **extra):
        base = {
            "schema": "boundary-lock/v1",
            "project": "test",
            "components": {
                "svc": {
                    "path": "svc",
                    "fingerprints": {"exact": "aaa", "boundary": "bbb", "compat": "ccc"},
                }
            },
            "slices": {},
        }
        base.update(extra)
        return base

    def _minimal_v2(self, **extra):
        base = self._minimal_v1()
        base["schema"] = "boundary-lock/v2"
        base["components"]["svc"]["provider"] = {
            "name": "implicit",
            "version": "2",
        }
        base["components"]["svc"]["consumers"] = []
        base.update(extra)
        return base

    def _minimal_v3(self, **extra):
        base = self._minimal_v2()
        base["schema"] = "boundary-lock/v3"
        base["config_contract"] = "boundver-semantic-config/v2"
        base["config_digest"] = "0" * 64
        base.update(extra)
        return base

    # ------------------------------------------------------------------
    # Unit tests for migrate_lockfile()
    # ------------------------------------------------------------------

    def test_migrate_v1_requires_regeneration(self):
        from boundver._lockfile import MigrationError, migrate_lockfile

        with self.assertRaises(MigrationError) as cm:
            migrate_lockfile(self._minimal_v1())

        message = str(cm.exception)
        self.assertIn("cannot be migrated", message)
        self.assertIn("boundver generate", message)

    def test_current_v3_cleanup_strips_generated_at(self):
        from boundver._lockfile import migrate_lockfile
        lf = self._minimal_v3(generated_at="2024-01-01T00:00:00Z")
        result = migrate_lockfile(lf)
        self.assertNotIn("generated_at", result)

    def test_v3_semantic_v1_requires_regeneration(self):
        from boundver._lockfile import MigrationError, migrate_lockfile

        lockfile = self._minimal_v3()
        lockfile["config_contract"] = "boundver-semantic-config/v1"
        with self.assertRaisesRegex(MigrationError, "cannot be relabelled"):
            migrate_lockfile(lockfile)

    def test_migrate_does_not_mutate_input(self):
        from boundver._lockfile import MigrationError, migrate_lockfile
        lf = self._minimal_v1(generated_at="x")
        with self.assertRaises(MigrationError):
            migrate_lockfile(lf)
        self.assertIn("generated_at", lf)  # original untouched

    def test_current_v3_cleanup_preserves_components(self):
        from boundver._lockfile import migrate_lockfile
        result = migrate_lockfile(self._minimal_v3())
        self.assertEqual(result["components"]["svc"]["fingerprints"]["exact"], "aaa")

    def test_current_v3_cleanup_adds_missing_components_and_slices(self):
        from boundver._lockfile import migrate_lockfile
        lf = {
            "schema": "boundary-lock/v3",
            "config_contract": "boundver-semantic-config/v2",
            "project": "x",
        }
        result = migrate_lockfile(lf)
        self.assertEqual(result["components"], {})
        self.assertEqual(result["slices"], {})

    def test_migrate_v2_requires_regeneration(self):
        from boundver._lockfile import MigrationError, migrate_lockfile
        with self.assertRaisesRegex(MigrationError, "cannot be migrated"):
            migrate_lockfile(self._minimal_v2())

    def test_migrate_unknown_schema_raises(self):
        from boundver._lockfile import MigrationError, migrate_lockfile
        lf = {"schema": "boundary-lock/v99", "components": {}, "slices": {}}
        with self.assertRaises(MigrationError) as cm:
            migrate_lockfile(lf)
        self.assertIn("v99", str(cm.exception))

    def test_migrate_unknown_schema_diagnostic_is_bounded_and_type_safe(self):
        from boundver._lockfile import MigrationError, migrate_lockfile

        for schema in (["not", "hashable"], "x" * 1_000_000):
            with self.subTest(schema_type=type(schema).__name__):
                with self.assertRaises(MigrationError) as cm:
                    migrate_lockfile({"schema": schema})
                self.assertLess(len(str(cm.exception)), 5_000)

    def test_migrate_no_schema_raises(self):
        from boundver._lockfile import MigrationError, migrate_lockfile
        with self.assertRaises(MigrationError) as cm:
            migrate_lockfile({"components": {}, "slices": {}})
        self.assertIn("schema", str(cm.exception))

    def test_migrate_idempotent(self):
        """Running migrate twice gives the same result as running it once."""
        from boundver._lockfile import migrate_lockfile
        lf = self._minimal_v3(generated_at="ts")
        once = migrate_lockfile(lf)
        twice = migrate_lockfile(once)
        self.assertEqual(once, twice)

    # ------------------------------------------------------------------
    # CLI tests for migrate-lock
    # ------------------------------------------------------------------

    def _run_migrate_cli(self, lock_path, extra_args=()):
        import sys as _sys
        from boundver.core import main
        old_argv = _sys.argv[:]
        try:
            _sys.argv = ["boundver", "migrate-lock", "--lock", str(lock_path)] + list(extra_args)
            try:
                main()
                return 0
            except SystemExit as exc:
                return exc.code
        finally:
            _sys.argv = old_argv

    def test_cli_missing_lockfile_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            rc = self._run_migrate_cli(Path(td) / "no.lock.json")
            self.assertEqual(rc, 2)

    def test_cli_invalid_json_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.lock.json"
            p.write_text("{not json}")
            rc = self._run_migrate_cli(p)
            self.assertEqual(rc, 2)

    def test_cli_unknown_schema_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "boundary.lock.json"
            p.write_text(json.dumps({"schema": "boundary-lock/v99", "components": {}, "slices": {}}))
            rc = self._run_migrate_cli(p)
            self.assertEqual(rc, 2)

    def test_cli_v1_requires_regeneration_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "boundary.lock.json"
            original = json.dumps(self._minimal_v1(generated_at="2020-01-01T00:00:00Z"))
            p.write_text(original)
            rc = self._run_migrate_cli(p)
            self.assertEqual(rc, 2)
            self.assertEqual(p.read_text(), original)

    def test_cli_v2_requires_regeneration_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "boundary.lock.json"
            original = json.dumps(self._minimal_v2(generated_at="ts"))
            p.write_text(original)
            rc = self._run_migrate_cli(p)
            self.assertEqual(rc, 2)
            self.assertEqual(p.read_text(), original)

    def test_cli_current_v3_dry_run_does_not_write(self):
        import io, sys as _sys
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "boundary.lock.json"
            original = json.dumps(self._minimal_v3(generated_at="ts"))
            p.write_text(original)
            buf = io.StringIO()
            old_stdout = _sys.stdout
            _sys.stdout = buf
            try:
                self._run_migrate_cli(p, ["--dry-run"])
            finally:
                _sys.stdout = old_stdout
            self.assertEqual(p.read_text(), original)   # file unchanged
            self.assertIn('"schema"', buf.getvalue())   # JSON printed to stdout
            self.assertNotIn("generated_at", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
