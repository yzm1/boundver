"""Six promises about the places where repository content meets boundver's own judgement.

The thread joining these obligations is that each names a point where a value
the repository controls decides something the repository is not supposed to
decide. A version file decides whether a component has a compatibility
identity; a `providers[]` entry decides whether Python gets imported; a
`verify_facets` key decides which drift fails CI; a lock entry's provider name
decides whether structural evidence is produced; provider metadata decides what
gets written into the lock; and a component name decides nothing at all, which
is the whole point of the last one. In every case the interesting question is
not what happens on the healthy path but what happens when the repository
supplies something the design did not intend, and none of these failure
surfaces had a test naming it.

Getting at them took more fixture work than the assertions suggest. The
version-source taxonomy is nine distinct causes sharing one `return None`, and
several of them - an absent `tomllib`, an absent PyYAML, a payload over the
ten-megabyte ceiling - cannot be reached with real inputs on a healthy host, so
they are reached by patching the module attribute the reader consults at call
time and running the CLI in-process, where a patched constant still applies.
Each of the thirteen rows below builds a repository that is healthy first,
generates a lock from it, and only then breaks it, because "generate refused"
means nothing unless the same fixture is shown generating. The custom-provider
tests write real modules into a temporary directory and put it on `sys.path`,
because "was not imported" is only worth asserting against a module whose
import is observable: each writes a marker file at import time, so the negative
survives even if something later evicts it from `sys.modules`. The structural
tests drive `structural_boundary_changes` with hand-built snapshots rather than
a range review, since every guard being tested fires before either endpoint's
source is read and a real two-commit range would only add cost.

Two of the six do not say what the register expected. `dump_lockfile` does not
refuse a non-finite float: it writes a bare `NaN` or `Infinity` token that its
own strict loader then rejects, so a provider that puts `float('inf')` into
`boundary_metadata` produces a lock boundver cannot read back. That is recorded
as an expected failure with three tests pinning the current behaviour beside
it. And the facet-precedence obligation asks for "a single config exercising
all four levels at once", which cannot exist: `defaults.verify_facets` is a
config-wide key, and its presence is exactly what stops the availability
fallback from ever being consulted, so the two lowest levels are mutually
exclusive by construction. Both configs are therefore exercised, each with
three components and with and without an explicit `--facets`, and the gating is
read twice - once from the recorded policy and once from which mismatches
verification actually raises - because the precedence rule has two independent
implementations that could drift apart.

Covers OBL-HASHING-115, OBL-PROVIDERS-012, OBL-PROVIDERS-015, OBL-PROVIDERS-017,
OBL-PROVIDERS-018 and OBL-PROVIDERS-021.
"""

from __future__ import annotations

import argparse
import ast
import copy
import importlib
import inspect
import json
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st

from boundver import _config, _lockfile, core, versions
from boundver._config import validate_config
from boundver._facet_policy import facet_policy_payload
from boundver._git import GitSourceSnapshot, _capture_git_source_snapshot
from boundver._lockfile import (
    dump_lockfile,
    generate_lockfile,
    parse_lockfile_text,
    verify_lockfile,
)
from boundver._provider_diff import STRUCTURAL_DIFF_INTERFACE
from boundver._structural_review import _provider_method, structural_boundary_changes
from boundver._utils import (
    ConfigError,
    LockfileError,
    _available_component_facets,
    _issue_facet,
)
from boundver.providers import create_registry

from tests._parity import run_cli_in_process
from tests._scenarios import Scenario

FACETS = ("exact", "behavior", "boundary", "compat")

#: A syntactically valid sha256 that no fixture can compute, used to plant
#: drift in a lock without touching the repository the current side reads.
PLANTED_DIGEST = "0" * 64


# ---------------------------------------------------------------------------
# OBL-HASHING-115 - the silent-None taxonomy of the version read layer
# ---------------------------------------------------------------------------

#: Every branch of ``extract_version``/``_extract_field_from_bytes`` that
#: returns ``None`` while ``version_source`` is a non-null object. Each row
#: names a healthy fixture and the one change that breaks it: ``broken`` swaps
#: the file's bytes, ``broken_selector`` swaps the declared filename, and
#: ``patches`` removes a module attribute the reader consults at call time.
#: The healthy payload always yields the version "1.2.3" so a row that stops
#: reading its own format is visible as a premise failure rather than as a
#: passing negative.
VERSION_SOURCE_BREAKAGES: Dict[str, Dict[str, Any]] = {
    "unsupported-extension": {
        "selector": "version.json",
        "field": "version",
        "healthy": b'{"version": "1.2.3"}\n',
        "broken_selector": "version.txt",
    },
    "not-utf8": {
        "selector": "version.json",
        "field": "version",
        "healthy": b'{"version": "1.2.3"}\n',
        "broken": b'{"version": "\xff\xfe"}\n',
    },
    "over-the-size-ceiling": {
        "selector": "version.json",
        "field": "version",
        "healthy": b'{"version": "1.2.3"}\n',
        "patches": {"max_version_file_bytes": 4},
    },
    "field-absent": {
        "selector": "version.json",
        "field": "version",
        "healthy": b'{"version": "1.2.3"}\n',
        "broken": b'{"other": "1.2.3"}\n',
    },
    "dotted-path-unresolved": {
        "selector": "version.json",
        "field": "a.b",
        "healthy": b'{"a": {"b": "1.2.3"}}\n',
        "broken": b'{"a": {"c": "1.2.3"}}\n',
    },
    "intermediate-not-a-mapping": {
        "selector": "version.json",
        "field": "a.b",
        "healthy": b'{"a": {"b": "1.2.3"}}\n',
        "broken": b'{"a": "1.2.3"}\n',
    },
    "terminal-boolean": {
        "selector": "version.json",
        "field": "version",
        "healthy": b'{"version": "1.2.3"}\n',
        "broken": b'{"version": true}\n',
    },
    "terminal-null": {
        "selector": "version.json",
        "field": "version",
        "healthy": b'{"version": "1.2.3"}\n',
        "broken": b'{"version": null}\n',
    },
    "terminal-container": {
        "selector": "version.json",
        "field": "version",
        "healthy": b'{"version": "1.2.3"}\n',
        "broken": b'{"version": ["1.2.3"]}\n',
    },
    "terminal-non-finite-number": {
        "selector": "version.yaml",
        "field": "version",
        "healthy": b'version: "1.2.3"\n',
        "broken": b"version: .inf\n",
    },
    "toml-value-not-a-quoted-string": {
        "selector": "version.toml",
        "field": "version",
        "healthy": b'version = "1.2.3"\n',
        "broken": b"version = 1\n",
    },
    "no-toml-parser": {
        "selector": "version.toml",
        "field": "version",
        "healthy": b'version = "1.2.3"\n',
        "patches": {"tomllib": None},
    },
    "no-yaml-parser": {
        "selector": "version.yaml",
        "field": "version",
        "healthy": b'version: "1.2.3"\n',
        "patches": {"yaml": None},
    },
}

#: The one row the CLI never reaches, because `validate_config` rejects an
#: unsupported version-source extension before any digest is computed. The
#: library still reports it through the shared taxonomy; the CLI reports the
#: config error instead. Both refuse, and neither writes a lock.
CONFIG_GATED_BREAKAGES = frozenset({"unsupported-extension"})

VERSION_TAXONOMY_MESSAGE = "Configured version source did not produce a version"
UNSUPPORTED_EXTENSION_MESSAGE = (
    "Component 'svc' version_source.file has unsupported extension '.txt'"
)


def _start_version_patches(spec: Dict[str, Any]) -> List[Any]:
    """Start the module-attribute patches one breakage row needs.

    The size ceiling has to move in two places: ``versions`` consults it for
    the disk fallback and ``_lockfile`` consults it when handing a captured
    blob to the accessor, and a row that patched only one would be reporting
    which of the two paths it happened to take.
    """
    patchers: List[Any] = []
    if "max_version_file_bytes" in spec:
        limit = spec["max_version_file_bytes"]
        patchers.append(mock.patch.object(versions, "MAX_VERSION_FILE_BYTES", limit))
        patchers.append(mock.patch.object(_lockfile, "MAX_VERSION_FILE_BYTES", limit))
    if "tomllib" in spec:
        patchers.append(mock.patch.object(versions, "tomllib", spec["tomllib"]))
    if "yaml" in spec:
        patchers.append(mock.patch.object(versions, "yaml", spec["yaml"]))
    for patcher in patchers:
        patcher.start()
    return patchers


def _version_taxonomy_message(row: Dict[str, Any]) -> str:
    selector = row.get("broken_selector", row["selector"])
    return (
        f"{VERSION_TAXONOMY_MESSAGE} "
        f"(file {selector!r}, field {row['field']!r})"
    )


def _observe_version_breakage(label: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """Build one repository, prove it healthy, break it, and record everything."""
    selector = row["selector"]
    field = row["field"]
    scene = Scenario()
    try:
        scene.component(
            "svc",
            path="svc",
            provider="leaf",
            version_source={"file": selector, "field": field},
        )
        scene.file("svc/main.py", "x\n")
        (scene.root / "svc" / selector).write_bytes(row["healthy"])
        scene.commit()
        lock_path = scene.root / "boundary.lock.json"

        healthy_run = run_cli_in_process(scene.root, "generate", "--source", "head")
        healthy_lock = (
            json.loads(lock_path.read_text(encoding="utf-8"))
            if lock_path.exists()
            else None
        )
        healthy_disk_version = versions.extract_version(
            scene.root, "svc", {"file": selector, "field": field}
        )

        broken_selector = row.get("broken_selector")
        if broken_selector is not None:
            (scene.root / "svc" / broken_selector).write_bytes(row["healthy"])
            scene.config["components"]["svc"]["version_source"] = {
                "file": broken_selector,
                "field": field,
            }
        if row.get("broken") is not None:
            (scene.root / "svc" / selector).write_bytes(row["broken"])
        scene.commit("break")
        lock_before = lock_path.read_bytes()

        patchers = _start_version_patches(row.get("patches", {}))
        try:
            try:
                generate_lockfile(scene.config, scene.root, source="head")
            except ConfigError as exc:
                library_refusal = str(exc)
            else:
                library_refusal = None
            broken_disk_version = versions.extract_version(
                scene.root,
                "svc",
                scene.config["components"]["svc"]["version_source"],
            )
            refusal = run_cli_in_process(
                scene.root, "generate", "--source", "head", "--out", "broken.lock.json"
            )
            report = run_cli_in_process(scene.root, "verify", "--source", "head")
        finally:
            for patcher in reversed(patchers):
                patcher.stop()

        return {
            "healthy_returncode": healthy_run.returncode,
            "healthy_lock": healthy_lock,
            "healthy_disk_version": healthy_disk_version,
            "library_refusal": library_refusal,
            "broken_disk_version": broken_disk_version,
            "refusal_returncode": refusal.returncode,
            "refusal_stderr": refusal.stderr,
            "wrote_new_lock": (scene.root / "broken.lock.json").exists(),
            "lock_unchanged": lock_path.read_bytes() == lock_before,
            "verify_returncode": report.returncode,
            "verify_output": report.stdout + report.stderr,
        }
    finally:
        scene.close()


class BrokenVersionSourceTests(unittest.TestCase):
    """OBL-HASHING-115: every silent None is reported, never written as null."""

    observed: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.observed = {
            label: _observe_version_breakage(label, row)
            for label, row in VERSION_SOURCE_BREAKAGES.items()
        }

    def test_every_row_reads_its_own_format_before_it_is_broken(self):
        """The premise: each fixture produces a version and a compat digest."""
        for label in VERSION_SOURCE_BREAKAGES:
            with self.subTest(label):
                record = self.observed[label]
                self.assertEqual(record["healthy_returncode"], 0)
                entry = record["healthy_lock"]["components"]["svc"]
                self.assertEqual(entry["version"], "1.2.3")
                self.assertEqual(entry["semver"]["compat_family"], "1")
                self.assertIsNotNone(entry["fingerprints"]["compat"])
                self.assertNotIn("version_errors", entry)
                self.assertEqual(record["healthy_disk_version"], "1.2.3")

    def test_every_broken_branch_makes_the_library_refuse_to_build_a_lock(self):
        for label in VERSION_SOURCE_BREAKAGES:
            with self.subTest(label):
                refusal = self.observed[label]["library_refusal"]
                self.assertIsNotNone(refusal, "generate_lockfile returned a lock")
                self.assertEqual(
                    refusal,
                    "Lockfile generation failed:\nsvc: "
                    + _version_taxonomy_message(VERSION_SOURCE_BREAKAGES[label]),
                )

    def test_every_broken_branch_makes_the_cli_exit_two_without_writing_a_lock(self):
        for label in VERSION_SOURCE_BREAKAGES:
            with self.subTest(label):
                record = self.observed[label]
                self.assertEqual(record["refusal_returncode"], 2)
                self.assertFalse(record["wrote_new_lock"])
                self.assertTrue(record["lock_unchanged"])

    def test_the_cli_names_the_taxonomy_message_for_every_branch_it_reaches(self):
        """The one exception is pinned rather than smoothed over.

        `validate_config` rejects an unsupported version-source extension
        before generation runs, so that row exits 2 with the config error. The
        shared taxonomy message still reports it at the library level, which
        the previous test asserts for all thirteen rows.
        """
        for label in VERSION_SOURCE_BREAKAGES:
            with self.subTest(label):
                stderr = self.observed[label]["refusal_stderr"]
                if label in CONFIG_GATED_BREAKAGES:
                    self.assertIn(UNSUPPORTED_EXTENSION_MESSAGE, stderr)
                    self.assertNotIn(VERSION_TAXONOMY_MESSAGE, stderr)
                else:
                    self.assertIn("ERROR: Lockfile generation failed:", stderr)
                    self.assertIn("svc: " + VERSION_TAXONOMY_MESSAGE, stderr)

    def test_every_broken_branch_verify_reaches_reports_a_current_digest_error(self):
        for label in VERSION_SOURCE_BREAKAGES:
            with self.subTest(label):
                record = self.observed[label]
                self.assertEqual(record["verify_returncode"], 2)
                if label in CONFIG_GATED_BREAKAGES:
                    self.assertIn(
                        UNSUPPORTED_EXTENSION_MESSAGE, record["verify_output"]
                    )
                else:
                    self.assertIn(
                        "CURRENT DIGEST ERROR svc: " + VERSION_TAXONOMY_MESSAGE,
                        record["verify_output"],
                    )

    def test_the_disk_fallback_reader_agrees_with_the_accessor_path(self):
        """`extract_version` without a reader callable takes the disk branch.

        The accessor branch and the disk branch enforce the same ceiling from
        two different module globals and catch two different exception sets, so
        agreement is a claim, not a restatement.
        """
        for label in VERSION_SOURCE_BREAKAGES:
            with self.subTest(label):
                self.assertIsNone(self.observed[label]["broken_disk_version"])


class DiskVersionReaderTests(unittest.TestCase):
    """OBL-HASHING-115: `_read_version_file_bytes` swallows exactly reader failures."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.payload = b'{"version": "1.2.3"}\n'
        self.readable = self.home / "version.json"
        self.readable.write_bytes(self.payload)

    def test_a_readable_payload_comes_back_verbatim(self):
        """The premise: the reader returns bytes when there is nothing wrong."""
        self.assertEqual(
            versions._read_version_file_bytes(self.readable, "version.json"),
            self.payload,
        )

    def test_an_absent_path_returns_none_rather_than_raising(self):
        self.assertIsNone(
            versions._read_version_file_bytes(
                self.home / "missing.json", "missing.json"
            )
        )

    def test_a_directory_returns_none_rather_than_raising(self):
        directory = self.home / "nested"
        directory.mkdir()
        self.assertIsNone(versions._read_version_file_bytes(directory, "nested"))

    def test_a_payload_over_the_ceiling_returns_none(self):
        with mock.patch.object(versions, "MAX_VERSION_FILE_BYTES", 4):
            self.assertIsNone(
                versions._read_version_file_bytes(self.readable, "version.json")
            )


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-012 - the custom-provider trust boundary
# ---------------------------------------------------------------------------

#: A provider module whose import is observable from outside the interpreter.
#: `sys.modules` alone would be enough for one call, but a marker file survives
#: eviction and makes "was never imported" an assertion about the filesystem
#: rather than about interpreter bookkeeping.
POISON_SOURCE = '''\
"""A provider module that records the fact that importing it ran code."""

from pathlib import Path

Path(__file__).with_suffix(".imported").write_text("imported", encoding="utf-8")


class Poison:
    name = "custom.chunk32_poison"
    version = "1"

    def resolve(self, ctx):
        raise AssertionError("the poisoned provider must never resolve")
'''

POISON_MODULE = "bv_chunk32_poison"


class _ImportableModules:
    """A temporary directory of provider modules placed on ``sys.path``."""

    def __init__(self, sources: Dict[str, str]) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.names = tuple(sources)
        for name, body in sources.items():
            (self.home / f"{name}.py").write_text(body, encoding="utf-8")
        sys.path.insert(0, str(self.home))

    def forget(self) -> None:
        """Evict every module so the next validate_config really imports it."""
        for name in self.names:
            sys.modules.pop(name, None)

    def close(self) -> None:
        self.forget()
        try:
            sys.path.remove(str(self.home))
        except ValueError:  # pragma: no cover - another cleanup won the race
            pass
        shutil.rmtree(self.home, ignore_errors=True)


class CustomProviderTrustBoundaryTests(unittest.TestCase):
    """OBL-PROVIDERS-012: repository config never authorizes a Python import."""

    modules: _ImportableModules
    scene: Scenario

    @classmethod
    def setUpClass(cls) -> None:
        cls.modules = _ImportableModules({POISON_MODULE: POISON_SOURCE})
        cls.scene = Scenario()
        cls.scene.component("svc", path="svc", provider="path-hash", boundary=["*.py"])
        cls.scene.file("svc/main.py", "x = 1\n")
        cls.scene.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()
        cls.modules.close()

    def setUp(self):
        self.marker = self.modules.home / f"{POISON_MODULE}.imported"
        self.marker.unlink(missing_ok=True)
        self.modules.forget()
        self.probes: List[str] = []

    def tearDown(self):
        self.marker.unlink(missing_ok=True)
        self.modules.forget()

    def _config(self, extra: Optional[dict] = None) -> dict:
        config = {
            "project": "trust",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "path-hash", "paths": ["*.py"]},
                }
            },
            "providers": [{"module": POISON_MODULE, "class": "Poison"}],
        }
        if extra:
            config.update(extra)
        return config

    def _spy(self, name: str, real):
        def wrapper(*args, **kwargs):
            self.probes.append(name)
            return real(*args, **kwargs)

        return wrapper

    def _validate(self, config: dict, **kwargs) -> List[str]:
        with mock.patch.object(
            _config,
            "validate_provider_environment",
            self._spy("environment", _config.validate_provider_environment),
        ), mock.patch.object(
            _config,
            "validate_provider_config",
            self._spy("config", _config.validate_provider_config),
        ):
            return validate_config(
                config, self.scene.root, source="working-tree", **kwargs
            )

    def test_a_declared_module_is_imported_when_the_caller_authorizes_it(self):
        """The premise: the import this file keeps denying is otherwise real."""
        self._validate(self._config(), allow_custom_providers=True)
        self.assertTrue(self.marker.exists(), "the module was never imported at all")
        self.assertIn(POISON_MODULE, sys.modules)

    def test_the_environment_and_config_probes_run_under_runtime_validation(self):
        """The premise for the two probe absences asserted below."""
        self._validate(self._config(), allow_custom_providers=False)
        self.assertEqual(sorted(set(self.probes)), ["config", "environment"])

    def test_no_declared_module_is_imported_without_caller_authorization(self):
        self._validate(self._config(), allow_custom_providers=False)
        self.assertFalse(self.marker.exists())
        self.assertNotIn(POISON_MODULE, sys.modules)

    def test_no_declared_module_is_imported_when_runtime_validation_is_off(self):
        """The mode historical-config review uses on an untrusted endpoint."""
        self._validate(
            self._config(), allow_custom_providers=True, validate_provider_runtime=False
        )
        self.assertFalse(self.marker.exists())
        self.assertNotIn(POISON_MODULE, sys.modules)

    def test_no_host_dependency_probe_runs_when_runtime_validation_is_off(self):
        self._validate(
            self._config(), allow_custom_providers=True, validate_provider_runtime=False
        )
        self.assertEqual(self.probes, [])

    def test_declarations_are_still_validated_when_runtime_validation_is_off(self):
        """Disabling the runtime gate must not disable validation itself."""
        broken = self._config()
        broken["components"]["svc"]["path"] = "../escape"
        errors = self._validate(broken, validate_provider_runtime=False)
        self.assertTrue(errors, "a path escape passed unreported")

    def test_a_top_level_allow_custom_providers_key_is_rejected(self):
        errors = self._validate(
            self._config({"allow_custom_providers": True}),
            allow_custom_providers=False,
        )
        self.assertIn(
            "Top-level 'allow_custom_providers' is not supported: repository config "
            "cannot authorize Python imports; pass --allow-custom-providers or set "
            "BOUNDVER_ALLOW_CUSTOM_PROVIDERS=1 in trusted automation",
            errors,
        )

    def test_a_top_level_allow_custom_providers_key_authorizes_nothing(self):
        self._validate(
            self._config({"allow_custom_providers": True}),
            allow_custom_providers=False,
        )
        self.assertFalse(self.marker.exists())
        self.assertNotIn(POISON_MODULE, sys.modules)

    def test_the_config_key_is_ignored_by_the_authorization_resolver(self):
        arguments = argparse.Namespace()
        self.assertFalse(
            core._resolve_allow_custom(arguments, {"allow_custom_providers": True})
        )
        arguments = argparse.Namespace(allow_custom_providers=True)
        self.assertTrue(core._resolve_allow_custom(arguments, {}))


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-015 - facet policy precedence
# ---------------------------------------------------------------------------

#: One declaration shape that can produce all four facets, so every facet a
#: policy leaves out is a policy decision rather than an inability.
def _capable_component(path: str, verify_facets: Optional[List[str]] = None) -> dict:
    entry: Dict[str, Any] = {
        "path": path,
        "boundary": {"provider": "path-hash", "paths": ["api/*.json"]},
        "behavior": {"paths": ["api/*.json", "impl/*.py"]},
        "version_source": {"file": "version.json", "field": "version"},
    }
    if verify_facets is not None:
        entry["verify_facets"] = list(verify_facets)
    return entry


#: The three components every precedence config declares: one with its own
#: override, one with none, and one whose override names two facets.
PRECEDENCE_COMPONENTS = {
    "alpha": ["behavior"],
    "beta": None,
    "gamma": ["exact", "boundary"],
}

#: What must gate, per config and per explicit selection. `with-defaults` puts
#: `defaults.verify_facets` between the component override and the availability
#: fallback; `no-defaults` removes the key so the fallback is reached. The two
#: cannot coexist, which is why the obligation's "single config" is impossible.
PRECEDENCE_EXPECTATIONS = {
    ("with-defaults", None): {
        "alpha": {"behavior"},
        "beta": {"boundary"},
        "gamma": {"exact", "boundary"},
    },
    ("with-defaults", ("compat",)): {
        "alpha": {"compat"},
        "beta": {"compat"},
        "gamma": {"compat"},
    },
    ("no-defaults", None): {
        "alpha": {"behavior"},
        "beta": set(FACETS),
        "gamma": {"exact", "boundary"},
    },
    ("no-defaults", ("behavior",)): {
        "alpha": {"behavior"},
        "beta": {"behavior"},
        "gamma": {"behavior"},
    },
}


class FacetPrecedenceTests(unittest.TestCase):
    """OBL-PROVIDERS-015: four levels, resolved in order, by both implementations."""

    scene: Scenario
    snapshot: GitSourceSnapshot

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = Scenario()
        for name in PRECEDENCE_COMPONENTS:
            cls.scene.file(f"{name}/api/v1.json", '{"a": 1}\n')
            cls.scene.file(f"{name}/impl/core.py", "x = 1\n")
            cls.scene.file(f"{name}/version.json", '{"version": "1.2.3"}\n')
        cls.scene.config = cls.config_for("with-defaults")
        cls.scene.commit()
        cls.snapshot = _capture_git_source_snapshot(cls.scene.root, "head")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    @staticmethod
    def config_for(shape: str) -> dict:
        config: Dict[str, Any] = {
            "project": "precedence",
            "components": {
                name: _capable_component(name, override)
                for name, override in PRECEDENCE_COMPONENTS.items()
            },
        }
        if shape == "with-defaults":
            config["defaults"] = {"verify_facets": ["boundary"]}
        return config

    def _lock_with_every_facet_moved(self, config: dict) -> dict:
        lock = copy.deepcopy(
            generate_lockfile(
                config, self.scene.root, source="head", snapshot=self.snapshot
            )
        )
        for entry in lock["components"].values():
            for facet in FACETS:
                entry["fingerprints"][facet] = PLANTED_DIGEST
        return lock

    def _gated_facets(self, config: dict, explicit) -> Dict[str, set]:
        lock = self._lock_with_every_facet_moved(config)
        observations: List[str] = []
        issues = verify_lockfile(
            config,
            lock,
            self.scene.root,
            source="head",
            snapshot=self.snapshot,
            facets=list(explicit) if explicit is not None else None,
            observations=observations,
        )
        gated = {name: set() for name in config["components"]}
        for issue in issues:
            facet = _issue_facet(issue)
            if facet is None or not issue.startswith("MISMATCH "):
                continue
            subject = issue.split(" ", 1)[1].rsplit(f".{facet}:", 1)[0]
            gated[subject].add(facet)
        return gated

    def test_every_component_can_produce_all_four_facets(self):
        """The premise: an absent gate below is policy, not an unavailable digest."""
        for shape in ("with-defaults", "no-defaults"):
            config = self.config_for(shape)
            lock = generate_lockfile(
                config, self.scene.root, source="head", snapshot=self.snapshot
            )
            for name, entry in lock["components"].items():
                with self.subTest(shape=shape, component=name):
                    for facet in FACETS:
                        self.assertIsNotNone(entry["fingerprints"][facet], facet)

    def test_the_recorded_policy_resolves_the_declared_precedence(self):
        for (shape, explicit), expected in PRECEDENCE_EXPECTATIONS.items():
            with self.subTest(shape=shape, explicit=explicit):
                payload = facet_policy_payload(
                    self.config_for(shape),
                    list(explicit) if explicit is not None else None,
                )
                recorded = {
                    name: set(facets)
                    for name, facets in payload["components"].items()
                }
                self.assertEqual(recorded, expected)

    def test_verification_gates_exactly_the_facets_the_precedence_selects(self):
        """The second, independent implementation of the same rule."""
        for (shape, explicit), expected in PRECEDENCE_EXPECTATIONS.items():
            with self.subTest(shape=shape, explicit=explicit):
                self.assertEqual(self._gated_facets(self.config_for(shape), explicit), expected)

    def test_an_ungated_mismatch_is_reported_as_a_non_gating_observation(self):
        """The premise for reading an absent gate off the issue list."""
        config = self.config_for("with-defaults")
        lock = self._lock_with_every_facet_moved(config)
        observations: List[str] = []
        verify_lockfile(
            config,
            lock,
            self.scene.root,
            source="head",
            snapshot=self.snapshot,
            observations=observations,
        )
        self.assertIn(
            "alpha.compat",
            " ".join(observations),
            "the compat mismatch alpha excludes was not observed at all",
        )

    def test_the_defaults_key_and_the_availability_fallback_cannot_coexist(self):
        """The finding behind the obligation's impossible 'single config'.

        `beta` declares no override in either shape. With the defaults key it
        gates the defaults set; without the key it gates everything it can
        produce. One config cannot show both, because the key that selects the
        third level is what stops the fourth from being consulted.
        """
        with_defaults = PRECEDENCE_EXPECTATIONS[("with-defaults", None)]["beta"]
        without_defaults = PRECEDENCE_EXPECTATIONS[("no-defaults", None)]["beta"]
        self.assertNotEqual(with_defaults, without_defaults)
        self.assertEqual(
            without_defaults,
            _available_component_facets(_capable_component("beta")),
        )


class AvailableFacetDerivationTests(unittest.TestCase):
    """OBL-PROVIDERS-015: the four availability rules, over the whole registry."""

    def test_exact_is_available_for_every_registered_provider(self):
        for name in sorted(create_registry()):
            with self.subTest(name):
                declaration = {"boundary": {"provider": name, "paths": []}}
                self.assertIn("exact", _available_component_facets(declaration))

    def test_boundary_availability_follows_the_provider_rule_for_every_member(self):
        """Enumerated from the registry, so a new provider is placed by the rule."""
        for name in sorted(create_registry()):
            for paths in ([], ["api/*.json"]):
                with self.subTest(provider=name, paths=paths):
                    declaration = {"boundary": {"provider": name, "paths": list(paths)}}
                    expected = name not in {"leaf", "implicit"} or (
                        name == "implicit" and bool(paths)
                    )
                    self.assertEqual(
                        "boundary" in _available_component_facets(declaration),
                        expected,
                    )

    def test_an_implicit_provider_with_paths_does_publish_a_boundary(self):
        """Named separately because it is the rule's one non-obvious member."""
        self.assertIn(
            "boundary",
            _available_component_facets(
                {"boundary": {"provider": "implicit", "paths": ["api/*.json"]}}
            ),
        )
        self.assertNotIn(
            "boundary",
            _available_component_facets(
                {"boundary": {"provider": "implicit", "paths": []}}
            ),
        )

    def test_behavior_is_available_exactly_when_behavior_paths_is_non_empty(self):
        cases = {
            "absent": ({}, False),
            "not-a-mapping": ({"behavior": ["api/*.json"]}, False),
            "empty": ({"behavior": {"paths": []}}, False),
            "not-a-list": ({"behavior": {"paths": "api/*.json"}}, False),
            "populated": ({"behavior": {"paths": ["impl/*.py"]}}, True),
        }
        for label, (declaration, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(
                    "behavior" in _available_component_facets(declaration), expected
                )

    def test_compat_is_available_exactly_when_version_source_is_a_mapping(self):
        cases = {
            "absent": ({}, False),
            "null": ({"version_source": None}, False),
            "a-string": ({"version_source": "version.json"}, False),
            "a-list": ({"version_source": ["version.json"]}, False),
            "a-mapping": (
                {"version_source": {"file": "version.json", "field": "version"}},
                True,
            ),
            "an-empty-mapping": ({"version_source": {}}, True),
        }
        for label, (declaration, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(
                    "compat" in _available_component_facets(declaration), expected
                )


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-017 - provider identity binding for structural evidence
# ---------------------------------------------------------------------------

#: Two modules registering the same provider name from different classes, and
#: one advertising an interface this host does not implement. Together they
#: reach the two guards that no built-in provider can exercise, because every
#: built-in resolves to the same class at both endpoints.
STRUCTURAL_MODULES = {
    "bv_chunk32_alpha": '''\
"""A structural provider that records exactly how it was invoked."""

INTERFACE = "boundver-structural-diff/v1"


class Alpha:
    name = "custom.chunk32_shared"
    version = "7"
    structural_diff_interface = INTERFACE

    instances = []
    calls = []

    def __init__(self):
        Alpha.instances.append(self)

    def resolve(self, ctx):
        raise AssertionError("resolve is not part of structural review")

    def structural_diff(self, before_ctx, after_ctx, budget):
        Alpha.calls.append((self, before_ctx.boundary_cfg, after_ctx.boundary_cfg))
        raise ValueError("alpha declined to explain")
''',
    "bv_chunk32_beta": '''\
"""A different class publishing the same provider name and version."""

INTERFACE = "boundver-structural-diff/v1"


class Beta:
    name = "custom.chunk32_shared"
    version = "7"
    structural_diff_interface = INTERFACE

    def resolve(self, ctx):
        raise AssertionError("resolve is not part of structural review")

    def structural_diff(self, before_ctx, after_ctx, budget):
        raise AssertionError("a mismatched implementation must never be invoked")
''',
    "bv_chunk32_oldiface": '''\
"""The same class at both endpoints, advertising an unsupported interface."""


class OldIface:
    name = "custom.chunk32_shared"
    version = "7"
    structural_diff_interface = "vendor-structural-diff/v0"

    def resolve(self, ctx):
        raise AssertionError("resolve is not part of structural review")

    def structural_diff(self, before_ctx, after_ctx, budget):
        raise AssertionError("an unsupported interface must never be invoked")
''',
}

SHARED_PROVIDER = "custom.chunk32_shared"
BASE_OID = "a" * 40
TARGET_OID = "b" * 40


class _ReviewBudget:
    """The row reservation `structural_boundary_changes` calls back into."""

    def __init__(self) -> None:
        self.rows: List[tuple] = []

    def reserve_row(self, *values: object, overhead: int = 128) -> None:
        self.rows.append(values)


def _endpoint_snapshot(oid: str) -> GitSourceSnapshot:
    return GitSourceSnapshot(source="head", tree_oid=oid, entries={}, head_oid=oid)


def _lock_entry(provider: str, version: str) -> dict:
    return {
        "path": "svc",
        "boundary_provider": provider,
        "boundary_provider_version": version,
        "fingerprints": {"boundary": "1" * 64},
    }


def _endpoint_config(
    provider: str,
    *,
    module: Optional[str] = None,
    klass: Optional[str] = None,
    selector: str = "api/*.json",
) -> dict:
    config: Dict[str, Any] = {
        "components": {
            "svc": {
                "path": "svc",
                "boundary": {"provider": provider, "paths": [selector]},
            }
        }
    }
    if module is not None:
        config["providers"] = [{"module": module, "class": klass}]
    return config


class StructuralProviderIdentityTests(unittest.TestCase):
    """OBL-PROVIDERS-017: one implementation, both contexts, or no explanation."""

    modules: _ImportableModules

    @classmethod
    def setUpClass(cls) -> None:
        cls.modules = _ImportableModules(STRUCTURAL_MODULES)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.modules.close()

    def _structural(
        self,
        base_entry: Optional[dict],
        target_entry: Optional[dict],
        base_config: dict,
        target_config: dict,
        *,
        allow_custom: bool = False,
    ) -> dict:
        budget = _ReviewBudget()
        return structural_boundary_changes(
            Path("."),
            _endpoint_snapshot(BASE_OID),
            _endpoint_snapshot(TARGET_OID),
            base_config,
            target_config,
            {"components": {"svc": base_entry} if base_entry else {}},
            {"components": {"svc": target_entry} if target_entry else {}},
            [{"name": "svc", "facets": [{"facet": "boundary"}]}],
            base_ref="base",
            target_ref="target",
            requested_base_commit=BASE_OID,
            requested_target_commit=TARGET_OID,
            allow_custom_providers=allow_custom,
            review_budget=budget,
        )

    def _cases(self) -> Dict[str, Tuple[dict, str, str]]:
        """Every unavailable reason a provider identity mismatch can produce.

        Built lazily rather than at module scope because three rows need the
        importable modules installed by ``setUpClass``.
        """
        builtin = _endpoint_config("openapi-canonical")
        shared_alpha = _endpoint_config(
            SHARED_PROVIDER, module="bv_chunk32_alpha", klass="Alpha"
        )
        shared_beta = _endpoint_config(
            SHARED_PROVIDER, module="bv_chunk32_beta", klass="Beta"
        )
        shared_old = _endpoint_config(
            SHARED_PROVIDER, module="bv_chunk32_oldiface", klass="OldIface"
        )
        absent = _endpoint_config(
            SHARED_PROVIDER, module="bv_chunk32_absent", klass="Missing"
        )
        return {
            "component-absent": (
                dict(
                    base_entry=None,
                    target_entry=_lock_entry("openapi-canonical", "4"),
                    base_config=builtin,
                    target_config=builtin,
                ),
                "component-absent",
                "Structural comparison requires the component at both endpoints",
            ),
            "provider-changed": (
                dict(
                    base_entry=_lock_entry("leaf", "1"),
                    target_entry=_lock_entry("openapi-canonical", "4"),
                    base_config=builtin,
                    target_config=builtin,
                ),
                "provider-changed",
                "Structural comparison requires the same provider at both endpoints",
            ),
            "provider-version-changed": (
                dict(
                    base_entry=_lock_entry("openapi-canonical", "3"),
                    target_entry=_lock_entry("openapi-canonical", "4"),
                    base_config=builtin,
                    target_config=builtin,
                ),
                "provider-version-changed",
                "Structural comparison requires the same provider version at "
                "both endpoints",
            ),
            "provider-unsupported": (
                dict(
                    base_entry=_lock_entry("path-hash", "3"),
                    target_entry=_lock_entry("path-hash", "3"),
                    base_config=_endpoint_config("path-hash"),
                    target_config=_endpoint_config("path-hash"),
                ),
                "provider-unsupported",
                "Provider 'path-hash' does not expose bounded structural diff output",
            ),
            "provider-implementation-changed": (
                dict(
                    base_entry=_lock_entry(SHARED_PROVIDER, "7"),
                    target_entry=_lock_entry(SHARED_PROVIDER, "7"),
                    base_config=shared_alpha,
                    target_config=shared_beta,
                    allow_custom=True,
                ),
                "provider-implementation-changed",
                "Structural comparison requires the same provider implementation "
                "at both endpoints",
            ),
            "provider-interface-unsupported": (
                dict(
                    base_entry=_lock_entry(SHARED_PROVIDER, "7"),
                    target_entry=_lock_entry(SHARED_PROVIDER, "7"),
                    base_config=shared_old,
                    target_config=shared_old,
                    allow_custom=True,
                ),
                "provider-interface-unsupported",
                "Provider structural-diff interface is not supported by this host "
                f"(expected {STRUCTURAL_DIFF_INTERFACE})",
            ),
            "provider-unavailable": (
                dict(
                    base_entry=_lock_entry(SHARED_PROVIDER, "7"),
                    target_entry=_lock_entry(SHARED_PROVIDER, "7"),
                    base_config=absent,
                    target_config=shared_alpha,
                    allow_custom=True,
                ),
                "provider-unavailable",
                "Failed to import provider module 'bv_chunk32_absent'",
            ),
        }

    def test_a_matched_provider_pair_reaches_the_comparison(self):
        """The premise: the guards below deny something otherwise reachable."""
        module = importlib.import_module("bv_chunk32_alpha")
        module.Alpha.calls.clear()
        module.Alpha.instances.clear()
        changes = self._structural(
            _lock_entry(SHARED_PROVIDER, "7"),
            _lock_entry(SHARED_PROVIDER, "7"),
            _endpoint_config(SHARED_PROVIDER, module="bv_chunk32_alpha", klass="Alpha"),
            _endpoint_config(SHARED_PROVIDER, module="bv_chunk32_alpha", klass="Alpha"),
            allow_custom=True,
        )
        self.assertEqual(len(module.Alpha.calls), 1)
        self.assertEqual(changes["reports"][0]["detail"], "alpha declined to explain")

    def test_each_provider_identity_mismatch_yields_its_own_unavailable_reason(self):
        for label, (arguments, reason, detail) in self._cases().items():
            with self.subTest(label):
                changes = self._structural(**arguments)
                report = changes["reports"][0]
                self.assertEqual(report["reason"], reason)
                self.assertIn(detail, report["detail"])
                self.assertEqual(report["status"], "unavailable")
                self.assertEqual(report["documents"], [])
                self.assertEqual(
                    report["summary"], {"added": 0, "removed": 0, "changed": 0}
                )
                self.assertFalse(report["complete"])
                self.assertFalse(changes["complete"])

    def test_every_unavailable_report_still_declares_the_host_interface(self):
        for label, (arguments, _reason, _detail) in self._cases().items():
            with self.subTest(label):
                changes = self._structural(**arguments)
                self.assertEqual(changes["interface"], STRUCTURAL_DIFF_INTERFACE)
                self.assertEqual(
                    changes["reports"][0]["interface"], STRUCTURAL_DIFF_INTERFACE
                )

    def test_the_comparison_binds_the_target_provider_to_both_contexts(self):
        """The base commit's sources are parsed by the target's implementation.

        Both endpoints load the same class, so the two registries hold two
        instances; the base registry is populated first. The recorded call
        proves the method came from the second - the target's - instance, and
        that the two contexts arrived in endpoint order rather than crossed.
        """
        module = importlib.import_module("bv_chunk32_alpha")
        module.Alpha.calls.clear()
        module.Alpha.instances.clear()
        base_config = _endpoint_config(
            SHARED_PROVIDER,
            module="bv_chunk32_alpha",
            klass="Alpha",
            selector="base-only.json",
        )
        target_config = _endpoint_config(
            SHARED_PROVIDER,
            module="bv_chunk32_alpha",
            klass="Alpha",
            selector="target-only.json",
        )
        self._structural(
            _lock_entry(SHARED_PROVIDER, "7"),
            _lock_entry(SHARED_PROVIDER, "7"),
            base_config,
            target_config,
            allow_custom=True,
        )
        self.assertEqual(len(module.Alpha.instances), 2)
        invoked, before_cfg, after_cfg = module.Alpha.calls[0]
        self.assertIs(invoked, module.Alpha.instances[1])
        self.assertEqual(before_cfg["paths"], ["base-only.json"])
        self.assertEqual(after_cfg["paths"], ["target-only.json"])

    def test_provider_method_returns_the_target_binding(self):
        """The same claim one layer down, without the review scaffolding."""
        module = importlib.import_module("bv_chunk32_alpha")
        base_registry = {SHARED_PROVIDER: module.Alpha()}
        target_registry = {SHARED_PROVIDER: module.Alpha()}
        method, reason, detail = _provider_method(
            SHARED_PROVIDER, base_registry, target_registry
        )
        self.assertIsNone(reason)
        self.assertIsNone(detail)
        self.assertIs(method.__self__, target_registry[SHARED_PROVIDER])
        self.assertIsNot(method.__self__, base_registry[SHARED_PROVIDER])


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-018 - lock serialisation of provider metadata
# ---------------------------------------------------------------------------

class LockSerialisationTests(unittest.TestCase):
    """OBL-PROVIDERS-018: what `dump_lockfile` does with unrepresentable numbers."""

    scene: Scenario
    healthy_lock: dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = Scenario()
        cls.scene.component(
            "svc", path="svc", provider="path-hash", boundary=["api/*.json"]
        )
        cls.scene.file("svc/api/v1.json", '{"a": 1}\n')
        cls.scene.commit()
        cls.healthy_lock = generate_lockfile(
            cls.scene.config, cls.scene.root, source="head"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def _poisoned(self, value: object) -> dict:
        lock = copy.deepcopy(self.healthy_lock)
        lock["components"]["svc"]["boundary_metadata"] = {"metric": value}
        return lock

    def test_a_healthy_lock_round_trips_through_its_own_reader(self):
        """The premise: dump and reload agree when nothing is wrong."""
        reloaded = parse_lockfile_text(dump_lockfile(self.healthy_lock))
        self.assertEqual(reloaded["components"], self.healthy_lock["components"])

    def test_dump_lockfile_does_refuse_a_document_it_cannot_persist(self):
        """The premise for the refusals asserted below: refusal is reachable."""
        with mock.patch.object(_lockfile, "MAX_LOCKFILE_BYTES", 32):
            with self.assertRaises(LockfileError) as caught:
                dump_lockfile(self.healthy_lock)
        self.assertEqual(
            str(caught.exception),
            "Lockfile output exceeds the 32-byte storage limit; no file was "
            "written. Reduce generated component or provider metadata before "
            "retrying.",
        )

    def test_dump_lockfile_refuses_a_non_finite_float_in_boundary_metadata(self):
        """The writer refuses a value its strict reader cannot reload."""
        with self.assertRaises(LockfileError):
            dump_lockfile(self._poisoned(float("nan")))

    def test_every_non_finite_float_is_refused_as_a_lockfile_error(self):
        for label, value in (
            ("nan", float("nan")),
            ("inf", float("inf")),
            ("-inf", float("-inf")),
        ):
            with self.subTest(label):
                with self.assertRaises(LockfileError) as caught:
                    dump_lockfile(self._poisoned(value))
                self.assertIn("non-finite number", str(caught.exception))
                self.assertIn("no file was written", str(caught.exception))

    def test_an_out_of_range_integer_is_refused_as_a_lockfile_error(self):
        with self.assertRaises(LockfileError) as caught:
            dump_lockfile(self._poisoned(10**4400))
        self.assertIn("oversized integer", str(caught.exception))
        self.assertIn("no file was written", str(caught.exception))

    def test_a_nested_non_finite_float_is_refused_too(self):
        lock = copy.deepcopy(self.healthy_lock)
        lock["components"]["svc"]["boundary_metadata"] = {
            "rows": [{"metric": float("inf")}]
        }
        with self.assertRaises(LockfileError):
            dump_lockfile(lock)


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-021 - repository strings must not move the exit code
# ---------------------------------------------------------------------------

def _safety_prefixes() -> Tuple[str, ...]:
    """Read the classifier's prefix list out of its own source.

    Listing the twelve prefixes here by hand would leave a thirteenth
    untested. They are a local tuple inside `_drift_exit_code`, so the source
    is parsed rather than imported; a shape this cannot read fails the test
    that calls it rather than silently fuzzing nothing.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(core._drift_exit_code)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "safety_prefixes"
            for target in node.targets
        ):
            continue
        if not isinstance(node.value, ast.Tuple):
            break
        values = [
            element.value
            for element in node.value.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        if len(values) == len(node.value.elts):
            return tuple(values)
    raise AssertionError("could not read safety_prefixes out of _drift_exit_code")


SAFETY_PREFIXES = _safety_prefixes()

#: Every token the severity classifier reacts to: the prefixes that promote an
#: issue to a usage error, and the facet suffixes the regex reads. A repository
#: string carrying any of them must still change nothing.
CLASSIFIER_TOKENS = (
    SAFETY_PREFIXES
    + tuple(f".{facet}:" for facet in FACETS)
    + ("MISMATCH", "SLICE MISMATCH", "AFFECTED CONSUMERS")
)

#: The exit code each true drift state must produce, whatever the repository
#: called its components. Taken from core's own constants rather than from
#: `_drift_exit_code`, which is the code under test.
EXIT_FOR_FACET = {
    "exact": core.EXIT_DRIFT,
    "behavior": core.EXIT_BEHAVIOR,
    "boundary": core.EXIT_BOUNDARY,
    "compat": core.EXIT_COMPAT,
}

SPOOF_PROFILE = settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
)


@st.composite
def hostile_labels(draw) -> str:
    """A repository-controlled string built around one classifier token.

    The padding alphabet excludes the comma, which component and consumer
    identifiers may not contain at all, so every draw is a label a repository
    could really declare rather than one validation would reject first.
    """
    token = draw(st.sampled_from(CLASSIFIER_TOKENS))
    head = draw(st.text(alphabet="ab .:-", max_size=4))
    tail = draw(st.text(alphabet="ab .:-", max_size=4))
    value = f"{head}{token}{tail}".strip()
    assume(value)
    return value


class IssueSeverityClassificationTests(unittest.TestCase):
    """OBL-PROVIDERS-021: severity comes from the drift state, not from the text."""

    scene: Scenario
    snapshot: GitSourceSnapshot

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = Scenario()
        cls.scene.file("svc/api/v1.json", '{"a": 1}\n')
        cls.scene.file("svc/impl/core.py", "x = 1\n")
        cls.scene.file("svc/version.json", '{"version": "1.2.3"}\n')
        cls.scene.config = cls.config_for("svc", "team-a", "public")
        cls.scene.commit()
        cls.snapshot = _capture_git_source_snapshot(cls.scene.root, "head")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    @staticmethod
    def config_for(name: str, label: str, slice_name: str) -> dict:
        return {
            "project": "severity",
            "components": {
                name: {
                    "path": "svc",
                    "boundary": {"provider": "path-hash", "paths": ["api/*.json"]},
                    "behavior": {"paths": ["api/*.json", "impl/*.py"]},
                    "version_source": {"file": "version.json", "field": "version"},
                    "external_consumers": [label],
                }
            },
            "slices": {slice_name: {"mode": "boundary", "components": [name]}},
        }

    def _verify_with_planted_drift(
        self, name: str, label: str, slice_name: str, facet: str
    ) -> List[str]:
        """Move exactly one facet in the lock and report what verify said."""
        config = self.config_for(name, label, slice_name)
        lock = copy.deepcopy(
            generate_lockfile(
                config, self.scene.root, source="head", snapshot=self.snapshot
            )
        )
        lock["components"][name]["fingerprints"][facet] = PLANTED_DIGEST
        if facet == "boundary":
            lock["slices"][slice_name]["fingerprint"] = PLANTED_DIGEST
        return verify_lockfile(
            config,
            lock,
            self.scene.root,
            source="head",
            snapshot=self.snapshot,
            observations=[],
        )

    def test_the_classifier_reacts_to_every_token_this_file_fuzzes(self):
        """The premise: a passing property is not a property about nothing."""
        for prefix in SAFETY_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    core._drift_exit_code([f"{prefix} something drifted"]),
                    core.EXIT_USAGE,
                )
        for facet, expected in EXIT_FOR_FACET.items():
            with self.subTest(facet=facet):
                self.assertEqual(
                    core._drift_exit_code([f"MISMATCH svc.{facet}: a b"]), expected
                )

    def test_a_benign_repository_produces_the_oracle_exit_code(self):
        """The premise: the fixture really creates the drift state it claims."""
        for facet, expected in EXIT_FOR_FACET.items():
            with self.subTest(facet):
                issues = self._verify_with_planted_drift(
                    "svc", "team-a", "public", facet
                )
                self.assertTrue(issues)
                self.assertIn(f"MISMATCH svc.{facet}:", " ".join(issues))
                self.assertEqual(core._drift_exit_code(issues), expected)

    def test_hostile_names_really_reach_the_rendered_issue_text(self):
        """The premise without which every assertion below is about nothing."""
        issues = self._verify_with_planted_drift(
            "LOCKFILE malformed",
            "UNAVAILABLE FACET x.compat:",
            "Config invalid|slice",
            "boundary",
        )
        joined = " ".join(issues)
        self.assertIn("MISMATCH LOCKFILE malformed.boundary:", joined)
        self.assertIn("UNAVAILABLE FACET x.compat:", joined)
        self.assertIn("SLICE MISMATCH Config invalid|slice.boundary:", joined)

    def test_a_config_full_of_classifier_tokens_is_accepted_as_valid(self):
        """The attack is real only if such a repository validates."""
        errors = validate_config(
            self.config_for(
                "LOCKFILE malformed",
                "UNAVAILABLE FACET z.compat:",
                "CURRENT DIGEST ERROR.compat:",
            ),
            self.scene.root,
            source="head",
            snapshot=self.snapshot,
        )
        self.assertEqual(errors, [])

    @SPOOF_PROFILE
    @given(
        name=hostile_labels(),
        label=hostile_labels(),
        slice_name=hostile_labels(),
        facet=st.sampled_from(FACETS),
    )
    # The greedy `.+` in `_FACET_ISSUE_RE` is what stops a component name from
    # winning the facet match against the real suffix; replacing it with `.+?`
    # promotes this first example from exit 1 to exit 5. The rest pin one draw
    # per attacked surface so the property never relies on the search finding
    # them.
    @example(name="x.compat:y", label="team-a", slice_name="public", facet="exact")
    @example(
        name="LOCKFILE malformed",
        label="UNAVAILABLE FACET q",
        slice_name="Config invalid",
        facet="boundary",
    )
    @example(
        name="a.boundary:b",
        label="c.compat:d",
        slice_name="e.compat:f",
        facet="exact",
    )
    @example(
        name="CURRENT DIGEST ERROR",
        label="DIAGNOSTICS TRUNCATED",
        slice_name="LOCKED DIGEST ERROR",
        facet="behavior",
    )
    def test_no_repository_controlled_string_changes_the_verification_severity(
        self, name: str, label: str, slice_name: str, facet: str
    ):
        issues = self._verify_with_planted_drift(name, label, slice_name, facet)
        self.assertIn(
            name,
            " ".join(issues),
            "the drawn component name never reached the rendered text",
        )
        self.assertEqual(core._drift_exit_code(issues), EXIT_FOR_FACET[facet])

    def test_a_declared_path_carrying_a_prefix_changes_nothing(self):
        """A component path is repository-controlled but never rendered here.

        It reaches issue text only through a digest-error message, which is a
        usage error on its own terms. On the ordinary drift path the path does
        not appear at all, which is pinned so a future message that starts
        interpolating it cannot do so unnoticed.
        """
        with Scenario() as scene:
            scene.file("LOCKFILE malformed/api/v1.json", '{"a": 1}\n')
            scene.config = {
                "project": "severity",
                "components": {
                    "svc": {
                        "path": "LOCKFILE malformed",
                        "boundary": {
                            "provider": "path-hash",
                            "paths": ["api/*.json"],
                        },
                    }
                },
            }
            scene.commit()
            snapshot = _capture_git_source_snapshot(scene.root, "head")
            lock = copy.deepcopy(
                generate_lockfile(
                    scene.config, scene.root, source="head", snapshot=snapshot
                )
            )
            lock["components"]["svc"]["fingerprints"]["exact"] = PLANTED_DIGEST
            issues = verify_lockfile(
                scene.config, lock, scene.root, source="head", snapshot=snapshot
            )
            self.assertEqual(core._drift_exit_code(issues), core.EXIT_DRIFT)
            self.assertNotIn("LOCKFILE malformed", " ".join(issues))

    def test_a_version_string_carrying_a_prefix_changes_nothing(self):
        """SemVer forbids the colon, so a version can carry a word but no token."""
        with Scenario() as scene:
            scene.file("svc/api/v1.json", '{"a": 1}\n')
            scene.file(
                "svc/version.json",
                '{"version": "1.2.3-UNAVAILABLE.FACET.compat.LOCKFILE"}\n',
            )
            scene.config = {
                "project": "severity",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "path-hash",
                            "paths": ["api/*.json"],
                        },
                        "version_source": {
                            "file": "version.json",
                            "field": "version",
                        },
                    }
                },
            }
            scene.commit()
            snapshot = _capture_git_source_snapshot(scene.root, "head")
            lock = copy.deepcopy(
                generate_lockfile(
                    scene.config, scene.root, source="head", snapshot=snapshot
                )
            )
            self.assertEqual(
                lock["components"]["svc"]["version"],
                "1.2.3-UNAVAILABLE.FACET.compat.LOCKFILE",
            )
            lock["components"]["svc"]["fingerprints"]["exact"] = PLANTED_DIGEST
            issues = verify_lockfile(
                scene.config, lock, scene.root, source="head", snapshot=snapshot
            )
            self.assertEqual(core._drift_exit_code(issues), core.EXIT_DRIFT)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
