"""
Tests for boundver.core.main() called directly, contributing to coverage of the
CLI dispatch layer. Each test patches sys.argv and git_root()
so no real git repo or subprocess is needed for most cases.
"""
from __future__ import annotations

import io
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

import boundver.core as core
from tests._repo_fixtures import commit_all, init_git_repo


def _run_main(*args: str, repo_root: Path = None) -> tuple[int, str, str]:
    """Call core.main() with the given argv, return (exit_code, stdout, stderr)."""
    argv = ["boundver"] + list(args)
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    exit_code = 0
    with patch("sys.argv", argv):
        if repo_root is not None:
            with patch("boundver.core.git_root", return_value=repo_root):
                try:
                    with redirect_stdout(out_buf), redirect_stderr(err_buf):
                        core.main()
                except SystemExit as e:
                    exit_code = int(e.code) if e.code is not None else 0
        else:
            try:
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    core.main()
            except SystemExit as e:
                exit_code = int(e.code) if e.code is not None else 0
    return exit_code, out_buf.getvalue(), err_buf.getvalue()



class MainNoCommandTests(unittest.TestCase):
    def test_version_uses_the_stable_public_program_name(self):
        code, out, error = _run_main("--version")

        self.assertEqual(code, 0)
        self.assertRegex(out, r"^boundver [0-9]+\.[0-9]+\.[0-9]+\n$")
        self.assertEqual(error, "")

    def test_no_command_exits_2(self):
        """main() with no subcommand exits 2 (usage error) and prints help."""
        code, out, _ = _run_main()
        self.assertEqual(code, 2)
        self.assertIn("usage", out.lower())

    def test_double_dash_preserves_quiet_as_why_component_name(self):
        with patch.object(sys, "argv", ["boundver", "why", "--", "--quiet"]), patch(
            "boundver.core.git_root", return_value=Path.cwd()
        ), patch("boundver.core._cmd_why") as handler:
            core.main()
        parsed = handler.call_args.args[0]
        self.assertEqual(parsed.component, "--quiet")
        self.assertFalse(parsed.quiet)

    def test_double_dash_preserves_verbose_as_diff_filename(self):
        with patch.object(
            sys, "argv", ["boundver", "diff", "--", "--verbose", "new.lock"]
        ), patch("boundver.core._cmd_diff") as handler:
            core.main()
        parsed = handler.call_args.args[0]
        self.assertEqual(parsed.old, "--verbose")
        self.assertFalse(parsed.verbose)

    def test_mutually_exclusive_global_flags_conflict_after_subcommand(self):
        code, _, error = _run_main("status", "--quiet", "--verbose")

        self.assertEqual(code, 2)
        self.assertIn("not allowed with argument", error)


class MainCompletionsTests(unittest.TestCase):
    """completions command runs before git_root() — no repo needed."""

    def test_completions_bash(self):
        code, out, _ = _run_main("completions", "--shell", "bash")
        self.assertEqual(code, 0)
        self.assertIn("boundver", out)

    def test_completions_zsh(self):
        code, out, _ = _run_main("completions", "--shell", "zsh")
        self.assertEqual(code, 0)
        self.assertIn("boundver", out)

    def test_completions_fish(self):
        code, out, _ = _run_main("completions", "--shell", "fish")
        self.assertEqual(code, 0)
        self.assertIn("boundver", out)

    def test_completions_missing_shell_exits_nonzero(self):
        code, _, _ = _run_main("completions")
        self.assertNotEqual(code, 0)


class MainNotGitRepoTests(unittest.TestCase):
    def test_non_git_repo_exits_1(self):
        """main() exits 2 with message when not in a git repo."""
        with patch("boundver.core.git_root", side_effect=subprocess.CalledProcessError(128, "git")):
            code, _, err = _run_main("generate")
        self.assertEqual(code, 2)
        self.assertIn("git", err.lower())


class MainGenerateTests(unittest.TestCase):

    def _minimal_cfg(self, root: Path) -> dict:
        cfg = {
            "project": "p",
            "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}},
            "slices": {},
        }
        (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
        (root / "svc").mkdir(exist_ok=True)
        (root / "svc" / "main.py").write_text("x=1\n")
        return cfg

    def test_generate_missing_config_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            code, _, err = _run_main(
                "generate",
                "--source",
                "working-tree",
                "--config",
                "missing.json",
                repo_root=root,
            )
            self.assertEqual(code, 2)
            self.assertIn("not found", err)

    def test_generate_writes_lockfile(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._minimal_cfg(root)
            code, out, err = _run_main("generate", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, 0, err)
            self.assertTrue((root / "boundary.lock.json").exists())

    def test_openapi_json_directory_does_not_require_yaml_extra(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            contracts = root / "svc" / "contracts"
            contracts.mkdir(parents=True)
            (contracts / "openapi.json").write_text(
                '{"openapi":"3.1.0","paths":{}}\n'
            )
            config = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "openapi-canonical",
                            "paths": ["contracts"],
                        },
                    }
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(
                json.dumps(config) + "\n"
            )
            commit_all(root, "add JSON OpenAPI directory")
            real_import = importlib.import_module

            def missing_yaml(name: str, package: str = None):
                if name == "yaml":
                    raise ModuleNotFoundError("No module named 'yaml'")
                return real_import(name, package)

            with patch(
                "boundver.providers.importlib.import_module",
                side_effect=missing_yaml,
            ):
                validated = _run_main("validate-config", repo_root=root)
                generated = _run_main(
                    "generate", "--source", "working-tree", repo_root=root
                )
                verified = _run_main(
                    "verify", "--source", "working-tree", repo_root=root
                )

            self.assertEqual(validated[0], 0, validated[2])
            self.assertEqual(generated[0], 0, generated[2])
            self.assertEqual(verified[0], 0, verified[2])

    def test_generate_dry_run_skips_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._minimal_cfg(root)
            code, out, _ = _run_main("generate", "--source", "working-tree", "--dry-run", repo_root=root)
            self.assertEqual(code, 0)
            self.assertFalse((root / "boundary.lock.json").exists())
            self.assertIn("Dry run", out)

    def test_generate_quiet_suppresses_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._minimal_cfg(root)
            code, out, _ = _run_main("--quiet", "generate", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "")

    def test_generate_format_json_prints_lockfile(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._minimal_cfg(root)
            code, out, err = _run_main(
                "--quiet", "generate", "--source", "working-tree", "--format", "json", repo_root=root
            )
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertIn("components", payload)

    def test_generate_verbose_prints_diagnostics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._minimal_cfg(root)
            code, out, _ = _run_main("--verbose", "generate", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, 0)
            self.assertIn("source=", out)

    def test_generate_components_filter(self):
        """generate --components only regenerates selected components."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "implicit"}},
                    "worker": {"path": "worker", "boundary": {"provider": "implicit"}},
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            (root / "svc").mkdir()
            (root / "svc" / "a.py").write_text("x=1\n")
            (root / "worker").mkdir()
            (root / "worker" / "b.py").write_text("y=1\n")
            # First full generate
            code, _, err = _run_main("generate", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, 0, err)
            # Now regenerate only svc
            code, _, err = _run_main(
                "generate", "--source", "working-tree", "--components", "svc", repo_root=root
            )
            self.assertEqual(code, 0, err)

    def test_generate_value_error_exits_1(self):
        """generate exits 1 when generate_lockfile raises ValueError (e.g. strict slice)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "implicit"}}
                },
                "slices": {"s1": {"mode": "boundary", "components": ["svc"]}},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            (root / "svc").mkdir()
            (root / "svc" / "a.py").write_text("x=1\n")
            code, _, err = _run_main("generate", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("ERROR", err)

    def test_allow_partial_lock_verifies_from_the_same_snapshot(self):
        """An implicit boundary and unversioned compat slice round-trip cleanly."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit", "paths": []},
                    }
                },
                "slices": {
                    "boundary-slice": {
                        "mode": "boundary",
                        "components": ["svc"],
                    },
                    "compat-slice": {
                        "mode": "compat",
                        "components": ["svc"],
                    },
                },
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            (root / "svc").mkdir()
            (root / "svc" / "a.py").write_text("x=1\n")

            strict_code, _, strict_err = _run_main(
                "generate", "--source", "working-tree", repo_root=root
            )
            self.assertEqual(strict_code, core.EXIT_USAGE)
            self.assertIn("requires boundary digest", strict_err)
            self.assertFalse((root / "boundary.lock.json").exists())

            code, _, err = _run_main(
                "generate",
                "--source",
                "working-tree",
                "--allow-partial",
                repo_root=root,
            )
            self.assertEqual(code, 0, err)
            code, out, err = _run_main(
                "verify", "--source", "working-tree", repo_root=root
            )
            self.assertEqual(code, 0, err)
            self.assertIn("up to date", out.lower())
            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--facets",
                "exact",
                repo_root=root,
            )
            self.assertEqual(code, 0, err)
            self.assertIn("up to date", out.lower())
            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--facets",
                "boundary",
                repo_root=root,
            )
            self.assertEqual(code, core.EXIT_USAGE, out + err)
            self.assertIn("UNAVAILABLE FACET svc.boundary", out)
            self.assertIn("cannot be generated from the current configuration", out)
            self.assertIn("`--update` will not modify the lock", out)
            self.assertNotIn("run `boundver verify --update`", out)
            lock_path = root / "boundary.lock.json"
            before_update = lock_path.read_bytes()
            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--facets",
                "boundary",
                "--update",
                repo_root=root,
            )
            self.assertEqual(code, core.EXIT_USAGE, out + err)
            self.assertIn("LOCKFILE NOT UPDATED", out)
            self.assertIn("cannot be generated from the current configuration", out)
            self.assertIn("`--update` will not modify the lock", out)
            self.assertEqual(lock_path.read_bytes(), before_update)
            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--facets",
                "boundary",
                "--update",
                "--format",
                "json",
                repo_root=root,
            )
            self.assertEqual(code, core.EXIT_USAGE, out + err)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["updated"])
            self.assertEqual(lock_path.read_bytes(), before_update)
            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--facets",
                "compat",
                repo_root=root,
            )
            self.assertEqual(code, core.EXIT_USAGE, out + err)
            self.assertIn("UNAVAILABLE FACET svc.compat", out)
            self.assertIn("UNAVAILABLE FACET compat-slice.compat", out)
            lock = json.loads((root / "boundary.lock.json").read_text())
            self.assertEqual(
                lock["components"]["svc"]["boundary_status"], "partial"
            )
            self.assertIsNone(
                lock["slices"]["boundary-slice"]["component_digests"]["svc"]
            )
            self.assertIsNone(
                lock["slices"]["compat-slice"]["component_digests"]["svc"]
            )

    def test_explicit_compat_policy_requires_a_version_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = self._minimal_cfg(root)
            cfg["components"]["svc"]["verify_facets"] = ["compat"]
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            code, out, err = _run_main(
                "validate-config", repo_root=root
            )
            self.assertEqual(code, core.EXIT_USAGE, out + err)
            self.assertTrue(
                "compat" in (out + err).lower()
                and (
                    "version" in (out + err).lower()
                    or "unavailable" in (out + err).lower()
                ),
                out + err,
            )

    def test_fail_fast_unavailable_facet_blocks_update_before_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            for name in ("a", "b"):
                (root / name).mkdir()
                (root / name / "api.json").write_text("{}\n")
            cfg = {
                "project": "p",
                "components": {
                    name: {
                        "path": name,
                        "boundary": {
                            "provider": "json-file",
                            "paths": ["api.json"],
                        },
                    }
                    for name in ("a", "b")
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            code, _out, err = _run_main(
                "generate", "--source", "working-tree", repo_root=root
            )
            self.assertEqual(code, 0, err)
            lock_path = root / "boundary.lock.json"
            before = lock_path.read_bytes()
            (root / "b" / "api.json").write_text('{"changed":true}\n')

            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--facets",
                "behavior,boundary",
                "--fail-fast",
                "--update",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_USAGE, out + err)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["updated"])
            self.assertTrue(payload["issues"][0].startswith("UNAVAILABLE FACET"))
            self.assertEqual(lock_path.read_bytes(), before)

    def test_allow_partial_does_not_bless_missing_declared_boundary(self):
        """A provider error remains fatal even when slice nulls are allowed."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "json-file",
                            "paths": ["missing.json"],
                        },
                    }
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            (root / "svc").mkdir()
            (root / "svc" / "a.py").write_text("x=1\n")

            # Bypass working-tree existence validation so generation itself
            # proves --allow-partial cannot bless a provider error.
            with patch("boundver.core.validate_config", return_value=[]):
                code, _, err = _run_main(
                    "generate",
                    "--source",
                    "working-tree",
                    "--allow-partial",
                    repo_root=root,
                )

            self.assertEqual(code, 2)
            self.assertIn("missing.json", err)
            self.assertFalse((root / "boundary.lock.json").exists())

    def test_generate_bad_config_json_exits_1(self):
        """generate reports malformed configuration as a usage error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "boundary.config.json").write_text("{not valid json}")
            code, _, err = _run_main("generate", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("ERROR", err)


class MainRemoveIntegrityTests(unittest.TestCase):
    def _repo(self, root: Path) -> None:
        init_git_repo(
            root,
            user_email="t@t.com",
            user_name="T",
        )

    def _assert_remove_refuses_without_writing(self, config: dict, name: str) -> str:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            for component in config["components"].values():
                path = root / component["path"]
                path.mkdir(parents=True, exist_ok=True)
                (path / "main.py").write_text("value = 1\n")
            config_path = root / "boundary.config.json"
            config_path.write_text(json.dumps(config, indent=2) + "\n")
            before = config_path.read_bytes()

            code, _out, err = _run_main("remove", name, repo_root=root)

            self.assertEqual(code, core.EXIT_USAGE, err)
            self.assertEqual(config_path.read_bytes(), before)
            return err

    def test_remove_refuses_incoming_consumer_edge(self):
        config = {
            "project": "p",
            "components": {
                "a": {
                    "path": "a",
                    "boundary": {"provider": "implicit"},
                    "consumers": ["b"],
                },
                "b": {"path": "b", "boundary": {"provider": "implicit"}},
            },
            "slices": {},
        }
        self.assertIn("unknown consumer", self._assert_remove_refuses_without_writing(config, "b"))

    def test_remove_refuses_closure_seed(self):
        config = {
            "project": "p",
            "components": {
                "a": {"path": "a", "boundary": {"provider": "implicit"}},
                "b": {"path": "b", "boundary": {"provider": "implicit"}},
            },
            "slices": {"impact": {"mode": "exact", "closure_of": "b"}},
        }
        self.assertIn("closure_of", self._assert_remove_refuses_without_writing(config, "b"))

    def test_remove_refuses_last_component(self):
        config = {
            "project": "p",
            "components": {
                "a": {"path": "a", "boundary": {"provider": "implicit"}},
            },
            "slices": {},
        }
        self.assertIn("at least one component", self._assert_remove_refuses_without_writing(config, "a"))

    def test_remove_refuses_to_leave_an_explicit_slice_empty(self):
        config = {
            "project": "p",
            "components": {
                "a": {"path": "a", "boundary": {"provider": "implicit"}},
                "b": {"path": "b", "boundary": {"provider": "implicit"}},
            },
            "slices": {
                "only-a": {"mode": "exact", "components": ["a"]},
            },
        }

        error = self._assert_remove_refuses_without_writing(config, "a")

        self.assertIn("add a component name or remove the empty slice", error)

    def test_add_refuses_invalid_component_without_writing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            (root / "existing").mkdir()
            (root / "existing" / "main.py").write_text("value = 1\n")
            config = {
                "project": "p",
                "components": {
                    "existing": {
                        "path": "existing",
                        "boundary": {"provider": "implicit"},
                    }
                },
                "slices": {},
            }
            config_path = root / "boundary.config.json"
            config_path.write_text(json.dumps(config, indent=2) + "\n")
            before = config_path.read_bytes()

            code, _out, err = _run_main(
                "add", "missing", "does-not-exist", repo_root=root
            )

            self.assertEqual(code, core.EXIT_USAGE, err)
            self.assertIn("would leave an invalid config", err)
            self.assertEqual(config_path.read_bytes(), before)

    def test_add_reports_schema_invalid_components_without_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            config_path = root / "boundary.config.json"
            config_path.write_text(
                json.dumps({"project": "p", "components": [], "slices": {}})
                + "\n"
            )

            code, _out, err = _run_main("add", "x", "x", repo_root=root)

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertIn("components", err)
            self.assertNotIn("Traceback", err)

    def test_remove_reports_schema_invalid_slices_without_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            (root / "a").mkdir()
            config_path = root / "boundary.config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project": "p",
                        "components": {
                            "a": {
                                "path": "a",
                                "boundary": {"provider": "implicit"},
                            }
                        },
                        "slices": [],
                    }
                )
                + "\n"
            )

            code, _out, err = _run_main("remove", "a", repo_root=root)

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertIn("slices", err)
            self.assertNotIn("Traceback", err)


class MainValidateConfigErrorPathTests(unittest.TestCase):
    """validate-config and check-config reject malformed configuration files."""

    def test_validate_config_bad_json_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "boundary.config.json").write_text("{bad}")
            code, _, err = _run_main("validate-config", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("ERROR", err)

    def test_check_config_bad_json_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "boundary.config.json").write_text("{bad}")
            code, _, err = _run_main("check-config", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("ERROR", err)


class MainStatusConfigWarningTests(unittest.TestCase):
    """status reports malformed configuration as a warning, not an error."""

    def test_status_bad_config_prints_warning_not_error(self):
        """status with a bad config file emits a WARNING to stderr but exits 0 (lockfile found)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            # Write a minimal lockfile so status proceeds past the lockfile-missing check
            lockfile = {
                "schema": "boundary-lock/v1",
                "project": "p",
                "components": {},
                "slices": {},
            }
            (root / "boundary.lock.json").write_text(json.dumps(lockfile))
            # Bad config — parse will fail
            (root / "boundary.config.json").write_text("{bad json}")
            code, _, err = _run_main(
                "status", "--source", "working-tree", repo_root=root
            )
            self.assertEqual(code, 0)
            self.assertIn("WARNING", err)


class MainMigrateLockRelativePathTests(unittest.TestCase):
    """migrate-lock accepts a relative lockfile path."""

    def test_migrate_lock_relative_path(self):
        """migrate-lock accepts a relative path, resolves against cwd."""
        import os
        with tempfile.TemporaryDirectory() as td:
            lf = {
                "schema": "boundary-lock/v3",
                "config_contract": "boundver-semantic-config/v2",
                "project": "x",
                "components": {},
                "slices": {},
            }
            (Path(td) / "boundary.lock.json").write_text(json.dumps(lf))
            old_dir = os.getcwd()
            os.chdir(td)
            try:
                code, _, _ = _run_main("migrate-lock", "--lock", "boundary.lock.json")
            finally:
                os.chdir(old_dir)
            self.assertEqual(code, 0)


class MainVerifyTests(unittest.TestCase):

    def _setup(self, root: Path) -> None:
        cfg = {
            "project": "p",
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
        (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
        (root / "svc").mkdir(exist_ok=True)
        (root / "svc" / "main.py").write_text("x=1\n")
        (root / "svc" / "version.json").write_text('{"version":"1.0.0"}\n')
        (root / "svc" / "contract.json").write_text('{"api":"v1"}\n')

    def test_verify_up_to_date_exits_0(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            code, out, _ = _run_main("verify", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, 0)
            self.assertIn("up to date", out.lower())

    def test_verify_head_missing_uncommitted_lock_explains_source_choice(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            commit_all(root, "record source")

            code, _out, err = _run_main(
                "generate", "--source", "head", repo_root=root
            )
            self.assertEqual(code, core.EXIT_OK, err)

            code, _out, err = _run_main(
                "verify", "--source", "head", repo_root=root
            )

            self.assertEqual(code, core.EXIT_USAGE, err)
            self.assertIn("Lockfile not found in captured head source", err)
            self.assertIn("`--source head` reads committed files", err)
            self.assertIn("Commit `boundary.lock.json`", err)
            self.assertIn("`--source working-tree`", err)

    def test_verify_index_missing_unstaged_lock_explains_source_choice(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            commit_all(root, "record source")

            code, _out, err = _run_main(
                "generate", "--source", "index", repo_root=root
            )
            self.assertEqual(code, core.EXIT_OK, err)

            code, _out, err = _run_main(
                "verify", "--source", "index", repo_root=root
            )

            self.assertEqual(code, core.EXIT_USAGE, err)
            self.assertIn("Lockfile not found in captured index source", err)
            self.assertIn("`--source index` reads staged files", err)
            self.assertIn("Stage `boundary.lock.json`", err)
            self.assertIn("`--source working-tree`", err)

    def test_verify_out_of_date_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            (root / "svc" / "main.py").write_text("x=2\n")  # change without re-generating
            code, _, _ = _run_main("verify", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, 1)

    def test_verify_quiet_suppresses_issue_list(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            (root / "svc" / "main.py").write_text("x=2\n")
            code, out, _ = _run_main("--quiet", "verify", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, 1)
            self.assertEqual(out.strip(), "")

    def test_verify_verbose_shows_zero_issues_message(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            code, out, _ = _run_main("--verbose", "verify", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, 0)
            self.assertIn("0 issues", out)

    def test_verify_format_json_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            code, out, _ = _run_main("verify", "--source", "working-tree", "--format", "json", repo_root=root)
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])

    def test_verify_verbose_json_is_one_json_document(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)

            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--format",
                "json",
                "--verbose",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_OK, err)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["issues"], [])
            self.assertEqual(payload["resolved_issues"], [])

    def test_verify_format_json_with_issues(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            (root / "svc" / "main.py").write_text("changed\n")
            code, out, _ = _run_main("verify", "--source", "working-tree", "--format", "json", repo_root=root)
            self.assertEqual(code, 1)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertGreater(len(payload["issues"]), 0)

    def test_verify_component_filter_rejects_unselected_vendored_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            config = {
                "project": "p",
                "components": {
                    name: {
                        "path": name,
                        "boundary": {"provider": "implicit"},
                    }
                    for name in ("a", "b")
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(config) + "\n")
            for name in ("a", "b"):
                (root / name).mkdir()
                (root / name / "main.py").write_text(f"{name}\n")
            code, _out, err = _run_main(
                "generate", "--source", "working-tree", repo_root=root
            )
            self.assertEqual(code, core.EXIT_OK, err)
            lock_path = root / "boundary.lock.json"
            lock = json.loads(lock_path.read_text())
            lock["components"]["b"]["vendored_errors"] = ["copy differs"]
            lock_path.write_text(json.dumps(lock) + "\n")

            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--components",
                "a",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_USAGE, out + err)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertTrue(
                any("copy differs" in issue for issue in payload["issues"]), payload
            )

    def test_verify_component_filter_rejects_unselected_partial_provider(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            config = {
                "project": "p",
                "components": {
                    name: {
                        "path": name,
                        "boundary": {"provider": "implicit"},
                    }
                    for name in ("a", "b")
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(config) + "\n")
            for name in ("a", "b"):
                (root / name).mkdir()
                (root / name / "main.py").write_text(f"{name}\n")
            code, _out, err = _run_main(
                "generate", "--source", "working-tree", repo_root=root
            )
            self.assertEqual(code, core.EXIT_OK, err)
            lock_path = root / "boundary.lock.json"
            lock = json.loads(lock_path.read_text())
            lock["components"]["b"].update(
                {
                    "boundary_provider": "custom.incomplete",
                    "boundary_status": "partial",
                    "boundary_errors": ["one input failed"],
                }
            )
            lock_path.write_text(json.dumps(lock) + "\n")

            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--components",
                "a",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_USAGE, out + err)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertTrue(
                any("one input failed" in issue for issue in payload["issues"]),
                payload,
            )

    def test_verify_unknown_components_exits_2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            code, _, err = _run_main(
                "verify", "--source", "working-tree", "--components", "no-such", repo_root=root
            )
            self.assertEqual(code, 2)

    def test_verify_changed_from_with_no_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            code, out, _ = _run_main(
                "verify", "--source", "working-tree", "--changed-from", "HEAD", repo_root=root
            )
            self.assertEqual(code, 0)
            self.assertIn(
                "Changed component paths (0): none; validating full lock integrity.",
                out,
            )

    def test_verify_changed_from_text_reports_changed_components(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            subprocess.run(
                ["git", "add", "."], cwd=root, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            _run_main("generate", "--source", "working-tree", repo_root=root)
            (root / "svc" / "main.py").write_text("x=2\n")

            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--changed-from",
                "HEAD",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_DRIFT, err)
            self.assertIn(
                "Changed component paths (1): svc; "
                "validating full lock integrity.",
                out,
            )

    def test_verify_changed_from_reports_safe_components_before_preflight(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            subprocess.run(
                ["git", "add", "."], cwd=root, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            _run_main("generate", "--source", "working-tree", repo_root=root)
            subprocess.run(
                ["git", "add", "boundary.lock.json"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "lock baseline"],
                cwd=root,
                check=True,
                capture_output=True,
            )

            config_path = root / "boundary.config.json"
            config = json.loads(config_path.read_text())
            other_name = "other\nERROR: forged"
            config["components"][other_name] = {
                "path": "other",
                "boundary": {"provider": "implicit"},
            }
            config_path.write_text(json.dumps(config) + "\n")
            (root / "other").mkdir()
            (root / "other" / "main.py").write_text("other\n")
            subprocess.run(
                ["git", "add", "boundary.config.json", "other/main.py"],
                cwd=root,
                check=True,
                capture_output=True,
            )

            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--changed-from",
                "HEAD",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_USAGE, out + err)
            self.assertIn(
                "Changed component paths (2): other\\x0aERROR: forged, svc; "
                "validating full lock integrity.",
                out,
            )
            self.assertNotIn("\nERROR: forged", out)
            self.assertIn("LOCKFILE component set differs from config", err)

            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--changed-from",
                "HEAD",
                "--update",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_OK, err)
            payload = json.loads(out)
            self.assertTrue(payload["updated"])
            self.assertEqual(payload["components_filter"], [])
            self.assertEqual(payload["changed_components"], [other_name, "svc"])

    def test_verify_changed_from_uses_selected_head_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            subprocess.run(
                ["git", "add", "."], cwd=root, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "source baseline"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            code, _out, err = _run_main(
                "generate", "--source", "head", repo_root=root
            )
            self.assertEqual(code, core.EXIT_OK, err)
            subprocess.run(
                ["git", "add", "boundary.lock.json"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "record lock"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            (root / "svc" / "main.py").write_text("unstaged\n")

            code, out, err = _run_main(
                "verify",
                "--source",
                "head",
                "--changed-from",
                "HEAD",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_OK, err)
            self.assertEqual(json.loads(out)["components_filter"], [])

    def test_verify_changed_from_with_components_filter_intersection(self):
        """--changed-from intersects with --components when both are given."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = {
                "project": "p",
                "defaults": {"verify_facets": ["exact"]},
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "implicit"}},
                    "worker": {"path": "worker", "boundary": {"provider": "implicit"}},
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            for d in ("svc", "worker"):
                (root / d).mkdir()
                (root / d / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            code, _, _ = _run_main(
                "verify",
                "--source", "working-tree",
                "--components", "svc,worker",
                "--changed-from", "HEAD",
                repo_root=root,
            )
            self.assertEqual(code, 0)

    def test_verify_facets_turn_exact_drift_into_observation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            (root / "svc" / "main.py").write_text("internal refactor\n")

            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--facets",
                "boundary,compat",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_OK, err)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["issues"], [])
            self.assertTrue(
                any(".exact:" in observation for observation in payload["observations"]),
                payload,
            )

    def test_verify_update_refreshes_observation_only_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            (root / "svc" / "main.py").write_text("internal refactor\n")

            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--facets",
                "boundary,compat",
                "--update",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_OK, err)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["updated"])
            self.assertTrue(payload["observations"])

            verify_code, _, verify_err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--format",
                "json",
                repo_root=root,
            )
            self.assertEqual(verify_code, core.EXIT_OK, verify_err)

    def test_verify_invalid_changed_from_exits_usage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            _run_main("generate", "--source", "working-tree", repo_root=root)

            code, _, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--changed-from",
                "not-a-real-ref",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertIn("not-a-real-ref", err)

    def test_verify_changed_from_no_paths_still_checks_provider_version(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            code, _, err = _run_main(
                "generate", "--source", "head", repo_root=root
            )
            self.assertEqual(code, core.EXIT_OK, err)
            config = json.loads((root / "boundary.config.json").read_text())
            self.assertEqual(
                core.changed_components_since_ref(config, root, "HEAD"), []
            )
            lock_path = root / "boundary.lock.json"
            lock = json.loads(lock_path.read_text())
            lock["components"]["svc"]["boundary_provider_version"] = "tampered"
            lock_path.write_text(json.dumps(lock) + "\n")
            subprocess.run(
                ["git", "add", "boundary.lock.json"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "tamper locked metadata"],
                cwd=root,
                check=True,
                capture_output=True,
            )

            code, out, err = _run_main(
                "verify",
                "--source",
                "head",
                "--changed-from",
                "HEAD",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_DRIFT, err)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertTrue(
                any(
                    "METADATA MISMATCH svc.boundary_provider_version" in issue
                    for issue in payload["issues"]
                ),
                payload,
            )

    def test_verify_changed_from_checks_unselected_component_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            config = {
                "project": "p",
                "defaults": {"verify_facets": ["exact"]},
                "components": {
                    "tagged": {
                        "path": "tagged",
                        "version_source": {"git_tag_prefix": "tagged-v"},
                        "boundary": {"provider": "implicit"},
                    },
                    "other": {
                        "path": "other",
                        "boundary": {"provider": "implicit"},
                    },
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(config) + "\n")
            for name in ("tagged", "other"):
                (root / name).mkdir()
                (root / name / "main.py").write_text(f"{name}\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "tag", "tagged-v1.0.0"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            code, _, err = _run_main("generate", "--source", "head", repo_root=root)
            self.assertEqual(code, core.EXIT_OK, err)

            lock_path = root / "boundary.lock.json"
            lock = json.loads(lock_path.read_text())
            lock["components"]["other"]["boundary_provider_version"] = "tampered"
            lock_path.write_text(json.dumps(lock) + "\n")
            subprocess.run(
                ["git", "add", "boundary.lock.json"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "stale metadata"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            self.assertEqual(
                core.changed_components_since_ref(config, root, "HEAD"), ["tagged"]
            )

            code, out, err = _run_main(
                "verify",
                "--source",
                "head",
                "--changed-from",
                "HEAD",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_DRIFT, err)
            payload = json.loads(out)
            self.assertEqual(payload["components_filter"], [])
            self.assertEqual(payload["changed_components"], ["tagged"])
            self.assertTrue(
                any(
                    "METADATA MISMATCH other.boundary_provider_version" in issue
                    for issue in payload["issues"]
                ),
                payload,
            )


class MainMalformedV2LockTests(unittest.TestCase):

    def _make_lock(self, root: Path) -> Path:
        init_git_repo(root)
        (root / "svc").mkdir()
        (root / "svc" / "main.py").write_text("x=1\n")
        config = {
            "project": "p",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "implicit"},
                }
            },
            "slices": {
                "all": {"mode": "exact", "components": ["svc"]},
            },
        }
        (root / "boundary.config.json").write_text(json.dumps(config) + "\n")
        code, _, err = _run_main(
            "generate", "--source", "working-tree", repo_root=root
        )
        self.assertEqual(code, core.EXIT_OK, err)
        return root / "boundary.lock.json"

    def _assert_controlled_usage_error(self, mutator, expected_message: str) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lock_path = self._make_lock(root)
            lock = json.loads(lock_path.read_text())
            mutator(lock)
            lock_path.write_text(json.dumps(lock) + "\n")

            code, out, err = _run_main(
                "verify", "--source", "working-tree", repo_root=root
            )

            combined = out + err
            self.assertEqual(code, core.EXIT_USAGE, combined)
            self.assertIn(expected_message, combined)
            self.assertNotIn("Traceback", combined)

    def test_non_string_consumer_is_a_controlled_usage_error(self):
        self._assert_controlled_usage_error(
            lambda lock: lock["components"]["svc"].__setitem__(
                "consumers", ["client", 7]
            ),
            "consumers must be an array of unique strings",
        )

    def test_non_string_slice_component_is_a_controlled_usage_error(self):
        self._assert_controlled_usage_error(
            lambda lock: lock["slices"]["all"].__setitem__(
                "components", ["svc", 7]
            ),
            "slice 'all' components must be an array of strings",
        )


class MainSeverityAndConsumerTests(unittest.TestCase):

    def test_boundary_drift_uses_exit_4_and_lists_consumers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "api").mkdir()
            (root / "api" / "openapi.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
            (root / "client").mkdir()
            (root / "client" / "main.py").write_text("pass\n")
            cfg = {
                "project": "p",
                "components": {
                    "api": {
                        "path": "api",
                        "boundary": {"provider": "openapi", "paths": ["openapi.yaml"]},
                        "consumers": ["client"],
                    },
                    "client": {"path": "client", "boundary": {"provider": "leaf"}},
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            code, _, err = _run_main(
                "generate", "--source", "working-tree", repo_root=root
            )
            self.assertEqual(code, core.EXIT_OK, err)
            generated = json.loads((root / "boundary.lock.json").read_text())
            self.assertEqual(generated["components"]["api"]["consumers"], ["client"])
            (root / "api" / "openapi.yaml").write_text(
                "openapi: 3.0.0\npaths:\n  /widgets: {}\n"
            )

            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--facets",
                "boundary",
                "--components",
                "api",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_BOUNDARY, err)
            payload = json.loads(out)
            self.assertTrue(any("MISMATCH api.boundary" in issue for issue in payload["issues"]))
            self.assertTrue(
                any("AFFECTED CONSUMERS api: client" in issue for issue in payload["issues"]),
                payload,
            )

    def test_compat_drift_uses_exit_5(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "version.json").write_text('{"version": "1.0.0"}\n')
            cfg = {
                "project": "p",
                "defaults": {"compat_mode": "major"},
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "leaf"},
                        "version_source": {"file": "version.json", "field": "version"},
                    }
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            subprocess.run(
                ["git", "add", "boundary.config.json", "svc/version.json"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "track version source"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            code, _, err = _run_main(
                "generate", "--source", "working-tree", repo_root=root
            )
            self.assertEqual(code, core.EXIT_OK, err)
            (root / "svc" / "version.json").write_text('{"version": "2.0.0"}\n')

            code, out, err = _run_main(
                "verify",
                "--source",
                "working-tree",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(code, core.EXIT_COMPAT, err)
            self.assertTrue(
                any("MISMATCH svc.compat" in issue for issue in json.loads(out)["issues"])
            )


class MainDiffTests(unittest.TestCase):
    @staticmethod
    def _valid_lock() -> dict:
        return {
            "schema": "boundary-lock/v3",
            "config_contract": "boundver-semantic-config/v2",
            "config_digest": "0" * 64,
            "project": "p",
            "components": {
                "svc": {
                    "version": None,
                    "path": "svc",
                    "boundary_provider": "leaf",
                    "boundary_provider_version": "1",
                    "boundary_status": "ok",
                    "consumers": [],
                    "external_consumers": [],
                    "fingerprints": {
                        "exact": "1" * 64,
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

    def test_diff_identical_lockfiles_text(self):
        with tempfile.TemporaryDirectory() as td:
            lock = self._valid_lock()
            p = Path(td) / "lock.json"
            p.write_text(json.dumps(lock))
            code, out, _ = _run_main("diff", str(p), str(p))
            self.assertEqual(code, 0)
            self.assertIn("UNCHANGED", out)

    def test_diff_format_json(self):
        with tempfile.TemporaryDirectory() as td:
            lock = self._valid_lock()
            p = Path(td) / "lock.json"
            p.write_text(json.dumps(lock))
            code, out, _ = _run_main("diff", str(p), str(p), "--format", "json")
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertIn("components", payload)

    def test_diff_accepts_existing_parent_relative_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            child = root / "nested"
            child.mkdir()
            lock = self._valid_lock()
            (root / "old.lock.json").write_text(json.dumps(lock))
            (root / "new.lock.json").write_text(json.dumps(lock))
            original_cwd = Path.cwd()
            try:
                os.chdir(child)
                code, out, err = _run_main(
                    "diff", "../old.lock.json", "../new.lock.json"
                )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(code, core.EXIT_OK, err)
            self.assertIn("UNCHANGED", out)


class MainSliceTests(unittest.TestCase):

    def test_slice_shows_fingerprint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "a.py").write_text("x=1\n")
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}},
                "slices": {"s1": {"mode": "exact", "components": ["svc"]}},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            _run_main("generate", "--source", "working-tree", repo_root=root)
            code, out, _ = _run_main("slice", "s1", repo_root=root)
            self.assertEqual(code, 0)
            self.assertIn("s1", out)
            self.assertIn("exact", out)

    def test_slice_not_found_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "a.py").write_text("x=1\n")
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            _run_main("generate", "--source", "working-tree", repo_root=root)
            code, _, _ = _run_main("slice", "no-such", repo_root=root)
            self.assertNotEqual(code, 0)


class MainValidateConfigTests(unittest.TestCase):

    def test_validate_config_valid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            cfg = {"project": "p", "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}}, "slices": {}}
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            code, out, _ = _run_main("validate-config", repo_root=root)
            self.assertEqual(code, 0)
            self.assertIn("valid", out.lower())

    def test_validate_config_invalid_exits_2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = {"project": "p", "components": {"svc": {"path": "svc", "boundary": {"provider": "bad-provider"}}}, "slices": {}}
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            code, out, _ = _run_main("validate-config", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("INVALID", out)

    def test_validate_config_missing_config_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            code, _, err = _run_main("validate-config", "--config", "missing.json", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("not found", err)

    def test_validate_config_rejects_behavior_that_does_not_cover_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0\n")
            (root / "svc" / "config.json").write_text('{"timeout": 30}\n')
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["config.json"]},
                    }
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")

            code, out, _err = _run_main("validate-config", repo_root=root)

            self.assertEqual(code, 2)
            self.assertIn("api.yaml", out)
            self.assertIn("must cover every boundary artifact", out)


class MainInitTests(unittest.TestCase):

    def test_init_creates_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            code, out, _ = _run_main("init", repo_root=root)
            self.assertEqual(code, 0)
            self.assertTrue((root / "boundary.config.json").exists())
            self.assertIn("Created", out)

    def test_init_refuses_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "boundary.config.json").write_text("{}")
            code, _, err = _run_main("init", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("already exists", err)

    def test_init_force_overwrites(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "boundary.config.json").write_text("{}")
            code, out, _ = _run_main("init", "--force", repo_root=root)
            self.assertEqual(code, 0)
            self.assertIn("Created", out)

    def test_init_discover_flag(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "package.json").write_text('{"version":"1.0"}')
            code, out, _ = _run_main("init", "--discover", repo_root=root)
            self.assertEqual(code, 0)
            cfg = json.loads((root / "boundary.config.json").read_text())
            self.assertIn("svc", cfg["components"])

    def test_init_discovery_failure_is_controlled(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            with patch(
                "boundver.core.discover_components",
                side_effect=OSError("cannot read index"),
            ):
                code, out, err = _run_main(
                    "init", "--discover", repo_root=root
                )

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("component discovery failed", err)
            self.assertNotIn("Traceback", err)

    def test_init_write_failure_is_controlled(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            with patch(
                "boundver.core._write_text_atomic",
                side_effect=PermissionError("target is locked"),
            ):
                code, out, err = _run_main("init", repo_root=root)

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("ERROR: init failed: target is locked", err)
            self.assertNotIn("Traceback", err)

    def test_init_custom_out(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            code, _, _ = _run_main("init", "--out", "custom.config.json", repo_root=root)
            self.assertEqual(code, 0)
            self.assertTrue((root / "custom.config.json").exists())


class MainDiscoverTests(unittest.TestCase):

    def test_discover_text_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "package.json").write_text('{"version":"1.0"}')
            code, out, _ = _run_main("discover", repo_root=root)
            self.assertEqual(code, 0)
            self.assertIn("svc", out)

    def test_discover_json_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "package.json").write_text('{"version":"1.0"}')
            code, out, _ = _run_main("discover", "--format", "json", repo_root=root)
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertIn("svc", payload["components"])

    def test_discover_git_failure_is_controlled(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            failure = subprocess.CalledProcessError(128, ["git", "ls-files"])
            with patch(
                "boundver.core.discover_components", side_effect=failure
            ):
                code, out, err = _run_main("discover", repo_root=root)

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertEqual(out, "")
            self.assertIn("component discovery failed", err)
            self.assertNotIn("Traceback", err)


class MainStatusTests(unittest.TestCase):

    def _setup(self, root: Path) -> None:
        cfg = {"project": "p", "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}}, "slices": {}}
        (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
        (root / "svc").mkdir(exist_ok=True)
        (root / "svc" / "main.py").write_text("x=1\n")

    def test_status_no_lockfile_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            code, _, _ = _run_main("status", repo_root=root)
            self.assertEqual(code, 2)

    def test_status_with_lockfile(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            code, out, _ = _run_main("status", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, 0)
            self.assertIn("p", out)

    def test_status_format_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            code, out, _ = _run_main("status", "--source", "working-tree", "--format", "json", repo_root=root)
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertIn("lockfile", payload)

    def test_status_invalid_config_shape_returns_controlled_json(self):
        invalid_configs = (
            {"project": "p", "components": [], "slices": {}},
            {"project": "p", "components": {}, "slices": []},
        )
        for invalid_config in invalid_configs:
            with (
                self.subTest(invalid_config=invalid_config),
                tempfile.TemporaryDirectory() as td,
            ):
                root = Path(td)
                init_git_repo(root)
                self._setup(root)
                generated = _run_main(
                    "generate", "--source", "working-tree", repo_root=root
                )
                self.assertEqual(generated[0], 0, generated[2])
                (root / "boundary.config.json").write_text(
                    json.dumps(invalid_config) + "\n"
                )

                code, out, err = _run_main(
                    "status",
                    "--source",
                    "working-tree",
                    "--format",
                    "json",
                    repo_root=root,
                )

                self.assertEqual(code, 0, err)
                self.assertNotIn("Traceback", err)
                payload = json.loads(out)
                self.assertIsNone(payload["facet_policy"])
                self.assertTrue(
                    any(
                        issue.startswith("Config invalid:")
                        for issue in payload["issues"]
                    ),
                    payload,
                )

    def test_status_with_drift_shows_issues(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            (root / "svc" / "main.py").write_text("changed\n")
            code, out, _ = _run_main("status", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, 0)  # status itself exits 0 even with drift
            self.assertIn("DRIFT", out)

    def test_status_quiet_suppresses_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            code, out, _ = _run_main("--quiet", "status", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, 0)
            # quiet suppresses print_status but JSON is still possible
            self.assertEqual(out.strip(), "")

    def test_status_no_config_file(self):
        """status with lockfile but no config still shows lockfile summary."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            # Remove config so status can't verify
            (root / "boundary.config.json").unlink()
            code, out, _ = _run_main("status", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, 0)
            self.assertIn("p", out)


class MainExplainTests(unittest.TestCase):

    def test_explain_unknown_component_exits_2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = {"project": "p", "components": {}, "slices": {}}
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            commit_all(root, "add config")
            code, _, _ = _run_main("explain", "ghost", repo_root=root)
            self.assertEqual(code, 2)

    def test_explain_known_component_exits_0(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {"project": "p", "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}}, "slices": {}}
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            commit_all(root, "add config")
            code, out, _ = _run_main("explain", "svc", repo_root=root)
            self.assertEqual(code, 0)
            self.assertIn("svc", out)

    def test_explain_reports_schema_invalid_component_without_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "boundary.config.json").write_text(
                json.dumps(
                    {
                        "project": "p",
                        "components": {"svc": "not-an-object"},
                        "slices": {},
                    }
                )
                + "\n"
            )
            commit_all(root, "add invalid config")

            code, _out, err = _run_main("explain", "svc", repo_root=root)

            self.assertEqual(code, core.EXIT_USAGE)
            self.assertIn("svc", err)
            self.assertNotIn("Traceback", err)


class MainUtilityTests(unittest.TestCase):
    """Tests for internal helpers exercised through main() flow."""

    def test_parse_components_arg_empty(self):
        self.assertEqual(core._parse_components_arg(""), [])
        self.assertEqual(core._parse_components_arg(None), [])

    def test_parse_components_arg_comma_separated(self):
        result = core._parse_components_arg("b,a,c")
        self.assertEqual(result, ["a", "b", "c"])

    def test_parse_components_arg_deduplicates(self):
        result = core._parse_components_arg("a,a,b")
        self.assertEqual(result, ["a", "b"])

    def test_parse_components_arg_strips_whitespace(self):
        result = core._parse_components_arg(" a , b ")
        self.assertEqual(result, ["a", "b"])

    def test_log_suppressed_when_quiet(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            core._log("hello", quiet=True)
        self.assertEqual(buf.getvalue(), "")

    def test_log_prints_when_not_quiet(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            core._log("hello", quiet=False)
        self.assertIn("hello", buf.getvalue())

    def test_print_json_outputs_sorted_keys(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            core._print_json({"z": 1, "a": 2})
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload, {"z": 1, "a": 2})
        # Keys should be sorted in output
        self.assertLess(buf.getvalue().index('"a"'), buf.getvalue().index('"z"'))

    @unittest.skipIf(sys.platform == "win32", "POSIX permission contract")
    def test_atomic_write_preserves_existing_permissions(self):
        import stat

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "boundary.lock.json"
            target.write_text("old\n", encoding="utf-8")
            target.chmod(0o640)

            core._write_text_atomic(target, "new\n")

            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)


class EnvVarAllowCustomProvidersTests(unittest.TestCase):
    """Tests for the BOUNDVER_ALLOW_CUSTOM_PROVIDERS environment variable."""

    def test_env_var_1_sets_allow_custom_providers(self):
        """BOUNDVER_ALLOW_CUSTOM_PROVIDERS=1 enables custom providers without the flag."""
        import os
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                    }
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            env = {**os.environ, "BOUNDVER_ALLOW_CUSTOM_PROVIDERS": "1"}
            with patch("os.environ", env):
                code, out, _ = _run_main("validate-config", repo_root=root)
            self.assertEqual(code, 0)

    def test_env_var_true_sets_allow_custom_providers(self):
        """BOUNDVER_ALLOW_CUSTOM_PROVIDERS=true enables custom providers."""
        import os
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                    }
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")
            env = {**os.environ, "BOUNDVER_ALLOW_CUSTOM_PROVIDERS": "true"}
            with patch("os.environ", env):
                code, out, _ = _run_main("validate-config", repo_root=root)
            self.assertEqual(code, 0)


class MainCheckConfigAliasTests(unittest.TestCase):
    """check-config is an alias for validate-config."""

    def test_check_config_alias_exits_0_for_valid_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            cfg = {"project": "p", "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}}, "slices": {}}
            (root / "boundary.config.json").write_text(json.dumps(cfg))
            code, out, _ = _run_main("check-config", repo_root=root)
            self.assertEqual(code, 0)
            self.assertIn("valid", out.lower())

    def test_check_config_alias_exits_1_for_invalid_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            # Component references a non-existent boundary path → validation error
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["missing.yaml"]},
                    }
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg))
            code, _, _ = _run_main("check-config", repo_root=root)
            self.assertNotEqual(code, 0)


class MainMigrateLockTests(unittest.TestCase):
    """migrate-lock runs without a git repo."""

    def test_migrate_lock_missing_file_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            code, _, err = _run_main("migrate-lock", "--lock", str(Path(td) / "nope.json"))
            self.assertEqual(code, 2)
            self.assertIn("error", err.lower())

    def test_migrate_lock_invalid_json_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("{not json}")
            code, _, err = _run_main("migrate-lock", "--lock", str(p))
            self.assertEqual(code, 2)

    def test_migrate_lock_unknown_schema_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "boundary.lock.json"
            p.write_text(json.dumps({"schema": "boundary-lock/v99", "components": {}, "slices": {}}))
            code, _, err = _run_main("migrate-lock", "--lock", str(p))
            self.assertEqual(code, 2)
            self.assertIn("v99", err)

    def test_migrate_lock_v2_dry_run_requires_regeneration_no_write(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "boundary.lock.json"
            lf = {"schema": "boundary-lock/v2", "project": "x", "components": {}, "slices": {}, "generated_at": "ts"}
            original = json.dumps(lf)
            p.write_text(original)
            code, out, _ = _run_main("migrate-lock", "--lock", str(p), "--dry-run")
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertEqual(p.read_text(), original)  # file unchanged

    def test_migrate_lock_current_v3_cleanup_writes_in_place(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "boundary.lock.json"
            lf = {
                "schema": "boundary-lock/v3",
                "config_contract": "boundver-semantic-config/v2",
                "project": "x",
                "components": {},
                "slices": {},
                "generated_at": "ts",
            }
            p.write_text(json.dumps(lf))
            code, _, _ = _run_main("migrate-lock", "--lock", str(p))
            self.assertEqual(code, 0)
            rewritten = json.loads(p.read_text())
            self.assertNotIn("generated_at", rewritten)


class MainVerifyErrorPathTests(unittest.TestCase):
    """Verify command configuration-loading error paths."""

    def test_verify_missing_config_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "boundary.lock.json").write_text("{}")
            commit_all(root, "add lock")
            code, _, err = _run_main("verify", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("ERROR", err)

    def test_verify_bad_config_json_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "boundary.config.json").write_text("{bad json}")
            (root / "boundary.lock.json").write_text("{}")
            commit_all(root, "add invalid config and lock")
            code, _, err = _run_main("verify", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("ERROR", err)


class MainWhyErrorPathTests(unittest.TestCase):
    """why command configuration and lockfile error paths."""

    def test_why_missing_config_exits_2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "boundary.lock.json").write_text("{}")
            commit_all(root, "add lock")
            code, _, err = _run_main("why", "svc", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("Config", err)

    def test_why_missing_lockfile_exits_2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = {"project": "p", "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}}, "slices": {}}
            (root / "boundary.config.json").write_text(json.dumps(cfg))
            commit_all(root, "add config")
            code, _, err = _run_main("why", "svc", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("Lockfile", err)

    def test_why_bad_config_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "boundary.config.json").write_text("{bad}")
            (root / "boundary.lock.json").write_text("{}")
            commit_all(root, "add invalid config and lock")
            code, _, err = _run_main("why", "svc", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("ERROR", err)


class MainExplainErrorPathTests(unittest.TestCase):
    """explain command configuration-loading error paths."""

    def test_explain_bad_config_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "boundary.config.json").write_text("{bad}")
            commit_all(root, "add invalid config")
            code, _, err = _run_main("explain", "svc", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("ERROR", err)


class ResolvAllowCustomTests(unittest.TestCase):
    """_resolve_allow_custom honors the command-line override."""

    def test_allow_custom_providers_flag_activates_custom_loading(self):
        """--allow-custom-providers enables custom provider loading."""
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x = 1\n")
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit", "paths": []}}},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg))
            subprocess.run(["git", "add", "."], cwd=root, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True)
            # --allow-custom-providers should not cause failure even with no custom providers declared.
            code, _, err = _run_main(
                "generate", "--allow-custom-providers", "--dry-run",
                repo_root=root,
            )
            self.assertEqual(code, 0)


class CliEntrypointTests(unittest.TestCase):
    """Package CLI entrypoint behavior."""

    def test_cli_main_entrypoint_delegates_to_core(self):
        """boundver.cli.main() delegates to core.main()."""
        import io
        from unittest.mock import patch
        from boundver.cli import main as cli_main
        out_buf = io.StringIO()
        with patch("sys.argv", ["boundver"]):
            try:
                with patch("sys.stdout", out_buf):
                    cli_main()
            except SystemExit as e:
                # Exits 2 because no subcommand given — that's expected.
                self.assertEqual(int(e.code), 2)


class CoreVerifyLockfileNotFoundTests(unittest.TestCase):
    """verify command behavior for missing or malformed lockfiles."""

    def test_verify_lockfile_not_found_exits_2(self):
        """verify exits 2 when the lockfile does not exist."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = {"project": "p", "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit"}}}, "slices": {}}
            (root / "boundary.config.json").write_text(json.dumps(cfg))
            code, _, err = _run_main("verify", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("ERROR", err)

    def test_verify_lockfile_bad_json_exits_2(self):
        """verify exits 2 when the lockfile contains invalid JSON."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = {"project": "p", "components": {}, "slices": {}}
            (root / "boundary.config.json").write_text(json.dumps(cfg))
            (root / "boundary.lock.json").write_text("{bad json}")
            code, _, err = _run_main("verify", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("ERROR", err)


class CoreDiffFileNotFoundTests(unittest.TestCase):
    """diff command behavior for missing or malformed lockfiles."""

    def test_diff_old_file_not_found_exits_2(self):
        """diff exits 2 when the old lockfile is missing."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            new_lock = td + "/new.lock.json"
            Path(new_lock).write_text(json.dumps({"schema": "boundary-lock/v1", "project": "p", "components": {}, "slices": {}}))
            code, _, err = _run_main("diff", td + "/missing.lock.json", new_lock, repo_root=root)
            self.assertEqual(code, 2)

    def test_diff_invalid_json_exits_2(self):
        """diff exits 2 when a lockfile contains invalid JSON."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            old_lock = td + "/old.lock.json"
            new_lock = td + "/new.lock.json"
            Path(old_lock).write_text("{bad json}")
            Path(new_lock).write_text(json.dumps({"schema": "boundary-lock/v1", "project": "p", "components": {}, "slices": {}}))
            code, _, err = _run_main("diff", old_lock, new_lock, repo_root=root)
            self.assertEqual(code, 2)


class CoreStatusBadJsonTests(unittest.TestCase):
    """status command behavior for a malformed lockfile."""

    def test_status_bad_json_lockfile_exits_2(self):
        """status exits 2 when boundary.lock.json is not valid JSON."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "boundary.lock.json").write_text("{bad json}")
            code, _, err = _run_main("status", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("ERROR", err)


class CoreSliceLockfileNotFoundTests(unittest.TestCase):
    """slice command behavior when its lockfile does not exist."""

    def test_slice_lockfile_not_found_exits_2(self):
        """slice exits 2 when the lockfile does not exist."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            code, _, err = _run_main("slice", "myslice", repo_root=root)
            self.assertEqual(code, 2)
            self.assertIn("ERROR", err)


class MainBehaviorTierCLITests(unittest.TestCase):
    """CLI integration tests for the behavior tier."""

    def _setup(self, root: Path) -> None:
        (root / "svc").mkdir()
        (root / "svc" / "main.py").write_text("x=1\n")
        (root / "svc" / "api.yaml").write_text("openapi: 3.0\n")
        (root / "svc" / "config.json").write_text('{"timeout": 30}\n')
        cfg = {
            "project": "p",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                    "behavior": {"paths": ["api.yaml", "config.json"]},
                }
            },
            "slices": {
                "behavior-slice": {"mode": "behavior", "components": ["svc"]},
            },
        }
        (root / "boundary.config.json").write_text(json.dumps(cfg) + "\n")

    def test_generate_json_includes_behavior_fingerprint(self):
        """generate --format json output includes behavior in fingerprints."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            code, out, err = _run_main(
                "--quiet", "generate", "--source", "working-tree", "--format", "json", repo_root=root
            )
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            fp = payload["components"]["svc"]["fingerprints"]
            self.assertIn("behavior", fp)
            self.assertIsNotNone(fp["behavior"])
            self.assertEqual(len(fp["behavior"]), 64)

    def test_verify_detects_behavior_drift_via_cli(self):
        """verify uses the behavior-specific exit code when behavior drifts."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            code, _, err = _run_main("generate", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, 0, err)
            # Change behavior-relevant file
            (root / "svc" / "config.json").write_text('{"timeout": 5}\n')
            code, out, _ = _run_main("verify", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, core.EXIT_BEHAVIOR)

    def test_verify_json_reports_behavior_mismatch(self):
        """verify --format json reports behavior mismatch in issues."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            (root / "svc" / "config.json").write_text('{"timeout": 5}\n')
            code, out, _ = _run_main(
                "verify", "--source", "working-tree", "--format", "json", repo_root=root
            )
            self.assertEqual(code, core.EXIT_BEHAVIOR)
            payload = json.loads(out)
            behavior_issues = [i for i in payload["issues"] if "behavior" in i]
            self.assertGreater(len(behavior_issues), 0)

    def test_behavior_slice_in_generated_lockfile(self):
        """behavior slice appears in generated lockfile with correct mode."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            code, out, err = _run_main(
                "--quiet", "generate", "--source", "working-tree", "--format", "json", repo_root=root
            )
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            s = payload["slices"]["behavior-slice"]
            self.assertEqual(s["mode"], "behavior")
            self.assertIsNotNone(s["fingerprint"])

    def test_diff_shows_behavioral_change(self):
        """diff between two lockfiles with behavior change shows correct summary."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            # Generate first lockfile
            _run_main("generate", "--source", "working-tree", repo_root=root)
            lock1 = Path(td) / "lock1.json"
            lock1.write_text((root / "boundary.lock.json").read_text())
            # Change config (behavior-relevant, not boundary-relevant)
            (root / "svc" / "config.json").write_text('{"timeout": 5}\n')
            _run_main("generate", "--source", "working-tree", repo_root=root)
            lock2 = root / "boundary.lock.json"
            code, out, _ = _run_main("diff", str(lock1), str(lock2), "--format", "json")
            self.assertEqual(code, 0)
            payload = json.loads(out)
            changed = payload["components"]["changed"]
            self.assertEqual(len(changed), 1)
            self.assertIn("behavior", changed[0]["changed_facets"])
            self.assertNotIn("boundary", changed[0]["changed_facets"])

    def test_why_reports_behavior_drift(self):
        """why command reports behavior drift when config changes."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            (root / "svc" / "config.json").write_text('{"timeout": 5}\n')
            code, out, _ = _run_main("why", "svc", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, 1)
            self.assertIn("behavior", out.lower())
            self.assertIn("behavioral", out.lower())

    def test_generate_components_filter_with_behavior(self):
        """generate --components works correctly with behavior configured."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            # Full generate first
            _run_main("generate", "--source", "working-tree", repo_root=root)
            # Partial regenerate
            code, _, err = _run_main(
                "generate", "--source", "working-tree", "--components", "svc", repo_root=root
            )
            self.assertEqual(code, 0, err)
            lock = json.loads((root / "boundary.lock.json").read_text())
            self.assertIsNotNone(lock["components"]["svc"]["fingerprints"]["behavior"])

    def test_status_shows_behavior_drift_in_issues(self):
        """status with drift shows behavior mismatch in DRIFT section."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            _run_main("generate", "--source", "working-tree", repo_root=root)
            (root / "svc" / "config.json").write_text('{"timeout": 5}\n')
            code, out, _ = _run_main("status", "--source", "working-tree", repo_root=root)
            self.assertEqual(code, 0)
            self.assertIn("DRIFT", out)


if __name__ == "__main__":
    unittest.main()
