"""The narrow commands, and what they are allowed to write.

`--components`, `--facets`, `--changed-from` and `--allow-partial` all narrow
something, and three of them narrow different things: `--components` narrows
what is written, `--facets` narrows what gates, `--changed-from` narrows what is
reported. The scoped update is the only place boundver merges fresh
fingerprints into a lock document it did not itself produce, so every guard
around it is a refusal, and refusals are the hardest thing in this suite to
test honestly: a command that refuses looks exactly like a command whose code
path was never entered. Every refusal below is therefore paired with a premise
in the same class that drives the same command to a successful write, so an
assertion that the lock bytes did not move is an assertion about a guard rather
than about a typo in an argument list.

Getting at OBL-HASHING-054 took the most care, because the obvious spelling
tests something else. A repository whose lock came from `generate
--allow-partial` has a boundary-mode slice with a null member digest, and if
you ask for that facet explicitly with `--facets boundary` the run stops at the
UNAVAILABLE FACET pre-gate, which declines to write and explains itself
politely. That is not the path the obligation is about. With no `--facets` at
all the implicit policy does not gate boundary, so verify reports ordinary
`exact` drift, accepts `--update`, and only then reaches a regeneration that is
hardcoded to `strict=True` - and the ConfigError from the slice recomputation
reaches the user as a bare `ERROR: update failed: Slice 'S' requires boundary
digest for component 'svc'`. Both paths are pinned here side by side, because
the difference between them is the whole finding. `--allow-partial` is not a
verify flag at all; the argparse refusal for it is pinned too, since it is the
reason no message could reasonably tell the user to add it to this command.

The two model-based obligations compare a scoped result against a full
`generate` on the same source, which is the oracle the obligations themselves
name, so the property is differential rather than independent. That makes a
sensitivity check mandatory rather than decorative: the test that rejects an
update mask builds by hand the exact wrong answer the obligation fears - a
merge that refreshed only the gated facet and kept the old slices - and shows
the comparison rejects it. The property runs against the
working-tree source so an example costs three hashing passes and no commit,
which is what makes twenty of them affordable. One finding came out of writing
it: the missing-entries refusal at `_lockfile.py:1147-1153` cannot be reached,
because every way to remove an entry is caught first by the unselected-stale
loop or by the structure guard, and the enumeration that shows this is the last
test in the refusal class.

Covers OBL-HASHING-054, OBL-HASHING-098, OBL-HASHING-099, OBL-LOCKFILE-016,
OBL-LOCKFILE-035 and OBL-LOCKFILE-036.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver._lockfile import (
    COMPONENT_METADATA_FIELDS,
    dump_lockfile,
    generate_lockfile,
    generate_lockfile_for_components,
    semantic_config_digest,
)
from boundver._utils import ConfigError

from tests._parity import run_cli
from tests._scenarios import Scenario

LOCK = "boundary.lock.json"

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_USAGE = 2

#: The trailing hint every `generate` failure appends after its own message.
GENERATE_HINT = (
    "Review the reported provider, source, or facet error. "
    "Use --allow-partial only when null slice facet inputs are intentional."
)

#: The one refusal OBL-LOCKFILE-016 is about, spelled exactly as observed.
UNSELECTED_STALE = (
    "ERROR: update failed: Cannot partially generate because unselected "
    "component 'c' is stale. Run a full `boundver generate`.\n"
)


def canonical(value: Any) -> str:
    """One string per document value, so two locks compare as documents."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def read_lock(scene: Scenario) -> dict:
    return json.loads((scene.root / LOCK).read_text(encoding="utf-8"))


def write_lock(scene: Scenario, document: dict) -> None:
    (scene.root / LOCK).write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8"
    )


def error_tail(stderr: str) -> str:
    """The diagnostic, without the `Source: ... | Inputs: ...` banner.

    Verify prints that banner to stderr on some refusal paths and to stdout on
    others, and it carries a commit id, so an exact comparison has to start at
    the first ERROR line.
    """
    index = stderr.find("ERROR:")
    return stderr if index < 0 else stderr[index:]


def scoped_repository(project: str = "scoped") -> Scenario:
    """Three independent path-hash components, each with a boundary selector."""
    scene = Scenario(project)
    for name in ("a", "b", "c"):
        scene.component(name, path=name, provider="path-hash", boundary=["api/*.yaml"])
        scene.file(f"{name}/api/v1.yaml", f"openapi: 3.1.0\ninfo: {name}\n")
        scene.file(f"{name}/impl.py", f"# {name}\n")
    scene.commit()
    return scene


def with_committed_lock(scene: Scenario) -> Scenario:
    """Generate a full lock and commit it, so `--source head` can read it."""
    result = run_cli(scene.root, "generate")
    assert result.returncode == EXIT_OK, result.stderr
    scene.commit("lock")
    return scene


def partial_repository(*, with_slice: bool = True) -> Scenario:
    """The shape `generate --allow-partial` exists for.

    `svc` uses the implicit provider with no declared boundary paths, so its
    boundary digest is null; the slice aggregates that facet anyway. Full
    generation refuses this configuration outright, which is why the adoption
    docs reach for `--allow-partial` here.
    """
    scene = Scenario("partial")
    scene.component("svc", path="svc", provider="implicit")
    scene.component("lib", path="lib", provider="path-hash", boundary=["api/*.yaml"])
    scene.file("svc/main.py", "x = 1\n")
    scene.file("lib/api/v1.yaml", "openapi: 3.1.0\n")
    if with_slice:
        scene.slice("S", mode="boundary", components=["svc", "lib"])
    scene.commit()
    result = run_cli(scene.root, "generate", "--allow-partial")
    assert result.returncode == EXIT_OK, result.stderr
    scene.commit("lock")
    return scene


class AllowPartialVerifyUpdateTests(unittest.TestCase):
    """OBL-HASHING-054: the adoption shape that cannot accept its own drift."""

    def _drifted(self, *, with_slice: bool = True) -> Scenario:
        scene = partial_repository(with_slice=with_slice)
        scene.file("svc/main.py", "x = 2\n")
        scene.commit("drift")
        return scene

    def test_verify_update_writes_when_no_boundary_slice_stands_in_the_way(self):
        """Premise: the same drift, the same command, an actual write."""
        with self._drifted(with_slice=False) as scene:
            before = (scene.root / LOCK).read_bytes()
            result = run_cli(scene.root, "verify", "--update")
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertIn("after successful generation.", result.stdout)
            self.assertNotEqual(before, (scene.root / LOCK).read_bytes())

    def test_plain_verify_reports_ordinary_drift_on_an_allow_partial_repository(self):
        with self._drifted() as scene:
            result = run_cli(scene.root, "verify")
            self.assertEqual(result.returncode, EXIT_DRIFT, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertIn("LOCKFILE OUT OF DATE (1 issues):", result.stdout)
            self.assertIn("MISMATCH svc.exact: lockfile=", result.stdout)

    def test_verify_update_tells_the_user_to_rerun_generate_with_allow_partial(self):
        """The refusal names the supported recovery workflow."""
        with self._drifted() as scene:
            result = run_cli(scene.root, "verify", "--update")
            self.assertIn("--allow-partial", result.stdout + result.stderr)

    def test_verify_update_exits_two_with_guidance_and_writes_nothing(self):
        with self._drifted() as scene:
            before = (scene.root / LOCK).read_bytes()
            result = run_cli(scene.root, "verify", "--update")
            self.assertEqual(result.returncode, EXIT_USAGE)
            self.assertEqual(
                result.stderr,
                "ERROR: update failed: Slice 'S' requires boundary digest "
                "for component 'svc'\n"
                "Run `boundver generate --allow-partial` only if null slice "
                "facet inputs are intentional.\n",
            )
            self.assertEqual(before, (scene.root / LOCK).read_bytes())

    def test_the_json_view_of_that_refusal_prints_no_payload_at_all(self):
        with self._drifted() as scene:
            result = run_cli(scene.root, "verify", "--update", "--format", "json")
            self.assertEqual(result.returncode, EXIT_USAGE)
            self.assertEqual(result.stdout, "")
            self.assertEqual(
                result.stderr,
                "ERROR: update failed: Slice 'S' requires boundary digest "
                "for component 'svc'\n"
                "Run `boundver generate --allow-partial` only if null slice "
                "facet inputs are intentional.\n",
            )

    def test_naming_the_boundary_facet_takes_the_pre_gate_path_instead(self):
        """The contrast case, and the reason the divergence stayed hidden.

        Existing coverage passes `--facets boundary`, which stops at the
        UNAVAILABLE FACET pre-gate. That path declines to write and explains
        itself; the ungated path above does not.
        """
        with self._drifted() as scene:
            before = (scene.root / LOCK).read_bytes()
            result = run_cli(scene.root, "verify", "--update", "--facets", "boundary")
            self.assertEqual(result.returncode, EXIT_USAGE)
            self.assertEqual(result.stderr, "")
            self.assertIn(
                "UNAVAILABLE FACET svc.boundary: selected gate requires both "
                "locked and current digests",
                result.stdout,
            )
            self.assertIn(
                "LOCKFILE NOT UPDATED: Unavailable facets cannot be generated "
                "from the current configuration.",
                result.stdout,
            )
            self.assertEqual(before, (scene.root / LOCK).read_bytes())

    def test_verify_does_not_accept_allow_partial_as_an_argument(self):
        """No message could tell the user to add the flag to this command."""
        with self._drifted() as scene:
            result = run_cli(scene.root, "verify", "--update", "--allow-partial")
            self.assertEqual(result.returncode, EXIT_USAGE)
            self.assertIn(
                "boundver: error: unrecognized arguments: --allow-partial",
                result.stderr,
            )

    def test_a_full_generate_with_allow_partial_is_the_only_way_forward(self):
        with self._drifted() as scene:
            before = (scene.root / LOCK).read_bytes()
            plain = run_cli(scene.root, "generate")
            self.assertEqual(plain.returncode, EXIT_USAGE)
            self.assertEqual(
                plain.stderr,
                "ERROR: Config is invalid (1 issues):\n"
                "  - Slice 'S' mode 'boundary' requires boundary digest from "
                "component 'svc' to supply that facet, but provider 'implicit' "
                "does not produce a boundary digest from this declaration\n",
            )
            self.assertEqual(before, (scene.root / LOCK).read_bytes())

            partial = run_cli(scene.root, "generate", "--allow-partial")
            self.assertEqual(partial.returncode, EXIT_OK, partial.stderr)
            self.assertNotEqual(before, (scene.root / LOCK).read_bytes())
            scene.commit("recovered")
            self.assertEqual(run_cli(scene.root, "verify").returncode, EXIT_OK)


#: Ways to leave the unselected component 'c' differing from its recomputed
#: entry without touching any file, and without touching the facet the gate
#: looks at. Each takes the locked entry for 'c' and edits it in place.
UNSELECTED_STALE_SHAPES: Dict[str, Callable[[dict], None]] = {
    "a fabricated warning": lambda e: e.__setitem__("warnings", ["fabricated"]),
    "a recorded version": lambda e: e.__setitem__("version", "9.9.9"),
    "a consumer edge": lambda e: e.__setitem__("consumers", ["a"]),
    "the provider version": lambda e: e.__setitem__("boundary_provider_version", "2"),
    "the ungated behavior facet": (
        lambda e: e["fingerprints"].__setitem__("behavior", "0" * 64)
    ),
    "the ungated compat facet": (
        lambda e: e["fingerprints"].__setitem__("compat", "0" * 64)
    ),
}


class UnselectedStaleRefusalTests(unittest.TestCase):
    """OBL-LOCKFILE-016: the guard against blessing unrelated drift."""

    def _repository_with_drifted_a(self, mutate=None) -> Scenario:
        """Drift 'a' so the gated update path is entered, then stale 'c'."""
        scene = with_committed_lock(scoped_repository())
        if mutate is not None:
            document = read_lock(scene)
            mutate(document["components"]["c"])
            write_lock(scene, document)
        scene.file("a/impl.py", "# a drifted\n")
        scene.commit("drift a")
        return scene

    def test_a_scoped_update_writes_when_every_unselected_entry_is_current(self):
        """Premise: without a stale 'c' this exact command rewrites the lock."""
        with self._repository_with_drifted_a() as scene:
            before = read_lock(scene)
            result = run_cli(scene.root, "verify", "--components", "a", "--update")
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            after = read_lock(scene)
            self.assertNotEqual(before["components"]["a"], after["components"]["a"])
            self.assertEqual(before["components"]["b"], after["components"]["b"])
            self.assertEqual(before["components"]["c"], after["components"]["c"])

    def test_every_stale_shape_of_an_unselected_entry_refuses_identically(self):
        for label, mutate in UNSELECTED_STALE_SHAPES.items():
            with self.subTest(stale=label):
                with self._repository_with_drifted_a(mutate) as scene:
                    before = (scene.root / LOCK).read_bytes()
                    result = run_cli(
                        scene.root, "verify", "--components", "a", "--update"
                    )
                    self.assertEqual(result.returncode, EXIT_USAGE, result.stdout)
                    self.assertEqual(result.stderr, UNSELECTED_STALE)
                    self.assertEqual(before, (scene.root / LOCK).read_bytes())

    def test_the_refusal_survives_a_narrowed_facet_gate(self):
        """`--facets boundary` reports less, and must not refuse less."""
        mutate = UNSELECTED_STALE_SHAPES["the ungated behavior facet"]
        with self._repository_with_drifted_a(mutate) as scene:
            scene.file("a/api/v1.yaml", "openapi: 3.1.0\ninfo: a2\n")
            scene.commit("drift a boundary")
            before = (scene.root / LOCK).read_bytes()
            result = run_cli(
                scene.root, "verify", "--components", "a",
                "--facets", "boundary", "--update",
            )
            self.assertEqual(result.returncode, EXIT_USAGE, result.stdout)
            self.assertEqual(result.stderr, UNSELECTED_STALE)
            self.assertEqual(before, (scene.root / LOCK).read_bytes())

    def test_real_source_drift_in_an_unselected_component_refuses_too(self):
        with self._repository_with_drifted_a() as scene:
            scene.file("c/impl.py", "# c drifted\n")
            scene.commit("drift c")
            before = (scene.root / LOCK).read_bytes()
            result = run_cli(scene.root, "verify", "--components", "a", "--update")
            self.assertEqual(result.returncode, EXIT_USAGE, result.stdout)
            self.assertEqual(result.stderr, UNSELECTED_STALE)
            self.assertEqual(before, (scene.root / LOCK).read_bytes())


#: Facet spellings that still gate the drift introduced in 'a', so the scoped
#: write really happens. `compat` is absent on purpose: both components leave
#: it null, so naming it stops at the UNAVAILABLE FACET pre-gate instead.
GATING_FACET_SPELLINGS: Dict[str, List[str]] = {
    "no --facets": [],
    "--facets exact": ["--facets", "exact"],
    "--facets boundary": ["--facets", "boundary"],
    "--facets boundary,exact": ["--facets", "boundary,exact"],
}


class ScopedEntryReplacementTests(unittest.TestCase):
    """OBL-HASHING-098: `--facets` narrows the gate, never the write."""

    def _drifted(self) -> Scenario:
        scene = with_committed_lock(scoped_repository())
        scene.file("a/api/v1.yaml", "openapi: 3.1.0\ninfo: a2\n")
        scene.file("a/impl.py", "# a drifted\n")
        scene.commit("drift a")
        return scene

    def _full_entry(self, scene: Scenario, name: str) -> dict:
        result = run_cli(scene.root, "generate")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        return read_lock(scene)["components"][name]

    def test_the_written_entry_matches_a_full_generate_under_every_gate(self):
        for label, flags in GATING_FACET_SPELLINGS.items():
            with self.subTest(gate=label):
                with self._drifted() as scene:
                    result = run_cli(
                        scene.root, "verify", "--components", "a", *flags, "--update"
                    )
                    self.assertEqual(result.returncode, EXIT_OK, result.stderr)
                    scoped = read_lock(scene)["components"]["a"]
                    full = self._full_entry(scene, "a")
                    self.assertEqual(canonical(scoped), canonical(full))

    def test_every_metadata_field_and_facet_is_present_and_refreshed(self):
        """The field surface is read from the module constant, not listed.

        Nine of the sixteen listed fields are written only when they carry
        something - a provider error list, vendored copies - so an entry for a
        clean path-hash component holds eight of them. The comparison is over
        the constant either way, and the key sets are compared whole, so a
        field that appeared in one document and not the other would fail here
        whether or not the constant happens to name it.
        """
        with self._drifted() as scene:
            stale = read_lock(scene)["components"]["a"]
            result = run_cli(
                scene.root, "verify", "--components", "a",
                "--facets", "boundary", "--update",
            )
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            scoped = read_lock(scene)["components"]["a"]
            full = self._full_entry(scene, "a")
            self.assertEqual(set(scoped), set(full))
            written = [field for field in COMPONENT_METADATA_FIELDS if field in full]
            self.assertEqual(
                written,
                [field for field in COMPONENT_METADATA_FIELDS if field in scoped],
            )
            self.assertEqual(
                written,
                [
                    "version", "path", "boundary_provider",
                    "boundary_provider_version", "boundary_status", "semver",
                    "consumers", "external_consumers",
                ],
            )
            for field in COMPONENT_METADATA_FIELDS:
                with self.subTest(field=field):
                    self.assertEqual(scoped.get(field), full.get(field))
            for facet in ("exact", "behavior", "boundary", "compat"):
                with self.subTest(facet=facet):
                    self.assertEqual(
                        scoped["fingerprints"][facet], full["fingerprints"][facet]
                    )
            # Both facets the drift moved really did move, so the equality
            # above is not the equality of two unchanged entries.
            self.assertNotEqual(stale["fingerprints"]["exact"],
                                scoped["fingerprints"]["exact"])
            self.assertNotEqual(stale["fingerprints"]["boundary"],
                                scoped["fingerprints"]["boundary"])

    def test_an_optional_metadata_field_rides_along_with_the_scoped_write(self):
        """One of the nine conditional fields, actually populated.

        `implicit` with no declared paths records a `boundary_errors` list, so
        this is the case where the entry carries a metadata field the clean
        path-hash fixture never produces.
        """
        with with_committed_lock(scoped_repository()) as scene:
            scene.component("w", path="w", provider="implicit")
            scene.file("w/main.py", "x = 1\n")
            scene.commit("add an implicit component")
            self.assertEqual(run_cli(scene.root, "generate").returncode, EXIT_OK)
            scene.commit("lock")
            scene.file("w/main.py", "x = 2\n")
            scene.commit("drift w")
            result = run_cli(scene.root, "verify", "--components", "w", "--update")
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            scoped = read_lock(scene)["components"]["w"]
            self.assertEqual(
                scoped["boundary_errors"],
                ["No boundary paths declared for implicit boundary"],
            )
            self.assertEqual(canonical(scoped), canonical(self._full_entry(scene, "w")))

    def test_the_entry_comparison_rejects_an_update_mask(self):
        """Premise for the two tests above: the comparison can fail.

        A mask would keep the stale `exact` digest beside the fresh `boundary`
        one. Built by hand, that entry must not compare equal to the entry a
        full generate produces, or nothing above is being checked.
        """
        with self._drifted() as scene:
            stale = read_lock(scene)["components"]["a"]
            full = self._full_entry(scene, "a")
            masked = json.loads(canonical(full))
            masked["fingerprints"]["exact"] = stale["fingerprints"]["exact"]
            self.assertNotEqual(canonical(masked), canonical(full))
            self.assertEqual(
                masked["fingerprints"]["boundary"], full["fingerprints"]["boundary"]
            )


#: Four components and two slices, so a removal can empty neither slice and a
#: scoped selection can miss one entirely.
PROPERTY_NAMES = ("a", "b", "c", "d")

MODEL_PROFILE = settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


class ScopedGenerationModelTests(unittest.TestCase):
    """OBL-HASHING-099 (and OBL-HASHING-098 as a document): scoped equals full.

    The oracle is a full `generate` on the same source, because that is the
    reference the obligation names. Running against the working-tree source
    keeps an example to three hashing passes and no commit.
    """

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario("model")
        for name in PROPERTY_NAMES:
            cls.scene.component(
                name, path=name, provider="path-hash", boundary=["api/*.yaml"]
            )
            cls.scene.file(f"{name}/api/v1.yaml", f"openapi: 3.1.0\ninfo: {name}\n")
            cls.scene.file(f"{name}/impl.py", f"# {name}\n")
        cls.scene.slice("all", mode="exact", components=list(PROPERTY_NAMES))
        cls.scene.slice("pair", mode="boundary", components=["a", "b"])
        cls.scene.commit()

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def _pristine(self) -> Dict[str, Any]:
        """Reset every tracked file and return a fresh copy of the config."""
        for name in PROPERTY_NAMES:
            (self.scene.root / name / "api" / "v1.yaml").write_text(
                f"openapi: 3.1.0\ninfo: {name}\n", encoding="utf-8"
            )
            (self.scene.root / name / "impl.py").write_text(
                f"# {name}\n", encoding="utf-8"
            )
        config = json.loads(json.dumps(self.scene.config))
        self._write_config(config)
        return config

    def _write_config(self, config: Dict[str, Any]) -> None:
        (self.scene.root / "boundary.config.json").write_text(
            json.dumps(config, indent=2) + "\n", encoding="utf-8"
        )

    def _base_lock(self, config: Dict[str, Any]) -> dict:
        base = generate_lockfile(config, self.scene.root, source="working-tree")
        (self.scene.root / LOCK).write_text(dump_lockfile(base), encoding="utf-8")
        return base

    def _apply(self, drift: Iterable[str], removed: Optional[str]) -> Dict[str, Any]:
        config = self._pristine()
        base = self._base_lock(config)
        for name in sorted(drift):
            (self.scene.root / name / "impl.py").write_text(
                f"# {name} drifted\n", encoding="utf-8"
            )
        if removed is not None:
            del config["components"][removed]
            for definition in config["slices"].values():
                definition["components"] = [
                    member
                    for member in definition["components"]
                    if member != removed
                ]
            self._write_config(config)
        self._base = base
        return config

    def _scoped(self, config: Dict[str, Any], selection: Iterable[str]) -> dict:
        return generate_lockfile_for_components(
            config,
            self.scene.root,
            sorted(selection),
            self.scene.root / LOCK,
            source="working-tree",
        )

    @given(
        drift=st.sets(st.sampled_from(PROPERTY_NAMES), max_size=4),
        extra=st.sets(st.sampled_from(PROPERTY_NAMES), max_size=4),
        removed=st.sampled_from((None,) + PROPERTY_NAMES),
    )
    @MODEL_PROFILE
    def test_a_scoped_generation_equals_a_full_one_document_for_document(
        self, drift: Set[str], extra: Set[str], removed: Optional[str]
    ):
        configured = set(PROPERTY_NAMES) - ({removed} if removed else set())
        config = self._apply(drift, removed)
        selection = (drift | extra) & configured
        scoped = self._scoped(config, selection)
        full = generate_lockfile(config, self.scene.root, source="working-tree")
        self.assertEqual(canonical(scoped), canonical(full))
        self.assertEqual(set(scoped["components"]), configured)
        self.assertEqual(set(scoped["slices"]), set(config["slices"]))
        self.assertEqual(scoped["config_digest"], semantic_config_digest(config))
        if removed is not None:
            self.assertNotEqual(scoped["config_digest"], self._base["config_digest"])

    def test_the_document_comparison_rejects_an_update_mask(self):
        """Premise: a merge that kept old slices and one stale facet fails.

        Without this the property above could be comparing a document to
        itself. The mask is built from the real base lock and the real scoped
        result, so it is the exact wrong answer OBL-HASHING-099 describes.
        """
        config = self._apply({"a"}, None)
        scoped = self._scoped(config, {"a"})
        masked = json.loads(canonical(scoped))
        masked["slices"] = json.loads(canonical(self._base["slices"]))
        self.assertNotEqual(canonical(masked), canonical(scoped))
        masked = json.loads(canonical(scoped))
        masked["components"]["a"]["fingerprints"]["exact"] = (
            self._base["components"]["a"]["fingerprints"]["exact"]
        )
        self.assertNotEqual(canonical(masked), canonical(scoped))

    def test_the_slice_set_is_rebuilt_from_the_config_not_carried_over(self):
        """A slice dropped from the config must not survive a scoped write."""
        config = self._apply({"a"}, None)
        del config["slices"]["pair"]
        config["slices"]["extra"] = {"mode": "exact", "components": ["b", "c"]}
        self._write_config(config)
        scoped = self._scoped(config, {"a"})
        self.assertEqual(sorted(self._base["slices"]), ["all", "pair"])
        self.assertEqual(sorted(scoped["slices"]), ["all", "extra"])
        full = generate_lockfile(config, self.scene.root, source="working-tree")
        self.assertEqual(canonical(scoped), canonical(full))

    def test_a_removed_component_is_dropped_from_entries_and_from_slices(self):
        config = self._apply({"a"}, "d")
        scoped = self._scoped(config, {"a"})
        self.assertIn("d", self._base["components"])
        self.assertNotIn("d", scoped["components"])
        self.assertNotIn("d", scoped["slices"]["all"]["components"])
        self.assertNotIn("d", scoped["slices"]["all"]["component_digests"])


#: The four incoherent base locks OBL-LOCKFILE-035 names, and the diagnostic
#: each command answers with. `generate --components` reports the refusal
#: raised inside the scoped merge; `verify --components --update` never gets
#: that far, because core's own preflight rejects the same lock first.
BASE_LOCK_PRECONDITIONS: Dict[str, Dict[str, Any]] = {
    "schema is not v4": {
        "mutate": lambda d: d.__setitem__("schema", "boundary-lock/v2"),
        "generate": (
            "ERROR: Cannot partially update schema 'boundary-lock/v2'; run a "
            "full `boundver generate` to create boundary-lock/v4.\n"
        ),
        "verify": (
            "ERROR: lockfile preflight failed:\n"
            "  - LOCKFILE schema unsupported: boundary-lock/v2 "
            "(expected boundary-lock/v4)\n"
        ),
    },
    "structure is malformed": {
        "mutate": lambda d: d["components"]["a"].__setitem__("fingerprints", "nope"),
        "generate": (
            "ERROR: Cannot partially update the selected lockfile:"
            "\\nLOCKFILE malformed: component 'a' missing fingerprints object\n"
        ),
        "verify": (
            "ERROR: lockfile preflight failed:\n"
            "  - LOCKFILE malformed: component 'a' missing fingerprints object\n"
        ),
    },
    "project was renamed": {
        "mutate": lambda d: d.__setitem__("project", "renamed"),
        "generate": (
            "ERROR: Cannot partially update after the project name changed. "
            "Run a full `boundver generate`.\n"
        ),
        "verify": (
            "ERROR: lockfile preflight failed:\n"
            "  - METADATA MISMATCH project: lockfile='renamed' current='scoped'\n"
            "  - LOCKFILE scoped --update cannot repair global component or "
            "slice preflight issues; rerun without --components after "
            "reviewing the full lock\n"
        ),
    },
    "a configured component has no entry": {
        "mutate": lambda d: d["components"].pop("c"),
        "generate": (
            "ERROR: Cannot partially generate because the existing lockfile "
            "has no entry for: c. Run a full `boundver generate`.\n"
        ),
        "verify": (
            "ERROR: lockfile preflight failed:\n"
            "  - LOCKFILE component set differs from config: "
            "locked=['a', 'b'] configured=['a', 'b', 'c']\n"
            "  - LOCKFILE scoped --update cannot repair global component or "
            "slice preflight issues; rerun without --components after "
            "reviewing the full lock\n"
        ),
    },
}


class IncoherentBaseLockRefusalTests(unittest.TestCase):
    """OBL-LOCKFILE-035: four ways a scoped merge must decline its base."""

    def _mutated(self, mutate) -> Scenario:
        scene = with_committed_lock(scoped_repository())
        document = read_lock(scene)
        mutate(document)
        write_lock(scene, document)
        scene.commit("mutate the lock")
        return scene

    def test_both_scoped_commands_write_when_the_base_lock_is_coherent(self):
        """Premise: neither command is inert on this repository."""
        with with_committed_lock(scoped_repository()) as scene:
            scene.file("a/impl.py", "# a drifted\n")
            scene.commit("drift a")
            before = (scene.root / LOCK).read_bytes()
            generated = run_cli(scene.root, "generate", "--components", "a")
            self.assertEqual(generated.returncode, EXIT_OK, generated.stderr)
            self.assertNotEqual(before, (scene.root / LOCK).read_bytes())

            scene.commit("accept")
            scene.file("a/impl.py", "# a drifted again\n")
            scene.commit("drift a again")
            before = (scene.root / LOCK).read_bytes()
            verified = run_cli(scene.root, "verify", "--components", "a", "--update")
            self.assertEqual(verified.returncode, EXIT_OK, verified.stderr)
            self.assertNotEqual(before, (scene.root / LOCK).read_bytes())

    def test_each_precondition_refuses_with_its_own_message_and_no_write(self):
        for label, case in BASE_LOCK_PRECONDITIONS.items():
            with self.subTest(precondition=label):
                with self._mutated(case["mutate"]) as scene:
                    before = (scene.root / LOCK).read_bytes()
                    generated = run_cli(scene.root, "generate", "--components", "a")
                    self.assertEqual(generated.returncode, EXIT_USAGE)
                    self.assertEqual(
                        generated.stderr, case["generate"] + GENERATE_HINT + "\n"
                    )
                    self.assertEqual(before, (scene.root / LOCK).read_bytes())

                    scene.file("a/impl.py", "# a drifted\n")
                    scene.commit("drift a")
                    before = (scene.root / LOCK).read_bytes()
                    verified = run_cli(
                        scene.root, "verify", "--components", "a", "--update"
                    )
                    self.assertEqual(verified.returncode, EXIT_USAGE)
                    self.assertEqual(error_tail(verified.stderr), case["verify"])
                    self.assertEqual(before, (scene.root / LOCK).read_bytes())

    def test_the_four_refusal_messages_are_distinct_from_one_another(self):
        """Distinctness is the point of the obligation, so assert it directly."""
        for command in ("generate", "verify"):
            with self.subTest(command=command):
                messages = [case[command] for case in BASE_LOCK_PRECONDITIONS.values()]
                self.assertEqual(len(set(messages)), len(messages))

    def test_a_missing_lock_is_refused_without_creating_the_output_file(self):
        # No `generate` has ever run here, so the lock is absent from the
        # working tree *and* from the head source the scoped merge reads.
        with scoped_repository() as scene:
            self.assertFalse((scene.root / LOCK).exists())
            result = run_cli(scene.root, "generate", "--components", "a")
            self.assertEqual(result.returncode, EXIT_USAGE)
            self.assertEqual(
                result.stderr,
                "ERROR: Cannot generate a component subset without an existing "
                "boundary-lock/v4 lockfile in the selected head source. Run a "
                "full `boundver generate` first.\n" + GENERATE_HINT + "\n",
            )
            self.assertFalse((scene.root / LOCK).exists())

    def test_missing_entry_refusal_covers_selected_and_unselected_entries(self):
        """A scoped refresh requires a coherent entry for every component."""
        with with_committed_lock(scoped_repository()) as scene:
            base = generate_lockfile(scene.config, scene.root, source="working-tree")
            shapes: Dict[str, Callable[[dict], None]] = {
                "an unselected entry removed": lambda d: d["components"].pop("c"),
                "the selected entry removed": lambda d: d["components"].pop("a"),
                "components replaced by a list": (
                    lambda d: d.__setitem__("components", [])
                ),
                "components emptied": lambda d: d.__setitem__("components", {}),
            }
            observed: Dict[str, str] = {}
            for label, mutate in shapes.items():
                document = json.loads(canonical(base))
                mutate(document)
                try:
                    generate_lockfile_for_components(
                        config=scene.config,
                        repo_root=scene.root,
                        selected_components=["a"],
                        out_path=scene.root / LOCK,
                        source="working-tree",
                        existing_lockfile=document,
                    )
                except ConfigError as exc:
                    observed[label] = str(exc)
                else:
                    observed[label] = ""
            missing = (
                "Cannot partially generate because the existing lockfile has "
                "no entry for: {names}. Run a full `boundver generate`."
            )
            self.assertEqual(
                observed["an unselected entry removed"], missing.format(names="c")
            )
            self.assertEqual(
                observed["the selected entry removed"], missing.format(names="a")
            )
            self.assertEqual(
                observed["components replaced by a list"],
                "Cannot partially update the selected lockfile:\n"
                "LOCKFILE malformed: components must be an object",
            )
            self.assertEqual(
                observed["components emptied"],
                missing.format(names="a, b, c"),
            )


class ChangedFromWithComponentsTests(unittest.TestCase):
    """OBL-LOCKFILE-036: adding --changed-from drops the scope silently."""

    def _repository(self) -> Scenario:
        """Lock committed at `base`, then 'b' and 'c' drift; 'a' does not."""
        scene = scoped_repository()
        scene.slice("all", mode="exact", components=["a", "b", "c"])
        scene.commit("slice")
        with_committed_lock(scene)
        scene.base = scene.head()
        scene.file("b/impl.py", "# b drifted\n")
        scene.file("c/impl.py", "# c drifted\n")
        scene.commit("drift b and c")
        return scene

    def _observation_only_repository(self) -> Scenario:
        """Two components whose gated facets are current and whose exact drifts.

        Narrowing verify_facets to boundary means an implementation edit moves
        the exact digest without producing a gated issue, so verify reports
        observations and no issues. That is the only way into the second
        update call site.
        """
        scene = Scenario("observation-only")
        for name in ("a", "b"):
            scene.component(
                name, path=name, provider="path-hash", boundary=["api.txt"]
            )
            scene.file(f"{name}/api.txt", "contract\n")
            scene.file(f"{name}/impl.py", "x = 1\n")
            scene.config["components"][name]["verify_facets"] = ["boundary"]
        scene.write_config()
        scene.commit("declare")
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("lock")
        scene.base = scene.head()
        scene.file("a/impl.py", "x = 2\n")
        scene.file("b/impl.py", "x = 2\n")
        scene.commit("drift both implementations")
        return scene

    def test_the_observation_only_update_regenerates_the_whole_lock(self):
        """Adding --changed-from drops the scope on this arm too.

        The covered arm is the one reached when verify finds a gated issue.
        This is the other: every gated facet is current, two components have
        drifted somewhere ungated, and `--components a --changed-from base
        --update` must still refresh both. Under MUT-B7-17 it refreshes only
        'a', leaves 'b' stale, and the run exits 2 instead of 0.
        """
        with self._observation_only_repository() as scene:
            result = run_cli(
                scene.root, "verify", "--source", "head", "--components", "a",
                "--changed-from", scene.base, "--update", "--format", "json",
            )
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            scene.commit("relock")

            after = run_cli(
                scene.root, "verify", "--source", "head", "--format", "json"
            )
            payload = json.loads(after.stdout)
            self.assertEqual(payload["observations"], [])
            self.assertEqual(payload["issues"], [])

    def test_that_repository_really_reports_observations_and_no_issues(self):
        """The premise: without it the test above proves nothing.

        If the fixture produced a gated issue, verify would take the drift arm
        that is already covered, and the assertion above would pass without
        ever reaching the branch it names.
        """
        with self._observation_only_repository() as scene:
            result = run_cli(
                scene.root, "verify", "--source", "head", "--format", "json"
            )
            payload = json.loads(result.stdout)
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            self.assertEqual(payload["issues"], [])
            self.assertEqual(len(payload["observations"]), 2)
            self.assertTrue(
                all("exact" in text for text in payload["observations"]),
                payload["observations"],
            )

    def test_the_reported_selection_is_the_intersection_of_flag_and_diff(self):
        with self._repository() as scene:
            result = run_cli(
                scene.root, "verify", "--components", "a,b",
                "--changed-from", scene.base, "--format", "json",
            )
            self.assertEqual(result.returncode, EXIT_DRIFT, result.stderr)
            payload = json.loads(result.stdout)
            # 'a' was selected but the diff did not touch it; 'c' was touched
            # but not selected. Neither may appear.
            self.assertEqual(payload["changed_components"], ["b"])
            self.assertEqual(payload["components_filter"], [])

    def test_the_full_lock_is_verified_even_though_the_scope_named_two(self):
        with self._repository() as scene:
            result = run_cli(
                scene.root, "verify", "--components", "a,b",
                "--changed-from", scene.base, "--format", "json",
            )
            issues = json.loads(result.stdout)["issues"]
            prefixes = sorted(issue.split(":")[0] for issue in issues)
            self.assertEqual(
                prefixes,
                ["MISMATCH b.exact", "MISMATCH c.exact", "SLICE MISMATCH all.exact"],
            )

    def test_the_text_view_announces_that_it_validates_the_full_lock(self):
        with self._repository() as scene:
            result = run_cli(
                scene.root, "verify", "--components", "a,b",
                "--changed-from", scene.base,
            )
            self.assertEqual(
                result.stdout.splitlines()[0],
                "Changed component paths (1): b; validating full lock integrity.",
            )

    def test_without_changed_from_the_same_update_takes_the_scoped_path(self):
        """Premise: the scoped branch is live and refuses on stale 'c'."""
        with self._repository() as scene:
            before = (scene.root / LOCK).read_bytes()
            result = run_cli(scene.root, "verify", "--components", "a,b", "--update")
            self.assertEqual(result.returncode, EXIT_USAGE, result.stdout)
            self.assertEqual(result.stderr, UNSELECTED_STALE)
            self.assertEqual(before, (scene.root / LOCK).read_bytes())

    def test_adding_changed_from_converts_the_update_into_a_full_generate(self):
        with self._repository() as scene:
            result = run_cli(
                scene.root, "verify", "--components", "a,b",
                "--changed-from", scene.base, "--update",
            )
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            self.assertEqual(result.stderr, "")
            updated = read_lock(scene)
            # 'c' was never named on the command line and was stale a moment
            # ago; the full generate blessed it anyway.
            full = generate_lockfile(scene.config, scene.root, source="head")
            self.assertEqual(canonical(updated), canonical(full))
            self.assertIn("MISMATCH c.exact", result.stdout)


if __name__ == "__main__":
    unittest.main()
