"""Regression coverage for destructive and self-referential lock paths."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import boundver
import boundver.core as core
from tests._repo_fixtures import commit_all, init_git_repo


SOURCES = ("head", "index", "working-tree")


def _make_directory_link(link: Path, target: Path) -> None:
    """Create a directory symlink, falling back to a Windows junction."""
    symlink_failure = None
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as exc:
        if os.name != "nt":
            raise
        symlink_failure = exc
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise OSError(
            "directory symlink and junction creation both failed: "
            f"{symlink_failure}; {result.stderr.strip()}"
        ) from symlink_failure


def _run_cli(root: Path, *args: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = 0
    with patch.object(sys, "argv", ["boundver", *args]), patch(
        "boundver.core.git_root", return_value=root
    ):
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                core.main()
        except SystemExit as exc:
            exit_code = int(exc.code) if exc.code is not None else 0
    return exit_code, stdout.getvalue(), stderr.getvalue()


def _config_text(config_format: str, *, vendored: bool) -> str:
    config = {
        "project": "lock-path-safety",
        "components": {
            "svc": {
                "path": "svc",
                "boundary": {"provider": "implicit"},
            }
        },
        "slices": {},
    }
    if vendored:
        config["components"]["svc"]["vendored_copies"] = ["vendor/svc"]
    if config_format == "json":
        return json.dumps(config, indent=2) + "\n"
    vendored_yaml = "    vendored_copies:\n      - vendor/svc\n" if vendored else ""
    if config_format == "yaml":
        return (
            "project: lock-path-safety\n"
            "components:\n"
            "  svc:\n"
            "    path: svc\n"
            f"{vendored_yaml}"
            "    boundary:\n"
            "      provider: implicit\n"
            "slices: {}\n"
        )
    vendored_toml = 'vendored_copies = ["vendor/svc"]\n' if vendored else ""
    if config_format == "toml":
        return (
            'project = "lock-path-safety"\n'
            "[components.svc]\n"
            'path = "svc"\n'
            f"{vendored_toml}"
            "[components.svc.boundary]\n"
            'provider = "implicit"\n'
            "[slices]\n"
        )
    raise AssertionError(f"unsupported test config format: {config_format}")


def _build_repo(
    root: Path,
    *,
    config_format: str = "json",
    vendored: bool = False,
    config_dir: str = "",
) -> Path:
    init_git_repo(root)
    (root / "svc").mkdir()
    (root / "svc" / "main.py").write_text("value = 1\n", encoding="utf-8")
    if vendored:
        (root / "vendor" / "svc").mkdir(parents=True)
        (root / "vendor" / "svc" / "main.py").write_text(
            "value = 1\n", encoding="utf-8"
        )
    suffix = {"json": "json", "yaml": "yaml", "toml": "toml"}[config_format]
    parent = root / config_dir
    parent.mkdir(parents=True, exist_ok=True)
    config_path = parent / f"boundary.config.{suffix}"
    config_path.write_text(
        _config_text(config_format, vendored=vendored), encoding="utf-8"
    )
    commit_all(root)
    return config_path


class ConfigAliasSafetyTests(unittest.TestCase):
    def test_cli_generate_rejects_config_output_for_every_format_and_source(self):
        for config_format in ("json", "yaml", "toml"):
            with self.subTest(config_format=config_format), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                config_path = _build_repo(root, config_format=config_format)
                config_label = config_path.relative_to(root).as_posix()
                expected = config_path.read_bytes()
                for source in SOURCES:
                    with self.subTest(source=source), patch.object(
                        core, "generate_lockfile"
                    ) as generate_lockfile:
                        code, _stdout, stderr = _run_cli(
                            root,
                            "generate",
                            "--config",
                            config_label,
                            "--source",
                            source,
                            "--out",
                            config_label,
                        )
                    self.assertEqual(code, core.EXIT_USAGE, stderr)
                    self.assertIn("aliases the selected config", stderr)
                    self.assertEqual(config_path.read_bytes(), expected)
                    generate_lockfile.assert_not_called()

    def test_cli_generate_rejects_absolute_and_normalized_config_aliases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = _build_repo(root)
            expected = config_path.read_bytes()
            aliases = (
                str(config_path.resolve()),
                (Path("unused") / ".." / config_path.name).as_posix(),
            )
            for alias in aliases:
                with self.subTest(alias=alias), patch.object(
                    core, "generate_lockfile"
                ) as generate_lockfile:
                    code, _stdout, stderr = _run_cli(
                        root,
                        "generate",
                        "--source",
                        "working-tree",
                        "--out",
                        alias,
                    )
                self.assertEqual(code, core.EXIT_USAGE, stderr)
                self.assertIn("aliases the selected config", stderr)
                self.assertEqual(config_path.read_bytes(), expected)
                generate_lockfile.assert_not_called()

    def test_cli_generate_rejects_symlinked_parent_alias(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = _build_repo(root, config_dir="config")
            alias = root / "config-alias"
            try:
                _make_directory_link(alias, config_path.parent)
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")
            expected = config_path.read_bytes()

            with patch.object(core, "generate_lockfile") as generate_lockfile:
                code, _stdout, stderr = _run_cli(
                    root,
                    "generate",
                    "--config",
                    config_path.relative_to(root).as_posix(),
                    "--source",
                    "working-tree",
                    "--out",
                    (alias / config_path.name).relative_to(root).as_posix(),
                )

            self.assertEqual(code, core.EXIT_USAGE, stderr)
            self.assertIn("aliases the selected config", stderr)
            self.assertEqual(config_path.read_bytes(), expected)
            generate_lockfile.assert_not_called()

    def test_cli_generate_rejects_output_parent_redirected_outside_repo(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "repo"
            root.mkdir()
            _build_repo(root)
            outside = base / "outside"
            outside.mkdir()
            redirected = outside / "boundary.lock.json"
            redirected.write_text("outside\n", encoding="utf-8")
            try:
                _make_directory_link(root / "artifacts", outside)
            except OSError as exc:
                self.skipTest(f"directory links are unavailable: {exc}")

            code, _stdout, stderr = _run_cli(
                root,
                "generate",
                "--source",
                "working-tree",
                "--out",
                "artifacts/boundary.lock.json",
            )

            self.assertEqual(code, core.EXIT_USAGE, stderr)
            self.assertRegex(stderr, "symlink|junction|reparse")
            self.assertEqual(
                redirected.read_text(encoding="utf-8"),
                "outside\n",
            )

    def test_cli_generate_resolves_symlink_before_parent_segments_for_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = _build_repo(root)
            (root / "deep").mkdir()
            (root / "outer").mkdir()
            alias = root / "outer" / "alias"
            try:
                _make_directory_link(alias, root / "deep")
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")
            expected = config_path.read_bytes()
            output = (Path("outer") / "alias" / ".." / config_path.name).as_posix()

            with patch.object(core, "generate_lockfile") as generate_lockfile:
                code, _stdout, stderr = _run_cli(
                    root,
                    "generate",
                    "--source",
                    "working-tree",
                    "--out",
                    output,
                )

            self.assertEqual(code, core.EXIT_USAGE, stderr)
            self.assertIn("aliases the selected config", stderr)
            self.assertEqual(config_path.read_bytes(), expected)
            generate_lockfile.assert_not_called()

    def test_cli_generate_rejects_existing_hardlink_alias(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = _build_repo(root)
            alias = root / "config-hardlink.json"
            try:
                os.link(config_path, alias)
            except OSError as exc:
                self.skipTest(f"hard links are unavailable: {exc}")
            expected = config_path.read_bytes()

            with patch.object(core, "generate_lockfile") as generate_lockfile:
                code, _stdout, stderr = _run_cli(
                    root,
                    "generate",
                    "--source",
                    "working-tree",
                    "--out",
                    alias.name,
                )

            self.assertEqual(code, core.EXIT_USAGE, stderr)
            self.assertIn("aliases the selected config", stderr)
            self.assertEqual(config_path.read_bytes(), expected)
            self.assertEqual(alias.read_bytes(), expected)
            generate_lockfile.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows paths are case-insensitive")
    def test_cli_generate_rejects_case_only_config_alias(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = _build_repo(root)
            alias = root / config_path.name.upper()
            if not alias.exists():
                self.skipTest("the test volume is case-sensitive")
            expected = config_path.read_bytes()

            code, _stdout, stderr = _run_cli(
                root,
                "generate",
                "--source",
                "working-tree",
                "--out",
                alias.name,
            )

            self.assertEqual(code, core.EXIT_USAGE, stderr)
            self.assertIn("aliases the selected config", stderr)
            self.assertEqual(config_path.read_bytes(), expected)

    def test_public_generate_rejects_config_output_for_every_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = _build_repo(root)
            expected = config_path.read_bytes()
            with patch("boundver._git.git_root", return_value=root):
                for source in SOURCES:
                    with self.subTest(source=source), patch(
                        "boundver._lockfile.generate_lockfile"
                    ) as generate_lockfile:
                        with self.assertRaisesRegex(
                            boundver.ConfigError, "aliases the selected config"
                        ):
                            boundver.generate(
                                source=source,
                                out_path=config_path.name,
                            )
                    self.assertEqual(config_path.read_bytes(), expected)
                    generate_lockfile.assert_not_called()

    def test_cli_and_public_verify_reject_config_as_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = _build_repo(root)
            expected = config_path.read_bytes()
            for source in SOURCES:
                with self.subTest(interface="cli", source=source), patch.object(
                    core, "_load_lockfile"
                ) as load_lockfile:
                    code, _stdout, stderr = _run_cli(
                        root,
                        "verify",
                        "--source",
                        source,
                        "--lock",
                        config_path.name,
                        "--update",
                    )
                self.assertEqual(code, core.EXIT_USAGE, stderr)
                self.assertIn("aliases the selected config", stderr)
                self.assertEqual(config_path.read_bytes(), expected)
                load_lockfile.assert_not_called()

                with self.subTest(interface="api", source=source), patch(
                    "boundver._git.git_root", return_value=root
                ), patch.object(core, "_load_lockfile") as load_lockfile:
                    with self.assertRaisesRegex(
                        boundver.ConfigError, "aliases the selected config"
                    ):
                        boundver.verify(
                            source=source,
                            lock_path=config_path.name,
                        )
                self.assertEqual(config_path.read_bytes(), expected)
                load_lockfile.assert_not_called()

    def test_valid_custom_lock_path_remains_supported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _build_repo(root)
            output = root / "artifacts" / "reviewed.lock.json"

            code, _stdout, stderr = _run_cli(
                root,
                "generate",
                "--source",
                "working-tree",
                "--out",
                output.relative_to(root).as_posix(),
            )

            self.assertEqual(code, core.EXIT_OK, stderr)
            self.assertTrue(output.is_file())
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["schema"],
                core.LOCKFILE_SCHEMA,
            )

class VendoredCopyLockSafetyTests(unittest.TestCase):
    def test_cli_generate_rejects_vendored_output_for_every_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = _build_repo(root, vendored=True)
            expected_config = config_path.read_bytes()
            expected_copy = (root / "vendor" / "svc" / "main.py").read_bytes()
            output = root / "vendor" / "svc" / "boundary.lock.json"
            for source in SOURCES:
                with self.subTest(source=source), patch.object(
                    core, "generate_lockfile"
                ) as generate_lockfile:
                    code, _stdout, stderr = _run_cli(
                        root,
                        "generate",
                        "--source",
                        source,
                        "--out",
                        output.relative_to(root).as_posix(),
                    )
                self.assertEqual(code, core.EXIT_USAGE, stderr)
                self.assertIn("inside vendored copy root", stderr)
                self.assertFalse(output.exists())
                self.assertEqual(config_path.read_bytes(), expected_config)
                self.assertEqual(
                    (root / "vendor" / "svc" / "main.py").read_bytes(),
                    expected_copy,
                )
                generate_lockfile.assert_not_called()

    def test_cli_verify_and_update_reject_vendored_lock_for_every_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _build_repo(root, vendored=True)
            output = root / "vendor" / "svc" / "boundary.lock.json"
            for source in SOURCES:
                for update in (False, True):
                    args = [
                        "verify",
                        "--source",
                        source,
                        "--lock",
                        output.relative_to(root).as_posix(),
                    ]
                    if update:
                        args.append("--update")
                    with self.subTest(source=source, update=update), patch.object(
                        core, "_load_lockfile"
                    ) as load_lockfile:
                        code, _stdout, stderr = _run_cli(root, *args)
                    self.assertEqual(code, core.EXIT_USAGE, stderr)
                    self.assertIn("inside vendored copy root", stderr)
                    self.assertFalse(output.exists())
                    load_lockfile.assert_not_called()

    def test_public_generate_and_verify_reject_vendored_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _build_repo(root, vendored=True)
            output_label = "vendor/svc/boundary.lock.json"
            output = root / output_label
            with patch("boundver._git.git_root", return_value=root):
                for source in SOURCES:
                    with self.subTest(operation="generate", source=source), patch(
                        "boundver._lockfile.generate_lockfile"
                    ) as generate_lockfile:
                        with self.assertRaisesRegex(
                            boundver.ConfigError, "inside vendored copy root"
                        ):
                            boundver.generate(
                                source=source,
                                out_path=output_label,
                            )
                    self.assertFalse(output.exists())
                    generate_lockfile.assert_not_called()

                    with self.subTest(operation="verify", source=source), patch.object(
                        core, "_load_lockfile"
                    ) as load_lockfile:
                        with self.assertRaisesRegex(
                            boundver.ConfigError, "inside vendored copy root"
                        ):
                            boundver.verify(
                                source=source,
                                lock_path=output_label,
                            )
                    self.assertFalse(output.exists())
                    load_lockfile.assert_not_called()

    def test_cli_generate_rejects_symlink_alias_into_vendored_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _build_repo(root, vendored=True)
            alias = root / "vendor-alias"
            try:
                _make_directory_link(alias, root / "vendor" / "svc")
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")
            output = alias / "boundary.lock.json"

            code, _stdout, stderr = _run_cli(
                root,
                "generate",
                "--source",
                "working-tree",
                "--out",
                output.relative_to(root).as_posix(),
            )

            self.assertEqual(code, core.EXIT_USAGE, stderr)
            self.assertIn("inside vendored copy root", stderr)
            self.assertFalse(output.exists())

    def test_cli_generate_resolves_symlink_before_parent_segments_for_component(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _build_repo(root, vendored=True)
            (root / "svc" / "deep").mkdir()
            alias = root / "component-alias"
            try:
                _make_directory_link(alias, root / "svc" / "deep")
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")
            output = Path("component-alias") / ".." / "boundary.lock.json"

            with patch.object(core, "generate_lockfile") as generate_lockfile:
                code, _stdout, stderr = _run_cli(
                    root,
                    "generate",
                    "--source",
                    "working-tree",
                    "--out",
                    output.as_posix(),
                )

            self.assertEqual(code, core.EXIT_USAGE, stderr)
            self.assertIn("inside component root", stderr)
            self.assertFalse((root / "svc" / "boundary.lock.json").exists())
            generate_lockfile.assert_not_called()

    def test_staged_self_referential_lock_cannot_be_verified_or_regenerated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = _build_repo(root, vendored=True)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            lockfile = core.generate_lockfile(config, root, source="head")
            output = root / "vendor" / "svc" / "boundary.lock.json"
            output.write_text(json.dumps(lockfile, indent=2) + "\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", output.relative_to(root).as_posix()],
                cwd=root,
                check=True,
                capture_output=True,
            )
            expected = output.read_bytes()

            for operation in ("verify", "generate"):
                args = [
                    operation,
                    "--source",
                    "index",
                    "--lock" if operation == "verify" else "--out",
                    output.relative_to(root).as_posix(),
                ]
                with self.subTest(operation=operation):
                    code, _stdout, stderr = _run_cli(root, *args)
                self.assertEqual(code, core.EXIT_USAGE, stderr)
                self.assertIn("inside vendored copy root", stderr)
                self.assertEqual(output.read_bytes(), expected)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
