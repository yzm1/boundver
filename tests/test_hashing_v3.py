"""First-principles regression tests for the v3 hash/lock contract."""

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from boundver._git import _resolve_head_oid, git_latest_tag
from boundver._hashing import _content_only_digest, source_tree_digest
from boundver._lockfile import (
    LOCKFILE_SCHEMA,
    MigrationError,
    _SourceAccessor,
    generate_lockfile,
    migrate_lockfile,
    semantic_config_digest,
    verify_lockfile,
)
from boundver._utils import ConfigError
from tests._repo_fixtures import commit_all, init_git_repo


_TEMP_ROOT = "/tmp" if os.name != "nt" and Path("/tmp").is_dir() else None


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _init_repo(root: Path) -> None:
    init_git_repo(root)


def _commit(root: Path, message: str) -> None:
    commit_all(root, message)


def _config(*, vendored: bool = False) -> dict:
    component = {
        "path": "svc",
        "boundary": {"provider": "json-file", "paths": ["contract.txt"]},
        "behavior": {"paths": ["behavior.txt"]},
        "external_consumers": ["web"],
    }
    if vendored:
        component["vendored_copies"] = ["vendor/svc"]
    return {
        "project": "demo",
        "defaults": {
            "compat_mode": "major",
            "verify_facets": ["exact", "behavior", "boundary"],
        },
        "components": {"svc": component},
        "slices": {},
    }


class ModeAndTypeBindingTests(unittest.TestCase):
    def test_exact_and_content_only_distinguish_644_755_and_symlink(self):
        if os.name == "nt":
            self.skipTest("requires POSIX executable bits and symlinks")
        with tempfile.TemporaryDirectory(dir=_TEMP_ROOT) as td:
            root = Path(td)
            _init_repo(root)
            target = root / "svc" / "contract.txt"
            target.parent.mkdir()
            target.write_bytes(b"target")
            _commit(root, "regular")
            regular = (
                source_tree_digest(root, "svc", source="head"),
                _content_only_digest(root, "svc", source="head"),
            )

            target.chmod(0o755)
            _commit(root, "executable")
            executable = (
                source_tree_digest(root, "svc", source="head"),
                _content_only_digest(root, "svc", source="head"),
            )

            target.unlink()
            target.symlink_to("target")
            _commit(root, "symlink")
            symlink = (
                source_tree_digest(root, "svc", source="head"),
                _content_only_digest(root, "svc", source="head"),
            )

            self.assertEqual(len({regular[0], executable[0], symlink[0]}), 3)
            self.assertEqual(len({regular[1], executable[1], symlink[1]}), 3)

    def test_raw_boundary_and_behavior_bind_modes_and_behavior_binds_boundary(self):
        if os.name == "nt":
            self.skipTest("requires POSIX executable bits")
        with tempfile.TemporaryDirectory(dir=_TEMP_ROOT) as td:
            root = Path(td)
            _init_repo(root)
            svc = root / "svc"
            svc.mkdir()
            contract = svc / "contract.txt"
            behavior = svc / "behavior.txt"
            contract.write_bytes(b"target")
            behavior.write_bytes(b"stable")
            _commit(root, "base")
            base = generate_lockfile(_config(), root, source="head")

            # Behavior selects a disjoint file, so its change here proves the
            # behavior envelope cryptographically includes boundary.
            contract.chmod(0o755)
            _commit(root, "contract executable")
            contract_mode = generate_lockfile(_config(), root, source="head")
            self.assertNotEqual(
                base["components"]["svc"]["fingerprints"]["boundary"],
                contract_mode["components"]["svc"]["fingerprints"]["boundary"],
            )
            self.assertNotEqual(
                base["components"]["svc"]["fingerprints"]["behavior"],
                contract_mode["components"]["svc"]["fingerprints"]["behavior"],
            )

            # Boundary is unchanged; this proves the raw behavior selection
            # itself binds mode as well.
            behavior.chmod(0o755)
            _commit(root, "behavior executable")
            behavior_mode = generate_lockfile(_config(), root, source="head")
            self.assertEqual(
                contract_mode["components"]["svc"]["fingerprints"]["boundary"],
                behavior_mode["components"]["svc"]["fingerprints"]["boundary"],
            )
            self.assertNotEqual(
                contract_mode["components"]["svc"]["fingerprints"]["behavior"],
                behavior_mode["components"]["svc"]["fingerprints"]["behavior"],
            )

            # Link-target bytes equal the preceding regular file bytes, so only
            # the 120000 type/mode transition can explain these changes.
            behavior.unlink()
            behavior.symlink_to("stable")
            _commit(root, "behavior symlink")
            behavior_symlink = generate_lockfile(_config(), root, source="head")
            self.assertNotEqual(
                behavior_mode["components"]["svc"]["fingerprints"]["behavior"],
                behavior_symlink["components"]["svc"]["fingerprints"]["behavior"],
            )

            contract.unlink()
            contract.symlink_to("target")
            _commit(root, "contract symlink")
            contract_symlink = generate_lockfile(_config(), root, source="head")
            self.assertNotEqual(
                behavior_symlink["components"]["svc"]["fingerprints"]["boundary"],
                contract_symlink["components"]["svc"]["fingerprints"]["boundary"],
            )
            self.assertNotEqual(
                behavior_symlink["components"]["svc"]["fingerprints"]["behavior"],
                contract_symlink["components"]["svc"]["fingerprints"]["behavior"],
            )

    def test_worktree_accessor_round_trips_non_utf8_symlink_target(self):
        if os.name == "nt":
            self.skipTest("requires POSIX byte-oriented symlink targets")
        with tempfile.TemporaryDirectory(dir=_TEMP_ROOT) as td:
            root = Path(td)
            _init_repo(root)
            component = root / "svc"
            component.mkdir()
            link = component / "contract.link"
            target = b"contract-\xff"
            os.symlink(target, os.fsencode(link))
            _commit(root, "non-UTF-8 symlink target")

            accessor = _SourceAccessor(root, "working-tree")

            self.assertEqual(bytes(accessor.read_file("svc/contract.link")), target)


class SourceSnapshotTests(unittest.TestCase):
    def test_working_tree_fails_closed_when_index_has_unmerged_stages(self):
        with tempfile.TemporaryDirectory(dir=_TEMP_ROOT) as td:
            root = Path(td)
            _init_repo(root)
            target = root / "svc" / "value.txt"
            target.parent.mkdir()
            target.write_text("base\n", encoding="utf-8")
            _commit(root, "base")
            primary = _git(root, "branch", "--show-current")

            _git(root, "checkout", "-qb", "other")
            target.write_text("other\n", encoding="utf-8")
            _commit(root, "other")
            _git(root, "checkout", "-q", primary)
            target.write_text("primary\n", encoding="utf-8")
            _commit(root, "primary")
            merge = subprocess.run(
                ["git", "merge", "other"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(merge.returncode, 0)

            with self.assertRaisesRegex(ValueError, "complete Git tree"):
                source_tree_digest(root, "svc", source="working-tree")
            with self.assertRaisesRegex(ValueError, "complete Git tree"):
                _SourceAccessor(root, "working-tree")

    def test_head_accessor_remains_on_captured_commit(self):
        with tempfile.TemporaryDirectory(dir=_TEMP_ROOT) as td:
            root = Path(td)
            _init_repo(root)
            target = root / "svc" / "value.txt"
            target.parent.mkdir()
            target.write_bytes(b"before")
            _commit(root, "before")
            accessor = _SourceAccessor(root, "head")

            target.write_bytes(b"after")
            _commit(root, "after")

            self.assertEqual(bytes(accessor.read_file("svc/value.txt")), b"before")

    def test_full_generation_captures_one_index_tree(self):
        with tempfile.TemporaryDirectory(dir=_TEMP_ROOT) as td:
            root = Path(td)
            _init_repo(root)
            for name in ("a", "b"):
                component = root / name
                component.mkdir()
                (component / "contract.txt").write_bytes(name.encode())
            _commit(root, "components")
            config = {
                "project": "snapshot",
                "components": {
                    name: {
                        "path": name,
                        "boundary": {
                            "provider": "json-file",
                            "paths": ["contract.txt"],
                        },
                    }
                    for name in ("a", "b")
                },
                "slices": {},
            }
            from boundver import _lockfile

            original = _lockfile._capture_git_source_snapshot
            calls = []

            def capture(repo_root, source):
                calls.append(source)
                return original(repo_root, source)

            with patch.object(
                _lockfile, "_capture_git_source_snapshot", side_effect=capture
            ):
                generate_lockfile(config, root, source="index")
            self.assertEqual(calls, ["index"])

    def test_tag_lookup_ignores_unreachable_repository_tag(self):
        with tempfile.TemporaryDirectory(dir=_TEMP_ROOT) as td:
            root = Path(td)
            _init_repo(root)
            (root / "old.txt").write_text("old", encoding="utf-8")
            _commit(root, "old history")
            _git(root, "tag", "v9.9.9")

            _git(root, "checkout", "--orphan", "current")
            _git(root, "read-tree", "--empty")
            (root / "old.txt").unlink()
            (root / "current.txt").write_text("current", encoding="utf-8")
            _commit(root, "current history")
            head_oid = _resolve_head_oid(root)

            self.assertIsNone(git_latest_tag(root, "v", ref=head_oid))


class SemanticConfigDigestTests(unittest.TestCase):
    def test_every_contract_input_changes_config_digest(self):
        base = _config(vendored=True)
        baseline = semantic_config_digest(base)
        mutations = {}

        def changed(name, update):
            candidate = copy.deepcopy(base)
            update(candidate)
            mutations[name] = semantic_config_digest(candidate)

        changed("path", lambda c: c["components"]["svc"].__setitem__("path", "svc2"))
        changed(
            "boundary glob",
            lambda c: c["components"]["svc"]["boundary"].__setitem__(
                "paths", ["**/*.txt"]
            ),
        )
        changed(
            "boundary option",
            lambda c: c["components"]["svc"]["boundary"].__setitem__(
                "format", "strict"
            ),
        )
        changed(
            "behavior",
            lambda c: c["components"]["svc"]["behavior"].__setitem__(
                "paths", ["**/*.json"]
            ),
        )
        changed(
            "version source",
            lambda c: c["components"]["svc"].__setitem__(
                "version_source", {"file": "package.json", "field": "version"}
            ),
        )
        changed(
            "vendored",
            lambda c: c["components"]["svc"].__setitem__(
                "vendored_copies", ["vendor/other"]
            ),
        )
        changed(
            "compat mode",
            lambda c: c["defaults"].__setitem__(
                "compat_mode", "semver_major_minor"
            ),
        )
        changed(
            "external consumers",
            lambda c: c["components"]["svc"].__setitem__(
                "external_consumers", ["mobile"]
            ),
        )
        changed(
            "provider declaration",
            lambda c: c.__setitem__(
                "providers", [{"name": "custom", "module": "pkg.provider"}]
            ),
        )
        changed(
            "slice",
            lambda c: c["slices"].__setitem__(
                "public", {"mode": "boundary", "components": ["svc"]}
            ),
        )
        changed(
            "default verify policy",
            lambda c: c["defaults"].__setitem__("verify_facets", ["boundary"]),
        )

        self.assertTrue(mutations)
        for name, digest in mutations.items():
            with self.subTest(name=name):
                self.assertNotEqual(baseline, digest)

    def test_set_like_order_and_schema_url_are_not_semantic(self):
        first = _config(vendored=True)
        second = copy.deepcopy(first)
        first["$schema"] = "https://example.invalid/one"
        second["$schema"] = "https://example.invalid/two"
        second["defaults"]["verify_facets"].reverse()
        self.assertEqual(
            semantic_config_digest(first), semantic_config_digest(second)
        )

    def test_presentation_only_ecosystem_and_note_are_not_semantic(self):
        baseline = _config()
        annotated = copy.deepcopy(baseline)
        annotated["components"]["svc"]["ecosystem"] = "python"
        annotated["components"]["svc"]["boundary"]["note"] = (
            "Human-facing onboarding context"
        )

        self.assertEqual(
            semantic_config_digest(baseline),
            semantic_config_digest(annotated),
        )

    def test_verify_reports_semantic_config_mutation(self):
        with tempfile.TemporaryDirectory(dir=_TEMP_ROOT) as td:
            root = Path(td)
            _init_repo(root)
            svc = root / "svc"
            svc.mkdir()
            (svc / "contract.txt").write_bytes(b"target")
            (svc / "behavior.txt").write_bytes(b"stable")
            _commit(root, "base")
            config = _config()
            lock = generate_lockfile(config, root, source="head")
            changed = copy.deepcopy(config)
            changed["defaults"]["verify_facets"] = ["boundary"]

            issues = verify_lockfile(changed, lock, root, source="head")

            self.assertTrue(
                any("METADATA MISMATCH config_digest" in issue for issue in issues),
                issues,
            )


class FailFastFacetParsingTests(unittest.TestCase):
    def test_component_name_cannot_spoof_fail_fast_severity(self):
        with tempfile.TemporaryDirectory(dir=_TEMP_ROOT) as td:
            root = Path(td)
            _init_repo(root)
            components = {}
            for name, path in (("spoof.compat:", "spoof"), ("real", "real")):
                directory = root / path
                directory.mkdir()
                (directory / "contract.txt").write_bytes(name.encode())
                components[name] = {
                    "path": path,
                    "boundary": {
                        "provider": "json-file",
                        "paths": ["contract.txt"],
                    },
                }
            _commit(root, "components")
            config = {"project": "severity", "components": components, "slices": {}}
            lock = generate_lockfile(config, root, source="head")
            lock["components"]["spoof.compat:"]["fingerprints"]["exact"] = "0" * 64
            lock["components"]["real"]["fingerprints"]["boundary"] = "1" * 64

            issues = verify_lockfile(
                config, lock, root, source="head", fail_fast=True
            )

            self.assertEqual(len(issues), 1)
            self.assertTrue(issues[0].startswith("MISMATCH real.boundary:"), issues)


class LockV3SafetyTests(unittest.TestCase):
    def _write_vendored_repo(self, root: Path, copy_content=b"target") -> None:
        _init_repo(root)
        svc = root / "svc"
        vendor = root / "vendor" / "svc"
        svc.mkdir(parents=True)
        vendor.mkdir(parents=True)
        (svc / "contract.txt").write_bytes(b"target")
        (svc / "behavior.txt").write_bytes(b"stable")
        (vendor / "contract.txt").write_bytes(copy_content)
        (vendor / "behavior.txt").write_bytes(b"stable")
        _commit(root, "vendored")

    def test_strict_generated_v3_lock_immediately_verifies(self):
        with tempfile.TemporaryDirectory(dir=_TEMP_ROOT) as td:
            root = Path(td)
            self._write_vendored_repo(root)
            config = _config(vendored=True)
            lock = generate_lockfile(config, root, source="head", strict=True)

            self.assertEqual(lock["schema"], "boundary-lock/v3")
            self.assertEqual(lock["config_digest"], semantic_config_digest(config))
            self.assertEqual(
                verify_lockfile(config, lock, root, source="head"), []
            )

    def test_missing_vendored_copy_fails_strict_for_every_source(self):
        with tempfile.TemporaryDirectory(dir=_TEMP_ROOT) as td:
            root = Path(td)
            _init_repo(root)
            svc = root / "svc"
            svc.mkdir()
            (svc / "contract.txt").write_bytes(b"target")
            (svc / "behavior.txt").write_bytes(b"stable")
            _commit(root, "source only")
            config = _config(vendored=True)

            for source in ("head", "index", "working-tree"):
                with self.subTest(source=source):
                    with self.assertRaisesRegex(
                        ConfigError, "Vendored copy.*has no files"
                    ):
                        generate_lockfile(config, root, source=source, strict=True)

    def test_divergent_vendored_copy_fails_strict(self):
        with tempfile.TemporaryDirectory(dir=_TEMP_ROOT) as td:
            root = Path(td)
            self._write_vendored_repo(root, copy_content=b"different")
            with self.assertRaisesRegex(ConfigError, "differs from source"):
                generate_lockfile(
                    _config(vendored=True), root, source="head", strict=True
                )

    def test_vendored_mode_divergence_fails_with_identical_bytes(self):
        if os.name == "nt":
            self.skipTest("requires POSIX executable bits")
        with tempfile.TemporaryDirectory(dir=_TEMP_ROOT) as td:
            root = Path(td)
            self._write_vendored_repo(root)
            (root / "vendor" / "svc" / "contract.txt").chmod(0o755)
            _commit(root, "vendored mode drift")
            with self.assertRaisesRegex(ConfigError, "differs from source"):
                generate_lockfile(
                    _config(vendored=True), root, source="head", strict=True
                )

    def test_v2_migration_is_rejected_and_requests_regeneration(self):
        with self.assertRaisesRegex(MigrationError, "cannot be migrated"):
            migrate_lockfile(
                {
                    "schema": "boundary-lock/v2",
                    "project": "old",
                    "components": {},
                    "slices": {},
                }
            )

    def test_json_schema_requires_v3_config_metadata(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        schema_path = Path(__file__).parents[1] / "spec" / "boundary.lock.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        minimal = {
            "schema": LOCKFILE_SCHEMA,
            "config_contract": "boundver-semantic-config/v2",
            "config_digest": "0" * 64,
            "project": "demo",
            "components": {},
            "slices": {},
        }
        jsonschema.Draft202012Validator(schema).validate(minimal)


if __name__ == "__main__":
    unittest.main()
