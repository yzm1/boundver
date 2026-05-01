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


if __name__ == "__main__":
    unittest.main()
