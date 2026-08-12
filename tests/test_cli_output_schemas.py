"""
Tests verifying that each CLI --format json output conforms to the
corresponding spec/cli-output.*.schema.json contract.

These tests run against real tmpdir git repos so every key that the schema
declares as required is actually present in live output.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec"

# Skip the entire module if jsonschema is not installed.
try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


def _load_schema(name: str) -> dict:
    return json.loads((SPEC / name).read_text())


def _assert_valid(schema: dict, instance: dict) -> None:
    jsonschema.validate(instance, schema)


def _init_repo(root: Path) -> None:
    for cmd in [
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ]:
        subprocess.run(cmd, cwd=root, check=True, capture_output=True)


def _commit_all(root: Path, msg: str = "init") -> None:
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=root, check=True, capture_output=True)


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema not installed")
class TestCLIOutputSchemas(unittest.TestCase):
    def _make_repo(self) -> tuple:
        """Return (tmpdir_path, cfg, lock) for a minimal one-component repo."""
        td = tempfile.mkdtemp()
        root = Path(td)
        _init_repo(root)
        svc = root / "svc"
        svc.mkdir()
        (svc / "api.yaml").write_text("openapi: 3.0.0\n")
        (svc / "main.py").write_text("# v1\n")
        cfg = {
            "project": "test",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                }
            },
            "slices": {
                "all": {"mode": "exact", "components": ["svc"]}
            },
        }
        (root / "boundary.config.json").write_text(json.dumps(cfg))
        _commit_all(root)
        return root, cfg

    def _run_cli(self, root: Path, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run(
            [sys.executable, "-m", "boundver", *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_verify_output_schema(self):
        schema = _load_schema("cli-output.verify.schema.json")
        root, _cfg = self._make_repo()
        try:
            generated = self._run_cli(root, "generate", "--format", "json")
            self.assertEqual(generated.returncode, 0, generated.stderr)
            verified = self._run_cli(root, "verify", "--format", "json")
            self.assertEqual(verified.returncode, 0, verified.stderr)
            _assert_valid(schema, json.loads(verified.stdout))

            filtered = self._run_cli(
                root, "verify", "--components", "svc", "--format", "json"
            )
            self.assertEqual(filtered.returncode, 0, filtered.stderr)
            _assert_valid(schema, json.loads(filtered.stdout))
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_status_output_schema(self):
        schema = _load_schema("cli-output.status.schema.json")
        root, _cfg = self._make_repo()
        try:
            generated = self._run_cli(root, "generate", "--format", "json")
            self.assertEqual(generated.returncode, 0, generated.stderr)
            status = self._run_cli(root, "status", "--format", "json")
            self.assertEqual(status.returncode, 0, status.stderr)
            _assert_valid(schema, json.loads(status.stdout))
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_diff_output_schema(self):
        schema = _load_schema("cli-output.diff.schema.json")
        root, cfg = self._make_repo()
        try:
            from boundver.core import generate_lockfile, diff_lockfiles
            lock_a = generate_lockfile(cfg, root, source="head")
            # modify content to produce a real diff
            (root / "svc" / "main.py").write_text("# v2\n")
            _commit_all(root, "bump")
            lock_b = generate_lockfile(cfg, root, source="head")
            for diff_out in [
                diff_lockfiles(lock_a, lock_b),
                diff_lockfiles(lock_a, lock_a),  # no-change case
            ]:
                _assert_valid(schema, diff_out)
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_discover_output_schema(self):
        schema = _load_schema("cli-output.discover.schema.json")
        root, cfg = self._make_repo()
        try:
            from boundver.core import discover_components
            disc = discover_components(root)
            output = {"count": len(disc), "components": disc}
            _assert_valid(schema, output)
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_verify_schema_rejects_missing_ok(self):
        schema = _load_schema("cli-output.verify.schema.json")
        with self.assertRaises(jsonschema.ValidationError):
            _assert_valid(schema, {"issues": [], "components_filter": []})  # missing "ok"

    def test_diff_schema_rejects_unknown_component_keys(self):
        schema = _load_schema("cli-output.diff.schema.json")
        with self.assertRaises(jsonschema.ValidationError):
            _assert_valid(schema, {
                "components": {"added": [], "removed": [], "changed": [], "unchanged": [], "extra": []},
                "slices": {"changed": [], "unchanged": []},
            })


if __name__ == "__main__":
    unittest.main()
