"""Six promises about the two commands that edit a config and the flag that
picks a diagnostic base.

`add`, `remove` and `init` are the commands a developer runs reflexively on a
branch someone else wrote, which is why the rule `_resolve_allow_custom` states
in its docstring - "checking out an untrusted branch must never be enough to
import its Python" - is most attractive to break exactly there. Neither `add`
nor `remove` defines `--allow-custom-providers`, so `main()`'s
`BOUNDVER_ALLOW_CUSTOM_PROVIDERS` fallback, guarded by `hasattr`, must leave
them at `allow_custom=False` even when the variable is set. The hard part of
testing that is not the refusal but its cost: an absent import proves nothing
unless the same module, the same variable and the same repository demonstrably
do produce an import somewhere. The fixture therefore ships a provider module
that writes a marker file at import time and runs it twice - once through
`validate-config`, which accepts the flag and does import it, and once through
`add` and `remove`, which do not. Only the pair is evidence. The other half of
that obligation is the one a hardening patch would break: with the flag off the
provider-load errors are discarded and `get_provider` returns None, so a config
that declares a `providers` array and a `custom.foo` component still passes both
validation gates and is rewritten intact, and that is asserted on the same
fixture rather than on a config that avoids custom providers.

The `--base-ref` guard uses a subprocess recorder. Argument-shaped, empty, and
whitespace-only values are now refused immediately after argument parsing,
before repository discovery starts. A valid `HEAD~1` premise reaches exactly
one `rev-parse` argument and the resulting immutable object ID, not the moving
name, reaches the diff.

Four divergences remain as expected failures, each with the current behaviour
pinned beside it so a partial fix cannot pass unnoticed. `add` and `remove` are
read-modify-write over the whole document
with no comparison of the leaf against what was read, so a competing write
landing between the load and the publish is silently discarded while both runs
exit 0. That one is demonstrated by interposing the competing write inside
`_write_config_atomic` itself, and by a premise run that performs the same
interposition but skips boundver's write, showing the competitor's edit does
survive when nothing overwrites it. The other three are `init` promising that
`validate-config` will pass for a scaffold it rejects, the post-edit refusal that
names config fields rather than the flags that produced them, and the empty
stdout a `--format json` consumer receives for a syntactically invalid ref.

Covers OBL-PROVIDERS-063, OBL-PROVIDERS-064, OBL-PROVIDERS-065,
OBL-PROVIDERS-066, OBL-GIT-SOURCE-131 and OBL-GIT-SOURCE-132.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest import mock

import boundver
from boundver import _git as git
from boundver import core
from boundver._cli_parser import build_parser

from tests._parity import run_cli, run_cli_in_process
from tests._repo_fixtures import init_git_repo
from tests._scenarios import Scenario

try:
    import jsonschema

    HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover - the suite installs it
    HAS_JSONSCHEMA = False

USAGE = 2
DRIFT = 1

REPO_ROOT = Path(__file__).resolve().parents[1]
WHY_SCHEMA = REPO_ROOT / "spec" / "cli-output.why.schema.json"
CONFIG_SCHEMA = Path(boundver.__file__).resolve().parent / "boundary.config.schema.json"

#: A provider module whose import is observable: it writes the file named by
#: BOUNDVER_PROVIDER_IMPORT_MARKER before defining anything. Everything below
#: the marker is the smallest object `load_custom_providers` will register.
SIDECAR_MODULE = (
    "import os\n"
    "from pathlib import Path\n"
    "\n"
    "_marker = os.environ.get('BOUNDVER_PROVIDER_IMPORT_MARKER')\n"
    "if _marker:\n"
    "    Path(_marker).write_text('imported', encoding='utf-8')\n"
    "\n"
    "\n"
    "class FooProvider:\n"
    "    name = 'custom.foo'\n"
    "    version = '1'\n"
    "\n"
    "    def resolve(self, ctx):\n"
    "        return None\n"
    "\n"
    "    def validate_config(self, boundary_cfg, component_path, repo_root):\n"
    "        return []\n"
    "\n"
    "    def explain_diff(self, old_metadata, new_metadata, ctx):\n"
    "        return 'changed'\n"
)

#: The exact bytes `boundver init` writes without --discover, as a template over
#: the one field it copies from the checkout. A release that moves the schema
#: URL has to move this line deliberately; that is what makes it a golden.
INIT_SCAFFOLD = """{
  "$schema": "https://raw.githubusercontent.com/yzm1/boundver/v0.16.0/boundary.config.schema.json",
  "project": %s,
  "defaults": {
    "compat_mode": "major"
  },
  "components": {
    "example-component": {
      "path": "src",
      "version_source": null,
      "boundary": {
        "provider": "implicit",
        "paths": []
      }
    }
  }
}
"""

INIT_NEXT_LINE = (
    "Next: review the config, then run `boundver validate-config` and "
    "`boundver generate`."
)

#: Every --paths spelling the obligation states a rule for, and the boundary
#: path list `add` must store for it. The provider is `leaf` throughout so the
#: empty results are legal and the composition rule is the only thing measured.
SELECTOR_SPELLINGS: Dict[str, Tuple[List[str], List[str]]] = {
    "a run of commas drops the empty entry": (["--paths", "a,,b"], ["a", "b"]),
    "surrounding whitespace is stripped": (["--paths", " a , b "], ["a", "b"]),
    "a lone comma yields nothing": (["--paths", ","], []),
    "an empty value yields nothing": (["--paths", ""], []),
    "--paths entries precede --boundary-path entries": (
        ["--paths", "a,b", "--boundary-path", "c,d", "--boundary-path", "e"],
        ["a", "b", "c,d", "e"],
    ),
}

#: The two provider shapes that make an otherwise well-formed `add` fail after
#: the edit, and the validation line each must produce.
PROVIDER_REFUSALS: Dict[str, Tuple[List[str], str]] = {
    "explicit provider with no boundary paths": (
        ["--provider", "openapi"],
        "  - Component 'sdk': No boundary paths declared for explicit "
        "boundary provider",
    ),
    "misspelled builtin provider": (
        ["--provider", "openpai"],
        "  - Component 'sdk' has unsupported boundary.provider 'openpai' "
        "(use a known provider or custom.* namespace)",
    ),
}

#: The trailer `add` prints after a post-edit validation failure. It names
#: config fields; the obligation asks for the flags that produced them.
ADD_REFUSAL_TRAILER = (
    "Correct the component path, --provider, --paths/--boundary-path, or "
    "policy before retrying."
)

#: The flags a user would have to reach for after each PROVIDER_REFUSALS case.
ADD_FLAGS = ("--paths", "--boundary-path", "--provider")


def _selector_scene() -> Scenario:
    """One component plus four addable files, one of whose names has a comma."""
    scene = Scenario()
    scene.component("svc", path="svc", provider="leaf")
    scene.file("svc/main.py", "x\n")
    for name in ("a", "b", "c,d", "e"):
        scene.file(f"sdk/{name}", "y\n")
    scene.commit()
    return scene


def _custom_provider_scene() -> Scenario:
    """A config that declares a custom provider and a component that uses it."""
    scene = Scenario()
    scene.component("svc", path="svc", provider="custom.foo")
    scene.component("sdk", path="sdk", provider="leaf")
    scene.config["providers"] = [
        {"name": "custom.foo", "module": "sidecar_provider", "class": "FooProvider"}
    ]
    scene.file("svc/main.py", "x\n")
    scene.file("sdk/a", "y\n")
    # A third directory, so `add` has a path no existing component claims.
    scene.file("api/a", "z\n")
    scene.file("sidecar_provider.py", SIDECAR_MODULE)
    scene.commit()
    return scene


def _drift_scene() -> Scenario:
    """A committed lock plus one later commit, so `why` has something to say."""
    scene = Scenario()
    scene.component("svc", path="svc", provider="leaf")
    scene.file("svc/impl.py", "VALUE = 1\n")
    scene.commit("first")
    run_cli(scene.root, "generate")
    scene.commit("lock")
    scene.file("svc/impl.py", "VALUE = 2\n")
    scene.commit("drift")
    return scene


class _Recorded:
    """One in-process CLI run plus the argv of every subprocess it spawned.

    `run_cli_in_process` is the model; the addition is a `subprocess.Popen`
    subclass installed for the duration of the call, which is where every Git
    invocation in this codebase ultimately lands.
    """

    def __init__(self, root: Path, *args: str) -> None:
        self.argv: List[List[str]] = []
        real_popen = subprocess.Popen
        recorded = self.argv

        class RecordingPopen(real_popen):
            def __init__(self, command, *rest: Any, **options: Any) -> None:
                recorded.append(
                    [command] if isinstance(command, str) else list(command)
                )
                super().__init__(command, *rest, **options)

        out, err = io.StringIO(), io.StringIO()
        previous_argv, previous_directory = sys.argv[:], Path.cwd()
        try:
            os.chdir(root)
            sys.argv = ["boundver", *args]
            with mock.patch.object(subprocess, "Popen", RecordingPopen):
                with redirect_stdout(out), redirect_stderr(err):
                    try:
                        core.main()
                    except SystemExit as exc:
                        self.code = int(exc.code or 0)
                    else:
                        self.code = 0
        finally:
            sys.argv = previous_argv
            os.chdir(previous_directory)
        self.stdout = out.getvalue()
        self.stderr = err.getvalue()

    def carrying(self, value: str) -> List[List[str]]:
        return [command for command in self.argv if value in command]


class CustomProviderImportTests(unittest.TestCase):
    """OBL-PROVIDERS-063: a rewrite never imports the branch's Python."""

    def _run(self, scene: Scenario, marker: Path, *args: str):
        environment = {
            "BOUNDVER_ALLOW_CUSTOM_PROVIDERS": "1",
            "BOUNDVER_PROVIDER_IMPORT_MARKER": str(marker),
        }
        with mock.patch.dict(os.environ, environment):
            return run_cli(scene.root, *args)

    def test_neither_mutation_command_defines_the_flag_the_fallback_looks_for(self):
        """Read the surface from the parser rather than from the source file."""
        parser = build_parser(version="test", epilog="")
        subparsers = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        self.assertEqual(len(subparsers), 1)
        accepting = set()
        for name, sub in subparsers[0].choices.items():
            options = {
                spelling
                for action in sub._actions
                for spelling in action.option_strings
            }
            if "--allow-custom-providers" in options:
                accepting.add(name)
        # The premise: the enumeration can find the flag where it exists.
        self.assertIn("validate-config", accepting)
        self.assertIn("status", accepting)
        self.assertNotIn("add", accepting)
        self.assertNotIn("remove", accepting)

    def test_a_command_that_accepts_the_flag_does_import_under_the_variable(self):
        """The premise for every absent marker below."""
        with tempfile.TemporaryDirectory() as holder:
            marker = Path(holder) / "imported"
            with _custom_provider_scene() as scene:
                result = self._run(scene, marker, "validate-config")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout, "Config is valid.\n")
            self.assertTrue(marker.exists(), "the module never imported")
            self.assertEqual(marker.read_text(encoding="utf-8"), "imported")

    def test_neither_rewrite_imports_the_module_under_the_variable(self):
        commands = {
            "add": (("add", "api", "api", "--provider", "leaf"), 0),
            "remove": (("remove", "sdk"), 0),
        }
        with tempfile.TemporaryDirectory() as holder:
            for label, (args, expected) in commands.items():
                with self.subTest(command=label):
                    marker = Path(holder) / f"{label}.marker"
                    with _custom_provider_scene() as scene:
                        result = self._run(scene, marker, *args)
                    self.assertEqual(
                        result.returncode, expected, result.stdout + result.stderr
                    )
                    self.assertEqual(result.stderr, "")
                    self.assertFalse(
                        marker.exists(),
                        f"`boundver {label}` imported the custom provider module",
                    )

    def test_the_rewrite_still_succeeds_and_keeps_the_custom_declaration(self):
        """The refusal to import must not become a refusal to operate."""
        with tempfile.TemporaryDirectory() as holder:
            marker = Path(holder) / "add.marker"
            with _custom_provider_scene() as scene:
                result = self._run(
                    scene, marker, "add", "api", "api", "--provider", "leaf"
                )
                document = json.loads(
                    (scene.root / "boundary.config.json").read_text(encoding="utf-8")
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout,
                "Added component 'api' at path 'api'\n"
                "Run: boundver generate --source working-tree\n",
            )
            self.assertEqual(sorted(document["components"]), ["api", "sdk", "svc"])
            self.assertEqual(
                document["components"]["svc"]["boundary"],
                {"provider": "custom.foo", "paths": []},
            )
            self.assertEqual(
                document["providers"],
                [
                    {
                        "name": "custom.foo",
                        "module": "sidecar_provider",
                        "class": "FooProvider",
                    }
                ],
            )

    def test_remove_leaves_the_custom_component_and_its_declaration_intact(self):
        with tempfile.TemporaryDirectory() as holder:
            marker = Path(holder) / "remove.marker"
            with _custom_provider_scene() as scene:
                result = self._run(scene, marker, "remove", "sdk")
                document = json.loads(
                    (scene.root / "boundary.config.json").read_text(encoding="utf-8")
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout, "Removed component 'sdk'\nRun: boundver generate\n"
            )
            self.assertEqual(sorted(document["components"]), ["svc"])
            self.assertEqual(
                document["components"]["svc"]["boundary"]["provider"], "custom.foo"
            )
            self.assertIn("providers", document)


class InitScaffoldGoldenTests(unittest.TestCase):
    """OBL-PROVIDERS-064: the first bytes every new user sees."""

    def _repository(self, holder: str, name: str) -> Path:
        root = Path(holder) / name
        root.mkdir()
        init_git_repo(root)
        return root

    def test_the_scaffold_is_the_golden_document(self):
        with tempfile.TemporaryDirectory() as holder:
            root = self._repository(holder, "widgets")
            result = run_cli(root, "init")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (root / "boundary.config.json").read_bytes(),
                (INIT_SCAFFOLD % '"widgets"').encode("utf-8"),
            )

    def test_the_schema_url_is_the_id_of_the_schema_this_build_validates(self):
        declared = json.loads(CONFIG_SCHEMA.read_text(encoding="utf-8"))["$id"]
        with tempfile.TemporaryDirectory() as holder:
            root = self._repository(holder, "widgets")
            run_cli(root, "init")
            written = json.loads(
                (root / "boundary.config.json").read_text(encoding="utf-8")
            )
        self.assertEqual(written["$schema"], declared)

    def test_init_reports_why_a_written_scaffold_needs_edits(self):
        """Two ways the scaffold is born invalid, and the warnings each produces."""
        born_invalid = {
            # No src/ in the checkout: the component path is not a directory.
            "no source directory": (
                "widgets",
                ["  - Component 'example-component' path not found or not a "
                 "directory: src"],
            ),
            # repo_root.name is copied into `project` unchecked.
            "unaddressable project name": (
                " lead",
                [
                    "  - Field 'project' must not have surrounding whitespace",
                    "  - Component 'example-component' path not found or not a "
                    "directory: src",
                ],
            ),
        }
        with tempfile.TemporaryDirectory() as holder:
            for label, (name, expected) in born_invalid.items():
                with self.subTest(case=label):
                    root = self._repository(holder, name)
                    created = run_cli(root, "init")
                    self.assertEqual(created.returncode, 0, created.stderr)
                    self.assertEqual(
                        created.stderr.splitlines(),
                        ["WARNING: The scaffold needs edits before it will validate:",
                         *expected],
                    )
                    lines = created.stdout.splitlines()
                    self.assertEqual(len(lines), 2, created.stdout)
                    self.assertTrue(lines[0].startswith("Created "), lines[0])
                    self.assertTrue(
                        lines[0].endswith(
                            "boundary.config.json with 1 component(s)."
                        ),
                        lines[0],
                    )
                    self.assertEqual(
                        lines[1],
                        "Next: edit the config, then run `boundver validate-config`.",
                    )
                    self.assertEqual(
                        (root / "boundary.config.json").read_bytes(),
                        (INIT_SCAFFOLD % json.dumps(name)).encode("utf-8"),
                    )
                    checked = run_cli(root, "validate-config")
                    self.assertEqual(checked.returncode, USAGE, checked.stdout)
                    self.assertEqual(
                        checked.stdout.splitlines(),
                        [f"CONFIG INVALID ({len(expected)} issues):", *expected],
                    )

    def test_the_same_scaffold_validates_once_the_component_path_exists(self):
        """The premise: the promise `init` prints is keepable, just not kept."""
        with tempfile.TemporaryDirectory() as holder:
            root = self._repository(holder, "widgets")
            (root / "src").mkdir()
            (root / "src" / "module.py").write_text("x\n", encoding="utf-8")
            created = run_cli(root, "init")
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(created.stdout.splitlines()[1], INIT_NEXT_LINE)
            checked = run_cli(root, "validate-config")
            self.assertEqual(checked.returncode, 0, checked.stdout)
            self.assertEqual(checked.stdout, "Config is valid.\n")

    def test_init_refuses_to_overwrite_the_scaffold(self):
        with tempfile.TemporaryDirectory() as holder:
            root = self._repository(holder, "widgets")
            self.assertEqual(run_cli(root, "init").returncode, 0)
            again = run_cli(root, "init")
            self.assertEqual(again.returncode, USAGE)
            self.assertEqual(again.stdout, "")
            self.assertIn("ERROR: Config already exists:", again.stderr)

    def test_init_warns_when_the_scaffold_it_wrote_cannot_be_validated(self):
        """An invalid starter is written for editing but never called ready."""
        with tempfile.TemporaryDirectory() as holder:
            root = self._repository(holder, " lead")
            created = run_cli(root, "init")
            warned = created.stderr != "" or INIT_NEXT_LINE not in created.stdout
            self.assertTrue(
                warned,
                "init promised validate-config would work for a config it "
                "rejects",
            )

    def test_valid_and_invalid_scaffolds_have_distinct_guidance(self):
        with tempfile.TemporaryDirectory() as holder:
            valid = self._repository(holder, "valid")
            (valid / "src").mkdir()
            (valid / "src" / "module.py").write_text("x\n", encoding="utf-8")
            invalid = self._repository(holder, "invalid")
            reports = {}
            for label, root in (("valid", valid), ("invalid", invalid)):
                result = run_cli(root, "init")
                reports[label] = (
                    result.returncode,
                    result.stderr,
                    result.stdout.splitlines()[1],
                )
        self.assertEqual(reports["valid"], (0, "", INIT_NEXT_LINE))
        self.assertEqual(reports["invalid"][0], 0)
        self.assertIn("WARNING: The scaffold needs edits", reports["invalid"][1])
        self.assertNotEqual(reports["invalid"][2], INIT_NEXT_LINE)


class AddBoundaryPathCompositionTests(unittest.TestCase):
    """OBL-PROVIDERS-065: how the two boundary-path spellings compose."""

    def _add(self, scene: Scenario, *extra: str, provider: str = "path-hash"):
        return run_cli(
            scene.root, "add", "sdk", "sdk", "--provider", provider, *extra
        )

    def test_every_selector_spelling_stores_the_stated_list(self):
        for label, (extra, expected) in SELECTOR_SPELLINGS.items():
            with self.subTest(spelling=label):
                with _selector_scene() as scene:
                    provider = "leaf" if not expected else "path-hash"
                    result = self._add(scene, *extra, provider=provider)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    document = json.loads(
                        (scene.root / "boundary.config.json").read_text("utf-8")
                    )
                self.assertEqual(
                    document["components"]["sdk"]["boundary"]["paths"], expected
                )

    def test_a_path_supplied_through_both_flags_is_refused_after_the_edit(self):
        with _selector_scene() as scene:
            path = scene.root / "boundary.config.json"
            before = path.read_bytes()
            result = self._add(scene, "--paths", "a", "--boundary-path", "a")
            after = path.read_bytes()
        self.assertEqual(result.returncode, USAGE, result.stdout)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr.splitlines(),
            [
                "ERROR: Adding 'sdk' would leave an invalid config:",
                "  - Component 'sdk' field 'boundary.paths' contains duplicates",
                ADD_REFUSAL_TRAILER,
            ],
        )
        self.assertEqual(after, before)

    def test_the_same_two_flags_without_a_collision_do_rewrite_the_file(self):
        """The premise for the byte-identical assertion above."""
        with _selector_scene() as scene:
            path = scene.root / "boundary.config.json"
            before = path.read_bytes()
            result = self._add(scene, "--paths", "a", "--boundary-path", "b")
            after = path.read_bytes()
            document = json.loads(after.decode("utf-8"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(after, before)
        self.assertEqual(document["components"]["sdk"]["boundary"]["paths"], ["a", "b"])

    def test_the_two_provider_shapes_that_fail_after_the_edit(self):
        for label, (extra, expected) in PROVIDER_REFUSALS.items():
            with self.subTest(shape=label):
                with _selector_scene() as scene:
                    path = scene.root / "boundary.config.json"
                    before = path.read_bytes()
                    result = run_cli(scene.root, "add", "sdk", "sdk", *extra)
                    after = path.read_bytes()
                self.assertEqual(result.returncode, USAGE, result.stdout)
                self.assertEqual(result.stdout, "")
                self.assertEqual(
                    result.stderr.splitlines(),
                    [
                        "ERROR: Adding 'sdk' would leave an invalid config:",
                        expected,
                        ADD_REFUSAL_TRAILER,
                    ],
                )
                self.assertEqual(after, before)

    def test_the_refusal_names_the_flag_the_user_has_to_supply(self):
        """The refusal points from config validation back to CLI controls."""
        with _selector_scene() as scene:
            result = run_cli(scene.root, "add", "sdk", "sdk", "--provider", "openapi")
        self.assertTrue(
            any(flag in result.stderr for flag in ADD_FLAGS),
            f"no flag named in: {result.stderr!r}",
        )

    def test_the_trailer_names_the_relevant_cli_flags(self):
        for label, (extra, _) in PROVIDER_REFUSALS.items():
            with self.subTest(shape=label):
                with _selector_scene() as scene:
                    result = run_cli(scene.root, "add", "sdk", "sdk", *extra)
                self.assertTrue(result.stderr.endswith(ADD_REFUSAL_TRAILER + "\n"))
                self.assertTrue(any(flag in result.stderr for flag in ADD_FLAGS))


class ConcurrentConfigEditTests(unittest.TestCase):
    """OBL-PROVIDERS-066: what survives when the file moves under the command."""

    def _race(self, scene: Scenario, *args: str, publish: bool = True):
        """Interpose a competing component between the load and the replace."""
        path = scene.root / "boundary.config.json"
        observed: Dict[str, Any] = {}
        real_write = core._write_config_atomic

        def racing_write(target, value, *, expected_content=None):
            document = json.loads(Path(target).read_text(encoding="utf-8"))
            document["components"]["rival"] = {
                "path": "sdk",
                "version_source": None,
                "boundary": {"provider": "leaf", "paths": []},
            }
            Path(target).write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8"
            )
            observed["at_publish"] = sorted(
                json.loads(Path(target).read_text(encoding="utf-8"))["components"]
            )
            if publish:
                real_write(target, value, expected_content=expected_content)

        with mock.patch.object(core, "_write_config_atomic", racing_write):
            result = run_cli_in_process(scene.root, *args)
        observed["after"] = sorted(
            json.loads(path.read_text(encoding="utf-8"))["components"]
        )
        return result, observed

    def test_the_competing_edit_is_on_disk_when_the_command_publishes(self):
        """The premise: without boundver's write the rival survives."""
        with _selector_scene() as scene:
            result, observed = self._race(
                scene, "add", "sdk", "sdk", "--provider", "leaf", publish=False
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rival", observed["at_publish"])
        self.assertEqual(observed["after"], ["rival", "svc"])

    def test_add_refuses_instead_of_discarding_the_competing_edit(self):
        with _selector_scene() as scene:
            result, observed = self._race(
                scene, "add", "sdk", "sdk", "--provider", "leaf"
            )
        self.assertEqual(result.returncode, USAGE, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("changed before publication", result.stderr)
        self.assertEqual(observed["at_publish"], ["rival", "svc"])
        self.assertEqual(observed["after"], ["rival", "svc"])

    def test_remove_refuses_instead_of_discarding_the_competing_edit(self):
        with _custom_provider_scene() as scene:
            result, observed = self._race(scene, "remove", "sdk")
        self.assertEqual(result.returncode, USAGE, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("changed before publication", result.stderr)
        self.assertEqual(observed["at_publish"], ["rival", "sdk", "svc"])
        self.assertEqual(observed["after"], ["rival", "sdk", "svc"])

    def test_a_config_that_changed_under_the_command_is_refused(self):
        """A compare-before-publish guard preserves an intervening edit."""
        with _selector_scene() as scene:
            result, observed = self._race(
                scene, "add", "sdk", "sdk", "--provider", "leaf"
            )
        self.assertEqual(result.returncode, USAGE, result.stdout)
        self.assertEqual(observed["after"], ["rival", "svc"])


class BaseRefGuardTests(unittest.TestCase):
    """OBL-GIT-SOURCE-131: where the argument-shaped ref is stopped."""

    #: One token, so argparse hands the value to boundver rather than refusing.
    REJECTED = "--base-ref=--output=unexpected.diff"
    #: A valid revision expression: boundver resolves it before the diff.
    ACCEPTED = "--base-ref=HEAD~1"
    VALUE = "--output=unexpected.diff"
    PREMISE_VALUE = "HEAD~1^{commit}"

    #: The refusal each command prints for the same rejected value.
    REFUSALS = {
        "why": "ERROR: invalid diagnostic base ref: '--output=unexpected.diff'",
        "explain": "ERROR: invalid base ref: '--output=unexpected.diff'",
    }

    def setUp(self):
        git._ambient_worktree_config_overrides.cache_clear()
        git._repository_filter_config_overrides.cache_clear()

    def tearDown(self):
        git._ambient_worktree_config_overrides.cache_clear()
        git._repository_filter_config_overrides.cache_clear()

    def test_an_accepted_ref_reaches_only_the_resolution_argv(self):
        with _drift_scene() as scene:
            for command in ("why", "explain"):
                with self.subTest(command=command):
                    run = _Recorded(scene.root, command, "svc", self.ACCEPTED)
                    carrying = run.carrying(self.PREMISE_VALUE)
                    self.assertEqual(len(carrying), 1, run.argv)
                    self.assertIn("rev-parse", carrying[0])
                    self.assertNotIn("diff", carrying[0])

    def test_both_commands_refuse_the_rejected_value_with_exit_two(self):
        with _drift_scene() as scene:
            for command, refusal in self.REFUSALS.items():
                with self.subTest(command=command):
                    run = _Recorded(scene.root, command, "svc", self.REJECTED)
                    self.assertEqual(run.code, USAGE, run.stderr)
                    self.assertEqual(run.stderr.splitlines(), [refusal])

    def test_no_subprocess_the_run_spawns_carries_the_rejected_value(self):
        with _drift_scene() as scene:
            for command in self.REFUSALS:
                with self.subTest(command=command):
                    run = _Recorded(scene.root, command, "svc", self.REJECTED)
                    self.assertEqual(run.carrying(self.VALUE), [])
                    self.assertEqual(run.carrying("unexpected.diff"), [])

    def test_a_dash_hidden_behind_whitespace_is_refused_the_same_way(self):
        hidden = "  --upload-pack=evil"
        with _drift_scene() as scene:
            run = _Recorded(scene.root, "why", "svc", "--base-ref", hidden)
        self.assertEqual(run.code, USAGE, run.stderr)
        self.assertEqual(
            run.stderr.splitlines(),
            [f"ERROR: invalid diagnostic base ref: {hidden!r}"],
        )
        self.assertEqual(run.carrying(hidden), [])
        self.assertEqual(run.carrying("--upload-pack=evil"), [])

    def test_the_two_token_spelling_never_reaches_boundvers_guard(self):
        """argparse claims it first, which is why the tests above use `=`."""
        with _drift_scene() as scene:
            run = _Recorded(scene.root, "why", "svc", "--base-ref", self.VALUE)
        self.assertEqual(run.code, USAGE)
        self.assertEqual(run.argv, [])
        self.assertIn("usage: boundver why", run.stderr)
        self.assertNotIn("invalid diagnostic base ref", run.stderr)

    def test_the_guard_runs_before_any_git_subprocess(self):
        """Argument-shaped refs are refused before repository probing."""
        with _drift_scene() as scene:
            run = _Recorded(scene.root, "why", "svc", self.REJECTED)
        self.assertEqual(run.argv, [])

    def test_both_commands_refuse_before_repository_work(self):
        with _drift_scene() as scene:
            for command in self.REFUSALS:
                with self.subTest(command=command):
                    run = _Recorded(scene.root, command, "svc", self.REJECTED)
                    self.assertEqual(run.argv, [])

    @unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema is not installed")
    def test_json_format_prints_a_conforming_document_for_an_accepted_ref(self):
        """The premise for the empty-stdout assertions below."""
        schema = json.loads(WHY_SCHEMA.read_text(encoding="utf-8"))
        with _drift_scene() as scene:
            run = _Recorded(
                scene.root, "why", "svc", "--format", "json", self.ACCEPTED
            )
        self.assertEqual(run.code, DRIFT, run.stderr)
        document = json.loads(run.stdout)
        jsonschema.validate(document, schema)
        self.assertEqual(document["changed_files_status"], "ok")

    def test_json_usage_errors_with_empty_stdout_are_documented(self):
        """Pre-result usage failures are explicitly outside JSON schemas."""
        reference = (REPO_ROOT / "docs" / "reference.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "--format json` may exit 2 with empty standard output",
            reference,
        )
        self.assertIn("invalid or ambiguous `--base-ref`", reference)

    def test_the_divergence_is_empty_stdout_for_a_json_consumer(self):
        with _drift_scene() as scene:
            run = _Recorded(
                scene.root, "why", "svc", "--format", "json", self.REJECTED
            )
        self.assertEqual(run.code, USAGE)
        self.assertEqual(run.stdout, "")
        self.assertEqual(run.stderr.splitlines(), [self.REFUSALS["why"]])


class BaseRefNormalizationTests(unittest.TestCase):
    """OBL-GIT-SOURCE-132: absent, empty and whitespace must differ."""

    INFERRED_ORIGIN = "commit that introduced the current lock entry for svc"

    def _why_json(self, scene: Scenario, *args: str):
        result = run_cli(scene.root, "why", "svc", "--format", "json", *args)
        return result, json.loads(result.stdout) if result.stdout.strip() else None

    def test_a_real_ref_is_reported_as_explicitly_supplied(self):
        """The premise: an honoured value is visible in the document."""
        with _drift_scene() as scene:
            resolved = scene.git("rev-parse", "HEAD~1")
            result, document = self._why_json(scene, "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, DRIFT, result.stderr)
        self.assertEqual(document["diagnostic_base"], resolved)
        self.assertEqual(document["diagnostic_base_requested"], "HEAD~1")
        self.assertEqual(document["diagnostic_base_origin"], "explicit --base-ref")
        self.assertEqual(document["changed_files_status"], "ok")

    def test_no_base_ref_infers_the_lock_history_origin(self):
        with _drift_scene() as scene:
            result, document = self._why_json(scene)
            head = scene.head()
        self.assertEqual(result.returncode, DRIFT, result.stderr)
        self.assertEqual(document["diagnostic_base_origin"], self.INFERRED_ORIGIN)
        self.assertNotEqual(document["diagnostic_base"], head)

    def test_an_empty_base_ref_is_used_or_refused_but_never_inferred(self):
        """An explicitly empty CI variable is a usage error, never inference."""
        with _drift_scene() as scene:
            result, document = self._why_json(scene, "--base-ref", "")
        self.assertEqual(result.returncode, USAGE)
        self.assertIsNone(document)
        self.assertIn("invalid diagnostic base ref", result.stderr)

    def test_an_empty_base_ref_differs_from_the_absent_spelling(self):
        with _drift_scene() as scene:
            absent, without = self._why_json(scene)
            empty, blank = self._why_json(scene, "--base-ref", "")
        self.assertEqual(absent.returncode, DRIFT)
        self.assertIsNotNone(without)
        self.assertEqual(empty.returncode, USAGE)
        self.assertIsNone(blank)
        self.assertNotEqual(absent.stdout, empty.stdout)

    def test_a_whitespace_base_ref_is_refused_before_the_diff(self):
        with _drift_scene() as scene:
            result, document = self._why_json(scene, "--base-ref", " ")
        self.assertEqual(result.returncode, USAGE)
        self.assertIsNone(document)
        self.assertIn("invalid diagnostic base ref", result.stderr)

    def test_explain_maps_the_same_three_forms_the_same_way(self):
        """The text command agrees: omission infers; explicit blanks refuse."""
        with _drift_scene() as scene:
            absent = run_cli(scene.root, "explain", "svc")
            empty = run_cli(scene.root, "explain", "svc", "--base-ref", "")
            blank = run_cli(scene.root, "explain", "svc", "--base-ref", " ")
        self.assertEqual(absent.returncode, 0, absent.stderr)
        self.assertIn(f"({self.INFERRED_ORIGIN})", absent.stdout)
        self.assertEqual(empty.returncode, USAGE)
        self.assertEqual(empty.stdout, "")
        self.assertIn("ERROR: invalid base ref: ''", empty.stderr)
        self.assertEqual(blank.returncode, USAGE, blank.stdout)
        self.assertIn("ERROR: invalid base ref: ' '", blank.stderr)


if __name__ == "__main__":
    unittest.main()
