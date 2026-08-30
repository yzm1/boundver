"""Fail-closed size contracts for generated configs and lockfiles."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import boundver
import boundver._config as config_module
import boundver._lockfile as lockfile_module
import boundver.core as core
from boundver._utils import GuardrailError, LockfileError, _bounded_json_dumps
from boundver.versions import MAX_VERSION_FILE_BYTES, parse_semver
from tests._repo_fixtures import commit_all, init_git_repo


def _run_main(*args: str, repo_root: Optional[Path] = None) -> tuple[int, str, str]:
    """Run the CLI in-process and capture its complete public result."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = 0
    with ExitStack() as stack:
        stack.enter_context(patch.object(sys, "argv", ["boundver", *args]))
        stack.enter_context(redirect_stdout(stdout))
        stack.enter_context(redirect_stderr(stderr))
        if repo_root is not None:
            stack.enter_context(
                patch("boundver.core.git_root", return_value=repo_root)
            )
        try:
            core.main()
        except SystemExit as exc:
            exit_code = int(exc.code or 0)
    return exit_code, stdout.getvalue(), stderr.getvalue()


def _write_json(path: Path, value: object, *, compact: bool = False) -> bytes:
    separators = (",", ":") if compact else None
    payload = (json.dumps(value, separators=separators) + "\n").encode("utf-8")
    path.write_bytes(payload)
    return payload


def _versioned_repo(root: Path) -> None:
    init_git_repo(root)
    component = root / "svc"
    component.mkdir()
    config = {
        "project": "artifact-bounds",
        "defaults": {"verify_facets": ["exact"]},
        "components": {
            "svc": {
                "path": "svc",
                "version_source": {"file": "version.json", "field": "version"},
                "boundary": {
                    "provider": "json-file",
                    "paths": ["contract.json"],
                },
            }
        },
        "slices": {},
    }
    _write_json(root / "boundary.config.json", config)
    _write_json(component / "version.json", {"version": "1.2.3"})
    _write_json(component / "contract.json", {"api": "v1"})
    commit_all(root, "artifact output fixture")


def _generate_initial_lock(root: Path) -> bytes:
    code, _, error = _run_main(
        "generate", "--source", "working-tree", repo_root=root
    )
    if code != 0:  # pragma: no cover - fixture assertion reports the full error
        raise AssertionError(error)
    return (root / "boundary.lock.json").read_bytes()


def _make_version_longer(root: Path, length: int = 2_000) -> None:
    _write_json(
        root / "svc" / "version.json",
        {"version": "1.2.3+" + ("a" * length)},
    )


def _assert_controlled_overflow(
    testcase: unittest.TestCase,
    result: tuple[int, str, str],
) -> None:
    code, _, error = result
    testcase.assertEqual(code, core.EXIT_USAGE, error)
    testcase.assertIn("storage limit", error)
    testcase.assertIn("no file was written", error)


class BoundedJsonEncoderTests(unittest.TestCase):
    def test_exact_utf8_limit_is_accepted_and_one_byte_less_is_rejected(self) -> None:
        value = {"non_ascii": "café", "items": [1, 2, 3]}
        rendered = _bounded_json_dumps(value, ensure_ascii=False, indent=2)
        size = len(rendered.encode("utf-8"))

        self.assertEqual(
            _bounded_json_dumps(
                value,
                ensure_ascii=False,
                indent=2,
                max_bytes=size,
            ),
            rendered,
        )
        with self.assertRaisesRegex(GuardrailError, "byte limit"):
            _bounded_json_dumps(
                value,
                ensure_ascii=False,
                indent=2,
                max_bytes=size - 1,
            )

    def test_incremental_limit_stops_before_later_aggregate_values(self) -> None:
        class MustNotBeVisited:
            pass

        visited: list[object] = []

        def encode_default(value: object) -> object:
            visited.append(value)
            return "visited"

        with self.assertRaises(GuardrailError):
            _bounded_json_dumps(
                [["first", "value"], MustNotBeVisited()],
                max_bytes=8,
                default=encode_default,
            )

        self.assertEqual(visited, [])

    def test_invalid_byte_limits_are_rejected(self) -> None:
        for invalid in (-1, 1.5, True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    _bounded_json_dumps({}, max_bytes=invalid)  # type: ignore[arg-type]


class ArtifactDumpContractTests(unittest.TestCase):
    def test_successful_config_dump_reloads_at_the_exact_same_limit(self) -> None:
        value = {"project": "p", "components": {}, "slices": {}}
        expected = (_bounded_json_dumps(value, indent=2) + "\n").encode("utf-8")

        with patch.object(config_module, "MAX_CONFIG_BYTES", len(expected)):
            actual = config_module.dump_config(value).encode("utf-8")
            reloaded = config_module.parse_config_bytes(actual, Path("config.json"))

        self.assertEqual(actual, expected)
        self.assertEqual(reloaded, value)

    def test_successful_lock_dump_reloads_at_the_exact_same_limit(self) -> None:
        value = {"schema": "test", "components": {}, "slices": {}}
        expected = (_bounded_json_dumps(value, indent=2) + "\n").encode("utf-8")

        with patch.object(lockfile_module, "MAX_LOCKFILE_BYTES", len(expected)):
            actual = lockfile_module.dump_lockfile(value).encode("utf-8")
            reloaded = lockfile_module.parse_lockfile_bytes(actual, "lock.json")

        self.assertEqual(actual, expected)
        self.assertEqual(reloaded, value)

    def test_dump_reserves_the_final_newline_inside_each_storage_contract(self) -> None:
        config = {"project": "p", "components": {}, "slices": {}}
        lock = {"schema": "test", "components": {}, "slices": {}}
        config_size = len(
            (_bounded_json_dumps(config, indent=2) + "\n").encode("utf-8")
        )
        lock_size = len(
            (_bounded_json_dumps(lock, indent=2) + "\n").encode("utf-8")
        )

        with patch.object(config_module, "MAX_CONFIG_BYTES", config_size - 1):
            with self.assertRaisesRegex(config_module.ConfigError, "no file was written"):
                config_module.dump_config(config)
        with patch.object(lockfile_module, "MAX_LOCKFILE_BYTES", lock_size - 1):
            with self.assertRaisesRegex(LockfileError, "no file was written"):
                lockfile_module.dump_lockfile(lock)

    def test_production_version_input_can_fit_while_derived_lock_does_not(self) -> None:
        version = "1.2.3+" + ("a" * 10_485_632)
        source = json.dumps({"version": version}).encode("utf-8")
        lock_like = {
            "schema": lockfile_module.LOCKFILE_SCHEMA,
            "config_contract": lockfile_module.SEMANTIC_CONFIG_VERSION,
            "components": {
                "svc": {
                    "path": "svc",
                    "version": version,
                    "boundary_provider": "json-file",
                    "boundary_metadata": {"description": "b" * 512},
                }
            },
            "slices": {},
        }

        self.assertLessEqual(len(source), MAX_VERSION_FILE_BYTES)
        self.assertEqual(parse_semver(version), ("1", "1.2", "1.2.3"))
        unbounded_size = len(
            (_bounded_json_dumps(lock_like, indent=2) + "\n").encode("utf-8")
        )
        self.assertGreater(unbounded_size, lockfile_module.MAX_LOCKFILE_BYTES)
        with self.assertRaisesRegex(LockfileError, "storage limit"):
            lockfile_module.dump_lockfile(lock_like)


class LockMutationOverflowTests(unittest.TestCase):
    def test_full_generate_preserves_an_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _versioned_repo(root)
            target = root / "boundary.lock.json"
            original = b"existing lock sentinel\n"
            target.write_bytes(original)

            with patch.object(lockfile_module, "MAX_LOCKFILE_BYTES", 128):
                result = _run_main(
                    "generate", "--source", "working-tree", repo_root=root
                )

            _assert_controlled_overflow(self, result)
            self.assertEqual(target.read_bytes(), original)

    def test_scoped_generate_preserves_an_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _versioned_repo(root)
            original = _generate_initial_lock(root)
            _make_version_longer(root)

            with patch.object(
                lockfile_module, "MAX_LOCKFILE_BYTES", len(original) + 128
            ):
                result = _run_main(
                    "generate",
                    "--source",
                    "working-tree",
                    "--components",
                    "svc",
                    repo_root=root,
                )

            _assert_controlled_overflow(self, result)
            self.assertEqual((root / "boundary.lock.json").read_bytes(), original)

    def test_verify_update_preserves_an_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _versioned_repo(root)
            original = _generate_initial_lock(root)
            _make_version_longer(root)

            with patch.object(
                lockfile_module, "MAX_LOCKFILE_BYTES", len(original) + 128
            ):
                result = _run_main(
                    "verify",
                    "--source",
                    "working-tree",
                    "--update",
                    repo_root=root,
                )

            _assert_controlled_overflow(self, result)
            self.assertEqual((root / "boundary.lock.json").read_bytes(), original)

    def test_migrate_lock_preserves_compact_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _versioned_repo(root)
            _generate_initial_lock(root)
            target = root / "boundary.lock.json"
            value = json.loads(target.read_text(encoding="utf-8"))
            original = _write_json(target, value, compact=True)
            pretty_size = len(
                (_bounded_json_dumps(value, indent=2) + "\n").encode("utf-8")
            )
            self.assertLess(len(original), pretty_size)

            with patch.object(
                lockfile_module, "MAX_LOCKFILE_BYTES", pretty_size - 1
            ):
                result = _run_main("migrate-lock", "--lock", str(target))

            _assert_controlled_overflow(self, result)
            self.assertEqual(target.read_bytes(), original)

    def test_public_generate_preserves_an_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _versioned_repo(root)
            target = root / "boundary.lock.json"
            original = b"public API sentinel\n"
            target.write_bytes(original)

            with (
                patch("boundver._git.git_root", return_value=root),
                patch.object(lockfile_module, "MAX_LOCKFILE_BYTES", 128),
            ):
                with self.assertRaisesRegex(LockfileError, "no file was written"):
                    boundver.generate(source="working-tree")

            self.assertEqual(target.read_bytes(), original)


class ConfigMutationOverflowTests(unittest.TestCase):
    def test_init_does_not_create_an_unreadable_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            target = root / "boundary.config.json"

            with patch.object(config_module, "MAX_CONFIG_BYTES", 128):
                result = _run_main("init", repo_root=root)

            _assert_controlled_overflow(self, result)
            self.assertFalse(target.exists())

    def test_add_preserves_compact_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            for name in ("one", "two"):
                (root / name).mkdir()
            value = {
                "project": "p",
                "components": {
                    "one": {
                        "path": "one",
                        "version_source": None,
                        "boundary": {"provider": "implicit", "paths": []},
                    }
                },
                "slices": {},
            }
            target = root / "boundary.config.json"
            original = _write_json(target, value, compact=True)
            expected = json.loads(json.dumps(value))
            expected["components"]["two"] = {
                "path": "two",
                "version_source": None,
                "boundary": {"provider": "implicit", "paths": []},
            }
            expected_size = len(
                (_bounded_json_dumps(expected, indent=2) + "\n").encode("utf-8")
            )
            self.assertLess(len(original), expected_size)

            with patch.object(config_module, "MAX_CONFIG_BYTES", expected_size - 1):
                result = _run_main("add", "two", "two", repo_root=root)

            _assert_controlled_overflow(self, result)
            self.assertEqual(target.read_bytes(), original)

    def test_remove_preserves_compact_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            components = {}
            for index in range(8):
                name = f"svc{index}"
                (root / name).mkdir()
                components[name] = {
                    "path": name,
                    "version_source": None,
                    "boundary": {"provider": "implicit", "paths": []},
                }
            value = {"project": "p", "components": components, "slices": {}}
            target = root / "boundary.config.json"
            original = _write_json(target, value, compact=True)
            expected = json.loads(json.dumps(value))
            del expected["components"]["svc0"]
            expected_size = len(
                (_bounded_json_dumps(expected, indent=2) + "\n").encode("utf-8")
            )
            self.assertLess(len(original), expected_size)

            with patch.object(config_module, "MAX_CONFIG_BYTES", expected_size - 1):
                result = _run_main("remove", "svc0", repo_root=root)

            _assert_controlled_overflow(self, result)
            self.assertEqual(target.read_bytes(), original)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
