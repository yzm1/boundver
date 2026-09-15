"""Six places where boundver decides how far to trust a provider.

The obligations gathered here all sit on the same seam. A boundary provider is
extension code, and every one of these promises is about a moment where
boundver either hands it something, believes what it hands back, or refuses to
let it speak at all. Range review must refuse to present RFC 6901 paths for a
provider that only hashed bytes. `_read_provider_file` must not believe an
accessor that ignores the limit it was given. `compute_boundary` must not
discard a built-in's hundred named failures because a validator disagrees with
the producer about where a list of errors stops. `why` must not run untrusted
explanation code on a component whose boundary never moved. And the published
facet policy must say what a slice actually gates, which is an any-member
question hiding next to an all-member one.

Three of them needed fixture work that a smaller test would have skipped. The
registry is read at runtime and each provider is classified by the entry labels
it publishes on one document that is simultaneously valid JSON, valid YAML and
a valid OpenAPI 3.1 contract - `file:` means it hashed bytes, `canonical:`
means it parsed them - so the raw set is derived rather than listed, and the
whole registry rides in a single committed range as one component per provider,
which turns thirteen repositories into one. Proving
`provider-implementation-changed` needed two modules defining the same provider
name as different classes, injected into `sys.modules` and declared by the base
and target configs respectively, because that branch compares
`type(base_provider)` with `type(target_provider)` and nothing else in the
suite makes those differ. And the byte-counter clause of OBL-PROVIDERS-033
needed a collector the test owns: `OpenApiCanonicalProvider._resolve` is the
one built-in that accepts an injected `_ProviderEntryCollector`, so the
counters can be read after the misbehaving accessor has been refused rather
than inferred from a digest.

OBL-PROVIDERS-057 turned out to be wrong rather than unmet, and that is the
main finding here. Its premise is that `analyze_component_drift` reaches the
guard `if provider is not None and not load_errors:` with a load failure in
hand and silently drops it, leaving an operator staring at a boundary change
with no Provider detail line. It does not. Every failure the obligation names -
a missing or unknown `provider` key, a custom provider without
`--allow-custom-providers`, an unimportable provider module - is fatal inside
the `generate_lockfile` call sixty lines earlier, so the function prints
`ERROR: could not compute current fingerprints:` with the specific reason and
returns None, and `why` exits 2 having printed nothing to stdout. The
discarded `load_errors` local is dead, not dangerous. The tests below pin all
six failure modes to that loud outcome and pin the ordering that makes it loud,
so the silent-degradation hazard the register imagined would announce itself if
a future change ever made fingerprint computation tolerant.

Covers OBL-PROVIDERS-027, OBL-PROVIDERS-033, OBL-PROVIDERS-035,
OBL-PROVIDERS-056, OBL-PROVIDERS-057 and OBL-CONFIG-030.
"""

from __future__ import annotations

import ast
import copy
import inspect
import io
import json
import sys
import textwrap
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from unittest import mock

from boundver import providers
from boundver._facet_policy import facet_policy_payload
from boundver._hashing import _ModeAwareBytes
from boundver._lockfile import dump_lockfile, generate_lockfile
from boundver._output import analyze_component_drift
from boundver._provider_diff import STRUCTURAL_DIFF_INTERFACE
from boundver._review import analyze_review_range
from boundver._utils import ProviderError
from boundver.providers import (
    MAX_PROVIDER_ERRORS,
    OpenApiCanonicalProvider,
    PathHashProvider,
    ProviderContext,
    ResolvedBoundary,
    _ProviderEntryCollector,
    _read_provider_file,
    compute_boundary,
    create_registry,
    load_custom_providers,
)

from tests._parity import run_cli
from tests._scenarios import Scenario

#: The claim string every structural report carries, whatever its outcome.
STRUCTURAL_CLAIM = "structural-explanation-only"

#: A YAML/OpenAPI document small enough to reason about byte counts over.
OPENAPI_YAML = (
    b"openapi: 3.1.0\n"
    b"info:\n"
    b"  title: t\n"
    b"  version: '1'\n"
    b"paths: {}\n"
)


class _LyingBytes(bytes):
    """A bytes subclass that is not ``_ModeAwareBytes``.

    The exact-type allowlist in ``_read_provider_file`` exists because a
    subclass can override ``__len__`` and lie to the post-hoc size check. This
    one does not need to lie: being the wrong exact type is the whole test.
    """


def _contract(extended: bool) -> dict:
    """One document every registered provider resolves without error.

    It is valid JSON, valid YAML and a valid OpenAPI 3.1 contract at once, so
    the raw providers hash it, the canonical providers parse it, and every
    member of the registry can be driven through the same range.
    """
    paths: Dict[str, Any] = {
        "/a": {"get": {"responses": {"200": {"description": "ok"}}}}
    }
    if extended:
        paths["/b"] = {"get": {"responses": {"204": {"description": "none"}}}}
    return {
        "openapi": "3.1.0",
        "info": {"title": "svc", "version": "1"},
        "paths": paths,
    }


def _commit_endpoint(
    scene: Scenario,
    message: str,
    *,
    allow_custom_providers: bool = False,
) -> str:
    """Commit config and lockfile together and return the commit id.

    Range review reads an immutable committed lockfile at each endpoint, which
    ``Scenario.commit`` alone does not produce.
    """
    scene.write_config()
    scene.git("add", "--all")
    lockfile = generate_lockfile(
        scene.config,
        scene.root,
        source="index",
        allow_custom_providers=allow_custom_providers,
    )
    (scene.root / "boundary.lock.json").write_text(
        dump_lockfile(lockfile), encoding="utf-8"
    )
    scene.git("add", "--all")
    scene.git("commit", "-m", message)
    return scene.head()


def _probe_context(content: bytes = b"") -> ProviderContext:
    """A context over one in-memory file, for classifying providers."""
    document = content or json.dumps(_contract(False)).encode("utf-8")
    files = {"svc/contract.json": document}
    return ProviderContext(
        repo_root=Path("/repo"),
        component_path="svc",
        boundary_cfg={"paths": ["contract.json"]},
        source="working-tree",
        read_file=lambda path: files[path],
        list_files=lambda prefix: sorted(
            path
            for path in files
            if path == prefix or path.startswith(prefix.rstrip("/") + "/")
        ),
    )


def _label_prefixes(provider: object) -> Set[str]:
    """The entry-label prefixes a provider publishes for the fixture document.

    ``file:`` means the provider hashed bytes and never parsed them, which is
    what "raw" means in the documented promise. ``canonical:`` means it parsed.
    An empty set means the provider publishes no boundary at all.
    """
    resolved = provider.resolve(_probe_context())
    return {label.split(":", 1)[0] for label, _ in resolved.entries}


class RawProviderRangeReviewTests(unittest.TestCase):
    """OBL-PROVIDERS-027: every raw provider, not one of them.

    One range carries a component per registry key, so a provider added
    tomorrow gets a component, a report and an assertion without anyone
    editing this file.
    """

    registry: Dict[str, Any]
    component_for: Dict[str, str]
    raw: Set[str]
    canonical: Set[str]
    silent: Set[str]
    reports: Dict[str, dict]
    result: dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = create_registry()
        names = sorted(cls.registry)
        cls.component_for = {name: f"c{index:02d}" for index, name in enumerate(names)}
        cls.raw, cls.canonical, cls.silent = set(), set(), set()
        for name in names:
            prefixes = _label_prefixes(cls.registry[name])
            if prefixes == {"file"}:
                cls.raw.add(name)
            elif prefixes == {"canonical"}:
                cls.canonical.add(name)
            elif not prefixes:
                cls.silent.add(name)

        cls.scene = Scenario()
        for name in names:
            component = cls.component_for[name]
            cls.scene.component(
                component,
                path=component,
                provider=name,
                boundary=[] if name == "leaf" else ["contract.json"],
            )
            cls.scene.json_file(f"{component}/contract.json", _contract(False))
        base = _commit_endpoint(cls.scene, "base")
        for name in names:
            cls.scene.json_file(
                f"{cls.component_for[name]}/contract.json", _contract(True)
            )
        target = _commit_endpoint(cls.scene, "target")
        cls.result = analyze_review_range(cls.scene.root, base, target)
        cls.reports = {
            report["component"]: report
            for report in cls.result["structural_changes"]["reports"]
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def _report(self, name: str) -> Optional[dict]:
        return self.reports.get(self.component_for[name])

    def test_every_registered_provider_is_classified_by_the_bytes_it_publishes(self):
        """Premise: the classification really partitions the live registry.

        Every assertion in this class is quantified over a set derived here.
        If the derivation dropped a member into no class, the quantified tests
        below would pass by covering nothing, so the partition is checked
        before it is used.
        """
        classified = self.raw | self.canonical | self.silent
        self.assertEqual(classified, set(self.registry))
        self.assertEqual(self.raw & self.canonical, set())
        self.assertEqual(self.raw & self.silent, set())
        self.assertEqual(self.canonical & self.silent, set())
        self.assertGreaterEqual(
            len(self.raw), 8, f"raw providers found: {sorted(self.raw)}"
        )
        for alias, target in sorted(providers._ALIASES.items()):
            with self.subTest(alias=alias):
                self.assertIn(alias, self.raw)
                self.assertIn(target, self.raw)
                self.assertIs(self.registry[alias], self.registry[target])

    def test_the_range_moves_the_boundary_of_every_provider_that_publishes_one(self):
        """Premise: each report below exists because a boundary really moved.

        "No document rows" says nothing if no comparison was attempted. This
        pins that every publishing provider produced a boundary-facet
        transition and therefore a report, and that the only providers without
        a report are the ones that publish no boundary at all.
        """
        boundary_changed = {
            component["name"]
            for component in self.result["components"]["changed"]
            if any(item["facet"] == "boundary" for item in component["facets"])
        }
        expected = {
            self.component_for[name]
            for name in self.raw | self.canonical
        }
        self.assertEqual(boundary_changed, expected)
        self.assertEqual(set(self.reports), expected)
        for name in sorted(self.silent):
            with self.subTest(provider=name):
                self.assertIsNone(self._report(name))

    def test_openapi_canonical_is_the_only_provider_that_produces_document_rows(self):
        """Premise for every emptiness asserted below, and the docs' claim.

        The set is derived from the interface each registered provider
        publishes, then pinned to the single name ``docs/reference.md`` calls
        "the first built-in that implements this optional interface". A second
        structural provider makes this fail, which is the point: the sentence
        in the docs has to be revised at the same time as the code.
        """
        declares_interface = {
            name
            for name, provider in self.registry.items()
            if getattr(provider, "structural_diff_interface", None)
            == STRUCTURAL_DIFF_INTERFACE
            and callable(getattr(provider, "structural_diff", None))
        }
        produced_rows = {
            name
            for name in self.registry
            if (self._report(name) or {}).get("documents")
        }
        self.assertEqual(produced_rows, declares_interface)
        self.assertEqual(produced_rows, {"openapi-canonical"})

        report = self._report("openapi-canonical")
        self.assertEqual(report["status"], "complete")
        self.assertIsNone(report["reason"])
        self.assertTrue(report["complete"])
        self.assertEqual(
            report["documents"],
            [
                {
                    "label": "canonical:contract.json",
                    "status": "changed",
                    "changes": [
                        {
                            "kind": "added",
                            "path": "/paths/~1b",
                            "before_type": None,
                            "after_type": "object",
                        }
                    ],
                }
            ],
        )
        self.assertEqual(
            report["summary"], {"added": 1, "removed": 0, "changed": 0}
        )

    def test_every_raw_provider_reports_provider_unsupported_with_no_rows(self):
        """The documented promise: raw providers are never parsed for review."""
        for name in sorted(self.raw):
            with self.subTest(provider=name):
                report = self._report(name)
                self.assertIsNotNone(report)
                self.assertEqual(report["status"], "unavailable")
                self.assertEqual(report["reason"], "provider-unsupported")
                self.assertEqual(
                    report["detail"],
                    f"Provider {name!r} does not expose bounded structural "
                    "diff output",
                )
                self.assertEqual(report["documents"], [])
                self.assertEqual(
                    report["summary"], {"added": 0, "removed": 0, "changed": 0}
                )
                self.assertFalse(report["complete"])
                self.assertFalse(report["truncated"])
                self.assertEqual(report["interface"], STRUCTURAL_DIFF_INTERFACE)
                self.assertEqual(report["claim"], STRUCTURAL_CLAIM)

    def test_a_canonical_provider_without_the_interface_is_also_unsupported(self):
        """`json-canonical` parses bytes but publishes no structural diff.

        The obligation names raw providers, but the refusal is keyed on the
        interface, not on rawness, and this is the member that separates the
        two readings.
        """
        for name in sorted(self.canonical - {"openapi-canonical"}):
            with self.subTest(provider=name):
                report = self._report(name)
                self.assertEqual(report["reason"], "provider-unsupported")
                self.assertEqual(report["documents"], [])

    def test_one_unsupported_report_makes_the_whole_range_incomplete(self):
        """Completeness is aggregated, so a refusal is visible at the top."""
        self.assertFalse(self.result["structural_changes"]["complete"])
        self.assertFalse(self.result["structural_changes"]["truncated"])
        self.assertEqual(
            self.result["structural_changes"]["claim"], STRUCTURAL_CLAIM
        )


#: One provider class per module, identical in name and version, different in
#: type. Two spellings of the same source is exactly what makes
#: ``type(base_provider) is not type(target_provider)`` true.
_PROBE_SOURCE = """
from boundver.providers import PathHashProvider


class Probe(PathHashProvider):
    name = "custom.probe"
    version = "1"
"""

_PROBE_MODULES = ("boundver_chunk38_alpha", "boundver_chunk38_beta")


class ProviderImplementationChangeTests(unittest.TestCase):
    """OBL-PROVIDERS-027: the same recorded name, two different classes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.previous = {
            name: sys.modules.get(name) for name in _PROBE_MODULES
        }
        for name in _PROBE_MODULES:
            module = types.ModuleType(name)
            exec(compile(_PROBE_SOURCE, name, "exec"), module.__dict__)
            sys.modules[name] = module

    @classmethod
    def tearDownClass(cls) -> None:
        for name, module in cls.previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_the_two_probe_modules_define_genuinely_different_classes(self):
        """Premise: without this the branch under test cannot be reached."""
        alpha, beta = (sys.modules[name].Probe for name in _PROBE_MODULES)
        self.assertIsNot(alpha, beta)
        self.assertEqual(alpha.name, beta.name)
        self.assertEqual(alpha.version, beta.version)
        self.assertIsNot(type(alpha()), type(beta()))

    def _review_with(self, target_module: str) -> dict:
        with Scenario() as scene:
            scene.component(
                "svc", path="svc", provider="custom.probe", boundary=["c.json"]
            )
            scene.config["providers"] = [
                {"module": _PROBE_MODULES[0], "class": "Probe"}
            ]
            scene.json_file("svc/c.json", {"a": 1})
            base = _commit_endpoint(scene, "base", allow_custom_providers=True)
            scene.config["providers"] = [
                {"module": target_module, "class": "Probe"}
            ]
            scene.json_file("svc/c.json", {"a": 2})
            target = _commit_endpoint(scene, "target", allow_custom_providers=True)
            result = analyze_review_range(
                scene.root, base, target, allow_custom_providers=True
            )
        return result["structural_changes"]["reports"][0]

    def test_one_class_at_both_endpoints_is_refused_for_lacking_the_interface(self):
        """Premise: the mismatch reason is chosen, not the only reason here.

        The probe provider subclasses ``PathHashProvider`` and so is
        unsupported either way. Loading it from one module at both endpoints
        shows which refusal the range reports when the implementations agree,
        so the reason below is attributable to the implementations differing.
        """
        report = self._review_with(_PROBE_MODULES[0])
        self.assertEqual(report["reason"], "provider-unsupported")
        self.assertEqual(
            report["detail"],
            "Provider 'custom.probe' does not expose bounded structural "
            "diff output",
        )
        self.assertEqual(report["documents"], [])

    def test_two_classes_for_one_name_report_provider_implementation_changed(self):
        report = self._review_with(_PROBE_MODULES[1])
        self.assertEqual(report["reason"], "provider-implementation-changed")
        self.assertEqual(
            report["detail"],
            "Structural comparison requires the same provider implementation "
            "at both endpoints",
        )
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["documents"], [])
        self.assertEqual(
            report["summary"], {"added": 0, "removed": 0, "changed": 0}
        )
        self.assertFalse(report["complete"])
        self.assertEqual(
            report["inputs"]["base"]["provider"],
            report["inputs"]["target"]["provider"],
        )
        self.assertEqual(
            report["inputs"]["base"]["provider_version"],
            report["inputs"]["target"]["provider_version"],
        )


#: Every shape a host accessor can hand back that ``_read_provider_file`` must
#: refuse, mapped to the message it must refuse it with. The limit is 4 bytes,
#: so ``b"x" * 5`` is limit+1.
ACCESSOR_REJECTIONS = {
    "limit ignored by one byte": (
        b"x" * 5,
        "source content for svc/c.yaml exceeds the 4-byte remaining limit",
    ),
    "str": ("xxxx", "source accessor returned non-bytes content"),
    "bytearray": (bytearray(b"xx"), "source accessor returned non-bytes content"),
    "memoryview": (memoryview(b"xx"), "source accessor returned non-bytes content"),
    "bytes subclass": (
        _LyingBytes(b"xx"),
        "source accessor returned non-bytes content",
    ),
}

#: The shapes that must pass, so the rejections above are not vacuous.
ACCESSOR_ACCEPTANCES = {
    "exact bytes at the limit": (b"xxxx", bytes, 4),
    "mode-aware bytes": (_ModeAwareBytes(b"xxxx", "100644"), _ModeAwareBytes, 4),
    "empty": (b"", bytes, 0),
}


class MisbehavingSourceAccessorTests(unittest.TestCase):
    """OBL-PROVIDERS-033: the accessor is host-injected and its limit advisory."""

    def _context(
        self,
        returns: object,
        *,
        limited: bool = True,
        recorder: Optional[List[int]] = None,
    ) -> ProviderContext:
        def read_file_limited(path: str, limit: int) -> object:
            if recorder is not None:
                recorder.append(limit)
            return returns

        return ProviderContext(
            repo_root=Path("/repo"),
            component_path="svc",
            boundary_cfg={"paths": ["c.yaml"]},
            source="working-tree",
            read_file=lambda path: returns,
            list_files=lambda prefix: ["svc/c.yaml"],
            read_file_limited=read_file_limited if limited else None,
        )

    def test_a_well_behaved_accessor_returns_its_content_unchanged(self):
        """Premise: the reader accepts what the contract allows.

        Every refusal below is an absence of a returned value. If the reader
        refused everything, the table would prove nothing about the guards it
        names.
        """
        for label, (value, kind, size) in ACCESSOR_ACCEPTANCES.items():
            for limited in (True, False):
                with self.subTest(shape=label, limit_aware=limited):
                    content = _read_provider_file(
                        self._context(value, limited=limited),
                        "svc/c.yaml",
                        max_bytes=4,
                    )
                    self.assertIs(type(content), kind)
                    self.assertEqual(len(content), size)

    def test_the_limit_aware_accessor_is_handed_the_ceiling_it_then_ignores(self):
        """Premise: the accessor really was told 4 and answered with 5.

        The obligation is about a limit that is advisory. That only means
        something if the advice was given, so the requested limit is recorded
        rather than assumed.
        """
        requested: List[int] = []
        with self.assertRaises(ProviderError):
            _read_provider_file(
                self._context(b"x" * 5, recorder=requested),
                "svc/c.yaml",
                max_bytes=4,
            )
        self.assertEqual(requested, [4])

    def test_every_misbehaving_accessor_answer_raises_provider_error(self):
        for label, (value, message) in ACCESSOR_REJECTIONS.items():
            for limited in (True, False):
                with self.subTest(shape=label, limit_aware=limited):
                    with self.assertRaises(ProviderError) as caught:
                        _read_provider_file(
                            self._context(value, limited=limited),
                            "svc/c.yaml",
                            max_bytes=4,
                        )
                    self.assertEqual(str(caught.exception), message)

    def test_the_post_hoc_length_check_is_the_only_guard_without_a_limit(self):
        """With no ``read_file_limited`` the unlimited read is still bounded.

        This is the same assertion the table makes, spelled once on its own
        because it is the clause the obligation singles out: on this path
        ``len(content) > limit`` is the sole thing holding the ceiling.
        """
        context = self._context(b"x" * 5, limited=False)
        self.assertIsNone(context.read_file_limited)
        with self.assertRaises(ProviderError) as caught:
            _read_provider_file(context, "svc/c.yaml", max_bytes=4)
        self.assertEqual(
            str(caught.exception),
            "source content for svc/c.yaml exceeds the 4-byte remaining limit",
        )

    def _openapi_context(self, returns: object) -> ProviderContext:
        return ProviderContext(
            repo_root=Path("/repo"),
            component_path="svc",
            boundary_cfg={"paths": ["c.yaml"]},
            source="working-tree",
            read_file=lambda path: returns,
            list_files=lambda prefix: ["svc/c.yaml"],
            read_file_limited=lambda path, limit: returns,
        )

    def _resolve_with_collector(
        self, returns: object
    ) -> Tuple[ResolvedBoundary, _ProviderEntryCollector]:
        collector = _ProviderEntryCollector(max_total_source_bytes=2048)
        resolved = OpenApiCanonicalProvider()._resolve(
            self._openapi_context(returns), collector=collector
        )
        return resolved, collector

    def test_a_well_behaved_read_advances_all_three_counters(self):
        """Premise: the counters move, so "did not move" is an observation.

        The three assertions in the next test are all absences. This one
        proves the same collector, driven through the same provider, records
        an entry and both byte totals when the accessor behaves.
        """
        resolved, collector = self._resolve_with_collector(OPENAPI_YAML)
        self.assertEqual(resolved.status, "ok")
        self.assertEqual(len(collector.entries), 1)
        self.assertEqual(collector.total_source_bytes, len(OPENAPI_YAML))
        self.assertGreater(collector.total_bytes, 0)

    def test_a_refused_read_advances_no_counter_and_retains_no_entry(self):
        cases = {
            "oversize": (
                b"x" * 4096,
                "OpenAPI canonicalization failed for c.yaml: source content "
                "for svc/c.yaml exceeds the 2048-byte remaining limit",
            ),
            "str": (
                OPENAPI_YAML.decode("ascii"),
                "OpenAPI canonicalization failed for c.yaml: source accessor "
                "returned non-bytes content",
            ),
            "bytes subclass": (
                _LyingBytes(OPENAPI_YAML),
                "OpenAPI canonicalization failed for c.yaml: source accessor "
                "returned non-bytes content",
            ),
        }
        for label, (value, message) in cases.items():
            with self.subTest(shape=label):
                resolved, collector = self._resolve_with_collector(value)
                self.assertEqual(resolved.status, "error")
                self.assertEqual(resolved.errors, [message])
                self.assertEqual(collector.entries, [])
                self.assertEqual(collector.total_bytes, 0)
                self.assertEqual(collector.total_source_bytes, 0)


#: Declared paths that match nothing, and what the built-in error list must
#: look like afterwards. 99 is the last count below saturation; 100 is where
#: the producer stops; above it nothing changes.
ERROR_SATURATION = {
    "one below the limit": (MAX_PROVIDER_ERRORS - 1, MAX_PROVIDER_ERRORS - 1, False),
    "exactly at the limit": (MAX_PROVIDER_ERRORS, MAX_PROVIDER_ERRORS, True),
    "one above the limit": (MAX_PROVIDER_ERRORS + 1, MAX_PROVIDER_ERRORS, True),
    "far above the limit": (250, MAX_PROVIDER_ERRORS, True),
}

SENTINEL = (
    f"Provider validation stopped after reaching the {MAX_PROVIDER_ERRORS}-error limit"
)


class _HostileErrorProvider:
    """A provider that returns one error more than the validator permits."""

    name = "custom.hostile"
    version = "1"

    def __init__(self, count: int) -> None:
        self.count = count

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        return ResolvedBoundary(
            status="error",
            errors=[f"synthetic error {index}" for index in range(self.count)],
        )


class SaturatedBuiltinErrorListTests(unittest.TestCase):
    """OBL-PROVIDERS-035: the producer's ``-1`` and the validator's ``>``."""

    def _context(self, paths: List[str]) -> ProviderContext:
        files = {"svc/present.json": b"{}\n"}
        return ProviderContext(
            repo_root=Path("/repo"),
            component_path="svc",
            boundary_cfg={"provider": "path-hash", "paths": list(paths)},
            source="working-tree",
            read_file=lambda path: files[path],
            list_files=lambda prefix: sorted(
                path
                for path in files
                if path == prefix or path.startswith(prefix.rstrip("/") + "/")
            ),
        )

    def _missing(self, count: int) -> List[str]:
        return [f"missing-{index:04d}.json" for index in range(count)]

    def test_the_validator_really_rejects_one_error_above_the_limit(self):
        """Premise: the rejection the built-in must avoid is live.

        Everything else in this class asserts that a saturated built-in result
        is *not* replaced by a contract error. That is only meaningful if the
        replacement happens to a result one error larger, so here it is,
        produced by a provider that ignores the producer-side stop.
        """
        digest, status, errors = compute_boundary(
            _HostileErrorProvider(MAX_PROVIDER_ERRORS + 1),
            self._context(["present.json"]),
        )
        self.assertIsNone(digest)
        self.assertEqual(status, "error")
        self.assertEqual(
            errors,
            [
                "Provider 'custom.hostile' returned an invalid result: errors "
                f"exceeds the {MAX_PROVIDER_ERRORS}-item limit"
            ],
        )

    def test_a_hostile_result_exactly_at_the_limit_survives_the_validator(self):
        """The validator's ``>`` and the producer's stop meet at 100 exactly."""
        digest, status, errors = compute_boundary(
            _HostileErrorProvider(MAX_PROVIDER_ERRORS),
            self._context(["present.json"]),
        )
        self.assertIsNone(digest)
        self.assertEqual(status, "error")
        self.assertEqual(len(errors), MAX_PROVIDER_ERRORS)
        self.assertEqual(errors[0], "synthetic error 0")

    def test_a_saturated_builtin_result_passes_compute_boundary_unchanged(self):
        for label, (declared, expected, sentinel) in ERROR_SATURATION.items():
            with self.subTest(case=label, declared=declared):
                digest, status, errors = compute_boundary(
                    PathHashProvider(), self._context(self._missing(declared))
                )
                self.assertIsNone(digest)
                self.assertEqual(status, "error")
                self.assertEqual(len(errors), expected)
                self.assertEqual(
                    errors[0],
                    "Declared boundary path matched no tracked files: "
                    "missing-0000.json",
                )
                if sentinel:
                    self.assertEqual(errors[-1], SENTINEL)
                else:
                    self.assertNotIn(SENTINEL, errors)
                for message in errors:
                    self.assertNotIn("returned an invalid result", message)

    def test_the_saturated_list_reaching_compute_boundary_is_the_one_resolve_built(self):
        """The direct call and the hashed call must not disagree.

        The existing suite checks ``resolve()`` alone. The coupling this
        obligation names only shows up when the same list crosses the
        validator, so both are read here and compared.
        """
        context = self._context(self._missing(250))
        resolved = PathHashProvider().resolve(context)
        _, status, errors = compute_boundary(PathHashProvider(), self._context(
            self._missing(250)
        ))
        self.assertEqual(resolved.status, status)
        self.assertEqual(resolved.errors, errors)
        self.assertEqual(len(errors), MAX_PROVIDER_ERRORS)

    def test_metadata_callers_see_the_same_saturated_list(self):
        result = compute_boundary(
            PathHashProvider(),
            self._context(self._missing(250)),
            include_metadata=True,
        )
        self.assertEqual(len(result), 4)
        digest, status, errors, metadata = result
        self.assertIsNone(digest)
        self.assertEqual(status, "error")
        self.assertEqual(len(errors), MAX_PROVIDER_ERRORS)
        self.assertEqual(errors[-1], SENTINEL)
        self.assertIsNone(metadata)


class _ExplainRecorder:
    """Records every ``explain_diff`` call made through a patched provider."""

    def __init__(self, answer: str = "RECORDED PROVIDER EXPLANATION") -> None:
        self.answer = answer
        self.calls: List[Tuple[str, object, object]] = []

    def install(self):
        recorder = self

        def explain_diff(provider_self, old_metadata, new_metadata, ctx):
            recorder.calls.append(
                (provider_self.name, old_metadata, new_metadata)
            )
            return recorder.answer

        return mock.patch.object(PathHashProvider, "explain_diff", explain_diff)


def _drift_scene() -> Scenario:
    scene = Scenario()
    scene.component(
        "svc",
        path="svc",
        provider="path-hash",
        boundary=["api.json"],
        version_source={"file": "package.json", "field": "version"},
    )
    scene.json_file("svc/api.json", {"a": 1})
    scene.file("svc/notes.txt", "one\n")
    scene.json_file("svc/package.json", {"version": "1.0.0"})
    scene.commit()
    return scene


#: Each drift a component can show without its boundary moving, and the facet
#: the fixture expects it to land in.
NON_BOUNDARY_DRIFTS = {
    "exact only": (
        lambda scene: scene.file("svc/notes.txt", "two\n"),
        {"exact"},
    ),
    "compat and version": (
        lambda scene: scene.json_file("svc/package.json", {"version": "2.0.0"}),
        {"exact", "compat"},
    ),
}


class ProviderExplanationGateTests(unittest.TestCase):
    """OBL-PROVIDERS-056: whether untrusted explanation code runs at all."""

    def _analyze(self, scene: Scenario, lockfile: dict, **kwargs) -> dict:
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            result = analyze_component_drift(
                scene.config,
                lockfile,
                scene.root,
                "svc",
                source="working-tree",
                **kwargs,
            )
        self.assertEqual(buffer.getvalue(), "")
        self.assertIsNotNone(result)
        return result

    def test_a_boundary_change_calls_the_provider_exactly_once(self):
        """Premise: the recorder is wired to the path the gate protects.

        The two non-invocation tests below assert an empty call list. This one
        shows the same patched provider, the same fixture and the same entry
        point produce one recorded call when the boundary does move, so an
        empty list means the gate held rather than that the recorder was never
        installed.
        """
        recorder = _ExplainRecorder()
        with _drift_scene() as scene:
            lockfile = generate_lockfile(scene.config, scene.root, source="head")
            scene.json_file("svc/api.json", {"a": 2})
            with recorder.install():
                result = self._analyze(scene, lockfile)
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(recorder.calls[0][0], "path-hash")
        self.assertIn("boundary", result["changes"])
        self.assertEqual(
            result["provider_explanation"], "RECORDED PROVIDER EXPLANATION"
        )

    def test_a_boundary_metadata_change_alone_also_opens_the_gate(self):
        """Premise: the gate has two openers, and both are live.

        ``boundary_metadata`` is the half of the condition that fires with no
        fingerprint movement at all, so it is exercised on a lockfile whose
        digests match the working tree exactly.
        """
        recorder = _ExplainRecorder()
        with _drift_scene() as scene:
            lockfile = generate_lockfile(scene.config, scene.root, source="head")
            tampered = copy.deepcopy(lockfile)
            tampered["components"]["svc"]["boundary_metadata"] = {"note": "stale"}
            with recorder.install():
                result = self._analyze(scene, tampered)
        self.assertEqual(result["changes"], {})
        self.assertIn("boundary_metadata", result["metadata_changes"])
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(
            recorder.calls[0][1:], ({"note": "stale"}, None)
        )
        self.assertEqual(
            result["provider_explanation"], "RECORDED PROVIDER EXPLANATION"
        )

    def test_drift_outside_the_boundary_never_calls_the_provider(self):
        for label, (mutate, expected) in NON_BOUNDARY_DRIFTS.items():
            with self.subTest(drift=label):
                recorder = _ExplainRecorder()
                with _drift_scene() as scene:
                    lockfile = generate_lockfile(
                        scene.config, scene.root, source="head"
                    )
                    mutate(scene)
                    with recorder.install():
                        result = self._analyze(scene, lockfile)
                self.assertEqual(set(result["changes"]), expected)
                self.assertNotIn("boundary", result["changes"])
                self.assertNotIn("boundary_metadata", result["metadata_changes"])
                self.assertEqual(recorder.calls, [])
                self.assertEqual(result["provider_explanation"], "")

    def test_an_unchanged_component_never_calls_the_provider(self):
        recorder = _ExplainRecorder()
        with _drift_scene() as scene:
            lockfile = generate_lockfile(scene.config, scene.root, source="head")
            with recorder.install():
                result = self._analyze(scene, lockfile)
        self.assertEqual(result["changes"], {})
        self.assertEqual(result["metadata_changes"], {})
        self.assertEqual(recorder.calls, [])
        self.assertEqual(result["provider_explanation"], "")

    def test_the_json_view_publishes_the_same_empty_provider_detail(self):
        """The field an operator reads is the one the gate controls."""
        with _drift_scene() as scene:
            self.assertEqual(run_cli(scene.root, "generate").returncode, 0)
            scene.git("add", "--all")
            scene.git("commit", "-m", "lock")
            scene.file("svc/notes.txt", "two\n")
            quiet = run_cli(
                scene.root, "why", "svc", "--source", "working-tree",
                "--format", "json",
            )
            scene.json_file("svc/api.json", {"a": 2})
            loud = run_cli(
                scene.root, "why", "svc", "--source", "working-tree",
                "--format", "json",
            )
        quiet_payload = json.loads(quiet.stdout)
        loud_payload = json.loads(loud.stdout)
        self.assertEqual(quiet_payload["provider_detail"], "")
        self.assertEqual(sorted(quiet_payload["changes"]), ["exact"])
        self.assertEqual(
            loud_payload["provider_detail"], "declared boundary artifact changed"
        )
        self.assertIn("boundary", loud_payload["changes"])


#: Every way the obligation says a boundary provider can fail to resolve, and
#: the diagnostic the run must carry. The value is (config mutation, extra
#: kwargs, expected stderr fragments).
UNRESOLVABLE_PROVIDERS = {
    "provider key removed": (
        lambda config: config["components"]["svc"]["boundary"].pop("provider"),
        {},
        ("Lockfile generation failed:", "Unknown boundary provider: 'unknown'"),
    ),
    "boundary block removed": (
        lambda config: config["components"]["svc"].pop("boundary"),
        {},
        ("Lockfile generation failed:", "Unknown boundary provider: 'unknown'"),
    ),
    "boundary block emptied": (
        lambda config: config["components"]["svc"].__setitem__("boundary", {}),
        {},
        ("Lockfile generation failed:", "Unknown boundary provider: 'unknown'"),
    ),
    "unregistered provider name": (
        lambda config: config["components"]["svc"]["boundary"].__setitem__(
            "provider", "does-not-exist"
        ),
        {},
        (
            "Lockfile generation failed:",
            "Unknown boundary provider: 'does-not-exist'",
        ),
    ),
    "custom provider not allowed": (
        lambda config: config.__setitem__(
            "providers", [{"module": "boundver_chunk38_absent", "class": "P"}]
        ),
        {},
        (
            "Custom provider loading failed:",
            "Config declares custom providers but loading is not enabled.",
        ),
    ),
    "custom provider module missing": (
        lambda config: config.__setitem__(
            "providers", [{"module": "boundver_chunk38_absent", "class": "P"}]
        ),
        {"allow_custom_providers": True},
        (
            "Custom provider loading failed:",
            "Failed to import provider module 'boundver_chunk38_absent'",
        ),
    ),
}


class UnresolvableBoundaryProviderTests(unittest.TestCase):
    """OBL-PROVIDERS-057: a stale obligation, pinned to what really happens.

    The register expected `why` to swallow a provider load failure and show an
    empty Provider detail line. It does not: the failure is fatal earlier, and
    the operator gets an explicit stderr diagnostic and exit code 2.
    """

    def test_a_resolvable_provider_produces_a_non_empty_detail(self):
        """Premise: the fixture's boundary change reaches the provider.

        Every case below asserts the analysis returned None. This one shows
        the same repository, the same lockfile and the same boundary change
        return a dict whose provider detail is non-empty, so a None is caused
        by the config mutation and nothing else.
        """
        with _drift_scene() as scene:
            lockfile = generate_lockfile(scene.config, scene.root, source="head")
            scene.json_file("svc/api.json", {"a": 2})
            buffer = io.StringIO()
            with redirect_stderr(buffer):
                result = analyze_component_drift(
                    scene.config, lockfile, scene.root, "svc",
                    source="working-tree",
                )
        self.assertEqual(buffer.getvalue(), "")
        self.assertIsNotNone(result)
        self.assertIn("boundary", result["changes"])
        self.assertEqual(
            result["provider_explanation"], "declared boundary artifact changed"
        )

    def test_every_unresolvable_provider_aborts_with_a_named_diagnostic(self):
        with _drift_scene() as scene:
            lockfile = generate_lockfile(scene.config, scene.root, source="head")
            scene.json_file("svc/api.json", {"a": 2})
            for label, (mutate, kwargs, fragments) in UNRESOLVABLE_PROVIDERS.items():
                with self.subTest(failure=label):
                    config = copy.deepcopy(scene.config)
                    mutate(config)
                    buffer = io.StringIO()
                    with redirect_stderr(buffer):
                        result = analyze_component_drift(
                            config, lockfile, scene.root, "svc",
                            source="working-tree", **kwargs,
                        )
                    self.assertIsNone(result)
                    stderr = buffer.getvalue()
                    self.assertIn(
                        "ERROR: could not compute current fingerprints:", stderr
                    )
                    for fragment in fragments:
                        self.assertIn(fragment, stderr)

    def test_load_custom_providers_really_reports_each_of_those_failures(self):
        """Premise: the loader the obligation names does fail for these configs.

        Without this, "the run aborted" could mean the config was rejected for
        some unrelated reason and the provider loader never ran.
        """
        entry = [{"module": "boundver_chunk38_absent", "class": "P"}]
        blocked = load_custom_providers(
            entry, allow_custom=False, registry=create_registry()
        )
        self.assertEqual(
            blocked,
            [
                "Config declares custom providers but loading is not enabled. "
                "Pass --allow-custom-providers (or the equivalent trusted API "
                "argument)."
            ],
        )
        unimportable = load_custom_providers(
            entry, allow_custom=True, registry=create_registry()
        )
        self.assertEqual(len(unimportable), 1)
        self.assertIn(
            "Failed to import provider module 'boundver_chunk38_absent'",
            unimportable[0],
        )

    def test_why_exits_two_and_prints_the_reason_rather_than_an_empty_detail(self):
        """The operator-visible half of the finding.

        A run whose provider cannot be loaded does not read like a healthy run
        with a terse provider. It emits a structured error, prints the detailed
        reason on stderr, and exits 2.
        """
        with _drift_scene() as scene:
            self.assertEqual(run_cli(scene.root, "generate").returncode, 0)
            scene.git("add", "--all")
            scene.git("commit", "-m", "lock")
            scene.json_file("svc/api.json", {"a": 2})
            healthy = run_cli(
                scene.root, "why", "svc", "--source", "working-tree",
                "--format", "json",
            )
            scene.config["providers"] = [
                {"module": "boundver_chunk38_absent", "class": "P"}
            ]
            scene.write_config()
            broken = run_cli(
                scene.root, "why", "svc", "--source", "working-tree",
                "--format", "json",
            )
        self.assertEqual(healthy.returncode, 1)
        self.assertEqual(
            json.loads(healthy.stdout)["provider_detail"],
            "declared boundary artifact changed",
        )
        self.assertEqual(broken.returncode, 2)
        self.assertEqual(
            json.loads(broken.stdout),
            {
                "component": "svc",
                "error": "component drift analysis failed; see stderr",
                "known_components": ["svc"],
            },
        )
        self.assertIn(
            "ERROR: could not compute current fingerprints:", broken.stderr
        )
        self.assertIn("Custom provider loading failed:", broken.stderr)

    def test_the_fatal_fingerprint_computation_precedes_the_provider_gate(self):
        """Why the failure is loud, pinned so a reordering cannot make it quiet.

        ``load_errors`` is genuinely discarded - the register is right about
        that - but it can never hold a value, because ``generate_lockfile``
        raises on the same loader errors first and the caller turns that into
        a printed diagnostic and a None. If the two calls ever swap places, or
        the fingerprint failure stops being fatal, the silent degradation the
        obligation feared becomes real, and this test is what notices.
        """
        source = textwrap.dedent(inspect.getsource(analyze_component_drift))
        tree = ast.parse(source).body[0]
        calls = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls.setdefault(node.func.id, node.lineno)
        self.assertIn("generate_lockfile", calls)
        self.assertIn("load_custom_providers", calls)
        self.assertLess(calls["generate_lockfile"], calls["load_custom_providers"])

        contexts = [
            type(node.ctx).__name__
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id == "load_errors"
        ]
        self.assertEqual(contexts, ["Store", "Load"])


def _policy_component(
    facets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "path": "p",
        "boundary": {"provider": "path-hash", "paths": ["api.json"]},
    }
    if facets is not None:
        entry["verify_facets"] = list(facets)
    return entry


#: Slice memberships against the ``boundary`` mode, and whether the slice gates
#: with no ``--facets``. ``mixed`` is the case that separates any() from all().
SLICE_MEMBERSHIPS = {
    "one member that gates": (["gates"], True),
    "one member that does not": (["abstains"], False),
    "mixed membership": (["gates", "abstains"], True),
    "two members, neither gates": (["abstains", "also_abstains"], False),
    "two members, both gate": (["gates", "also_gates"], True),
    "no members": ([], False),
    "member that does not exist": (["absent"], False),
}

POLICY_CONFIG = {
    "project": "policy",
    "components": {
        "gates": _policy_component(["boundary"]),
        "also_gates": _policy_component(["boundary"]),
        "abstains": _policy_component(["exact"]),
        "also_abstains": _policy_component(["exact"]),
    },
    "slices": {
        name: {"mode": "boundary", "components": list(members)}
        for name, (members, _) in SLICE_MEMBERSHIPS.items()
    },
}


class SliceGatingPolicyTests(unittest.TestCase):
    """OBL-CONFIG-030: any-member gating, and the explicit override above it."""

    def test_the_fixture_really_splits_its_components_by_effective_facets(self):
        """Premise: "gates" and "abstains" differ where the rule reads them.

        Every gating assertion below depends on two components disagreeing
        about whether ``boundary`` is in their effective facets. If the
        override were ignored the whole table would collapse into one case.
        """
        payload = facet_policy_payload(POLICY_CONFIG, None)
        self.assertEqual(
            payload["components"],
            {
                "abstains": ["exact"],
                "also_abstains": ["exact"],
                "also_gates": ["boundary"],
                "gates": ["boundary"],
            },
        )
        self.assertIsNone(payload["explicit"])
        self.assertIsNone(payload["defaults"])

    def test_a_slice_gates_when_any_resolved_member_gates_the_mode(self):
        payload = facet_policy_payload(POLICY_CONFIG, None)
        for name, (members, expected) in SLICE_MEMBERSHIPS.items():
            with self.subTest(slice=name, members=members):
                self.assertEqual(
                    payload["slices"][name],
                    {"mode": "boundary", "gated": expected},
                )

    def test_explicit_facets_make_gating_independent_of_membership(self):
        gated = facet_policy_payload(POLICY_CONFIG, ["boundary"])
        ungated = facet_policy_payload(POLICY_CONFIG, ["exact", "compat"])
        for name in SLICE_MEMBERSHIPS:
            with self.subTest(slice=name):
                self.assertTrue(gated["slices"][name]["gated"])
                self.assertFalse(ungated["slices"][name]["gated"])
        self.assertEqual(gated["explicit"], ["boundary"])
        self.assertEqual(ungated["explicit"], ["exact", "compat"])

    def test_the_explicit_list_overrides_every_component_override_too(self):
        payload = facet_policy_payload(POLICY_CONFIG, ["boundary"])
        self.assertEqual(
            payload["components"],
            {
                "abstains": ["boundary"],
                "also_abstains": ["boundary"],
                "also_gates": ["boundary"],
                "gates": ["boundary"],
            },
        )

    def test_the_published_policy_view_reports_the_same_gating(self):
        """The end-to-end half: what a team reads out of ``verify --format json``.

        Config validation applies the all-member *availability* rule to slice
        members - every member must be capable of a boundary digest - which is
        a different question from whether the slice gates. Both members here
        are capable and only their policy differs, so the slice is valid and
        the gating answer is the one under test.
        """
        with Scenario() as scene:
            scene.component(
                "gates", path="gates", boundary=["api.json"],
                verify_facets=["boundary"],
            )
            scene.component(
                "abstains", path="abstains", boundary=["api.json"],
                verify_facets=["exact"],
            )
            scene.slice("mixed", mode="boundary", components=["gates", "abstains"])
            scene.slice("only_abstains", mode="boundary", components=["abstains"])
            scene.json_file("gates/api.json", {"a": 1})
            scene.json_file("abstains/api.json", {"b": 2})
            scene.commit()
            self.assertEqual(run_cli(scene.root, "generate").returncode, 0)
            scene.git("add", "--all")
            scene.git("commit", "-m", "lock")
            answers = {}
            for label, extra in (
                ("no --facets", ()),
                ("--facets boundary", ("--facets", "boundary")),
                ("--facets exact", ("--facets", "exact")),
            ):
                result = run_cli(scene.root, "verify", "--format", "json", *extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                answers[label] = json.loads(result.stdout)["facet_policy"]["slices"]

        self.assertEqual(
            answers["no --facets"],
            {
                "mixed": {"mode": "boundary", "gated": True},
                "only_abstains": {"mode": "boundary", "gated": False},
            },
        )
        self.assertEqual(
            answers["--facets boundary"],
            {
                "mixed": {"mode": "boundary", "gated": True},
                "only_abstains": {"mode": "boundary", "gated": True},
            },
        )
        self.assertEqual(
            answers["--facets exact"],
            {
                "mixed": {"mode": "boundary", "gated": False},
                "only_abstains": {"mode": "boundary", "gated": False},
            },
        )


if __name__ == "__main__":
    unittest.main()
