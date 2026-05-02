import tempfile
import unittest
from pathlib import Path
import subprocess
import os

import boundver.core as boundary_lock


class BoundaryLockTests(unittest.TestCase):
    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True, capture_output=True, text=True)

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

    def test_validate_config_api_slice_leaf_component(self):
        cfg = {
            "components": {
                "x": {
                    "path": "service/x",
                    "boundary": {"provider": "leaf", "paths": []},
                }
            },
            "slices": {"s1": {"mode": "api", "components": ["x"]}},
        }
        errs = boundary_lock.validate_config(cfg, Path.cwd())
        self.assertTrue(any("api mode cannot include" in e for e in errs))

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
            self.assertIsNotNone(entry["fingerprints"]["api"])
            self.assertEqual(entry["fingerprints"]["boundary"], entry["fingerprints"]["api"])

    def test_generate_lockfile_normalizes_api_slice_mode(self):
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
                "slices": {"s1": {"mode": "api", "components": ["svc"]}},
            }
            lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            self.assertEqual(lock["slices"]["s1"]["mode"], "boundary")

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
                lock_before["components"]["svc"]["fingerprints"]["api"],
                lock_after["components"]["svc"]["fingerprints"]["api"],
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
            self.assertNotEqual(before["api"], after["api"])
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

    def test_api_slice_with_missing_boundary_fails_in_strict_mode(self):
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
                "slices": {"api-slice": {"mode": "api", "components": ["svc"]}},
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
