"""Regression tests for bounded fallback enumeration and canonical reads."""

import io
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import boundver._git as git_helpers
import boundver._hashing as hashing
import boundver._utils as utils
import boundver._canonical_providers as canonical_providers
import boundver._discovery as discovery
import boundver.providers as providers
from boundver._config import (
    _detect_provider,
    _expand_component_paths,
    _json_value_issues,
    _load_config_schema,
    discover_components,
    parse_config_bytes,
    validate_config,
)
from boundver._git import _list_files_for_source, _load_gitignore_patterns
from boundver._lockfile import _lockfile_schema_issues, parse_lockfile_bytes
from boundver._utils import (
    ConfigError,
    GuardrailError,
    LockfileError,
    ProviderError,
    _bounded_json_dumps,
    _iter_bounded_filesystem_paths,
)
from boundver.providers import (
    JsonCanonicalProvider,
    OpenApiCanonicalProvider,
    ProviderContext,
    load_custom_providers,
)
from tests._repo_fixtures import commit_all, init_git_repo


class _FakeDirEntry:
    def __init__(self, path):
        self.path = str(path)

    def is_dir(self, *, follow_symlinks=True):
        del follow_symlinks
        return False


class _FakeScandir:
    def __init__(self, paths):
        self._paths = iter(paths)
        self.next_calls = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        self.closed = True

    def __iter__(self):
        return self

    def __next__(self):
        self.next_calls += 1
        return _FakeDirEntry(next(self._paths))


class _TrackingBytesIO(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class FilesystemEnumerationBoundTests(unittest.TestCase):
    def test_scandir_traversal_stops_after_one_sentinel_entry(self):
        root = Path("/repo")
        scan = _FakeScandir(root / f"file-{index}" for index in range(10))
        with patch("boundver._utils.os.scandir", return_value=scan):
            with self.assertRaisesRegex(GuardrailError, "two entries"):
                list(
                    _iter_bounded_filesystem_paths(
                        root,
                        recursive=False,
                        max_entries=2,
                        exceeded_message="two entries exceeded",
                    )
                )
        self.assertEqual(scan.next_calls, 3)
        self.assertTrue(scan.closed)

    def test_hash_fallback_caps_before_sorting_and_stops_at_sentinel(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            component = root / "svc"
            component.mkdir()
            paths = []
            for name in ("c.txt", "b.txt", "a.txt"):
                path = component / name
                path.write_bytes(name.encode("ascii"))
                paths.append(path)

            visited = []

            def fake_walk(*args, **kwargs):
                del args, kwargs
                for path in paths:
                    visited.append(path)
                    yield path

            git_failure = subprocess.CalledProcessError(1, ["git"])
            with (
                patch(
                    "boundver._git._iter_bounded_git_paths",
                    side_effect=git_failure,
                ),
                patch("boundver._git._git_run", side_effect=git_failure),
                patch(
                    "boundver._git._iter_bounded_filesystem_paths",
                    side_effect=fake_walk,
                ),
                patch("boundver._git.MAX_FALLBACK_FILES", 1),
            ):
                with self.assertRaisesRegex(GuardrailError, ">1 files"):
                    _list_files_for_source(
                        root,
                        "svc",
                        source="working-tree",
                    )
            self.assertEqual(visited, paths[:2])

    def test_hash_fallback_prunes_always_ignored_directories(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ignored = root / "node_modules"
            ignored.mkdir()
            (ignored / "one.js").write_bytes(b"one")
            (ignored / "two.js").write_bytes(b"two")
            (root / "kept.txt").write_bytes(b"kept")

            git_failure = subprocess.CalledProcessError(1, ["git"])
            with (
                patch(
                    "boundver._git._iter_bounded_git_paths",
                    side_effect=git_failure,
                ),
                patch("boundver._git._git_run", side_effect=git_failure),
                patch("boundver._git.MAX_FALLBACK_TRAVERSAL_ENTRIES", 2),
            ):
                files = _list_files_for_source(
                    root,
                    ".",
                    source="working-tree",
                )
            self.assertEqual(files, ["kept.txt"])

    def test_gitignore_read_has_a_hard_byte_ceiling(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".gitignore").write_bytes(b"ab")
            with patch("boundver._git.MAX_GITIGNORE_BYTES", 1):
                with self.assertRaisesRegex(GuardrailError, "file too large"):
                    _load_gitignore_patterns(root)

    def test_component_expansion_raises_instead_of_returning_partial_set(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            component = root / "svc"
            component.mkdir()
            (component / "a.txt").write_bytes(b"a")
            (component / "b.txt").write_bytes(b"b")
            with patch("boundver._config.MAX_COMPONENT_EXPANSION_FILES", 1):
                with self.assertRaisesRegex(GuardrailError, ">1 files"):
                    _expand_component_paths(root, "svc", ["*.txt"])

    def test_component_discovery_caps_raw_filesystem_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.txt").write_bytes(b"a")
            (root / "b.txt").write_bytes(b"b")
            git_failure = subprocess.CalledProcessError(1, ["git"])
            with (
                patch(
                    "boundver._discovery._iter_bounded_git_paths",
                    side_effect=git_failure,
                ),
                patch.object(
                    discovery, "_is_git_repository", return_value=False
                ),
                patch("boundver._config.MAX_FILESYSTEM_TRAVERSAL_ENTRIES", 1),
            ):
                with self.assertRaisesRegex(
                    GuardrailError,
                    "filesystem traversal exceeds 1 entries",
                ):
                    discover_components(root)

    def test_component_discovery_fails_instead_of_returning_partial_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("one", "two"):
                component = root / name
                component.mkdir()
                (component / "package.json").write_text(
                    '{"name":"example","version":"1.0.0"}',
                    encoding="utf-8",
                )
            git_failure = subprocess.CalledProcessError(1, ["git"])
            with (
                patch(
                    "boundver._discovery._iter_bounded_git_paths",
                    side_effect=git_failure,
                ),
                patch.object(
                    discovery, "_is_git_repository", return_value=False
                ),
                patch("boundver._config.MAX_DISCOVERED_COMPONENTS", 1),
            ):
                with self.assertRaisesRegex(GuardrailError, ">1 components"):
                    discover_components(root)

    def test_provider_detection_caps_directory_fanout_before_sorting(self):
        with tempfile.TemporaryDirectory() as td:
            component = Path(td)
            (component / "a.txt").write_bytes(b"a")
            (component / "b.txt").write_bytes(b"b")
            with patch("boundver._config.MAX_PROVIDER_DETECTION_ENTRIES", 1):
                with self.assertRaisesRegex(GuardrailError, ">1 directory entries"):
                    _detect_provider(component)

    def test_source_tree_schema_fallback_uses_bounded_reader(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "boundary.config.schema.json").write_bytes(b"{}")
            with (
                patch(
                    "boundver._config.resources.read_text",
                    side_effect=FileNotFoundError,
                ),
                patch("boundver._config.MAX_CONFIG_BYTES", 1),
            ):
                self.assertIsNone(_load_config_schema(root))


class CanonicalProviderBudgetTests(unittest.TestCase):
    def test_json_canonical_reads_use_remaining_aggregate_budget(self):
        files = {
            "svc/a.json": b'{"a":1}',
            "svc/b.json": b"{}",
        }
        requested_limits = []

        def read_limited(path, limit):
            requested_limits.append(limit)
            content = files[path]
            if len(content) > limit:
                raise ProviderError("source content exceeds remaining budget")
            return content

        ctx = ProviderContext(
            repo_root=Path("/repo"),
            component_path="svc",
            boundary_cfg={"paths": ["*.json"]},
            source="working-tree",
            read_file=lambda path: files[path],
            list_files=lambda prefix: sorted(files),
            read_file_limited=read_limited,
        )
        with patch.object(providers, "MAX_PROVIDER_TOTAL_BYTES", 8):
            resolved = JsonCanonicalProvider().resolve(ctx)
        self.assertEqual(resolved.status, "error")
        self.assertIn("remaining budget", resolved.errors[0])
        self.assertEqual(requested_limits, [8, 1])

    def test_json_canonical_passes_zero_remaining_budget_to_accessor(self):
        files = {
            "svc/a.json": b'{"a":1}',
            "svc/b.json": b"{}",
        }
        requested_limits = []

        def read_limited(path, limit):
            requested_limits.append(limit)
            content = files[path]
            if len(content) > limit:
                raise ProviderError("source content exceeds remaining budget")
            return content

        ctx = ProviderContext(
            repo_root=Path("/repo"),
            component_path="svc",
            boundary_cfg={"paths": ["*.json"]},
            source="working-tree",
            read_file=lambda path: files[path],
            list_files=lambda prefix: sorted(files),
            read_file_limited=read_limited,
        )
        with patch.object(providers, "MAX_PROVIDER_TOTAL_BYTES", 7):
            resolved = JsonCanonicalProvider().resolve(ctx)
        self.assertEqual(resolved.status, "error")
        self.assertIn("remaining budget", resolved.errors[0])
        self.assertEqual(requested_limits, [7, 0])

    def test_openapi_canonical_reads_use_remaining_aggregate_budget(self):
        document = b'{"openapi":"3.1.0","paths":{}}'
        files = {
            "svc/a.json": document,
            "svc/b.json": document,
        }
        requested_limits = []

        def read_limited(path, limit):
            requested_limits.append(limit)
            content = files[path]
            if len(content) > limit:
                raise ProviderError("source content exceeds remaining budget")
            return content

        ctx = ProviderContext(
            repo_root=Path("/repo"),
            component_path="svc",
            boundary_cfg={"paths": ["*.json"]},
            source="working-tree",
            read_file=lambda path: files[path],
            list_files=lambda prefix: sorted(files),
            read_file_limited=read_limited,
        )
        with patch.object(providers, "MAX_PROVIDER_TOTAL_BYTES", len(document)):
            resolved = OpenApiCanonicalProvider().resolve(ctx)
        self.assertEqual(resolved.status, "error")
        self.assertIn("remaining budget", resolved.errors[0])
        self.assertEqual(requested_limits, [len(document), 0])

    def test_canonical_string_is_sized_before_escaping_allocation(self):
        with patch.object(
            canonical_providers._json_mod,
            "dumps",
            side_effect=AssertionError("must not allocate quoted value"),
        ) as dumps:
            with self.assertRaisesRegex(ProviderError, "5-byte remaining"):
                providers._canonical_json_bytes("\x00", "contract.json", max_bytes=5)
        dumps.assert_not_called()

    def test_bounded_canonical_encoder_preserves_wire_format(self):
        value = {
            "control": "line\n",
            "integer": 12345678901234567890,
            "unicode": "caf\N{LATIN SMALL LETTER E WITH ACUTE}",
            "values": [None, True, 1.5],
        }
        expected = _bounded_json_dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        actual = providers._canonical_json_bytes(
            value,
            "contract.json",
            max_bytes=len(expected),
        )
        self.assertEqual(actual, expected)


class JsonTraversalBudgetTests(unittest.TestCase):
    @staticmethod
    def _nested_json(depth):
        return "[" * depth + "0" + "]" * depth

    def test_depth_and_node_limits_are_enforced_iteratively(self):
        nested = 0
        for _ in range(5):
            nested = [nested]

        with patch.object(utils, "MAX_JSON_TREE_DEPTH", 4):
            issues = _json_value_issues(nested)
        self.assertEqual(len(issues), 1)
        self.assertIn("nested too deeply", issues[0])

        with patch.object(utils, "MAX_JSON_TREE_NODES", 3):
            issues = _json_value_issues([0] * 100_000)
        self.assertEqual(len(issues), 1)
        self.assertIn("3-value JSON tree limit", issues[0])

    def test_yaml_node_limit_is_enforced_during_composition(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML is not installed")

        with patch.object(utils, "MAX_YAML_COMPOSE_NODES", 3):
            with self.assertRaisesRegex(ConfigError, "pre-parse structural"):
                parse_config_bytes(
                    b"project: p\ncomponents: [a, b, c]\n",
                    Path("boundary.config.yaml"),
                )

    def test_toml_structure_limit_is_enforced_before_parsing(self):
        with patch.object(utils, "MAX_JSON_TREE_NODES", 2):
            with self.assertRaisesRegex(ConfigError, "pre-parse structural"):
                parse_config_bytes(
                    b'project = "p"\ncomponents = ["a", "b", "c", "d"]\n',
                    Path("boundary.config.toml"),
                )

    def test_diagnostic_paths_and_issue_counts_are_bounded(self):
        invalid = object()
        for _ in range(8):
            invalid = {"long-key-" * 100: invalid}
        with patch.object(utils, "MAX_JSON_DIAGNOSTIC_PATH_BYTES", 64):
            issues = _json_value_issues(invalid)
        diagnostic_path = issues[0].split(" contains ", 1)[0]
        self.assertLessEqual(len(diagnostic_path.encode("ascii")), 64)
        self.assertIn("path truncated", diagnostic_path)

        with patch.object(utils, "MAX_JSON_TREE_ISSUES", 3):
            issues = _json_value_issues([object()] * 100_000)
        self.assertEqual(len(issues), 3)
        self.assertIn("3-issue limit", issues[-1])

    def test_config_lock_and_canonical_providers_share_depth_limit(self):
        nested = self._nested_json(5)
        with patch.object(utils, "MAX_JSON_TREE_DEPTH", 4):
            with self.assertRaisesRegex(ConfigError, "nested too deeply"):
                parse_config_bytes(
                    (
                        '{"project":"p","components":{},"value":'
                        + nested
                        + "}"
                    ).encode(),
                    Path("config.json"),
                )
            with self.assertRaisesRegex(LockfileError, "nested too deeply"):
                parse_lockfile_bytes(("{\"value\":" + nested + "}").encode())

            files = {"svc/value.json": ("{\"value\":" + nested + "}").encode()}
            json_context = ProviderContext(
                repo_root=Path("/repo"),
                component_path="svc",
                boundary_cfg={"paths": ["value.json"]},
                source="head",
                read_file=lambda path: files[path],
                list_files=lambda prefix: sorted(files),
            )
            json_result = JsonCanonicalProvider().resolve(json_context)
            self.assertEqual(json_result.status, "error")
            self.assertIn("nested too deeply", json_result.errors[0])

            openapi = (
                '{"openapi":"3.1.0","paths":{},"x-value":'
                + nested
                + "}"
            ).encode()
            openapi_context = ProviderContext(
                repo_root=Path("/repo"),
                component_path="svc",
                boundary_cfg={"paths": ["openapi.json"]},
                source="head",
                read_file=lambda path: openapi,
                list_files=lambda prefix: ["svc/openapi.json"],
            )
            openapi_result = OpenApiCanonicalProvider().resolve(openapi_context)
            self.assertEqual(openapi_result.status, "error")
            self.assertIn("nested too deeply", openapi_result.errors[0])

    def test_value_at_depth_limit_remains_valid(self):
        value = 0
        for _ in range(4):
            value = [value]
        with patch.object(utils, "MAX_JSON_TREE_DEPTH", 4):
            self.assertEqual(_json_value_issues(value), [])

    @unittest.skipUnless(
        hasattr(sys, "set_int_max_str_digits"),
        "Python runtime has no process-wide integer conversion limit",
    )
    def test_large_integer_diagnostics_are_setting_independent_and_bounded(self):
        integer_text = "9" * 1_000
        config_bytes = (
            '{"project":"p","defaults":{"compat_mode":'
            + integer_text
            + '},"components":{"svc":{"path":"svc","boundary":'
            '{"provider":"leaf"}}},"slices":{"public":{"mode":'
            + integer_text
            + ',"components":["svc"]}}}'
        ).encode("ascii")
        original_limit = sys.get_int_max_str_digits()
        outcomes = []
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "svc").mkdir()
                for limit in (640, 4_300, 0):
                    sys.set_int_max_str_digits(limit)
                    config = parse_config_bytes(
                        config_bytes,
                        Path("boundary.config.json"),
                    )
                    large_integer = config["defaults"]["compat_mode"]
                    outcomes.append(
                        (
                            validate_config(config, root),
                            _lockfile_schema_issues({"schema": large_integer}),
                            load_custom_providers(
                                [{"module": large_integer, "class": "Provider"}],
                                allow_custom=True,
                                registry={},
                            ),
                        )
                    )
        finally:
            sys.set_int_max_str_digits(original_limit)

        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[1], outcomes[2])
        messages = [
            message
            for group in outcomes[0]
            for message in group
        ]
        self.assertTrue(
            any("Unsupported defaults.compat_mode" in message for message in messages)
        )
        self.assertTrue(any("unknown mode" in message for message in messages))
        self.assertTrue(any("LOCKFILE schema unsupported" in message for message in messages))
        self.assertTrue(any("Provider entry missing" in message for message in messages))
        self.assertLessEqual(max(map(len, messages)), 600)


class AggregateReadBoundTests(unittest.TestCase):
    def setUp(self):
        self._ambient_config = patch(
            "boundver._git._ambient_worktree_config_overrides",
            return_value=(),
        )
        self._ambient_config.start()
        self.addCleanup(self._ambient_config.stop)

    @staticmethod
    def _batch_process(stdout):
        process = MagicMock()
        process.stdin = io.BytesIO()
        process.stdout = stdout
        process.stderr = io.BytesIO()
        process.poll.return_value = 0
        process.wait.return_value = 0
        return process

    def test_git_batch_rejects_advertised_size_before_content_read(self):
        stdout = _TrackingBytesIO(b"object-id blob 5\n12345\n")
        process = self._batch_process(stdout)
        with (
            patch("boundver._git.subprocess.Popen", return_value=process),
            patch("boundver._git.MAX_GIT_BATCH_BYTES", 4),
        ):
            with self.assertRaisesRegex(GuardrailError, "remaining aggregate"):
                git_helpers._git_batch_cat(Path("."), ["object-id"])
        self.assertEqual(stdout.read_sizes, [])

    def test_git_blob_stream_honors_dynamic_logical_remaining_budget(self):
        stdout = _TrackingBytesIO(b"object-id blob 3\nabc\n")
        process = self._batch_process(stdout)
        with patch("boundver._git.subprocess.Popen", return_value=process):
            stream = git_helpers._iter_git_blobs(
                Path("."),
                ["object-id"],
                max_total_bytes=10,
                remaining_bytes=lambda: 2,
            )
            with self.assertRaisesRegex(GuardrailError, "remaining aggregate"):
                next(stream)
        self.assertEqual(stdout.read_sizes, [])

    def test_working_tree_reads_use_remaining_logical_budget(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.txt").write_bytes(b"aa")
            (root / "b.txt").write_bytes(b"bb")
            requested_limits = []
            real_reader = hashing._read_bounded_path_bytes

            def read_limited(path, label, *, max_bytes=None):
                requested_limits.append(max_bytes)
                return real_reader(path, label, max_bytes=max_bytes)

            with (
                patch("boundver._hashing.MAX_HASH_TOTAL_BYTES", 3),
                patch(
                    "boundver._hashing._read_bounded_path_bytes",
                    side_effect=read_limited,
                ),
            ):
                with self.assertRaisesRegex(GuardrailError, "file too large"):
                    hashing.source_tree_digest(
                        root,
                        ".",
                        source="working-tree",
                    )
            self.assertEqual(requested_limits, [3, 1])

    def test_git_tree_stream_rechecks_budget_after_duplicate_blob(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(
                root,
                user_email="test@example.com",
                user_name="Test",
            )
            component = root / "svc"
            component.mkdir()
            (component / "a.txt").write_bytes(b"aa")
            (component / "b.txt").write_bytes(b"aa")
            (component / "c.txt").write_bytes(b"bb")
            commit_all(root, "initial")

            with patch("boundver._hashing.MAX_HASH_TOTAL_BYTES", 5):
                with self.assertRaisesRegex(
                    GuardrailError,
                    "remaining aggregate",
                ):
                    hashing.source_tree_digest(root, "svc", source="head")

    def test_symlink_size_is_checked_before_readlink(self):
        link_stat = types.SimpleNamespace(
            st_dev=1,
            st_ino=2,
            st_size=2,
            st_mtime_ns=3,
            st_mode=stat.S_IFLNK | 0o777,
        )
        with (
            patch.object(Path, "lstat", return_value=link_stat),
            patch("boundver._hashing.os.readlink") as readlink,
        ):
            with self.assertRaisesRegex(GuardrailError, "file too large"):
                hashing._read_path_content(
                    Path("/repo"),
                    Path("/repo/link"),
                    source="working-tree",
                    max_bytes=1,
                )
        readlink.assert_not_called()

if __name__ == "__main__":
    unittest.main()
