"""
Tests verifying that each CLI --format json output conforms to the
corresponding spec/cli-output.*.schema.json contract.

These tests run against real tmpdir git repos so every key that the schema
declares as required is actually present in live output.
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests._repo_fixtures import commit_all, init_git_repo

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


def _empty_diff() -> dict:
    return {
        "changed_metadata": {},
        "components": {
            "added": [],
            "removed": [],
            "changed": [],
            "unchanged": [],
        },
        "slices": {
            "added": [],
            "removed": [],
            "changed": [],
            "unchanged": [],
        },
    }


def _verify_result(**overrides) -> dict:
    result = {
        "ok": True,
        "updated": False,
        "issues": [],
        "resolved_issues": [],
        "observations": [],
        "facets": None,
        "facet_policy": {
            "explicit": None,
            "defaults": None,
            "components": {},
            "slices": {},
        },
        "components_filter": [],
        "changed_components": [],
    }
    result.update(overrides)
    return result


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema not installed")
class TestCLIOutputSchemas(unittest.TestCase):
    def _make_repo(self) -> tuple:
        """Return (tmpdir_path, cfg, lock) for a minimal one-component repo."""
        td = tempfile.mkdtemp()
        root = Path(td)
        init_git_repo(
            root,
            initial_branch="main",
            user_email="test@example.com",
            user_name="Test",
        )
        svc = root / "svc"
        svc.mkdir()
        (svc / "api.yaml").write_text("openapi: 3.0.0\n")
        (svc / "main.py").write_text("# v1\n")
        cfg = {
            "project": "test",
            "defaults": {"verify_facets": ["exact", "boundary"]},
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
        commit_all(root, "init")
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
            commit_all(root, "record lock")
            verified = self._run_cli(root, "verify", "--format", "json")
            self.assertEqual(verified.returncode, 0, verified.stderr)
            _assert_valid(schema, json.loads(verified.stdout))

            filtered = self._run_cli(
                root, "verify", "--components", "svc", "--format", "json"
            )
            self.assertEqual(filtered.returncode, 0, filtered.stderr)
            _assert_valid(schema, json.loads(filtered.stdout))

            (root / "svc" / "main.py").write_text("# v2\n")
            commit_all(root, "introduce drift")
            drifted = self._run_cli(root, "verify", "--format", "json")
            self.assertEqual(drifted.returncode, 1, drifted.stderr)
            drift_payload = json.loads(drifted.stdout)
            _assert_valid(schema, drift_payload)
            self.assertFalse(drift_payload["ok"])
            self.assertTrue(drift_payload["issues"])
            self.assertEqual(drift_payload["resolved_issues"], [])

            repaired = self._run_cli(
                root, "verify", "--update", "--format", "json"
            )
            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            repair_payload = json.loads(repaired.stdout)
            _assert_valid(schema, repair_payload)
            self.assertTrue(repair_payload["ok"])
            self.assertTrue(repair_payload["updated"])
            self.assertEqual(repair_payload["issues"], [])
            self.assertTrue(repair_payload["resolved_issues"])
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_status_output_schema(self):
        schema = _load_schema("cli-output.status.schema.json")
        root, _cfg = self._make_repo()
        try:
            generated = self._run_cli(root, "generate", "--format", "json")
            self.assertEqual(generated.returncode, 0, generated.stderr)
            commit_all(root, "record lock")
            status = self._run_cli(root, "status", "--format", "json")
            self.assertEqual(status.returncode, 0, status.stderr)
            _assert_valid(schema, json.loads(status.stdout))
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_why_output_schema(self):
        schema = _load_schema("cli-output.why.schema.json")
        root, _cfg = self._make_repo()
        try:
            generated = self._run_cli(root, "generate", "--format", "json")
            self.assertEqual(generated.returncode, 0, generated.stderr)
            commit_all(root, "record lock")
            explained = self._run_cli(
                root, "why", "svc", "--format", "json", "--transitive"
            )
            self.assertEqual(explained.returncode, 0, explained.stderr)
            payload = json.loads(explained.stdout)
            _assert_valid(schema, payload)
            self.assertEqual(payload["changed_files_status"], "not-run")
            self.assertIsNone(payload["changed_files_error"])

            diagnostic_error = copy.deepcopy(payload)
            diagnostic_error["changed_files_status"] = "error"
            diagnostic_error["changed_files_error"] = "git diff failed"
            diagnostic_error["changed_files"] = []
            _assert_valid(schema, diagnostic_error)

            diagnostic_error["changed_files_error"] = None
            with self.assertRaises(jsonschema.ValidationError):
                _assert_valid(schema, diagnostic_error)
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_slice_output_schema(self):
        schema = _load_schema("cli-output.slice.schema.json")
        root, _cfg = self._make_repo()
        try:
            generated = self._run_cli(root, "generate", "--format", "json")
            self.assertEqual(generated.returncode, 0, generated.stderr)
            commit_all(root, "record lock")
            sliced = self._run_cli(root, "slice", "all", "--format", "json")
            self.assertEqual(sliced.returncode, 0, sliced.stderr)
            _assert_valid(schema, json.loads(sliced.stdout))
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_diff_output_schema(self):
        schema = _load_schema("cli-output.diff.schema.json")
        root, cfg = self._make_repo()
        try:
            from boundver.core import generate_lockfile, diff_lockfiles
            lock_a = generate_lockfile(cfg, root, source="head")
            cfg_b = copy.deepcopy(cfg)
            cfg_b["project"] = "test-next"
            cfg_b["components"]["svc"]["external_consumers"] = ["mobile"]
            cfg_b["slices"]["all"].update({
                "description": "public boundary",
                "mode": "boundary",
            })
            (root / "svc" / "main.py").write_text("# v2\n")
            commit_all(root, "bump")
            lock_b = generate_lockfile(cfg_b, root, source="head")
            real_diff = diff_lockfiles(lock_a, lock_b)

            (root / "old.lock.json").write_text(json.dumps(lock_a))
            (root / "new.lock.json").write_text(json.dumps(lock_b))
            diffed = self._run_cli(
                root,
                "diff",
                "old.lock.json",
                "new.lock.json",
                "--format",
                "json",
            )
            self.assertEqual(diffed.returncode, 0, diffed.stderr)
            cli_diff = json.loads(diffed.stdout)
            self.assertEqual(cli_diff, real_diff)

            self.assertEqual(
                set(real_diff["changed_metadata"]),
                {"project", "config_digest"},
            )
            component_change = real_diff["components"]["changed"][0]
            self.assertIn(
                "external_consumers", component_change["changed_metadata"]
            )
            slice_change = real_diff["slices"]["changed"][0]
            self.assertTrue(
                {"description", "mode"}.issubset(slice_change["changed_metadata"])
            )

            for diff_out in [cli_diff, diff_lockfiles(lock_a, lock_a)]:
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

    def test_verify_schema_enforces_final_state_semantics(self):
        schema = _load_schema("cli-output.verify.schema.json")
        invalid_results = [
            _verify_result(ok=True, issues=["still unresolved"]),
            _verify_result(
                ok=False,
                updated=True,
                resolved_issues=["repaired"],
            ),
            _verify_result(updated=False, resolved_issues=["not repaired"]),
        ]
        for candidate in invalid_results:
            with self.subTest(candidate=candidate):
                with self.assertRaises(jsonschema.ValidationError):
                    _assert_valid(schema, candidate)

        _assert_valid(
            schema,
            _verify_result(
                updated=True,
                resolved_issues=["repaired by update"],
            ),
        )

    def test_diff_schema_rejects_unknown_component_keys(self):
        schema = _load_schema("cli-output.diff.schema.json")
        candidate = _empty_diff()
        candidate["components"]["extra"] = []
        with self.assertRaises(jsonschema.ValidationError):
            _assert_valid(schema, candidate)

    def test_diff_schema_rejects_unknown_nested_properties(self):
        schema = _load_schema("cli-output.diff.schema.json")
        digest_a = "a" * 64
        digest_b = "b" * 64
        base = _empty_diff()
        base["components"]["added"] = [
            {"name": "added", "version": "1.0.0"}
        ]
        base["components"]["removed"] = [
            {"name": "removed", "version": None}
        ]
        base["components"]["changed"] = [{
            "name": "changed",
            "old_version": "1.0.0",
            "new_version": "2.0.0",
            "summary": "changed",
            "changed_facets": {
                "exact": {"old": digest_a, "new": digest_b}
            },
            "changed_metadata": {
                "version": {"old": "1.0.0", "new": "2.0.0"}
            },
        }]
        base["slices"]["added"] = [
            {"name": "added", "fingerprint": digest_a}
        ]
        base["slices"]["removed"] = [
            {"name": "removed", "fingerprint": digest_b}
        ]
        base["slices"]["changed"] = [{
            "name": "changed",
            "old": digest_a,
            "new": digest_b,
            "changed_metadata": {
                "mode": {"old": "exact", "new": "boundary"}
            },
        }]
        _assert_valid(schema, base)

        mutations = [
            lambda value: value["changed_metadata"].update(
                {"unknown": {"old": 1, "new": 2}}
            ),
            lambda value: value["components"]["added"][0].update(
                {"unknown": True}
            ),
            lambda value: value["components"]["removed"][0].update(
                {"unknown": True}
            ),
            lambda value: value["components"]["changed"][0].update(
                {"unknown": True}
            ),
            lambda value: value["components"]["changed"][0][
                "changed_facets"
            ].update({"unknown": {"old": digest_a, "new": digest_b}}),
            lambda value: value["components"]["changed"][0][
                "changed_facets"
            ]["exact"].update({"unknown": digest_a}),
            lambda value: value["components"]["changed"][0][
                "changed_metadata"
            ].update({"unknown": {"old": 1, "new": 2}}),
            lambda value: value["components"]["changed"][0][
                "changed_metadata"
            ]["version"].update({"unknown": 1}),
            lambda value: value["slices"]["added"][0].update(
                {"unknown": True}
            ),
            lambda value: value["slices"]["removed"][0].update(
                {"unknown": True}
            ),
            lambda value: value["slices"]["changed"][0].update(
                {"unknown": True}
            ),
            lambda value: value["slices"]["changed"][0][
                "changed_metadata"
            ].update({"unknown": {"old": 1, "new": 2}}),
            lambda value: value["slices"]["changed"][0][
                "changed_metadata"
            ]["mode"].update({"unknown": 1}),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                candidate = copy.deepcopy(base)
                mutate(candidate)
                with self.assertRaises(jsonschema.ValidationError):
                    _assert_valid(schema, candidate)

    def test_diff_schema_rejects_unknown_facets_and_malformed_digests(self):
        schema = _load_schema("cli-output.diff.schema.json")
        candidate = _empty_diff()
        candidate["components"]["changed"] = [{
            "name": "svc",
            "old_version": "1.0.0",
            "new_version": "1.0.1",
            "summary": "changed",
            "changed_facets": {
                "exact": {"old": "not-a-digest", "new": "b" * 64}
            },
            "changed_metadata": {},
        }]
        with self.assertRaises(jsonschema.ValidationError):
            _assert_valid(schema, candidate)

    def test_diff_schema_rejects_mistyped_metadata(self):
        schema = _load_schema("cli-output.diff.schema.json")
        digest_a = "a" * 64
        digest_b = "b" * 64
        base = _empty_diff()
        base["changed_metadata"] = {
            "project": {"old": "old-project", "new": "new-project"},
            "config_digest": {"old": digest_a, "new": digest_b},
        }
        base["components"]["changed"] = [{
            "name": "svc",
            "old_version": None,
            "new_version": "1.0.0",
            "summary": "component metadata changed",
            "changed_facets": {},
            "changed_metadata": {
                "version": {"old": None, "new": "1.0.0"},
                "path": {"old": "old-svc", "new": "svc"},
                "boundary_provider": {"old": "leaf", "new": "openapi"},
                "boundary_provider_version": {"old": None, "new": "2"},
                "boundary_status": {"old": "partial", "new": "ok"},
                "semver": {
                    "old": {
                        "compat_family": None,
                        "api_surface": None,
                        "exact_version": None,
                    },
                    "new": {
                        "compat_family": "1",
                        "api_surface": "1.0",
                        "exact_version": "1.0.0",
                    },
                },
                "consumers": {"old": [], "new": ["client"]},
                "external_consumers": {"old": [], "new": ["mobile"]},
                "boundary_metadata": {"old": None, "new": {"format": "json"}},
                "version_errors": {"old": None, "new": ["invalid version"]},
                "vendored_copies": {"old": None, "new": ["vendor/api.json"]},
                "vendored_digests": {
                    "old": None,
                    "new": {"vendor/api.json": digest_a},
                },
            },
        }]
        base["slices"]["changed"] = [{
            "name": "public",
            "old": digest_a,
            "new": digest_b,
            "changed_metadata": {
                "description": {"old": "old", "new": "new"},
                "mode": {"old": "exact", "new": "boundary"},
                "components": {"old": ["svc"], "new": ["svc", "client"]},
                "component_digests": {
                    "old": {"svc": digest_a},
                    "new": {"svc": digest_b, "client": None},
                },
            },
        }]
        _assert_valid(schema, base)

        mutations = [
            lambda value: value["changed_metadata"]["project"].update(
                {"new": 7}
            ),
            lambda value: value["changed_metadata"]["config_digest"].update(
                {"old": 7}
            ),
            lambda value: value["components"]["changed"][0][
                "changed_metadata"
            ]["version"].update({"new": {}}),
            lambda value: value["components"]["changed"][0][
                "changed_metadata"
            ]["path"].update({"old": None}),
            lambda value: value["components"]["changed"][0][
                "changed_metadata"
            ]["boundary_provider"].update({"new": ""}),
            lambda value: value["components"]["changed"][0][
                "changed_metadata"
            ]["boundary_provider_version"].update({"new": []}),
            lambda value: value["components"]["changed"][0][
                "changed_metadata"
            ]["boundary_status"].update({"new": "unknown"}),
            lambda value: value["components"]["changed"][0][
                "changed_metadata"
            ]["semver"].update({"new": {"compat_family": "1"}}),
            lambda value: value["components"]["changed"][0][
                "changed_metadata"
            ]["consumers"].update({"new": [1]}),
            lambda value: value["components"]["changed"][0][
                "changed_metadata"
            ]["external_consumers"].update({"new": ["mobile", "mobile"]}),
            lambda value: value["components"]["changed"][0][
                "changed_metadata"
            ]["boundary_metadata"].update({"new": []}),
            lambda value: value["components"]["changed"][0][
                "changed_metadata"
            ]["version_errors"].update({"new": ["error", 1]}),
            lambda value: value["components"]["changed"][0][
                "changed_metadata"
            ]["vendored_copies"].update({"new": [1]}),
            lambda value: value["components"]["changed"][0][
                "changed_metadata"
            ]["vendored_digests"].update({"new": {"vendor/api.json": 1}}),
            lambda value: value["slices"]["changed"][0][
                "changed_metadata"
            ]["description"].update({"new": 1}),
            lambda value: value["slices"]["changed"][0][
                "changed_metadata"
            ]["mode"].update({"new": "unknown"}),
            lambda value: value["slices"]["changed"][0][
                "changed_metadata"
            ]["components"].update({"new": ["svc", 1]}),
            lambda value: value["slices"]["changed"][0][
                "changed_metadata"
            ]["component_digests"].update({"new": {"svc": 1}}),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                candidate = copy.deepcopy(base)
                mutate(candidate)
                with self.assertRaises(jsonschema.ValidationError):
                    _assert_valid(schema, candidate)


if __name__ == "__main__":
    unittest.main()
