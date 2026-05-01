import tempfile
import unittest
from pathlib import Path

import boundary_lock


class BoundaryLockTests(unittest.TestCase):
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
                    "boundary": {"kind": "leaf", "paths": []},
                }
            },
            "slices": {"s1": {"mode": "api", "components": ["x"]}},
        }
        errs = boundary_lock.validate_config(cfg, Path.cwd())
        self.assertTrue(any("api mode cannot include" in e for e in errs))

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
                        "boundary": {"kind": "implicit", "paths": []},
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
                        "boundary": {"kind": "openapi", "paths": ["api.yaml"]},
                    }
                },
                "slices": {},
            }
            lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            entry = lock["components"]["svc"]
            self.assertEqual(entry["boundary_status"], "ok")
            self.assertIsNotNone(entry["fingerprints"]["api"])

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
                        "boundary": {"kind": "openapi", "paths": []},
                    }
                },
                "slices": {},
            }
            lock = boundary_lock.generate_lockfile(cfg, root, source="working-tree")
            entry = lock["components"]["svc"]
            self.assertEqual(entry["boundary_status"], "error")
            self.assertIn(
                "No boundary paths declared for explicit boundary kind",
                entry.get("boundary_errors", []),
            )


if __name__ == "__main__":
    unittest.main()
