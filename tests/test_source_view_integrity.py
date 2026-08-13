"""End-to-end source-view integrity regressions."""

import json
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import boundver
import boundver.core as core
from boundver._config import load_config_file, parse_config_bytes, validate_config
from boundver._git import _capture_git_source_snapshot
from boundver._lockfile import (
    _SourceAccessor,
    _lockfile_structure_issues,
    generate_lockfile,
    generate_lockfile_for_components,
    load_lockfile_file,
    parse_lockfile_bytes,
    semantic_config_digest,
    verify_lockfile,
)
from boundver.providers import ResolvedBoundary
from boundver._utils import ConfigError, LockfileError


@contextmanager
def _cwd(path: Path):
    import os

    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )


def _init_repo(root: Path) -> dict:
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Source View Test")
    (root / "svc").mkdir()
    (root / "svc" / "main.py").write_text("value = 1\n", encoding="utf-8")
    config = {
        "project": "source-view",
        "components": {
            "svc": {
                "path": "svc",
                "boundary": {"provider": "implicit"},
            }
        },
        "slices": {},
    }
    (root / "boundary.config.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )
    _git(root, "add", "boundary.config.json", "svc/main.py")
    _git(root, "commit", "-m", "initial source")
    snapshot = _capture_git_source_snapshot(root, "head")
    lock = generate_lockfile(config, root, source="head", snapshot=snapshot)
    (root / "boundary.lock.json").write_text(
        json.dumps(lock, indent=2) + "\n", encoding="utf-8"
    )
    _git(root, "add", "boundary.lock.json")
    _git(root, "commit", "-m", "record lock")
    return config


def _configure_path_boundary(root: Path) -> dict:
    """Commit a current lock whose boundary provider has an explanation."""
    config = _init_repo(root)
    config = json.loads(json.dumps(config))
    config["components"]["svc"]["boundary"] = {
        "provider": "implicit",
        "paths": ["main.py"],
    }
    (root / "boundary.config.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )
    _git(root, "add", "boundary.config.json")
    _git(root, "commit", "-m", "declare boundary")
    snapshot = _capture_git_source_snapshot(root, "head")
    lock = generate_lockfile(config, root, source="head", snapshot=snapshot)
    (root / "boundary.lock.json").write_text(
        json.dumps(lock, indent=2) + "\n", encoding="utf-8"
    )
    _git(root, "add", "boundary.lock.json")
    _git(root, "commit", "-m", "record declared boundary")
    return config


def _run_cli(root: Path, *args: str):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch.object(sys, "argv", ["boundver", *args]), patch(
        "boundver.core.git_root", return_value=root
    ):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                core.main()
            except SystemExit as exc:
                return exc.code, stdout.getvalue(), stderr.getvalue()
    return 0, stdout.getvalue(), stderr.getvalue()


class SourceViewIntegrityTests(unittest.TestCase):
    def test_explain_head_ignores_unstaged_config_and_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "svc" / "main.py").write_text(
                "value = 2\n", encoding="utf-8"
            )
            _git(root, "add", "svc/main.py")
            _git(root, "commit", "-m", "change selected head")

            (root / "boundary.config.json").write_text(
                '{"project":"unreviewed","components":{},"slices":{}}\n',
                encoding="utf-8",
            )
            (root / "boundary.lock.json").write_text(
                "not reviewed JSON\n", encoding="utf-8"
            )

            with patch(
                "boundver.core._capture_git_source_snapshot",
                wraps=_capture_git_source_snapshot,
            ) as capture:
                rc, stdout, stderr = _run_cli(
                    root, "explain", "svc", "--source", "head"
                )
            self.assertEqual(rc, 0, stderr)
            self.assertIn("svc/main.py", stdout)
            self.assertIn("Source: head", stdout)
            self.assertNotIn("Traceback", stdout + stderr)
            self.assertEqual(capture.call_count, 1)

    def test_explain_index_ignores_unstaged_config_and_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "svc" / "main.py").write_text(
                "value = 2\n", encoding="utf-8"
            )
            _git(root, "add", "svc/main.py")

            (root / "boundary.config.json").write_text(
                '{"project":"unreviewed","components":{},"slices":{}}\n',
                encoding="utf-8",
            )
            (root / "boundary.lock.json").write_text(
                "not reviewed JSON\n", encoding="utf-8"
            )

            with patch(
                "boundver.core._capture_git_source_snapshot",
                wraps=_capture_git_source_snapshot,
            ) as capture:
                rc, stdout, stderr = _run_cli(
                    root, "explain", "svc", "--source", "index"
                )
            self.assertEqual(rc, 0, stderr)
            self.assertIn("svc/main.py", stdout)
            self.assertIn("Source: index", stdout)
            self.assertNotIn("Traceback", stdout + stderr)
            self.assertEqual(capture.call_count, 1)

    def test_why_head_uses_one_snapshot_despite_unstaged_config_and_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _configure_path_boundary(root)
            (root / "svc" / "main.py").write_text(
                "value = 2\n", encoding="utf-8"
            )
            _git(root, "add", "svc/main.py")
            _git(root, "commit", "-m", "drift selected head")

            (root / "boundary.config.json").write_text(
                '{"project":"unreviewed","components":{},"slices":{}}\n',
                encoding="utf-8",
            )
            (root / "boundary.lock.json").write_text(
                "not reviewed JSON\n", encoding="utf-8"
            )

            with patch(
                "boundver.core._capture_git_source_snapshot",
                wraps=_capture_git_source_snapshot,
            ) as capture, patch(
                "boundver._lockfile._capture_git_source_snapshot",
                side_effect=AssertionError("why recaptured its source"),
            ):
                rc, stdout, stderr = _run_cli(
                    root, "why", "svc", "--source", "head"
                )
            self.assertEqual(rc, 1, stderr)
            self.assertIn("Status: DRIFTED", stdout)
            self.assertIn("Provider detail:", stdout)
            self.assertIn("--source head", stdout)
            self.assertNotIn("Traceback", stdout + stderr)
            self.assertEqual(capture.call_count, 1)

    def test_why_index_uses_one_snapshot_despite_unstaged_config_and_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _configure_path_boundary(root)
            (root / "svc" / "main.py").write_text(
                "value = 2\n", encoding="utf-8"
            )
            _git(root, "add", "svc/main.py")

            (root / "boundary.config.json").write_text(
                '{"project":"unreviewed","components":{},"slices":{}}\n',
                encoding="utf-8",
            )
            (root / "boundary.lock.json").write_text(
                "not reviewed JSON\n", encoding="utf-8"
            )

            with patch(
                "boundver.core._capture_git_source_snapshot",
                wraps=_capture_git_source_snapshot,
            ) as capture, patch(
                "boundver._lockfile._capture_git_source_snapshot",
                side_effect=AssertionError("why recaptured its source"),
            ):
                rc, stdout, stderr = _run_cli(
                    root, "why", "svc", "--source", "index"
                )
            self.assertEqual(rc, 1, stderr)
            self.assertIn("Status: DRIFTED", stdout)
            self.assertIn("Provider detail:", stdout)
            self.assertIn("--source index", stdout)
            self.assertNotIn("Traceback", stdout + stderr)
            self.assertEqual(capture.call_count, 1)

    def test_unstaged_regenerated_lock_cannot_mask_index_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "svc" / "main.py").write_text("value = 2\n", encoding="utf-8")
            _git(root, "add", "svc/main.py")

            index_snapshot = _capture_git_source_snapshot(root, "index")
            index_config = load_config_file(
                root / "boundary.config.json",
                repo_root=root,
                snapshot=index_snapshot,
            )
            regenerated = generate_lockfile(
                index_config, root, source="index", snapshot=index_snapshot
            )
            # This fresh lock exists only on disk, not in the selected index.
            (root / "boundary.lock.json").write_text(
                json.dumps(regenerated, indent=2) + "\n", encoding="utf-8"
            )

            selected_lock = load_lockfile_file(
                root / "boundary.lock.json",
                repo_root=root,
                snapshot=index_snapshot,
            )
            issues = verify_lockfile(
                index_config,
                selected_lock,
                root,
                source="index",
                snapshot=index_snapshot,
            )
            self.assertTrue(any("svc.exact" in issue for issue in issues), issues)

            with _cwd(root):
                public_issues = boundver.verify(source="index")
            self.assertTrue(
                any("svc.exact" in issue for issue in public_issues),
                public_issues,
            )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(
                sys,
                "argv",
                ["boundver", "verify", "--source", "index", "--format", "json"],
            ), patch("boundver.core.git_root", return_value=root):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        core.main()
            self.assertNotEqual(raised.exception.code, 0)
            cli_payload = json.loads(stdout.getvalue())
            self.assertTrue(
                any("svc.exact" in issue for issue in cli_payload["issues"]),
                cli_payload,
            )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(
                sys,
                "argv",
                [
                    "boundver",
                    "status",
                    "--source",
                    "index",
                    "--strict",
                    "--format",
                    "json",
                ],
            ), patch("boundver.core.git_root", return_value=root):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        core.main()
            self.assertNotEqual(raised.exception.code, 0)
            status_payload = json.loads(stdout.getvalue())
            self.assertTrue(
                any("svc.exact" in issue for issue in status_payload["issues"]),
                status_payload,
            )

    def test_unstaged_config_cannot_change_index_generation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            committed = _init_repo(root)
            staged = dict(committed)
            staged["project"] = "index-selected"
            (root / "boundary.config.json").write_text(
                json.dumps(staged, indent=2) + "\n", encoding="utf-8"
            )
            _git(root, "add", "boundary.config.json")
            unstaged = dict(staged)
            unstaged["project"] = "working-tree-only"
            (root / "boundary.config.json").write_text(
                json.dumps(unstaged, indent=2) + "\n", encoding="utf-8"
            )

            with _cwd(root):
                generated = boundver.generate(source="index", out_path=None)
            self.assertEqual(generated["project"], "index-selected")

    def test_explicit_head_config_can_be_deleted_from_working_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = _init_repo(root)
            custom_path = root / "selected.config.json"
            custom_path.write_text(
                json.dumps(config, indent=2) + "\n", encoding="utf-8"
            )
            _git(root, "add", custom_path.name)
            _git(root, "commit", "-m", "add explicit selected config")
            custom_path.unlink()

            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(
                sys,
                "argv",
                [
                    "boundver",
                    "generate",
                    "--config",
                    custom_path.name,
                    "--source",
                    "head",
                    "--dry-run",
                    "--format",
                    "json",
                ],
            ), patch("boundver.core.git_root", return_value=root):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    try:
                        core.main()
                    except SystemExit as exc:
                        self.fail(f"source-backed generate exited {exc.code}: {stderr.getvalue()}")
            generated = json.loads(stdout.getvalue())
            self.assertEqual(generated["project"], "source-view")

    def test_accessor_reuses_explicit_snapshot_without_recapture(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = _init_repo(root)
            snapshot = _capture_git_source_snapshot(root, "head")
            with patch(
                "boundver._lockfile._capture_git_source_snapshot",
                side_effect=AssertionError("unexpected recapture"),
            ):
                accessor = _SourceAccessor(root, "head", snapshot=snapshot)
                self.assertIs(accessor.snapshot, snapshot)
                generate_lockfile(
                    config, root, source="head", snapshot=snapshot
                )

            with patch(
                "boundver.core._capture_git_source_snapshot",
                wraps=_capture_git_source_snapshot,
            ) as operation_capture, patch(
                "boundver._lockfile._capture_git_source_snapshot",
                side_effect=AssertionError("operation recaptured its source"),
            ):
                with _cwd(root):
                    boundver.generate(source="head", out_path=None)
            self.assertEqual(operation_capture.call_count, 1)

    def test_accessor_memoizes_tag_resolution_per_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            snapshot = _capture_git_source_snapshot(root, "head")
            accessor = _SourceAccessor(root, "head", snapshot=snapshot)
            with patch(
                "boundver._lockfile.git_latest_tag", return_value="1.2.3"
            ) as resolve_tag:
                self.assertEqual(accessor.latest_tag(root, "svc-v"), "1.2.3")
                self.assertEqual(accessor.latest_tag(root, "svc-v"), "1.2.3")
            resolve_tag.assert_called_once_with(
                root, "svc-v", ref=snapshot.head_oid
            )

    def test_partial_index_generation_does_not_use_working_tree_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            _git(root, "rm", "--cached", "boundary.lock.json")
            self.assertTrue((root / "boundary.lock.json").is_file())
            snapshot = _capture_git_source_snapshot(root, "index")
            config = load_config_file(
                root / "boundary.config.json",
                repo_root=root,
                snapshot=snapshot,
            )
            with self.assertRaisesRegex(ConfigError, "selected index source"):
                generate_lockfile_for_components(
                    config,
                    root,
                    selected_components=["svc"],
                    out_path=root / "boundary.lock.json",
                    source="index",
                    snapshot=snapshot,
                )

    def test_flapping_provider_version_becomes_controlled_generation_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = _init_repo(root)
            config["components"]["svc"]["boundary"] = {
                "provider": "custom.flapping"
            }
            reads = {"count": 0}

            class FlappingProvider:
                name = "custom.flapping"

                @property
                def version(self):
                    reads["count"] += 1
                    if reads["count"] >= 2:
                        raise RuntimeError("version disappeared")
                    return "1"

                def resolve(self, ctx):
                    return ResolvedBoundary(entries=[("contract", b"value")])

            snapshot = _capture_git_source_snapshot(root, "head")
            registry = {"custom.flapping": FlappingProvider()}
            accessor = _SourceAccessor(root, "head", snapshot=snapshot)
            with patch("boundver._lockfile.create_registry", return_value=registry), patch(
                "boundver._lockfile.load_custom_providers", return_value=[]
            ):
                with self.assertRaisesRegex(ConfigError, "version.*could not be read"):
                    generate_lockfile(
                        config, root, source="head", snapshot=snapshot
                    )

    def test_status_strict_reports_missing_selected_source_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            _git(root, "rm", "boundary.config.json")
            _git(root, "commit", "-m", "remove selected config")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(
                sys,
                "argv",
                [
                    "boundver",
                    "status",
                    "--source",
                    "head",
                    "--strict",
                    "--format",
                    "json",
                ],
            ), patch("boundver.core.git_root", return_value=root):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        core.main()
            self.assertEqual(raised.exception.code, core.EXIT_USAGE)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(
                any("Config unavailable" in issue for issue in payload["issues"]),
                payload,
            )

    def test_status_strict_classifies_verification_failure_as_usage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(
                sys,
                "argv",
                [
                    "boundver",
                    "status",
                    "--source",
                    "head",
                    "--strict",
                    "--format",
                    "json",
                ],
            ), patch("boundver.core.git_root", return_value=root), patch(
                "boundver.core.verify_lockfile",
                side_effect=ConfigError("selected source failed"),
            ):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        core.main()
            self.assertEqual(raised.exception.code, core.EXIT_USAGE)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(
                any("Verification error" in issue for issue in payload["issues"]),
                payload,
            )

    def test_selected_source_validation_reports_missing_version_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = _init_repo(root)
            config = json.loads(json.dumps(config))
            config["components"]["svc"]["version_source"] = {
                "file": "version.json",
                "field": "version",
            }
            (root / "boundary.config.json").write_text(
                json.dumps(config, indent=2) + "\n", encoding="utf-8"
            )
            _git(root, "add", "boundary.config.json")
            _git(root, "commit", "-m", "configure missing source version")
            snapshot = _capture_git_source_snapshot(root, "head")
            selected = load_config_file(
                root / "boundary.config.json",
                repo_root=root,
                snapshot=snapshot,
            )
            issues = validate_config(
                selected, root, source="head", snapshot=snapshot
            )
            self.assertTrue(
                any("not found in captured head source" in issue for issue in issues),
                issues,
            )


class ParserIntegrityTests(unittest.TestCase):
    def test_duplicate_json_keys_are_rejected_for_config_and_lock(self):
        with self.assertRaisesRegex(ConfigError, "duplicate JSON object key"):
            parse_config_bytes(b'{"project":"a","project":"b"}', Path("c.json"))
        with self.assertRaisesRegex(LockfileError, "duplicate JSON object key"):
            parse_lockfile_bytes(b'{"schema":"a","schema":"b"}')

        huge_integer = (b'{"number":' + b"9" * 5000 + b"}")
        with self.assertRaises(ConfigError):
            parse_config_bytes(huge_integer, Path("c.json"))
        with self.assertRaises(LockfileError):
            parse_lockfile_bytes(huge_integer)

    def test_lock_loaders_share_strict_json_interpretation(self):
        duplicate = b'{"schema":"a","schema":"b"}'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lock_path = root / "boundary.lock.json"
            lock_path.write_bytes(duplicate)
            with self.assertRaisesRegex(LockfileError, "duplicate JSON object key"):
                load_lockfile_file(lock_path)

            _git(root, "init")
            _git(root, "config", "user.email", "test@example.invalid")
            _git(root, "config", "user.name", "Source View Test")
            _git(root, "add", lock_path.name)
            _git(root, "commit", "-m", "malformed reviewed lock")
            snapshot = _capture_git_source_snapshot(root, "head")
            with self.assertRaisesRegex(LockfileError, "duplicate JSON object key"):
                load_lockfile_file(
                    lock_path, repo_root=root, snapshot=snapshot
                )

    def test_duplicate_and_non_string_yaml_keys_are_rejected(self):
        with self.assertRaisesRegex(ConfigError, "duplicate YAML mapping key"):
            parse_config_bytes(
                b"project: one\nproject: two\ncomponents: {}\n",
                Path("c.yaml"),
            )
        with self.assertRaisesRegex(ConfigError, "mapping keys must be strings"):
            parse_config_bytes(
                b"project: p\ncomponents: {}\n1: value\n",
                Path("c.yaml"),
            )
        with self.assertRaisesRegex(ConfigError, "aliases are not supported"):
            parse_config_bytes(
                b"project: p\ncomponents: &all {}\nslices: *all\n",
                Path("c.yaml"),
            )

    def test_non_json_scalars_and_nonfinite_values_are_rejected(self):
        with self.assertRaisesRegex(ConfigError, "non-JSON scalar type"):
            parse_config_bytes(
                b"project: p\nreleased: 2026-08-12\ncomponents: {}\n",
                Path("c.yaml"),
            )
        with self.assertRaisesRegex(ConfigError, "non-JSON scalar type"):
            parse_config_bytes(
                b'project = "p"\nreleased = 2026-08-12T12:30:00Z\n',
                Path("c.toml"),
            )
        issues = validate_config(
            {"project": "p", "components": {}, "bad": float("nan")},
            Path("/repo"),
        )
        self.assertTrue(any("non-finite" in issue for issue in issues), issues)
        with self.assertRaisesRegex(ConfigError, "non-finite"):
            parse_config_bytes(b'{"project":NaN}', Path("c.json"))
        with self.assertRaisesRegex(LockfileError, "non-finite"):
            parse_lockfile_bytes(b'{"config_digest":Infinity}')

    def test_deep_config_and_lock_inputs_fail_with_controlled_errors(self):
        deeply_nested = "[" * 1200 + "0" + "]" * 1200
        with self.assertRaises(ConfigError):
            parse_config_bytes(
                ('{"project":"p","nested":' + deeply_nested + "}").encode(),
                Path("c.json"),
            )
        with self.assertRaises(LockfileError):
            parse_lockfile_bytes(("{\"nested\":" + deeply_nested + "}").encode())

        nested_value = 0
        for _ in range(1200):
            nested_value = [nested_value]
        direct_config = {
            "project": "p",
            "components": {},
            "nested": nested_value,
        }
        issues = validate_config(direct_config, Path("/repo"))
        self.assertTrue(any("nested too deeply" in issue for issue in issues), issues)
        with self.assertRaisesRegex(ConfigError, "nested too deeply"):
            semantic_config_digest(direct_config)

    def test_source_config_size_limit_is_enforced(self):
        with patch("boundver._config.MAX_CONFIG_BYTES", 8):
            with self.assertRaisesRegex(ConfigError, "too large"):
                parse_config_bytes(b'{"project":"too large"}', Path("c.json"))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git(root, "init")
            _git(root, "config", "user.email", "test@example.invalid")
            _git(root, "config", "user.name", "Source View Test")
            config_path = root / "boundary.config.json"
            lock_path = root / "boundary.lock.json"
            config_path.write_bytes(b'{"project":"too large"}')
            lock_path.write_bytes(b'{"schema":"too large"}')
            _git(root, "add", config_path.name, lock_path.name)
            _git(root, "commit", "-m", "oversized source inputs")
            snapshot = _capture_git_source_snapshot(root, "head")
            with patch("boundver._config.MAX_CONFIG_BYTES", 8):
                with self.assertRaisesRegex(ConfigError, "too large"):
                    load_config_file(
                        config_path, repo_root=root, snapshot=snapshot
                    )
            with patch("boundver._lockfile.MAX_LOCKFILE_BYTES", 8):
                with self.assertRaisesRegex(LockfileError, "limit"):
                    load_lockfile_file(
                        lock_path, repo_root=root, snapshot=snapshot
                    )
                with self.assertRaisesRegex(LockfileError, "limit"):
                    load_lockfile_file(lock_path)
            with patch("boundver._git.MAX_GIT_BLOB_BYTES", 8):
                with self.assertRaisesRegex(ConfigError, "Cannot read config"):
                    load_config_file(
                        config_path, repo_root=root, snapshot=snapshot
                    )
                with self.assertRaisesRegex(LockfileError, "Cannot read lockfile"):
                    load_lockfile_file(
                        lock_path, repo_root=root, snapshot=snapshot
                    )

    def test_working_tree_validation_rejects_untracked_version_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git(root, "init")
            _git(root, "config", "user.email", "test@example.invalid")
            _git(root, "config", "user.name", "Source View Test")
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x = 1\n", encoding="utf-8")
            _git(root, "add", "svc/main.py")
            _git(root, "commit", "-m", "tracked component")
            (root / "svc" / "version.json").write_text(
                '{"version":"1.0.0"}\n', encoding="utf-8"
            )
            config = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "version_source": {
                            "file": "version.json",
                            "field": "version",
                        },
                        "boundary": {"provider": "implicit"},
                    }
                },
                "slices": {},
            }
            issues = validate_config(config, root, source="working-tree")
            self.assertTrue(any("must be tracked in Git" in issue for issue in issues), issues)

    def test_unborn_empty_index_allows_filesystem_version_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git(root, "init")
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x = 1\n", encoding="utf-8")
            (root / "svc" / "version.json").write_text(
                '{"version":"1.0.0"}\n', encoding="utf-8"
            )
            config = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "version_source": {
                            "file": "version.json",
                            "field": "version",
                        },
                        "boundary": {"provider": "implicit"},
                    }
                },
                "slices": {},
            }
            issues = validate_config(config, root, source="working-tree")
            self.assertFalse(
                any("version_source.file" in issue for issue in issues), issues
            )

    def test_lock_schema_and_runtime_reject_malformed_nested_digests(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "spec" / "boundary.lock.schema.json").read_text(
                encoding="utf-8"
            )
        )
        digest_schema = schema["properties"]["components"]["additionalProperties"][
            "properties"
        ]["fingerprints"]["properties"]["exact"]
        self.assertIn("^[0-9a-f]{64}$", json.dumps(digest_schema))

        lock = {
            "schema": "boundary-lock/v3",
            "config_contract": "boundver-semantic-config/v1",
            "config_digest": "0" * 64,
            "project": "p",
            "components": {
                "svc": {
                    "version": None,
                    "path": "svc",
                    "boundary_provider": "implicit",
                    "boundary_provider_version": "2",
                    "boundary_status": "partial",
                    "consumers": [],
                    "external_consumers": [],
                    "fingerprints": {
                        "exact": "not-a-digest",
                        "behavior": None,
                        "boundary": None,
                        "compat": None,
                    },
                    "semver": {
                        "compat_family": None,
                        "api_surface": None,
                        "exact_version": None,
                    },
                }
            },
            "slices": {},
        }
        issues = _lockfile_structure_issues(lock)
        self.assertTrue(any("lowercase SHA-256" in issue for issue in issues), issues)

        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "malformed.lock.json"
            lock_path.write_text(json.dumps(lock), encoding="utf-8")
            stderr = io.StringIO()
            with patch.object(
                sys,
                "argv",
                [
                    "boundver",
                    "diff",
                    str(lock_path),
                    str(lock_path),
                    "--format",
                    "json",
                ],
            ):
                with redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        core.main()
            self.assertEqual(raised.exception.code, core.EXIT_USAGE)
            self.assertIn("lowercase SHA-256", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
