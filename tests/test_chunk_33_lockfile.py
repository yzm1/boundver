"""Six promises about the lock: what it refuses, what it cleans up, what it names.

Four of the obligations gathered here share one uncomfortable property. They
are all statements about a path the happy case never takes, so the danger is
not that the assertion is wrong but that the code under it was never reached.
A config carrying a lone surrogate has to be refused *before* a digest encodes
it, which means the test has to prove that the same document without the
surrogate loads. A failed atomic write has to leave no sidecar behind, which is
trivially true if the writer never created one, so the test has to observe a
real sidecar in flight before it is entitled to assert its absence. A partial
generation has to refuse a stale unselected component, which is indistinguishable
from a partial generation that silently rewrote it unless the diagnostic is
pinned word for word. Every absence asserted below is therefore preceded by a
test that produced the thing being denied.

Two of the six were already answered by the code and only needed to be shown.
Config parsing rejects a lone surrogate at every position tried - object key or
string value, component name, slice name, consumer label, provider name,
declared path or project name - because the loader itself walks the value tree,
not only the schema traversal; the register's note that the value scan is never
called on a config was checked against `_config.load_config_file` and is stale.
And `violation_identity` survives every adversarial name the message grammar
invites: `x.exact`, `svc.compat: y`, `trailing.`, a name that reproduces an
entire foreign MISMATCH line. The greedy `.+` cannot overshoot because the only
text after the facet anchor is ` lockfile=<12 hex>... current=<12 hex>...`,
which contains no second `.facet:`. The property here generates names from the
shipped schema's own `componentIdentifier` pattern rather than a transcription
of it, so widening the grammar widens the test.

One obligation diverges. `generate_lockfile_for_components` is required to
refuse when the config declares a component the lock has no entry for. It
refuses only when that component is *unselected*, and then with the staleness
diagnostic rather than the dedicated one; when the missing component is
selected, the entry is created and the generation succeeds. The dedicated
"has no entry for" branch appears to be unreachable: the staleness loop runs
first over every configured name that is not selected, and every selected name
is written into the merged tree immediately after, so the set it computes is
always empty. That is pinned in both directions below.

The fixture work that made this possible was mostly in choosing doors. Scoped
generation is exercised through `existing_lockfile=` so an adversarial lock can
be handed in without writing and committing one; the facet policy is exercised
twice, once through `verify_lockfile` where the config-driven arms are actually
reachable and once through the real CLI where two of the three are intercepted
by config validation first, because the obligation is about exit 2 and those
are two different roads to it.

Covers OBL-PROVIDERS-022, OBL-LOCKFILE-013, OBL-LOCKFILE-015, OBL-LOCKFILE-024,
OBL-LOCKFILE-025 and OBL-LOCKFILE-026.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st

from boundver import _baseline, core
from boundver._config import load_config_file, validate_config
from boundver._hashing import sha256_hex
from boundver._lockfile import (
    FACETS as DECLARED_FACETS,
    generate_lockfile,
    generate_lockfile_for_components,
    verify_lockfile,
)
from boundver._utils import ConfigError

from tests._parity import run_cli
from tests._scenarios import Scenario

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

#: Every facet a component entry can carry, read from the lockfile module so a
#: fifth one added tomorrow is enumerated here rather than overlooked.
FACETS = tuple(sorted(DECLARED_FACETS))


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-022 - a config string that cannot be encoded to UTF-8
# ---------------------------------------------------------------------------


def _site_component_name(config: dict, text: str) -> None:
    config["components"]["sv" + text + "c"] = config["components"].pop("svc")
    config["slices"]["all"]["components"] = ["sv" + text + "c"]


def _site_slice_name(config: dict, text: str) -> None:
    config["slices"]["al" + text + "l"] = config["slices"].pop("all")


def _site_consumers_entry(config: dict, text: str) -> None:
    config["components"]["svc"]["consumers"] = ["ot" + text + "her"]


def _site_external_consumers_entry(config: dict, text: str) -> None:
    config["components"]["svc"]["external_consumers"] = ["te" + text + "am"]


def _site_provider_name(config: dict, text: str) -> None:
    config["components"]["svc"]["boundary"]["provider"] = "path-h" + text + "ash"


def _site_boundary_path(config: dict, text: str) -> None:
    config["components"]["svc"]["boundary"]["paths"] = ["api/" + text + "x.yaml"]


def _site_component_path(config: dict, text: str) -> None:
    config["components"]["svc"]["path"] = "sv" + text + "c"


def _site_project_name(config: dict, text: str) -> None:
    config["project"] = "pr" + text + "oj"


#: Every place a config document can carry a string that a later digest will
#: encode. Each entry splices *text* into an otherwise ordinary declaration;
#: the keys are the positions the obligation names.
SURROGATE_SITES = {
    "component name": _site_component_name,
    "slice name": _site_slice_name,
    "consumers entry": _site_consumers_entry,
    "external_consumers entry": _site_external_consumers_entry,
    "provider name": _site_provider_name,
    "boundary path": _site_boundary_path,
    "component path": _site_component_path,
    "project name": _site_project_name,
}

#: Sites whose marker-bearing variant is still a *valid* configuration, so the
#: in-memory `validate_config` door can be premised against a clean answer.
IN_MEMORY_SITES = ("project name", "slice name", "external_consumers entry")

#: An ASCII stand-in used to prove the site is really part of the document.
MARKER = "ZZMARKZZ"


def _document(site: str, text: str) -> dict:
    config: Dict[str, Any] = {
        "project": "proj",
        "components": {
            "svc": {
                "path": "svc",
                "boundary": {"provider": "path-hash", "paths": ["api/*.yaml"]},
                "consumers": [],
                "external_consumers": [],
            }
        },
        "slices": {"all": {"mode": "exact", "components": ["svc"]}},
    }
    SURROGATE_SITES[site](config, text)
    return config


class ConfigSurrogateRejectionTests(unittest.TestCase):
    """OBL-PROVIDERS-022: reject at parse, not at `.encode('utf-8')`."""

    def _load(self, config: dict):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "boundary.config.json"
            path.write_bytes(
                json.dumps(config, indent=2, ensure_ascii=True).encode("ascii")
            )
            return load_config_file(path, repo_root=root)

    def test_the_encode_that_would_have_crashed_really_does_crash(self):
        """The premise for the whole class: the danger is not hypothetical."""
        with self.assertRaises(UnicodeEncodeError):
            sha256_hex("svc\udc80@compat:1.0.0")

    def test_every_injection_site_survives_a_load_when_its_text_is_ascii(self):
        """The premise: each site is really part of the parsed document."""
        for site in sorted(SURROGATE_SITES):
            with self.subTest(site=site):
                loaded = self._load(_document(site, MARKER))
                self.assertIn(MARKER, json.dumps(loaded))

    def test_a_lone_surrogate_at_any_site_is_refused_by_the_loader(self):
        for site in sorted(SURROGATE_SITES):
            with self.subTest(site=site):
                with self.assertRaises(ConfigError) as raised:
                    self._load(_document(site, "\udc80"))
                self.assertIn(
                    "contains values that cannot be represented as "
                    "deterministic JSON",
                    str(raised.exception),
                )
                self.assertIn("not valid Unicode/UTF-8", str(raised.exception))

    def test_an_object_key_and_a_string_value_get_different_diagnostics(self):
        """Both arms of the walker are reached, not one standing in for both."""
        with self.assertRaises(ConfigError) as key_raised:
            self._load(_document("component name", "\udc80"))
        self.assertIn(
            "config.components contains an object key that is not valid "
            "Unicode/UTF-8",
            str(key_raised.exception),
        )
        with self.assertRaises(ConfigError) as value_raised:
            self._load(_document("external_consumers entry", "\udc80"))
        self.assertIn(
            "config.components.svc.external_consumers[0] contains a string "
            "that is not valid Unicode/UTF-8",
            str(value_raised.exception),
        )

    @settings(max_examples=80, deadline=None)
    @given(
        site=st.sampled_from(sorted(SURROGATE_SITES)),
        codepoint=st.integers(min_value=0xD800, max_value=0xDFFF),
    )
    def test_no_surrogate_at_any_site_ever_reaches_a_digest(self, site, codepoint):
        """Whatever the surrogate and wherever it sits, parse refuses it.

        The oracle is independent of the code under test: any string holding a
        code point in D800-DFFF is by definition not encodable as UTF-8, so a
        loader that returns rather than raises has handed a later
        `sha256_hex` an argument that will crash it.
        """
        document = _document(site, chr(codepoint))
        with self.assertRaises(ConfigError):
            self._load(document)

    def test_the_in_memory_door_refuses_the_same_strings(self):
        """`validate_config` is public, and an embedder never touches a file."""
        with Scenario(project="proj") as scene:
            scene.component(
                "svc", path="svc", provider="path-hash", boundary=["api/*.yaml"]
            )
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.commit()
            for site in IN_MEMORY_SITES:
                with self.subTest(site=site, text="clean"):
                    clean = copy.deepcopy(scene.config)
                    clean.setdefault("slices", {})["all"] = {
                        "mode": "exact",
                        "components": ["svc"],
                    }
                    SURROGATE_SITES[site](clean, MARKER)
                    self.assertEqual(
                        validate_config(clean, scene.root, source="head"), []
                    )
                with self.subTest(site=site, text="surrogate"):
                    hostile = copy.deepcopy(scene.config)
                    hostile.setdefault("slices", {})["all"] = {
                        "mode": "exact",
                        "components": ["svc"],
                    }
                    SURROGATE_SITES[site](hostile, "\udc80")
                    errors = validate_config(hostile, scene.root, source="head")
                    self.assertTrue(errors, "surrogate config validated clean")
                    self.assertTrue(
                        all("not valid Unicode/UTF-8" in error for error in errors),
                        errors,
                    )


# ---------------------------------------------------------------------------
# OBL-LOCKFILE-013 - facet gating precedence and unavailable facets
# ---------------------------------------------------------------------------

#: The exact diagnostic verify emits when a selected facet has no digest on one
#: side or the other. Observed, not guessed.
UNAVAILABLE = (
    "UNAVAILABLE FACET svc.behavior: selected gate requires both locked "
    "and current digests"
)


def _null_behavior_scene() -> Scenario:
    """A component that can produce every facet except `behavior`."""
    scene = Scenario(project="proj")
    scene.component("svc", path="svc", provider="path-hash", boundary=["api/*.yaml"])
    scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
    return scene


#: The three policy sources the spec ranks, each selecting `behavior` for a
#: component whose declaration cannot produce it. The value applies the policy
#: to a scenario and returns the keyword arguments verify needs.
def _policy_cli(scene: Scenario) -> dict:
    return {"facets": ["behavior"]}


def _policy_component(scene: Scenario) -> dict:
    scene.config["components"]["svc"]["verify_facets"] = ["behavior"]
    return {}


def _policy_defaults(scene: Scenario) -> dict:
    scene.config.setdefault("defaults", {})["verify_facets"] = ["behavior"]
    return {}


BEHAVIOR_POLICIES = {
    "cli --facets": _policy_cli,
    "component verify_facets": _policy_component,
    "defaults verify_facets": _policy_defaults,
}

#: What `boundver verify` prints, and where, for each policy source. Two of the
#: three never reach verification at all: config validation rejects an explicit
#: gate on a facet the declaration cannot produce, which is a different road to
#: the same exit code and is recorded here as such.
CLI_POLICY_EXPECTATIONS = {
    "no policy": (0, "Lockfile is up to date.", "stdout"),
    "cli --facets": (2, UNAVAILABLE, "stdout"),
    "component verify_facets": (
        2,
        "Component 'svc' explicitly gates 'behavior' but has no behavior.paths",
        "stderr",
    ),
    "defaults verify_facets": (
        2,
        "Component 'svc' explicitly gates 'behavior' but has no behavior.paths",
        "stderr",
    ),
}


class VerificationFacetPolicyTests(unittest.TestCase):
    """OBL-LOCKFILE-013: three precedence levels, and one null is enough."""

    def _assert_only_unavailable(self, issues: List[str]) -> None:
        """The facet gate is the only complaint besides the moved config digest.

        Changing the declaration after the lock was written necessarily moves
        `config_digest`, so that one entry is expected. Nothing else may be
        there - in particular the null/non-null pair must not also be reported
        as ordinary drift.
        """
        self.assertIn(UNAVAILABLE, issues)
        others = [issue for issue in issues if issue != UNAVAILABLE]
        self.assertEqual(len(others), 1, issues)
        self.assertTrue(
            others[0].startswith("METADATA MISMATCH config_digest: "), issues
        )

    def test_a_null_facet_passes_under_the_policy_free_fallback(self):
        """The premise for every exit-2 assertion in this class."""
        with _null_behavior_scene() as scene:
            scene.commit()
            lockfile = scene.generate()
            self.assertIsNone(
                lockfile["components"]["svc"]["fingerprints"]["behavior"]
            )
            self.assertEqual(verify_lockfile(scene.config, lockfile, scene.root), [])

    def test_each_policy_source_turns_the_same_null_facet_into_a_usage_error(self):
        for label, apply_policy in BEHAVIOR_POLICIES.items():
            with self.subTest(policy=label):
                with _null_behavior_scene() as scene:
                    # The policy has to be in place before the lock is written,
                    # or the config digest moves and the list below grows a
                    # METADATA MISMATCH that has nothing to do with facets.
                    keywords = apply_policy(scene)
                    scene.commit()
                    lockfile = scene.generate()
                    issues = verify_lockfile(
                        scene.config, lockfile, scene.root, **keywords
                    )
                    self.assertEqual(issues, [UNAVAILABLE])

    def test_a_selected_facet_present_on_both_sides_is_not_a_usage_error(self):
        """The premise for the one-sided-null tests: the gate itself passes."""
        with Scenario(project="proj") as scene:
            scene.component(
                "svc",
                path="svc",
                provider="path-hash",
                boundary=["api/*.yaml"],
                behavior=["api/*.yaml", "impl/*.py"],
                verify_facets=["behavior"],
            )
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.file("svc/impl/core.py", "x = 1\n")
            scene.commit()
            lockfile = scene.generate()
            self.assertIsNotNone(
                lockfile["components"]["svc"]["fingerprints"]["behavior"]
            )
            self.assertEqual(verify_lockfile(scene.config, lockfile, scene.root), [])

    def test_a_locked_digest_with_no_current_one_is_a_usage_error(self):
        """A provider that stopped producing a boundary is the realistic case."""
        with Scenario(project="proj") as scene:
            scene.component(
                "svc",
                path="svc",
                provider="path-hash",
                boundary=["api/*.yaml"],
                behavior=["api/*.yaml", "impl/*.py"],
                verify_facets=["behavior"],
            )
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.file("svc/impl/core.py", "x = 1\n")
            scene.commit()
            lockfile = scene.generate()
            self.assertIsNotNone(
                lockfile["components"]["svc"]["fingerprints"]["behavior"]
            )
            scene.config["components"]["svc"].pop("behavior")
            scene.commit("drop the behavior declaration")
            current = scene.generate()
            self.assertIsNone(current["components"]["svc"]["fingerprints"]["behavior"])
            self._assert_only_unavailable(
                verify_lockfile(scene.config, lockfile, scene.root)
            )

    def test_a_current_digest_with_no_locked_one_is_a_usage_error(self):
        """The mirror case, so the null test cannot be weakened to an `and`."""
        with Scenario(project="proj") as scene:
            scene.component(
                "svc",
                path="svc",
                provider="path-hash",
                boundary=["api/*.yaml"],
                verify_facets=["behavior"],
            )
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.file("svc/impl/core.py", "x = 1\n")
            scene.commit()
            lockfile = scene.generate()
            self.assertIsNone(lockfile["components"]["svc"]["fingerprints"]["behavior"])
            scene.config["components"]["svc"]["behavior"] = {
                "paths": ["api/*.yaml", "impl/*.py"]
            }
            scene.commit("add the behavior declaration")
            current = scene.generate()
            self.assertIsNotNone(
                current["components"]["svc"]["fingerprints"]["behavior"]
            )
            self._assert_only_unavailable(
                verify_lockfile(scene.config, lockfile, scene.root)
            )

    def test_the_command_exits_two_for_every_policy_source_and_zero_without_one(self):
        for label, (code, marker, stream) in CLI_POLICY_EXPECTATIONS.items():
            with self.subTest(policy=label):
                with _null_behavior_scene() as scene:
                    arguments = ["verify"]
                    if label == "cli --facets":
                        arguments += ["--facets", "behavior"]
                    elif label in BEHAVIOR_POLICIES:
                        BEHAVIOR_POLICIES[label](scene)
                    scene.commit()
                    lockfile = scene.generate()
                    (scene.root / "boundary.lock.json").write_text(
                        json.dumps(lockfile, indent=2) + "\n", encoding="utf-8"
                    )
                    scene.commit("record the lock")
                    result = run_cli(scene.root, *arguments)
                    self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                    observed = result.stdout if stream == "stdout" else result.stderr
                    self.assertIn(marker, observed)


# ---------------------------------------------------------------------------
# OBL-LOCKFILE-015 - slice membership under a component-scoped generation
# ---------------------------------------------------------------------------

def closure_oracle(edges: Dict[str, List[str]], seed: str) -> List[str]:
    """The downstream closure of *seed*, computed without boundver.

    A breadth-first walk of declared `consumers` edges, seed included. This is
    the definition the spec gives; it shares no code with
    `_consumer_graph.consumer_closure`.
    """
    if seed not in edges:
        return []
    reached = {seed}
    pending = [seed]
    while pending:
        current = pending.pop()
        for consumer in edges.get(current, []):
            if consumer in edges and consumer not in reached:
                reached.add(consumer)
                pending.append(consumer)
    return sorted(reached)


GRAPH_SIZE = 5
GRAPH_NAMES = tuple(f"c{index}" for index in range(GRAPH_SIZE))


class PartialGenerationSliceMembershipTests(unittest.TestCase):
    """OBL-LOCKFILE-015: scoped generation resolves the same membership."""

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario(project="graph")
        for name in GRAPH_NAMES:
            cls.scene.component(
                name, path=name, provider="path-hash", boundary=["api/*.yaml"]
            )
            cls.scene.file(f"{name}/api/v1.yaml", f"openapi: 3.1.0\n# {name}\n")
        cls.scene.commit()

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def _config(self, edges: Dict[str, List[str]], seed: str) -> dict:
        config: Dict[str, Any] = {
            "project": "graph",
            "components": {
                name: {
                    "path": name,
                    "boundary": {"provider": "path-hash", "paths": ["api/*.yaml"]},
                    "consumers": list(edges[name]),
                }
                for name in GRAPH_NAMES
            },
            "slices": {
                "closure": {"mode": "exact", "closure_of": seed},
                "explicit": {"mode": "exact", "components": [GRAPH_NAMES[0]]},
            },
        }
        return config

    def _both_ways(self, config: dict, selected: List[str]) -> Tuple[dict, dict]:
        full = generate_lockfile(config, self.scene.root, source="working-tree")
        partial = generate_lockfile_for_components(
            config,
            self.scene.root,
            selected,
            self.scene.root / "boundary.lock.json",
            source="working-tree",
            existing_lockfile=copy.deepcopy(full),
        )
        return full, partial

    def test_a_scoped_generation_recomputes_slices_rather_than_copying_them(self):
        """The premise: the slices block in the output is not the input's."""
        edges = {name: [] for name in GRAPH_NAMES}
        edges["c0"] = ["c1"]
        config = self._config(edges, "c0")
        full = generate_lockfile(config, self.scene.root, source="working-tree")
        corrupted = copy.deepcopy(full)
        corrupted["slices"]["closure"]["components"] = list(GRAPH_NAMES)
        corrupted["slices"]["closure"]["fingerprint"] = "0" * 64
        partial = generate_lockfile_for_components(
            config,
            self.scene.root,
            ["c3"],
            self.scene.root / "boundary.lock.json",
            source="working-tree",
            existing_lockfile=corrupted,
        )
        self.assertEqual(partial["slices"]["closure"]["components"], ["c0", "c1"])
        self.assertEqual(partial["slices"], full["slices"])

    def test_membership_reaches_past_the_selected_components(self):
        """Selecting the tail of a chain must still resolve the whole closure."""
        edges = {name: [] for name in GRAPH_NAMES}
        edges["c0"] = ["c1"]
        edges["c1"] = ["c2"]
        config = self._config(edges, "c0")
        _, partial = self._both_ways(config, ["c2"])
        self.assertEqual(
            partial["slices"]["closure"]["components"], ["c0", "c1", "c2"]
        )

    def test_a_consumer_cycle_resolves_the_same_way_under_a_scoped_generation(self):
        edges = {name: [] for name in GRAPH_NAMES}
        edges["c0"] = ["c1"]
        edges["c1"] = ["c0", "c2"]
        config = self._config(edges, "c1")
        full, partial = self._both_ways(config, ["c4"])
        self.assertEqual(partial["slices"], full["slices"])
        self.assertEqual(
            partial["slices"]["closure"]["components"], ["c0", "c1", "c2"]
        )

    @settings(max_examples=30, deadline=None)
    @given(
        adjacency=st.lists(
            st.lists(st.sampled_from(GRAPH_NAMES), max_size=3, unique=True),
            min_size=GRAPH_SIZE,
            max_size=GRAPH_SIZE,
        ),
        seed=st.sampled_from(GRAPH_NAMES),
        selected=st.lists(
            st.sampled_from(GRAPH_NAMES), min_size=1, max_size=GRAPH_SIZE, unique=True
        ),
    )
    def test_scoped_and_full_generation_agree_on_every_slice(
        self, adjacency, seed, selected
    ):
        """The whole slices block, on any graph, for any selection.

        `generate` resolves `closure_of` from the merged lock entries while
        `verify` resolves it from the configuration. The oracle is the closure
        computed here from the configuration alone.
        """
        edges = {
            name: [target for target in targets if target != name]
            for name, targets in zip(GRAPH_NAMES, adjacency)
        }
        config = self._config(edges, seed)
        full, partial = self._both_ways(config, selected)
        self.assertEqual(partial["slices"], full["slices"])
        self.assertEqual(
            partial["slices"]["closure"]["components"], closure_oracle(edges, seed)
        )


# ---------------------------------------------------------------------------
# OBL-LOCKFILE-024 - no sidecar survives a failed atomic write
# ---------------------------------------------------------------------------

SIDECAR_GLOB = ".boundary.lock.json.*.tmp"
SIDECAR_NAME = re.compile(r"^\.boundary\.lock\.json\.[0-9a-f]{16}\.tmp$")

#: The original bytes every failure below has to leave untouched.
ORIGINAL = b"original\n"


class AtomicWriteSidecarTests(unittest.TestCase):
    """OBL-LOCKFILE-024: a failed write leaves neither a sidecar nor damage."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.parent = Path(self._directory.name) / "artifacts"
        self.parent.mkdir()
        self.target = self.parent / "boundary.lock.json"
        self.target.write_bytes(ORIGINAL)

    def tearDown(self):
        self._directory.cleanup()

    def _sidecars(self) -> List[str]:
        return sorted(path.name for path in self.parent.glob(SIDECAR_GLOB))

    def test_a_sidecar_really_exists_while_the_write_is_in_flight(self):
        """The premise: the glob below can see the file it later denies."""
        seen: List[List[str]] = []
        original = core._revalidate_atomic_output_ancestors

        def peek(ancestors):
            seen.append(self._sidecars())
            return original(ancestors)

        with mock.patch.object(
            core, "_revalidate_atomic_output_ancestors", peek
        ):
            core._write_text_atomic(self.target, "new\n")
        self.assertEqual(len(seen), 1, seen)
        self.assertEqual(len(seen[0]), 1, seen)
        self.assertRegex(seen[0][0], SIDECAR_NAME)
        self.assertEqual(self._sidecars(), [])
        self.assertEqual(self.target.read_bytes(), b"new\n")

    def _stage(self, name: str):
        """One injected failure per stage that runs after the sidecar exists."""
        if name == "serialisation":
            return contextlib.nullcontext(), "new \udc80\n", UnicodeEncodeError
        if name == "fsync":
            return (
                mock.patch.object(os, "fsync", side_effect=OSError(5, "injected")),
                "new\n",
                OSError,
            )
        if name == "revalidation":
            return (
                mock.patch.object(
                    core,
                    "_revalidate_atomic_output_ancestors",
                    side_effect=ConfigError("injected"),
                ),
                "new\n",
                ConfigError,
            )
        if name == "replace":
            return (
                mock.patch.object(
                    core._MutationDirectory,
                    "replace",
                    side_effect=OSError(13, "injected"),
                ),
                "new\n",
                OSError,
            )
        raise AssertionError(f"unknown stage {name!r}")

    def test_no_failed_stage_leaves_a_sidecar_or_touches_the_existing_lock(self):
        for stage in ("serialisation", "fsync", "revalidation", "replace"):
            with self.subTest(stage=stage):
                self.target.write_bytes(ORIGINAL)
                patcher, text, expected = self._stage(stage)
                with patcher:
                    with self.assertRaises(expected):
                        core._write_text_atomic(self.target, text)
                self.assertEqual(self._sidecars(), [])
                self.assertEqual(self.target.read_bytes(), ORIGINAL)

    def test_a_successful_write_publishes_and_leaves_nothing_beside_it(self):
        core._write_text_atomic(self.target, "published\n")
        self.assertEqual(self.target.read_bytes(), b"published\n")
        self.assertEqual(self._sidecars(), [])


# ---------------------------------------------------------------------------
# OBL-LOCKFILE-025 - what a component-scoped generation refuses
# ---------------------------------------------------------------------------

#: The refusal diagnostics, quoted from observed output.
STALE_REFUSAL = (
    "Cannot partially generate because unselected component 'b' is stale. "
    "Run a full `boundver generate`."
)
PROJECT_REFUSAL = (
    "Cannot partially update after the project name changed. "
    "Run a full `boundver generate`."
)
MISSING_ENTRY_REFUSAL = (
    "Cannot partially generate because the existing lockfile has no entry for"
)


def _three_component_scene() -> Scenario:
    """a -> b, plus an unrelated c, a closure slice and an explicit one."""
    scene = Scenario(project="proj")
    scene.component(
        "a", path="a", provider="path-hash", boundary=["api/*.yaml"], consumers=["b"]
    )
    scene.component("b", path="b", provider="path-hash", boundary=["api/*.yaml"])
    scene.component("c", path="c", provider="path-hash", boundary=["api/*.yaml"])
    for name in ("a", "b", "c"):
        scene.file(f"{name}/api/v1.yaml", f"openapi: 3.1.0\n# {name}\n")
    scene.slice("closure", mode="exact", closure_of="a")
    scene.slice("only_c", mode="exact", components=["c"])
    scene.commit()
    return scene


class PartialGenerationStalenessTests(unittest.TestCase):
    """OBL-LOCKFILE-025: an output-selection convenience, not a laundry."""

    def _partial(
        self,
        scene: Scenario,
        selected: List[str],
        lockfile: dict,
        config: Optional[dict] = None,
    ) -> dict:
        return generate_lockfile_for_components(
            scene.config if config is None else config,
            scene.root,
            selected,
            scene.root / "boundary.lock.json",
            source="working-tree",
            existing_lockfile=copy.deepcopy(lockfile),
        )

    def test_an_untouched_repository_regenerates_without_refusing(self):
        """The premise: every refusal below is caused by what the test changed."""
        with _three_component_scene() as scene:
            full = generate_lockfile(scene.config, scene.root, source="working-tree")
            partial = self._partial(scene, ["a"], full)
            self.assertEqual(partial["components"], full["components"])
            self.assertEqual(partial["slices"], full["slices"])

    def test_a_stale_unselected_component_is_refused_by_name(self):
        with _three_component_scene() as scene:
            full = generate_lockfile(scene.config, scene.root, source="working-tree")
            corrupted = copy.deepcopy(full)
            corrupted["components"]["b"]["fingerprints"]["exact"] = "1" * 64
            with self.assertRaises(ConfigError) as raised:
                self._partial(scene, ["a"], corrupted)
            self.assertEqual(str(raised.exception), STALE_REFUSAL)

    def test_a_drifted_unselected_working_tree_is_refused_by_name(self):
        """The same clause reached by real drift rather than a doctored lock."""
        with _three_component_scene() as scene:
            full = generate_lockfile(scene.config, scene.root, source="working-tree")
            scene.file("b/api/v1.yaml", "openapi: 3.1.0\n# b drifted\n")
            with self.assertRaises(ConfigError) as raised:
                self._partial(scene, ["a"], full)
            self.assertEqual(str(raised.exception), STALE_REFUSAL)

    def test_a_changed_project_name_is_refused(self):
        with _three_component_scene() as scene:
            full = generate_lockfile(scene.config, scene.root, source="working-tree")
            renamed = copy.deepcopy(full)
            renamed["project"] = "something-else"
            with self.assertRaises(ConfigError) as raised:
                self._partial(scene, ["a"], renamed)
            self.assertEqual(str(raised.exception), PROJECT_REFUSAL)

    def test_a_component_removed_from_config_is_pruned_from_the_output(self):
        with _three_component_scene() as scene:
            full = generate_lockfile(scene.config, scene.root, source="working-tree")
            self.assertIn("c", full["components"])
            reduced = copy.deepcopy(scene.config)
            reduced["components"].pop("c")
            reduced["slices"].pop("only_c")
            partial = self._partial(scene, ["a"], full, config=reduced)
            self.assertEqual(sorted(partial["components"]), ["a", "b"])
            self.assertEqual(sorted(partial["slices"]), ["closure"])

    def test_a_slice_with_no_selected_member_is_still_recomputed(self):
        with _three_component_scene() as scene:
            full = generate_lockfile(scene.config, scene.root, source="working-tree")
            corrupted = copy.deepcopy(full)
            corrupted["slices"]["only_c"]["components"] = ["a", "c"]
            corrupted["slices"]["only_c"]["fingerprint"] = "0" * 64
            partial = self._partial(scene, ["a"], corrupted)
            self.assertEqual(partial["slices"]["only_c"], full["slices"]["only_c"])

    def test_a_missing_unselected_entry_is_refused_as_an_incomplete_base(self):
        with _three_component_scene() as scene:
            full = generate_lockfile(scene.config, scene.root, source="working-tree")
            without_b = copy.deepcopy(full)
            without_b["components"].pop("b")
            with self.assertRaises(ConfigError) as raised:
                self._partial(scene, ["a"], without_b)
            self.assertIn(MISSING_ENTRY_REFUSAL, str(raised.exception))
            self.assertIn("b", str(raised.exception))

    def test_a_missing_selected_entry_is_refused_as_an_incomplete_base(self):
        with _three_component_scene() as scene:
            full = generate_lockfile(scene.config, scene.root, source="working-tree")
            without_c = copy.deepcopy(full)
            without_c["components"].pop("c")
            with self.assertRaises(ConfigError) as raised:
                self._partial(scene, ["c"], without_c)
            self.assertIn(MISSING_ENTRY_REFUSAL, str(raised.exception))
            self.assertIn("c", str(raised.exception))

    def test_a_component_the_lock_has_no_entry_for_is_refused(self):
        """A scoped refresh cannot bootstrap an incomplete base lock."""
        with _three_component_scene() as scene:
            full = generate_lockfile(scene.config, scene.root, source="working-tree")
            without_c = copy.deepcopy(full)
            without_c["components"].pop("c")
            with self.assertRaises(ConfigError) as raised:
                self._partial(scene, ["c"], without_c)
            self.assertIn(MISSING_ENTRY_REFUSAL, str(raised.exception))


# ---------------------------------------------------------------------------
# OBL-LOCKFILE-026 - a name can never be read as another name's identity
# ---------------------------------------------------------------------------

#: The message shapes `_lockfile.py` builds for a facet mismatch. The two
#: binding tests below prove verify really emits exactly these, so the property
#: is entitled to construct them rather than run a repository per example.
COMPONENT_MESSAGE = "MISMATCH {subject}.{facet}: lockfile={locked} current={current}"
SLICE_MESSAGE = (
    "SLICE MISMATCH {subject}.{facet}: lockfile={locked} current={current}"
)

#: Fragments chosen because each one re-creates part of the message grammar the
#: regexes anchor on: the facet delimiter, the field names, the digest ellipsis,
#: the leading verb, and the newline that DOTALL makes invisible.
NAME_FRAGMENTS = (
    "a",
    "b",
    ".",
    ":",
    ": ",
    "\n",
    "-",
    ".exact:",
    ".behavior:",
    ".boundary:",
    ".compat:",
    "MISMATCH ",
    "SLICE MISMATCH ",
    "lockfile=",
    " current=",
    "none",
    "...",
)


def _component_identifier_pattern() -> "re.Pattern[str]":
    """The accepted component-name grammar, read from the packaged schema.

    This is the same file `_config._load_config_schema` reads through
    `importlib.resources`, so widening the grammar there widens the property
    below instead of leaving it testing a transcription that has gone stale.
    """
    schema = json.loads(
        (REPOSITORY_ROOT / "src" / "boundver" / "boundary.config.schema.json").read_text(
            encoding="utf-8"
        )
    )
    return re.compile(schema["$defs"]["componentIdentifier"]["pattern"])


ACCEPTED_NAME = _component_identifier_pattern()


def _short(digest: Optional[str]) -> str:
    """How a digest is displayed: twelve hex characters and an ellipsis."""
    return "none" if digest is None else digest[:12] + "..."


class ViolationIdentityRoundTripTests(unittest.TestCase):
    """OBL-LOCKFILE-026: parse the message back into the name that made it."""

    def _drifting_scene(self, name: str) -> Scenario:
        scene = Scenario(project="proj")
        scene.component(
            name,
            path="svc",
            provider="path-hash",
            boundary=["api/*.yaml"],
            behavior=["api/*.yaml", "impl/*.py"],
            version_source={"file": "package.json", "field": "version"},
        )
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("svc/impl/core.py", "x = 1\n")
        scene.json_file("svc/package.json", {"version": "1.0.0"})
        scene.commit()
        return scene

    def test_verify_emits_exactly_the_message_shape_the_property_assumes(self):
        """The binding: the template is observed output, not a guess."""
        name = "svc.compat: y"
        with self._drifting_scene(name) as scene:
            self.assertEqual(validate_config(scene.config, scene.root, source="head"), [])
            locked = scene.generate()
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n# drift\n")
            scene.file("svc/impl/core.py", "x = 2\n")
            scene.json_file("svc/package.json", {"version": "2.0.0"})
            scene.commit("drift every facet")
            current = scene.generate()
            issues = verify_lockfile(scene.config, locked, scene.root)
            for facet in FACETS:
                with self.subTest(facet=facet):
                    expected = COMPONENT_MESSAGE.format(
                        subject=name,
                        facet=facet,
                        locked=_short(
                            locked["components"][name]["fingerprints"][facet]
                        ),
                        current=_short(
                            current["components"][name]["fingerprints"][facet]
                        ),
                    )
                    self.assertIn(expected, issues)
                    identity = _baseline.violation_identity(expected)
                    self.assertEqual(identity["kind"], "component-facet")
                    self.assertEqual(identity["subject"], name)
                    self.assertEqual(identity["facet"], facet)

    def test_a_slice_message_binds_to_the_slice_grammar_too(self):
        name = "rel.exact: train"
        with Scenario(project="proj") as scene:
            scene.component(
                "a", path="a", provider="path-hash", boundary=["api/*.yaml"]
            )
            scene.file("a/api/v1.yaml", "openapi: 3.1.0\n")
            scene.slice(name, mode="boundary", components=["a"])
            scene.commit()
            locked = scene.generate()
            scene.file("a/api/v1.yaml", "openapi: 3.1.0\n# drift\n")
            scene.commit("drift")
            current = scene.generate()
            expected = SLICE_MESSAGE.format(
                subject=name,
                facet="boundary",
                locked=_short(locked["slices"][name]["fingerprint"]),
                current=_short(current["slices"][name]["fingerprint"]),
            )
            self.assertIn(expected, verify_lockfile(scene.config, locked, scene.root))
            identity = _baseline.violation_identity(expected)
            self.assertEqual(identity["kind"], "slice-facet")
            self.assertEqual(identity["subject"], name)
            self.assertEqual(identity["facet"], "boundary")

    def test_a_name_that_is_not_a_mismatch_message_has_no_identity(self):
        """The premise for the round trip: the parser can say no."""
        self.assertIsNone(
            _baseline.violation_identity("NEW component not in lockfile: svc")
        )
        self.assertIsNone(
            _baseline.violation_identity(
                "UNAVAILABLE FACET svc.behavior: selected gate requires both "
                "locked and current digests"
            )
        )

    @settings(max_examples=400, deadline=None,
              suppress_health_check=[HealthCheck.filter_too_much])
    @given(
        pieces=st.lists(st.sampled_from(NAME_FRAGMENTS), min_size=1, max_size=6),
        facet=st.sampled_from(FACETS),
        locked=st.sampled_from(["none", "26f98b0c16b3..."]),
        current=st.sampled_from(["none", "55aaa8eb8f50..."]),
        template=st.sampled_from([COMPONENT_MESSAGE, SLICE_MESSAGE]),
    )
    @example(
        pieces=["a", ".exact:"],
        facet="exact",
        locked="none",
        current="none",
        template=COMPONENT_MESSAGE,
    )
    @example(
        pieces=["MISMATCH ", "a", ".boundary:", " current="],
        facet="compat",
        locked="none",
        current="none",
        template=COMPONENT_MESSAGE,
    )
    def test_every_accepted_name_parses_back_to_itself(
        self, pieces, facet, locked, current, template
    ):
        """Whatever the name mimics, the identity is the name and the facet.

        The oracle is the input: a message built for (N, F) must parse back to
        (N, F). No part of it is derived from the regexes under test.
        """
        name = "".join(pieces)
        assume(ACCEPTED_NAME.match(name) is not None)
        message = template.format(
            subject=name, facet=facet, locked=locked, current=current
        )
        identity = _baseline.violation_identity(message)
        self.assertIsNotNone(identity, message)
        self.assertEqual(identity["subject"], name)
        self.assertEqual(identity["facet"], facet)
        self.assertEqual(
            identity["kind"],
            "component-facet" if template is COMPONENT_MESSAGE else "slice-facet",
        )

    @settings(max_examples=300, deadline=None,
              suppress_health_check=[HealthCheck.filter_too_much])
    @given(
        first=st.lists(st.sampled_from(NAME_FRAGMENTS), min_size=1, max_size=5),
        second=st.lists(st.sampled_from(NAME_FRAGMENTS), min_size=1, max_size=5),
        first_facet=st.sampled_from(FACETS),
        second_facet=st.sampled_from(FACETS),
    )
    def test_two_different_subjects_never_share_a_baseline_identity(self, first,
                                                                    second,
                                                                    first_facet,
                                                                    second_facet):
        """The harm the obligation names: one component's debt acknowledging another's."""
        left, right = "".join(first), "".join(second)
        assume(ACCEPTED_NAME.match(left) is not None)
        assume(ACCEPTED_NAME.match(right) is not None)
        assume((left, first_facet) != (right, second_facet))
        left_identity = _baseline.violation_identity(
            COMPONENT_MESSAGE.format(
                subject=left, facet=first_facet, locked="none", current="none"
            )
        )
        right_identity = _baseline.violation_identity(
            COMPONENT_MESSAGE.format(
                subject=right, facet=second_facet, locked="none", current="none"
            )
        )
        self.assertNotEqual(left_identity["id"], right_identity["id"])


if __name__ == "__main__":  # pragma: no cover - manual entry point
    unittest.main()
